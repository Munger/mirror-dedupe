#/usr/bin/env python3
## @file inventory.py
##
## @brief Thread-safe in-memory hash-to-inode inventory for mirror-dedupe.
##
## ``Inventory`` wraps a pair of ``dict``s (hash->inode, inode->hash) with an
## internal ``threading.Lock`` so that every read and write is atomic.  The
## class has no concept of paths, pool layout, or repo structure - it is
## purely a bidirectional lookup table.
##
## Two logical inventories exist at runtime:
##
## 1. **Pool inventory** - every file in the content-addressed pool,
##    built once at startup via ``Inventory.from_pool(pool_root)``.
## 2. **Per-repo inventory** - a subset of the pool whose inodes are
##    hardlinked into a specific repo's directory tree, built by
##    ``Inventory.from_repos()`` via reverse lookup against the pool
##    inventory.
##
## The lock is **never exposed** to callers.  All thread safety is
## internal.
##
## @copyright Copyright (c) 2026 Tim Hosking
## @see https://github.com/munger
## @par Licence: MIT


import os
import threading
from pathlib import Path
from typing import Dict

from .lib.exceptions import ExceptionMsg
from .lib.find import find_binary, find_stream


class Inventory:
    ## @brief Thread-safe bidirectional ``{hash<->inode}`` lookup table.
    ##
    ## ``_dict`` maps SHA-256 hex (64-char string) -> inode (int).
    ## ``_rev`` maps inode (int) -> SHA-256 hex (64-char string).
    ##
    ## Both dicts are always updated under the same lock so they never
    ## drift apart.  Because the from-scratch factories are called before
    ## the object is shared, they can write directly to the private dicts
    ## without acquiring the lock.


    def __init__(self, id: str = "", path: str = "") -> None:
        ## @brief Initialise an empty inventory.
        ## @param id    Unique identifier (e.g. ``"_pool_"``).
        ## @param path  Filesystem path this inventory was built from.
        self._dict: Dict[str, int] = {}
        self._rev: Dict[int, str] = {}
        self._lock = threading.Lock()
        self.id: str = id
        self.path: str = path
        ## @brief Paths found on disk during the pre-sync ``from_repos`` scan.
        ##
        ## Populated once by ``from_repos`` with every file path (relative to
        ## ``repo_root``) present in this repo's destination directory before
        ## the sync begins.  During sync, each node removes its own path via
        ## ``Node.sync()`` as soon as it is declared wanted.  Whatever remains
        ## when ``_sweep_stale`` runs is a file that exists on disk but was
        ## never wanted - safe to delete.  Only meaningful on per-repo
        ## inventories; the pool inventory leaves this empty.
        self.stale_paths: set[str] = set()

    # -- readers ----------------------------------------------------------

    def has(self, hash: str) -> bool:
        ## @brief Check whether *hash* is present in the inventory.
        ## @param hash  SHA-256 hex string to look up.
        ## @return ``True`` if the hash exists.
        with self._lock:
            return hash in self._dict

    def get(self, hash: str) -> int | None:
        ## @brief Return the inode for *hash*, or ``None``.
        ## @param hash  SHA-256 hex string to look up.
        ## @return The inode number, or ``None``.
        with self._lock:
            return self._dict.get(hash)

    def get_hash(self, inode: int) -> str | None:
        ## @brief Return the hash for *inode*, or ``None``.
        ## @param inode  Filesystem inode number.
        ## @return The SHA-256 hex string, or ``None``.
        with self._lock:
            return self._rev.get(inode)

    def keys(self) -> set:
        ## @brief Return a snapshot of all hashes currently in the inventory.
        ## @return A ``set`` of hash strings.
        with self._lock:
            return set(self._dict.keys())

    def __len__(self) -> int:
        ## @brief Return the number of entries in the inventory.
        ## @return The number of hash<->inode mappings.
        with self._lock:
            return len(self._dict)

    # -- writers ----------------------------------------------------------

    def add(self, hash: str, inode: int) -> None:
        ## @brief Insert or update a single hash<->inode mapping.
        ## @param hash   SHA-256 hex string.
        ## @param inode  Filesystem inode number.
        ## @return None
        with self._lock:
            self._dict[hash] = inode
            self._rev[inode] = hash

    def add_bulk(self, entries: Dict[str, int]) -> None:
        ## @brief Insert or update multiple mappings atomically.
        ## @param entries  Dict of ``{hash: inode}`` to add.
        ## @return None
        with self._lock:
            self._dict.update(entries)
            for h, ino in entries.items():
                self._rev[ino] = h

    # -- factories --------------------------------------------------------

    @staticmethod
    def from_path_file(
        path_file: str,
        pool_inv: "Inventory",
        dest_name: str,
        repo_root: str,
    ) -> "Inventory":
        ## @brief Build a per-repo Inventory from a pre-sync path list file.
        ##
        ## Called lazily from ``_sync_one()`` when a repo's sync slot opens,
        ## not upfront for all repos.  The path file is written by
        ## ``build-repo-paths.sh`` during the startup find pass and lives
        ## under ``/tmp/mirror-dedupe/<dest_name>.paths``.
        ##
        ## The file is unlinked immediately after opening so it vanishes
        ## even if this process subsequently crashes - the kernel keeps the
        ## inode alive via the open file descriptor until it is closed.
        ##
        ## File format: null-delimited ``rel_path\0inode\0`` pairs, where
        ## ``rel_path`` is relative to ``repo_root`` (includes the repo
        ## prefix, e.g. ``postgresql/dists/focal/Release``).
        ##
        ## @param path_file   Path to the pre-sync paths file.
        ## @param pool_inv    Complete pool inventory for inode-to-hash lookup.
        ## @param dest_name   Repo dest directory name (used as inventory id).
        ## @param repo_root   Root of the repository tree on disk.
        ## @return A populated ``Inventory``.

        inv = Inventory(id=dest_name, path=str(Path(repo_root) / dest_name))

        try:
            fd = os.open(path_file, os.O_RDONLY)
        except OSError:
            return inv  ## path file absent - empty inventory, no stale sweep

        ## Unlink while the fd is still open.  The file disappears from the
        ## directory immediately; the kernel frees the inode when the fd closes.
        try:
            os.unlink(path_file)
        except OSError:
            pass

        try:
            with os.fdopen(fd, "rb") as f:
                data = f.read()
        except OSError:
            return inv

        ## Parse null-delimited pairs written by build-repo-paths.sh
        parts = data.split(b"\0")
        for i in range(0, len(parts) - 1, 2):
            rel = parts[i].decode()
            ino_str = parts[i + 1].decode().strip()
            if not rel or not ino_str:
                continue
            ino = int(ino_str)

            ## Every path found on disk is a stale candidate until Node.sync()
            ## discards it by declaring the path wanted.
            inv.stale_paths.add(rel)

            ## Cross-reference against pool inventory to build hash index.
            h = pool_inv.get_hash(ino)
            if h is not None:
                inv._dict[h] = ino
                inv._rev[ino] = h

        return inv

    @staticmethod
    def from_pool(pool_root: str) -> "Inventory":
        ## @brief Build an inventory by scanning the content-addressed pool.
        ##
        ## Streams ``find`` output via ``find_stream()`` so the process
        ## never buffers more than one read chunk regardless of pool size.
        ## The pool subdirectory structure is
        ## ``by-hash/SHA256/{ab}/{cd}/{hash}``, so ``%f`` (filename) is
        ## the SHA-256 hash and ``%i`` the inode.
        ##
        ## No lock is needed during construction - the inventory is not
        ## shared before this method returns.
        ##
        ## @param pool_root  Root directory of the content-addressed pool.
        ## @return A populated ``Inventory`` (empty if the pool does not
        ##         exist or ``find`` fails).
        pool_dir = Path(pool_root) / "by-hash" / "SHA256"
        if not pool_dir.is_dir():
            return Inventory(id="_pool_", path=pool_root)

        inv = Inventory(id="_pool_", path=pool_root)
        try:
            it = find_stream(str(pool_dir), r"%f\0%i\0")
            for h, ino_str in zip(it, it):
                ino = int(ino_str)
                inv._dict[h] = ino
                inv._rev[ino] = h
        except (ExceptionMsg, OSError):
            pass

        return inv

    @staticmethod
    def from_repos(
        repo_root: str,
        pool_inv: "Inventory",
        managed_dests: set,
    ) -> dict:
        ## @brief Build per-repo inventories from a single ``find`` pass.
        ##
        ## Runs ``find`` on *repo_root* once, collecting every regular
        ## file's relative path and inode.  Files whose first path
        ## component matches a name in *managed_dests* are
        ## cross-referenced against *pool_inv* via reverse (inode->hash)
        ## lookup - if the inode maps to a known pool hash, the pair is
        ## stored in that dest's ``Inventory``.
        ##
        ## Directories not in *managed_dests* are silently skipped.
        ##
        ## Lock-free during construction - none of the returned
        ## inventories are shared until the caller distributes them.
        ##
        ## @param repo_root     Root of the repository tree.
        ## @param pool_inv      Already-populated pool ``Inventory``.
        ## @param managed_dests Set of directory names under *repo_root*
        ##                      to scan (e.g. ``{"postgresql", "test"}``).
        ## @return ``{dest_name: Inventory}`` - one entry per managed
        ##         destination.
        root = Path(repo_root)
        if not root.is_dir():
            return {}

        result: dict = {
            d: Inventory(id=d, path=str(root / d)) for d in managed_dests
        }
        if not result:
            return result

        try:
            it = find_stream(str(root), r"%P\0%i\0", extra_args=["-xdev"])
            for rel, ino_str in zip(it, it):
                ino = int(ino_str)
                dest_name = rel.split("/", 1)[0]

                inv = result.get(dest_name)
                if inv is None:
                    continue

                # Record every path we find - pool-linked or not.  Release,
                # InRelease, Packages, and Sources files all land here even
                # though they have no pool hash.  Node.sync() removes a path
                # from this set the moment it is declared wanted, so anything
                # left after the sync is stale and safe to delete.
                inv.stale_paths.add(rel)

                h = pool_inv.get_hash(ino)
                if h is not None:
                    inv._dict[h] = ino
                    inv._rev[ino] = h
        except (ExceptionMsg, OSError):
            pass

        return result
