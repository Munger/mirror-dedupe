## @file apt_vendor.py
##
## @brief Vendor-flavoured APT Repo implementation.
##
## ``AptVendor`` is a thin subclass of ``Apt`` so callers can explicitly
## select a more forgiving APT repo type (via ``repo_type="apt_vendor"``)
## for third-party archives whose layouts differ from stock Debian/Ubuntu
## mirrors.
##
## The primary distinction from ``Apt`` is its separate ``REPO_TYPE`` and
## a ``_probe_fallback_suites`` helper that probes well-known suite names
## directly (bypassing HTML directory listing) for repos whose
## ``/dists/`` index is not browsable.
##
## @copyright Copyright (c) 2026 Tim Hosking
## @see https://github.com/munger
## @par Licence: MIT

from __future__ import annotations

from typing import Any, Dict, List
from mirror_dedupe import schema as Schema
from mirror_dedupe.lib.codenames import apt_codenames
from mirror_dedupe.lib.html_helpers import build_url
from mirror_dedupe.repos.apt.apt import Apt


class AptVendor(Apt):
    ## @brief Vendor APT Repo implementation.
    ##
    ## Behaviour is currently identical to ``Apt`` at parse time but it
    ## can be forced via ``repo_type="apt_vendor"`` / ``--repo-type
    ## apt_vendor`` in the scanner.  This hook lets us later relax
    ## detection/parsing for vendor layouts without loosening the rules
    ## for normal distro mirrors.

    REPO_TYPE = "apt_vendor"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        ## @brief Construct an AptVendor repo and precompute dist candidates.
        ##
        ## Reuses the core Apt machinery for parsing.  The only
        ## vendor-specific state is an optional list of candidate
        ## distribution paths (relative to ``/dists/``) discovered via
        ## ``_probe_fallback_suites`` for repos whose ``/dists`` index is
        ## not browsable (e.g. Grafana, nodesource).

        super().__init__(*args, **kwargs)

        try:
            upstreams_list = [u.url for u in self.upstreams if u.url]
            upstream_idx = self.upstream_idx
            upstream = upstreams_list[upstream_idx] if upstreams_list else None
            http_client = getattr(self, "http", None)
            if upstream and http_client:
                candidates = self._probe_fallback_suites(upstream, http_client)
            else:
                candidates = []
        except Exception:
            candidates = []

        if candidates:
            self.dist_candidates = candidates

    @classmethod
    def _probe_fallback_suites(cls, upstream: str, http_client: Any) -> list[str]:
        ## @brief Probe a small set of well-known suites via ``dists/<suite>/Release``.
        ##
        ## Used both by ``is_this_yours`` and by the Parser as a
        ## last-resort distributions source for vendor repos whose
        ## ``/dists/`` index is not browsable (e.g. Grafana).  Never
        ## mutates any Repo state; callers decide how to use the
        ## returned names.
        ##
        ## @param upstream     Upstream URL.
        ## @param http_client  HTTPClient for fetching.
        ## @return List of suite names that have plausible Release files.

        def _looks_like_release(body: str) -> bool:
            markers = 0
            for line in body.splitlines():
                if line.startswith("Suite:") or line.startswith("Codename:"):
                    markers += 1
                if line.startswith("Components:") or line.startswith("Architectures:"):
                    markers += 1
                if markers >= 2:
                    return True
            return False

        try:
            known = apt_codenames()
        except Exception:
            known = []

        suites_to_probe: List[str] = ["stable"]
        suites_to_probe.extend(known)

        candidates: list[str] = []
        for suite in suites_to_probe:
            rel_url = build_url(upstream, cls.INDEX_ROOT_DIR, suite, cls.INDEX_ANCHOR_FILENAME)
            try:
                text = http_client.fetch_text(rel_url, timeout=5)
            except Exception:
                text = None

            if text and _looks_like_release(text):
                candidates.append(suite)

        return candidates

    @classmethod
    def is_this_yours(cls, upstream: str, http_client: Any) -> bool:
        ## @brief Heuristic check for vendor APT-style repos.
        ##
        ## If the core ``Apt`` implementation would claim this repo,
        ## explicitly does *not* claim it here (Apt remains the owner for
        ## normal mirrors).  Falls back to probing well-known suite names
        ## for vendor archives where ``/dists/`` is not indexable.
        ##
        ## @param upstream     Upstream URL.
        ## @param http_client  HTTPClient for fetching.
        ## @return True if this looks like a vendor APT repo.

        from mirror_dedupe.repos.apt.apt import Apt

        if Apt.is_this_yours(upstream, http_client):
            return False

        return bool(cls._probe_fallback_suites(upstream, http_client))


Schema.Repo.register(AptVendor)
