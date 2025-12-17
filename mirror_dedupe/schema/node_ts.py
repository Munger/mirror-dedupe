"""node_ts.py

  Thread-safe variants of the Node/NodeList primitives.

  This module defines optional concurrency-aware types that can be used
  in place of the core dict-backed graph primitives when a tree of
  objects is shared across threads:

  * ``TSNode``      – a locking ``Node`` variant that constrains
    payload values to other TSNode/TSNodeList objects or immutable
    scalars/tuples.
  * ``TSNodeList``  – a list-like container restricted to TSNode
    elements with a per-instance lock around list mutations.
  * ``NodeTransaction`` – a small helper that acquires multiple TSNode
    locks in a stable order for multi-node critical sections.

  These types provide mutual exclusion for structural mutations but do
  not implement full transactional semantics such as rollback.

Copyright (c) 2025 Tim Hosking
Website: https://github.com/munger
Licence: MIT
"""

from __future__ import annotations

import threading
from typing import Any, Callable, Generic, Iterable, TypeVar

from .node import Node, NodeList


TNode = TypeVar("TNode", bound="TSNode")


class TSNode(Node):
    """Thread-safe variant of Node.

    This wraps all mutating operations with a re-entrant lock. It is
    intended for scenarios where a Node tree may be accessed from
    multiple threads within a single process. For most use cases the
    plain ``Node`` class is sufficient and avoids locking overhead.
    
    IMPORTANT: TSNode trees must only contain other TSNodes, TSNodeLists,
    and immutable values (str, int, float, bool, None, tuples of immutable).
    Raw dicts, plain lists, and regular Node instances are not allowed as
    they would not be protected by the locking mechanism.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        # Per-instance re-entrant lock for mutation. This must be
        # initialised before calling ``Node.__init__`` so that any
        # dict/attribute writes it performs (which may go via the
        # overridden mutators) can safely acquire the lock.
        object.__setattr__(self, "_lock", threading.RLock())
        # Initialise the underlying mapping.
        super().__init__(*args, **kwargs)

    # --- validation helpers -----------------------------------------------

    def _validate_value(self, value: Any) -> None:
        """Ensure value is safe for thread-safe tree.
        
        Raises TypeError if value would break thread-safety guarantees.
        """
        # TSNode and TSNodeList are always safe
        if isinstance(value, (TSNode, TSNodeList)):
            return
        
        # Plain Node instances are NOT safe - they lack locking
        if isinstance(value, Node):
            raise TypeError(
                f"TSNode cannot contain plain Node instances. "
                f"Convert to TSNode or use a regular Node tree instead. "
                f"Got: {type(value).__name__}"
            )
        
        # Plain lists are NOT safe - use TSNodeList
        if isinstance(value, list) and not isinstance(value, TSNodeList):
            raise TypeError(
                f"TSNode cannot contain plain lists. "
                f"Use TSNodeList instead to maintain thread safety."
            )
        
        # Raw dicts are NOT safe (unless they're Node subclasses, already caught above)
        if isinstance(value, dict) and not isinstance(value, Node):
            raise TypeError(
                f"TSNode cannot contain raw dicts. "
                f"Wrap in TSNode to maintain thread safety."
            )
        
        # Immutable primitives are safe
        if value is None or isinstance(value, (str, int, float, bool, bytes)):
            return
        
        # Tuples are safe if their contents are safe (recurse)
        if isinstance(value, tuple):
            for item in value:
                self._validate_value(item)
            return
        
        # Anything else is potentially unsafe - be conservative
        raise TypeError(
            f"TSNode cannot safely contain {type(value).__name__}. "
            f"Only TSNode, TSNodeList, and immutable values (str, int, float, "
            f"bool, None, tuples) are allowed."
        )

    # --- lock helpers -----------------------------------------------------

    def _with_lock(self, func: Callable[[], Any]) -> Any:
        lock = getattr(self, "_lock")
        with lock:
            return func()

    @property
    def lock(self) -> threading.RLock:
        """Expose the per-instance lock for coordination helpers.

        This is primarily intended for utilities such as NodeTransaction
        that need to acquire multiple node locks in a well-defined
        order. Normal callers should use the node's public API rather
        than manipulating the lock directly.
        """

        return getattr(self, "_lock")

    # --- core dict/attribute mutators ------------------------------------

    def __setitem__(self, key: Any, value: Any) -> None:  # type: ignore[override]
        self._validate_value(value)
        self._with_lock(lambda: super(TSNode, self).__setitem__(key, value))

    def __delitem__(self, key: Any) -> None:  # type: ignore[override]
        self._with_lock(lambda: super(TSNode, self).__delitem__(key))

    def __setattr__(self, name: str, value: Any) -> None:  # type: ignore[override]
        # Skip validation for internal attributes (they're not part of the schema tree)
        if not (name.startswith("_") or name in type(self)._reserved):
            self._validate_value(value)
        
        def op() -> None:
            super(TSNode, self).__setattr__(name, value)

        self._with_lock(op)

    def pop(self, key: Any, *args: Any) -> Any:  # type: ignore[override]
        return self._with_lock(lambda: super(TSNode, self).pop(key, *args))

    def popitem(self) -> Any:  # type: ignore[override]
        return self._with_lock(lambda: super(TSNode, self).popitem())

    def update(self, *args: Any, **kwargs: Any) -> None:  # type: ignore[override]
        def op() -> None:
            # Validate all incoming values before updating
            if args:
                if len(args) > 1:
                    raise TypeError("update expected at most 1 argument")
                other = args[0]
                if hasattr(other, "items"):
                    for v in other.values():
                        self._validate_value(v)
                else:
                    for k, v in other:
                        self._validate_value(v)
            
            for v in kwargs.values():
                self._validate_value(v)
            
            super(TSNode, self).update(*args, **kwargs)

        self._with_lock(op)

    # --- higher-level helpers that mutate state --------------------------

    def merge(
        self,
        other: dict[str, Any] | Node,
        *,
        extend_lists: bool = False,
    ) -> TSNode:  # type: ignore[override]
        def op() -> TSNode:
            # Validate the entire incoming tree before merging
            if isinstance(other, dict):
                for v in other.values():
                    self._validate_value(v)
            
            return super(TSNode, self).merge(other, extend_lists=extend_lists)  # type: ignore[return-value]

        return self._with_lock(op)

    def thaw(self, *, deep: bool = True) -> None:  # type: ignore[override]
        def op() -> None:
            super(TSNode, self).thaw(deep=deep)

        self._with_lock(op)

    def freeze(self, *, deep: bool = True) -> None:  # type: ignore[override]
        def op() -> None:
            super(TSNode, self).freeze(deep=deep)

        self._with_lock(op)


class TSNodeList(NodeList[TNode], Generic[TNode]):
    """Thread-safe variant of NodeList.

    This wraps list-mutating operations with a re-entrant lock. It
    expects its elements to be ``TSNode`` instances to maintain thread
    safety guarantees across the entire tree.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        # Per-instance re-entrant lock for list mutations. Initialise
        # this before calling the base ``NodeList.__init__`` so that any
        # overridden mutators invoked during construction can safely
        # acquire the lock.
        object.__setattr__(self, "_lock", threading.RLock())
        super().__init__(*args, **kwargs)

    def _with_lock(self, func: Callable[[], Any]) -> Any:
        lock = getattr(self, "_lock")
        with lock:
            return func()

    # --- list mutators ----------------------------------------------------

    def _check_item(self, item: TNode) -> None:
        if not isinstance(item, TSNode):
            raise TypeError(
                f"TSNodeList only accepts TSNode elements. "
                f"Got {type(item).__name__}. "
                f"Convert plain Nodes to TSNodes or use regular NodeList."
            )

    def append(self, item: TNode) -> None:  # type: ignore[override]
        self._check_item(item)
        self._with_lock(lambda: super(TSNodeList, self).append(item))

    def extend(self, items: Iterable[TNode]) -> None:  # type: ignore[override]
        def op() -> None:
            for i in items:
                self._check_item(i)
            super(TSNodeList, self).extend(items)

        self._with_lock(op)

    def insert(self, index: int, item: TNode) -> None:  # type: ignore[override]
        self._check_item(item)
        self._with_lock(lambda: super(TSNodeList, self).insert(index, item))

    def pop(self, index: int = -1) -> TNode:  # type: ignore[override]
        return self._with_lock(lambda: super(TSNodeList, self).pop(index))

    def remove(self, item: TNode) -> None:  # type: ignore[override]
        self._with_lock(lambda: super(TSNodeList, self).remove(item))

    def clear(self) -> None:  # type: ignore[override]
        self._with_lock(lambda: super(TSNodeList, self).clear())

    def __setitem__(self, index, value) -> None:  # type: ignore[override]
        # Support both single index assignment and slice assignment.
        def op() -> None:
            if isinstance(index, slice):
                for v in value:
                    self._check_item(v)
            else:
                self._check_item(value)
            super(TSNodeList, self).__setitem__(index, value)

        self._with_lock(op)

    def __delitem__(self, index) -> None:  # type: ignore[override]
        self._with_lock(lambda: super(TSNodeList, self).__delitem__(index))


class NodeTransaction:
    """Context manager to lock multiple TSNode instances at once.

    Locks are acquired in a stable order (by ``id``) to avoid deadlocks
    when different call sites lock overlapping sets of nodes.

    This provides mutual exclusion only; it does not guarantee
    consistency, isolation, or rollback.
    
    Example usage:
        with NodeTransaction(parent, parent.child, sibling):
            parent.child.value = sibling.value
            parent.status = "updated"
    
    Note: You must manually identify and pass ALL nodes that will be
    accessed during the transaction. The context manager cannot
    automatically discover which nodes will be touched.
    """

    def __init__(self, *nodes: TSNode) -> None:
        # Deduplicate and sort by object id for a stable lock order.
        self._nodes = sorted(set(nodes), key=id)

    def __enter__(self) -> "NodeTransaction":
        for n in self._nodes:
            n.lock.acquire()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        # Release in reverse order of acquisition.
        for n in reversed(self._nodes):
            n.lock.release()
        # Do not suppress exceptions.
        return False