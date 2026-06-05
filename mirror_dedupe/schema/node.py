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

from ..lib.http_download import HTTPFetch, HTTPDownload, kill_active_subprocesses
from ..lib.log import log

_staging_locks: dict[str, threading.Lock] = {}
_staging_locks_lock = threading.Lock()


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

        # Recursively convert Node → dict, list → list, scalar → identity.
        def convert(value: Any) -> Any:
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

        if getattr(self, "_frozen", False):
            raise TypeError(
                f"{type(self).__name__} is frozen and cannot be modified. "
                "Call `thaw(deep=...)` before mutating, or avoid mutating "
                "frozen snapshots that are being shared across threads."
            )

    def freeze(self, *, deep: bool = True) -> None:
        ## @brief Mark this node (and optionally its children) as frozen.
        ##
        ## When ``deep`` is True, recurse into nested Nodes so the entire
        ## tree becomes immutable at the schema layer.
        ##
        ## @param deep  Whether to recursively freeze child nodes (default True).

        object.__setattr__(self, "_frozen", True)
        if deep:
            self._walk_child_nodes(lambda n: n.freeze(deep=True))

    def thaw(self, *, deep: bool = True) -> None:
        ## @brief Clear the frozen flag on this node (and optionally children).
        ##
        ## Restores mutability after a previous ``freeze()`` call.
        ##
        ## @param deep  Whether to recursively thaw child nodes (default True).

        object.__setattr__(self, "_frozen", False)
        if deep:
            self._walk_child_nodes(lambda n: n.thaw(deep=True))

    def _walk_child_nodes(self, func: Callable[["Node"], None]) -> None:
        ## @brief Apply *func* to all direct child Nodes and NodeLists.
        ##
        ## Used by operations such as ``freeze``/``thaw`` that need to recurse
        ## across the schema tree without duplicating traversal logic.
        ##
        ## @param func  Callable to apply to each child Node.

        # Recurse into Node/NodeList/dict/list to apply func to every leaf.
        def recurse(value: Any) -> None:
            if isinstance(value, Node):
                func(value)
            if isinstance(value, NodeList):
                list_func = getattr(value, func.__name__, None)
                if list_func:
                    list_func(deep=False)
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

        # Deep-clone a single value: recurse into Node/list/dict.
        def clone_value(value: Any) -> Any:
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
    def probe_url(cls, uri: str, config: Optional[Dict[str, Any]] = None) -> Optional[bytes]:
        ## @brief Check whether a URL is reachable and return its content.
        ## @param uri     The URL to probe.
        ## @param config  Optional configuration dict (e.g. timeout values).
        ## @return The raw response bytes, or ``None`` if the URL is empty or unreachable.

        if not uri:
            return None
        try:
            return HTTPFetch(uri)
        except RuntimeError:
            return None



    @staticmethod
    def _pool_path(hash_val: str) -> Path:
        ## @brief Resolve the on-disk pool path for a SHA-256 hash.
        ##
        ## The pool directory structure is
        ## ``{pool_root}/by-hash/SHA256/{first2}/{next2}/{full_hash}``,
        ## where ``first2 = hash_val[:2]`` and ``next2 = hash_val[2:4]``.
        ## This fan-out prevents any single directory from holding more
        ## than 256 subdirectories.
        ##
        ## @param hash_val  Full SHA-256 hex string (64 chars).
        ## @return The pool ``Path`` for this hash.
        from mirror_dedupe.config import Config
        cfg = Config.load()
        return Path(cfg.pool_root) / "by-hash" / "SHA256" / hash_val[:2] / hash_val[2:4] / hash_val

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
        from mirror_dedupe.config import Config
        cfg = Config.load()
        return open(Path(cfg.repo_root) / self["path"], "rb")

    def _iter_lines(self, data: Optional[bytes] = None):
        ## @brief Yield decoded text lines from this node's content.
        ##
        ## Handles ``.gz``, ``.xz``, and plain text transparently via
        ## the file extension in ``self["path"]``.  Each yielded line
        ## has trailing ``\\n`` and ``\\r`` stripped.
        ##
        ## @param data  Optional bytes to read from (scan mode).
        ## @yield Decoded text lines.

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

        return iter([])

    def fetch(self, uri: str, *, config: Optional[Dict[str, Any]] = None) -> Optional[bytes]:
        ## @brief Fetch *uri* content into memory.
        ##
        ## In scan mode, performs a pure in-memory HTTP fetch with result
        ## caching in ``_cache``.  In sync mode (when ``Config._sync_mode``
        ## is set), redirects to ``self.read()`` which synchronises the
        ## file through the pool and then reads it from disk.
        ##
        ## @param uri     URL to fetch.
        ## @param config  Optional configuration dict for fetch parameters.
        ## @return The raw bytes, or ``None`` if *uri* is empty.

        if not uri:
            return None
        if self._cache is not None:
            return self._cache
        from mirror_dedupe.config import Config
        cfg = Config.load()
        if getattr(cfg, "_sync_mode", False):
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
        from mirror_dedupe.config import Config
        cfg = Config.load()
        return (Path(cfg.repo_root) / self.get("path")).read_bytes()

    def sync(self, *, config: Optional[Dict[str, Any]] = None) -> List[Path]:
        ## @brief Synchronise this node's file from upstream into the pool and repo.
        ##
        ## Ensures the destination file matches upstream by verifying it is
        ## hardlinked from the correct pool file.  Verification is one
        ## inode comparison — no readback, no hash computation.
        ##
        ## Pushes a ``self["stats"]`` dict with hit/miss/bytes_tx counters
        ## after a successful sync, so that ``Repo.stats()`` can aggregate
        ## across the tree.
        ##
        ## Flow for known hashes:
        ## 1. Pool file exists + dest exists + inodes match → done.
        ## 2. Pool file exists + dest stale/missing → unlink dest, link from pool.
        ## 3. Pool missing → download to staging, capture hash from curl,
        ##    move into pool, unlink stale dest, link from pool.
        ##
        ## Files without a known hash (e.g. Release) always go through
        ## the download path — they are small and the cost is negligible.
        ##
        ## @param config  Optional configuration dict for fetch parameters.
        ## @return List of ``Path`` objects for linked/downloaded files.

        uri = self.get("uri")
        path_val = self.get("path")
        if not uri or not path_val:
            return []
        from mirror_dedupe.config import Config
        cfg = Config.load()
        dest = Path(cfg.repo_root) / path_val
        pool_root = Path(cfg.pool_root)
        hash_val = self.checksum
        if hash_val:
            pool_path = pool_root / "by-hash" / "SHA256" / hash_val[:2] / hash_val[2:4] / hash_val
            if pool_path.exists():
                if dest.exists():
                    try:
                        if dest.stat().st_ino == pool_path.stat().st_ino:
                            self["stats"] = {"hit": 1, "miss": 0, "bytes_tx": 0}
                            from ..lib.log import log
                            log(f"  Unchanged {path_val}")
                            return [dest]
                    except OSError:
                        pass
                    dest.unlink()
                dest.parent.mkdir(parents=True, exist_ok=True)
                os.link(pool_path, dest)
                from ..lib.log import log
                log(f"  Linked {path_val}")
                self["stats"] = {"hit": 1, "miss": 0, "bytes_tx": 0}
                return [dest]
        staging_dir = pool_root / "staging"
        staging_dir.mkdir(parents=True, exist_ok=True)
        staging_key = hash_val or hashlib.sha256(uri.encode()).hexdigest()
        with _staging_locks_lock:
            lock = _staging_locks.setdefault(staging_key, threading.Lock())
        with lock:
            if hash_val:
                pool_path = pool_root / "by-hash" / "SHA256" / hash_val[:2] / hash_val[2:4] / hash_val
                if pool_path.exists():
                    if dest.exists():
                        try:
                            if dest.stat().st_ino == pool_path.stat().st_ino:
                                self["stats"] = {"hit": 1, "miss": 0, "bytes_tx": 0}
                                from ..lib.log import log
                                log(f"  Unchanged {path_val}")
                                return [dest]
                        except OSError:
                            pass
                        dest.unlink()
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    os.link(pool_path, dest)
                    from ..lib.log import log
                    log(f"  Linked {path_val}")
                    self["stats"] = {"hit": 1, "miss": 0, "bytes_tx": 0}
                    return [dest]
            tmp = str(staging_dir / staging_key)
            actual_hash = HTTPDownload(uri, tmp)
            if hash_val and actual_hash != hash_val:
                os.unlink(tmp)
                raise RuntimeError(
                    f"Hash mismatch for {uri}: expected {hash_val}, got {actual_hash}"
                )
            self["hash"] = actual_hash
            hash_val = hash_val or actual_hash
            pool_path = pool_root / "by-hash" / "SHA256" / hash_val[:2] / hash_val[2:4] / hash_val
            pool_path.parent.mkdir(parents=True, exist_ok=True)
            os.replace(tmp, pool_path)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.unlink(missing_ok=True)
            os.link(pool_path, dest)
            sz = int(dest.stat().st_size)
            self["size"] = sz
            from ..lib.log import log
            log(f"  Downloaded {path_val}")
            self["stats"] = {"hit": 0, "miss": 1, "bytes_tx": sz}
            return [dest]


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

        if getattr(self, "_frozen", False):
            raise TypeError(f"{type(self).__name__} is frozen and cannot be modified")

    def freeze(self, *, deep: bool = True) -> None:
        ## @brief Mark this list (and optionally its children) as frozen.
        ## @param deep  Whether to recursively freeze child Nodes (default True).

        object.__setattr__(self, "_frozen", True)
        if deep:
            for item in self:
                if isinstance(item, Node):
                    item.freeze(deep=True)

    def thaw(self, *, deep: bool = True) -> None:
        ## @brief Clear the frozen flag on this list (and optionally children).
        ## @param deep  Whether to recursively thaw child Nodes (default True).

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
