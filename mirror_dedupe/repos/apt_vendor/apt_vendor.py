"""Vendor-flavoured APT Repo implementation.

AptVendor is a thin subclass of :class:`Apt` so callers can explicitly
select a more forgiving APT repo type (via ``repo_type="apt_vendor"``)
for third-party archives whose layouts differ from stock Debian/Ubuntu
mirrors.

Initially it simply reuses the core :class:`Apt.Parser` behaviour; the
primary distinction is its separate ``REPO_TYPE`` so it can be selected
without affecting the stricter :class:`Apt` used for distribution
mirrors.
"""

from __future__ import annotations

from typing import Any, Dict, List
from mirror_dedupe import schema as Schema
from mirror_dedupe.lib.codenames import apt_codenames
from mirror_dedupe.lib.html_helpers import build_url
from mirror_dedupe.repos.apt.apt import Apt


class AptVendor(Apt):
    """Vendor APT Repo implementation.

    Behaviour is currently identical to :class:`Apt` at parse time but it
    can be forced via ``repo_type="apt_vendor"`` / ``--repo-type
    apt_vendor`` in the scanner. This hook lets us later relax
    detection/parsing for vendor layouts without loosening the rules for
    normal distro mirrors.
    """

    REPO_TYPE = "apt_vendor"

    def __init__(self, *args: Any, **kwargs: Any) -> None:  # type: ignore[override]
        """Construct an AptVendor repo and precompute dist candidates.

        We deliberately reuse the core Apt machinery for parsing; the
        only vendor-specific state we need to attach is an optional list
        of candidate distribution paths (relative to /dists/) discovered
        via :meth:`_probe_fallback_suites` for repos whose /dists index
        is not browsable.
        """

        super().__init__(*args, **kwargs)

        # If we already have upstream/http wired, seed dist_candidates
        # so the shared DistributionsParser can probe these directly.
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
        """Probe a small set of well-known suites via dists/<suite>/Release.

        This is used both by ``is_this_yours`` and by the Parser as a
        last-resort distributions source for vendor repos whose
        ``/dists/`` index is not browsable (e.g. Grafana). It never
        mutates any Repo state; callers decide how to use the returned
        names.
        """

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

        # Seed with the generic "stable" alias and then append a small
        # bounded set of known short codenames from distro-info-data via
        # apt_codenames(). This keeps detection cheap while remaining
        # future-proof as new releases are added.
        try:
            known = apt_codenames()
        except Exception:
            known = []

        # Prepend 'stable', then all known series. We rely on
        # apt_codenames() for ordering and let the HTTP timeouts bound
        # the worst-case probe cost.
        suites_to_probe: List[str] = ["stable"]
        suites_to_probe.extend(known)

        candidates: list[str] = []
        for suite in suites_to_probe:
            # Use the same layout constants and URL builder as the core
            # Apt parser so vendor probing stays in sync with APT
            # semantics.
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
        """Heuristic check for vendor APT-style repos.

        Behaviour is:

        * This is *only* used for vendor-style repos; the core
          :class:`Apt.is_this_yours` is run separately during type
          selection and remains responsible for normal distro mirrors.
        * We probe a small set of well-known suite names such as
          ``stable`` by fetching
          ``dists/<suite>/Release`` directly and applying the same
          ``_looks_like_release`` heuristic used by Apt.
        """

        # If the core Apt implementation would claim this repo, we
        # explicitly *do not* claim it here; Apt remains the owner for
        # normal mirrors.
        from mirror_dedupe.repos.apt.apt import Apt

        if Apt.is_this_yours(upstream, http_client):
            return False

        # Fallback for vendor archives where /dists/ is not indexable but
        # at least one common suite (e.g. "stable") still has a plausible
        # Release file.
        return bool(cls._probe_fallback_suites(upstream, http_client))


# Register this Repo so discovery code can obtain it via the shared
# Repo registry rather than hard-coding it in multiple modules.
Schema.Repo.register(AptVendor)
