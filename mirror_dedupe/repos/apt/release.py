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

from typing import Any, Dict, List, Optional

from mirror_dedupe import schema as Schema
from mirror_dedupe.lib.html_helpers import build_url
from .index import AptIndex


class Release(Schema.Release):
    ## @brief APT-specific Release node that can parse its own indices.

    _children = ["indices"]
    ## @brief Base ``Node.parse()`` recurses into the child Index list
    ##        after ``on_parse()`` completes.

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
        dest: str = "",
    ) -> None:
        ## @brief Construct an APT Release bound to a URL.
        ##
        ## The Release body is not fetched here.  The parent Distribution
        ## pre-caches the body in ``_cache`` before the base recursion
        ## calls ``on_parse()``.  ``path`` is set at construction time
        ## based on *dest* and *suite* so it is correct from birth.
        ##
        ## @param url      URL of the Release file.
        ## @param upstream Base upstream URL.
        ## @param suite    Logical suite name (e.g. ``"noble"``).
        ## @param dest     Repo dest prefix (empty in scan mode).

        pocket: str | None = None
        relative_dir = f"dists/{suite}"
        path = f"{dest}/dists/{suite}/Release" if dest else f"dists/{suite}/Release"
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
        self._dest = dest

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

    def on_parse(self, *, config: Optional[Dict[str, Any]] = None) -> None:
        ## @brief Parse the Release body into child Index nodes.
        ##
        ## The Release body is fetched via ``fetch()`` (returned from
        ## ``_cache`` pre-cached by the parent Distribution).
        ## Hash sections are parsed into ``self.indices``.  Index nodes
        ## are created in memory only — sync to disk happens later via
        ## the sync pipeline.
        ##
        ## Architecture/component filters are read from
        ## ``self._arch_filter`` / ``self._comp_filter``, set by the
        ## parent Distribution during its own ``on_parse()``.
        ##
        ## @param config  Optional network config dict (ipv6_ok, timeout).
        ## @return None

        text_bytes = self.fetch(uri=self.get("uri"), config=config)
        if text_bytes is None:
            raise RuntimeError(f"No content for Release at {self.get('uri', 'unknown')}")
        text = text_bytes.decode("utf-8", errors="replace")

        arch_filter: Optional[List[str]] = getattr(self, "_arch_filter", None)
        comp_filter: Optional[List[str]] = getattr(self, "_comp_filter", None)

        indices = Schema.Indices()
        seen: Dict[str, Any] = {}
        dest = self._dest

        # Iterate all three hash sections to maximise cross-repo compatibility.
        # Deduplicate by path — SHA256 entries (iterated last) overwrite MD5/SHA1.
        for section in ("MD5Sum", "SHA1", "SHA256"):
            for entry in self._parse_hash_section(text, section):
                entry_path = entry["path"]
                # Classify index by path pattern for downstream processing
                if "Packages" in entry_path:
                    kind = "packages"
                elif "Sources" in entry_path:
                    kind = "sources"
                else:
                    kind = "other"

                # Apply architecture/component filters when provided
                if arch_filter is not None or comp_filter is not None:
                    parts = entry_path.split("/")
                    if len(parts) >= 2:
                        comp = parts[0]
                        arch_part = parts[1]
                        if arch_part.startswith("binary-"):
                            arch = arch_part[7:]
                        else:
                            arch = arch_part
                        if arch_filter is not None and arch not in arch_filter:
                            continue
                        if comp_filter is not None and comp not in comp_filter:
                            continue

                metadata = self.IndexMetadata(
                    suite=self.suite,
                    algorithm=entry["algorithm"],
                    checksum=entry["checksum"],
                    size=entry["size"],
                )

                index_uri = build_url(self.upstream, self.relative_dir, entry_path)
                index_path = f"{dest}/dists/{self.suite}/{entry_path}" if dest else f"dists/{self.suite}/{entry_path}"

                seen[entry_path] = AptIndex(
                    path=index_path,
                    kind=kind,
                    metadata=metadata,
                    uri=index_uri,
                    dest=dest,
                    size=entry["size"],
                )

        self.indices = Schema.Indices(seen.values())


