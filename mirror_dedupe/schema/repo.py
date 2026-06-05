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
import signal
import time
import traceback
from pathlib import Path
from dataclasses import dataclass
from typing import Any, ClassVar, Dict, List, Optional, Tuple, Type, TypeVar
from datetime import datetime, timezone

import yaml

from .node import Node, NodeList
from ..schema.architecture import Architecture, Architectures
from ..schema.component import Component, Components
from ..schema.suite import Suite, Suites
from ..schema.distribution import Distribution, Distributions
from ..schema.index import Index, Indices
from ..schema.release import Release, Releases
from ..schema.vars import Vars
from ..schema.upstream import Upstream, Upstreams
from ..lib.log import log
from ..lib.http_download import kill_active_subprocesses


@dataclass
class Repo(Node):
    ## @brief Root Node for repo-type-specific ecosystems (APT, Yum, etc.).
    ##
    ## Each concrete subclass registers itself via the ``_registry``
    ## mechanism and provides ``is_this_yours()``.  ``_children`` is
    ## ``["distributions"]`` — the base ``parse()`` recurses into each
    ## distribution's own child tree.

    _children = ["distributions"]

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
        ## repo metadata (upstream, repo_type, IPv6 flags, etc.) plus any
        ## child nodes (Distributions, Vars, etc.) attached by parsers.
        ##
        ## @param upstream_idx  Index into the Upstreams collection.
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

        self["params"] = {"ipv6_ok": True}

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

    @classmethod
    def probe_known_labels(cls, upstream: str) -> List[str]:
        ## @brief Probe well-known labels for this repo type.
        ##
        ## Returns labels that exist at *upstream*.  Base returns empty.
        ## Subclasses implement with type-specific probes
        ## (e.g. probing ``dists/{name}/Release`` for APT).
        ##
        ## @param upstream  Upstream URL to probe.
        ## @return List of label strings that were confirmed to exist.
        return []

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
        ipv6_ok: bool | None = None,
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
        ## @param ipv6_ok         Whether IPv6 is supported.
        ## @param repo_type       Explicit repo type override (e.g. ``"apt"``).
        ## @param upstream_urls   Additional candidate upstream URLs.
        ## @param dist_candidates Optional dist names from config for probing.
        ## @return A fully wired Repo instance.

        data: Dict[str, Any] = {"upstream_idx": 0}
        if repo_type is not None:
            data["repo_type"] = repo_type

        sync_hint = None
        if upstream.startswith("https://"):
            sync_hint = "https"
        elif upstream.startswith("http://"):
            sync_hint = "http"

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

        for idx, url in enumerate(ordered):
            upstreams.append(
                Upstream(
                    url=url,
                    sync_method=sync_hint
                    if url.split(":", 1)[0] == upstream.split(":", 1)[0]
                    else None,
                )
            )

        repo = rt_cls(upstreams=upstreams, **data)
        if ipv6_ok is not None:
            repo["params"]["ipv6_ok"] = ipv6_ok

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
        ## files, and populates the full node tree.  Pure in-memory —
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
        ## probing), recurses to populate Release/Index metadata (each
        ## node fetching content through the pool when ``_sync_mode`` is
        ## active), and downloads all files via ``_sync_content()``.
        ##
        ## All nodes know their full repo-root-relative ``path`` from
        ## construction time — no deferred path patching is needed.
        ##
        ## When *pool* is None the tree is built but no content is
        ## transferred — useful for testing or dry-run.
        ##
        ## When *pool* is set, every leaf node pushes its own stats to
        ## ``node["stats"]`` during ``sync()``.  ``Repo.stats()``
        ## aggregates these on demand.  Per-repo NDJSON and summary
        ## output are produced by the parent ``Repos`` container.
        ##
        ## @param config  Optional configuration dict (suites, filters, etc.).
        ## @param pool    Shared ``ThreadPoolExecutor`` for parallel downloads.
        ##                When ``None``, content sync and stale sweep are
        ##                skipped.

        from ..config import Config
        cfg = Config.load()
        cfg._sync_mode = True
        try:
            self._build_sync_tree(config=config)
            self.recurse(config=config)
            if pool is not None:
                self._top_stats: Dict[str, Any] = {}
                t0 = time.monotonic()
                ipv6_before = config.get("ipv6_ok", True) if config else True
                self._sync_content(pool, config=config)
                self._top_stats["elapsed"] = time.monotonic() - t0
                self._top_stats["ipv6_fallback"] = (
                    config.get("ipv6_ok") is False and ipv6_before is not False
                ) if config else False
                self._sweep_stale()
        finally:
            cfg._sync_mode = False

    def _sync_content(
        self,
        pool: concurrent.futures.ThreadPoolExecutor,
        config: Optional[Dict[str, Any]] = None,
    ) -> None:
        ## @brief Submit all syncable nodes to the repo's download pool.
        ##
        ## Iterates ``_tree_iter()`` and submits every node that has both
        ## ``uri`` and ``path`` to *pool* for parallel download via
        ## ``Node.sync()``.  Each leaf node pushes ``node["stats"]``
        ## (hit/miss/bytes_tx).  Errors are totalled and stored as
        ## ``self._top_stats["errors"]``.
        ##
        ## @param pool    Shared ``ThreadPoolExecutor`` for parallel downloads.
        ## @param config  Optional configuration dict forwarded to each
        ##                ``Node.sync()`` call (e.g. ``ipv6_ok``, ``timeout``).
        ## @return None

        futures: Dict[concurrent.futures.Future, Node] = {}
        for node in self._tree_iter():
            if node.get("uri") and node.get("path"):
                future = pool.submit(node.sync, config=config)
                futures[future] = node

        errors = 0
        for future in concurrent.futures.as_completed(futures):
            node = futures[future]
            try:
                future.result()
            except Exception as e:
                errors += 1
                if "stats" not in node:
                    node["stats"] = {"hit": 0, "miss": 0, "bytes_tx": 0}
                node["stats"]["error"] = str(e)
                from ..lib.log import log
                log(f"  {e}")

        self._top_stats["errors"] = errors

    def _sweep_stale(self) -> None:
        ## @brief Remove files in the repo destination not in the tree.
        ##
        ## Walks the repo's destination directory on disk and deletes any
        ## file whose path (relative to ``repo_root``) is not present in
        ## the in-memory node tree.  Empty directories are pruned
        ## bottom-up.
        ##
        ## @return None

        from ..config import Config

        cfg = Config.load()
        tree_paths: set[str] = set()
        for node in self._tree_iter():
            p = node.get("path")
            if p:
                tree_paths.add(p)

        dest = self.get("dest", "")
        if not dest:
            return
        dest_path = Path(cfg.repo_root) / dest
        if not dest_path.exists():
            return

        removed = 0
        for f in dest_path.rglob("*"):
            if f.is_file():
                rel = str(f.relative_to(Path(cfg.repo_root)))
                if rel not in tree_paths:
                    f.unlink()
                    removed += 1

        for dirpath, dirnames, filenames in os.walk(str(dest_path), topdown=False):
            try:
                dp = Path(dirpath)
                if not any(dp.iterdir()):
                    dp.rmdir()
            except OSError:
                continue

        if removed:
            from ..lib.log import log
            log(f"Repo sweep: removed {removed} stale files from '{dest}'", level="INFO")

    def stats(self) -> Dict[str, Any]:
        ## @brief Aggregate per-node stats from the full tree into a single dict.
        ##
        ## Walks ``_tree_iter()`` summing leaf counter stats (hit, miss,
        ## bytes_tx), counting files, computing total/deduped bytes from
        ## node metadata, and merging top-level values from
        ## ``_top_stats`` (errors, elapsed, ipv6_fallback).
        ##
        ## @return Stats dict with keys: file_count, total_bytes,
        ##         deduped_bytes, bytes_transferred, pool_hits,
        ##         pool_misses, errors, elapsed, ipv6_fallback.

        file_count = 0
        total_bytes = 0
        deduped_bytes = 0
        bytes_transferred = 0
        pool_hits = 0
        pool_misses = 0
        seen_hashes: set[str] = set()

        for node in self._tree_iter():
            s = node.get("stats")
            if s is not None:
                pool_hits += s.get("hit", 0)
                pool_misses += s.get("miss", 0)
                bytes_transferred += s.get("bytes_tx", 0)

            if node.get("uri") and node.get("path"):
                sz = node.get("size", 0) or 0
                h = node.get("hash", "") or ""
                file_count += 1
                total_bytes += sz
                if h and h not in seen_hashes:
                    seen_hashes.add(h)
                    deduped_bytes += sz

        top = getattr(self, "_top_stats", {}) or {}
        return {
            "file_count": file_count,
            "total_bytes": total_bytes,
            "deduped_bytes": deduped_bytes,
            "bytes_transferred": bytes_transferred,
            "pool_hits": pool_hits,
            "pool_misses": pool_misses,
            "errors": top.get("errors", 0),
            "elapsed": top.get("elapsed", 0.0),
            "ipv6_fallback": top.get("ipv6_fallback", False),
        }

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

    def _build_sync_tree(self, *, config: Optional[Dict[str, Any]] = None) -> None:
        ## @brief Construct the distribution tree from known config.
        ##
        ## Override in concrete subclasses (e.g. ``Apt``) to create
        ## Distribution nodes from configured suite names rather than
        ## discovering them via HTTP probing.
        ##
        ## @param config  Optional configuration dict.
        ## @raise NotImplementedError

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
        repo["params"].setdefault("ipv6_ok", True)

        cls._restore_children(repo, snapshot)

        return repo


class RepoLock:
    ## @brief Per-repo file lock to prevent concurrent syncs.

    FLOCK_DIR = ".mirror-dedupe"
    LOCK_FILE = "sync.lock"

    def __init__(self, repo_root: str, repo_name: str) -> None:
        ## @brief Initialise a RepoLock for *repo_name* under *repo_root*.
        ##
        ## @param repo_root  Root directory for all repos.
        ## @param repo_name  Name of the repo to lock.
        ## @return None
        self.path = Path(repo_root) / self.FLOCK_DIR / repo_name / self.LOCK_FILE
        self.fd: int | None = None

    def acquire(self, timeout: float = 600) -> None:
        ## @brief Acquire an exclusive lock, waiting up to *timeout* seconds.
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
    def session_ts(self) -> str:
        ## @brief Session timestamp for the current batch.
        ##
        ## Set once by ``sync_all()`` and used in every per-repo NDJSON
        ## record so all repos in the same run share the same timestamp.
        ##
        ## @return ISO-8601 timestamp string.
        return self._stats.get("session_ts", "")

    @session_ts.setter
    def session_ts(self, value: str) -> None:
        ## @brief Set the session timestamp.
        ## @param value  ISO-8601 timestamp string.
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
        ## Loads ``{config_dir}/repos-enabled/{name}.conf`` for each name,
        ## parses with ``yaml.safe_load``, and delegates to
        ## ``Repo.from_config()``.
        ##
        ## @param repo_names  List of repo names (``*.conf`` filenames without
        ##                    the extension).
        ## @param config_dir  Override path to the configuration directory.
        ## @return A ``Repos`` instance containing the resolved repos.

        from ..config import Config, DEFAULT_CONFIG_DIR

        cfg = Config.load(config_dir)
        repos_dir = Path(config_dir or cfg._config_dir or DEFAULT_CONFIG_DIR) / "repos-enabled"

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
        ## ``ThreadPoolExecutor`` via ``_sync_one()``, runs
        ## ``_pool_sweep()`` after all repos complete, writes per-repo
        ## NDJSON, and prints a cross-repo summary table.
        ##
        ## @param config_dir  Override path to the configuration directory
        ##                    (passed to ``from_names`` if used externally).
        ## @return None

        from ..config import Config

        cfg = Config.load(config_dir)

        if not self:
            log("No repos to sync", level="WARN")
            return

        max_concurrent = cfg.max_concurrent_syncs

        def _sigint_handler(signum: int, frame: Any) -> None:
            ## @brief SIGINT handler: kill tracked subprocesses and restore default.
            ## @param signum  Signal number.
            ## @param frame   Current stack frame.
            ## @return None
            kill_active_subprocesses()
            signal.signal(signal.SIGINT, signal.SIG_DFL)

        self.session_ts = datetime.now(timezone.utc).isoformat()
        t0 = time.monotonic()
        original_sigint = signal.signal(signal.SIGINT, _sigint_handler)

        try:
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=min(max_concurrent, len(self))
            ) as repo_pool:
                futures = {}
                for repo in self:
                    name = repo.get("name", "unknown")
                    future = repo_pool.submit(self._sync_one, repo, cfg)
                    futures[future] = name

                for future in concurrent.futures.as_completed(futures):
                    name = futures[future]
                    try:
                        future.result()
                    except Exception as e:
                        log(
                            f"Repo '{name}' sync failed: {e}\n{traceback.format_exc()}",
                            level="ERROR",
                        )

            _pool_sweep(cfg.pool_root)

            for repo in self:
                self._write_ndjson(repo)

            self._print_summary()
            self._print_rss()
        finally:
            signal.signal(signal.SIGINT, original_sigint)

    def _sync_one(self, repo: Repo, cfg: "Config") -> None:
        ## @brief Sync a single repo: metadata, packages, stats.
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
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
                repo.sync(pool=pool, config=config)

    def _write_ndjson(self, repo: Repo) -> None:
        ## @brief Append a stats NDJSON record for *repo* to its per-repo file.
        ##
        ## Writes to ``<repo_root>/.mirror-dedupe/<name>/stats.ndjson``.
        ## Computes delta_files and delta_bytes against the previous
        ## record for trend analysis.
        ##
        ## @param repo  The ``Repo`` instance whose stats to record.
        ## @return None
        name = repo.get("name", "")
        if not name:
            return

        from ..config import Config
        cfg = Config.load()
        stats_dir = Path(cfg.repo_root) / ".mirror-dedupe" / name
        stats_dir.mkdir(parents=True, exist_ok=True)
        stats_file = stats_dir / "stats.ndjson"

        s = repo.stats()
        record = {
            "session_ts": self.session_ts,
            "ts": datetime.now(timezone.utc).isoformat(),
            "elapsed": round(s["elapsed"], 2),
            "file_count": s["file_count"],
            "total_bytes": s["total_bytes"],
            "deduped_bytes": s["deduped_bytes"],
            "bytes_transferred": s["bytes_transferred"],
            "errors": s["errors"],
            "pool_hits": s["pool_hits"],
            "pool_misses": s["pool_misses"],
            "ipv6_fallback": s["ipv6_fallback"],
        }

        # Deltas from previous record
        try:
            with open(stats_file) as f:
                for line in f:
                    pass
                prev = json.loads(line) if line else {}
            curr_file_count = record["file_count"]
            prev_file_count = prev.get("file_count", 0)
            curr_bytes = record["total_bytes"]
            prev_bytes = prev.get("total_bytes", 0)
            record["delta_files"] = curr_file_count - prev_file_count
            record["delta_bytes"] = curr_bytes - prev_bytes
        except (OSError, json.JSONDecodeError):
            record["delta_files"] = 0
            record["delta_bytes"] = 0

        with open(stats_file, "a") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _print_summary(self) -> None:
        ## @brief Print a cross-repo sync summary table to stdout.
        ##
        ## Columns: Repo, Files, Total, Deduped, TX, Hit, Miss, Err, Time, IPv6.
        ## A ``Total`` row at the bottom aggregates all repos.
        ##
        ## @return None

        rows: List[Dict[str, str]] = []
        for repo in self:
            s = repo.stats()
            rows.append(self._aggregate_stats(s, repo.get("name", "?")))

        if not rows:
            return

        total_files = sum(int(r["files"].replace(",", "")) for r in rows)
        total_bytes = sum(
            self._parse_fmt(r["total"]) for r in rows
        )
        total_deduped = sum(
            self._parse_fmt(r["deduped"]) for r in rows
        )
        total_tx = sum(
            self._parse_fmt(r["tx"]) for r in rows
        )
        total_hits = sum(int(r["hit"].replace(",", "")) for r in rows)
        total_misses = sum(int(r["miss"].replace(",", "")) for r in rows)
        total_errors = sum(int(r["errors"]) for r in rows)
        max_time = max(
            (float(r["time"].rstrip("s")) for r in rows), default=0.0
        )

        def _fmt(b: int) -> str:
            ## @brief Format byte count as human-readable string.
            ## @param b  Byte count.
            ## @return Formatted string (e.g. ``"450MB"``).
            if b >= 1073741824:
                return f"{b/1073741824:.1f}GB"
            if b >= 1048576:
                return f"{b/1048576:.0f}MB"
            if b >= 1024:
                return f"{b/1024:.0f}KB"
            return f"{b}B"

        def _fmt_int(n: int) -> str:
            ## @brief Format integer with thousands separator.
            ## @param n  Integer to format.
            ## @return Formatted string (e.g. ``"1,234"``).
            return f"{n:,}" if n >= 1000 else str(n)

        def _pad(s: str, width: int) -> str:
            ## @brief Left-pad a string to *width*.
            ## @param s      Input string.
            ## @param width  Minimum width.
            ## @return Left-justified string.
            return s.ljust(width) if len(s) < width else s

        cw = self._col_widths(rows)
        # Ensure idx column is wide enough for the row count
        cw["idx"] = max(cw.get("idx", 3), len(str(len(rows) - 1)) if rows else 1)

        sep = "  ".join("-" * w for w in cw.values())

        print("")
        print("sync summary")
        print(sep)
        header = _pad("#", cw["idx"]).rjust(cw["idx"]) + "  "
        header += _pad("Repo", cw["name"]) + "  "
        header += _pad("Files", cw["files"]).rjust(cw["files"]) + "  "
        header += _pad("Total", cw["total"]).rjust(cw["total"]) + "  "
        header += _pad("Deduped", cw["deduped"]).rjust(cw["deduped"]) + "  "
        header += _pad("TX", cw["tx"]).rjust(cw["tx"]) + "  "
        header += _pad("Hit", cw["hit"]).rjust(cw["hit"]) + "  "
        header += _pad("Miss", cw["miss"]).rjust(cw["miss"]) + "  "
        header += _pad("Err", cw["errors"]).rjust(cw["errors"]) + "  "
        header += _pad("Time", cw["time"]).rjust(cw["time"]) + "  "
        header += _pad("IPv6", cw["ipv6"]).rjust(cw["ipv6"])
        print(header)
        print(sep)

        for ri, r in enumerate(rows):
            line = str(ri).rjust(cw["idx"]) + "  "
            line += _pad(r["name"], cw["name"]) + "  "
            line += r["files"].rjust(cw["files"]) + "  "
            line += r["total"].rjust(cw["total"]) + "  "
            line += r["deduped"].rjust(cw["deduped"]) + "  "
            line += r["tx"].rjust(cw["tx"]) + "  "
            line += r["hit"].rjust(cw["hit"]) + "  "
            line += r["miss"].rjust(cw["miss"]) + "  "
            line += r["errors"].rjust(cw["errors"]) + "  "
            line += r["time"].rjust(cw["time"]) + "  "
            line += r["ipv6"].rjust(cw["ipv6"])
            print(line)

        print(sep)
        total_line = "".rjust(cw["idx"]) + "  "
        total_line += _pad("Total", cw["name"]) + "  "
        total_line += _fmt_int(total_files).rjust(cw["files"]) + "  "
        total_line += _fmt(total_bytes).rjust(cw["total"]) + "  "
        total_line += _fmt(total_deduped).rjust(cw["deduped"]) + "  "
        total_line += _fmt(total_tx).rjust(cw["tx"]) + "  "
        total_line += _fmt_int(total_hits).rjust(cw["hit"]) + "  "
        total_line += _fmt_int(total_misses).rjust(cw["miss"]) + "  "
        total_line += str(total_errors).rjust(cw["errors"]) + "  "
        total_line += f"{max_time:.1f}s".rjust(cw["time"]) + "  "
        total_line += "".rjust(cw["ipv6"])
        print(total_line)
        print("")

    def _aggregate_stats(
        self, s: Dict[str, Any], name: str
    ) -> Dict[str, str]:
        ## @brief Convert a raw stats dict into a display row for the summary table.
        ##
        ## @param s     Stats dict from ``Repo.stats()``.
        ## @param name  Repo name.
        ## @return Dict with formatted string values for each column.

        def _fmt(b: int) -> str:
            ## @brief Format byte count as human-readable string.
            ## @param b  Byte count.
            ## @return Formatted string (e.g. ``"450MB"``).
            if b >= 1073741824:
                return f"{b/1073741824:.1f}GB"
            if b >= 1048576:
                return f"{b/1048576:.0f}MB"
            if b >= 1024:
                return f"{b/1024:.0f}KB"
            return f"{b}B"

        def _fmt_int(n: int) -> str:
            ## @brief Format integer with thousands separator.
            ## @param n  Integer to format.
            ## @return Formatted string (e.g. ``"1,234"``).
            return f"{n:,}" if n >= 1000 else str(n)

        return {
            "name": name,
            "files": _fmt_int(s.get("file_count", 0)),
            "total": _fmt(s.get("total_bytes", 0)),
            "deduped": _fmt(s.get("deduped_bytes", 0)),
            "tx": _fmt(s.get("bytes_transferred", 0)),
            "hit": _fmt_int(s.get("pool_hits", 0)),
            "miss": _fmt_int(s.get("pool_misses", 0)),
            "errors": str(s.get("errors", 0)),
            "time": f"{s.get('elapsed', 0):.1f}s",
            "ipv6": "v4" if s.get("ipv6_fallback") else "",
        }

    @staticmethod
    def _parse_fmt(s: str) -> int:
        ## @brief Parse a human-readable byte string back to an integer.
        ##
        ## @param s  Formatted byte string (e.g. ``"450MB"``, ``"1.2GB"``).
        ## @return Byte count as integer.
        s = s.strip()
        if s.endswith("GB"):
            return int(float(s[:-2]) * 1073741824)
        if s.endswith("MB"):
            return int(float(s[:-2]) * 1048576)
        if s.endswith("KB"):
            return int(float(s[:-2]) * 1024)
        if s.endswith("B") and not any(c in s for c in "GMK"):
            return int(s[:-1])
        return 0

    @staticmethod
    def _col_widths(rows: List[Dict[str, str]]) -> Dict[str, int]:
        ## @brief Compute column widths for the summary table.
        ##
        ## @param rows  List of formatted row dicts.
        ## @return Dict mapping column name to minimum pixel width.
        widths = {"idx": 1, "name": 20, "files": 8, "total": 10, "deduped": 10,
                  "tx": 12, "hit": 8, "miss": 8, "errors": 6, "time": 10, "ipv6": 4}
        for r in rows:
            for k, v in r.items():
                widths[k] = max(widths[k], len(v))
        return widths

    @staticmethod
    def _print_rss() -> None:
        ## @brief Print peak RSS to stdout.
        ##
        ## macOS returns bytes, Linux returns KB.  Both are converted to MB.
        ##
        ## @return None
        try:
            import resource
            rss_raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            if platform.system() == "Darwin":
                peak_rss = rss_raw // (1024 * 1024)
            else:
                peak_rss = rss_raw // 1024
        except (ImportError, AttributeError):
            return
        if peak_rss:
            print(f"Peak RSS: {peak_rss}MB")
