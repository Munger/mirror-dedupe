from __future__ import annotations

"""
Shared pool fetcher: download once by hash into the pool, then link into repos.

This is transport-agnostic; callers supply a `download_fn(hash, size, url)`
that returns bytes (or a file-like) for the requested object.

Usage pattern (HTTP immediate mode):
    fetcher = Fetcher()
    fetcher.fetch_and_link(items, download_fn=download_http)

Where items is an iterable of dicts:
    {
        "hash": "<sha256>",
        "size": <int> | None,
        "url": "<http url>",
        "repo_path": "<absolute path to repo file>",
    }
"""

import hashlib
import os
from pathlib import Path
from typing import Callable, Iterable, Mapping, Optional

from mirror_dedupe.config import Config
from .inventory import Inventory


class Fetcher:
    def __init__(self) -> None:
        cfg = Config.get()
        self.pool_root = Path(cfg.pool_root)
        self.hash_root = self.pool_root / "by-hash" / "SHA256"
        self.hash_root.mkdir(parents=True, exist_ok=True)
        # Use the singleton inventory (auto-resolves roots from Config).
        self.inventory = Inventory.get()

    def fetch_and_link(
        self,
        items: Iterable[Mapping[str, str]],
        download_fn: Callable[[str, Optional[int], str], bytes],
    ) -> None:
        """Download missing hashes into the pool and link into repo paths.

        items: iterable with keys hash, repo_path, url, size (optional)
        download_fn(hash, size, url) -> bytes
        """
        seen_hashes: set[str] = set()
        for item in items:
            h = item["hash"]
            if h in seen_hashes:
                self._link_repo(item)
                continue
            if self._pool_has(h):
                self._link_repo(item)
                seen_hashes.add(h)
                continue

            # Need to download
            size = item.get("size") if isinstance(item, dict) else None  # type: ignore
            url = item.get("url") if isinstance(item, dict) else ""  # type: ignore
            blob = download_fn(h, size, url)  # bytes
            self._write_pool(h, blob)
            self._link_repo(item)
            seen_hashes.add(h)

    def _pool_has(self, h: str) -> bool:
        # Prefer Inventory as the authoritative view to avoid extra filesystem calls.
        if self.inventory is not None and h in self.inventory.pool_files:
            return True
        dest = self._hash_path(h)
        exists = dest.exists()
        if exists and self.inventory is not None:
            st = dest.stat()
            self.inventory.record_pool_hash(h, st.st_ino, st.st_nlink)
        return exists

    def _hash_path(self, h: str) -> Path:
        return self.hash_root / h[:2] / h[2:4] / h

    def _write_pool(self, h: str, content: bytes) -> None:
        dest = self._hash_path(h)
        dest.parent.mkdir(parents=True, exist_ok=True)
        # verify hash
        digest = hashlib.sha256(content).hexdigest()
        if digest != h:
            raise ValueError(f"Hash mismatch for {h}: got {digest}")
        if dest.exists():
            if self.inventory is not None:
                st = dest.stat()
                self.inventory.record_pool_hash(h, st.st_ino, st.st_nlink)
            return
        tmp = dest.with_suffix(".tmp")
        with open(tmp, "wb") as f:
            f.write(content)
        os.replace(tmp, dest)
        if self.inventory is not None:
            st = dest.stat()
            self.inventory.record_pool_hash(h, st.st_ino, st.st_nlink)

    def _link_repo(self, item: Mapping[str, str]) -> None:
        h = item["hash"]
        repo_path = item["repo_path"]
        if self.inventory is None:
            raise RuntimeError("Inventory not initialized")
        self.inventory.link_repo_path(h, repo_path)
