## @file upstream.py
##
## @brief Upstream endpoint descriptor and collection.
##
## An ``Upstream`` captures the base URL and optional metadata for a
## single remote source. The URL scheme alone determines the sync
## protocol. ``Upstreams`` is the corresponding ``NodeList`` wrapper.
##
## @copyright Copyright (c) 2026 Tim Hosking
## @see https://github.com/munger
## @par Licence: MIT


from typing import Any, Dict, List

from .mdnode import MDNode as Node
from ..lib.node_x import SerialisableNodeList as NodeList


class Upstream(Node):
    ## @brief A single upstream endpoint for a Repo.
    ##
    ## Core fields capture the information needed by scan/discovery and
    ## sync policy:
    ##
    ## * ``url`` - base HTTP/HTTPS URL for discovery and sync
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
            ## @brief Initialise upstream metadata from keyword values.
            ##
            ## @param values  Arbitrary keyword values for the metadata container.
            ## @return None
            super().__init__(dict(values))

    def __init__(
        self,
        *,
        url: str,
        metadata: "Upstream.Metadata" | None = None,
    ) -> None:
        ## @brief Initialise an Upstream endpoint descriptor.
        ##
        ## @param url       Base HTTP/HTTPS URL for discovery and sync.
        ## @param metadata  Optional per-upstream metadata.
        ## @return None
        data: Dict[str, Any] = {
            "url": url,
        }
        if metadata is not None:
            data["metadata"] = metadata
        super().__init__(data)


class Upstreams(NodeList[Upstream]):
    ## @brief Collection wrapper for Upstream nodes.
    pass
