## @file upstream.py
##
## @brief Upstream endpoint descriptor and collection.
##
## An ``Upstream`` captures the base URL, preferred sync method, and
## optional metadata for a single remote source. ``Upstreams`` is the
## corresponding ``NodeList`` wrapper.
##
## @copyright Copyright (c) 2026 Tim Hosking
## @see https://github.com/munger
## @par Licence: MIT

from __future__ import annotations

from typing import Any, Dict, List

from .node import Node, NodeList


class Upstream(Node):
    ## @brief A single upstream endpoint for a Repo.
    ##
    ## Core fields capture the information needed by scan/discovery and
    ## sync policy:
    ##
    ## * ``url``         — base HTTP/HTTPS URL for discovery and sync
    ## * ``sync_method`` — preferred sync method (e.g. ``"https"``, ``"http"``)
    ## * ``ipv6_ok``     — whether IPv6 is known to work for this upstream
    ##
    ## An open-ended metadata mapping is also provided so callers can
    ## attach additional per-upstream details without changing the schema.

    class Metadata(Node):
        ## @brief Opaque per-upstream metadata container.
        ##
        ## Allows callers to attach additional structured information to an
        ## Upstream without changing the top-level schema. The payload is
        ## an arbitrary mapping.

        def __init__(self, **values: Any) -> None:  # type: ignore[override]
            super().__init__(dict(values))

    def __init__(
        self,
        *,
        url: str,
        sync_method: str | None = "https",
        ipv6_ok: bool | None = True,
        metadata: "Upstream.Metadata" | None = None,
    ) -> None:
        data: Dict[str, Any] = {
            "url": url,
        }
        if sync_method is not None:
            data["sync_method"] = sync_method
        if ipv6_ok is not None:
            data["ipv6_ok"] = ipv6_ok
        if metadata is not None:
            data["metadata"] = metadata
        super().__init__(data)


class Upstreams(NodeList[Upstream]):
    ## @brief Collection wrapper for Upstream nodes.
    pass
