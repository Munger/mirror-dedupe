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

from typing import Any, Dict, List, Optional

from mirror_dedupe import schema as Schema
from mirror_dedupe.lib.html_helpers import build_url
from mirror_dedupe.schema.node import Node
from .discovery import _iter_href_names, _release_text_cache, probe_any_suite, probe_fallback_suites
from .distributions import DistributionsParser
from .release import Release


class Apt(Schema.Repo):
    ## @brief APT Repo implementation and its parser helpers.
    ##
    ## Discovers suites under ``/dists/`` via HTML BFS or codename
    ## fallback, fetches Release files, parses index metadata, and
    ## populates the full schema tree (distributions, releases, indices,
    ## suites, components, architectures).

    REPO_TYPE = "apt"
    ## @brief Registered repo type string for ``"apt"``.

    INDEX_ROOT_DIR = "dists"
    ## @brief Upstream directory that holds per-suite sub-directories
    ##        (``/dists/`` for Debian/Ubuntu repositories).

    INDEX_ANCHOR_FILENAME = "Release"
    ## @brief Filename of the per-suite metadata file.

    SIGNATURE_EXTENSION = ".gpg"
    ## @brief File extension appended to ``Release`` for its detached
    ##        GPG signature (``Release.gpg``).

    @classmethod
    def restore(cls, snapshot: Dict[str, Any]) -> "Apt":
        ## @brief Restore a fully functional Apt Repo from a snapshot.
        ##
        ## Delegates data/tree reconstruction to ``Repo.from_snapshot``.
        ##
        ## @param snapshot  Plain dict from an earlier ``snapshot()`` call.
        ## @return A reconstructed Apt instance.

        return cls.from_snapshot(snapshot)

    class Parser(Schema.Repo.Parser):
        ## @brief Concrete APT parser bound to an Apt Repo instance.

        def parse(self):

            repo = self.repo

            import sys

            print(f"[apt] parsing repo class: {type(repo).__name__}", file=sys.stderr)

            # Constants that drive where Release/anchor files are found under dists/
            repo.vars = Schema.Vars(
                index_root=Apt.INDEX_ROOT_DIR,
                anchor_filename=Apt.INDEX_ANCHOR_FILENAME,
                signature_extension=Apt.SIGNATURE_EXTENSION,
            )

            # Suite discovery: explicit --release candidates beat auto BFS beat codename fallback
            candidates = getattr(repo, "dist_candidates", None)
            repo.distributions = DistributionsParser(repo, candidates=candidates).parse()

            upstreams_list = [u.url for u in repo.upstreams if u.url]

            # Fetch and parse a Release file per distribution, collecting indices
            for dist in repo.distributions:
                name = str(dist.name)
                if not name:
                    continue
                parent_upstream = getattr(dist, "upstream", None) or upstreams_list[repo.upstream_idx] if upstreams_list else ""
                url = build_url(parent_upstream, repo.INDEX_ROOT_DIR, name, repo.INDEX_ANCHOR_FILENAME)

                # Reuse Release body already fetched by Distribution.parse()
                cached_text = _release_text_cache.get((parent_upstream, repo.INDEX_ROOT_DIR, name))
                if cached_text is None:
                    print(f"  {name}: fetching Release", file=sys.stderr)
                release = Release(
                    url=url,
                    upstream=parent_upstream,
                    suite=name,
                ).parse(config=repo.get("network"), text=cached_text)

                repo.releases.append(release)

                indices = getattr(release, "indices", None)
                if indices:
                    repo.indices.extend(indices)

            # Deduplicate suites: distribution names may include a /pocket suffix
            repo.suites = Schema.Suites()
            seen_suites = set()
            for dist in repo.distributions:
                suite_name = str(dist.name).split("/", 1)[0]
                if suite_name and suite_name not in seen_suites:
                    seen_suites.add(suite_name)
                    repo.suites.append(Schema.Suite(name=suite_name))

            # Gather unique components and architectures from all distribution metadata
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
    def is_this_yours(cls, upstream: str) -> bool:
        ## @brief Lightweight check: does this upstream look like an APT repo?
        ##
        ## Tries in order:
        ##  1. HTML directory listing at ``/dists/`` — cheap existence check
        ##  2. Child prefix resolution — ``/dists/`` under a subdirectory
        ##  3. Codename probing — short-circuits on first hit
        ##
        ## No per-suite Release probes here; that belongs in the parser.
        ##
        ## @param upstream  Upstream URL to probe.
        ## @return True if the upstream appears to be APT.

        # 1. Check if /dists/ serves a directory listing with any entries
        try:
            dists_url = build_url(upstream, cls.INDEX_ROOT_DIR)
            raw = Node.probe_url(dists_url)
            if raw is not None:
                html = raw.decode("utf-8", errors="replace")
                if any(_iter_href_names(html.splitlines(), dirs_only=True)):
                    return True
                if any(_iter_href_names(html.splitlines())):
                    return True
        except Exception:
            pass

        # 2. Child prefix: /dists/ may sit under a subdirectory (e.g. nodesource)
        try:
            raw = Node.probe_url(upstream)
            if raw is not None:
                html = raw.decode("utf-8", errors="replace")
                for child in _iter_href_names(html.splitlines(), dirs_only=True):
                    child_dists = build_url(upstream, child, cls.INDEX_ROOT_DIR)
                    if Node.probe_url(child_dists) is not None:
                        return True
        except Exception:
            pass

        # 3. Codename fallback (one-hit short-circuit)
        try:
            return probe_any_suite(
                upstream,
                index_root=cls.INDEX_ROOT_DIR,
                anchor=cls.INDEX_ANCHOR_FILENAME,
            )
        except Exception:
            return False

    @classmethod
    def probe_known_labels(cls, upstream: str) -> List[str]:
        ## @brief Probe well-known APT suite names at *upstream*.
        ##
        ## Delegates to ``probe_fallback_suites`` which probes
        ## ``dists/{name}/Release`` for all known suite names and
        ## returns the full confirmed list.
        ##
        ## @param upstream  Upstream URL to probe.
        ## @return Names of suites confirmed to have valid Release files.

        try:
            return probe_fallback_suites(
                upstream,
                index_root=cls.INDEX_ROOT_DIR,
                anchor=cls.INDEX_ANCHOR_FILENAME,
            )
        except Exception:
            return []

    def make_parser(self) -> "Repo.Parser":
        ## @brief Return an ``Apt.Parser`` bound to this Repo instance.
        ## @return An Apt.Parser instance.

        return Apt.Parser(self)

    # --- sync --------------------------------------------------------------

    def sync(self, pool_path: str) -> None:
        ## @brief Synchronise this APT repo by fetching indices and storing them.
        ##
        ## Parses the repo if not already parsed, then stores indices using
        ## the pool at *pool_path*.
        ##
        ## @param pool_path  Path to the content-addressable pool directory.
        ## @param config     Network configuration settings.
        ## @return None
        ## @raise FileNotFoundError  If the pool path does not exist.

        if not self.releases:
            self.parse()
        self.store(config=self.get("network"))


Schema.Repo.register(Apt)
