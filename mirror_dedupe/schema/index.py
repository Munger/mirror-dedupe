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

from typing import Any, Dict, List, Optional

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

    _children = ["packages"]
    ## @brief Declare ``packages`` as child nodes so that ``_tree_iter()``
    ##        (used by ``_sync_content()``) walks into Package children.

    _restore_via_payload = True

    def __init__(
        self,
        *,
        path: str,
        kind: str,
        metadata: "Index.Metadata | None" = None,
        uri: str = "",
        size: int = 0,
    ) -> None:
        ## @brief Initialise an Index descriptor.
        ##
        ## @param path      Relative path under the repo root.
        ## @param kind      Logical kind (e.g. ``"packages"``).
        ## @param metadata  Optional index metadata.
        ## @param uri       Index URI for fetching.
        ## @param size      File size in bytes (0 if unknown).
        ## @return None
        data: Dict[str, Any] = {
            "path": path,
            "kind": kind,
        }
        if uri:
            data["uri"] = uri
        if metadata is not None:
            data["metadata"] = metadata
        if size:
            data["size"] = size
        super().__init__(data)

    @property
    def checksum(self) -> str:
        ## @brief SHA-256 checksum from attached metadata (if any).
        ## @return The checksum string, or ``""`` if absent.
        md = self.get("metadata")
        if md:
            return md.get("checksum", "")
        return ""

    def stream(self, data: Optional[bytes] = None):
        ## @brief Yield Package children from this Index's content.
        ##
        ## The base implementation returns an empty iterator (no child
        ## Package nodes).  Subclasses that parse Package stanzas
        ## override this method.
        ##
        ## @param data  Optional bytes to parse (scan mode).
        ## @yield Package child nodes.

        self.packages = Packages()
        return iter([])

    def parse(self, **kwargs: Any) -> "Index":  # type: ignore[override]
        ## @brief Fetch index content, stream-parse into child Package nodes.
        ##
        ## Only processes indices whose ``kind`` is ``"packages"`` or
        ## ``"sources"`` — Contents, Translation, and other metadata files
        ## are skipped (they are already enumeratively downloaded by the
        ## Release's hash-section entries and do not need stanza parsing).
        ##
        ## In scan mode, ``fetch()`` performs an in-memory HTTP download
        ## and passes the bytes to ``stream(data=data)``.
        ## In sync mode, ``fetch()`` synchronises through the pool via
        ## ``self.read()`` and then reads from disk.
        ##
        ## @param config  Optional configuration dict passed to ``fetch()``.
        ## @return This Index (with ``packages`` populated).
        config: Optional[Dict[str, Any]] = kwargs.get("config")
        kind = self.get("kind")
        if kind not in ("packages", "sources"):
            return self
        try:
            data = self.fetch(uri=self.get("uri"), config=config)
        except RuntimeError:
            from ..lib.log import log
            log(f"  FAILED index {self.get('uri', 'unknown')}")
            return self
        if not data:
            return self
        list(self.stream(data=data))
        self._cache = None
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


class VariantIndex(Index):
    ## @brief Non-primary compression variant (e.g. ``.bz2``, secondary ``.gz``).
    ##
    ## A ``VariantIndex`` represents an alternative compression of a
    ## primary index file (e.g. ``Packages.bz2`` when ``Packages.xz`` is
    ## the primary).  It has ``_children = []`` so ``_tree_iter()`` does
    ## not descend into Package children — only the primary variant holds
    ## them.
    ##
    ## ``stream()`` is a no-op — variant indices are downloaded but not
    ## parsed for child packages.

    _children: list[str] = []

    def stream(self, data: Optional[bytes] = None):
        ## @brief No-op stream for non-primary variants.
        ## @param data  Ignored.
        ## @return Empty iterator.

        self.packages = Packages()
        return iter([])


class Indices(NodeList[Index]):
    ## @brief Container for Index descriptors.
    ##
    ## This is just a plain list of ``Index`` nodes.  Any schema or
    ## metadata lives either on the individual ``Index`` instances or
    ## on the parent repo, not on this list type itself.
    pass
