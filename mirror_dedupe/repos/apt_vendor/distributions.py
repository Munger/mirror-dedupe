"""Vendor-specific distributions parser for APT repos.

This module mirrors the core ``apt.distributions`` logic but adds
slightly more defensive heuristics that are tailored for third-party
APT archives whose ``/dists/`` HTML indices may contain absolute URLs or
other non-suite links.
"""

from __future__ import annotations

from typing import List

from mirror_dedupe import schema as Schema


class VendorDistributionsParser:
    """Parse suites/pockets for vendor-style APT repositories.

    The behaviour is intentionally close to the core
    ``apt.distributions.DistributionsParser`` but with two small
    changes:

    * We ignore ``href`` targets that look like absolute URLs
      (contain ``"://"``), which some vendor indexes include as
      marketing links.
    * We otherwise keep the same suite naming and filtering rules so
      downstream code sees a familiar shape.
    """

    def __init__(self, repo: Schema.Repo) -> None:
        self.repo = repo

    def parse(self) -> Schema.Distributions:
        repo = self.repo
        http = repo.http

        upstream = repo.upstream.rstrip("/")
        index_root = repo.vars.index_root

        dists_url = f"{upstream}/{index_root}/"
        html = http.fetch_text(dists_url, timeout=10)
        suites = Schema.Suites()

        if not html:
            return Schema.Distributions()

        for line in html.splitlines():
            if 'href="' not in line or '/"' not in line:
                continue
            start = line.find('href="') + 6
            end = line.find('/"', start)
            if start <= 5 or end <= start:
                continue
            raw = line[start:end]
            name = raw.strip("/")
            if not name:
                continue
            # Ignore absolute URLs such as "https://www.example.com/".
            if "://" in name:
                continue
            if all(suite.name != name for suite in suites):
                suites.append(Schema.Suite(name=name))

        dists = Schema.Distributions()
        for suite in suites:
            dists.append(Schema.Distribution(name=suite.name))

        return dists
