from __future__ import annotations

from typing import Any, Dict, List
import hashlib

from .node import Node, NodeList


class Release(Node):
    """Dict-backed descriptor for a single Release-derived file.

    This envelopes both the human-facing suite/pocket information and
    various pieces of data that layout-specific parsers consider
    important to REST clients.

    Required fields (stored as mapping keys):

        suite: str            # logical suite name
        pocket: str | None    # logical pocket name or ``None``
        upstream: str         # upstream URL where this Release lives
        relative_dir: str
        path: str
        url: str
        repo_type: str
        kind: str

    Optional fields:

        signature_extension: str
    """

    # On restore we want to seed the underlying mapping directly from the
    # snapshot payload, bypassing the keyword-only constructor.
    _restore_via_payload = True

    def __init__(
        self,
        *,
        suite: str,
        pocket: str | None,
        upstream: str,
        relative_dir: str,
        path: str,
        url: str,
        repo_type: str,
        kind: str,
        signature_extension: str | None = None,
        digest: "Release.Digest | None" = None,
    ) -> None:
        data: Dict[str, Any] = {
            "suite": suite,
            "pocket": pocket,
            "upstream": upstream,
            "relative_dir": relative_dir,
            "path": path,
            "url": url,
            "repo_type": repo_type,
            "kind": kind,
        }
        if signature_extension is not None:
            data["signature_extension"] = signature_extension
        if digest is not None:
            data["digest"] = digest
        super().__init__(data)

    class Digest(Node):
        """Digest describing this Release's body.

        Callers provide the raw Release text so we can compute cheap
        comparison information (hash/length), but only those summary
        fields are persisted in the mapping to keep scan outputs lean.
        This type is scoped under Release to make ownership explicit in
        the type hierarchy.
        """

        def __init__(self, *, text: str) -> None:
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
            data: Dict[str, Any] = {
                "sha256": digest,
                "length": len(text),
            }
            super().__init__(data)


class Releases(NodeList[Release]):
    """Container for Release descriptors.

    This is just a plain list of ``Release`` nodes. Any schema or
    metadata lives either on the individual ``Release`` instances or on
    the parent repo, not on this list type itself.
    """

    def iter(self):
        """Iterate over Release nodes in this collection."""

        return iter(self)
