## @file sync_orch.py
##
## @brief Orchestrator for the schema-based sync pipeline.
##
## Manages per-repo download pools, repo-level concurrency, per-repo
## locking, stale sweep, and pool-level orphan cleanup.  The entry
## point ``sync_repos()`` is called from the CLI's ``--sync`` path.
##
## @copyright Copyright (c) 2026 Tim Hosking
## @see https://github.com/munger
## @par Licence: MIT

from __future__ import annotations

import concurrent.futures
import fcntl
import os
import signal
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from . import repos as _repos  # noqa: F401  # trigger Repo subclass registration
from .config import Config, DEFAULT_CONFIG_DIR
from .lib.log import log
from .lib.http_download import kill_active_subprocesses
from .schema.repo import Repo
from .schema.upstream import Upstream, Upstreams


class RepoLock:
    ## @brief Per-repo file lock to prevent concurrent syncs.

    FLOCK_DIR = ".mirror-dedupe"
    LOCK_FILE = "sync.lock"

    def __init__(self, repo_root: str, repo_name: str) -> None:
        ## @brief Initialise a RepoLock for *repo_name* under *repo_root*.
        ##
        ## The lock file lives at ``{repo_root}/.mirror-dedupe/{repo_name}/sync.lock``.
        ##
        ## @param repo_root  Root directory for all repos.
        ## @param repo_name  Name of the repo to lock.
        ## @return None
        self.path = Path(repo_root) / self.FLOCK_DIR / repo_name / self.LOCK_FILE
        self.fd: int | None = None

    def acquire(self, timeout: float = 600) -> None:
        ## @brief Acquire an exclusive lock, waiting up to *timeout* seconds.
        ##
        ## Uses non-blocking ``flock`` in a retry loop with 1-second sleeps.
        ## Designed to guard against multiple ``mirror-dedupe --sync``
        ## processes touching the same repo simultaneously.
        ##
        ## @param timeout  Maximum seconds to wait for the lock.
        ## @raises TimeoutError  If the lock cannot be acquired within *timeout*.
        ## @return None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fd = os.open(str(self.path), os.O_CREAT | os.O_RDWR)
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"Could not acquire lock for {self.path} "
                        f"after {timeout}s"
                    )
                time.sleep(1)

    def release(self) -> None:
        ## @brief Release the lock and close the file descriptor.
        ##
        ## Safe to call multiple times — the ``if self.fd is not None``
        ## guard prevents double-close.
        ##
        ## @return None
        if self.fd is not None:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_UN)
            finally:
                os.close(self.fd)
                self.fd = None

    def __enter__(self) -> "RepoLock":
        ## @brief Context manager entry: acquire the lock.
        ## @return This RepoLock instance.
        self.acquire()
        return self

    def __exit__(self, *args: Any) -> None:
        ## @brief Context manager exit: release the lock.
        ## @param *args  Standard exception tuple (unused).
        ## @return None
        self.release()


def _repo_from_config(mirror_cfg: Dict[str, Any], cfg: Config) -> Repo:
    ## @brief Build a ``Repo`` instance from a YAML config dict.
    ##
    ## Reads name, upstreams, distributions, architectures, components,
    ## and params from *mirror_cfg*, resolves the concrete Repo subclass
    ## via ``Repo.get_type_for_urls()``, and attaches all config-derived
    ## values (suites, architectures, ipv6_ok, etc.) as ``repo["params"]``.
    ##
    ## @param mirror_cfg  Parsed YAML config for one enabled repo.
    ## @param cfg         Global ``Config`` singleton (for defaults).
    ## @return A fully wired ``Repo`` instance (still needs ``sync()``).
    name = mirror_cfg.get("name", "unknown")
    upstreams_raw = mirror_cfg.get("upstreams") or []
    if not upstreams_raw:
        upstream = mirror_cfg.get("upstream", "")
        upstreams_raw = [upstream] if upstream else []

    ordered: List[str] = []
    seen: set[str] = set()
    upstream_objs = Upstreams()
    for u in upstreams_raw:
        if isinstance(u, dict):
            url = u.get("url", "")
            sync_method = u.get("sync_method")
        else:
            url = str(u) if u else ""
            sync_method = None
        if not url or url in seen:
            continue
        seen.add(url)
        ordered.append(url)
        upstream_objs.append(Upstream(url=url, sync_method=sync_method))

    releases = mirror_cfg.get("releases") or mirror_cfg.get("distributions") or []

    rt_cls, _ = Repo.get_type_for_urls(
        {"repo_type": mirror_cfg.get("repo_type", "unknown")}, ordered
    )
    if rt_cls is None:
        rt_cls = Repo

    repo = rt_cls(
        name=name,
        upstreams=upstream_objs,
        upstream_idx=0,
        repo_type=getattr(rt_cls, "REPO_TYPE", "unknown"),
    )
    repo["dest"] = mirror_cfg.get("dest", name)
    if upstream_objs:
        repo["uri"] = upstream_objs[0].url

    params: Dict[str, Any] = {}
    if releases:
        params["suites"] = releases
    arches = mirror_cfg.get("architectures")
    if arches:
        params["architectures"] = arches if isinstance(arches, list) else [arches]
    comps = mirror_cfg.get("components")
    if comps:
        params["components"] = comps if isinstance(comps, list) else [comps]

    mirror_params = mirror_cfg.get("params") or {}
    ipv6_enabled = mirror_params.get("ipv6_enabled")
    if ipv6_enabled is not None:
        params["ipv6_ok"] = ipv6_enabled

    if params:
        repo["params"] = params

    return repo


def _pool_sweep(pool_root: str) -> None:
    ## @brief Remove pool files with no hardlinks (``st_nlink == 1``).
    ##
    ## Purges orphaned content from the pool that isn't referenced by any
    ## repo destination.  Designed to be called once after all repos have
    ## completed their sync, so that in-progress downloads don't trigger
    ## false sweeps.
    ##
    ## Also removes empty subdirectories within ``by-hash/SHA256/``.
    ##
    ## @param pool_root  Root path of the content-addressed pool.
    ## @return None
    by_hash = Path(pool_root) / "by-hash" / "SHA256"
    if not by_hash.exists():
        return

    removed = 0
    for dirpath, dirnames, filenames in os.walk(str(by_hash), topdown=False):
        for name in filenames:
            path = Path(dirpath) / name
            try:
                if path.stat().st_nlink == 1:
                    path.unlink()
                    removed += 1
            except OSError:
                continue

    for dirpath, dirnames, filenames in os.walk(str(by_hash), topdown=False):
        try:
            dp = Path(dirpath)
            if dp != by_hash and not any(dp.iterdir()):
                dp.rmdir()
        except OSError:
            continue

    if removed:
        log(f"Pool sweep: removed {removed} orphaned files (st_nlink == 1)", level="INFO")


def _sync_one_repo(
    repo: Repo,
    cfg: Config,
) -> None:
    ## @brief Sync a single repo: download metadata, expand tree, fetch packages.
    ##
    ## Creates a per-repo ``ThreadPoolExecutor`` sized by
    ## ``repo.params.parallel_downloads`` (or ``cfg.parallel_downloads``),
    ## acquires a ``RepoLock`` to prevent cross-process clashes, and
    ## delegates to ``repo.sync()``.
    ##
    ## @param repo  The ``Repo`` instance to sync.
    ## @param cfg   Global ``Config`` singleton.
    ## @return None
    name = repo.get("name", "unknown")
    dest = repo.get("dest", "")
    params = repo.get("params") or {}
    workers = params.get("parallel_downloads", cfg.parallel_downloads)

    with RepoLock(cfg.repo_root, name):
        log(f"Syncing repo '{name}' to '{dest}'", level="INFO")
        config = repo.get("params")
        if config is None:
            config = {"ipv6_ok": True}
            repo["params"] = config
        ipv6_before = config.get("ipv6_ok", True)
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            repo.sync(pool=pool, config=config)
        if config.get("ipv6_ok") is False and ipv6_before is not False:
            log(f"  Repo '{name}': IPv6 unavailable, falling back to IPv4", level="INFO")

        stats = getattr(repo, "_sync_stats", None)
        if stats:
            log(
                f"Repo '{name}' done: {stats.get('ok', 0)} ok, "
                f"{stats.get('skipped', 0)} skipped, "
                f"{stats.get('errors', 0)} errors",
                level="INFO",
            )


def sync_repos(
    repo_names: List[str],
    config_dir: Optional[str] = None,
) -> None:
    ## @brief Sync a list of repos by name.
    ##
    ## This is the top-level entry point called from the CLI's ``--sync``
    ## path.  Loads the global ``Config``, discovers enabled repos,
    ## dispatches each to its own per-repo download pool via
    ## ``_sync_one_repo``, and runs a pool-level orphan sweep when all
    ## repos complete.
    ##
    ## @param repo_names  List of repo names (corresponding to
    ##                    ``repos-enabled/{name}.conf``).
    ## @param config_dir  Override path to the configuration directory.
    ## @return None
    cfg = Config.load(config_dir)

    repos_dir = Path(config_dir or cfg._config_dir or DEFAULT_CONFIG_DIR) / "repos-enabled"
    mirrors: List[Dict[str, Any]] = []
    for name in repo_names:
        path = repos_dir / f"{name}.conf"
        if not path.exists():
            log(f"Repo config not found: {path}", level="WARN")
            continue
        with open(path) as f:
            mirror_cfg = yaml.safe_load(f) or {}
            mirror_cfg["name"] = name
            mirrors.append(mirror_cfg)

    if not mirrors:
        log("No repos to sync", level="WARN")
        return

    max_concurrent = cfg.max_concurrent_syncs

    def _sigint_handler(signum: int, frame: Any) -> None:
        ## @brief Kill all tracked subprocesses on Ctrl-C and restore default.
        ##
        ## Without this, orphaned curl processes would continue downloading
        ## to staging files even after the Python process exits.
        ##
        ## @param signum  Signal number (SIGINT = 2).
        ## @param frame   Current stack frame (unused).
        ## @return None
        kill_active_subprocesses()
        signal.signal(signal.SIGINT, signal.SIG_DFL)

    original_sigint = signal.signal(signal.SIGINT, _sigint_handler)
    try:
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(max_concurrent, len(mirrors))
        ) as repo_pool:
            futures = {}
            for mirror_cfg in mirrors:
                repo = _repo_from_config(mirror_cfg, cfg)
                future = repo_pool.submit(_sync_one_repo, repo, cfg)
                futures[future] = mirror_cfg.get("name", "unknown")

            for future in concurrent.futures.as_completed(futures):
                name = futures[future]
                try:
                    future.result()
                    log(f"Repo '{name}' sync complete", level="INFO")
                except Exception as e:
                    log(f"Repo '{name}' sync failed: {e}", level="ERROR")

        _pool_sweep(cfg.pool_root)
    finally:
        signal.signal(signal.SIGINT, original_sigint)
