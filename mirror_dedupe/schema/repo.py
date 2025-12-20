"""Repo abstractions and registry.

A RepoType represents a family of repositories that share parsing and
sync behaviour (e.g. APT, Yum). Each concrete RepoType subclass owns
its own Parser implementation and can be auto-detected via a
lightweight is_this_yours() probe.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, ClassVar, Dict, List, Type, TypeVar

from .node import Node, NodeList
from mirror_dedupe.lib.http_client import HTTPClient
from ..schema.architecture import Architecture, Architectures
from ..schema.component import Component, Components
from ..schema.suite import Suite, Suites
from ..schema.distribution import Distribution, Distributions
from ..schema.index import Index, Indices
from ..schema.release import Release, Releases
from ..schema.vars import Vars
from ..schema.network import NetworkConfig


@dataclass
class Repo(Node):
    """Root Node for repo-type-specific ecosystems (APT, Yum, etc.)."""

    REPO_TYPE: ClassVar[str] = "abstract"

    # Registry of concrete Repo implementations.
    _registry: ClassVar[List[Type["Repo"]]] = []

    http: Any

    # Minimal structural metadata so the Node-level helpers can rebuild
    # child collections generically from snapshots without each caller
    # having to list them explicitly.
    _list_fields: ClassVar[Dict[str, tuple[Type[NodeList], Type[Node]]]] = {
        "distributions": (Distributions, Distribution),
        "architectures": (Architectures, Architecture),
        "components": (Components, Component),
        "suites": (Suites, Suite),
        "indices": (Indices, Index),
        "releases": (Releases, Release),
    }

    # Single-node children that should be restored via Node._restore_children.
    _node_fields: ClassVar[Dict[str, Type[Node]]] = {
        "vars": Vars,
        "network": NetworkConfig,
    }

    def __init__(
        self,
        *,
        upstream: str,
        http_client: Any,
        name: str = "",
        repo_type: str = "unknown",
        ipv6_ok: bool = True,
        sync_method: str | None = None,
        gpg_key_url: str | None = None,
        alt_upstreams: list[str] | None = None,
        selected_distributions: list[str] | None = None,
        selected_architectures: list[str] | None = None,
        selected_components: list[str] | None = None,
    ) -> None:
        """Initialise a Repo root node and bind an HTTP client.

        The Repo itself is a Node whose payload contains all scalar repo
        metadata (upstream, repo_type, IPv6 flags, etc.) plus any
        child nodes (Distributions, Vars, etc.) attached by parsers.
        """

        # Build the Repo payload explicitly, mirroring Distribution.__init__.
        data: Dict[str, Any] = {
            "upstream": upstream,
            "name": name,
            "repo_type": repo_type,
            "ipv6_ok": ipv6_ok,
            "sync_method": sync_method,
            "gpg_key_url": gpg_key_url,
            # Always materialise alt_upstreams as a list so callers and
            # snapshots can rely on its presence without getattr() or
            # None checks.
            "alt_upstreams": alt_upstreams or [],
            "selected_distributions": selected_distributions or [],
            "selected_architectures": selected_architectures or [],
            "selected_components": selected_components or [],
        }

        # Initialise the underlying Node payload with scalar fields only.
        super().__init__(data)

        # Runtime HTTP client bound to this repo (not part of the payload).
        self.http = http_client

        # Per-repo network configuration; parsers or callers can override
        # this later, but we seed a default from the ipv6_ok hint so that
        # restore() can reconstruct equivalent behaviour from snapshots.
        self.network = NetworkConfig(ipv6_ok=ipv6_ok)

        # Initialise empty schema collections; parsers can populate or
        # replace these. Attribute assignment routes into the mapping via
        # Node.__setattr__, so ``self.distributions`` and
        # ``self["distributions"]`` remain in sync.
        self.distributions = Distributions()
        self.architectures = Architectures()
        self.components = Components()
        self.suites = Suites()
        self.indices = Indices()
        self.releases = Releases()

    # --- registry helpers -------------------------------------------------

    @classmethod
    def register(cls, repo_cls: Type["Repo"]) -> None:
        """Register a concrete Repo subclass for later discovery."""

        cls._registry.append(repo_cls)

    @classmethod
    def all_types(cls) -> List[Type["Repo"]]:
        """Return the list of registered Repo classes."""

        return list(cls._registry)

    # --- detection --------------------------------------------------------

    @classmethod
    @abstractmethod
    def is_this_yours(cls, upstream: str, http_client: Any) -> bool:
        """Lightweight probe: does this upstream look like this repo type?"""

        raise NotImplementedError

    # --- parser factory ---------------------------------------------------

    @abstractmethod
    def make_parser(self) -> "Repo.Parser":
        """Return a Parser instance bound to this Repo."""

        raise NotImplementedError

    class Parser(ABC):
        """Base parser bound to a Repo instance.

        Subclasses get convenient access to self.repo and self.http.
        """

        def __init__(self, rt: "Repo") -> None:
            # The concrete Repo instance this parser operates on.
            self.repo = rt

        @abstractmethod
        def parse(self) -> Any:
            """Inspect the upstream and populate the bound repo data object."""

            raise NotImplementedError

    # --- selection helpers ------------------------------------------------

    @classmethod
    def get_type_for_urls(
        cls,
        repo: Any,
        urls: list[str],
        http_client: Any,
    ) -> tuple["Type[Repo] | None", str | None]:
        """Select an appropriate Repo class for this repo/URL set.

        Behaviour mirrors the previous Parser.get_parser_for_url helper
        but extended to support multiple candidate upstream URLs:

        - If repo["repo_type"] is unset/"unknown", run is_this_yours()
          for each registered RepoType across all URLs until one claims
          a URL.
        - If repo["repo_type"] is set, only probe the matching
          RepoType across all URLs.

        Returns a tuple of (RepoClass | None, url_used | None).
        """

        rt_cls: Type[Repo] | None = None
        used_url: str | None = None
        types = cls.all_types()

        # Normalise and de-duplicate URL list while preserving order.
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
            # Auto-detect: walk Repo types outer, URLs inner so type
            # priority follows registration order.
            for t in types:
                for url in ordered_urls:
                    if t.is_this_yours(url, http_client):
                        rt_cls = t
                        used_url = url
                        repo["repo_type"] = getattr(t, "REPO_TYPE", "unknown")
                        break
                if rt_cls is not None:
                    break
        else:
            # Fixed type: only probe the matching RepoType across all
            # candidate URLs.
            for t in types:
                if getattr(t, "REPO_TYPE", None) == repo_type_name:
                    for url in ordered_urls:
                        if t.is_this_yours(url, http_client):
                            rt_cls = t
                            used_url = url
                            break
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
        alt_upstreams: list[str] | None = None,
    ) -> "Repo":
        """Construct a Repo instance bound to a URL and HTTP client.

        This is the primary entry point for HTTP-based discovery. It
        initialises an HTTPClient honouring any ipv6_ok hint, selects an
        appropriate concrete Repo subclass via the get_type_for_url
        registry helper, and returns an instance bound to both the
        upstream and HTTP client.
        """
        # Seed initial scalar config for repo detection.
        data: Dict[str, Any] = {"upstream": upstream}
        if repo_type is not None:
            data["repo_type"] = repo_type
        if ipv6_ok is not None:
            data["ipv6_ok"] = ipv6_ok

        # Propagate any alternate upstreams into the detection payload so
        # get_type_for_url can consider them when running is_this_yours
        # across registered Repo types.
        if alt_upstreams is not None:
            data["alt_upstreams"] = alt_upstreams

        # Seed an initial sync_method hint based on the upstream scheme so
        # downstream code has a sensible default until a more specific
        # method (e.g. rsync) is discovered.
        if not data.get("sync_method"):
            if upstream.startswith("https://"):
                data["sync_method"] = "https"
            elif upstream.startswith("http://"):
                data["sync_method"] = "http"

        http = HTTPClient(ipv6_ok=bool(data.get("ipv6_ok", True)))

        # When alternate upstreams are provided, attempt detection across
        # the full set in a deterministic order while keeping ``upstream``
        # as the canonical primary in the Repo payload.
        urls: list[str] = [upstream, *(alt_upstreams or [])]
        rt_cls, _used_url = cls.get_type_for_urls(data, urls, http)
        if rt_cls is None:
            rt_cls = cls

        # Pass the discovered scalar config through to the Repo. ``data``
        # already contains an ``upstream`` key, so we do not pass it
        # twice.
        return rt_cls(http_client=http, **data)

    # --- high-level instance helpers ---------------------------------------

    def parse(self) -> "Repo":
        """Run repo-type-specific parsing and return this Repo.

        This uses the concrete Repo implementation's make_parser()
        factory to construct a parser and run it against the bound
        upstream, mutating this Repo instance in place.
        """

        parser = self.make_parser()
        parser.parse()
        return self

    # --- upstream helpers ----------------------------------------------------

    def iter_upstreams(self) -> list[str]:
        """Return the primary upstream followed by any alternates.

        The first element is always ``self.upstream``. Any URLs recorded
        in ``self.alt_upstreams`` are appended in order. Callers can use
        this to try multiple discovery/sync endpoints while keeping a
        single source of truth for upstream topology on the Repo.
        """

        prim = str(self.get("upstream", ""))
        alts = self.get("alt_upstreams", []) or []
        urls: list[str] = []
        if prim:
            urls.append(prim)
        for u in alts:
            # Avoid trivial duplication if alt_upstreams accidentally
            # contains the primary as well.
            if u and u not in urls:
                urls.append(u)
        return urls

    # --- snapshot / restore helpers ------------------------------------------

    @classmethod
    def from_snapshot(
        cls,
        snapshot: Dict[str, Any],
        http_client: HTTPClient | None = None,
    ) -> "Repo":
        """Rebuild a Repo (and its children) from a plain snapshot.

        This expects *snapshot* to be the result of ``repo.snapshot()`` or an
        equivalent plain-data structure (e.g. loaded from JSON). Runtime
        helpers such as the HTTP client are supplied explicitly via the
        ``http_client`` argument so that callers remain in control of
        networking policy.
        """
        if not isinstance(snapshot, dict):
            raise TypeError(
                f"from_snapshot expected mapping data for {cls.__name__}, "
                f"got {type(snapshot)!r}"
            )

        # Seed the underlying mapping directly from the snapshot payload;
        # any ctor-level sugar lives in ``__init__`` and does not need to
        # run on restore for data-only fields.
        repo = cls._from_payload(snapshot)  # type: ignore[assignment]

        # Ensure a NetworkConfig exists even for legacy snapshots that
        # predate the "network" child node.
        if "network" not in snapshot:
            ipv6_ok = snapshot.get("ipv6_ok", True)
            repo.network = NetworkConfig(ipv6_ok=ipv6_ok)

        # (Re)construct an HTTP client if none was supplied, using the
        # restored network configuration as the single source of truth.
        if http_client is None:
            cfg = getattr(repo, "network", None)
            ipv6_ok = bool(cfg.get("ipv6_ok", True)) if cfg is not None else bool(
                snapshot.get("ipv6_ok", True)
            )
            http_client = HTTPClient(ipv6_ok=ipv6_ok)

        # Reattach runtime wiring that is not part of the payload.
        repo.http = http_client

        # Delegate child reconstruction to the Node-level helper using the
        # structural metadata defined above. This keeps the logic generic
        # while letting subclasses add/override fields by tweaking
        # ``_list_fields`` / ``_node_fields``.
        cls._restore_children(repo, snapshot)

        return repo


class Repos(NodeList[Repo]):
    """Container for Repo instances.

    This is just a plain list of ``Repo`` nodes. Any schema or metadata
    about the collection itself lives either on the individual ``Repo``
    instances or on the owner of this list, not on this class.
    """

