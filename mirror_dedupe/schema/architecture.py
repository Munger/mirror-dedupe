from __future__ import annotations

from typing import Any, Dict

from .node import Node, NodeList


class Architecture(Node):
    """Dict-backed descriptor for a single architecture label.

    For now this carries only a name plus an optional metadata dict. It
    exists so that richer per-arch metadata can be added without
    changing the Repo shape.
    """

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


class Architectures(NodeList[Architecture]):
    """Container for Architecture descriptors.

    This is just a plain list of ``Architecture`` nodes. Any schema or
    metadata lives either on the individual ``Architecture`` instances
    or on the parent repo, not on this list type itself.
    """
