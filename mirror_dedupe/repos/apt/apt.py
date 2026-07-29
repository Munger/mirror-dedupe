## @file apt.py
##
## @brief APT Repo implementation.
##
## Provides the concrete ``Apt`` Repo subclass that discovers suites
## under ``/dists/`` and populates the schema tree recursively via
## ``on_parse()``.  Also handles the ``is_this_yours()`` probe and
## registration in the global registry.
##
## @copyright Copyright (c) 2026 Tim Hosking
## @see https://github.com/munger
## @par Licence: MIT


from typing import Any, Dict, List, Optional, cast
from fnmatch import fnmatch

from mirror_dedupe import schema as Schema
from mirror_dedupe.lib.exceptions import ExceptionMsg
from mirror_dedupe.lib.html_helpers import build_url
from mirror_dedupe.lib.log import log
from mirror_dedupe.schema.mdnode import MDNode as Node
from .discovery import _iter_href_names, discover_distribution_paths, probe_any_suite
from .distributions import DistributionsParser


class Apt(Schema.Repo):
    ## @brief APT Repo implementation and its parser helpers.
    ##
    ## Discovers suites under ``/dists/`` via HTML BFS or codename
    ## fallback, fetches Release files, parses index metadata, and
    ## populates the full schema tree (distributions, suites,
    ## components, architectures).

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

        return cast("Apt", cls.from_snapshot(snapshot))

    def on_parse(self, *, config: Optional[Dict[str, Any]] = None) -> None:
        ## @brief Discover distributions and populate suites.
        ##
        ## Sets ``self.vars``, discovers distributions via
        ## ``DistributionsParser``, then deduplicates suites.  The base
        ## ``Node.parse()`` recursion walks into each distribution's
        ## child tree (Release -> Indices).  Components and architectures
        ## are computed lazily via properties that aggregate from parsed
        ## distribution metadata.
        ##
        ## @param config  Optional config dict (suite/arch/component filters).
        ## @return None

        log(f"[apt] parsing repo class: {type(self).__name__}", level="DEBUG")

        # Constants that drive where Release/anchor files are found under dists/
        self.vars = Schema.Vars(
            index_root=Apt.INDEX_ROOT_DIR,
            anchor_filename=Apt.INDEX_ANCHOR_FILENAME,
            signature_extension=Apt.SIGNATURE_EXTENSION,
        )

        # Suite discovery: explicit --distribution candidates beat auto BFS beat codename fallback
        candidates = getattr(self, "distribution_candidates", None)
        self.distributions = DistributionsParser(self, candidates=candidates).parse()

        # Architecture/component filters from repo.params - stored for
        # child Distribution/Release nodes to consume during their parse.
        p = self.params or {}
        self._arch_filter = p.get("architectures")
        self._comp_filter = p.get("components")
        self._exclude_packages = p.get("exclude_packages")
        self._exclude_paths = p.get("exclude_paths")

        # After discovery, filter upstreams to only those that actually served
        # distributions.  This ensures generated configs only contain working
        # URLs, ready for future round-robin sync.
        if self.distributions:
            used_upstreams: set[str] = set()
            for dist in self.distributions:
                used_upstreams.add(str(dist.upstream))
            filtered = Schema.Upstreams()
            for u in self.upstreams:
                if u.url in used_upstreams:
                    filtered.append(u)
            self.upstreams = filtered
            self["upstream_idx"] = 0
            if filtered:
                self["uri"] = filtered[0].url

        # Deduplicate suites: distribution names may include a /pocket suffix
        self.suites = Schema.Suites()
        seen_suites = set()
        for dist in self.distributions:
            suite_name = str(dist.name).split("/", 1)[0]
            if suite_name and suite_name not in seen_suites:
                seen_suites.add(suite_name)
                self.suites.append(Schema.Suite(name=suite_name))

    def _build_sync_tree(self, *, config: Optional[Dict[str, Any]] = None) -> None:
        ## @brief Construct the distribution tree from ``self.params``.
        ##
        ## Reads suite names from ``self.params["suites"]``, creates
        ## ``Distribution`` nodes with their child ``Release`` nodes
        ## directly (no upstream probing - the Release will stream-parse
        ## into Index children after its download completes).
        ##
        ## @param config  Optional configuration dict (unused, read from
        ##                ``self.params`` instead).
        ## @return None

        self.vars = Schema.Vars(
            index_root=Apt.INDEX_ROOT_DIR,
            anchor_filename=Apt.INDEX_ANCHOR_FILENAME,
            signature_extension=Apt.SIGNATURE_EXTENSION,
        )
        p = self.params or {}
        self._arch_filter = p.get("architectures")
        self._comp_filter = p.get("components")
        self._exclude_packages = p.get("exclude_packages")
        self._exclude_paths = p.get("exclude_paths")
        anchor_fn: str = p.get("anchor_filename") or "Release"
        suite_anchor_exceptions: dict = p.get("suite_anchor_exceptions", {})
        suites: List[str] = p.get("suites", [])

        has_glob = any(any(c in s for c in ('*', '?', '[')) for s in suites)
        if has_glob:
            base_uri = self.get("uri") or ""
            globs = [s for s in suites if any(c in s for c in ('*', '?', '['))]
            explicit = [s for s in suites if s not in globs]
            shallow_globs = [g for g in globs if "/" not in g]
            if shallow_globs:
                def _top_level_filter(name: str) -> bool:
                    return any(fnmatch(name, g) for g in shallow_globs) or name in explicit
                discovered = discover_distribution_paths(
                    base_uri, top_level_filter=_top_level_filter,
                )
            else:
                discovered = discover_distribution_paths(base_uri)
            discovered_names = [path for path, _upstream, _anchor in discovered]
            resolved: List[str] = []
            for s in suites:
                if any(c in s for c in ('*', '?', '[')):
                    matches = [d for d in discovered_names if fnmatch(d, s)]
                    if matches:
                        resolved.extend(sorted(matches))
                    else:
                        log(f"[apt] glob '{s}' matched nothing upstream", level="WARN")
                else:
                    resolved.append(s)
            suites = resolved

        if not suites:
            raise ExceptionMsg(0,
                "Apt._build_sync_tree: no suites configured in repo.params",
            )
        self.distributions = Schema.Distributions()
        self.suites = Schema.Suites()
        from .distribution import Distribution
        from .release import Release as AptRelease
        dest = self.get("dest", "")
        base_uri = self.get("uri") or ""
        for suite_name in suites:
            suite_dir = f"{dest}/dists/{suite_name}" if dest else f"dists/{suite_name}"
            dir_url = build_url(base_uri, "dists", suite_name)
            this_anchor = suite_anchor_exceptions.get(suite_name, anchor_fn)
            anchor_url = f"{dir_url}/{this_anchor}"

            dist = Distribution(
                url=anchor_url, upstream=base_uri, name=suite_name,
            )
            dist._repo = self
            dist._repo_vars = self._repo_vars

            anchor = AptRelease(
                url=anchor_url,
                upstream=base_uri,
                suite=suite_name,
                dest=dest,
                filename=this_anchor,
            )
            anchor._arch_filter = self._arch_filter
            anchor._comp_filter = self._comp_filter
            anchor._exclude_packages = self._exclude_packages
            anchor._exclude_paths = self._exclude_paths
            anchor._repo_vars = self._repo_vars

            # Assign anchor to the correct slot; companions go in the other slots.
            if this_anchor == "InRelease":
                dist.inrelease = anchor
                dist.release = cast(AptRelease, Schema.Node({
                    "uri": f"{dir_url}/Release",
                    "path": f"{suite_dir}/Release",
                    "optional": True,
                }))
                dist.release._repo_vars = self._repo_vars
            else:
                dist.release = anchor
                dist.inrelease = cast(AptRelease, Schema.Node({
                    "uri": f"{dir_url}/InRelease",
                    "path": f"{suite_dir}/InRelease",
                    "optional": True,
                }))
                dist.inrelease._repo_vars = self._repo_vars

            dist.release_gpg = Schema.Node({
                "uri": f"{dir_url}/Release.gpg",
                "path": f"{suite_dir}/Release.gpg",
                "optional": True,
            })
            dist.release_gpg._repo_vars = self._repo_vars

            self.distributions.append(dist)
            self.suites.append(Schema.Suite(name=suite_name))

    # --- computed properties ----------------------------------------------

    @property
    def architectures(self) -> Schema.Architectures:
        ## @brief Aggregate all unique architectures across distributions.
        ##
        ## Walks each distribution's ``metadata.architectures`` and collects
        ## unique entries.  Returns an empty ``Architectures`` NodeList if
        ## no distribution has metadata.
        ##
        ## @return NodeList of Architecture nodes.
        result = Schema.Architectures()
        seen = set()
        for dist in self.distributions:
            md = dist.get("metadata")
            if not md:
                continue
            for arch in getattr(md, "architectures", []):
                name = getattr(arch, "name", None)
                if name and name not in seen:
                    seen.add(name)
                    result.append(arch)
        return result

    @architectures.setter
    def architectures(self, value: Schema.Architectures) -> None:
        ## @brief Set the aggregated architectures override.
        ## @param value  ``Architectures`` NodeList to store.
        ## @return None
        self._architectures = value

    @property
    def components(self) -> Schema.Components:
        ## @brief Aggregate all unique components across distributions.
        ##
        ## Walks each distribution's ``metadata.components`` and collects
        ## unique entries.  Returns an empty ``Components`` NodeList if
        ## no distribution has metadata.
        ##
        ## @return NodeList of Component nodes.
        result = Schema.Components()
        seen = set()
        for dist in self.distributions:
            md = dist.get("metadata")
            if not md:
                continue
            for comp in getattr(md, "components", []):
                name = getattr(comp, "name", None)
                if name and name not in seen:
                    seen.add(name)
                    result.append(comp)
        return result

    @components.setter
    def components(self, value: Schema.Components) -> None:
        ## @brief Set the internally-cached component collection.
        ## @param value  ``Components`` instance to store.
        ## @return None
        self._components = value

    @classmethod
    def is_this_yours(cls, upstream: str, extra_suites: Optional[list[str]] = None) -> bool:
        ## @brief Lightweight check: does this upstream look like an APT repo?
        ##
        ## Tries in order:
        ##  1. HTML directory listing at ``/dists/`` - cheap existence check
        ##  2. Child prefix resolution - ``/dists/`` under a subdirectory
        ##  3. Codename probing - short-circuits on first hit
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

        # 3. Codename fallback (one-hit short-circuit) with distribution_candidates
        try:
            return probe_any_suite(
                upstream,
                index_root=cls.INDEX_ROOT_DIR,
                anchor=cls.INDEX_ANCHOR_FILENAME,
                extra_suites=extra_suites,
            )
        except Exception:
            return False

Schema.Repo.register(Apt)
