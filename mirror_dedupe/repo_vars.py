#!/usr/bin/env python3
## @file repo_vars.py
##
## @brief Per-repo variables shared across all nodes in a repo tree.
##
## ``RepoVars`` holds everything that every child node needs access to:
## the per-repo inventory, the global pool inventory, network settings,
## root paths, and sync mode.  A single ``RepoVars`` instance is created
## per repo and shared via ``Node._repo_vars`` through the entire tree.
##
## No node should ever call ``Config.load()`` — all runtime state lives
## here, propagated automatically during tree construction.
##
## @copyright Copyright (c) 2026 Tim Hosking
## @see https://github.com/munger
## @par Licence: MIT

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any, Optional

from .inventory import Inventory


class SyncStats:
    ## @brief Thread-safe accumulator for per-repo sync statistics.
    ##
    ## A single instance is created per repo at the start of each sync run
    ## and stored on ``RepoVars``.  ``Node.sync()`` calls ``record()`` at
    ## every outcome point — pool hit, re-link, or genuine download.  The
    ## coordinator sets ``elapsed`` and ``removed`` after the work queue
    ## drains.
    ##
    ## All counter mutations are serialised under a single ``threading.Lock``
    ## because ``Node.sync()`` may be called from worker threads.
    ## ``set.add`` on ``_seen_hashes`` is likewise protected so deduplication
    ## accounting stays correct under concurrency.

    def __init__(self) -> None:
        ## @brief Initialise all counters to zero.
        ## @return None
        self.file_count: int = 0
        self.total_bytes: int = 0
        self.deduped_bytes: int = 0
        self.bytes_transferred: int = 0
        self.pool_hits: int = 0
        self.pool_misses: int = 0
        self.errors: int = 0
        self.elapsed: float = 0.0
        self.removed: int = 0
        self._lock = threading.Lock()
        self._seen_hashes: set[str] = set()

    def record(
        self,
        *,
        hit: int = 0,
        miss: int = 0,
        bytes_tx: int = 0,
        size: int = 0,
        hash_val: str = "",
    ) -> None:
        ## @brief Record one sync outcome under lock.
        ##
        ## Called from ``Node.sync()`` — possibly from a worker thread —
        ## so all mutations are protected.  ``hash_val`` is used to count
        ## unique content for deduplication accounting: a hash seen for the
        ## first time contributes its ``size`` to ``deduped_bytes``.
        ##
        ## @param hit       1 if served from pool inventory, else 0.
        ## @param miss      1 if downloaded from upstream, else 0.
        ## @param bytes_tx  Bytes transferred (0 for pool hits).
        ## @param size      File size in bytes.
        ## @param hash_val  SHA-256 hex digest for deduplication accounting.
        ## @return None
        with self._lock:
            self.pool_hits += hit
            self.pool_misses += miss
            self.bytes_transferred += bytes_tx
            self.file_count += 1
            self.total_bytes += size
            if hash_val and hash_val not in self._seen_hashes:
                self._seen_hashes.add(hash_val)
                self.deduped_bytes += size

    def add_error(self) -> None:
        ## @brief Increment the error counter under lock.
        ## @return None
        with self._lock:
            self.errors += 1

    def to_dict(self) -> dict[str, Any]:
        ## @brief Return a plain-dict snapshot of the current totals.
        ## @return Stats dict consumed by ``Repo.stats()``, ``_write_ndjson()``,
        ##         and ``_print_summary()``.
        return {
            "file_count": self.file_count,
            "total_bytes": self.total_bytes,
            "deduped_bytes": self.deduped_bytes,
            "bytes_transferred": self.bytes_transferred,
            "pool_hits": self.pool_hits,
            "pool_misses": self.pool_misses,
            "errors": self.errors,
            "elapsed": self.elapsed,
            "removed": self.removed,
        }


@dataclass
class RepoVars:
    ## @brief Per-repo variables passed to every node in the schema tree.
    ##
    ## ``inv``      — the per-repo ``Inventory`` (hash→inode for files
    ##                hardlinked into this repo's dest directory).
    ## ``pool_inv`` — the global pool ``Inventory`` (hash→inode for every
    ##                file in the content-addressed pool).
    ## ``repo_root`` — root of the repository tree on disk.
    ## ``pool_root`` — root of the content-addressed pool on disk.
    ## ``sync_mode`` — ``True`` during a sync run (set on the specific
    ##                 repo before sync begins, cleared after).
    ## ``stats``    — accumulates per-node sync outcomes during a sync run;
    ##                ``None`` outside of sync or when pool is ``None``.

    inv: Optional[Inventory] = None
    pool_inv: Optional[Inventory] = None
    repo_root: str = ""
    pool_root: str = ""
    sync_mode: bool = False
    stats: Optional[SyncStats] = None
