from __future__ import annotations

from typing import List, TYPE_CHECKING, Iterable, Optional
import re
import sys

from mirror_dedupe import schema as Schema
from mirror_dedupe.lib import http_client
from mirror_dedupe.lib.html_helpers import extract_href
from .distribution import Distribution
from .utils import looks_like_release

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

        # If explicit candidate paths were supplied, probe only those;
        # otherwise delegate all discovery to the HTML/BFS helper.
        if self._candidates:
            distributions = Schema.Distributions()
            for path in self._candidates:
                release_url = f"{upstream.rstrip('/')}/dists/{path}/Release"
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

        # Delegate all /dists walking and candidate selection to a
        # helper so this method only needs to construct Distribution
        # nodes from the discovered paths.
        dists_url = f"{upstream.rstrip('/')}/dists/"
        print(f"[apt] probing dists index: {dists_url}", file=sys.stderr)
        html = http_client.fetch_text(dists_url)
        if not html:
            print("[apt] no HTML content at /dists/; giving up on suite discovery", file=sys.stderr)
            return []

        paths = self._discover_distribution_paths(upstream, http_client, html)

        distributions = Schema.Distributions()
        for path in paths:
            release_url = f"{upstream.rstrip('/')}/dists/{path}/Release"
            dist = Distribution(
                url=release_url,
                http_client=http_client,
                upstream=upstream,
                name=path,
            ).parse()
            distributions.append(dist)

        return distributions

    def _discover_distribution_paths(self, upstream: str, http_client: Any, root_html: str) -> List[str]:
        """Walk /dists and return all nested paths with plausible Releases.

        This is responsible for suite/pocket discovery only; it never
        mutates the repo and never inspects component/arch indices.
        """

        suites = Schema.Suites()
        lines = root_html.splitlines()
        print(f"[apt] /dists/ HTML line count: {len(lines)}", file=sys.stderr)

        for name in self._iter_href_names(lines, dirs_only=False):
            if name and all(suite.name != name for suite in suites):
                print(f"[apt] discovered suite under /dists: {name}", file=sys.stderr)
                suites.append(Schema.Suite(name=name))

        if not suites:
            print("[apt] no suites discovered in /dists/ HTML; falling back to synthetic 'stable'", file=sys.stderr)

        # Walk the /dists hierarchy breadth-first starting from the
        # top-level suite names we just discovered. Any nested path of
        # the form "foo" or "foo/bar/..." that has a plausible
        # dists/<path>/Release is treated as a distribution
        # entry-point; deeper component/arch indices remain internal
        # to the Release/Packages parsing.
        queue: List[str] = [s.name for s in suites]
        seen_paths = set(queue)

        print(f"[apt] total top-level suites discovered: {len(queue)}", file=sys.stderr)

        discovered_paths: List[str] = []

        while queue:
            path = queue.pop(0)

            # 1) Validate this candidate via dists/<path>/Release; only
            #    accept it as a distribution if the Release looks
            #    plausible (has at least Suite/Codename metadata).
            release_url = f"{upstream.rstrip('/')}/dists/{path}/Release"
            print(f"[apt] probing Release for candidate {path}: {release_url}", file=sys.stderr)
            text = http_client.fetch_text(release_url)
            if text and looks_like_release(text):
                discovered_paths.append(path)
                # We found a real distribution entry-point; do not
                # descend into nested paths under it (avoid walking
                # into component/arch trees such as main/binary-amd64).
                continue

            index_url = f"{upstream.rstrip('/')}/dists/{path}/"
            index_html = http_client.fetch_text(index_url)
            if not index_html:
                continue

            for child_name in self._iter_href_names(index_html.splitlines(), dirs_only=True):
                child_path = f"{path}/{child_name}"
                if child_path in seen_paths:
                    continue

                print(f"[apt] discovered nested candidate under /dists: {child_path}", file=sys.stderr)
                seen_paths.add(child_path)
                queue.append(child_path)

        return discovered_paths

    @classmethod
    def _iter_href_names(cls, lines: Iterable[str], *, dirs_only: bool = False) -> Iterable[str]:
        """Yield normalised names from href attributes in directory listings.

        This helper is used both for top-level /dists/ discovery and for
        nested walks under /dists/<path>/. It deliberately ignores:

        * absolute URLs (marketing links)
        * Apache "?C=N;O=D" style sort links
        * parent/self entries (".", "..") and obvious index names
          like "dists"
        * any href whose final segment still contains a URI scheme
          marker
        """

        for line in lines:
            raw = extract_href(line)
            if not raw:
                continue

            # When crawling nested paths we may be looking for
            # directory-style hrefs only; bare filenames such as
            # InRelease/Release/Release.gpg are handled entirely by
            # Release parsing.
            if dirs_only and not raw.endswith("/"):
                continue

            # Ignore absolute URLs and Apache sort/query links.
            if raw.startswith("http://") or raw.startswith("https://") or "?" in raw:
                continue

            name = raw.strip("/")
            if "/" in name:
                name = name.split("/")[-1]

            if name in (".", "..", "dists"):
                continue
            if "://" in name or ":" in name:
                continue

            if name:
                yield name
