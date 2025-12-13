from __future__ import annotations

import json
from typing import Any, Dict, List, Generic, Iterable, TypeVar

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
    _reserved: set[str] = set()

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
            raise AttributeError(name) from exc

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
                raise TypeError("Node accepts at most one positional argument")
            initial = args[0]
            if isinstance(initial, dict):
                for k, v in initial.items():
                    self[k] = v
            else:
                raise TypeError("Positional arg to Node must be a dict")

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
                return [convert(v) for v in value]
            if isinstance(value, dict):
                return {k: convert(v) for k, v in value.items()}
            return value

        return convert(self)

    def to_pretty_json(self, indent: int = 2) -> str:
        """Return an indented JSON string representation of this node."""
        return json.dumps(self.to_plain(), indent=indent)

    # --- Immutability helpers -------------------------------------------------

    def _check_frozen(self) -> None:
        """Raise if this node has been frozen.

        Nodes are mutable by default. ``freeze()`` marks a node (and
        optionally its nested children) as frozen, after which any
        attempt to mutate the underlying mapping via dict-like methods
        will raise ``TypeError``.
        """

        if getattr(self, "_frozen", False):
            raise TypeError(f"{type(self).__name__} is frozen and cannot be modified")

    def freeze(self, *, deep: bool = True) -> None:
        """Mark this node (and optionally its children) as frozen.

        When ``deep`` is True, recurse into nested Nodes contained in
        values, lists, and dicts so the entire tree becomes immutable at
        the schema layer.
        """

        object.__setattr__(self, "_frozen", True)

        if not deep:
            return

        for value in self.values():
            if isinstance(value, Node):
                value.freeze(deep=True)
            elif isinstance(value, list):
                for v in value:
                    if isinstance(v, Node):
                        v.freeze(deep=True)
            elif isinstance(value, dict):
                for v in value.values():
                    if isinstance(v, Node):
                        v.freeze(deep=True)

    def thaw(self, *, deep: bool = True) -> None:
        """Clear the frozen flag on this node (and optionally children).

        This restores mutability after a previous ``freeze()`` call. When
        ``deep`` is True, recurse into nested Nodes contained in values,
        lists, and dicts.
        """

        object.__setattr__(self, "_frozen", False)

        if not deep:
            return

        for value in self.values():
            if isinstance(value, Node):
                value.thaw(deep=True)
            elif isinstance(value, list):
                for v in value:
                    if isinstance(v, Node):
                        v.thaw(deep=True)
            elif isinstance(value, dict):
                for v in value.values():
                    if isinstance(v, Node):
                        v.thaw(deep=True)

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

        items = other.items()  # type: ignore[union-attr]

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
            raise TypeError("Node subclass must remain dict-backed.")

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

    This adds small convenience helpers without changing list semantics.
    """

    def iter(self) -> Iterable[T]:
        """Iterate over nodes in this collection."""

        return iter(self)

    def to_pretty_json(self, indent: int = 2) -> str:
        """Return an indented JSON string for this collection of nodes."""

        # Use each element's to_plain() if available, otherwise the value itself.
        payload = [getattr(item, "to_plain", lambda: item)() for item in self]
        return json.dumps(payload, indent=indent)
