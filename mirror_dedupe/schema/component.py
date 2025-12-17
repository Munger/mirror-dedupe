from __future__ import annotations

from typing import Any, Dict

from .node import Node, NodeList


class Component(Node):
    """Dict-backed descriptor for a single component label.

    As with Architecture, this is a hook for future per-component
    metadata while keeping Repo JSON-shaped.
    """

    # On restore we want to seed the underlying mapping directly from the
    # snapshot payload, bypassing the keyword-only constructor.
    _restore_via_payload = True

    def __init__(
        self,
        *,
        name: str,
        metadata: Dict[str, Any] | None = None,
    ) -> None:
        data: Dict[str, Any] = {"name": name}
        if metadata is not None:
            data["metadata"] = metadata
        super().__init__(data)


class Components(NodeList[Component]):
    """Container for Component descriptors.

    This is just a plain list of ``Component`` nodes. Any schema or
    metadata lives either on the individual ``Component`` instances or
    on the parent repo, not on this list type itself.
    """
