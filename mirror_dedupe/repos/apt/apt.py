"""APT repo helpers for mirror-dedupe.

This module currently provides a small helper for parsing Debian/Ubuntu
Release files. In the future it can grow into a fuller AptParser that
also understands indices, layouts, etc.
"""

from __future__ import annotations

from typing import Any, Dict, List

from mirror_dedupe import schema as Schema
from .distributions import DistributionsParser
from .release import Release


class Apt(Schema.Repo):
    """APT Repo implementation and its parser helpers."""

    REPO_TYPE = "apt"

    # Canonical APT layout/signature constants so other components can
    # refer to them without hard-coding strings.
    INDEX_ROOT_DIR = "dists"           # Root under which suites live
    INDEX_ANCHOR_FILENAME = "Release"  # Primary metadata file per suite
    SIGNATURE_EXTENSION = ".gpg"       # Detached signature extension

    class Parser(Schema.Repo.Parser):
        """Concrete APT parser bound to an Apt Repo instance."""

        def parse(self):
            """Parse an APT-style upstream and return the complete Apt repo.

            This performs suite discovery under /dists/, fetches Release
            files, parses them, and populates repo.distributions
            accordingly.
            """

            repo = self.repo  # Apt instance

            # Initialise per-repo invariants for APT layout/signatures.
            repo.vars = Schema.Vars(
                index_root=Apt.INDEX_ROOT_DIR,
                anchor_filename=Apt.INDEX_ANCHOR_FILENAME,
                signature_extension=Apt.SIGNATURE_EXTENSION,
                repo_type=Apt.REPO_TYPE,
            )
            # Delegate suite/distribution parsing to the dedicated
            # DistributionsParser, which is pure and returns a list.
            repo.distributions = DistributionsParser(repo).parse()

            # Populate releases and indices for each distribution by
            # constructing a Release node from its URL and parsing it.
            for dist in repo.distributions:
                release = Release(
                    url=dist.release_url,
                    http_client=repo.http,
                    upstream=repo.upstream,
                    suite=dist.name,
                ).parse()

                repo.releases.append(release)

                indices = getattr(release, "indices", None)
                if indices:
                    repo.indices.extend(indices)

            # Populate top-level suites, components, and architectures
            # from the discovered distributions and their metadata.

            # Suites: unique logical suite names (before any pocket suffix).
            repo.suites = Schema.Suites()
            seen_suites = set()
            for dist in repo.distributions:
                suite_name = str(dist.name).split("/", 1)[0]
                if suite_name and suite_name not in seen_suites:
                    seen_suites.add(suite_name)
                    repo.suites.append(Schema.Suite(name=suite_name))

            # Components and architectures: roll up uniques from
            # per-distribution metadata.
            repo.components = Schema.Components()
            repo.architectures = Schema.Architectures()
            seen_components = set()
            seen_arches = set()

            for dist in repo.distributions:
                md = dist.get("metadata")
                if not md:
                    continue

                for comp in getattr(md, "components", []):
                    name = getattr(comp, "name", None)
                    if name and name not in seen_components:
                        seen_components.add(name)
                        repo.components.append(comp)

                for arch in getattr(md, "architectures", []):
                    name = getattr(arch, "name", None)
                    if name and name not in seen_arches:
                        seen_arches.add(name)
                        repo.architectures.append(arch)

            return repo

    # --- Repo hooks ---------------------------------------------------

    @classmethod
    def is_this_yours(cls, upstream: str, http_client: Any) -> bool:
        """Heuristic check: does this upstream look like an APT repo?"""

        try:
            dists_url = f"{upstream.rstrip('/')}/dists/"
            html = http_client.fetch_text(dists_url, timeout=5)
        except Exception:
            return False

        if not html:
            return False

        # Very small HTML dir parser inline to avoid importing helpers.
        suites = Schema.Suites()
        for line in html.splitlines():
            if 'href="' in line and '/"' in line:
                start = line.find('href="') + 6
                end = line.find('/"', start)
                if start > 5 and end > start:
                    raw = line[start:end]
                    name = raw.strip("/")
                    if name and all(suite.name != name for suite in suites):
                        suites.append(Schema.Suite(name=name))

        if not suites:
            return False

        # Probe the first few candidates for a plausible Release file.
        for suite_node in list(suites)[:3]:
            suite = suite_node.name
            # First try the standard flat layout: dists/<suite>/Release.
            rel_url = f"{upstream.rstrip('/')}/dists/{suite}/Release"
            try:
                text = http_client.fetch_text(rel_url, timeout=5)
            except Exception:
                text = None

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

            if text and _looks_like_release(text):
                return True

            # If there is no flat Release for this suite (or it did not look
            # like a Release), try a shallow nested pocket layout such as
            # dists/noble-updates/epoxy/Release used by ubuntu-cloud.
            try:
                pocket_index_url = f"{upstream.rstrip('/')}/dists/{suite}/"
                pocket_html = http_client.fetch_text(pocket_index_url, timeout=5)
            except Exception:
                pocket_html = None

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
                        if name and name not in pockets:
                            pockets.append(name)

            for pocket in pockets[:3]:
                pocket_rel_url = f"{upstream.rstrip('/')}/dists/{suite}/{pocket}/Release"
                try:
                    pocket_text = http_client.fetch_text(pocket_rel_url, timeout=5)
                except Exception:
                    continue

                if pocket_text and _looks_like_release(pocket_text):
                    return True

        return False

    def make_parser(self) -> "Repo.Parser":
        """Return an Apt.Parser bound to this Repo instance."""

        return Apt.Parser(self)


# Register this Repo so discovery code can obtain it via the
# shared Repo registry rather than hard-coding it in multiple
# modules.
Schema.Repo.register(Apt)
