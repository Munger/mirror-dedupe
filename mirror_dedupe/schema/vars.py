## @file vars.py
##
## @brief Per-repo variables populated by parsers.
##
## ``Vars`` holds invariants and derived facts that apply to the entire
## repository (e.g. APT layout conventions), not per-release data.
##
## @copyright Copyright (c) 2026 Tim Hosking
## @see https://github.com/munger
## @par Licence: MIT

from __future__ import annotations

from typing import Any, Dict

from .node import Node


class Vars(Node):
    ## @brief Per-repo variables populated by parsers.
    ##
    ## Holds invariants and derived facts that apply to the entire
    ## repository (e.g. APT layout conventions), not per-release data.

    _restore_via_payload = True

    def __init__(
        self,
        *,
        index_root: str = "",
        anchor_filename: str = "",
        signature_extension: str = "",
    ) -> None:
        data: Dict[str, Any] = {
            "index_root": index_root,
            "anchor_filename": anchor_filename,
            "signature_extension": signature_extension,
        }

        super().__init__(data)

    @property
    def index_root(self) -> str:
        return self.get("index_root", "")

    @property
    def anchor_filename(self) -> str:
        return self.get("anchor_filename", "")

    @property
    def signature_extension(self) -> str:
        return self.get("signature_extension", "")
