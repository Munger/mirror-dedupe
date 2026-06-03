## @file release.py
##
## @brief APT-specific Release node with index parsing.
##
## ``Release`` extends ``Schema.Release``, adding the ability to fetch a
## Release file and parse its hash sections into ``Schema.Index`` entries
## for Packages and Sources.  URI and content operations are inherited
## from ``Node``.
##
## @copyright Copyright (c) 2026 Tim Hosking
## @see https://github.com/munger
## @par Licence: MIT

from __future__ import annotations

from typing import Any, Dict, List

from mirror_dedupe import schema as Schema
from mirror_dedupe.lib.html_helpers import build_url
from .index import AptIndex


class Release(Schema.Release):
    ## @brief APT-specific Release node that can parse its own indices.

    class IndexMetadata(Schema.Index.Metadata):
        ## @brief APT-specific metadata for an index entry derived from a Release.

        def __init__(
            self,
            *,
            suite: str,
            algorithm: str,
            checksum: str,
            size: int,
        ) -> None:
            super().__init__(
                suite=suite,
                algorithm=algorithm,
                checksum=checksum,
                size=size,
            )

    def __init__(
        self,
        *,
        url: str,
        upstream: str,
        suite: str,
    ) -> None:
        ## @brief Construct an APT Release bound to a URL.
        ##
        ## Initialises the underlying ``Schema.Release`` with APT-specific
        ## layout defaults.
        ##
        ## @param url      URL of the Release file.
        ## @param upstream Base upstream URL.
        ## @param suite    Logical suite name (e.g. ``"noble"``).

        pocket: str | None = None
        relative_dir = f"dists/{suite}"
        path = f"{relative_dir}/Release"
        repo_type = "apt"
        kind = "release"

        super().__init__(
            suite=suite,
            pocket=pocket,
            relative_dir=relative_dir,
            path=path,
            repo_type=repo_type,
            kind=kind,
            signature_extension=None,
            digest=None,
            uri=url,
        )

        self.url = url
        self.upstream = upstream
        self.suite = suite

    def _parse_hash_section(self, data: str, section: str) -> List[Dict[str, Any]]:
        ## @brief Parse a single hash section (``MD5Sum``, ``SHA1``, ``SHA256``)
        ##        from a Release body.
        ##
        ## @param data     Raw Release text.
        ## @param section  Section name to parse.
        ## @return List of entries with keys ``algorithm``, ``checksum``,
        ##         ``size``, ``path``.

        lines = data.splitlines()
        entries: List[Dict[str, Any]] = []
        in_section = False

        for line in lines:
            if not in_section:
                if line.startswith(f"{section}:"):
                    in_section = True
                continue

            if not line or not line[0].isspace():
                if in_section:
                    break
                continue

            parts = line.split()
            if len(parts) < 3:
                continue

            checksum = parts[0]
            try:
                size = int(parts[1])
            except ValueError:
                continue
            path = parts[2]

            entries.append(
                {
                    "algorithm": section.lower(),
                    "checksum": checksum,
                    "size": size,
                    "path": path,
                }
            )

        return entries

    def parse(self, config: Any = None, text: str | None = None) -> "Release":
        ## @brief Populate indices for this Release from its hash sections.
        ##
        ## If *text* is provided (e.g. from Distribution metadata), skip the
        ## HTTP fetch and parse directly from the cached body.
        ##
        ## @param config  Optional network config dict (ipv6_ok, timeout).
        ## @param text    Optional pre-fetched Release body text.
        ## @return This Release (for chaining).

        if text is None:
            text_bytes = self.fetch(config=config)
            if text_bytes is None:
                raise RuntimeError(f"No content for Release at {self.get('uri', 'unknown')}")
            text = text_bytes.decode("utf-8", errors="replace")

        indices = Schema.Indices()

        # Iterate all three hash sections to maximise cross-repo compatibility
        for section in ("MD5Sum", "SHA1", "SHA256"):
            for entry in self._parse_hash_section(text, section):
                path = entry["path"]
                # Classify index by path pattern for downstream processing
                if "Packages" in path:
                    kind = "packages"
                elif "Sources" in path:
                    kind = "sources"
                else:
                    kind = "other"

                metadata = self.IndexMetadata(
                    suite=self.suite,
                    algorithm=entry["algorithm"],
                    checksum=entry["checksum"],
                    size=entry["size"],
                )

                index_uri = build_url(self.upstream, self.relative_dir, path)

                indices.append(
                    AptIndex(
                        path=path,
                        kind=kind,
                        metadata=metadata,
                        uri=index_uri,
                    )
                )

        self.indices = indices
        return self


