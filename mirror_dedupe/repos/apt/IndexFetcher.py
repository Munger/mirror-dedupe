from __future__ import annotations

from pathlib import Path
from typing import Iterable, Optional

from mirror_dedupe import pool, schema as Schema
from mirror_dedupe.config import Config
from mirror_dedupe.lib.html_helpers import build_url


class IndexFetcher:
    """Fetch APT index files (Packages/Sources) using the shared pool Fetcher."""

    def __init__(self, http_client) -> None:
        self.http = http_client
        cfg = Config.load()
        self.repo_root = Path(cfg.repo_root)
        # Build a pool Fetcher using configured roots (uses Inventory singleton internally).
        self.pool_fetcher = pool.Fetcher(pool_root=cfg.pool_root, repos_root=cfg.repo_root)

    def fetch_all(self, release: Schema.Node, upstream_base: str) -> Iterable[Path]:
        """Download all indices referenced by a Release, yielding repo paths."""
        indices = release["indices"] if "indices" in release else None
        if not indices:
            return []

        downloaded_paths: list[Path] = []
        for index in indices:
            repo_path = self.fetch_index(upstream_base, index)
            if repo_path:
                downloaded_paths.append(repo_path)
        return downloaded_paths

    def fetch_index(self, upstream_base: str, index: Schema.Index) -> Optional[Path]:
        """Download a single index file via the pool fetcher, return repo path."""
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

        def download_fn(expected_hash: str, size: Optional[int], _url: str) -> bytes:
            resp = self.http.get(_url)
            data = resp.content if hasattr(resp, "content") else resp
            if size is not None and len(data) != size:
                raise ValueError(f"Size mismatch for {_url}: expected {size}, got {len(data)}")
            return data

        self.pool_fetcher.fetch_and_link(
            [
                {
                    "hash": checksum,
                    "repo_path": str(repo_path),
                    "url": url,
                    "size": metadata.get("size"),
                }
            ],
            download_fn=download_fn,
        )
        return repo_path
