## @file release.py
##
## @brief Release file descriptor, digest helper, and collection.
##
## A ``Release`` envelopes a single Release-derived file and its
## metadata.  ``Release.Digest`` provides cheap comparison via SHA-256
## hash and byte length.  ``Releases`` is the corresponding ``NodeList``
## wrapper.
##
## @copyright Copyright (c) 2026 Tim Hosking
## @see https://github.com/munger
## @par Licence: MIT

from __future__ import annotations

from typing import Any, Dict, List
import hashlib

from .node import Node, NodeList


class Release(Node):
    ## @brief Dict-backed descriptor for a single Release-derived file.
    ##
    ## Envelopes both the human-facing suite/pocket information and
    ## various pieces of data that layout-specific parsers consider
    ## important to REST clients.
    ##
    ## Required fields:
    ##
    ## * ``suite``       — logical suite name
    ## * ``pocket``      — logical pocket name or ``None``
    ## * ``relative_dir``
    ## * ``path``
    ## * ``url``
    ## * ``repo_type``
    ## * ``kind``
    ##
    ## Optional fields:
    ##
    ## * ``signature_extension``

    _restore_via_payload = True

    def __init__(
        self,
        *,
        suite: str,
        pocket: str | None,
        relative_dir: str,
        path: str,
        repo_type: str,
        kind: str,
        signature_extension: str | None = None,
        digest: "Release.Digest | None" = None,
    ) -> None:
        data: Dict[str, Any] = {
            "suite": suite,
            "pocket": pocket,
            "relative_dir": relative_dir,
            "path": path,
            "repo_type": repo_type,
            "kind": kind,
        }
        if signature_extension is not None:
            data["signature_extension"] = signature_extension
        if digest is not None:
            data["digest"] = digest
        super().__init__(data)

    class Digest(Node):
        ## @brief Digest describing this Release's body.
        ##
        ## Callers provide the raw Release text so we can compute cheap
        ## comparison information (SHA-256 hash and byte length), but
        ## only those summary fields are persisted in the mapping to keep
        ## scan outputs lean.

        def __init__(self, *, text: str) -> None:
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
            data: Dict[str, Any] = {
                "sha256": digest,
                "length": len(text),
            }
            super().__init__(data)


class Releases(NodeList[Release]):
    ## @brief Container for Release descriptors.
    ##
    ## This is just a plain list of ``Release`` nodes.  Any schema or
    ## metadata lives either on the individual ``Release`` instances or
    ## on the parent repo, not on this list type itself.
    pass
