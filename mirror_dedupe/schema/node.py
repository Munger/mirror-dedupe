## @file node.py
##
## @brief Dict-backed object graph primitives with snapshot/restore support.
##
## Defines two core building blocks:
##
## * ``Node``     — a thin wrapper over ``dict`` that routes most attribute
##   access into an underlying mapping while supporting snapshot/restore and
##   optional structural metadata for child nodes.
## * ``NodeList`` — a list-like container for ``Node`` subclasses with
##   matching snapshot/restore helpers.
##
## The snapshot format is transport-agnostic: objects are converted to plain
## Python dict/list/scalar structures that can be stored or transmitted in any
## encoding (JSON, YAML, msgpack, databases, etc.) and later restored into a
## typed object graph.
##
## @copyright Copyright (c) 2026 Tim Hosking
## @see https://github.com/munger
## @par Licence: MIT

from __future__ import annotations

import hashlib
import io
import json
import os
import threading
from pathlib import Path
from typing import Any, ClassVar, Dict, List, Generic, Iterable, Optional, Tuple, Type, TypeVar, Callable

from ..lib import fmt_size, LOG_LABEL_W
from ..lib.http_download import HTTPFetch, HTTPDownload, kill_active_subprocesses
from ..lib.log import log
from ..repo_vars import RepoVars

# Lock map: staging_key -> (per-hash lock, refcount).  Refcount tracks
# concurrent threads waiting on the same hash so the entry can be pruned
# when the last thread releases it, avoiding unbounded dict growth.
_staging_locks: dict[str, tuple[threading.Lock, int]] = {}
_staging_locks_lock = threading.Lock()


def _pool_path(pool_root: str, hash_val: str) -> Path:
    ## @brief Build the deterministic pool path for a hash value.
    ##
    ## Pool layout is ``<pool_root>/by-hash/SHA256/<ab>/<cd>/<full_hash>``
    ## where ``ab`` and ``cd`` are the first two and second two hex chars
    ## of the hash.  This fan-out avoids millions of files in one directory.
    ##
    ## @param pool_root  Root directory of the content-addressed pool.
    ## @param hash_val   64-char SHA-256 hex digest.
    ## @return Absolute ``Path`` to the pool entry.
    return Path(pool_root) / "by-hash" / "SHA256" / hash_val[:2] / hash_val[2:4] / hash_val


def _sync_link(pool_path: Path, dest: Path, rv: RepoVars, hash_val: str) -> int:
    ## @brief Hardlink a pool file into a repo destination directory.
    ##
    ## Creates the parent directory tree if needed, removes any existing
    ## file at *dest*, links *pool_path* → *dest*, and records the
    ## mapping in the repo inventory so future lookups are fast.
    ##
    ## @param pool_path  Existing file in the content-addressed pool.
    ## @param dest       Destination path under the repo root.
    ## @param rv         ``RepoVars`` containing the repo inventory.
    ## @param hash_val   SHA-256 hex digest of the content.
    ## @return The inode number of the linked file.
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.unlink(missing_ok=True)
    os.link(pool_path, dest)
    ino = dest.stat().st_ino
    if rv.inv is not None:
        rv.inv.add(hash_val, ino)
    return ino


class Node(dict):
    ## @brief Base class for all dict-backed schema nodes.
    ##
    ## This is a thin wrapper over ``dict`` so that higher-level schema
    ## objects can share helpers like ``to_plain`` / ``to_pretty_json``.
    ## Subclasses are free to shape their payloads as needed; there is no
    ## enforced ``_key`` field or child tree logic here.

    _reserved: ClassVar[set[str]] = set()
    _restore_via_payload: ClassVar[bool] = False
    _node_fields: ClassVar[Dict[str, Type["Node"]]] = {}
    _list_fields: ClassVar[Dict[str, Tuple[Type["NodeList"], Type["Node"]]]] = {}
    _children: ClassVar[List[str]] = []
    ## @brief Names of attributes holding child nodes to recurse into
    ##        during ``parse()``.  Subclasses override to declare their
    ##        structural children (e.g. ``["distributions"]``,
    ##        ``["release"]``, ``["indices"]``).

    def __init_subclass__(cls, **kwargs: Any) -> None:
        ## @brief Collect reserved attribute names for each subclass.
        ##
        ## Methods, class attributes, properties, and annotated fields
        ## are treated as real attributes rather than schema payload keys.
        ##
        ## @param kwargs  Keyword arguments forwarded to ``super().__init_subclass__``.
        ## @return None

        super().__init_subclass__(**kwargs)
        reserved: set[str] = set()

        for base in cls.mro():
            for name, value in getattr(base, "__dict__", {}).items():
                if not name.startswith("_") and not isinstance(value, property):
                    reserved.add(name)

            for name in getattr(base, "__annotations__", {}).keys():
                if not name.startswith("_"):
                    reserved.add(name)

        reserved.update({"_frozen", "_reserved"})
        cls._reserved = reserved

    def __getattr__(self, name: str) -> Any:
        ## @brief Fallback attribute access to mapping keys.
        ##
        ## Allows ``node.foo`` to behave like ``node["foo"]`` for schema
        ## data, while still supporting normal attributes for internals.
        ##
        ## @param name  The attribute name to look up.
        ## @return The value mapped to *name*, or raises ``AttributeError``.

        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name)

    def __setattr__(self, name: str, value: Any) -> None:
        ## @brief Route most attribute writes into the underlying mapping.
        ##
        ## Attributes whose names start with an underscore or are in the
        ## per-class ``_reserved`` set are treated as true attributes on the
        ## instance. All other names are stored in the dict payload under the
        ## same key, after honouring the frozen check.
        ##
        ## @param name   The attribute name.
        ## @param value  The value to store.
        ## @return None

        if name.startswith("_") or name in type(self)._reserved:
            object.__setattr__(self, name, value)
        else:
            self._check_frozen()
            self[name] = value

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        ## @brief Construct a Node backed by an underlying mapping.
        ##
        ## Optionally accepts a single positional ``dict`` which is used to
        ## seed the mapping; all keyword arguments are then routed through
        ## ``__setattr__`` so they become schema fields by default.
        ##
        ## @param args  Optional single positional dict payload.
        ## @param kwargs  Keyword arguments stored as schema fields.

        super().__init__()
        object.__setattr__(self, "_cache", None)
        object.__setattr__(self, "_repo_vars", None)

        if args:
            if len(args) > 1:
                raise TypeError(
                    "Node accepts at most one positional argument: a mapping "
                    "payload. Pass a single dict or use keyword arguments for "
                    "individual fields."
                )
            initial = args[0]
            if isinstance(initial, dict):
                for k, v in initial.items():
                    self[k] = v
            else:
                raise TypeError(
                    "Positional argument to Node must be a dict-like mapping. "
                    "Use Node(mapping) or Node(key=value, ...) instead of "
                    f"{type(initial)!r}."
                )

        for key, value in kwargs.items():
            setattr(self, key, value)

    def to_plain(self) -> Any:
        ## @brief Return a JSON-serialisable structure for this Node tree.
        ##
        ## Walks the mapping and any nested Nodes/lists/dicts, converting
        ## Node instances to plain dicts recursively.
        ##
        ## @return A plain dict/list/scalar structure.

        def convert(value: Any) -> Any:
            ## @brief Recursively convert Node → dict, list → list, scalar → identity.
            ## @param value  Any value in the tree.
            ## @return Plain Python data structure (no Node instances).
            if isinstance(value, Node):
                return {k: convert(v) for k, v in value.items()}
            if isinstance(value, list):
                return [convert(v) for v in value]
            if isinstance(value, dict):
                return {k: convert(v) for k, v in value.items()}
            return value

        return convert(self)

    def to_pretty_json(self, indent: int = 2) -> str:
        ## @brief Return an indented JSON string representation.
        ## @param indent  Number of spaces per indent level (default 2).
        ## @return Pretty-printed JSON string.

        return json.dumps(self.to_plain(), indent=indent)

    def snapshot(self) -> Any:
        ## @brief Return a JSON-serialisable snapshot of this Node tree.
        ##
        ## Scalar fields are emitted first in their original insertion order,
        ## followed by nested structures (dicts/lists).  This keeps snapshots
        ## readable without requiring each subclass to define its own ordering.
        ##
        ## @return Plain dict/list structure suitable for JSON/YAML.

        plain = self.to_plain()
        if not isinstance(plain, dict):
            return plain

        scalars: Dict[str, Any] = {}
        nested: Dict[str, Any] = {}

        for key, value in plain.items():
            if isinstance(value, (dict, list)):
                nested[key] = value
            else:
                scalars[key] = value

        ordered: Dict[str, Any] = {}
        ordered.update(scalars)
        ordered.update(nested)
        return ordered

    @classmethod
    def restore(cls, snapshot: Any) -> "Node":
        ## @brief Rebuild a Node (or subclass) from a snapshot payload.
        ##
        ## The default implementation assumes *snapshot* is a mapping whose
        ## keys/values should seed the Node's dict payload as-is.  Subclasses
        ## that need to rebuild richer structure should override this method.
        ##
        ## @param snapshot  Plain dict/list structure from an earlier ``snapshot()`` call.
        ## @return A reconstructed Node instance.
        ## @raise TypeError  If the snapshot type is unsupported or construction fails.

        if isinstance(snapshot, dict):
            if getattr(cls, "_restore_via_payload", False):
                return cls._from_payload(snapshot)

            try:
                node = cls(snapshot)
            except TypeError as exc:
                raise TypeError(
                    f"{cls.__name__}.restore() could not call "
                    f"{cls.__name__}.__init__ with a single mapping payload. "
                    f"Either set `_restore_via_payload = True` on {cls.__name__} "
                    f"for payload-based restore, or provide a custom "
                    f"{cls.__name__}.restore() implementation."
                ) from exc

            return node
        raise TypeError(
            f"{cls.__name__}.restore() expected a mapping payload (e.g. the "
            f"result of {cls.__name__}.snapshot()), got {type(snapshot)!r}. "
            "Ensure you are passing plain dict/list/scalar data rather than "
            "an already-instantiated object."
        )

    @classmethod
    def _from_payload(cls, payload: Dict[str, Any]) -> "Node":
        ## @brief Create a Node from a plain payload dict without parsing.
        ##
        ## Bypasses the normal ``__init__`` to avoid side effects —
        ## used during snapshot restore where the payload already contains
        ## all fields.
        ##
        ## @param payload  Plain dict of field values.
        ## @return A new Node instance.
        self = cls.__new__(cls)
        Node.__init__(self, payload)
        return self

    @classmethod
    def _restore_children(cls, node: "Node", snapshot: Any) -> None:
        ## @brief Generic helper to rebuild declared child Nodes/NodeLists.
        ##
        ## Subclasses opt in by populating ``_node_fields`` and/or
        ## ``_list_fields`` and then calling ``cls._restore_children`` from their
        ## own ``restore``/factory methods after constructing *node*.
        ##
        ## @param node     The parent Node whose children should be restored.
        ## @param snapshot  Plain dict snapshot containing child data.
        ## @return None

        if not isinstance(snapshot, dict):
            return

        for field, child_cls in getattr(cls, "_node_fields", {}).items():
            raw = snapshot.get(field)
            if raw is not None:
                setattr(node, field, child_cls.restore(raw))

        for field, (list_cls, item_cls) in getattr(cls, "_list_fields", {}).items():
            items = snapshot.get(field, [])
            setattr(node, field, list_cls.restore(items, item_type=item_cls))

    def _check_frozen(self) -> None:
        ## @brief Raise if this node has been frozen.
        ## @raise TypeError  If the node is frozen.
        ## @return None

        if getattr(self, "_frozen", False):
            raise TypeError(
                f"{type(self).__name__} is frozen and cannot be modified. "
                "Call `thaw(deep=...)` before mutating, or avoid mutating "
                "frozen snapshots that are being shared across threads."
            )

    def freeze(self, *, deep: bool = True) -> None:
        ## @brief Mark this node (and optionally its children) as frozen.
        ##
        ## When ``deep`` is True, recurse into nested Nodes and NodeLists so
        ## the entire tree becomes immutable at the schema layer.
        ##
        ## @param deep  Whether to recursively freeze child nodes (default True).
        ## @return None

        object.__setattr__(self, "_frozen", True)
        if deep:
            self._walk_child_nodes(
                lambda n: n.freeze(deep=True),
                lambda lst: lst.freeze(deep=False),
            )

    def thaw(self, *, deep: bool = True) -> None:
        ## @brief Clear the frozen flag on this node (and optionally children).
        ##
        ## Restores mutability after a previous ``freeze()`` call.
        ##
        ## @param deep  Whether to recursively thaw child nodes (default True).
        ## @return None

        object.__setattr__(self, "_frozen", False)
        if deep:
            self._walk_child_nodes(
                lambda n: n.thaw(deep=True),
                lambda lst: lst.thaw(deep=False),
            )

    def _walk_child_nodes(
        self,
        func: Callable[["Node"], None],
        list_func: Optional[Callable[["NodeList"], None]] = None,
    ) -> None:
        ## @brief Walk the subtree, applying *func* to every descendant Node
        ##        and *list_func* (if supplied) to every descendant NodeList.
        ##
        ## Callers that need to propagate a flag onto NodeList containers —
        ## e.g. the frozen flag during ``freeze()``/``thaw()`` — should supply
        ## *list_func*.  Without it only Node instances are touched.
        ##
        ## The ``elif`` chain below is deliberately ordered Node → NodeList →
        ## list → dict.  Two earlier bugs are fixed here:
        ##
        ##  1. The old code used ``getattr(nodelist, func.__name__, None)`` to
        ##     find the matching method on each NodeList.  Lambdas always have
        ##     ``__name__ == '<lambda>'``, so the lookup always returned ``None``
        ##     and NodeLists were never frozen or thawed.  The fix is an explicit
        ##     *list_func* parameter.
        ##
        ##  2. The old ``if isinstance(value, Node)`` was a standalone ``if``
        ##     (not ``elif``), so Node values — which are also ``dict`` subclasses
        ##     — fell through into the ``elif isinstance(value, dict)`` branch and
        ##     had their subtrees traversed a second time.  For deep trees this
        ##     caused exponential duplicate work.  Made ``elif`` to fix it.
        ##
        ## @param func       Callable applied to each descendant ``Node``.
        ## @param list_func  Optional callable applied to each descendant
        ##                   ``NodeList`` (called with ``deep=False`` by
        ##                   convention — the caller owns recursion).
        ## @return None

        def recurse(value: Any) -> None:
            ## @brief Dispatch a single value by type and recurse as needed.
            ## @param value  Any value encountered while walking the payload.
            ## @return None
            if isinstance(value, Node):
                # Node is a dict subclass; using elif here prevents the
                # ``isinstance(value, dict)`` branch below from also firing
                # and re-traversing the subtree (fix for bug #2 above).
                func(value)
            elif isinstance(value, NodeList):
                if list_func is not None:
                    list_func(value)
                for v in value:
                    recurse(v)
            elif isinstance(value, list):
                for v in value:
                    recurse(v)
            elif isinstance(value, dict):
                for v in value.values():
                    recurse(v)

        for val in self.values():
            recurse(val)

    def merge(self, other: Dict[str, Any] | "Node", *, extend_lists: bool = False) -> "Node":
        ## @brief Merge another mapping or Node into this Node recursively.
        ##
        ## Node fields are merged recursively, plain dicts are merged
        ## shallowly at their level, lists are either overwritten (default)
        ## or extended when ``extend_lists`` is True, and scalars are
        ## overwritten.
        ##
        ## @param other         The mapping or Node to merge into this one.
        ## @param extend_lists  Whether to extend lists instead of overwriting.
        ## @return This Node after the merge.
        ## @raise TypeError  If *other* is not a mapping.

        self._check_frozen()
        try:
            items = other.items()
        except AttributeError as exc:
            raise TypeError(
                "Node.merge() expected a mapping or Node instance as `other`, "
                f"got {type(other)!r}. Pass a dict-like object or another Node, "
                "or convert your input to a mapping before merging."
            ) from exc

        for key, value in items:
            if key in self:
                current = self[key]
                if isinstance(current, Node) and isinstance(value, (Node, dict)):
                    current.merge(value, extend_lists=extend_lists)
                elif isinstance(current, dict) and isinstance(value, dict):
                    for k, v in value.items():
                        current[k] = v
                elif extend_lists and isinstance(current, list) and isinstance(value, list):
                    current.extend(value)
                else:
                    self[key] = value
            else:
                self[key] = value

        return self

    def clone(self) -> "Node":
        ## @brief Deep-clone this Node and any nested Nodes.
        ##
        ## Preserves runtime attributes (e.g. loaders, caches) and avoids
        ## calling ``__init__`` on subclasses, so ``Loadable`` nodes and
        ## custom constructors remain valid on the clone.
        ##
        ## @return A deep copy of this Node.

        return self._clone_recursive({})

    def _clone_recursive(self, memo: Dict[int, "Node"]) -> "Node":
        ## @brief Deep-clone this node using an identity memo dict.
        ##
        ## Uses ``object.__new__`` and ``dict.__init__`` to bypass the
        ## subclass ``__init__`` (which may have side effects) while
        ## preserving the subclass type.  Shared references (same Node
        ## reachable through multiple paths) are preserved via the memo.
        ##
        ## @param memo  Dict mapping ``id(original) → clone`` for cycle
        ##              and shared-reference detection.
        ## @return A deep copy of this Node.
        if not isinstance(self, dict):
            raise TypeError(
                "Node subclass must remain dict-backed for clone() to work. "
                "If you override the base type, ensure your class still "
                "inherits from dict or override clone() with custom logic."
            )

        obj_id = id(self)
        if obj_id in memo:
            return memo[obj_id]

        new = object.__new__(type(self))
        memo[obj_id] = new
        dict.__init__(new, {})

        def clone_value(value: Any) -> Any:
            ## @brief Deep-clone a single value, recursing into Node/list/dict.
            ## @param value  Any value in the tree.
            ## @return Deep clone of *value*.
            if isinstance(value, Node):
                return value._clone_recursive(memo)
            if isinstance(value, list):
                return [clone_value(v) for v in value]
            if isinstance(value, dict):
                return {k: clone_value(v) for k, v in value.items()}
            return value

        for k, v in self.items():
            dict.__setitem__(new, k, clone_value(v))

        for attr, val in self.__dict__.items():
            object.__setattr__(new, attr, val)

        return new

    def __setitem__(self, key: Any, value: Any) -> None:
        ## @brief Prevent payload keys from colliding with reserved attribute names.
        ## @param key    The key to set in the payload.
        ## @param value  The value to associate with *key*.
        ## @return None

        if key in type(self)._reserved:
            raise KeyError(f"Cannot set reserved attribute {key!r} in Node payload")
        self._check_frozen()
        super().__setitem__(key, value)

    def __delitem__(self, key: Any) -> None:
        ## @brief Delete a key from the payload after checking frozen state.
        ## @param key  The key to delete.
        ## @return None

        self._check_frozen()
        super().__delitem__(key)

    def clear(self) -> None:
        ## @brief Remove all items from the payload after checking frozen state.
        ## @return None

        self._check_frozen()
        super().clear()

    def pop(self, key: Any, *args: Any) -> Any:
        ## @brief Remove and return a payload item, with optional default.
        ## @param key    The key to pop from the payload.
        ## @param args   Optional default value if *key* is not found.
        ## @return The value for *key*, or the default if provided.

        self._check_frozen()
        return super().pop(key, *args)

    def popitem(self) -> Any:
        ## @brief Remove and return the last inserted payload item.
        ## @return A ``(key, value)`` tuple from the payload.

        self._check_frozen()
        return super().popitem()

    def update(self, *args: Any, **kwargs: Any) -> None:
        ## @brief Update the payload with key/value pairs from another mapping.
        ## @param args    Optional single mapping or iterable of ``(key, value)`` pairs.
        ## @param kwargs  Additional key/value pairs.
        ## @return None

        self._check_frozen()
        super().update(*args, **kwargs)

    # --- content operations -----------------------------------------------

    @classmethod
    def probe_url(cls, uri: str) -> Optional[bytes]:
        ## @brief Check whether a URL is reachable and return its content.
        ## @param uri  The URL to probe.
        ## @return The raw response bytes, or ``None`` if the URL is empty or unreachable.

        if not uri:
            return None
        try:
            return HTTPFetch(uri)
        except RuntimeError:
            return None



    def on_parse(self, *, config: Optional[Dict[str, Any]] = None) -> None:
        ## @brief Populate this node's schema and create child nodes.
        ##
        ## Called by ``parse()``.  Subclasses override to populate their
        ## own payload fields and attach child nodes via the attributes
        ## declared in ``_children``.  The base implementation is a no-op.
        ##
        ## @param config  Optional configuration dict (network settings,
        ##                filters, etc.).
        ## @return None
        pass

    def parse(self, *, config: Optional[Dict[str, Any]] = None) -> "Node":
        ## @brief Populate this node and recursively parse its children.
        ##
        ## Calls ``on_parse()`` to populate this node, then walks each
        ## attribute named in ``_children`` and calls ``parse()`` on each
        ## child node.  Children can be a ``NodeList`` of nodes or a
        ## single ``Node`` (e.g. a distribution's child Release).
        ##
        ## @param config  Optional configuration dict passed down to
        ##                all ``on_parse()`` calls.
        ## @return This node (for chaining).
        self.on_parse(config=config)
        self.recurse(config=config)
        return self

    def recurse(self, *, config: Optional[Dict[str, Any]] = None) -> "Node":
        ## @brief Walk declared children and call ``parse()`` on each.
        ##
        ## Skips this node's ``on_parse()`` — use when the tree already
        ## has its structural children created (e.g. sync mode builds
        ## distributions from config, then recurses to populate them).
        ##
        ## @param config  Optional configuration dict passed down to
        ##                all ``on_parse()`` calls on children.
        ## @return This node (for chaining).
        for attr in type(self)._children:
            children = getattr(self, attr, None)
            if children is None:
                continue
            if isinstance(children, NodeList):
                for child in children:
                    child.parse(config=config)
            elif isinstance(children, Node):
                children.parse(config=config)
        return self

    def _tree_iter(self):
        ## @brief Recursive generator yielding every node in this tree.
        ##
        ## Walks all children declared in ``_children`` in depth-first
        ## order, yielding each node (including self) exactly once.
        ## Used by ``_sync_content()`` to feed work items to the worker
        ## pool without building an intermediate list.
        ##
        ## @yield ``Node`` instances in depth-first order.
        ## @return A generator that iterates the full tree.
        yield self
        for attr in type(self)._children:
            children = getattr(self, attr, None)
            if children is None:
                continue
            if isinstance(children, NodeList):
                for child in children:
                    yield from child._tree_iter()
            elif isinstance(children, Node):
                yield from children._tree_iter()

    @property
    def checksum(self) -> str:
        ## @brief Return the SHA-256 checksum for this node.
        ##
        ## Checks ``self["hash"]`` — populated at construction for
        ## packages, or set by ``sync()`` after the first download.
        ##
        ## @return The hex digest string, or an empty string when not available.

        return self.get("hash", "")

    def _open_binary(self, data: Optional[bytes] = None):
        ## @brief Return a binary file-like object for this node's content.
        ##
        ## If *data* is provided, wraps it in ``BytesIO`` (scan mode).
        ## Otherwise opens ``repo_root / path`` in ``rb`` mode (sync mode).
        ##
        ## Callers are responsible for closing the returned handle.
        ##
        ## @param data  Optional bytes to read from (scan mode).
        ## @return A binary file-like object.

        if data is not None:
            return io.BytesIO(data)
        if self._repo_vars is not None:
            return open(Path(self._repo_vars.repo_root) / self["path"], "rb")
        raise RuntimeError(
            "Cannot open file on disk: node has no _repo_vars "
            "and no data bytes were provided"
        )

    def _iter_lines(self, data: Optional[bytes] = None):
        ## @brief Yield decoded text lines from this node's content.
        ##
        ## Handles ``.gz``, ``.xz``, and plain text transparently via
        ## the file extension in ``self["path"]``.  Each yielded line
        ## has trailing ``\\n`` and ``\\r`` stripped.
        ##
        ## @param data  Optional bytes to read from (scan mode).
        ## @yield Decoded text lines.
        ## @return A generator over decoded text lines.

        raw = self._open_binary(data)
        path: str = self.get("path", "")
        try:
            if path.endswith(".gz"):
                import gzip
                f = gzip.GzipFile(fileobj=raw)
                for line in f:
                    yield line.decode("utf-8", errors="replace").rstrip("\n\r")
            elif path.endswith(".xz"):
                import lzma
                decomp = lzma.LZMADecompressor()
                buf = b""
                while True:
                    chunk = raw.read(65536)
                    if not chunk:
                        break
                    buf += decomp.decompress(chunk)
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        yield line.decode("utf-8", errors="replace").rstrip("\r")
                if buf:
                    yield buf.decode("utf-8", errors="replace").rstrip("\n\r")
            elif path.endswith(".bz2"):
                import bz2
                text = bz2.decompress(raw.read()).decode("utf-8", errors="replace")
                for line in text.splitlines():
                    yield line.rstrip("\n\r")
            else:
                for line in raw:
                    yield line.decode("utf-8", errors="replace").rstrip("\n\r")
        finally:
            raw.close()

    def stream(self, data: Optional[bytes] = None):
        ## @brief Yield child nodes discovered by reading this node's content.
        ##
        ## The default implementation is a no-op (empty iterator).
        ## Subclasses override to parse their file format and yield child
        ## nodes as a side effect of materialising the subtree.
        ##
        ## *stream()* is always a local read — never touches the network.
        ## In scan mode *data* receives bytes from an HTTP fetch.
        ## In sync mode *data* is ``None`` and the method reads from disk.
        ##
        ## @param data  Optional bytes to parse (scan mode).
        ## @yield Child Node instances.
        ## @return A generator over child nodes.

        return iter([])

    def fetch(self, uri: str, *, config: Optional[Dict[str, Any]] = None) -> Optional[bytes]:
        ## @brief Fetch *uri* content into memory.
        ##
        ## In scan mode, performs a pure in-memory HTTP fetch with result
        ## caching in ``_cache``.  In sync mode (when
        ## ``_repo_vars.sync_mode`` is set), redirects to ``self.read()``
        ## which synchronises the file through the pool and then reads it
        ## from disk.
        ##
        ## @param uri     URL to fetch.
        ## @param config  Optional configuration dict forwarded to ``read()``
        ##                in sync mode.
        ## @return The raw bytes, or ``None`` if *uri* is empty.

        if not uri:
            return None
        if self._cache is not None:
            return self._cache
        if self._repo_vars is not None and self._repo_vars.sync_mode:
            return self.read(config=config)
        data = HTTPFetch(uri)
        self._cache = data
        return data

    def read(self, *, config: Optional[Dict[str, Any]] = None) -> Optional[bytes]:
        ## @brief Return the content of this node's file from disk.
        ##
        ## Calls ``sync()`` first to ensure the file is in the pool and
        ## hardlinked to the repo destination, then reads the bytes from
        ## ``repos_root / path``.
        ##
        ## @param config  Optional configuration dict for sync parameters.
        ## @return The raw bytes, or ``None`` if no URI or path is set.

        if not self.get("uri") or not self.get("path"):
            return None
        self.sync(config=config)
        if self._repo_vars is not None:
            return (Path(self._repo_vars.repo_root) / self.get("path")).read_bytes()
        return None

    def sync(self, *, config: Optional[Dict[str, Any]] = None) -> List[Path]:
        ## @brief Synchronise this node's file from upstream into the pool and repo.
        ##
        ## Ensures the destination file exists in the repo and is correctly
        ## hardlinked to the pool. Uses inventories as a fast path; falls
        ## back to stat() on disk when inventories miss. Logs each outcome
        ## with a descriptive label.
        ##
        ## @param config  Optional configuration dict (carries suite,
        ##                architecture, component filters).
        ## @return List of paths hardlinked into the repo (one-element in
        ##         normal operation; empty if no uri/path is set).

        uri = self.get("uri")
        path_val = self.get("path")
        if not uri or not path_val:
            return []

        rv = self._repo_vars
        if rv is None:
            raise RuntimeError(
                f"sync() called on {path_val} without _repo_vars"
            )

        # Declare this path as wanted before any I/O.  Removes it from the
        # pre-sync stale set so _sweep_stale() does not delete a file we are
        # about to create or confirm — regardless of whether we end up hitting
        # the inventory fast path, re-linking from the pool, or downloading.
        # set.discard() is GIL-atomic in CPython; no separate lock needed.
        if rv.inv is not None:
            rv.inv.stale_paths.discard(path_val)

        dest = Path(rv.repo_root) / path_val
        hash_val = self.checksum

        def _record(*, hit: int = 0, miss: int = 0, bytes_tx: int = 0) -> None:
            ## @brief Forward one sync outcome to the repo-level SyncStats.
            ##
            ## The node no longer carries a per-node stats dict — outcomes
            ## go directly to the shared accumulator on RepoVars so the
            ## coordinator's post-sync tree walk is eliminated.  Called at
            ## every return point in sync(); closes over ``rv``, ``self``,
            ## and ``hash_val`` (read at call time, so phase-3 updates to
            ## hash_val are reflected correctly).
            ##
            ## @param hit      1 if served from pool, else 0.
            ## @param miss     1 if downloaded, else 0.
            ## @param bytes_tx Bytes transferred.
            ## @return None
            if rv.stats is not None:
                rv.stats.record(
                    hit=hit,
                    miss=miss,
                    bytes_tx=bytes_tx,
                    size=self.get("size", 0) or 0,
                    hash_val=hash_val,
                )

        # ---------------------------------------------------------
        # Phase 1 — Inventory fast path (no disk beyond link)
        # ---------------------------------------------------------
        if hash_val:
            _inv_has = rv.inv is not None and rv.inv.has(hash_val)
            if _inv_has and dest.exists():
                _record(hit=1)
                log(f"  {'Unchanged':<{LOG_LABEL_W}} {fmt_size(self.get('size') or 0):>7}  {path_val}")
                return [dest]

            if rv.pool_inv is not None and rv.pool_inv.has(hash_val):
                pool_path = _pool_path(rv.pool_root, hash_val)
                _sync_link(pool_path, dest, rv, hash_val)
                _record(hit=1)
                label = 'Linked*' if _inv_has else 'Linked'
                log(f"  {label:<{LOG_LABEL_W}} {fmt_size(self.get('size') or 0):>7}  {path_val}")
                return [dest]

        # ---------------------------------------------------------
        # Phase 2 — Staging lock + stat fallback (cross-process)
        # ---------------------------------------------------------
        staging_dir = Path(rv.pool_root) / "staging"
        staging_dir.mkdir(parents=True, exist_ok=True)
        staging_key = hash_val or hashlib.sha256(uri.encode()).hexdigest()
        # Refcounted lock: track concurrent users per staging key so the map
        # entry can be pruned when the last thread finishes with this hash.
        with _staging_locks_lock:
            lock, refs = _staging_locks.get(staging_key, (threading.Lock(), 0))
            _staging_locks[staging_key] = (lock, refs + 1)
        with lock:
            try:
                if hash_val:
                    # Recheck inventories after lock (another thread may have raced)
                    _inv_has = rv.inv is not None and rv.inv.has(hash_val)
                    if _inv_has and dest.exists():
                        _record(hit=1)
                        log(f"  {'Unchanged':<{LOG_LABEL_W}} {fmt_size(self.get('size') or 0):>7}  {path_val}")
                        return [dest]

                    if rv.pool_inv is not None and rv.pool_inv.has(hash_val):
                        pool_path = _pool_path(rv.pool_root, hash_val)
                        _sync_link(pool_path, dest, rv, hash_val)
                        _record(hit=1)
                        label = 'Linked*' if _inv_has else 'Linked'
                        log(f"  {label:<{LOG_LABEL_W}} {fmt_size(self.get('size') or 0):>7}  {path_val}")
                        return [dest]

                    # Stat fallback — file exists on disk but neither inventory knew
                    pool_path = _pool_path(rv.pool_root, hash_val)
                    if pool_path.exists():
                        # Update pool inventory with on-disk inode
                        if rv.pool_inv is not None:
                            rv.pool_inv.add(hash_val, pool_path.stat().st_ino)
                        if dest.exists() and dest.stat().st_ino == pool_path.stat().st_ino:
                            # Already correctly linked — update repo inventory
                            if rv.inv is not None:
                                rv.inv.add(hash_val, dest.stat().st_ino)
                            _record(hit=1)
                            log(f"  {'Unchanged*':<{LOG_LABEL_W}} {fmt_size(self.get('size') or 0):>7}  {path_val}")
                            return [dest]
                        # Stale or missing dest — fix the link
                        _sync_link(pool_path, dest, rv, hash_val)
                        _record(hit=1)
                        log(f"  {'Linked':<{LOG_LABEL_W}} {fmt_size(self.get('size') or 0):>7}  {path_val}")
                        return [dest]

                # ---------------------------------------------------------
                # Phase 3 — Genuine download
                # ---------------------------------------------------------
                tmp = str(staging_dir / staging_key)
                actual_hash = HTTPDownload(uri, tmp)
                if hash_val and actual_hash != hash_val:
                    os.unlink(tmp)
                    raise RuntimeError(
                        f"Hash mismatch for {uri}: expected {hash_val}, got {actual_hash}"
                    )
                self["hash"] = actual_hash
                hash_val = hash_val or actual_hash
                pool_path = _pool_path(rv.pool_root, hash_val)
                pool_path.parent.mkdir(parents=True, exist_ok=True)
                os.replace(tmp, pool_path)
                ino = _sync_link(pool_path, dest, rv, hash_val)
                sz = int(dest.stat().st_size)
                self["size"] = sz
                if rv.pool_inv is not None:
                    rv.pool_inv.add(hash_val, ino)
                _record(miss=1, bytes_tx=sz)
                log(f"  {'Downloaded':<{LOG_LABEL_W}} {fmt_size(sz):>7}  {path_val}")
                return [dest]
            finally:
                # Decref and prune — runs on every return or raise above.
                with _staging_locks_lock:
                    lock, refs = _staging_locks[staging_key]
                    if refs == 1:
                        del _staging_locks[staging_key]
                    else:
                        _staging_locks[staging_key] = (lock, refs - 1)


T = TypeVar("T", bound="Node")


class NodeList(List[T], Generic[T]):
    ## @brief List-like collection of Node subclasses with frozen-state support.
    ##
    ## Adds convenience helpers and ensures the collection respects the
    ## frozen/immutable state inherited from its parent Node graph.

    _frozen: bool

    def _check_frozen(self) -> None:
        ## @brief Raise if this list has been frozen.
        ## @raise TypeError  If the list is frozen.
        ## @return None

        if getattr(self, "_frozen", False):
            raise TypeError(f"{type(self).__name__} is frozen and cannot be modified")

    def freeze(self, *, deep: bool = True) -> None:
        ## @brief Mark this list (and optionally its children) as frozen.
        ## @param deep  Whether to recursively freeze child Nodes (default True).
        ## @return None

        object.__setattr__(self, "_frozen", True)
        if deep:
            for item in self:
                if isinstance(item, Node):
                    item.freeze(deep=True)

    def thaw(self, *, deep: bool = True) -> None:
        ## @brief Clear the frozen flag on this list (and optionally children).
        ## @param deep  Whether to recursively thaw child Nodes (default True).
        ## @return None

        object.__setattr__(self, "_frozen", False)
        if deep:
            for item in self:
                if isinstance(item, Node):
                    item.thaw(deep=True)

    def append(self, __object: T) -> None:
        ## @brief Append a node to this collection after checking frozen state.
        ## @param __object  The Node instance to append.
        ## @return None

        self._check_frozen()
        super().append(__object)

    def extend(self, __iterable: Iterable[T]) -> None:
        ## @brief Extend this collection with nodes from an iterable.
        ## @param __iterable  Iterable of Node instances to add.
        ## @return None

        self._check_frozen()
        super().extend(__iterable)

    def insert(self, __index: int, __object: T) -> None:
        ## @brief Insert a node at a given position.
        ## @param __index   The index at which to insert.
        ## @param __object  The Node instance to insert.
        ## @return None

        self._check_frozen()
        super().insert(__index, __object)

    def pop(self, __index: int = -1) -> T:
        ## @brief Remove and return the node at the given index.
        ## @param __index  The index to pop from (default -1, the last element).
        ## @return The removed Node instance.

        self._check_frozen()
        return super().pop(__index)

    def remove(self, __value: T) -> None:
        ## @brief Remove the first occurrence of a value from this collection.
        ## @param __value  The value to remove.
        ## @return None

        self._check_frozen()
        super().remove(__value)

    def reverse(self) -> None:
        ## @brief Reverse the order of nodes in this collection in place.
        ## @return None

        self._check_frozen()
        super().reverse()

    def sort(self, **kwargs) -> None:
        ## @brief Sort this collection in place.
        ## @param kwargs  Keyword arguments forwarded to ``list.sort()``.
        ## @return None

        self._check_frozen()
        super().sort(**kwargs)

    def __setitem__(self, __key: int | slice, __value: T | Iterable[T]) -> None:
        ## @brief Set an item at a given index or slice.
        ## @param __key    The index or slice to set.
        ## @param __value  The value (or iterable for slices) to assign.
        ## @return None

        self._check_frozen()
        super().__setitem__(__key, __value)

    def __delitem__(self, __key: int | slice) -> None:
        ## @brief Delete an item at a given index or slice.
        ## @param __key  The index or slice to delete.
        ## @return None

        self._check_frozen()
        super().__delitem__(__key)

    def iter(self) -> Iterable[T]:
        ## @brief Iterate over nodes in this collection.
        ## @return An iterator over the contained Nodes.

        return iter(self)

    def snapshot(self) -> Any:
        ## @brief Return a JSON-serialisable list snapshot for this collection.
        ## @return A list of plain dicts/scalars.

        return [getattr(item, "snapshot", lambda: item)() for item in self]

    @classmethod
    def restore(cls, snapshots: Iterable[Any], item_type: type[T]) -> "NodeList[T]":
        ## @brief Rebuild a NodeList from an iterable of element snapshots.
        ##
        ## ``item_type`` is the concrete Node subclass to construct for each
        ## element.  It must provide a compatible ``restore`` classmethod.
        ##
        ## @param snapshots  Iterable of snapshot dicts/scalars.
        ## @param item_type  The Node subclass to restore each element as.
        ## @return A reconstructed NodeList.

        lst: "NodeList[T]" = cls()
        for snap in snapshots:
            if isinstance(snap, item_type):
                lst.append(snap)
            else:
                lst.append(item_type.restore(snap))
        return lst

    def to_pretty_json(self, indent: int = 2) -> str:
        ## @brief Return an indented JSON string for this collection.
        ## @param indent  Number of spaces per indent level (default 2).
        ## @return Pretty-printed JSON string.

        payload = [getattr(item, "to_plain", lambda: item)() for item in self]
        return json.dumps(payload, indent=indent)
