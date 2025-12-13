from __future__ import annotations

from typing import Any, Dict

from .node import Node


class Vars(Node):
    """Per-repo variables populated by parsers.

    Vars holds invariants and derived facts that apply to the entire
    repository (e.g. APT layout conventions), not per-release data.
    """

    def __init__(
        self,
        *,
        index_root: str = "",
        anchor_filename: str = "",
        signature_extension: str = "",
        repo_type: str | None = None,
    ) -> None:
        data: Dict[str, Any] = {
            "index_root": index_root,
            "anchor_filename": anchor_filename,
            "signature_extension": signature_extension,
        }
        if repo_type is not None:
            data["repo_type"] = repo_type

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

    @property
    def repo_type(self) -> str | None:
        return self.get("repo_type")
