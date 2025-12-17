from __future__ import annotations

from typing import Any, Dict

from .node import Node


class Vars(Node):
    """Per-repo variables populated by parsers.

    Vars holds invariants and derived facts that apply to the entire
    repository (e.g. APT layout conventions), not per-release data.
    """

    # On restore we want to seed the underlying mapping directly from the
    # snapshot payload, bypassing the keyword-only constructor.
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
