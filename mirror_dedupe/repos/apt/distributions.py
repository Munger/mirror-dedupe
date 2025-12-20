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
    """Discover distributions under /dists and delegate per-distribution parsing.

    This parser is pure with respect to the Apt repo: it only reads upstream
    and http from the repo instance and returns a Distributions list.
    """

    def __init__(self, repo: "Apt", candidates: Optional[List[str]] = None) -> None:
        self.repo = repo
        self.http = repo.http
        self.upstream = repo.upstream
        # Optional explicit candidate paths (relative to /dists) to
        # probe directly as distributions, bypassing HTML discovery.
        self._candidates: List[str] = candidates or []

    def parse(self):
        """Return a Distributions list discovered under /dists without mutating the repo."""

        upstream = self.upstream
        http_client = self.http
        root = self.repo.INDEX_ROOT_DIR
        anchor = self.repo.INDEX_ANCHOR_FILENAME

        # If explicit candidate paths were supplied, probe only those;
        # otherwise delegate all discovery to the HTML/BFS helper.
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

        # Delegate all /dists walking and candidate selection to the
        # shared helper so this method only needs to construct
        # Distribution nodes from the discovered paths. We try the
        # primary upstream first, then any alternates recorded on the
        # Repo via iter_upstreams().
        paths: List[str] = []
        upstreams = self.repo.iter_upstreams()

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
            # Always build Release URLs from the primary upstream so the
            # resulting Repo snapshot describes the logical layout
            # independently of which mirror was used for discovery.
            release_url = build_url(upstream, root, path, anchor)
            dist = Distribution(
                url=release_url,
                http_client=http_client,
                upstream=upstream,
                name=path,
            ).parse()
            distributions.append(dist)

        return distributions
