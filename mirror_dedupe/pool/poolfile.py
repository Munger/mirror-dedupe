## @file poolfile.py
##
## @brief Self-contained file object with upstream URI and hash identity.
##
## ``PoolFile(uri, hash)`` encapsulates everything needed to fetch
## content from upstream, cache it in the content-addressable pool by
## hash, and hardlink it to a repo destination.  The pool path is an
## internal detail derived from Config; callers never touch it.
##
## Usage::
##
##     pf = PoolFile(uri="https://.../Packages.gz", hash="abc123...")
##     data = pf.fetch()         # bytes, cached in pool
##     pf.store("/srv/mirror/repos/ubuntu/dists/...")  # hardlink from pool
##
## @copyright Copyright (c) 2026 Tim Hosking
## @see https://github.com/munger
## @par Licence: MIT

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path
from typing import Optional

from mirror_dedupe.config import Config
from .inventory import Inventory


class PoolFile:
    ## @brief A file identified by upstream URI and content hash.

    def __init__(self, uri: str, hash: str, size: Optional[int] = None) -> None:
        ## @param uri   Upstream URL for fetching.
        ## @param hash  Expected SHA-256 hex digest.
        ## @param size  Expected size in bytes (optional, used for stat skip).
        self.uri = uri
        self.hash = hash
        self._size = size
        self._pool_root: Optional[Path] = None
        self._inventory: Optional[Inventory] = None

    # --- internal helpers -------------------------------------------------

    def _init_pool(self) -> None:
        if self._pool_root is not None:
            return
        cfg = Config.load()
        self._pool_root = Path(cfg.pool_root) / "by-hash" / "SHA256"
        self._inventory = Inventory.get()

    def _hash_path(self) -> Path:
        self._init_pool()
        h = self.hash
        return self._pool_root / h[:2] / h[2:4] / h

    def _pool_has(self) -> bool:
        self._init_pool()
        if self._inventory is not None and self.hash in self._inventory.pool_files:
            return True
        p = self._hash_path()
        if p.exists():
            st = p.stat()
            if self._inventory is not None:
                self._inventory.record_pool_hash(self.hash, st.st_ino, st.st_nlink)
            return True
        return False

    def _write_pool(self, content: bytes) -> None:
        self._init_pool()
        dest = self._hash_path()
        dest.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(content).hexdigest()
        if digest != self.hash:
            raise ValueError(
                f"Hash mismatch for {self.hash[:16]}... "
                f"from {self.uri}: got {digest[:16]}..."
            )
        if dest.exists():
            st = dest.stat()
            if self._inventory is not None:
                self._inventory.record_pool_hash(self.hash, st.st_ino, st.st_nlink)
            return
        tmp = dest.with_suffix(".tmp")
        tmp.write_bytes(content)
        os.replace(tmp, dest)
        st = dest.stat()
        if self._inventory is not None:
            self._inventory.record_pool_hash(self.hash, st.st_ino, st.st_nlink)

    def _download(self) -> bytes:
        proc = subprocess.run(
            ["curl", "-s", "-f", "-L", "--max-time", "300", self.uri],
            capture_output=True,
        )
        if proc.returncode != 0:
            msg = proc.stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(
                f"Failed to fetch {self.uri}: curl exit {proc.returncode} {msg}"
            )
        return proc.stdout

    # --- public API -------------------------------------------------------

    def exists(self) -> bool:
        ## @brief Return True if this file's hash is already in the pool.
        return self._pool_has()

    def fetch(self) -> bytes:
        ## @brief Return file content, ensuring it is cached in the pool.
        ##
        ## If the hash already exists in the pool, reads from there.
        ## Otherwise downloads from *uri*, verifies SHA-256, writes to
        ## the pool, and returns the bytes.
        if self._pool_has():
            return self._hash_path().read_bytes()
        data = self._download()
        self._write_pool(data)
        return data

    def store(self, dest_path: str) -> None:
        ## @brief Ensure the file exists in the pool and hardlink to *dest_path*.
        ##
        ## Creates parent directories under *dest_path* as needed.
        ## If the pool does not have this hash, ``store()`` fetches it
        ## first before linking.
        if not self._pool_has():
            self.fetch()
        self._init_pool()
        self._inventory.link_repo_path(self.hash, dest_path)
