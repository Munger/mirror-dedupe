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
## No node should ever call ``Config.load()`` - all runtime state lives
## here, propagated automatically during tree construction.
##
## @copyright Copyright (c) 2026 Tim Hosking
## @see https://github.com/munger
## @par Licence: MIT


import threading
from dataclasses import dataclass
from typing import Any, Optional

from .inventory import Inventory


class SyncStats:
    ## @brief Thread-safe accumulator for per-repo sync statistics.
    ##
    ## A single instance is created per repo at the start of each sync run
    ## and stored on ``RepoVars``.  ``Node.sync()`` calls ``record()`` at
    ## every outcome point - pool hit, re-link, or genuine download.  The
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
        self.gpg_failures: int = 0
        self.no_response: int = 0
        self.elapsed: float = 0.0
        self.removed: int = 0
        self._lock = threading.Lock()
        self._seen_hashes: set[str] = set()
        self._consecutive_no_response: int = 0

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
        ## Called from ``Node.sync()`` - possibly from a worker thread -
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

    def add_gpg_failure(self) -> None:
        ## @brief Increment the GPG failure counter under lock.
        ## @return None
        with self._lock:
            self.gpg_failures += 1

    def add_no_response(self) -> int:
        ## @brief Record a host-not-responding event.
        ## @return The updated consecutive count (used to decide abort threshold).
        with self._lock:
            self.no_response += 1
            self._consecutive_no_response += 1
            return self._consecutive_no_response

    def reset_consecutive_no_response(self) -> None:
        ## @brief Reset the consecutive counter after any successful sync.
        with self._lock:
            self._consecutive_no_response = 0

    def to_dict(self) -> dict[str, Any]:
        ## @brief Return a plain-dict snapshot of the current totals.
        ## @return Stats dict consumed by ``Repo.stats()``, ``stats.write_ndjson()``,
        ##         and ``_print_summary()``.
        return {
            "file_count": self.file_count,
            "total_bytes": self.total_bytes,
            "deduped_bytes": self.deduped_bytes,
            "bytes_transferred": self.bytes_transferred,
            "pool_hits": self.pool_hits,
            "pool_misses": self.pool_misses,
            "errors": self.errors,
            "gpg_failures": self.gpg_failures,
            "no_response": self.no_response,
            "elapsed": self.elapsed,
            "removed": self.removed,
        }


@dataclass
class RepoVars:
    ## @brief Per-repo variables passed to every node in the schema tree.
    ##
    ## ``inv``        - the per-repo ``Inventory`` (hash->inode for files
    ##                  hardlinked into this repo's dest directory).
    ## ``pool_inv``   - the global pool ``Inventory`` (hash->inode for every
    ##                  file in the content-addressed pool).
    ## ``mirror_root`` - root of all mirror data on disk.
    ## ``repo_root``   - root of the repository tree (``<mirror_root>/repos``).
    ## ``pool_root``   - root of the content-addressed pool (``<mirror_root>/pool``).
    ## ``repo_name``  - name of this repo; used as the subprocess group key
    ##                  for per-repo abort (matches the thread-local tag set
    ##                  in ``Repos._sync_one()``).
    ## ``abort_event`` - set to signal all coordinator/worker threads for
    ##                   this repo to stop immediately.  Checked at the top
    ##                   of each ``_sync_content()`` iteration.
    ## ``sync_mode``  - ``True`` during a sync run (set on the specific
    ##                  repo before sync begins, cleared after).
    ## ``stats``      - accumulates per-node sync outcomes during a sync run;
    ##                  ``None`` outside of sync or when pool is ``None``.

    inv: Optional[Inventory] = None
    pool_inv: Optional[Inventory] = None
    mirror_root: str = ""
    repo_root: str = ""
    pool_root: str = ""
    repo_name: str = ""
    gpg_keyring_path: str = ""
    connect_timeout: int = 10
    abort_event: threading.Event = None  # type: ignore[assignment]
    sync_mode: bool = False
    stats: Optional[SyncStats] = None

    def __post_init__(self) -> None:
        ## @brief Initialise fields that cannot use dataclass defaults.
        ##
        ## ``threading.Event`` cannot be a dataclass field default because
        ## it is mutable - using ``field(default_factory=...)`` would work but
        ## requires importing ``dataclasses.field``.  A ``__post_init__``
        ## is cleaner and avoids the extra import at the call site.
        ##
        ## @return None
        if self.abort_event is None:
            self.abort_event = threading.Event()
