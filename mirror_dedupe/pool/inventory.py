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
    # inode -> {"hash": str | None, "links": int}
    inodes: Dict[int, Dict[str, object]] = field(default_factory=dict)
    # hash -> inode
    pool_files: Dict[str, int] = field(default_factory=dict)
    # repo_path -> inode
    repo_files: Dict[str, int] = field(default_factory=dict)
    pool_root: str = ""
    repos_root: str = ""

    @classmethod
    def get(cls, refresh: bool = False) -> "Inventory":
        """Lazy singleton accessor: build once per process unless refresh=True."""
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
        """Record a hash present in the pool (inode from pool file)."""
        entry = self.inodes.setdefault(inode, {"hash": h, "links": links or 0})
        entry["hash"] = h
        if links is not None:
            entry["links"] = links
        self.pool_files[h] = inode

    def record_repo_link(self, h: str, repo_path: str) -> None:
        """Record that a repo path links (or should link) to a hash."""
        repo_inode = self.pool_files.get(h)
        if repo_inode is None:
            return
        self.repo_files[repo_path] = repo_inode

    def remove_repo_path(self, repo_path: str) -> None:
        """Remove a repo path from bookkeeping (e.g., deletion)."""
        self.repo_files.pop(repo_path, None)

    def link_count(self, h: str) -> int:
        """Return how many repo paths are recorded for this hash."""
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
        """Hardlink the pool hash file into repo_path and update bookkeeping."""
        dest = self._hash_path(h)
        repo_p = Path(repo_path)
        repo_p.parent.mkdir(parents=True, exist_ok=True)
        if not dest.exists():
            raise FileNotFoundError(f"Pool hash missing: {dest}")
        if repo_p.exists():
            try:
                if dest.stat().st_ino == repo_p.stat().st_ino:
                    # Already linked; refresh counts/mapping
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
        """Unlink a repo path from the filesystem and update bookkeeping."""
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
        """Ensure the pool has this hash by linking from an existing repo file."""
        repo_p = Path(repo_path)
        if not repo_p.exists():
            raise FileNotFoundError(f"Repo file missing: {repo_p}")
        dest = self._hash_path(h)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            # Already present; just link repo -> pool mapping.
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
    """Scan pool and repos once via find to map hashes to repo paths."""

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
