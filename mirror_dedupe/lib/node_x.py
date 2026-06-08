## @file node_x.py
##
## @brief Generic thread-safe dict-backed tree node library.
##
## Provides foundational building blocks for constructing object graphs
## with optional serialisation, streaming child discovery, and
## multi-node locking.  Designed for extractability as a standalone
## package — zero external dependencies beyond the Python standard library.
##
## Class hierarchy (dependency order):
##
##     Node                    — dict-backed, RLock, payload validation,
##                               freeze/thaw, subtree walking, merging
##     NodeList                — thread-safe Node-only collection
##     StreamMixin             — virtual stream() for lazy child discovery
##     Serialisable            — snapshot/restore/clone mixin
##     SerialisableNodeList    — NodeList + snapshot/restore
##     NodeTransaction         — ordered multi-node lock acquisition
##
## All mutation operations on ``Node`` and ``NodeList`` are protected by
## a per-instance ``threading.RLock``.  Nested acquisition (e.g. a method
## on a child called from a method on the parent) is safe.
##
## @copyright Copyright (c) 2026 Tim Hosking
## @see https://github.com/munger
## @par Licence: MIT

from __future__ import annotations

import json
import threading
from typing import (
    Any,
    Callable,
    ClassVar,
    Dict,
    Generic,
    Iterable,
    List,
    Optional,
    Tuple,
    Type,
    TypeVar,
)

T = TypeVar("T", bound="Node")


# ============================================================================
# Node
# ============================================================================


class Node(dict):
    ## @brief Thread-safe dict-backed tree node.
    ##
    ## All payload mutations (``__setitem__``, ``__delitem__``, ``update``,
    ## ``pop``, ``clear``, ``popitem``) are protected by a per-instance
    ## ``threading.RLock``.  Attribute writes to non-reserved names are
    ## routed into the dict payload under the same lock.
    ##
    ## ``__setattr__`` distinguishes two kinds of attribute:
    ##
    ##   * **Private/reserved** (name starts with ``_``, or in
    ##     ``type(self)._reserved``) — written via
    ##     ``object.__setattr__``, no lock, no freeze check.
    ##   * **Payload fields** — written into the dict under the lock
    ##     after checking the frozen flag.
    ##
    ## ``_reserved`` is populated automatically by ``__init_subclass__``
    ## from method names, properties, and annotated fields.  Subclasses
    ## that add public methods or properties do not need to maintain
    ## ``_reserved`` manually.
    ##
    ## ``_children`` is a list of attribute names whose values are
    ## Node or NodeList instances that form the structural tree.
    ## ``_tree_iter()`` walks these names to yield the full subtree.
    ## ``_walk_child_nodes()`` applies a callable to every descendant.
    ##
    ## Payload values are constrained by ``_validate_value()``:
    ##
    ##   * Allowed: ``Node``, ``NodeList``, ``None``, ``str``, ``int``,
    ##     ``float``, ``bool``, ``bytes``, and ``tuple`` (recursively
    ##     validated).
    ##   * Rejected: raw ``list`` (use ``NodeList``) and raw ``dict``
    ##     (wrap in a ``Node`` subclass).

    _reserved: ClassVar[set[str]] = set()
    ## @brief Attribute names treated as real object attributes rather
    ##        than payload keys.  Populated automatically.

    _children: ClassVar[List[str]] = []
    ## @brief Names of payload fields that hold child Node/NodeList
    ##        instances.  Used by ``_tree_iter()`` and
    ##        ``_walk_child_nodes()``.

    def __init_subclass__(cls, **kwargs: Any) -> None:
        ## @brief Collect reserved attribute names for each subclass.
        ##
        ## Walks the MRO and collects every public (non-underscore) name
        ## defined as a method, class attribute (except ``_children`` and
        ## ``_reserved`` themselves), or annotated field.  These are added
        ## to ``_reserved`` so that ``__setattr__`` routes them to
        ## ``object.__setattr__`` rather than the dict payload.
        ##
        ## Also ensures ``_frozen``, ``_reserved``, and ``_lock`` are
        ## always reserved so they cannot be set as payload keys.
        ##
        ## @param kwargs  Forwarded to ``super().__init_subclass__``.
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

        reserved.update({"_frozen", "_reserved", "_lock"})
        cls._reserved = reserved

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        ## @brief Construct a Node with optional initial payload.
        ##
        ## If a single positional dict argument is supplied its items are
        ## copied into the new node's payload via ``__setitem__`` (which
        ## validates values, checks frozen, and acquires the lock).
        ## All keyword arguments are then routed through ``__setattr__``.
        ##
        ## @param args    Optional single positional dict payload.
        ## @param kwargs  Optional keyword fields set via ``__setattr__``.
        ## @return None

        super().__init__()
        object.__setattr__(self, "_lock", threading.RLock())
        object.__setattr__(self, "_frozen", False)

        if args:
            if len(args) > 1:
                raise TypeError(
                    "Node accepts at most one positional mapping "
                    "payload argument."
                )
            initial = args[0]
            if isinstance(initial, dict):
                for k, v in initial.items():
                    self[k] = v
            else:
                raise TypeError(
                    f"Positional argument to Node must be a "
                    f"dict-like mapping, got {type(initial)!r}."
                )

        for key, value in kwargs.items():
            setattr(self, key, value)

    def _check_frozen(self) -> None:
        ## @brief Raise if this node has been frozen.
        ## @raise TypeError  If ``_frozen`` is ``True``.
        ## @return None

        if getattr(self, "_frozen", False):
            raise TypeError(
                f"{type(self).__name__} is frozen and cannot be modified."
            )

    def _with_lock(self, func: Callable[[], Any]) -> Any:
        ## @brief Execute *func* under the instance RLock.
        ## @param func  Zero-argument callable.
        ## @return Whatever *func* returns.

        with self._lock:
            return func()

    @property
    def lock(self) -> threading.RLock:
        ## @brief Expose the instance RLock for external callers.
        ##
        ## Used by ``NodeTransaction`` and any code that needs to hold
        ## locks across multiple nodes.
        ##
        ## @return The per-instance ``threading.RLock``.

        return self._lock

    def __getattr__(self, name: str) -> Any:
        ## @brief Fallback: route unknown attribute reads to dict keys.
        ##
        ## Allows ``node.foo`` to resolve as ``node["foo"]`` for schema
        ## data, while real object attributes (privates, methods,
        ## properties) continue to work normally.
        ##
        ## @param name  The attribute name to look up.
        ## @return The value of ``self[name]``.
        ## @raise AttributeError  If the key is not found in the payload.

        try:
            return self[name]
        except KeyError:
            raise AttributeError(name)

    def __setattr__(self, name: str, value: Any) -> None:
        ## @brief Route payload attributes into the dict under lock.
        ##
        ## Names starting with underscore or in ``_reserved`` are written
        ## via ``object.__setattr__`` (direct instance attribute, no
        ## lock, no freeze check).  All other names are stored as
        ## payload keys through ``__setitem__`` after acquiring the lock
        ## and checking the frozen flag.
        ##
        ## @param name   The attribute name.
        ## @param value  The value to store.
        ## @return None

        if name.startswith("_") or name in type(self)._reserved:
            object.__setattr__(self, name, value)
        else:
            def op() -> None:
                self._check_frozen()
                self[name] = value
            self._with_lock(op)

    def __setitem__(self, key: Any, value: Any) -> None:
        ## @brief Validate value, check frozen, then store under lock.
        ##
        ## @param key    The payload key (must not be in ``_reserved``).
        ## @param value  The value to store (validated by
        ##               ``_validate_value``).
        ## @return None
        ## @raise KeyError  If *key* is in ``_reserved``.

        if key in type(self)._reserved:
            raise KeyError(
                f"Cannot set reserved attribute {key!r} in Node payload."
            )

        def op() -> None:
            self._check_frozen()
            self._validate_value(value)
            super(Node, self).__setitem__(key, value)

        self._with_lock(op)

    def __delitem__(self, key: Any) -> None:
        ## @brief Delete a payload key after checking frozen state.
        ## @param key  The key to remove.
        ## @return None

        def op() -> None:
            self._check_frozen()
            super(Node, self).__delitem__(key)

        self._with_lock(op)

    def clear(self) -> None:
        ## @brief Remove all items from the payload under lock.
        ## @return None

        def op() -> None:
            self._check_frozen()
            super(Node, self).clear()

        self._with_lock(op)

    def pop(self, key: Any, *args: Any) -> Any:
        ## @brief Remove and return a payload item under lock.
        ## @param key    The key to remove.
        ## @param args   Optional default if *key* is not found.
        ## @return The value for *key*, or the default if provided.

        def op() -> Any:
            self._check_frozen()
            return super(Node, self).pop(key, *args)

        return self._with_lock(op)

    def popitem(self) -> Any:
        ## @brief Remove and return the last-inserted payload item.
        ## @return A ``(key, value)`` tuple.

        def op() -> Any:
            self._check_frozen()
            return super(Node, self).popitem()

        return self._with_lock(op)

    def update(self, *args: Any, **kwargs: Any) -> None:
        ## @brief Merge keys into the payload, validating all values.
        ## @param args    Optional single mapping or iterable of pairs.
        ## @param kwargs  Additional key/value pairs.
        ## @return None

        def op() -> None:
            self._check_frozen()
            if args:
                if len(args) > 1:
                    raise TypeError("update expected at most 1 argument.")
                other = args[0]
                if hasattr(other, "items"):
                    for v in other.values():
                        self._validate_value(v)
                else:
                    for _, v in other:
                        self._validate_value(v)
            for v in kwargs.values():
                self._validate_value(v)
            super(Node, self).update(*args, **kwargs)

        self._with_lock(op)

    def _validate_value(self, value: Any) -> None:
        ## @brief Ensure *value* is safe to store in a Node payload.
        ##
        ## Allowed types: Node, NodeList, None, str, int, float, bool,
        ## bytes, and tuple (recursively validated).
        ##
        ## Raw ``list`` and ``dict`` are rejected — use ``NodeList``
        ## or a ``Node`` subclass respectively.
        ##
        ## @param value  The value to check.
        ## @return None
        ## @raise TypeError  If *value* is a plain list or dict.

        if isinstance(value, (Node, NodeList)):
            return
        if isinstance(value, list):
            raise TypeError(
                "Node cannot contain plain lists. Use NodeList instead."
            )
        if isinstance(value, dict):
            raise TypeError(
                "Node cannot contain raw dicts. Wrap in a Node subclass."
            )
        if value is None or isinstance(value, (str, int, float, bool, bytes)):
            return
        if isinstance(value, tuple):
            for item in value:
                self._validate_value(item)
            return
        raise TypeError(
            f"Node cannot safely contain {type(value).__name__}."
        )

    def freeze(self, *, deep: bool = True) -> None:
        ## @brief Mark this node (and optionally its subtree) as frozen.
        ##
        ## When *deep* is ``True``, recurses into ``_walk_child_nodes``
        ## to freeze every descendant Node and NodeList.
        ##
        ## @param deep  Whether to recursively freeze children.
        ## @return None

        def op() -> None:
            object.__setattr__(self, "_frozen", True)
            if deep:
                self._walk_child_nodes(
                    lambda n: n.freeze(deep=True),
                    lambda lst: lst.freeze(deep=False),
                )

        self._with_lock(op)

    def thaw(self, *, deep: bool = True) -> None:
        ## @brief Restore mutability on this node (and optionally subtree).
        ## @param deep  Whether to recursively thaw children.
        ## @return None

        def op() -> None:
            object.__setattr__(self, "_frozen", False)
            if deep:
                self._walk_child_nodes(
                    lambda n: n.thaw(deep=True),
                    lambda lst: lst.thaw(deep=False),
                )

        self._with_lock(op)

    def _walk_child_nodes(
        self,
        func: Callable[[Node], None],
        list_func: Optional[Callable[[NodeList], None]] = None,
    ) -> None:
        ## @brief Apply *func* to every descendant Node and *list_func*
        ##        to every descendant NodeList.
        ##
        ## Callers that need to propagate state onto NodeList containers
        ## (e.g. the frozen flag) should supply *list_func*.  Without it
        ## only Node instances are touched.
        ##
        ## The ``elif`` chain is deliberately ordered:
        ## ``Node → NodeList → list → dict``.  Two earlier bugs are
        ## fixed here:
        ##
        ##   1. ``list_func`` is an explicit parameter rather than
        ##      ``getattr(nodelist, func.__name__, None)``, which always
        ##      returned ``None`` for lambdas (``__name__ == '<lambda>'``).
        ##
        ##   2. ``isinstance(value, Node)`` is an ``elif`` (not ``if``)
        ##      so that Node — a dict subclass — does not also match
        ##      the ``elif isinstance(value, dict)`` branch and
        ##      re-traverse its subtree, which caused exponential
        ##      duplicate work on deep trees.
        ##
        ## @param func       Callable applied to each descendant ``Node``.
        ## @param list_func  Optional callable applied to each descendant
        ##                   ``NodeList``.
        ## @return None

        def recurse(value: Any) -> None:
            if isinstance(value, Node):
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

    def merge(
        self, other: Dict[str, Any] | Node, *, extend_lists: bool = False
    ) -> Node:
        ## @brief Merge another mapping or Node into this one recursively.
        ##
        ## Node fields are merged recursively, plain dicts are merged
        ## shallowly, lists are overwritten (or extended when
        ## ``extend_lists`` is ``True``), and scalars are overwritten.
        ##
        ## @param other         The mapping to merge from.
        ## @param extend_lists  Whether to extend lists instead of
        ##                      overwriting.
        ## @return This node (for chaining).
        ## @raise TypeError  If *other* is not a mapping.

        def op() -> Node:
            self._check_frozen()
            try:
                items = other.items()
            except AttributeError as exc:
                raise TypeError(
                    f"Node.merge() expected a mapping, got {type(other)!r}."
                ) from exc

            for key, value in items:
                self._validate_value(value)
                if key in self:
                    current = self[key]
                    if isinstance(current, Node) and isinstance(
                        value, (Node, dict)
                    ):
                        current.merge(value, extend_lists=extend_lists)
                    elif isinstance(current, dict) and isinstance(value, dict):
                        for k, v in value.items():
                            current[k] = v
                    elif (
                        extend_lists
                        and isinstance(current, list)
                        and isinstance(value, list)
                    ):
                        current.extend(value)
                    else:
                        self[key] = value
                else:
                    self[key] = value
            return self

        return self._with_lock(op)

    def _tree_iter(self) -> Iterable[Node]:
        ## @brief Depth-first generator over this node and its children.
        ##
        ## Walks all attributes named in ``_children`` recursively.
        ## Each node (including self) is yielded exactly once.
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


# ============================================================================
# NodeList
# ============================================================================


class NodeList(list, Generic[T]):
    ## @brief Thread-safe list-like collection of ``Node`` elements.
    ##
    ## Every mutating operation acquires the instance RLock, checks
    ## the frozen flag, and validates that each element is a ``Node``
    ## instance before delegating to the standard ``list`` implementation.
    ##
    ## Like ``Node``, this class supports ``freeze``/``thaw`` with
    ## optional deep recursion into contained Node instances.

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        ## @brief Initialise an empty list with RLock and frozen state.
        ## @param args    Forwarded to ``list.__init__``.
        ## @param kwargs  Forwarded to ``list.__init__``.
        ## @return None

        object.__setattr__(self, "_lock", threading.RLock())
        object.__setattr__(self, "_frozen", False)
        super().__init__(*args, **kwargs)

    def _check_frozen(self) -> None:
        ## @brief Raise if this list has been frozen.
        ## @raise TypeError  If ``_frozen`` is ``True``.

        if getattr(self, "_frozen", False):
            raise TypeError(
                f"{type(self).__name__} is frozen and cannot be modified."
            )

    def _with_lock(self, func: Callable[[], Any]) -> Any:
        ## @brief Execute *func* under the instance RLock.
        ## @param func  Zero-argument callable.
        ## @return Whatever *func* returns.

        with self._lock:
            return func()

    def freeze(self, *, deep: bool = True) -> None:
        ## @brief Mark this list (and optionally its children) as frozen.
        ## @param deep  Whether to recursively freeze child Nodes.
        ## @return None

        def op() -> None:
            object.__setattr__(self, "_frozen", True)
            if deep:
                for item in self:
                    if isinstance(item, Node):
                        item.freeze(deep=True)

        self._with_lock(op)

    def thaw(self, *, deep: bool = True) -> None:
        ## @brief Restore mutability on this list (and optionally children).
        ## @param deep  Whether to recursively thaw child Nodes.
        ## @return None

        def op() -> None:
            object.__setattr__(self, "_frozen", False)
            if deep:
                for item in self:
                    if isinstance(item, Node):
                        item.thaw(deep=True)

        self._with_lock(op)

    def append(self, __object: T) -> None:
        ## @brief Append a Node to this list under lock.
        ## @param __object  The Node instance to append.
        ## @return None
        ## @raise TypeError  If *__object* is not a ``Node``.

        def op() -> None:
            self._check_frozen()
            if not isinstance(__object, Node):
                raise TypeError(
                    f"{type(self).__name__} only accepts Node elements."
                )
            super(NodeList, self).append(__object)

        self._with_lock(op)

    def extend(self, __iterable: Iterable[T]) -> None:
        ## @brief Extend this list with Nodes from an iterable.
        ## @param __iterable  Iterable of Node instances.
        ## @return None

        def op() -> None:
            self._check_frozen()
            items = list(__iterable)
            for item in items:
                if not isinstance(item, Node):
                    raise TypeError(
                        f"{type(self).__name__} only accepts Node elements."
                    )
            super(NodeList, self).extend(items)

        self._with_lock(op)

    def insert(self, __index: int, __object: T) -> None:
        ## @brief Insert a Node at a given index under lock.
        ## @param __index   The position to insert at.
        ## @param __object  The Node instance to insert.
        ## @return None

        def op() -> None:
            self._check_frozen()
            if not isinstance(__object, Node):
                raise TypeError(
                    f"{type(self).__name__} only accepts Node elements."
                )
            super(NodeList, self).insert(__index, __object)

        self._with_lock(op)

    def pop(self, __index: int = -1) -> T:
        ## @brief Remove and return the Node at *__index* under lock.
        ## @param __index  The index to pop (default -1, last element).
        ## @return The removed Node instance.

        def op() -> T:
            self._check_frozen()
            return super(NodeList, self).pop(__index)

        return self._with_lock(op)

    def remove(self, __value: T) -> None:
        ## @brief Remove the first occurrence of *__value* under lock.
        ## @param __value  The Node instance to remove.
        ## @return None

        def op() -> None:
            self._check_frozen()
            super(NodeList, self).remove(__value)

        self._with_lock(op)

    def clear(self) -> None:
        ## @brief Remove all elements under lock.
        ## @return None

        def op() -> None:
            self._check_frozen()
            super(NodeList, self).clear()

        self._with_lock(op)

    def reverse(self) -> None:
        ## @brief Reverse this list in place under lock.
        ## @return None

        def op() -> None:
            self._check_frozen()
            super(NodeList, self).reverse()

        self._with_lock(op)

    def sort(self, **kwargs: Any) -> None:
        ## @brief Sort this list in place under lock.
        ## @param kwargs  Keyword arguments forwarded to ``list.sort()``.
        ## @return None

        def op() -> None:
            self._check_frozen()
            super(NodeList, self).sort(**kwargs)

        self._with_lock(op)

    def __setitem__(
        self, __key: int | slice, __value: T | Iterable[T]
    ) -> None:
        ## @brief Set item at index or slice with type validation.
        ## @param __key    Index or slice.
        ## @param __value  Node instance (or iterable for slices).
        ## @return None

        def op() -> None:
            self._check_frozen()
            if isinstance(__key, slice):
                items = list(__value)  # type: ignore[arg-type]
                for item in items:
                    if not isinstance(item, Node):
                        raise TypeError(
                            f"{type(self).__name__} only accepts "
                            "Node elements."
                        )
                super(NodeList, self).__setitem__(__key, items)
            else:
                if not isinstance(__value, Node):
                    raise TypeError(
                        f"{type(self).__name__} only accepts "
                        "Node elements."
                    )
                super(NodeList, self).__setitem__(__key, __value)

        self._with_lock(op)

    def __delitem__(self, __key: int | slice) -> None:
        ## @brief Delete element at index or slice under lock.
        ## @param __key  Index or slice.
        ## @return None

        def op() -> None:
            self._check_frozen()
            super(NodeList, self).__delitem__(__key)

        self._with_lock(op)

    def iter(self) -> Iterable[T]:
        ## @brief Iterate over the contained Node instances.
        ## @return An iterator over elements.

        return iter(self)


# ============================================================================
# StreamMixin
# ============================================================================


class StreamMixin:
    ## @brief Mixin that adds lazy child-discovery to a ``Node``.
    ##
    ## Callers walk the static tree skeleton (``_children`` /
    ## ``_tree_iter``) and then call ``stream()`` on each node to
    ## discover dynamic children derived from the node's content
    ## (e.g. packages parsed from an archive file, indices parsed
    ## from a Release file).
    ##
    ## The base implementation is a no-op.  Subclasses override to
    ## yield child ``Node`` instances on demand.

    def stream(self, data: Optional[bytes] = None) -> Iterable[Node]:
        ## @brief Yield dynamically-discovered child nodes.
        ##
        ## Called after a node's content has been synchronised (or
        ## fetched into memory in scan mode).  *data* carries the
        ## raw bytes in scan mode; in sync mode it is ``None`` and
        ## the implementation reads from disk.
        ##
        ## The default implementation returns an empty iterator.
        ## Override this method to parse the node's content format
        ## and yield child nodes as they are discovered.
        ##
        ## @param data  Optional raw bytes (scan mode).  ``None`` in
        ##              sync mode (read from disk instead).
        ## @yield Child ``Node`` instances discovered from content.

        return iter([])


# ============================================================================
# Serialisable
# ============================================================================


class Serialisable:
    ## @brief Mixin that adds snapshot/restore/clone to a ``Node``.
    ##
    ## Snapshots are plain Python dict/list/scalar trees with no
    ## ``Node`` instances — they can be serialised to JSON, YAML,
    ## msgpack, or any text/binary format and later restored into a
    ## typed object graph.
    ##
    ## Subclasses opt into structured child restoration by setting
    ## ``_node_fields`` and/or ``_list_fields``.  The default
    ## ``restore()`` passes the snapshot dict as ``__init__`` kwargs
    ## which works for simple payload-only nodes.

    _restore_via_payload: ClassVar[bool] = False
    ## @brief If ``True``, ``restore()`` bypasses ``__init__`` and
    ##        constructs via ``_from_payload()``.

    _node_fields: ClassVar[Dict[str, Type[Any]]] = {}
    ## @brief Mapping of field name → Node subclass for restoring
    ##        single-child attributes.

    _list_fields: ClassVar[Dict[str, Tuple[Type[Any], Type[Any]]]] = {}
    ## @brief Mapping of field name → (NodeList subclass, item Node
    ##        subclass) for restoring list-child attributes.

    def to_plain(self) -> Any:
        ## @brief Recursively convert this node tree to plain Python
        ##        structures (no ``Node`` instances).
        ##
        ## Nested Nodes become dicts, lists remain lists, scalars pass
        ## through unmodified.
        ##
        ## @return A plain dict/list/scalar tree suitable for JSON.

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
        ## @brief Return an indented JSON string of this node tree.
        ## @param indent  Number of spaces per indent level (default 2).
        ## @return Pretty-printed JSON string.

        return json.dumps(self.to_plain(), indent=indent)

    def snapshot(self) -> Any:
        ## @brief Return a JSON-serialisable snapshot of this node tree.
        ##
        ## Scalar fields are emitted first in insertion order, followed
        ## by nested structures (dicts, lists).  This keeps snapshots
        ## readable without requiring each subclass to define ordering.
        ##
        ## @return Plain dict with scalars before nested structures.

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
    def restore(cls, snapshot: Any) -> Any:
        ## @brief Rebuild a node (or subclass) from a snapshot.
        ##
        ## If ``_restore_via_payload`` is ``True``, uses
        ## ``_from_payload()`` to bypass ``__init__`` side effects.
        ## Otherwise passes the snapshot dict as ``__init__``'s
        ## single positional argument.
        ##
        ## @param snapshot  Plain dict from an earlier ``snapshot()``.
        ## @return A reconstructed ``Node`` subclass instance.
        ## @raise TypeError  If *snapshot* is not a dict.

        if isinstance(snapshot, dict):
            if getattr(cls, "_restore_via_payload", False):
                return cls._from_payload(snapshot)

            try:
                node = cls(snapshot)
            except TypeError as exc:
                raise TypeError(
                    f"{cls.__name__}.restore() could not call "
                    f"{cls.__name__}.__init__.  Either set "
                    "`_restore_via_payload = True` or provide a "
                    "custom restore() implementation."
                ) from exc
            return node

        raise TypeError(
            f"{cls.__name__}.restore() expected a mapping payload, "
            f"got {type(snapshot)!r}."
        )

    @classmethod
    def _from_payload(cls, payload: Dict[str, Any]) -> Any:
        ## @brief Construct a node from a plain payload dict without
        ##        invoking ``__init__``.
        ##
        ## Creates the instance via ``__new__`` then initialises the
        ## dict portion via ``Node.__init__``.  Used during snapshot
        ## restore when the payload already contains all fields.
        ##
        ## @param payload  Plain dict of field values.
        ## @return A new instance of ``cls``.

        instance = cls.__new__(cls)
        Node.__init__(instance, payload)
        return instance

    @classmethod
    def _restore_children(
        cls, node: Any, snapshot: Any
    ) -> None:
        ## @brief Rebuild declared child Nodes/NodeLists from a snapshot.
        ##
        ## Subclasses opt in by populating ``_node_fields`` and/or
        ## ``_list_fields``, then call this from their own ``restore()``
        ## after constructing *node*.
        ##
        ## @param node      The parent node whose children to restore.
        ## @param snapshot  Plain dict snapshot containing child data.
        ## @return None

        if not isinstance(snapshot, dict):
            return

        for field, child_cls in getattr(cls, "_node_fields", {}).items():
            raw = snapshot.get(field)
            if raw is not None:
                setattr(node, field, child_cls.restore(raw))

        for field, (list_cls, item_cls) in getattr(
            cls, "_list_fields", {}
        ).items():
            items = snapshot.get(field, [])
            setattr(node, field, list_cls.restore(items, item_type=item_cls))

    def clone(self) -> Any:
        ## @brief Deep-clone this node and its subtree.
        ##
        ## Preserves runtime attributes (e.g. loaders, caches) and
        ## avoids calling ``__init__`` on subclasses so that custom
        ## constructors remain valid on the clone.
        ##
        ## @return A deep copy of this node.

        return self._clone_recursive({})

    def _clone_recursive(self, memo: Dict[int, Any]) -> Any:
        ## @brief Internal deep-clone with identity-based memoisation.
        ##
        ## Uses ``object.__new__`` and ``dict.__init__`` to bypass the
        ## subclass ``__init__`` (which may have side effects) while
        ## preserving the subclass type.  Shared references (same Node
        ## reachable through multiple paths) are preserved via *memo*.
        ##
        ## @param memo  ``id(original) → clone`` dict for cycle and
        ##              shared-reference detection.
        ## @return A deep copy of this node.

        if not isinstance(self, dict):
            raise TypeError(
                "Node subclass must remain dict-backed for "
                "clone() to work."
            )

        obj_id = id(self)
        if obj_id in memo:
            return memo[obj_id]

        new = object.__new__(type(self))
        memo[obj_id] = new
        dict.__init__(new, {})

        def clone_value(value: Any) -> Any:
            clone_func = getattr(value, "_clone_recursive", None)
            if clone_func:
                return clone_func(memo)
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


# ============================================================================
# SerialisableNodeList
# ============================================================================


class SerialisableNodeList(NodeList[T], Generic[T]):
    ## @brief A ``NodeList`` with snapshot/restore and pretty-printing.
    ##
    ## Provides the same serialisation interface as ``Serialisable``
    ## but for list-shaped collections of ``Node`` instances.

    def snapshot(self) -> Any:
        ## @return A list of plain dicts/scalars from each element's
        ##         ``snapshot()`` method, falling back to identity for
        ##         non-Node items.

        return [
            getattr(item, "snapshot", lambda: item)() for item in self
        ]

    @classmethod
    def restore(
        cls,
        snapshots: Iterable[Any],
        item_type: type[T],
    ) -> SerialisableNodeList[T]:
        ## @brief Rebuild a ``SerialisableNodeList`` from a list of
        ##        element snapshots.
        ##
        ## Each snapshot is restored via ``item_type.restore()``
        ## (if available) or by passing it to ``item_type()`` directly.
        ## Already-instantiated ``item_type`` instances pass through.
        ##
        ## @param snapshots  Iterable of snapshot dicts/scalars.
        ## @param item_type  The ``Node`` subclass to restore each
        ##                   element as.
        ## @return A reconstructed ``SerialisableNodeList``.

        lst: SerialisableNodeList[T] = cls()
        for snap in snapshots:
            if isinstance(snap, item_type):
                lst.append(snap)
            else:
                restore_func = getattr(item_type, "restore", None)
                if restore_func:
                    lst.append(restore_func(snap))
                else:
                    lst.append(item_type(snap))
        return lst

    def to_pretty_json(self, indent: int = 2) -> str:
        ## @brief Return an indented JSON string of this list.
        ## @param indent  Number of spaces per indent level (default 2).
        ## @return Pretty-printed JSON string.

        payload = [
            getattr(item, "to_plain", lambda: item)() for item in self
        ]
        return json.dumps(payload, indent=indent)


# ============================================================================
# NodeTransaction
# ============================================================================


class NodeTransaction:
    ## @brief Ordered multi-node lock acquisition.
    ##
    ## Acquires one or more ``Node`` locks in a stable order (sorted by
    ## ``id()``) to prevent deadlock when performing cross-node
    ## operations.  Locks are released in reverse order.
    ##
    ## Usage::
    ##
    ##     with NodeTransaction(node_a, node_b, node_c):
    ##         # safe: locks held in id order
    ##         node_a["ref"] = node_b["id"]

    def __init__(self, *nodes: Node) -> None:
        ## @brief Accept one or more ``Node`` instances to manage.
        ##
        ## Nodes are sorted by ``id()`` for deterministic ordering.
        ##
        ## @param nodes  One or more ``Node`` instances.

        self._nodes = sorted(set(nodes), key=id)

    def __enter__(self) -> NodeTransaction:
        ## @brief Acquire all node locks in sorted order.
        ## @return This transaction instance.

        for n in self._nodes:
            n.lock.acquire()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        ## @brief Release all node locks in reverse order.
        ## @param exc_type  Exception type (unused).
        ## @param exc       Exception value (unused).
        ## @param tb        Traceback (unused).
        ## @return ``False`` — exceptions are not suppressed.

        for n in reversed(self._nodes):
            n.lock.release()
        return False


# ============================================================================
# Public API
# ============================================================================

__all__ = [
    "Node",
    "NodeList",
    "StreamMixin",
    "Serialisable",
    "SerialisableNodeList",
    "NodeTransaction",
]
