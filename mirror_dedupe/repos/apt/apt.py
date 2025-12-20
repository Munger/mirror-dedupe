"""APT repo helpers for mirror-dedupe.

This module currently provides a small helper for parsing Debian/Ubuntu
Release files. In the future it can grow into a fuller AptParser that
also understands indices, layouts, etc.
"""

from __future__ import annotations

from typing import Any, Dict, List

from mirror_dedupe import schema as Schema
from mirror_dedupe.lib.html_helpers import build_url
from .distributions import DistributionsParser
from .release import Release
from .utils import discover_distribution_paths


class Apt(Schema.Repo):
    """APT Repo implementation and its parser helpers."""

    REPO_TYPE = "apt"

    # Canonical APT layout/signature constants so other components can
    # refer to them without hard-coding strings.
    INDEX_ROOT_DIR = "dists"           # Root under which suites live
    INDEX_ANCHOR_FILENAME = "Release"  # Primary metadata file per suite
    SIGNATURE_EXTENSION = ".gpg"       # Detached signature extension

    @classmethod
    def restore(cls, snapshot: Dict[str, Any]) -> "Apt":
        """Restore a fully functional Apt Repo from a snapshot.

        This delegates data/tree reconstruction to ``Repo.from_snapshot``,
        which uses the Node-level metadata to restore all child
        collections. HTTP wiring can then be provided lazily by the
        Repo/HTTP client layer based on the restored network config.
        """

        # Repo.from_snapshot constructs ``cls`` instances. We pass a
        # placeholder for the HTTP client; callers can replace or
        # lazily construct it as needed based on ``repo.network``.
        return cls.from_snapshot(snapshot, http_client=None)  # type: ignore[return-value]

    class Parser(Schema.Repo.Parser):
        """Concrete APT parser bound to an Apt Repo instance."""

        def parse(self):
            """Parse an APT-style upstream and return the complete Apt repo.

            This performs suite discovery under /dists/, fetches Release
            files, parses them, and populates repo.distributions
            accordingly.
            """

            repo = self.repo  # Apt (or subclass) instance

            # Debug: show which concrete Repo subclass is being parsed so
            # we can see whether autodetection selected Apt or a
            # subclass such as AptVendor.
            import sys

            print(f"[apt] parsing repo class: {type(repo).__name__}", file=sys.stderr)

            # Initialise per-repo invariants for APT layout/signatures.
            repo.vars = Schema.Vars(
                index_root=Apt.INDEX_ROOT_DIR,
                anchor_filename=Apt.INDEX_ANCHOR_FILENAME,
                signature_extension=Apt.SIGNATURE_EXTENSION,
            )
            # Delegate suite/distribution parsing to the dedicated
            # DistributionsParser, which is pure and returns a list.
            # If the Repo has been primed with explicit candidate
            # distribution paths (e.g. by AptVendor), those are passed
            # through so we can probe them directly instead of
            # relying solely on /dists/ HTML.
            candidates = getattr(repo, "dist_candidates", None)
            repo.distributions = DistributionsParser(repo, candidates=candidates).parse()

            # Populate releases and indices for each distribution by
            # constructing a Release node from its URL and parsing it.
            for dist in repo.distributions:
                name = str(dist.name)
                if not name:
                    continue
                url = build_url(repo.upstream, repo.INDEX_ROOT_DIR, name, repo.INDEX_ANCHOR_FILENAME)
                release = Release(
                    url=url,
                    http_client=repo.http,
                    upstream=repo.upstream,
                    suite=name,
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
        """Heuristic check: does this upstream look like an APT repo?

        We delegate to the shared ``discover_distribution_paths`` helper,
        which walks ``/dists`` and looks for any ``dists/<path>/Release``
        that passes :func:`looks_like_release`. If at least one
        distribution path is discovered, we claim the upstream as APT.
        """

        try:
            paths = discover_distribution_paths(
                upstream,
                http_client,
                index_root=cls.INDEX_ROOT_DIR,
                anchor=cls.INDEX_ANCHOR_FILENAME,
                max_depth=3,
            )
        except Exception:
            return False

        return bool(paths)

    def make_parser(self) -> "Repo.Parser":
        """Return an Apt.Parser bound to this Repo instance."""

        return Apt.Parser(self)


# Register this Repo so discovery code can obtain it via the
# shared Repo registry rather than hard-coding it in multiple
# modules.
Schema.Repo.register(Apt)
