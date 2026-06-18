## @file repo.py
##
## @brief Repo abstraction, registry, and high-level factories.
##
## ``Repo`` is the root node for repo-type-specific ecosystems (APT,
## Yum, etc.).  Each concrete ``Repo`` subclass overrides ``on_parse()``
## to populate its schema tree, and can be auto-detected via a lightweight
## ``is_this_yours()`` probe.  ``Repos`` is the corresponding
## ``NodeList`` wrapper.
##
## @copyright Copyright (c) 2026 Tim Hosking
## @see https://github.com/munger
## @par Licence: MIT

from __future__ import annotations

import concurrent.futures
import fcntl
import json
import os
import platform
import shlex
import subprocess
import sys
import faulthandler
import signal
import threading
import time
import traceback
from pathlib import Path
from typing import Any, ClassVar, Dict, List, Optional, Tuple, Type, TypeVar
from datetime import datetime, timezone

import yaml

from .mdnode import MDNode as Node
from ..lib.node_x import SerialisableNodeList as NodeList
from ..schema.architecture import Architecture, Architectures
from ..schema.component import Component, Components
from ..schema.suite import Suite, Suites
from ..schema.distribution import Distribution, Distributions
from ..schema.index import Index, Indices
from ..schema.release import Release, Releases
from ..schema.vars import Vars
from ..schema.upstream import Upstream, Upstreams
from ..lib.log import log
from ..lib.subproc import kill_active_subprocesses_signal_safe
from .. import stats
from ..inventory import Inventory
from ..repo_vars import RepoVars, SyncStats


class Repo(Node):
    ## @brief Root Node for repo-type-specific ecosystems (APT, Yum, etc.).
    ##
    ## Each concrete subclass registers itself via the ``_registry``
    ## mechanism and provides ``is_this_yours()``.  ``_children`` is
    ## ``["distributions"]`` - the base ``parse()`` recurses into each
    ## distribution's own child tree.
    ##
    ## Not decorated with ``@dataclass``: with no annotated instance fields
    ## the decorator generates a zero-field ``__eq__`` (all instances of the
    ## same class compare as equal) and a zero-field ``__repr__`` (always
    ## ``Repo()``), both of which silently shadow the dict-based
    ## implementations inherited from ``Node``.

    _children = ("distributions",)

    REPO_TYPE: ClassVar[str] = "abstract"

    _registry: ClassVar[List[Type["Repo"]]] = []

    _list_fields: ClassVar[Dict[str, tuple[Type[NodeList], Type[Node]]]] = {
        "distributions": (Distributions, Distribution),
        "architectures": (Architectures, Architecture),
        "components": (Components, Component),
        "suites": (Suites, Suite),
        "indices": (Indices, Index),
        "releases": (Releases, Release),
        "upstreams": (Upstreams, Upstream),
    }

    _node_fields: ClassVar[Dict[str, Type[Node]]] = {
        "vars": Vars,
    }

    def __init__(
        self,
        *,
        upstream_idx: int = 0,
        name: str = "",
        repo_type: str = "unknown",
        gpg_key_url: str | None = None,
        upstreams: Upstreams | None = None,
    ) -> None:
        ## @brief Initialise a Repo root node.
        ##
        ## The Repo itself is a Node whose payload contains all scalar
        ## repo metadata (upstream, repo_type, etc.) plus any
        ## child nodes (Distributions, Vars, etc.) attached by parsers.
        ##
        ## @param upstream_idx  Index of upstream used during scan - persisted
        ##                       so the same mirror is preferred on sync
        ## @param name          Human-friendly repo name.
        ## @param repo_type     Repo type string (e.g. ``"apt"``).
        ## @param gpg_key_url   Optional GPG key URL for verification.
        ## @param upstreams     Upstreams collection (defaults to empty).

        data: Dict[str, Any] = {
            "upstream_idx": upstream_idx,
            "name": name,
            "repo_type": repo_type,
            "gpg_key_url": gpg_key_url,
            "upstreams": upstreams if upstreams is not None else Upstreams(),
        }

        super().__init__(data)

        self["params"] = {}

        self.distributions = Distributions()
        self._architectures = Architectures()
        self._components = Components()
        self.suites = Suites()
        self.indices = Indices()
        self.releases = Releases()

    # --- computed properties ----------------------------------------------

    @property
    def architectures(self) -> Architectures:
        ## @brief Computed property: aggregated architectres across all dists.
        ## @return The ``Architectures`` collection.
        return self._architectures

    @architectures.setter
    def architectures(self, value: Architectures) -> None:
        ## @brief Setter for the architectures property.
        ## @param value  New ``Architectures`` collection.
        ## @return None
        self._architectures = value

    @property
    def components(self) -> Components:
        ## @brief Computed property: aggregated components across all dists.
        ## @return The ``Components`` collection.
        return self._components

    @components.setter
    def components(self, value: Components) -> None:
        ## @brief Setter for the components property.
        ## @param value  New ``Components`` collection.
        ## @return None
        self._components = value

    # --- registry helpers -------------------------------------------------

    @classmethod
    def register(cls, repo_cls: Type["Repo"]) -> None:
        ## @brief Register a concrete Repo subclass for later discovery.
        ## @param repo_cls  The Repo subclass to register.
        ## @return None

        cls._registry.append(repo_cls)

    @classmethod
    def all_types(cls) -> List[Type["Repo"]]:
        ## @brief Return the list of registered Repo classes.
        ##
        ## Lazily imports known repo-type submodules so they register
        ## themselves via ``Repo.register()``.
        ##
        ## @return A copy of the registered types list.

        if not cls._registry:
            try:
                from ..repos.apt import Apt as _  # noqa: F401
            except ImportError:
                pass
        return list(cls._registry)

    # --- detection --------------------------------------------------------

    @classmethod
    def is_this_yours(cls, upstream: str, extra_suites: Optional[list[str]] = None) -> bool:
        ## @brief Lightweight probe: does this upstream look like this repo type?
        ## @param upstream      Upstream URL to probe.
        ## @param extra_suites  Optional dist names from config to try as suites.
        ## @return True if the upstream matches this repo type.
        raise NotImplementedError

    # --- selection helpers ------------------------------------------------

    @classmethod
    def get_type_for_urls(
        cls,
        repo: Any,
        urls: list[str],
        extra_suites: Optional[list[str]] = None,
    ) -> tuple["Type[Repo] | None", str | None]:
        ## @brief Select an appropriate Repo class for this repo/URL set.
        ##
        ## Behaviour mirrors the previous ``Parser.get_parser_for_url``
        ## helper but extended to support multiple candidate upstream URLs:
        ##
        ## * If ``repo["repo_type"]`` is unset/``"unknown"``, run
        ##   ``is_this_yours()`` for each registered RepoType across all
        ##   URLs until one claims a URL.
        ## * If ``repo["repo_type"]`` is set, only probe the matching
        ##   RepoType across all URLs.
        ##
        ## @param repo          A dict-like object with an optional ``repo_type`` key.
        ## @param urls          List of candidate upstream URLs.
        ## @param extra_suites  Optional list of extra suite names to probe
        ##                      (from config's ``distributions`` field).
        ## @return Tuple of ``(RepoClass | None, url_used | None)``.

        rt_cls: Type[Repo] | None = None
        used_url: str | None = None
        types = cls.all_types()

        ordered_urls: list[str] = []
        seen: set[str] = set()
        for u in urls:
            if not u:
                continue
            if u in seen:
                continue
            seen.add(u)
            ordered_urls.append(u)

        repo_type_name = repo.get("repo_type")
        if not repo_type_name or repo_type_name == "unknown":
            for t in types:
                for url in ordered_urls:
                    if extra_suites:
                        if t.is_this_yours(url, extra_suites=extra_suites):
                            rt_cls = t
                            used_url = url
                            repo["repo_type"] = getattr(t, "REPO_TYPE", "unknown")
                            break
                    else:
                        if t.is_this_yours(url):
                            rt_cls = t
                            used_url = url
                            repo["repo_type"] = getattr(t, "REPO_TYPE", "unknown")
                            break
                if rt_cls is not None:
                    break
        else:
            for t in types:
                if getattr(t, "REPO_TYPE", None) == repo_type_name:
                    rt_cls = t
                    used_url = ordered_urls[0] if ordered_urls else None
                    break

        return rt_cls, used_url

    # --- high-level factories ----------------------------------------------

    @classmethod
    def from_url(
        cls,
        upstream: str,
        *,
        repo_type: str | None = None,
        upstream_urls: list[str] | None = None,
        dist_candidates: Optional[list[str]] = None,
    ) -> "Repo":
        ## @brief Construct a Repo instance from a URL.
        ##
        ## This is the primary entry point for HTTP-based discovery.  It
        ## selects an appropriate concrete Repo subclass via the
        ## ``get_type_for_url`` registry helper and returns an instance
        ## bound to the upstream tree.
        ##
        ## @param upstream        Primary upstream URL.
        ## @param repo_type       Explicit repo type override (e.g. ``"apt"``).
        ## @param upstream_urls   Additional candidate upstream URLs.
        ## @param dist_candidates Optional dist names from config for probing.
        ## @return A fully wired Repo instance.

        data: Dict[str, Any] = {"upstream_idx": 0}  # updated after discovery
        if repo_type is not None:
            data["repo_type"] = repo_type

        urls: list[str] = [upstream, *(upstream_urls or [])]
        rt_cls, _used_url = cls.get_type_for_urls(data, urls, extra_suites=dist_candidates)
        if rt_cls is None:
            rt_cls = cls

        upstreams = Upstreams()
        ordered: list[str] = []
        seen: set[str] = set()
        for u in urls:
            if not u or u in seen:
                continue
            seen.add(u)
            ordered.append(u)

        for url in ordered:
            upstreams.append(Upstream(url=url))

        repo = rt_cls(upstreams=upstreams, **data)

        return repo

    # --- high-level instance helpers ---------------------------------------

    def parse(self, *, config: Optional[Dict[str, Any]] = None) -> "Repo":
        ## @brief Run repo-type-specific parsing and return this Repo.
        ##
        ## Calls the base ``Node.parse()`` which invokes ``on_parse()``
        ## on this node and recursively on all children declared in
        ## ``_children``.
        ##
        ## @param config  Optional configuration dict.
        ## @return This Repo after parsing (for chaining).

        return super().parse(config=config)

    def analyse(self, *, config: Optional[Dict[str, Any]] = None) -> "Repo":
        ## @brief Discover upstream and populate the schema tree (scan mode).
        ##
        ## Probes the upstream to discover distributions, parses Release
        ## files, and populates the full node tree.  Pure in-memory -
        ## no disk writes, no pool operations.
        ##
        ## @param config  Optional network configuration dict.
        ## @return This Repo after analysis.

        return self.parse(config=config)

    def sync(
        self,
        *,
        config: Optional[Dict[str, Any]] = None,
        pool: Optional[concurrent.futures.ThreadPoolExecutor] = None,
    ) -> None:
    ## @brief Build the schema tree from config and download content.
    ##
    ## Constructs the distribution tree from known config (no upstream
    ## probing), then drives ``_sync_content()`` as a dynamic work
    ## queue: known nodes are submitted to *pool* first; when one
    ## completes, ``stream()`` is called to materialise children,
    ## which are submitted in turn.  This lets the download and
    ## discovery phases overlap.
    ##
    ## @param config  Optional config dict (suite/arch/component filters).
    ## @return None
    ##
        ## All nodes know their full repo-root-relative ``path`` from
        ## construction time - no deferred path patching is needed.
        ##
        ## When *pool* is None the tree is built but no content is
        ## transferred - useful for testing or dry-run.
        ##
        ## When *pool* is set, every leaf node pushes its own stats to
        ## ``node["stats"]`` during ``sync()``.  ``Repo.stats()``
        ## aggregates these on demand.  Per-repo NDJSON and summary
        ## output are produced by the parent ``Repos`` container.
        ##
        ## @param config    Optional configuration dict (suites, filters, etc.).
        ## @param pool      Shared ``ThreadPoolExecutor`` for parallel downloads.

        self._repo_vars.sync_mode = True
        try:
            self._build_sync_tree(config=config)
            if pool is not None:
                self._repo_vars.stats = SyncStats()
                t0 = time.monotonic()
                self._sync_content(pool, config=config)
                self._repo_vars.stats.elapsed = time.monotonic() - t0
                self._repo_vars.stats.removed = self._sweep_stale()
        finally:
            self._repo_vars.sync_mode = False

    def _sync_content(
        self,
        pool: concurrent.futures.ThreadPoolExecutor,
        config: Optional[Dict[str, Any]] = None,
    ) -> None:
        ## @brief Dynamic work queue: fast-path links, worker-driven downloads.
        ##
        ## The coordinator drives tree discovery using a manual stack.
        ## For each node:
        ##   - If its hash is in the pool inventory, ``node.sync()`` runs
        ##     directly on the coordinator - the file already exists on
        ##     disk, so this is a fast ``os.link()`` with ``log()`` in the
        ##     coordinator context.
        ##   - Otherwise the node is submitted to *pool* for a genuine
        ##     download, keeping the coordinator free to continue
        ##     discovery.
        ##
        ## After either path completes, ``stream()`` is called on the node
        ## to materialise children (Release -> Index -> Package), which are
        ## stacked for the same fast/slow decision.
        ##
        ## ``node.sync()`` is the single module responsible for all disk
        ## access - the coordinator never duplicates its logic.
        ##
        ## @param pool     Shared ``ThreadPoolExecutor`` for genuine downloads.
        ## @param config   Optional configuration dict forwarded to each
        ##                  ``Node.sync()`` call.
        ## @return None

        from ..lib.exceptions import RepoAbortError

        futures: Dict[concurrent.futures.Future, Node] = {}
        # Thread-safety: _tree_iter() is unprotected, but the static tree
        # skeleton (Release -> Distribution -> Suite -> Index) is fully built
        # by _build_sync_tree() before this method runs.  Worker threads
        # only update node payloads (path, hash, size, etc.) via the
        # protected Node.__setitem__; they never add or remove structural
        # children, so the snapshot here is stable.
        stack = list(self._tree_iter())
        rv = self._repo_vars

        def _aborted() -> bool:
            return _ABORT_SYNC or rv.abort_event.is_set()

        while (stack or futures) and not _aborted():
            while stack and not _aborted():
                node = stack.pop()
                if not (node.get("uri") and node.get("path")):
                    continue

                h = node.checksum
                if (
                    h
                    and rv.pool_inv is not None
                    and rv.pool_inv.has(h)
                ):
                    try:
                        node.sync(config=config)
                        for child in node.stream():
                            stack.append(child)
                    except RepoAbortError:
                        raise
                    except Exception as e:
                        if rv.stats is not None:
                            rv.stats.add_error()
                        log(f"  {e}")
                    continue

                future = pool.submit(node.sync, config=config)
                futures[future] = node

            if not futures or _aborted():
                break

            done, _ = concurrent.futures.wait(
                futures, return_when=concurrent.futures.FIRST_COMPLETED,
            )
            for future in done:
                node = futures.pop(future)
                try:
                    future.result()
                    for child in node.stream():
                        stack.append(child)
                except RepoAbortError:
                    raise
                except Exception as e:
                    if rv.stats is not None:
                        rv.stats.add_error()
                    log(f"  {e}")

    def _sweep_stale(self) -> int:
        ## @brief Delete pre-sync paths that were never declared as wanted.
        ##
        ## ``Inventory.from_repos`` captures every file path in the repo
        ## destination into ``rv.inv.stale_paths`` before the sync begins.
        ## During the sync, ``Node.sync()`` removes each path from that set
        ## the moment the node is processed - whether it ends up being an
        ## inventory hit, a pool re-link, or a fresh download.  By the time
        ## the work queue drains, ``stale_paths`` contains only files that
        ## existed on disk but were never wanted: old package versions, dropped
        ## architectures, removed distributions.
        ##
        ## No disk walk is needed here - the stale set was built for free
        ## during the startup ``find`` pass.  The removed count is returned
        ## so the caller can store it in ``_top_stats["removed"]``, which
        ## feeds the per-repo stats record, NDJSON log, and summary table.
        ##
        ## Empty directories are pruned bottom-up after deletions.
        ##
        ## @return Number of stale files removed.

        from ..lib import fmt_size, LOG_LABEL_W
        from ..lib.log import log

        rv = self._repo_vars
        if rv.inv is None or not rv.inv.stale_paths:
            return 0

        repo_root = rv.repo_root
        dest = self.get("dest", "")
        removed = 0

        for rel in rv.inv.stale_paths:
            f = Path(repo_root) / rel
            try:
                sz = f.stat().st_size
                f.unlink()
                removed += 1
                log(f"  {'Removing':<{LOG_LABEL_W}} {fmt_size(sz):>7}  {rel}", level="INFO")
            except OSError:
                continue

        if removed:
            log(f"Removed {removed} stale files from repo '{dest}'", level="INFO")
            if dest:
                dest_path = Path(repo_root) / dest
                if dest_path.exists():
                    for dirpath, _, _ in os.walk(str(dest_path), topdown=False):
                        try:
                            if not any(Path(dirpath).iterdir()):
                                Path(dirpath).rmdir()
                        except OSError:
                            continue

        return removed

    def stats(self) -> Dict[str, Any]:
        ## @brief Return the sync statistics for this repo.
        ##
        ## Reads directly from the ``SyncStats`` accumulator on
        ## ``_repo_vars`` - updated in real time by ``Node.sync()`` during
        ## the sync run, with ``elapsed`` and ``removed`` set by the
        ## coordinator after the work queue drains.  No post-sync tree walk
        ## needed; the accumulator replaces both the per-node ``node["stats"]``
        ## dict and the old ``_top_stats`` coordinator dict.
        ##
        ## Returns all-zero values when called outside a sync run (e.g.
        ## during ``--list`` or before the first sync).
        ##
        ## @return Stats dict with keys: file_count, total_bytes,
        ##         deduped_bytes, bytes_transferred, pool_hits,
        ##         pool_misses, errors, elapsed, removed.

        rv = self._repo_vars
        if rv is None or rv.stats is None:
            return {
                "file_count": 0, "total_bytes": 0, "deduped_bytes": 0,
                "bytes_transferred": 0, "pool_hits": 0, "pool_misses": 0,
                "errors": 0, "elapsed": 0.0, "removed": 0,
            }
        return rv.stats.to_dict()

    @classmethod
    def from_config(cls, mirror_cfg: Dict[str, Any], cfg: "Config") -> "Repo":
        ## @brief Build a Repo from a YAML config dict.
        ##
        ## Resolves the concrete Repo subclass, attaches upstreams,
        ## suites, architectures, components, and network settings
        ## from *mirror_cfg* to the returned instance.
        ##
        ## @param mirror_cfg  Parsed YAML config for one enabled repo.
        ## @param cfg         Global ``Config`` singleton (for defaults).
        ## @return A ``Repo`` instance ready for ``sync()``.

        from .upstream import Upstream, Upstreams

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
            else:
                url = str(u) if u else ""
            if not url or url in seen:
                continue
            seen.add(url)
            ordered.append(url)
            upstream_objs.append(Upstream(url=url))

        releases = mirror_cfg.get("releases") or mirror_cfg.get("distributions") or []

        rt_cls, _ = cls.get_type_for_urls(
            {"repo_type": mirror_cfg.get("repo_type", "unknown")}, ordered
        )
        if rt_cls is None:
            rt_cls = cls

        repo = rt_cls(
            name=name,
            upstreams=upstream_objs,
            upstream_idx=0,
            repo_type=getattr(rt_cls, "REPO_TYPE", "unknown"),
        )
        repo["dest"] = mirror_cfg.get("dest", name)
        if upstream_objs:
            repo["uri"] = upstream_objs[0].url

        repo["gpg_key_url"] = mirror_cfg.get("gpg_key_url") or ""
        repo["gpg_key_path"] = mirror_cfg.get("gpg_key_path") or ""
        per_repo_check = mirror_cfg.get("check_gpg_signature")
        repo["check_gpg_signature"] = (
            bool(per_repo_check) if per_repo_check is not None
            else cfg.check_gpg_signature
        )

        params: Dict[str, Any] = {}
        if releases:
            params["suites"] = releases
        expand = mirror_cfg.get("expand_distributions")
        if expand is not None:
            params["expand_distributions"] = expand
        arches = mirror_cfg.get("architectures")
        if arches:
            params["architectures"] = arches if isinstance(arches, list) else [arches]
        comps = mirror_cfg.get("components")
        if comps:
            params["components"] = comps if isinstance(comps, list) else [comps]

        if params:
            repo["params"] = params

        return repo

    def _build_sync_tree(self, *, config: Optional[Dict[str, Any]] = None) -> None:
        ## @brief Construct the distribution tree from known config.
        ##
        ## Override in concrete subclasses (e.g. ``Apt``) to create
        ## Distribution nodes from configured suite names rather than
        ## discovering them via HTTP probing.
        ##
        ## @param config  Optional configuration dict.
        ## @raise NotImplementedError
        ## @return None

        raise NotImplementedError

    # --- snapshot / restore helpers ------------------------------------------

    @classmethod
    def from_snapshot(
        cls,
        snapshot: Dict[str, Any],
    ) -> "Repo":
        ## @brief Rebuild a Repo (and its children) from a plain snapshot.
        ##
        ## Expects *snapshot* to be the result of ``repo.snapshot()`` or
        ## an equivalent plain-data structure (e.g. loaded from JSON).
        ##
        ## @param snapshot  Plain dict from an earlier ``snapshot()`` call.
        ## @return A reconstructed Repo instance.

        if not isinstance(snapshot, dict):
            raise TypeError(
                f"from_snapshot expected mapping data for {cls.__name__}, "
                f"got {type(snapshot)!r}"
            )

        repo = cls._from_payload(snapshot)

        params = repo.get("params")
        if not isinstance(params, dict):
            repo["params"] = {}
        
        cls._restore_children(repo, snapshot)

        return repo


class RepoLock:
    ## @brief Per-repo file lock to prevent concurrent syncs.

    FLOCK_DIR = "mirror-dedupe"
    LOCK_FILE = "sync.lock"

    def __init__(self, mirror_root: str, repo_name: str) -> None:
        ## @brief Initialise a RepoLock for *repo_name* under *mirror_root*.
        ##
        ## @param mirror_root  Root directory for all mirror data.
        ## @param repo_name    Name of the repo to lock.
        ## @return None
        self.path = Path(mirror_root) / self.FLOCK_DIR / repo_name / self.LOCK_FILE
        self.fd: int | None = None

    def acquire(self, timeout: float = 0) -> None:
        ## @brief Acquire an exclusive lock, waiting up to *timeout* seconds.
        ##
        ## The default of 0 means a single non-blocking attempt: if the lock
        ## is held, ``TimeoutError`` is raised immediately.  Pass a positive
        ## value to retry for up to that many seconds before giving up.
        ##
        ## @param timeout  Maximum seconds to wait for the lock (default 0).
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


def _pool_sweep(pool_root: str) -> None:
    ## @brief Remove pool files with no hardlinks (``st_nlink == 1``).
    ##
    ## Purges orphaned content from the pool that isn't referenced by any
    ## repo destination.  Designed to be called once after all repos have
    ## completed their sync, so that in-progress downloads don't trigger
    ## false sweeps.  Empty subdirectories within ``by-hash/SHA256/`` are
    ## also removed.
    ##
    ## @param pool_root  Root path of the content-addressed pool.
    ## @return None
    by_hash = Path(pool_root) / "by-hash" / "SHA256"
    if not by_hash.exists():
        return

    log("Sweeping the pool and removing floaters...", level="INFO")
    removed = 0
    try:
        result = subprocess.run(
            f"find {shlex.quote(str(by_hash))} -type f -links 1 -delete -print | wc -l",
            shell=True, capture_output=True, text=True,
        )
        out = result.stdout.strip()
        if out:
            removed = int(out)
    except OSError as e:
        log(f"Pool sweep error: {e}", level="ERROR")
        return

    subprocess.run(
        ["find", str(by_hash), "-depth", "-type", "d", "-delete"],
        stderr=subprocess.DEVNULL,
    )

    if removed:
        log(f"Pool sweep: removed {removed} orphaned files (st_nlink == 1)", level="INFO")


def _build_repo_path_files(repo_root: str, dest_names: List[str]) -> None:
    ## @brief Run build-repo-paths.sh to write per-repo pre-sync path lists.
    ##
    ## Calls the bash helper which performs a single sequential find pass
    ## over all managed repo directories and routes path+inode pairs into
    ## per-repo files at /tmp/mirror-dedupe/<dest_name>.paths.
    ##
    ## Must complete before any sync worker starts so the disk scan does
    ## not compete with concurrent hardlink and download I/O.
    ##
    ## @param repo_root   Root of the repository tree on disk.
    ## @param dest_names  First-level directory names under repo_root to scan.
    ## @return None

    if not dest_names:
        return

    script = Path(__file__).resolve().parents[2] / "scripts" / "build-repo-paths.sh"
    if not script.exists():
        log(f"WARNING: build-repo-paths.sh not found at {script} - falling back to in-process repo scan", level="WARN")
        return

    try:
        subprocess.run(
            ["bash", str(script), repo_root] + dest_names,
            check=True,
        )
    except subprocess.CalledProcessError as e:
        log(f"WARNING: build-repo-paths.sh failed (exit {e.returncode}) - per-repo inventories will be empty", level="WARN")


def _check_any_sync_lock(mirror_root: str, repo_names: list[str]) -> str | None:
    ## @brief Check if any named repo has an active sync lock.
    ##
    ## Uses ``LOCK_NB`` so this is a non-blocking probe.  Returns the
    ## first repo name whose lock is held, or ``None`` if all are free.
    ##
    ## @param mirror_root  Root directory for all mirror data.
    ## @param repo_names   List of repo names to check.
    ## @return Name of the first locked repo, or ``None``.
    for name in repo_names:
        lock_path = Path(mirror_root) / RepoLock.FLOCK_DIR / name / RepoLock.LOCK_FILE
        if not lock_path.exists():
            continue
        try:
            fd = os.open(str(lock_path), os.O_RDWR)
        except (FileNotFoundError, OSError):
            continue
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
        except BlockingIOError:
            os.close(fd)
            return name
    return None


def pool_sweep_safe(cfg: "Config", *, fail_if_locked: bool = False) -> bool:
    ## @brief Sweep the pool for orphaned entries, respecting sync locks.
    ##
    ## Checks every known repo for an active lock via
    ## ``_check_any_sync_lock()``.  If any is held and *fail_if_locked*
    ## is ``True``, logs an error and returns ``False``.  If *fail_if_locked*
    ## is ``False``, skips the sweep with an info message and returns
    ## ``True`` (harmless skip).
    ##
    ## @param cfg            Loaded ``Config`` instance.
    ## @param fail_if_locked If ``True``, fail when a lock is held.
    ## @return ``True`` on success or harmless skip; ``False`` on error.
    names = cfg.list_repo_names()
    busy = _check_any_sync_lock(cfg.mirror_root, names)
    if busy:
        if fail_if_locked:
            log(
                f"ERROR: Sync in progress for '{busy}' - cannot sweep pool",
                level="ERROR",
            )
            return False
        log(
            f"Pool sweep skipped - another process holds the lock for '{busy}'",
            level="INFO",
        )
        return True
    _pool_sweep(cfg.pool_root)
    return True


_ABORT_SYNC: bool = False


class Repos(NodeList[Repo]):
    ## @brief Container for Repo instances with high-level operations.
    ##
    ## Owns ``sync_all()``, ``analyse_all()``, NDJSON output, and the
    ## cross-repo summary table.  ``session_ts`` is set once at the start
    ## of each batch and stamped into every NDJSON record.

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        ## @brief Initialise Repos with per-object state.
        ##
        ## @param *args    Positional args for ``NodeList.__init__``.
        ## @param **kwargs  Keyword args for ``NodeList.__init__``.
        ## @return None
        super().__init__(*args, **kwargs)
        self._stats: Dict[str, Any] = {}

    @property
    def session_ts(self) -> int:
        ## @brief Session timestamp for the current batch (Unix epoch seconds).
        ## @return Unix timestamp integer.
        return self._stats.get("session_ts", 0)

    @session_ts.setter
    def session_ts(self, value: int) -> None:
        ## @brief Set the session timestamp.
        ## @param value  Unix timestamp integer.
        ## @return None
        self._stats["session_ts"] = value

    @classmethod
    def from_names(
        cls,
        repo_names: List[str],
        config_dir: Optional[str] = None,
    ) -> "Repos":
        ## @brief Build a ``Repos`` instance from a list of enabled repo names.
        ##
        ## Loads ``<config_dir>/repos-enabled/{name}.conf`` for each name.
        ##
        ## @param repo_names  List of repo names (``*.conf`` filenames without
        ##                    the extension).
        ## @param config_dir  Path to the configuration directory.
        ## @return A ``Repos`` instance containing the resolved repos.

        from ..config import Config

        cfg = Config.load(config_dir)
        repos_dir = Path(cfg.config_dir) / "repos-enabled"

        instances = cls()
        for name in repo_names:
            path = repos_dir / f"{name}.conf"
            if not path.exists():
                log(f"Repo config not found: {path}", level="WARN")
                continue
            with open(path) as f:
                mirror_cfg = yaml.safe_load(f) or {}
                mirror_cfg["name"] = name
                instances.append(Repo.from_config(mirror_cfg, cfg))

        return instances

    def sync_all(self, config_dir: Optional[str] = None) -> None:
        ## @brief Sync all repos in this collection.
        ##
        ## Sets ``session_ts``, registers a SIGINT handler that kills
        ## tracked subprocesses, dispatches each repo to its own per-repo
        ## ``ThreadPoolExecutor`` via ``_sync_one()``, writes per-repo
        ## NDJSON, and prints a cross-repo summary table.
        ## Pool sweep is handled by the caller.
        ##
        ## @param config_dir  Path to the configuration directory.
        ## @return None

        from ..config import Config

        cfg = Config.load(config_dir)

        if not self:
            log("No repos to sync", level="WARN")
            return

        max_concurrent = cfg.max_concurrent_syncs

        def _sigint_handler(signum: int, frame: Any) -> None:
            ## @brief Signal handler for SIGINT: abort all syncs immediately.
            ##
            ## Sets ``_ABORT_SYNC``, kills tracked curl subprocesses
            ## (best-effort, non-blocking lock), then calls ``os._exit(130)``
            ## to terminate the process without any further Python cleanup.
            ## One Ctrl-C press is sufficient.
            ##
            ## @param signum  Signal number (unused).
            ## @param frame   Current stack frame (unused).
            ## @return None
            global _ABORT_SYNC
            _ABORT_SYNC = True
            kill_active_subprocesses_signal_safe()
            os._exit(130)

        self.session_start = datetime.now(timezone.utc)
        self.session_ts = int(self.session_start.timestamp())
        original_sigint = signal.signal(signal.SIGINT, _sigint_handler)
        faulthandler.register(signal.SIGINFO)

        # Enforce repo name uniqueness - names map to directories under
        # repo_root, so duplicates would cause two workers to fight over
        # the same destination tree, stale_paths set, and inventory file.
        # Thread-safety: these iterations over `self` (a NodeList) run in
        # the coordinator thread before the repo_pool executor is started,
        # so no concurrent modifications to the list are possible here.
        seen_names: set[str] = set()
        for repo in self:
            name = repo.get("name", "")
            if name in seen_names:
                log(
                    f"ERROR: duplicate repo name '{name}' - each repo must "
                    "have a unique name since it maps to a distinct directory "
                    "under repo_root.",
                    level="ERROR",
                )
                sys.exit(1)
            seen_names.add(name)

        log("Checking inventory...", level="INFO")
        try:
            pool_inv = Inventory.from_pool(cfg.pool_root)
        except ExceptionMsg as e:
            sys.exit(e.args[0] if e.args else "gfind not found")

        # Determine the dest directory name for each repo.
        # This is the first-level subdirectory under repo_root and is used
        # as the key for per-repo path files and inventory objects.
        def _dest_name(repo: Repo) -> str:
            dest = repo.get("dest", "")
            prefix = cfg.repo_root.rstrip("/") + "/"
            if dest.startswith(prefix):
                return dest[len(prefix):].split("/", 1)[0]
            return repo.get("name", "")

        managed_dests = [_dest_name(r) for r in self]

        # Single find pass: build-repo-paths.sh scans all managed repo
        # directories in one sequential sweep and writes per-repo
        # path+inode lists to /tmp/mirror-dedupe/<dest_name>.paths.
        # Completing this before sync workers start keeps disk access
        # sequential and avoids competing with hardlink and download I/O.
        _build_repo_path_files(cfg.repo_root, managed_dests)

        # Assign RepoVars without per-repo inventory - each repo loads its
        # own inventory lazily from the tmpfs path file when its sync slot
        # opens (in _sync_one), so only max_concurrent_syncs inventories
        # are in memory at once rather than all of them simultaneously.
        # Thread-safety: still in the coordinator thread; no concurrent
        # access to `self` or any individual repo node yet.
        for repo in self:
            repo._repo_vars = RepoVars(
                pool_inv=pool_inv,
                mirror_root=cfg.mirror_root,
                repo_root=cfg.repo_root,
                pool_root=cfg.pool_root,
                connect_timeout=cfg.connect_timeout,
            )

        t0 = time.monotonic()

        try:
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=min(max_concurrent, len(self)),
                thread_name_prefix="sync",
            ) as repo_pool:
                # Thread-safety: `self` is iterated here in the coordinator
                # thread only - workers receive individual repo references and
                # never modify the Repos NodeList.  Each repo is dispatched to
                # exactly one worker, so there is no concurrent access to any
                # individual Repo node until its worker starts.
                futures = {}
                for repo in self:
                    name = repo.get("name", "unknown")
                    future = repo_pool.submit(
                        self._sync_one, repo, cfg,
                    )
                    futures[future] = name

                for future in concurrent.futures.as_completed(futures):
                    name = futures[future]
                    try:
                        future.result()
                    except Exception as e:
                        if _ABORT_SYNC:
                            break
                        log(
                            f"Repo '{name}' sync failed: {e}\n{traceback.format_exc()}",
                            level="ERROR",
                        )

            if _ABORT_SYNC:
                os._exit(130)

            rss_mb = self._get_peak_rss_mb()
            for repo in self:
                stats.write_ndjson(
                    self.session_ts, repo,
                    mirror_root=repo._repo_vars.mirror_root if repo._repo_vars else "",
                    peak_rss_mb=rss_mb,
                )

            self.session_end = datetime.now(timezone.utc)
            self.session_elapsed = time.monotonic() - t0
            self._print_summary()
            self._print_rss()
        finally:
            signal.signal(signal.SIGINT, original_sigint)

    @staticmethod
    def _make_worker_init(name: str):
        ## @brief Create a ``ThreadPoolExecutor`` initializer that names workers.
        ##
        ## Returns a closure that, when called in each worker thread,
        ## atomically increments a counter and sets
        ## ``threading.current_thread().name`` to ``"<name> <N>"``.
        ## This enables per-repo colour mapping in log output.
        ##
        ## @param name  Repo name to embed in worker thread names.
        ## @return Initializer callable for ``ThreadPoolExecutor(initializer=...)``.
        _data: Dict[str, Any] = {"counter": 0, "lock": threading.Lock()}
        def _init() -> None:
            ## @brief Per-worker initializer: set thread name and repo tag.
            ## @return None
            with _data["lock"]:
                _data["counter"] += 1
                threading.current_thread().name = f"{name} {_data['counter']}"
            from ..lib.subproc import set_repo_tag
            set_repo_tag(name)
        return _init

    def _sync_one(
        self,
        repo: Repo,
        cfg: "Config",
    ) -> None:
        ## @brief Sync a single repo: load inventory, sync content, record stats.
        ##
        ## The per-repo inventory is loaded here - lazily, when this repo's
        ## sync slot opens - rather than upfront for all repos.  This bounds
        ## peak memory to ``max_concurrent_syncs`` inventories at once.
        ##
        ## ``Inventory.from_path_file()`` opens the tmpfs path file written
        ## by ``build-repo-paths.sh``, unlinks it immediately, and builds
        ## ``stale_paths`` plus the hash index against the already-complete
        ## pool inventory.
        ##
        ## @param repo  The ``Repo`` instance to sync.
        ## @param cfg   Global ``Config`` singleton.
        ## @return None
        from ..lib.exceptions import RepoAbortError
        from ..lib.subproc import set_repo_tag

        name = repo.get("name", "unknown")
        threading.current_thread().name = f"{name} 0"
        set_repo_tag(name)
        params = repo.get("params") or {}
        workers = params.get("parallel_downloads", cfg.parallel_downloads)

        # Determine dest_name (first-level subdir under repo_root)
        dest = repo.get("dest", "")
        prefix = cfg.repo_root.rstrip("/") + "/"
        dest_name = dest[len(prefix):].split("/", 1)[0] if dest.startswith(prefix) else name

        # Load this repo's pre-sync path list from tmpfs and build its
        # inventory.  The file is unlinked on open so it cannot leak.
        rv = repo._repo_vars
        rv.repo_name = name

        # GPG keyring fetch happens before the lock - it's a network-only
        # operation that doesn't touch repo content and is safe to run
        # concurrently with another process holding the lock.
        if repo.get("check_gpg_signature") and repo.get("gpg_key_url"):
            from ..lib.gpg import GpgKeyError, prepare_keyring
            gpg_key_url: str = repo.get("gpg_key_url", "")
            gpg_key_path_cfg: str = repo.get("gpg_key_path", "")
            if gpg_key_path_cfg:
                keyring_path = Path(cfg.repo_root) / gpg_key_path_cfg
            else:
                keyring_path = (
                    Path(cfg.mirror_root) / "mirror-dedupe" / name / "keyring.gpg"
                )
            try:
                prepare_keyring(gpg_key_url, keyring_path, cfg.connect_timeout)
                rv.gpg_keyring_path = str(keyring_path)
            except GpgKeyError as e:
                log(f"[{name}] GPG key unavailable - skipping sync: {e.message}",
                    level="ERROR")
                return

        try:
            with RepoLock(cfg.mirror_root, name):
                # Inventory loaded inside the lock so it reflects current disk
                # state at the moment this process owns the repo, not before.
                rv.inv = Inventory.from_path_file(
                    f"/tmp/mirror-dedupe/{dest_name}.paths",
                    rv.pool_inv,
                    dest_name,
                    cfg.repo_root,
                )
                log(f"Syncing repo '{name}' to '{repo.get('dest', '')}'", level="INFO")
                config = repo.get("params")
                if config is None:
                    config = {}
                    repo["params"] = config
                with concurrent.futures.ThreadPoolExecutor(
                    max_workers=workers,
                    initializer=self._make_worker_init(name),
                ) as pool:
                    repo.sync(pool=pool, config=config)
        except TimeoutError:
            log(f"[{name}] Skipping - sync already running", level="WARN")
        except RepoAbortError as e:
            log(f"[{name}] Sync aborted: {e.reason}", level="WARN")

    def _print_summary(self) -> None:
        ## @brief Print a cross-repo sync summary table to stdout.
        ##
        ## Delegates to ``stats.print_summary_table()``.
        ##
        ## @return None
        from ..lib.datetimeutils import fmt_datetime
        from ..stats import format_row, print_summary_table

        rows: List[Dict[str, str]] = []
        for repo in self:
            s = repo.stats()
            rows.append(format_row(s, repo.get("name", "?")))

        if not rows:
            return

        print_summary_table(
            rows,
            session_start=fmt_datetime(self.session_start),
            session_end=fmt_datetime(self.session_end),
            session_elapsed=self.session_elapsed,
        )

    @staticmethod
    def _get_peak_rss_mb() -> int:
        ## @brief Return peak RSS in MB, or 0 if unavailable.
        ##
        ## macOS ``ru_maxrss`` is in bytes; Linux is in KB.
        ##
        ## @return Peak RSS in MB as an integer.
        try:
            import resource
            rss_raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            if platform.system() == "Darwin":
                return rss_raw // (1024 * 1024)
            return rss_raw // 1024
        except (ImportError, AttributeError):
            return 0

    @staticmethod
    def _print_rss() -> None:
        ## @brief Print peak RSS to stdout.
        ## @return None
        peak_rss = Repos._get_peak_rss_mb()
        if peak_rss:
            print(f"Peak RSS: {peak_rss}MB")
