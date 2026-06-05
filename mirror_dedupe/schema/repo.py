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
import os
from pathlib import Path
from dataclasses import dataclass
from typing import Any, ClassVar, Dict, List, Optional, Type, TypeVar

from .node import Node, NodeList
from ..schema.architecture import Architecture, Architectures
from ..schema.component import Component, Components
from ..schema.suite import Suite, Suites
from ..schema.distribution import Distribution, Distributions
from ..schema.index import Index, Indices
from ..schema.release import Release, Releases
from ..schema.vars import Vars
from ..schema.upstream import Upstream, Upstreams


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
        ## @return A copy of the registered types list.

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
                self._sync_content(pool, config=config)
                self._sweep_stale()
        finally:
            cfg._sync_mode = False

    def _sync_content(
        self,
        pool: concurrent.futures.ThreadPoolExecutor,
        config: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, int]:
        ## @brief Submit all syncable nodes to the repo's download pool.
        ##
        ## Iterates ``_tree_iter()`` and submits every node that has both
        ## ``uri`` and ``path`` to *pool* for parallel download via
        ## ``Node.sync()``.  Stores statistics in ``_sync_stats``.
        ##
        ## @param pool    Shared ``ThreadPoolExecutor`` for parallel downloads.
        ## @param config  Optional configuration dict forwarded to each
        ##                ``Node.sync()`` call (e.g. ``ipv6_ok``, ``timeout``).
        ## @return Dict with keys ``ok``, ``skipped``, ``errors``.

        futures: Dict[concurrent.futures.Future, Node] = {}
        for node in self._tree_iter():
            if node.get("uri") and node.get("path"):
                future = pool.submit(node.sync, config=config)
                futures[future] = node

        stats: Dict[str, int] = {"ok": 0, "skipped": 0, "errors": 0}
        for future in concurrent.futures.as_completed(futures):
            node = futures[future]
            try:
                result = future.result()
                if result:
                    stats["ok"] += 1
                else:
                    stats["skipped"] += 1
            except Exception as e:
                stats["errors"] += 1
                from ..lib.log import log
                log(
                    f"  Sync failed for {node.get('uri', 'unknown')}: {e}",
                    level="ERROR",
                )

        self._sync_stats = stats
        return stats

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


class Repos(NodeList[Repo]):
    ## @brief Container for Repo instances.
    ##
    ## This is just a plain list of ``Repo`` nodes.  Any schema or
    ## metadata about the collection itself lives either on the individual
    ## ``Repo`` instances or on the owner of this list, not on this class.
    pass
