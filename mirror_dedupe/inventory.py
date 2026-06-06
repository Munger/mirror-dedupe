#/usr/bin/env python3
## @file inventory.py
##
## @brief Thread-safe in-memory hash-to-inode inventory for mirror-dedupe.
##
## ``Inventory`` wraps a pair of ``dict``s (hash→inode, inode→hash) with an
## internal ``threading.Lock`` so that every read and write is atomic.  The
## class has no concept of paths, pool layout, or repo structure — it is
## purely a bidirectional lookup table.
##
## Two logical inventories exist at runtime:
##
## 1. **Pool inventory** — every file in the content-addressed pool,
##    built once at startup via ``Inventory.from_pool(pool_root)``.
## 2. **Per-repo inventory** — a subset of the pool whose inodes are
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

from __future__ import annotations

import platform
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Dict


class Inventory:
    ## @brief Thread-safe bidirectional ``{hash↔inode}`` lookup table.
    ##
    ## ``_dict`` maps SHA-256 hex (64-char string) → inode (int).
    ## ``_rev`` maps inode (int) → SHA-256 hex (64-char string).
    ##
    ## Both dicts are always updated under the same lock so they never
    ## drift apart.  Because the from-scratch factories are called before
    ## the object is shared, they can write directly to the private dicts
    ## without acquiring the lock.

    _find_cmd: str | None = None

    def __init__(self, id: str = "", path: str = "") -> None:
        ## @brief Initialise an empty inventory.
        ## @param id    Unique identifier (e.g. ``"_pool_"``).
        ## @param path  Filesystem path this inventory was built from.
        self._dict: Dict[str, int] = {}
        self._rev: Dict[int, str] = {}
        self._lock = threading.Lock()
        self.id: str = id
        self.path: str = path

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
        with self._lock:
            return len(self._dict)

    # -- writers ----------------------------------------------------------

    def add(self, hash: str, inode: int) -> None:
        ## @brief Insert or update a single hash↔inode mapping.
        ## @param hash   SHA-256 hex string.
        ## @param inode  Filesystem inode number.
        with self._lock:
            self._dict[hash] = inode
            self._rev[inode] = hash

    def add_bulk(self, entries: Dict[str, int]) -> None:
        ## @brief Insert or update multiple mappings atomically.
        ## @param entries  Dict of ``{hash: inode}`` to add.
        with self._lock:
            self._dict.update(entries)
            for h, ino in entries.items():
                self._rev[ino] = h

    # -- find binary ------------------------------------------------------

    @classmethod
    def _find_binary(cls) -> str:
        ## @brief Resolve the ``find`` command, preferring GNU find.
        ##
        ## On macOS the BSD ``find`` lacks ``-printf``, so we require
        ## ``gfind`` from Homebrew's ``findutils``.  On Linux the system
        ## ``find`` is GNU find and works directly.
        ##
        ## The resolved path is cached on the class so that the lookup
        ## runs only once per process.
        ##
        ## @return The path to the ``find`` executable.
        ## @raise RuntimeError  If GNU ``find`` is not available.
        if cls._find_cmd is not None:
            return cls._find_cmd

        if platform.system() == "Darwin":
            gfind = shutil.which("gfind")
            if gfind is None:
                raise RuntimeError(
                    "gfind not found.  Install with: brew install findutils"
                )
            cls._find_cmd = gfind
        else:
            cls._find_cmd = "find"

        return cls._find_cmd

    # -- factories --------------------------------------------------------

    @staticmethod
    def from_pool(pool_root: str) -> "Inventory":
        ## @brief Build an inventory by scanning the content-addressed pool.
        ##
        ## Uses ``find -printf`` via the resolved ``find`` binary (GNU
        ## ``find`` on Linux, ``gfind`` on macOS).  The pool subdirectory
        ## structure is ``by-hash/SHA256/{2-char-prefix}/{2-char-suffix}/{hash}``,
        ## so the filename itself is the SHA-256 hash.  ``%f`` extracts the
        ## filename (the hash) and ``%i`` the inode.
        ##
        ## Both forward and reverse dicts are populated from the single
        ## ``find`` pass.
        ##
        ## No lock is needed during construction — the inventory is not
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
            data = subprocess.check_output(
                [
                    Inventory._find_binary(), str(pool_dir), "-type", "f",
                    "-printf", "%f\\0%i\\0",
                ],
                timeout=30,
                stderr=subprocess.DEVNULL,
            )
        except (subprocess.CalledProcessError, OSError):
            return inv

        parts = data.split(b"\0")
        for i in range(0, len(parts) - 1, 2):
            h = parts[i].decode()
            ino = int(parts[i + 1])
            inv._dict[h] = ino
            inv._rev[ino] = h

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
        ## cross-referenced against *pool_inv* via reverse (inode→hash)
        ## lookup — if the inode maps to a known pool hash, the pair is
        ## stored in that dest's ``Inventory``.
        ##
        ## Directories not in *managed_dests* are silently skipped.
        ##
        ## Lock-free during construction — none of the returned
        ## inventories are shared until the caller distributes them.
        ##
        ## @param repo_root     Root of the repository tree.
        ## @param pool_inv      Already-populated pool ``Inventory``.
        ## @param managed_dests Set of directory names under *repo_root*
        ##                      to scan (e.g. ``{"postgresql", "test"}``).
        ## @return ``{dest_name: Inventory}`` — one entry per managed
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
            data = subprocess.check_output(
                [
                    Inventory._find_binary(), str(root), "-xdev", "-type", "f",
                    "-printf", "%P\\0%i\\0",
                ],
                timeout=60,
                stderr=subprocess.DEVNULL,
            )
        except (subprocess.CalledProcessError, OSError):
            return result

        parts = data.split(b"\0")
        for i in range(0, len(parts) - 1, 2):
            rel = parts[i].decode()
            ino = int(parts[i + 1])
            dest_name = rel.split("/", 1)[0]

            inv = result.get(dest_name)
            if inv is None:
                continue

            h = pool_inv.get_hash(ino)
            if h is not None:
                inv._dict[h] = ino
                inv._rev[ino] = h

        return result
