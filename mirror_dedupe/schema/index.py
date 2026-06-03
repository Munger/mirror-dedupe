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

from typing import Any, Dict, Optional

from .node import Node, NodeList
from .package import Package, Packages


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
        metadata: "Index.Metadata | None" = None,
        uri: str = "",
    ) -> None:
        ## @brief Initialise an Index descriptor.
        ##
        ## @param path      Relative path under the repo root.
        ## @param kind      Logical kind (e.g. ``"packages"``).
        ## @param metadata  Optional index metadata.
        ## @param uri       Index URI for fetching.
        ## @return None
        data: Dict[str, Any] = {
            "path": path,
            "kind": kind,
        }
        if uri:
            data["uri"] = uri
        if metadata is not None:
            data["metadata"] = metadata
        super().__init__(data)

    @property
    def checksum(self) -> str:
        ## @brief SHA-256 checksum from attached metadata (if any).
        ## @return The checksum string, or ``""`` if absent.
        md = self.get("metadata")
        if md:
            return md.get("checksum", "")
        return ""

    def _parse_packages(self, text: str, uri: str = "") -> "Packages":
        ## @brief Virtual: parse *text* into ``Package`` children.
        ##
        ## Subclasses override this with format-specific index parsing.
        ## The base implementation returns an empty ``Packages`` so that
        ## non-package indices (e.g. Sources) are harmless.
        ##
        ## @param text  Decompressed index text.
        ## @param uri   The URI of this index (for building package URIs).
        ## @return A ``Packages`` NodeList.
        return Packages()

    def parse(self) -> "Index":
        ## @brief Decompress raw bytes and parse into child Package nodes.
        ## @return This Index (with ``packages`` populated).
        data = self._raw_bytes
        if not data:
            return self
        path = self.get("path", "")
        if path.endswith(".gz"):
            import gzip
            text = gzip.decompress(data).decode("utf-8", errors="replace")
        elif path.endswith(".xz"):
            import lzma
            text = lzma.decompress(data).decode("utf-8", errors="replace")
        else:
            text = data.decode("utf-8", errors="replace")
        uri = self.get("uri", "")
        self.packages = self._parse_packages(text, uri=uri)
        self._raw_bytes = None
        return self

    class Metadata(Node):
        ## @brief Base class for parser-specific index payloads.
        ##
        ## Parsers are free to subclass this to provide a more structured
        ## schema for their index metadata while keeping the outer Index
        ## envelope generic.

        def __init__(self, **fields: Any) -> None:
            ## @brief Initialise index metadata from keyword fields.
            ##
            ## @param fields  Arbitrary keyword fields for the metadata envelope.
            ## @return None
            super().__init__(dict(fields))


class Indices(NodeList[Index]):
    ## @brief Container for Index descriptors.
    ##
    ## This is just a plain list of ``Index`` nodes.  Any schema or
    ## metadata lives either on the individual ``Index`` instances or
    ## on the parent repo, not on this list type itself.
    pass
