from __future__ import annotations

from typing import List, TYPE_CHECKING

from mirror_dedupe import schema as Schema
from .distribution import Distribution

if TYPE_CHECKING:
    from .apt import Apt


class DistributionsParser:
    """Discover distributions under /dists and delegate per-distribution parsing.

    This parser is pure with respect to the Apt repo: it only reads upstream
    and http from the repo instance and returns a Distributions list.
    """

    def __init__(self, repo: "Apt") -> None:
        self.repo = repo
        self.http = repo.http
        self.upstream = repo.upstream

    def parse(self):
        """Return a Distributions list discovered under /dists without mutating the repo."""

        upstream = self.upstream
        http_client = self.http

        # Discover suites by inspecting /dists/ HTML and validating via Release.
        dists_url = f"{upstream.rstrip('/')}/dists/"
        html = http_client.fetch_text(dists_url)
        if not html:
            return []

        distributions = Schema.Distributions()
        suites = Schema.Suites()
        for line in html.splitlines():
            if 'href="' in line and '/"' in line:
                start = line.find('href="') + 6
                end = line.find('/"', start)
                if start > 5 and end > start:
                    raw = line[start:end]
                    name = raw.strip("/")
                    # Skip parent/self directory entries like '.' and '..'.
                    if name in (".", ".."):
                        continue

                    if name and all(suite.name != name for suite in suites):
                        suites.append(Schema.Suite(name=name))

        # For each suite, construct a Distribution node from its Release URL
        # and let it parse itself.
        for suite_node in suites:
            suite = suite_node.name
            release_url = f"{upstream.rstrip('/')}/dists/{suite}/Release"
            dist = Distribution(
                url=release_url,
                http_client=http_client,
                upstream=upstream,
                name=suite,
            ).parse()
            distributions.append(dist)


            # Nested pocket layout: look for dists/<suite>/<pocket>/Release.
            pocket_index_url = f"{upstream.rstrip('/')}/dists/{suite}/"
            pocket_html = http_client.fetch_text(pocket_index_url)
            if not pocket_html:
                continue

            pockets: List[str] = []
            for line in pocket_html.splitlines():
                if 'href="' in line and '/"' in line:
                    start = line.find('href="') + 6
                    end = line.find('/"', start)
                    if start > 5 and end > start:
                        raw = line[start:end]
                        name = raw.strip("/")
                        # Skip parent/self directory entries like '.' and '..'.
                        if name in (".", ".."):
                            continue

                        if name and name not in pockets:
                            pockets.append(name)

            for pocket in pockets:
                pocket_release_url = f"{upstream.rstrip('/')}/dists/{suite}/{pocket}/Release"
                dist_name = f"{suite}/{pocket}"

                dist = Distribution(
                    url=pocket_release_url,
                    http_client=http_client,
                    upstream=upstream,
                    name=dist_name,
                ).parse()
                distributions.append(dist)

        return distributions
