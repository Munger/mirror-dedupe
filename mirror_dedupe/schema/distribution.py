## @file distribution.py
##
## @brief Per-distribution descriptor and collection.
##
## A ``Distribution`` represents a logical suite or suite/pocket entry
## discovered under ``/dists`` for repo types that follow an APT-like
## layout.  ``Distributions`` is the corresponding ``NodeList`` wrapper.
##
## @copyright Copyright (c) 2026 Tim Hosking
## @see https://github.com/munger
## @par Licence: MIT

from __future__ import annotations

from typing import Any, Dict

from .mdnode import MDNode as Node
from ..lib.node_x import SerialisableNodeList as NodeList
from .component import Components
from .architecture import Architectures


class Distribution(Node):
    ## @brief Per-distribution node.
    ##
    ## Captures high-level facts about a suite and the location/shape of
    ## its Release-like metadata.

    _restore_via_payload = True

    def __init__(
        self,
        *,
        name: str,
        has_release: bool,
        components: Components,
        architectures: Architectures,
        release_path: str,
        metadata: "Distribution.Metadata" | None = None,
    ) -> None:
        ## @brief Initialise a Distribution descriptor.
        ##
        ## @param name           The distribution label (suite/pocket).
        ## @param has_release    Whether a Release file was found.
        ## @param components     Discovered components.
        ## @param architectures  Discovered architectures.
        ## @param release_path   Relative path to the Release file.
        ## @param metadata       Optional distribution metadata.
        ## @return None
        data: Dict[str, Any] = {
            "name": name,
            "has_release": has_release,
            "components": components,
            "architectures": architectures,
            "release_path": release_path,
        }
        if metadata is not None:
            data["metadata"] = metadata
        super().__init__(data)

    class Metadata(Node):
        ## @brief Per-distribution metadata envelope.
        ##
        ## Repo-specific implementations (e.g. APT Release metadata)
        ## should subclass this type to make their ownership explicit
        ## while keeping the outer Distribution envelope generic.

        def __init__(self, **fields: Any) -> None:
            ## @brief Initialise distribution metadata from keyword fields.
            ##
            ## @param fields  Arbitrary keyword fields for the metadata envelope.
            ## @return None
            super().__init__(dict(fields))


class Distributions(NodeList[Distribution]):
    ## @brief Container for Distribution descriptors.
    ##
    ## This is just a plain list of ``Distribution`` nodes.  Any schema or
    ## metadata lives either on the individual ``Distribution`` instances
    ## or on the parent repo, not on this list type itself.
    pass
