## @file distributions.py
##
## @brief Distribution discovery across ``/dists/`` for APT repos.
##
## ``DistributionsParser`` walks the upstream index to find all plausible
## distribution paths, then delegates per-path parsing to the
## ``Distribution`` class.  Supports explicit candidate paths for repos
## whose ``/dists/`` directory is not browsable.
##
## @copyright Copyright (c) 2026 Tim Hosking
## @see https://github.com/munger
## @par Licence: MIT


from typing import List, Optional, Tuple

from mirror_dedupe import schema as Schema
from mirror_dedupe.lib.html_helpers import build_url
from mirror_dedupe.lib.log import log
from .discovery import looks_like_release, discover_distribution_paths, probe_fallback_suites
from .distribution import Distribution


class DistributionsParser:
    ## @brief Discover distributions under ``/dists`` and delegate
    ##        per-distribution parsing.

    def __init__(self, repo: "Apt", candidates: Optional[List[str]] = None) -> None:
        ## @brief Initialise the DistributionsParser.
        ## @param repo        The Apt Repo instance to discover for.
        ## @param candidates  Optional explicit distribution paths to probe
        ##                    directly, bypassing HTML discovery.
        ## @return None

        self.repo = repo
        self.upstream_index = int(repo.get("upstream_idx", 0))  # prefer the mirror used during scan
        self._candidates: List[str] = candidates or []

    def parse(self):
        ## @brief Return a ``Distributions`` list discovered under ``/dists``.
        ##
        ## Probes in order:
        ##  1. Explicit ``_candidates`` (``--release`` flag)
        ##  2. HTML directory listing BFS at ``/dists/``
        ##  3. Child prefix resolution (nested ``/dists/``)
        ##  4. Codename-based fallback probing (S3-hosted repos)
        ##
        ## Cached ``repo.params`` from a previous scan can short-circuit
        ## strategies that are known to fail (e.g. ``nobrowse=True`` skips
        ## the HTML BFS entirely).  After discovery, result metadata is
        ## written back into ``repo.params`` for the benefit of the next
        ## scan.
        ##
        ## @return A ``Distributions`` NodeList.

        upstreams_list = [u.url for u in self.repo.upstreams if u.url]
        upstream = upstreams_list[self.upstream_index] if upstreams_list else ""  # consistent mirror avoids skew
        root = self.repo.INDEX_ROOT_DIR
        anchor = self.repo.INDEX_ANCHOR_FILENAME

        cached = self.repo.get("params", {})
        nobrowse = cached.get("nobrowse", False)

        # --- 1. Explicit candidates (highest priority) -------------------

        if self._candidates:
            from collections import Counter
            from mirror_dedupe.schema.mdnode import MDNode as Node
            from .release import strip_pgp_wrapper
            distributions = Schema.Distributions()
            found: List[Tuple[str, str]] = []  # (path, anchor_fn)
            for path in self._candidates:
                suite_anchor = None
                suite_bytes = None

                ir_bytes = Node.probe_url(build_url(upstream, root, path, "InRelease"))
                if ir_bytes:
                    stripped = strip_pgp_wrapper(ir_bytes.decode("utf-8", errors="replace"))
                    if looks_like_release(stripped):
                        suite_anchor = "InRelease"
                        suite_bytes = ir_bytes

                if suite_anchor is None:
                    rel_bytes = Node.probe_url(build_url(upstream, root, path, "Release"))
                    if rel_bytes and looks_like_release(rel_bytes.decode("utf-8", errors="replace")):
                        suite_anchor = "Release"
                        suite_bytes = rel_bytes

                if not suite_anchor:
                    continue

                found.append((path, suite_anchor))
                log(f"  {path}: fetching {suite_anchor}")
                release_url = build_url(upstream, root, path, suite_anchor)
                dist = Distribution(url=release_url, upstream=upstream, name=path)
                dist._cache = suite_bytes
                dist._repo = self.repo
                distributions.append(dist)

            anchor_counts: Counter = Counter(anchor_fn for _, anchor_fn in found)
            mixed = len(anchor_counts) > 1
            default_anchor = "InRelease" if mixed else (anchor_counts.most_common(1)[0][0] if anchor_counts else "Release")
            p = self.repo.setdefault("params", {})
            p["discovery_method"] = "explicit"
            p["anchor_filename"] = default_anchor
            if mixed:
                p["suite_anchor_exceptions"] = {
                    path: anchor_fn for path, anchor_fn in found
                    if anchor_fn != default_anchor
                }
            else:
                p.pop("suite_anchor_exceptions", None)
            p.setdefault("log_colour", "DEFAULT")
            p.setdefault("log_colour_bg", "NONE")
            return distributions

        # --- 2/3. HTML BFS + child prefix resolution --------------------

        upstream_results: List[Tuple[str, str]] = []
        used_fallback = False

        if nobrowse:
            log("[apt] skipping HTML BFS (cached: not browsable)", level="DEBUG")
            used_fallback = True
        else:
            for idx, url in enumerate(upstreams_list):
                upstream_results = discover_distribution_paths(
                    url,
                    index_root=root,
                    anchor=anchor,
                )
                if upstream_results:
                    if idx > 0:
                        log(f"[apt] discovered distributions via alternate upstream {url}", level="DEBUG")
                    break

        # --- 4. Codename fallback probe ---------------------------------

        if not upstream_results:
            log("[apt] HTML discovery found no suites; trying codename fallback", level="DEBUG")
            fallback = probe_fallback_suites(
                upstream,
                index_root=root,
                anchor=anchor,
            )
            if fallback:
                log(f"[apt] codename fallback found: {', '.join(n for n, _ in fallback)}", level="DEBUG")
                upstream_results = [(name, upstream, anchor_fn) for name, anchor_fn in fallback]
                used_fallback = True

        if not upstream_results:
            log("[apt] no distributions discovered under /dists on any upstream; giving up", level="WARN")
            return Schema.Distributions()

        # --- Set discovery params for next scan -------------------------

        params = self.repo.setdefault("params", {})
        if used_fallback:
            params["discovery_method"] = "codename_fallback"
            params["nobrowse"] = True
        else:
            params["discovery_method"] = "html_bfs"
            params["nobrowse"] = False
        params.setdefault("log_colour", "DEFAULT")
        params.setdefault("log_colour_bg", "NONE")

        # --- Derive repo-level anchor (majority) and per-suite exceptions --

        from collections import Counter
        anchor_counts: Counter = Counter(anchor_fn for _, _, anchor_fn in upstream_results)
        mixed = len(anchor_counts) > 1
        default_anchor = "InRelease" if mixed else (anchor_counts.most_common(1)[0][0] if anchor_counts else "Release")
        params["anchor_filename"] = default_anchor

        if mixed:
            params["suite_anchor_exceptions"] = {
                path: anchor_fn
                for path, _, anchor_fn in upstream_results
                if anchor_fn != default_anchor
            }
        else:
            params.pop("suite_anchor_exceptions", None)

        # --- Build Distribution nodes -----------------------------------

        distributions = Schema.Distributions()
        from .discovery import _release_text_cache
        for path, eff_upstream, anchor_fn in upstream_results:
            release_url = build_url(eff_upstream, root, path, anchor_fn)
            dist = Distribution(
                url=release_url,
                upstream=eff_upstream,
                name=path,
            )
            # Pre-cache the full upstream content (PGP wrapper intact) so
            # Distribution.on_parse() does not re-fetch from upstream.
            cached_text = _release_text_cache.get((eff_upstream, root, path))
            if cached_text is not None:
                dist._cache = cached_text.encode("utf-8")
            dist._repo = self.repo
            distributions.append(dist)

        return distributions
