## @file index.py
##
## @brief Generic index file descriptor and collection.
##
## An ``Index`` represents a single index metadata file (e.g.
## ``Packages.gz``, ``Sources.xz``).  It is repo-type agnostic at the
## top level and provides a place for parsers to attach their own
## payloads under a namespaced key.  ``Indices`` is the corresponding
## ``NodeList`` wrapper.
##
## @copyright Copyright (c) 2026 Tim Hosking
## @see https://github.com/munger
## @par Licence: MIT

from __future__ import annotations

from typing import Any, Dict

from .node import Node, NodeList


class Index(Node):
    ## @brief Generic descriptor for a single index file.
    ##
    ## Required fields:
    ##
    ## * ``path`` — relative path under the repo root
    ## * ``kind`` — logical kind, e.g. ``"packages"``, ``"sources"``
    ##
    ## Optional fields:
    ##
    ## * ``metadata`` — parser-specific data stored in an ``Index.Metadata`` node

    _restore_via_payload = True

    def __init__(
        self,
        *,
        path: str,
        kind: str,
        metadata: "Index.Metadata" | None = None,
    ) -> None:
        data: Dict[str, Any] = {
            "path": path,
            "kind": kind,
        }
        if metadata is not None:
            data["metadata"] = metadata
        super().__init__(data)

    class Metadata(Node):
        ## @brief Base class for parser-specific index payloads.
        ##
        ## Parsers are free to subclass this to provide a more structured
        ## schema for their index metadata while keeping the outer Index
        ## envelope generic.

        def __init__(self, **fields: Any) -> None:
            super().__init__(dict(fields))


class Indices(NodeList[Index]):
    ## @brief Container for Index descriptors.
    ##
    ## This is just a plain list of ``Index`` nodes.  Any schema or
    ## metadata lives either on the individual ``Index`` instances or
    ## on the parent repo, not on this list type itself.
    pass
