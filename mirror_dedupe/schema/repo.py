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
from ..schema.architecture import Architectures
from ..schema.component import Components
from ..schema.suite import Suites
from ..schema.distribution import Distributions
from ..schema.index import Indices
from ..schema.release import Releases


@dataclass
class Repo(Node):
    """Root Node for repo-type-specific ecosystems (APT, Yum, etc.)."""

    REPO_TYPE: ClassVar[str] = "abstract"

    # Registry of concrete Repo implementations.
    _registry: ClassVar[List[Type["Repo"]]] = []

    http: Any

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
        gpg_key_path: str | None = None,
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
            "gpg_key_path": gpg_key_path,
            "selected_distributions": selected_distributions or [],
            "selected_architectures": selected_architectures or [],
            "selected_components": selected_components or [],
        }

        # Initialise the underlying Node payload with scalar fields only.
        super().__init__(data)

        # Runtime HTTP client bound to this repo (not part of the payload).
        self.http = http_client

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

    # --- selection helper -------------------------------------------------

    @classmethod
    def get_type_for_url(
        cls,
        repo: Any,
        upstream: str,
        http_client: Any,
    ) -> "Type[Repo] | None":
        """Select an appropriate Repo class for this repo/upstream.

        Behaviour mirrors the previous Parser.get_parser_for_url helper:

        - If repo["repo_type"] is unset/"unknown", run is_this_yours() on
          each registered RepoType until one claims the upstream.
        - If repo["repo_type"] is set, pick the matching RepoType by its
          REPO_TYPE constant.
        """

        rt_cls: Type[Repo] | None = None
        types = cls.all_types()

        repo_type_name = repo.get("repo_type")
        if not repo_type_name or repo_type_name == "unknown":
            for t in types:
                if t.is_this_yours(upstream, http_client):
                    rt_cls = t
                    repo["repo_type"] = getattr(t, "REPO_TYPE", "unknown")
                    break
        else:
            for t in types:
                if getattr(t, "REPO_TYPE", None) == repo_type_name:
                    rt_cls = t
                    break

        return rt_cls

    # --- high-level factories ----------------------------------------------

    @classmethod
    def from_url(
        cls,
        upstream: str,
        *,
        ipv6_ok: bool | None = None,
        repo_type: str | None = None,
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

        # Seed an initial sync_method hint based on the upstream scheme so
        # downstream code has a sensible default until a more specific
        # method (e.g. rsync) is discovered.
        if not data.get("sync_method"):
            if upstream.startswith("https://"):
                data["sync_method"] = "https"
            elif upstream.startswith("http://"):
                data["sync_method"] = "http"

        http = HTTPClient(ipv6_ok=bool(data.get("ipv6_ok", True)))

        rt_cls = cls.get_type_for_url(data, upstream, http) or cls

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


class Repos(NodeList[Repo]):
    """Container for Repo instances.

    This is just a plain list of ``Repo`` nodes. Any schema or metadata
    about the collection itself lives either on the individual ``Repo``
    instances or on the owner of this list, not on this class.
    """

