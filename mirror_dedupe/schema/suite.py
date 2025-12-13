from __future__ import annotations

from typing import Any, Dict

from .node import Node, NodeList


class Suite(Node):
    """Single suite label (e.g. "noble", "jammy")."""

    def __init__(self, *, name: str) -> None:
        data: Dict[str, Any] = {"name": name}
        super().__init__(data)


class Suites(NodeList[Suite]):
    """Collection of Suite nodes.

    This models the set of logical suites discovered for a repository
    (e.g. "noble", "jammy"). It does not encode pockets; those can be
    layered on separately in other nodes.
    """
