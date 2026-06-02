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

import json
from typing import Any, ClassVar, Dict, List, Generic, Iterable, Tuple, Type, TypeVar, Callable


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

    def __init_subclass__(cls, **kwargs: Any) -> None:
        ## @brief Collect reserved attribute names for each subclass.
        ##
        ## Methods, class attributes, properties, and annotated fields
        ## are treated as real attributes rather than schema payload keys.

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

        if key in type(self)._reserved:
            raise KeyError(f"Cannot set reserved attribute {key!r} in Node payload")
        self._check_frozen()
        super().__setitem__(key, value)

    def __delitem__(self, key: Any) -> None:
        self._check_frozen()
        super().__delitem__(key)

    def clear(self) -> None:
        self._check_frozen()
        super().clear()

    def pop(self, key: Any, *args: Any) -> Any:
        self._check_frozen()
        return super().pop(key, *args)

    def popitem(self) -> Any:
        self._check_frozen()
        return super().popitem()

    def update(self, *args: Any, **kwargs: Any) -> None:
        self._check_frozen()
        super().update(*args, **kwargs)


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

    def sort(self, **kwargs) -> None:
        self._check_frozen()
        super().sort(**kwargs)

    def __setitem__(self, __key: int | slice, __value: T | Iterable[T]) -> None:
        self._check_frozen()
        super().__setitem__(__key, __value)

    def __delitem__(self, __key: int | slice) -> None:
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
