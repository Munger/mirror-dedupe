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
            ## @brief Initialise APT-specific Index metadata from a Release hash entry.
            ##
            ## @param suite     Suite name for scoping.
            ## @param algorithm Hash algorithm label (e.g. ``"sha256"``).
            ## @param checksum  Hex digest.
            ## @param size      File size in bytes.
            ## @return None
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

    def stream(self, data: Optional[bytes] = None):
        ## @brief Yield Index children by parsing the Release file.
        ##
        ## Reads the Release body (from *data* bytes or disk), parses hash
        ## sections, groups by base path, creates one ``AptIndex`` per
        ## primary compression variant and ``VariantIndex`` for the rest.
        ##
        ## Sets ``self.indices`` to the full ``Indices`` NodeList.
        ##
        ## @param data  Optional bytes to parse (scan mode).
        ## @yield Index child nodes.

        raw = self._open_binary(data)
        try:
            text = raw.read().decode("utf-8", errors="replace")
        finally:
            raw.close()

        arch_filter: Optional[List[str]] = getattr(self, "_arch_filter", None)
        comp_filter: Optional[List[str]] = getattr(self, "_comp_filter", None)
        dest = self._dest

        # Collect all hash-section entries, excluding non-Packages/Sources
        entries: List[Dict[str, Any]] = []
        for section in ("MD5Sum", "SHA1", "SHA256"):
            for entry in self._parse_hash_section(text, section):
                entry_path = entry["path"]
                if "Packages" not in entry_path and "Sources" not in entry_path:
                    continue
                entry["_kind"] = "packages" if "Packages" in entry_path else "sources"

                if arch_filter is not None or comp_filter is not None:
                    parts = entry_path.split("/")
                    if len(parts) >= 2:
                        comp = parts[0]
                        arch_part = parts[1]
                        arch = arch_part[7:] if arch_part.startswith("binary-") else arch_part
                        if arch_filter is not None and arch not in arch_filter:
                            continue
                        if comp_filter is not None and comp not in comp_filter:
                            continue
                entries.append(entry)

        # Deduplicate by path — later sections (SHA256) overwrite MD5/SHA1
        seen_by_path: Dict[str, Dict[str, Any]] = {}
        for entry in entries:
            seen_by_path[entry["path"]] = entry
        entries = list(seen_by_path.values())

        # Group by base path (strip compression extensions), pick primary variant
        by_base: Dict[str, List[Dict[str, Any]]] = {}
        for entry in entries:
            ep = entry["path"]
            base = ep
            for ext in (".gz", ".bz2", ".xz", ".lzma", ".lz4", ".zst"):
                if base.endswith(ext):
                    base = base[: -len(ext)]
                    break
            by_base.setdefault(base, []).append(entry)

        indices = Schema.Indices()
        seen_paths: set = set()

        for base, group in by_base.items():
            def _rank(entry: Dict[str, Any]) -> int:
                ## @brief Rank compression by preference (lower = better).
                ##
                ## Preference order: ``xz`` > ``bz2`` > ``gz`` > uncompressed.
                ##
                ## @param entry  A hash entry with a ``"path"`` key.
                ## @return Rank integer (0 = best).
                p = entry["path"]
                if p.endswith(".xz"):
                    return 0
                if p.endswith(".bz2"):
                    return 1
                if p.endswith(".gz"):
                    return 2
                return 3
            group.sort(key=_rank)
            primary = group[0]
            variants = group[1:]

            kind = primary["_kind"]
            entry_path = primary["path"]
            if entry_path in seen_paths:
                continue
            seen_paths.add(entry_path)

            metadata = self.IndexMetadata(
                suite=self.suite,
                algorithm=primary["algorithm"],
                checksum=primary["checksum"],
                size=primary["size"],
            )
            index_uri = build_url(self.upstream, self.relative_dir, entry_path)
            index_path = f"{dest}/dists/{self.suite}/{entry_path}" if dest else f"dists/{self.suite}/{entry_path}"

            primary_index = AptIndex(
                path=index_path,
                kind=kind,
                metadata=metadata,
                uri=index_uri,
                dest=dest,
                size=primary["size"],
            )
            primary_index._repo_vars = self._repo_vars
            indices.append(primary_index)
            yield primary_index

            # Variants get VariantIndex — downloaded but not parsed for children
            for variant in variants:
                v_path = variant["path"]
                if v_path in seen_paths:
                    continue
                seen_paths.add(v_path)
                v_metadata = self.IndexMetadata(
                    suite=self.suite,
                    algorithm=variant["algorithm"],
                    checksum=variant["checksum"],
                    size=variant["size"],
                )
                v_uri = build_url(self.upstream, self.relative_dir, v_path)
                v_index_path = f"{dest}/dists/{self.suite}/{v_path}" if dest else f"dists/{self.suite}/{v_path}"
                v_index = Schema.VariantIndex(
                    path=v_index_path,
                    kind=kind,
                    metadata=v_metadata,
                    uri=v_uri,
                    size=variant["size"],
                )
                v_index._repo_vars = self._repo_vars
                indices.append(v_index)
                yield v_index

        self.indices = indices

    def on_parse(self, *, config: Optional[Dict[str, Any]] = None) -> None:
        ## @brief Fetch the Release body and stream-parse into Index children.
        ##
        ## Calls ``fetch()`` to retrieve the Release body (from cache,
        ## HTTP, or disk depending on mode), then ``list(self.stream(data))``
        ## to materialise the Index tree.
        ##
        ## @param config  Optional network config dict (ipv6_ok, timeout).
        ## @return None

        text_bytes = self.fetch(uri=self.get("uri"), config=config)
        if text_bytes is None:
            raise RuntimeError(f"No content for Release at {self.get('uri', 'unknown')}")
        list(self.stream(data=text_bytes))


