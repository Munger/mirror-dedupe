## @file IndexFetcher.py
##
## @brief Fetch APT index files (Packages/Sources) using the shared pool.
##
## ``IndexFetcher`` downloads all indices referenced by a parsed Release
## file, routing each through ``PoolFile`` so identical content is stored
## once by hash in the content-addressable pool and hardlinked into the
## repo tree.
##
## Callers never touch the pool directly — ``PoolFile`` handles fetch,
## verify, and link internally.
##
## @copyright Copyright (c) 2026 Tim Hosking
## @see https://github.com/munger
## @par Licence: MIT

from __future__ import annotations

from pathlib import Path
from typing import List, Optional

from mirror_dedupe import schema as Schema
from mirror_dedupe.config import Config
from mirror_dedupe.lib.html_helpers import build_url
from mirror_dedupe.pool.poolfile import PoolFile


class IndexFetcher:
    ## @brief Fetch APT index files using PoolFile for content-addressed storage.

    def __init__(self) -> None:
        cfg = Config.load()
        self.repo_root = Path(cfg.repo_root)

    def fetch_all(self, release: Schema.Node, upstream_base: str) -> List[Path]:
        ## @brief Download all indices referenced by a Release, yielding repo paths.
        ##
        ## @param release        A Schema.Release (or compatible Node) with
        ##                       an ``indices`` attribute.
        ## @param upstream_base  Base upstream URL for building index URLs.
        ## @return List of downloaded repo paths.

        indices = release["indices"] if "indices" in release else None
        if not indices:
            return []

        downloaded_paths: List[Path] = []
        for index in indices:
            repo_path = self.fetch_index(upstream_base, index)
            if repo_path:
                downloaded_paths.append(repo_path)
        return downloaded_paths

    def fetch_index(self, upstream_base: str, index: Schema.Index) -> Optional[Path]:
        ## @brief Download a single index file into the pool and hardlink to the repo.
        ##
        ## Validates that the hash algorithm is ``sha256`` (the only
        ## supported algorithm for pool addressing).  Delegates fetch,
        ## verify, and link to ``PoolFile``.
        ##
        ## @param upstream_base  Base upstream URL.
        ## @param index          Schema.Index with metadata containing
        ##                       ``checksum``, ``algorithm``, and ``size``.
        ## @return The local repo path, or None if the index has no path.
        ## @raise ValueError  If the hash algorithm is unsupported or the
        ##                    checksum is missing.

        path = index["path"] if "path" in index else None
        metadata = index["metadata"] if "metadata" in index else {}
        checksum = metadata.get("checksum")
        algorithm = metadata.get("algorithm", "sha256")

        if not path:
            return None

        if algorithm != "sha256":
            raise ValueError(f"Unsupported index hash algorithm: {algorithm} for {path}")
        if not checksum:
            raise ValueError(f"Missing checksum for index: {path}")

        url = build_url(upstream_base, path)
        repo_path = self.repo_root / path

        pf = PoolFile(uri=url, hash=checksum, size=metadata.get("size"))
        pf.store(str(repo_path))
        return repo_path
