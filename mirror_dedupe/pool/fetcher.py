## @file fetcher.py
##
## @brief Shared pool fetcher: download once by hash into the pool, then
##        link into repos.
##
## Transport-agnostic: callers supply a ``download_fn(hash, size, url)``
## that returns bytes for the requested object.
##
## Usage pattern (HTTP immediate mode)::
##
##     fetcher = Fetcher()
##     fetcher.fetch_and_link(items, download_fn=download_http)
##
## Where *items* is an iterable of dicts with keys ``hash``, ``size``
## (optional), ``url``, and ``repo_path``.
##
## @copyright Copyright (c) 2026 Tim Hosking
## @see https://github.com/munger
## @par Licence: MIT

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Callable, Iterable, Mapping, Optional

from mirror_dedupe.config import Config
from .inventory import Inventory


class Fetcher:
    ## @brief Download content by hash into the content-addressable pool
    ##        and hardlink into repo paths.
    ##
    ## Uses the inventory as the authoritative view; skips files whose
    ## hash is already recorded, falling back to filesystem existence
    ## checks for inventory misses.

    def __init__(self) -> None:
        ## @brief Initialise the Fetcher from the global Config.
        ##
        ## Resolves ``pool_root``, creates the SHA-256 by-hash
        ## directory structure, and acquires a singleton Inventory.

        cfg = Config.load()
        self.pool_root = Path(cfg.pool_root)
        self.hash_root = self.pool_root / "by-hash" / "SHA256"
        self.hash_root.mkdir(parents=True, exist_ok=True)
        self.inventory = Inventory.get()

    def fetch_and_link(
        self,
        items: Iterable[Mapping[str, str]],
        download_fn: Callable[[str, Optional[int], str], bytes],
    ) -> None:
        ## @brief Download missing hashes into the pool and link into repo paths.
        ##
        ## Iterates *items*; for each unique hash, checks the inventory
        ## (and then the filesystem) to see if it is already present.
        ## Only downloads what is missing, then hardlinks each repo path
        ## to the pool file.
        ##
        ## @param items       Iterable of dicts with keys ``hash``,
        ##                    ``repo_path``, ``url``, and optionally
        ##                    ``size``.
        ## @param download_fn  Callable ``(hash, size, url) -> bytes`` used
        ##                     to fetch content for missing hashes.

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

            size = item.get("size") if isinstance(item, dict) else None
            url = item.get("url") if isinstance(item, dict) else ""
            blob = download_fn(h, size, url)
            self._write_pool(h, blob)
            self._link_repo(item)
            seen_hashes.add(h)

    def _pool_has(self, h: str) -> bool:
        ## @brief Check whether *h* already exists in the pool.
        ##
        ## Prefers the Inventory as the authoritative view to avoid
        ## extra filesystem calls.  Falls back to ``Path.exists()``
        ## and records the result in the inventory on a hit.

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
        ## @brief Atomically write *content* to the pool under hash *h*.
        ##
        ## Verifies the SHA-256 digest matches *h* before writing,
        ## uses an atomic rename via ``os.replace``, and records the
        ## result in the inventory.

        dest = self._hash_path(h)
        dest.parent.mkdir(parents=True, exist_ok=True)
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
        ## @brief Hardlink the pool hash file into the repo path.
        ## @param item  Dict with keys ``hash`` and ``repo_path``.

        h = item["hash"]
        repo_path = item["repo_path"]
        if self.inventory is None:
            raise RuntimeError("Inventory not initialized")
        self.inventory.link_repo_path(h, repo_path)
