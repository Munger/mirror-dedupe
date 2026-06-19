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


from typing import Any, Dict

from .mdnode import MDNode as Node


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
        ## @brief Initialise per-repo variables.
        ##
        ## @param index_root           Root directory for index files.
        ## @param anchor_filename      Name of the anchor file used for discovery.
        ## @param signature_extension  File extension for signature files.
        ## @return None
        data: Dict[str, Any] = {
            "index_root": index_root,
            "anchor_filename": anchor_filename,
            "signature_extension": signature_extension,
        }

        super().__init__(data)

    @property
    def index_root(self) -> str:
        ## @brief The root directory for index files.
        ## @return The index root path string.
        return self.get("index_root", "")

    @property
    def anchor_filename(self) -> str:
        ## @brief The anchor filename used during repo discovery.
        ## @return The anchor filename string.
        return self.get("anchor_filename", "")

    @property
    def signature_extension(self) -> str:
        ## @brief The file extension for signature files.
        ## @return The signature extension string (e.g. ``".asc"``).
        return self.get("signature_extension", "")
