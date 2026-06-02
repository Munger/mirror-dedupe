## @file inventory.py
##
## @brief In-memory inventory of pool hashes, inodes, and repo-path
##        links with filesystem helpers.
##
## The ``Inventory`` dataclass tracks the relationship between SHA-256
## hashes, filesystem inodes, and repository paths.  It is the
## authoritative view for the ``Fetcher`` and provides helpers for
## hardlinking, unlinking, and bootstrapping the pool from existing
## repo files.
##
## @copyright Copyright (c) 2026 Tim Hosking
## @see https://github.com/munger
## @par Licence: MIT

from __future__ import annotations

import hashlib
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List

from mirror_dedupe.config import Config

@dataclass
class Inventory:
    ## @brief Tracks inodes, pool hashes, and repo-path links.
    ##
    ## Fields:
    ## * ``inodes``    — ``{inode: {"hash": str | None, "links": int}}``
    ## * ``pool_files`` — ``{hash: inode}``
    ## * ``repo_files`` — ``{repo_path: inode}``
    ## * ``pool_root``  — resolved pool root
    ## * ``repos_root`` — resolved repos root

    inodes: Dict[int, Dict[str, object]] = field(default_factory=dict)
    pool_files: Dict[str, int] = field(default_factory=dict)
    repo_files: Dict[str, int] = field(default_factory=dict)
    pool_root: str = ""
    repos_root: str = ""

    @classmethod
    def get(cls, refresh: bool = False) -> "Inventory":
        ## @brief Lazy singleton accessor.
        ##
        ## Builds once per process unless *refresh* is True.  The
        ## singleton is re-built when ``pool_root`` or ``repos_root``
        ## change between calls.
        ##
        ## @param refresh  Force a full rebuild even if a cache exists.
        ## @return The singleton Inventory instance.

        global _inventory_cache
        cfg = Config.load()
        pool_root_resolved = str(Path(cfg.pool_root).resolve())
        repos_root_resolved = str(Path(cfg.repo_root).resolve())
        if (
            not refresh
            and _inventory_cache is not None
            and _inventory_cache.pool_root == pool_root_resolved
            and _inventory_cache.repos_root == repos_root_resolved
        ):
            return _inventory_cache
        _inventory_cache = build_inventory(pool_root_resolved, repos_root_resolved)
        return _inventory_cache

    # --- update helpers -------------------------------------------------

    def record_pool_hash(self, h: str, inode: int, links: int | None = None) -> None:
        ## @brief Record a hash present in the pool.
        ## @param h      SHA-256 hash string.
        ## @param inode  Filesystem inode of the pool file.
        ## @param links  Hardlink count (optional; read from stat if omitted).

        entry = self.inodes.setdefault(inode, {"hash": h, "links": links or 0})
        entry["hash"] = h
        if links is not None:
            entry["links"] = links
        self.pool_files[h] = inode

    def record_repo_link(self, h: str, repo_path: str) -> None:
        ## @brief Record that a repo path links (or should link) to a hash.
        ## @param h          SHA-256 hash.
        ## @param repo_path  Absolute path of the repo file.

        repo_inode = self.pool_files.get(h)
        if repo_inode is None:
            return
        self.repo_files[repo_path] = repo_inode

    def remove_repo_path(self, repo_path: str) -> None:
        ## @brief Remove a repo path from bookkeeping (e.g., deletion).
        ## @param repo_path  Absolute path to remove.

        self.repo_files.pop(repo_path, None)

    def link_count(self, h: str) -> int:
        ## @brief Return how many repo paths are recorded for this hash.
        ## @param h  SHA-256 hash.
        ## @return The recorded link count for the inode holding *h*.

        inode = self.pool_files.get(h)
        if inode is not None:
            entry = self.inodes.get(inode)
            if entry:
                return int(entry.get("links", 0))
        return 0

    # --- filesystem operations -----------------------------------------

    def _hash_path(self, h: str) -> Path:
        return Path(self.pool_root) / "by-hash" / "SHA256" / h[:2] / h[2:4] / h

    def link_repo_path(self, h: str, repo_path: str) -> None:
        ## @brief Hardlink the pool hash file into *repo_path* and update bookkeeping.
        ##
        ## Creates parent directories as needed.  If *repo_path* already
        ## exists and shares the same inode as the pool file, this is a
        ## no-op.  If it exists with a different inode, a
        ## ``FileExistsError`` is raised to prevent data corruption.
        ##
        ## @param h          SHA-256 hash.
        ## @param repo_path  Target path for the hardlink.
        ## @raise FileNotFoundError  If the pool hash file is missing.
        ## @raise FileExistsError    If *repo_path* exists with different content.

        dest = self._hash_path(h)
        repo_p = Path(repo_path)
        repo_p.parent.mkdir(parents=True, exist_ok=True)
        if not dest.exists():
            raise FileNotFoundError(f"Pool hash missing: {dest}")
        if repo_p.exists():
            try:
                if dest.stat().st_ino == repo_p.stat().st_ino:
                    st = dest.stat()
                    self.record_pool_hash(h, st.st_ino, st.st_nlink)
                    self.record_repo_link(h, str(repo_p))
                    return
            except FileNotFoundError:
                pass
            raise FileExistsError(f"Repo path exists with different content: {repo_p}")
        os.link(dest, repo_p)
        st = dest.stat()
        self.record_pool_hash(h, st.st_ino, st.st_nlink)
        self.record_repo_link(h, str(repo_p))

    def unlink_repo_path(self, repo_path: str) -> None:
        ## @brief Unlink a repo path from the filesystem and update bookkeeping.
        ##
        ## If the pool file's link count drops to 1 (only the pool
        ## itself), the pool file is also removed.
        ##
        ## @param repo_path  Target path to unlink.

        repo_p = Path(repo_path)
        repo_inode = self.repo_files.get(str(repo_p))
        if repo_p.exists():
            try:
                repo_p.unlink()
            except FileNotFoundError:
                pass
        if repo_inode is not None:
            entry = self.inodes.get(repo_inode)
            h = entry.get("hash") if entry else None
            if h:
                dest = self._hash_path(h)
                try:
                    st = dest.stat()
                    self.record_pool_hash(h, st.st_ino, st.st_nlink)
                    if st.st_nlink <= 1:
                        try:
                            dest.unlink()
                        except FileNotFoundError:
                            pass
                        self.pool_files.pop(h, None)
                        self.inodes.pop(repo_inode, None)
                except FileNotFoundError:
                    pass
        self.remove_repo_path(repo_path)

    def link_pool_from_repo(self, h: str, repo_path: str, verify: bool = True) -> None:
        ## @brief Ensure the pool has this hash by linking from an existing repo file.
        ##
        ## Used when bootstrapping the pool from pre-existing repo files
        ## (e.g. initial migration).  Optionally verifies the SHA-256
        ## digest before linking.
        ##
        ## @param h          SHA-256 hash.
        ## @param repo_path  Source file to link into the pool.
        ## @param verify     Whether to verify the hash before linking (default True).
        ## @raise FileNotFoundError  If the source file is missing.
        ## @raise ValueError        If the hash does not match (when *verify* is True).

        repo_p = Path(repo_path)
        if not repo_p.exists():
            raise FileNotFoundError(f"Repo file missing: {repo_p}")
        dest = self._hash_path(h)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            st = dest.stat()
            self.record_pool_hash(h, st.st_ino, st.st_nlink)
            self.record_repo_link(h, str(repo_p))
            return
        if verify:
            digest = hashlib.sha256(repo_p.read_bytes()).hexdigest()
            if digest != h:
                raise ValueError(f"Hash mismatch linking pool from repo: expected {h}, got {digest}")
        os.link(repo_p, dest)
        st = dest.stat()
        self.record_pool_hash(h, st.st_ino, st.st_nlink)
        self.record_repo_link(h, str(repo_p))


_inventory_cache: Inventory | None = None


def build_inventory(pool_root: str, repos_root: str) -> Inventory:
    ## @brief Scan pool and repos via ``find`` to map hashes to repo paths.
    ##
    ## Uses a single ``find`` invocation with ``-printf`` to collect
    ## inode, link count, and path for every file under both roots.
    ## Builds and returns a populated ``Inventory``.
    ##
    ## @param pool_root   Absolute path to the content-addressable pool.
    ## @param repos_root  Absolute path to the repo tree.
    ## @return A fully populated Inventory.

    proc = subprocess.run(
        ["find", pool_root, repos_root, "-xdev", "-type", "f", "-printf", "%i %n %p\0"],
        check=True,
        capture_output=True,
    )
    data = proc.stdout.split(b"\0")

    inodes: Dict[int, Dict[str, object]] = {}
    pool_files: Dict[str, int] = {}
    repo_files: Dict[str, int] = {}
    for entry in data:
        if not entry:
            continue
        try:
            inode_str, links_str, path_str = entry.decode("utf-8", errors="ignore").split(" ", 2)
            inode = int(inode_str)
            links = int(links_str)
        except ValueError:
            continue
        path = path_str.strip()
        entry_ref = inodes.setdefault(inode, {"hash": None, "links": links})
        entry_ref["links"] = links
        if path.startswith(pool_root):
            hash_val = Path(path).name
            entry_ref["hash"] = hash_val
            pool_files[hash_val] = inode
        elif path.startswith(repos_root):
            repo_files[path] = inode

    return Inventory(
        inodes=inodes,
        pool_files=pool_files,
        repo_files=repo_files,
        pool_root=pool_root,
        repos_root=repos_root,
    )
