#/usr/bin/env python3
## @file inventory.py
##
## @brief Master pool inventory with per-session repo tracking.
##
## ``FileRecord`` is the canonical descriptor for a single piece of
## content: pool inode, size, real hash, and which repos have the file
## hardlinked this session.  It also owns the per-content download lock
## that serialises concurrent staging promotions.
##
## ``Inventory`` wraps a ``Dict[str, FileRecord]`` (keyed by SHA-256
## content hash, or by SHA-256(uri) for hash-less files before their
## real hash is known) and a reverse inode→key map.  A single shared
## instance built from the pool at startup is the authoritative
## source of truth for content state during a sync run.
##
## Per-repo inventories are thin wrappers that carry only ``stale_paths``
## (the pre-sync disk snapshot used for stale-file detection).  All
## hash-based lookups go through the pool inventory's FileRecords.
##
## @copyright Copyright (c) 2026 Tim Hosking
## @see https://github.com/munger
## @par Licence: MIT


import os
import threading
from pathlib import Path
from typing import Dict, Optional

from .lib.exceptions import ExceptionMsg
from .lib.find import find_stream


class FileRecord:
    ## @brief Descriptor for one piece of content in the pool inventory.
    ##
    ## Shared across all repos within a sync session.  The primary lookup
    ## key is the SHA-256 content hash; for files whose hash is not known
    ## in advance (e.g. Release files) a URI-derived temporary key is used
    ## (``temp=True``) and the real hash is set after the first download.
    ##
    ## Two locks serve distinct purposes:
    ##
    ## ``lock``        - serialises concurrent download and pool promotion.
    ##                   Held for the duration of a download so that the
    ##                   next thread to acquire it can check ``in_pool``
    ##                   and skip its own download entirely.
    ##
    ## ``_stat_lock``  - guards brief concurrent updates to ``repo_mask``
    ##                   and ``ref_count`` (e.g. simultaneous Phase-1 hits
    ##                   on the same hash from different repo workers).

    def __init__(self, inode: int = 0, size: int = 0, temp: bool = False) -> None:
        ## @brief Initialise a FileRecord.
        ## @param inode  Pool inode; 0 until the file is promoted to the pool.
        ## @param size   File size in bytes; 0 until known.
        ## @param temp   True if keyed by SHA-256(uri), not by content hash.
        ## @return None
        self.inode: int = inode          ## pool inode; 0 until promoted
        self.size: int = size            ## file size in bytes; 0 until known
        self.hash: Optional[str] = None  ## real content hash; None for temp records until promoted
        self.temp: bool = temp           ## True when keyed by SHA-256(uri), not content hash
        self.repo_mask: int = 0          ## bitmask: bit N set when repo N has this file
        self.ref_count: int = 0          ## managed hardlinks confirmed or created this session
        self.lock = threading.Lock()     ## download + promotion serialisation
        self._stat_lock = threading.Lock()  ## repo_mask / ref_count mutation guard

    @property
    def in_pool(self) -> bool:
        ## @brief True once the file has been promoted to the content-addressed pool.
        ## @return bool
        return self.inode > 0

    def set_repo_bit(self, repo_id: int) -> None:
        ## @brief Mark repo *repo_id* as having this file at session start.
        ##
        ## Called during the pre-sync inventory scan; does **not** increment
        ## ``ref_count`` since that counter tracks only links confirmed or
        ## established during the current sync run.
        ##
        ## @param repo_id  Session-scoped integer repo ID.
        ## @return None
        bit = 1 << repo_id
        with self._stat_lock:
            self.repo_mask |= bit

    def mark_linked(self, repo_id: int) -> None:
        ## @brief Record that repo *repo_id* confirmed or created a hardlink
        ##        to this file during the current sync run.
        ##
        ## Sets the repo's bit in ``repo_mask`` (idempotent) and increments
        ## ``ref_count`` so that ``query_deduped`` can identify shared files.
        ##
        ## @param repo_id  Session-scoped integer repo ID (must be >= 0).
        ## @return None
        bit = 1 << repo_id
        with self._stat_lock:
            self.repo_mask |= bit
            self.ref_count += 1


class Inventory:
    ## @brief Master pool inventory: ``{key -> FileRecord}`` with a reverse
    ##        inode-to-key index.
    ##
    ## The pool inventory is built once from the content-addressed pool at
    ## startup (``from_pool``), then updated in place as files are promoted
    ## during sync.  Per-repo inventories share the same class but carry
    ## only ``stale_paths`` — their ``_dict`` is always empty.
    ##
    ## ``_lock`` protects structural changes to ``_dict`` and ``_rev``
    ## (insertions and key registration).  FileRecord-level mutations use
    ## the record's own locks, not the inventory lock.

    def __init__(self, id: str = "", path: str = "") -> None:
        ## @brief Initialise an empty inventory.
        ## @param id    Unique identifier (e.g. ``"_pool_"`` or a dest name).
        ## @param path  Filesystem path this inventory was built from.
        ## @return None
        self._dict: Dict[str, FileRecord] = {}   ## key -> FileRecord
        self._rev: Dict[int, str] = {}           ## inode -> lookup key
        self._lock = threading.Lock()
        self.id: str = id
        self.path: str = path

    # -- readers --------------------------------------------------------------

    def has(self, key: str) -> bool:
        ## @brief True if *key* maps to a pool-resident FileRecord (``inode > 0``).
        ##
        ## Lock-free: individual dict.get() is GIL-atomic.  Returns False for
        ## records with ``inode == 0`` (download in progress).
        ##
        ## @param key  SHA-256 content hash or temp URI-hash.
        ## @return bool
        record = self._dict.get(key)
        return record is not None and bool(record.inode)

    def get_hash(self, inode: int) -> str | None:
        ## @brief Return the lookup key for *inode*, or ``None``.
        ## @param inode  Filesystem inode number.
        ## @return The lookup key string, or ``None``.
        return self._rev.get(inode)

    def get_record(self, key: str) -> Optional[FileRecord]:
        ## @brief Return the ``FileRecord`` for *key*, or ``None``.
        ##
        ## Lock-free: dict.get() is GIL-atomic.  The returned record may be
        ## mutated by other threads; callers that need a consistent snapshot
        ## must acquire the record's own locks.
        ##
        ## @param key  SHA-256 content hash or temp URI-hash.
        ## @return ``FileRecord`` or ``None``.
        return self._dict.get(key)

    def get(self, key: str, is_temp: bool = False, timeout: float = 5.0) -> FileRecord:
        ## @brief Return a locked ``FileRecord`` for *key*, creating it if absent.
        ##
        ## Atomically gets or creates the record under the inventory lock, then
        ## acquires the record lock before releasing the inventory lock.  If the
        ## record lock is contended (another thread is mid-download), the
        ## inventory lock is released first and this call blocks on the record
        ## lock — so the inventory is never held for more than microseconds.
        ##
        ## The caller **must** release ``record.lock`` when done.
        ##
        ## @param key      SHA-256 content hash or URI-derived temp hash.
        ## @param is_temp  True when *key* is URI-derived (hash-less files).
        ## @param timeout  Seconds to wait for a contended record lock.
        ## @return Locked ``FileRecord``; ``inode`` is 0 if not yet promoted.
        ## @raises StagingLockTimeout  If the record lock is not acquired in time.
        from .lib.exceptions import StagingLockTimeout
        with self._lock:
            record = self._dict.get(key)
            if record is None:
                record = FileRecord(temp=is_temp)
                self._dict[key] = record
            if record.lock.acquire(blocking=False):
                return record
        # Record lock is held by a downloading thread; block outside the
        # inventory lock so other inventory ops are not stalled.
        if not record.lock.acquire(timeout=timeout):
            raise StagingLockTimeout()
        return record

    def keys(self) -> set:
        ## @brief Return a snapshot of all keys currently in the inventory.
        ## @return A ``set`` of key strings.
        with self._lock:
            return set(self._dict.keys())

    def __len__(self) -> int:
        ## @brief Return the number of entries in the inventory.
        ## @return The number of key→FileRecord mappings.
        with self._lock:
            return len(self._dict)

    # -- writers --------------------------------------------------------------

    def add(self, key: str, inode: int, size: int = 0) -> None:
        ## @brief Insert or update a key→FileRecord mapping.
        ##
        ## If no record exists for *key*, creates one.  If a record already
        ## exists (from an in-progress download or an earlier pool scan),
        ## updates ``inode`` and ``size`` in place without replacing the
        ## object (preserving the existing lock and repo_mask).
        ##
        ## @param key    SHA-256 content hash (or temp URI-hash).
        ## @param inode  Pool inode number.
        ## @param size   File size in bytes (0 to leave unchanged).
        ## @return None
        with self._lock:
            record = self._dict.get(key)
            if record is None:
                record = FileRecord(inode=inode, size=size)
                record.hash = key
                self._dict[key] = record
            else:
                if inode:
                    record.inode = inode
                if size:
                    record.size = size
            if inode:
                self._rev[inode] = key

    def add_bulk(self, entries: Dict[str, int]) -> None:
        ## @brief Insert or update multiple key→inode mappings atomically.
        ## @param entries  Dict of ``{key: inode}`` to add.
        ## @return None
        with self._lock:
            for key, ino in entries.items():
                record = self._dict.get(key)
                if record is None:
                    record = FileRecord(inode=ino)
                    self._dict[key] = record
                else:
                    if ino:
                        record.inode = ino
                if ino:
                    self._rev[ino] = key

    def register(self, real_hash: str, record: FileRecord) -> None:
        ## @brief Register *real_hash* as a second lookup key for *record*.
        ##
        ## Called after a temp-keyed (URI-hash) record is promoted to the
        ## pool: the real content hash is inserted into the inventory so
        ## future hash-based lookups find the same ``FileRecord`` as the
        ## temp key.  The reverse inode→key index is updated to point at
        ## the real hash so ``get_hash()`` returns the canonical value.
        ##
        ## @param real_hash  64-char SHA-256 content hash.
        ## @param record     The ``FileRecord`` that was just promoted.
        ## @return None
        with self._lock:
            if real_hash not in self._dict:
                self._dict[real_hash] = record
            if record.inode:
                self._rev[record.inode] = real_hash

    def count_repo_files(self, repo_id: int) -> tuple[int, int]:
        ## @brief Count unique files belonging to *repo_id* in the pool.
        ##
        ## Iterates all pool-resident FileRecords whose repo bit is set,
        ## deduplicating by inode so temp+real-hash aliases are counted once.
        ##
        ## @param repo_id  Session-scoped integer repo ID.
        ## @return ``(file_count, total_bytes)`` of files in this repo.
        bit = 1 << repo_id
        files = 0
        total = 0
        seen_inodes: set[int] = set()
        with self._lock:
            for record in self._dict.values():
                if not record.in_pool:
                    continue
                if not (record.repo_mask & bit) or record.ref_count == 0:
                    continue
                if record.inode in seen_inodes:
                    continue
                seen_inodes.add(record.inode)
                files += 1
                total += record.size
        return files, total

    def query_deduped(self, repo_id: int) -> tuple[int, int]:
        ## @brief Count files in *repo_id* that are shared with at least one
        ##        other repo this session.
        ##
        ## Iterates all FileRecords, using the pool inode to deduplicate
        ## entries (a temp record and its real-hash alias share the same
        ## inode and must be counted once).
        ##
        ## @param repo_id  Session-scoped integer repo ID.
        ## @return ``(file_count, total_bytes)`` of shared files.
        bit = 1 << repo_id
        files = 0
        total = 0
        seen_inodes: set[int] = set()
        with self._lock:
            for record in self._dict.values():
                if not record.in_pool:
                    continue
                if record.inode in seen_inodes:
                    continue
                seen_inodes.add(record.inode)
                if (record.repo_mask & bit) and record.ref_count > 1:
                    files += 1
                    total += record.size
        return files, total

    # -- factories ------------------------------------------------------------

    def load_repo_paths(self, path_file: str, repo_id: int = -1) -> set:
        ## @brief Read a pre-sync repo path file, set repo bits, return stale paths.
        ##
        ## Called on the pool inventory instance.  Reads null-delimited
        ## ``rel_path\0inode\0`` pairs written by ``build-repo-paths.sh``,
        ## cross-references each inode against the pool to find its FileRecord,
        ## and sets the repo's bit when *repo_id* is non-negative.  The
        ## full set of relative paths is returned as the caller's stale-path
        ## tracking set — entirely separate from the pool inventory itself.
        ##
        ## The path file is unlinked immediately after opening (the kernel
        ## keeps the inode alive via the open fd until close).
        ##
        ## @param path_file  Path to the pre-sync paths file.
        ## @param repo_id    Session-scoped integer repo ID; -1 to skip bit-setting.
        ## @return ``set[str]`` of all repo-relative paths found on disk.

        stale: set[str] = set()

        try:
            fd = os.open(path_file, os.O_RDONLY)
        except OSError:
            return stale

        try:
            os.unlink(path_file)
        except OSError:
            pass

        try:
            with os.fdopen(fd, "rb") as f:
                data = f.read()
        except OSError:
            return stale

        parts = data.split(b"\0")
        for i in range(0, len(parts) - 1, 2):
            rel = parts[i].decode()
            ino_str = parts[i + 1].decode().strip()
            if not rel or not ino_str:
                continue
            ino = int(ino_str)

            stale.add(rel)

            if repo_id >= 0:
                key = self.get_hash(ino)
                if key is not None:
                    record = self.get_record(key)
                    if record is not None:
                        record.set_repo_bit(repo_id)

        return stale

    @staticmethod
    def from_pool(pool_root: str) -> "Inventory":
        ## @brief Build the master pool inventory by scanning the pool on disk.
        ##
        ## Streams ``find`` output using ``%f\0%i\0%s\0`` so filename (hash),
        ## inode, and size are read in one pass with no extra ``stat()`` calls.
        ##
        ## @param pool_root  Root directory of the content-addressed pool.
        ## @return A populated ``Inventory`` (empty if the pool does not
        ##         exist or ``find`` fails).
        pool_dir = Path(pool_root) / "by-hash" / "SHA256"
        if not pool_dir.is_dir():
            return Inventory(id="_pool_", path=pool_root)

        inv = Inventory(id="_pool_", path=pool_root)
        try:
            it = find_stream(str(pool_dir), r"%f\0%i\0%s\0")
            for h, ino_str, sz_str in zip(it, it, it):
                ino = int(ino_str)
                record = FileRecord(inode=ino, size=int(sz_str))
                record.hash = h
                inv._dict[h] = record
                inv._rev[ino] = h
        except (ExceptionMsg, OSError):
            pass

        return inv

