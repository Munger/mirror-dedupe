from __future__ import annotations

from typing import Any, Dict, List

from .node import Node, NodeList


class Upstream(Node):
    """Represents a single upstream endpoint for a Repo.

    Core fields capture the information needed by scan/discovery and
    sync policy:

    - url: base HTTP/HTTPS URL for discovery and sync
    - sync_method: preferred sync method for this upstream ("https",
      "http", "rsync", etc.)
    - ipv6_ok: whether IPv6 is known to work for this upstream
    - rsync_roots: optional list of rsync daemon roots discovered for this upstream

    An open-ended metadata mapping is also provided so callers can
    attach additional per-upstream details without changing the
    schema.
    """

    class Metadata(Node):
        """Opaque per-upstream metadata container.

        This nested Node allows callers to attach additional structured
        information to an Upstream without changing the top-level
        schema. The payload is an arbitrary mapping.
        """

        def __init__(self, **values: Any) -> None:  # type: ignore[override]
            super().__init__(dict(values))

    def __init__(
        self,
        *,
        url: str,
        sync_method: str | None = "https",
        ipv6_ok: bool | None = True,
        rsync_roots: List[str] | None = None,
        metadata: "Upstream.Metadata" | None = None,
    ) -> None:
        data: Dict[str, Any] = {
            "url": url,
        }
        if sync_method is not None:
            data["sync_method"] = sync_method
        if ipv6_ok is not None:
            data["ipv6_ok"] = ipv6_ok
        if rsync_roots is not None:
            data["rsync_roots"] = rsync_roots
        if metadata is not None:
            data["metadata"] = metadata
        super().__init__(data)


class Upstreams(NodeList[Upstream]):
    """Collection wrapper for Upstream nodes."""
