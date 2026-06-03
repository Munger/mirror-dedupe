## @file repo.py
##
## @brief Repo abstraction, registry, and high-level factories.
##
## ``Repo`` is the root node for repo-type-specific ecosystems (APT,
## Yum, etc.).  Each concrete ``Repo`` subclass owns its own Parser
## implementation and can be auto-detected via a lightweight
## ``is_this_yours()`` probe.  ``Repos`` is the corresponding
## ``NodeList`` wrapper.
##
## @copyright Copyright (c) 2026 Tim Hosking
## @see https://github.com/munger
## @par Licence: MIT

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
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
from ..schema.network import NetworkConfig


@dataclass
class Repo(Node):
    ## @brief Root Node for repo-type-specific ecosystems (APT, Yum, etc.).
    ##
    ## Each concrete subclass registers itself via the ``_registry``
    ## mechanism and provides ``is_this_yours()`` and ``make_parser()``.

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
        "network": NetworkConfig,
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

        self.network = NetworkConfig(ipv6_ok=True)
        self["params"] = {}

        self.distributions = Distributions()
        self.architectures = Architectures()
        self.components = Components()
        self.suites = Suites()
        self.indices = Indices()
        self.releases = Releases()

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
    @abstractmethod
    def is_this_yours(cls, upstream: str) -> bool:
        ## @brief Lightweight probe: does this upstream look like this repo type?
        ## @param upstream  Upstream URL to probe.
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

    # --- parser factory ---------------------------------------------------

    @abstractmethod
    def make_parser(self) -> "Repo.Parser":
        ## @brief Return a Parser instance bound to this Repo.
        ## @return A concrete Parser subclass instance.

        raise NotImplementedError

    class Parser(ABC):
        ## @brief Base parser bound to a Repo instance.

        def __init__(self, rt: "Repo") -> None:
            ## @brief Initialise the base Parser.
            ##
            ## @param rt  The concrete Repo instance this parser operates on.
            ## @return None
            self.repo = rt

        @abstractmethod
        def parse(self) -> Any:
            ## @brief Inspect the upstream and populate the bound repo data object.
            ## @return Populated data (structure depends on concrete parser).
            raise NotImplementedError

    # --- content operations ------------------------------------------------

    def sync(self, *, config: Optional[Dict[str, Any]] = None) -> List[Path]:
        ## @brief Sync all releases under this Repo to the pool.
        ##
        ## @param config  Optional sync configuration dict.
        ## @return List of paths written during sync.
        if not Node._sync_enabled:
            return []
        results: List[Path] = []
        for release in self.releases:
            results.extend(release.sync(config=config or self.get("network")))
        return results

    # --- selection helpers ------------------------------------------------

    @classmethod
    def get_type_for_urls(
        cls,
        repo: Any,
        urls: list[str],
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
        ## @param repo  A dict-like object with an optional ``repo_type`` key.
        ## @param urls  List of candidate upstream URLs.
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
    ) -> "Repo":
        ## @brief Construct a Repo instance from a URL.
        ##
        ## This is the primary entry point for HTTP-based discovery.  It
        ## selects an appropriate concrete Repo subclass via the
        ## ``get_type_for_url`` registry helper and returns an instance
        ## bound to the upstream tree.
        ##
        ## @param upstream      Primary upstream URL.
        ## @param ipv6_ok       Whether IPv6 is supported.
        ## @param repo_type     Explicit repo type override (e.g. ``"apt"``).
        ## @param upstream_urls Additional candidate upstream URLs.
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
        rt_cls, _used_url = cls.get_type_for_urls(data, urls)
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
            repo.network = NetworkConfig(ipv6_ok=ipv6_ok)

        return repo

    # --- high-level instance helpers ---------------------------------------

    def parse(self) -> "Repo":
        ## @brief Run repo-type-specific parsing and return this Repo.
        ##
        ## Uses the concrete Repo implementation's ``make_parser()``
        ## factory to construct a parser and run it against the bound
        ## upstream, mutating this Repo instance in place.
        ##
        ## @return This Repo after parsing (for chaining).

        parser = self.make_parser()
        parser.parse()
        return self

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

        if "network" not in snapshot:
            repo.network = NetworkConfig(ipv6_ok=True)

        cls._restore_children(repo, snapshot)

        return repo


class Repos(NodeList[Repo]):
    ## @brief Container for Repo instances.
    ##
    ## This is just a plain list of ``Repo`` nodes.  Any schema or
    ## metadata about the collection itself lives either on the individual
    ## ``Repo`` instances or on the owner of this list, not on this class.
    pass
