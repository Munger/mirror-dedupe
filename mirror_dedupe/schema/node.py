"""node.py

  Dict-backed object graph primitives with snapshot/restore support.

  This module defines two core building blocks:

  * ``Node``      – a thin wrapper over ``dict`` that routes most
    attribute access into an underlying mapping while supporting
    snapshot/restore and optional structural metadata for child nodes.
  * ``NodeList``  – a list-like container for ``Node`` subclasses with
    matching snapshot/restore helpers.

  The snapshot format is transport-agnostic: objects are converted to
  plain Python dict/list/scalar structures that can be stored or
  transmitted in any encoding (JSON, YAML, msgpack, databases, etc.) and
  later restored into a typed object graph.

Copyright (c) 2025 Tim Hosking
Website: https://github.com/munger
Licence: MIT
"""

from __future__ import annotations

import json
from typing import Any, ClassVar, Dict, List, Generic, Iterable, Tuple, Type, TypeVar, Callable

# --- Node ---

class Node(dict):
    """Base class for all dict-backed schema nodes.

    This is a thin wrapper over ``dict`` so that higher-level schema
    objects can share helpers like ``to_dict`` / ``to_pretty_json`` and
    ``from_source``. Subclasses are free to shape their payloads as
    needed; there is no enforced ``_key`` field or child tree logic
    here.
    """

    # Names that are treated as real attributes rather than schema
    # payload keys. Populated per-subclass in ``__init_subclass__``.
    _reserved: ClassVar[set[str]] = set()

    # If set to True by a subclass, ``restore`` will bypass the normal
    # constructor and use ``_from_payload`` to seed the underlying
    # mapping directly from the snapshot payload. This is appropriate
    # for pure data containers whose ``__init__`` is a structured
    # convenience API rather than something that must run on restore.
    _restore_via_payload: ClassVar[bool] = False

    # Optional per-subclass metadata describing child Node fields. These
    # are consulted by the generic ``_restore_children`` helper when a
    # subclass chooses to delegate child reconstruction to Node.
    _node_fields: ClassVar[Dict[str, Type["Node"]]] = {}
    _list_fields: ClassVar[Dict[str, Tuple[Type["NodeList"], Type["Node"]]]] = {}

    def __init_subclass__(cls, **kwargs: Any) -> None:  # type: ignore[override]
        super().__init_subclass__(**kwargs)
        reserved: set[str] = set()

        for base in cls.mro():
            # Methods, class attributes, etc. (excluding privates and
            # property descriptors used as schema accessors).
            for name, value in getattr(base, "__dict__", {}).items():
                if not name.startswith("_") and not isinstance(value, property):
                    reserved.add(name)

            # Annotated attributes (e.g. dataclass fields like Repo.http)
            # should also be treated as real attributes, not payload keys.
            for name in getattr(base, "__annotations__", {}).keys():
                if not name.startswith("_"):
                    reserved.add(name)

        # Always protect core internal flags.
        reserved.update({"_frozen", "_reserved"})

        cls._reserved = reserved

    def __getattr__(self, name: str) -> Any:
        """Fallback attribute access to mapping keys.

        This allows ``node.foo`` to behave like ``node["foo"]`` for
        schema data, while still supporting normal attributes for
        internals via direct assignment on the
        instance or class.
        """

        try:
            return self[name]
        except KeyError as exc:
            # Minor cosmetic change: Do not chain from KeyError for cleaner traceback
            raise AttributeError(name)

    def __setattr__(self, name: str, value: Any) -> None:
        """Route most attribute writes into the underlying mapping.

        Attributes whose names start with an underscore or are in the
        per-class ``_reserved`` set are treated as true attributes on
        the instance. All other names are stored in the dict payload
        under the same key, after honouring the frozen check. This keeps
        ``node.foo = x`` and ``node["foo"] = x`` in sync for schema
        fields while avoiding leaking runtime-only attributes into the
        mapping.
        """

        if name.startswith("_") or name in type(self)._reserved:
            object.__setattr__(self, name, value)
        else:
            self._check_frozen()
            self[name] = value

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Construct a Node backed by an underlying mapping.

        Optionally accepts a single positional ``dict`` which is used to
        seed the mapping; all keyword arguments are then routed through
        ``__setattr__`` so they become schema fields by default.
        """

        # Start from an empty dict-backed mapping.
        super().__init__()

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
        """Return a JSON-serializable structure for this Node tree.

        This walks the mapping and any nested Nodes/lists/dicts,
        converting Node instances to plain dicts recursively.
        """

        def convert(value: Any) -> Any:
            if isinstance(value, Node):
                return {k: convert(v) for k, v in value.items()}
            if isinstance(value, list):
                # Handles NodeList as it inherits from list
                return [convert(v) for v in value]
            if isinstance(value, dict):
                return {k: convert(v) for k, v in value.items()}
            return value

        return convert(self)

    def to_pretty_json(self, indent: int = 2) -> str:
        """Return an indented JSON string representation of this node."""
        return json.dumps(self.to_plain(), indent=indent)

    # --- Snapshot / restore --------------------------------------------------

    def snapshot(self) -> Any:
        """Return a JSON-serialisable snapshot of this Node tree.

        This is a semantic alias around ``to_plain`` and is intended to be
        used by higher-level code that thinks in terms of saving and
        restoring subtrees rather than pretty-printing.
        """

        return self.to_plain()

    @classmethod
    def restore(cls, snapshot: Any) -> "Node":
        """Rebuild a Node (or subclass) from a snapshot payload.

        The default implementation assumes *snapshot* is a mapping whose
        keys/values should seed the Node's dict payload as-is. Subclasses
        that need to rebuild richer structure (e.g. child NodeLists) are
        expected to override this method while keeping the signature.
        """

        if isinstance(snapshot, dict):
            if getattr(cls, "_restore_via_payload", False):
                # Subclasses that opt in are restored directly from the
                # snapshot mapping without invoking their ``__init__``
                # signature.
                return cls._from_payload(snapshot)

            try:
                node = cls(snapshot)
            except TypeError as exc:
                # Provide a clearer hint when a subclass has a custom
                # constructor that cannot accept a single mapping
                # payload. Such classes should either set
                # ``_restore_via_payload = True`` if they are pure data
                # containers on restore, or implement a custom
                # ``restore()`` that knows how to rebuild instances from
                # snapshots.
                raise TypeError(
                    f"{cls.__name__}.restore() could not call "
                    f"{cls.__name__}.__init__ with a single mapping payload. "
                    f"Either set `_restore_via_payload = True` on {cls.__name__} "
                    f"for payload-based restore, or provide a custom "
                    f"{cls.__name__}.restore() implementation."
                ) from exc

            # By default we do not assume anything about child structure
            # here; subclasses that want generic child reconstruction can
            # either override ``restore`` or call ``_restore_children``
            # explicitly after construction.
            return node
        raise TypeError(
            f"{cls.__name__}.restore() expected a mapping payload (e.g. the "
            f"result of {cls.__name__}.snapshot()), got {type(snapshot)!r}. "
            "Ensure you are passing plain dict/list/scalar data rather than "
            "an already-instantiated object."
        )

    @classmethod
    def _from_payload(cls, payload: Dict[str, Any]) -> "Node":
        """Construct *cls* from an existing mapping payload.

        This bypasses the normal ``__init__`` signature and seeds the
        underlying dict payload directly via ``Node.__init__``. It is
        intended for subclasses whose constructors take structured
        keyword arguments, but which on restore simply need the raw
        mapping data reinstated.
        """

        self = cls.__new__(cls)
        Node.__init__(self, payload)  # type: ignore[misc]
        return self  # type: ignore[return-value]

    @classmethod
    def _restore_children(cls, node: "Node", snapshot: Any) -> None:
        """Generic helper to rebuild declared child Nodes/NodeLists.

        Subclasses opt in by populating ``_node_fields`` and/or
        ``_list_fields`` and then calling ``cls._restore_children`` from
        their own ``restore``/factory methods after constructing *node*.
        This keeps the traversal logic central while letting subclasses
        define only minimal structural metadata.
        """

        if not isinstance(snapshot, dict):
            return

        # Single-node children.
        for field, child_cls in getattr(cls, "_node_fields", {}).items():
            raw = snapshot.get(field)
            if raw is not None:
                setattr(node, field, child_cls.restore(raw))

        # List-of-node children.
        for field, (list_cls, item_cls) in getattr(cls, "_list_fields", {}).items():
            items = snapshot.get(field, [])
            setattr(node, field, list_cls.restore(items, item_type=item_cls))

    # --- Immutability helpers -------------------------------------------------

    def _check_frozen(self) -> None:
        """Raise if this node has been frozen.

        Nodes are mutable by default. ``freeze()`` marks a node (and
        optionally its nested children) as frozen, after which any
        attempt to mutate the underlying mapping via dict-like methods
        will raise ``TypeError``.
        """

        if getattr(self, "_frozen", False):
            raise TypeError(
                f"{type(self).__name__} is frozen and cannot be modified. "
                "Call `thaw(deep=...)` before mutating, or avoid mutating "
                "frozen snapshots that are being shared across threads."
            )

    def freeze(self, *, deep: bool = True) -> None:
        """Mark this node (and optionally its children) as frozen.

        When ``deep`` is True, recurse into nested Nodes contained in
        values, lists, and dicts so the entire tree becomes immutable at
        the schema layer.
        """

        object.__setattr__(self, "_frozen", True)

        if deep:
            self._walk_child_nodes(lambda n: n.freeze(deep=True))

    def thaw(self, *, deep: bool = True) -> None:
        """Clear the frozen flag on this node (and optionally children).

        This restores mutability after a previous ``freeze()`` call. When
        ``deep`` is True, recurse into nested Nodes contained in values,
        lists, and dicts.
        """

        object.__setattr__(self, "_frozen", False)

        if deep:
            self._walk_child_nodes(lambda n: n.thaw(deep=True))

    def _walk_child_nodes(self, func: Callable[["Node"], None]) -> None:
        """Apply *func* to all direct child Nodes and NodeLists in values/lists/dicts.

        This is a small internal utility used by operations such as
        ``freeze``/``thaw`` that need to recurse across the schema tree
        without duplicating traversal logic.
        """

        def recurse(value: Any) -> None:
            if isinstance(value, Node):
                # Apply func to the Node (e.g., node.freeze())
                func(value)
            
            if isinstance(value, NodeList):
                # Apply func to the NodeList container itself (e.g., list.freeze())
                list_func = getattr(value, func.__name__, None)
                if list_func:
                    list_func(deep=False)
                # Then recurse through its contents
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

    # --- Merge helpers --------------------------------------------------------

    def merge(
        self,
        other: Dict[str, Any] | "Node",
        *,
        extend_lists: bool = False,
    ) -> "Node":
        """Merge another mapping or Node into this Node recursively.

        - Node fields are merged recursively.
        - Plain dicts are merged shallowly at their level.
        - Lists are either overwritten (default) or extended when
          ``extend_lists`` is True.
        - Scalars and other values are overwritten.
        """

        self._check_frozen()

        try:
            items = other.items()  # type: ignore[union-attr]
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
                    # simple dict merge at this level
                    for k, v in value.items():
                        current[k] = v

                elif extend_lists and isinstance(current, list) and isinstance(value, list):
                    current.extend(value)

                else:
                    # overwrite for everything else (including lists by default)
                    self[key] = value
            else:
                self[key] = value

        return self

    def clone(self) -> "Node":
        """Deep-clone this Node and any nested Nodes.

        This preserves runtime attributes (e.g. loaders, caches) and
        avoids calling ``__init__`` on subclasses, so ``Loadable``
        nodes and custom constructors remain valid on the clone.
        """

        return self._clone_recursive({})

    def _clone_recursive(self, memo: Dict[int, "Node"]) -> "Node":
        # Sanity check: Node subclasses are expected to remain dict-backed.
        if not isinstance(self, dict):
            raise TypeError(
                "Node subclass must remain dict-backed for clone() to work. "
                "If you override the base type, ensure your class still "
                "inherits from dict or override clone() with custom logic."
            )

        # Cycle detection for graphs with shared or cyclical references.
        obj_id = id(self)
        if obj_id in memo:
            return memo[obj_id]

        # Allocate an uninitialised instance of our concrete subclass.
        new = object.__new__(type(self))
        memo[obj_id] = new

        # Rebuild the dict payload directly without going through
        # ``__init__`` / ``__setattr__``.
        dict.__init__(new, {})

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

        # Copy non-mapping attributes (loaders, caches, etc.) directly
        # onto the instance, bypassing Node.__setattr__ so we don't
        # accidentally treat them as schema fields.
        for attr, val in self.__dict__.items():
            object.__setattr__(new, attr, val)

        return new

    # --- dict mutation guards -------------------------------------------------

    def __setitem__(self, key: Any, value: Any) -> None:  # type: ignore[override]
        # Prevent payload from stomping on reserved attribute names.
        if key in type(self)._reserved:
            raise KeyError(f"Cannot set reserved attribute {key!r} in Node payload")

        self._check_frozen()
        super().__setitem__(key, value)

    def __delitem__(self, key: Any) -> None:  # type: ignore[override]
        self._check_frozen()
        super().__delitem__(key)

    def clear(self) -> None:  # type: ignore[override]
        self._check_frozen()
        super().clear()

    def pop(self, key: Any, *args: Any) -> Any:  # type: ignore[override]
        self._check_frozen()
        return super().pop(key, *args)

    def popitem(self) -> Any:  # type: ignore[override]
        self._check_frozen()
        return super().popitem()

    def update(self, *args: Any, **kwargs: Any) -> None:  # type: ignore[override]
        self._check_frozen()
        super().update(*args, **kwargs)


T = TypeVar("T", bound="Node")


class NodeList(List[T], Generic[T]):
    """Common base for list-like collections of Node subclasses.

    This adds convenience helpers and ensures the collection respects the
    frozen/immutable state inherited from its parent Node graph.
    """
    
    # Use a hidden attribute for internal frozen state, consistent with Node.
    _frozen: bool

    def _check_frozen(self) -> None:
        """Raise if this list has been frozen."""

        if getattr(self, "_frozen", False):
            raise TypeError(f"{type(self).__name__} is frozen and cannot be modified")

    def freeze(self, *, deep: bool = True) -> None:
        """Mark this list (and optionally its children) as frozen."""

        object.__setattr__(self, "_frozen", True)

        if deep:
            for item in self:
                if isinstance(item, Node):
                    item.freeze(deep=True)

    def thaw(self, *, deep: bool = True) -> None:
        """Clear the frozen flag on this list (and optionally children)."""

        object.__setattr__(self, "_frozen", False)

        if deep:
            for item in self:
                if isinstance(item, Node):
                    item.thaw(deep=True)

    # --- Mutation guards ------------------------------------------------------
    # Override mutation methods to enforce the frozen check.

    def append(self, __object: T) -> None:
        self._check_frozen()
        super().append(__object)

    def extend(self, __iterable: Iterable[T]) -> None:
        self._check_frozen()
        super().extend(__iterable)

    def insert(self, __index: int, __object: T) -> None:
        self._check_frozen()
        super().insert(__index, __object)

    def pop(self, __index: int = -1) -> T:
        self._check_frozen()
        return super().pop(__index)

    def remove(self, __value: T) -> None:
        self._check_frozen()
        super().remove(__value)

    def reverse(self) -> None:
        self._check_frozen()
        super().reverse()

    def sort(self, **kwargs) -> None:  # type: ignore[no-untyped-def]
        self._check_frozen()
        super().sort(**kwargs)

    def __setitem__(self, __key: int | slice, __value: T | Iterable[T]) -> None:  # type: ignore[override]
        self._check_frozen()
        super().__setitem__(__key, __value)

    def __delitem__(self, __key: int | slice) -> None:
        self._check_frozen()
        super().__delitem__(__key)

    # --- Original methods (retained) ------------------------------------------

    def iter(self) -> Iterable[T]:
        """Iterate over nodes in this collection."""

        return iter(self)

    def snapshot(self) -> Any:
        """Return a JSON-serialisable list snapshot for this collection."""

        return [getattr(item, "snapshot", lambda: item)() for item in self]

    @classmethod
    def restore(cls, snapshots: Iterable[Any], item_type: type[T]) -> "NodeList[T]":
        """Rebuild a NodeList from an iterable of element snapshots.

        ``item_type`` is the concrete Node subclass to construct for each
        element. It must provide a compatible ``restore`` classmethod.
        """

        lst: "NodeList[T]" = cls()
        for snap in snapshots:
            if isinstance(snap, item_type):  # already constructed
                lst.append(snap)
            else:
                lst.append(item_type.restore(snap))
        return lst

    def to_pretty_json(self, indent: int = 2) -> str:
        """Return an indented JSON string for this collection of nodes."""

        # Use each element's to_plain() if available, otherwise the value itself.
        payload = [getattr(item, "to_plain", lambda: item)() for item in self]
        return json.dumps(payload, indent=indent)