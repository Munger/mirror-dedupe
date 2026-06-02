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

from typing import List, TYPE_CHECKING, Optional
import sys

from mirror_dedupe import schema as Schema
from mirror_dedupe.lib import http_client
from mirror_dedupe.lib.html_helpers import build_url
from .distribution import Distribution
from .utils import looks_like_release, discover_distribution_paths

if TYPE_CHECKING:
    pass


class DistributionsParser:
    ## @brief Discover distributions under ``/dists`` and delegate
    ##        per-distribution parsing.
    ##
    ## Pure with respect to the Apt repo: it only reads upstream and http
    ## from the repo instance and returns a ``Distributions`` list.

    def __init__(self, repo: "Apt", candidates: Optional[List[str]] = None) -> None:
        ## @param repo        The Apt Repo instance to discover for.
        ## @param candidates  Optional explicit distribution paths to probe
        ##                    directly, bypassing HTML discovery.

        self.repo = repo
        self.http = repo.http
        self.upstream_index = int(repo.get("upstream_idx", 0))
        self._candidates: List[str] = candidates or []

    def parse(self):
        ## @brief Return a ``Distributions`` list discovered under ``/dists``.
        ##
        ## When ``_candidates`` is set, probes only those explicit paths.
        ## Otherwise delegates to ``discover_distribution_paths`` for
        ## BFS-based HTML discovery.
        ##
        ## @return A ``Distributions`` NodeList.

        upstreams_list = [u.url for u in self.repo.upstreams if u.url]
        upstream = upstreams_list[self.upstream_index] if upstreams_list else ""
        http_client = self.http
        root = self.repo.INDEX_ROOT_DIR
        anchor = self.repo.INDEX_ANCHOR_FILENAME

        if self._candidates:
            distributions = Schema.Distributions()
            for path in self._candidates:
                release_url = build_url(upstream, root, path, anchor)
                print(f"[apt] probing Release for explicit candidate {path}: {release_url}", file=sys.stderr)
                text = http_client.fetch_text(release_url)
                if not text or not looks_like_release(text):
                    continue

                dist = Distribution(
                    url=release_url,
                    http_client=http_client,
                    upstream=upstream,
                    name=path,
                ).parse()
                distributions.append(dist)

            return distributions

        paths: List[str] = []
        upstreams = upstreams_list

        for idx, url in enumerate(upstreams):
            candidate_paths = discover_distribution_paths(
                url,
                http_client,
                index_root=root,
                anchor=anchor,
            )
            if candidate_paths:
                if idx > 0:
                    print(
                        f"[apt] discovered distributions via alternate upstream {url}",
                        file=sys.stderr,
                    )
                paths = candidate_paths
                break

        if not paths:
            print("[apt] no distributions discovered under /dists on any upstream; giving up", file=sys.stderr)
            return []

        distributions = Schema.Distributions()
        for path in paths:
            release_url = build_url(upstream, root, path, anchor)
            dist = Distribution(
                url=release_url,
                http_client=http_client,
                upstream=upstream,
                name=path,
            ).parse()
            distributions.append(dist)

        return distributions
