## @file apt.py
##
## @brief APT Repo implementation and parser.
##
## Provides the concrete ``Apt`` Repo subclass with a parser that
## discovers suites under ``/dists/``, fetches Release files, and
## populates the schema tree.  Also handles the ``is_this_yours()``
## probe and registration in the global registry.
##
## @copyright Copyright (c) 2026 Tim Hosking
## @see https://github.com/munger
## @par Licence: MIT

from __future__ import annotations

from typing import Any, Dict, List

from mirror_dedupe import schema as Schema
from mirror_dedupe.lib.html_helpers import build_url
from .distributions import DistributionsParser
from .IndexFetcher import IndexFetcher
from .release import Release
from .utils import discover_distribution_paths


class Apt(Schema.Repo):
    ## @brief APT Repo implementation and its parser helpers.

    REPO_TYPE = "apt"

    INDEX_ROOT_DIR = "dists"
    INDEX_ANCHOR_FILENAME = "Release"
    SIGNATURE_EXTENSION = ".gpg"

    @classmethod
    def restore(cls, snapshot: Dict[str, Any]) -> "Apt":
        ## @brief Restore a fully functional Apt Repo from a snapshot.
        ##
        ## Delegates data/tree reconstruction to ``Repo.from_snapshot``,
        ## which uses Node-level metadata to restore all child collections.
        ## HTTP wiring can be provided lazily based on the restored
        ## network config.
        ##
        ## @param snapshot  Plain dict from an earlier ``snapshot()`` call.
        ## @return A reconstructed Apt instance.

        return cls.from_snapshot(snapshot, http_client=None)

    class Parser(Schema.Repo.Parser):
        ## @brief Concrete APT parser bound to an Apt Repo instance.

        def parse(self):
            ## @brief Parse an APT-style upstream and return the complete Apt repo.
            ##
            ## Performs suite discovery under ``/dists/``, fetches Release
            ## files, parses them, and populates ``repo.distributions``
            ## accordingly.
            ##
            ## @return The populated Apt Repo instance.

            repo = self.repo

            import sys

            print(f"[apt] parsing repo class: {type(repo).__name__}", file=sys.stderr)

            repo.vars = Schema.Vars(
                index_root=Apt.INDEX_ROOT_DIR,
                anchor_filename=Apt.INDEX_ANCHOR_FILENAME,
                signature_extension=Apt.SIGNATURE_EXTENSION,
            )

            candidates = getattr(repo, "dist_candidates", None)
            repo.distributions = DistributionsParser(repo, candidates=candidates).parse()

            for dist in repo.distributions:
                name = str(dist.name)
                if not name:
                    continue
                upstreams_list = [u.url for u in repo.upstreams if u.url]
                upstream_idx = repo.upstream_idx
                upstream_url = upstreams_list[upstream_idx] if upstreams_list else ""
                url = build_url(upstream_url, repo.INDEX_ROOT_DIR, name, repo.INDEX_ANCHOR_FILENAME)
                release = Release(
                    url=url,
                    http_client=repo.http,
                    upstream=upstream_url,
                    suite=name,
                ).parse()

                repo.releases.append(release)

                indices = getattr(release, "indices", None)
                if indices:
                    repo.indices.extend(indices)

            repo.suites = Schema.Suites()
            seen_suites = set()
            for dist in repo.distributions:
                suite_name = str(dist.name).split("/", 1)[0]
                if suite_name and suite_name not in seen_suites:
                    seen_suites.add(suite_name)
                    repo.suites.append(Schema.Suite(name=suite_name))

            repo.components = Schema.Components()
            repo.architectures = Schema.Architectures()
            seen_components = set()
            seen_arches = set()

            for dist in repo.distributions:
                md = dist.get("metadata")
                if not md:
                    continue

                for comp in getattr(md, "components", []):
                    name = getattr(comp, "name", None)
                    if name and name not in seen_components:
                        seen_components.add(name)
                        repo.components.append(comp)

                for arch in getattr(md, "architectures", []):
                    name = getattr(arch, "name", None)
                    if name and name not in seen_arches:
                        seen_arches.add(name)
                        repo.architectures.append(arch)

            return repo

    # --- Repo hooks ---------------------------------------------------

    @classmethod
    def is_this_yours(cls, upstream: str, http_client: Any) -> bool:
        ## @brief Heuristic check: does this upstream look like an APT repo?
        ##
        ## Delegates to ``discover_distribution_paths``, which walks
        ## ``/dists`` and looks for any ``dists/<path>/Release`` that
        ## passes the ``looks_like_release`` heuristic.  Returns True if
        ## at least one distribution path is discovered.
        ##
        ## @param upstream     Upstream URL to probe.
        ## @param http_client  HTTPClient for fetching.
        ## @return True if the upstream appears to be APT.

        try:
            paths = discover_distribution_paths(
                upstream,
                http_client,
                index_root=cls.INDEX_ROOT_DIR,
                anchor=cls.INDEX_ANCHOR_FILENAME,
                max_depth=3,
            )
        except Exception:
            return False

        return bool(paths)

    def make_parser(self) -> "Repo.Parser":
        ## @brief Return an ``Apt.Parser`` bound to this Repo instance.
        ## @return An Apt.Parser instance.

        return Apt.Parser(self)

    # --- sync --------------------------------------------------------------

    def sync(self, pool_path: str) -> None:
        ## @brief Synchronise this Apt repo into the shared pool.
        ##
        ## Parses the repo if not already parsed, then iterates all
        ## discovered releases and fetches their indices (Packages.gz,
        ## Sources.gz, etc.) via IndexFetcher.  Each index is routed
        ## through PoolFile for content-addressed storage — identical
        ## content across releases is stored once by hash in the pool
        ## and hardlinked into each repo path that needs it.
        ##
        ## The *pool_path* parameter is accepted for API compatibility but
        ## the actual pool location is read from Config; all storage
        ## operations go through PoolFile which resolves the pool root
        ## internally.
        ##
        ## @param pool_path  Ignored; pool path is read from Config.

        if not self.releases:
            self.parse()
        fetcher = IndexFetcher()
        for release in self.releases:
            upstream_base = getattr(release, "upstream", None)
            if not upstream_base:
                continue
            indices = getattr(release, "indices", None)
            if not indices:
                continue
            fetcher.fetch_all(release, upstream_base)


Schema.Repo.register(Apt)
