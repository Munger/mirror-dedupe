## @file package.py
##
## @brief Transient leaf-node descriptor for a single package file.
##
## A ``Package`` represents a single downloadable file (e.g. a ``.deb``
## or ``.dsc``) discovered by parsing an index.  It carries the minimal
## fields needed for pool-based sync - ``path``, ``hash``, ``size``,
## ``uri`` - and inherits ``Node.sync()`` which handles pool check,
## stream download, and hardlink.
##
## ``Package`` nodes are **ephemeral**: created during index parsing,
## grafted onto their parent ``Index`` for the sync tree walk, then
## garbage collected.  They are never persisted in snapshots or the
## schema tree between runs.
##
## @copyright Copyright (c) 2026 Tim Hosking
## @see https://github.com/munger
## @par Licence: MIT

from __future__ import annotations

from typing import Any, Dict

from .mdnode import MDNode as Node
from ..lib.node_x import SerialisableNodeList as NodeList


class Package(Node):
    ## @brief Transient leaf-node for a single package file.

    _restore_via_payload = True

    def __init__(
        self,
        *,
        path: str,
        hash: str,
        size: int,
        uri: str = "",
    ) -> None:
        ## @brief Initialise a transient Package leaf node.
        ##
        ## @param path  Relative path under the repo root.
        ## @param hash  SHA-256 checksum.
        ## @param size  File size in bytes.
        ## @param uri   Package URI for fetching.
        ## @return None
        data: Dict[str, Any] = {
            "path": path,
            "hash": hash,
            "size": size,
        }
        if uri:
            data["uri"] = uri
        super().__init__(data)

    @property
    def checksum(self) -> str:
        ## @brief SHA-256 checksum from the ``hash`` field.
        ## @return The checksum string.
        return self.get("hash", "")


class Packages(NodeList[Package]):
    ## @brief Container for Package instances.
    pass
