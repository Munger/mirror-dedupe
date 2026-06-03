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

from __future__ import annotations

from typing import List, Optional, Tuple
import sys

from mirror_dedupe import schema as Schema
from mirror_dedupe.lib.html_helpers import build_url
from .discovery import looks_like_release, discover_distribution_paths, probe_fallback_suites, _release_text_cache
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
        self.upstream_index = int(repo.get("upstream_idx", 0))
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
        upstream = upstreams_list[self.upstream_index] if upstreams_list else ""
        net_config = self.repo.get("network")
        root = self.repo.INDEX_ROOT_DIR
        anchor = self.repo.INDEX_ANCHOR_FILENAME

        cached = self.repo.get("params", {})
        nobrowse = cached.get("nobrowse", False)

        # --- 1. Explicit candidates (highest priority) -------------------

        if self._candidates:
            distributions = Schema.Distributions()
            for path in self._candidates:
                release_url = build_url(upstream, root, path, anchor)
                print(f"  {path}: fetching Release", file=sys.stderr)
                from mirror_dedupe.schema.node import Node

                text_bytes = Node.probe_url(release_url, net_config)
                if text_bytes is None:
                    continue
                text = text_bytes.decode("utf-8", errors="replace")
                if not text or not looks_like_release(text):
                    continue

                _release_text_cache[(upstream, root, path)] = text

                dist = Distribution(
                    url=release_url,
                    upstream=upstream,
                    name=path,
                ).parse(config=net_config, text=text)
                distributions.append(dist)

            self.repo.setdefault("params", {})["discovery_method"] = "explicit"
            return distributions

        # --- 2/3. HTML BFS + child prefix resolution --------------------

        upstream_results: List[Tuple[str, str]] = []
        used_fallback = False

        if nobrowse:
            print("[apt] skipping HTML BFS (cached: not browsable)", file=sys.stderr)
            used_fallback = True
        else:
            for idx, url in enumerate(upstreams_list):
                upstream_results = discover_distribution_paths(
                    url,
                    config=net_config,
                    index_root=root,
                    anchor=anchor,
                )
                if upstream_results:
                    if idx > 0:
                        print(
                            f"[apt] discovered distributions via alternate upstream {url}",
                            file=sys.stderr,
                        )
                    break

        # --- 4. Codename fallback probe ---------------------------------

        if not upstream_results:
            print("[apt] HTML discovery found no suites; trying codename fallback", file=sys.stderr)
            fallback = probe_fallback_suites(
                upstream,
                index_root=root,
                anchor=anchor,
                config=net_config,
            )
            if fallback:
                print(f"[apt] codename fallback found: {', '.join(fallback)}", file=sys.stderr)
                upstream_results = [(name, upstream) for name in fallback]
                used_fallback = True

        if not upstream_results:
            print("[apt] no distributions discovered under /dists on any upstream; giving up", file=sys.stderr)
            return []

        # --- Set discovery params for next scan -------------------------

        params = self.repo.setdefault("params", {})
        if used_fallback:
            params["discovery_method"] = "codename_fallback"
            params["nobrowse"] = True
        else:
            params["discovery_method"] = "html_bfs"
            params["nobrowse"] = False

        # --- Build Distribution nodes -----------------------------------

        distributions = Schema.Distributions()
        for path, eff_upstream in upstream_results:
            release_url = build_url(eff_upstream, root, path, anchor)
            cached_text = _release_text_cache.get((eff_upstream, root, path))
            dist = Distribution(
                url=release_url,
                upstream=eff_upstream,
                name=path,
            ).parse(config=net_config, text=cached_text)
            distributions.append(dist)

        return distributions
