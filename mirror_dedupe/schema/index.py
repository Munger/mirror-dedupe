from __future__ import annotations

from typing import Any, Dict

from .node import Node, NodeList


class Index(Node):
    """Generic descriptor for a single index file.

    This is repo-type agnostic at the top level and provides a place
    for parsers to attach their own payloads under a namespaced key
    (e.g. "apt", "yum", etc.).

    Required fields (stored as mapping keys):

        url: str
        path: str
        kind: str        # logical kind, e.g. "packages", "sources"

    Optional fields:

        metadata: Index.Metadata    # parser-specific data
    """

    # On restore we want to seed the underlying mapping directly from the
    # snapshot payload, bypassing the keyword-only constructor.
    _restore_via_payload = True

    def __init__(
        self,
        *,
        url: str,
        path: str,
        kind: str,
        metadata: "Index.Metadata" | None = None,
    ) -> None:
        data: Dict[str, Any] = {
            "url": url,
            "path": path,
            "kind": kind,
        }
        if metadata is not None:
            data["metadata"] = metadata
        super().__init__(data)

    class Metadata(Node):
        """Base class for parser-specific index payloads.

        Parsers are free to subclass this to provide a more structured
        schema for their index metadata while keeping the outer Index
        envelope generic.
        """

        def __init__(self, **fields: Any) -> None:
            super().__init__(dict(fields))


class Indices(NodeList[Index]):
    """Container for Index descriptors.

    This is just a plain list of ``Index`` nodes. Any schema or
    metadata lives either on the individual ``Index`` instances or
    on the parent repo, not on this list type itself.
    """

    def iter(self):
        """Iterate over Index nodes in this collection."""

        return iter(self)
