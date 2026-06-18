## @file distribution.py
##
## @brief APT-specific Distribution node with Release parsing.
##
## ``Distribution`` extends ``Schema.Distribution``, adding the ability
## to fetch and parse a Release file to populate components,
## architectures, fields, and hash sections.
##
## @copyright Copyright (c) 2026 Tim Hosking
## @see https://github.com/munger
## @par Licence: MIT

from __future__ import annotations

from typing import Any, Dict, List, Optional

from mirror_dedupe.lib.exceptions import ExceptionMsg
from mirror_dedupe import schema as Schema


class Distribution(Schema.Distribution):
    ## @brief APT-specific Distribution node that fetches Release files, parses
    ##        release headers (components, architectures, fields) and hash sections
    ##        (MD5Sum, SHA1, SHA256), and populates metadata.

    _children = ("release",)
    ## @brief Base ``Node.parse()`` recurses into the child Release after
    ##        ``on_parse()`` completes.

    class Metadata(Schema.Distribution.Metadata):
        ## @brief APT-specific metadata for a distribution derived from a Release.

        def __init__(
            self,
            *,
            fields: Dict[str, Any],
            components: Schema.Components,
            architectures: Schema.Architectures,
            hash_sections: Dict[str, List[Dict[str, Any]]] | None = None,
            body: Schema.Release.Digest | None = None,
        ) -> None:
            ## @brief Initialise APT-specific Distribution metadata from a Release.
            ##
            ## @param fields         Raw Release key-value fields.
            ## @param components     Parsed component list.
            ## @param architectures  Parsed architecture list.
            ## @param hash_sections  Raw hash section blocks (MD5Sum/SHA1/SHA256).
            ## @param body           Digest snapshot for cheap identity checks.
            ## @return None
            super().__init__(
                fields=fields,
                components=components,
                architectures=architectures,
                hash_sections=hash_sections,
                body=body,
            )

    def __init__(
        self,
        *,
        url: str,
        upstream: str,
        name: str,
    ) -> None:
        ## @brief Construct an APT Distribution bound to a Release URL.
        ##
        ## Initialises the underlying ``Schema.Distribution`` with empty
        ## Components/Architectures.
        ##
        ## @param url      URL pointing at the Release file.
        ## @param upstream Base upstream URL.
        ## @param name     Distribution name (e.g. ``"noble"``).

        components = Schema.Components()
        architectures = Schema.Architectures()
        release_path = url.split("/dists/", 1)[-1]

        super().__init__(
            name=name,
            has_release=False,
            components=components,
            architectures=architectures,
            release_path=release_path,
            metadata=None,
        )

        self.url = url
        self.upstream = upstream
        self["uri"] = url

    def _parse_release_headers(self, data: str) -> Dict[str, Any]:
        ## @brief Extract top-level fields, components, and architectures
        ##        from a Release body.
        ## @param data  Raw Release text.
        ## @return Dict with keys ``components``, ``architectures``, ``fields``.

        components = Schema.Components()
        architectures = Schema.Architectures()
        fields: Dict[str, str] = {}

        for line in data.splitlines():
            if not line:
                continue
            if line.startswith("Components:"):
                for comp in line.split(":", 1)[1].strip().split():
                    components.append(Schema.Component(name=comp))
            elif line.startswith("Architectures:"):
                for arch in line.split(":", 1)[1].strip().split():
                    architectures.append(Schema.Architecture(name=arch))
            else:
                if (
                    line.startswith("MD5Sum:")
                    or line.startswith("SHA1:")
                    or line.startswith("SHA256:")
                ):
                    continue

                if ":" in line:
                    key, value = line.split(":", 1)
                    fields[key.strip()] = value.strip()

        return {
            "components": components,
            "architectures": architectures,
            "fields": fields,
        }

    def _parse_hash_sections(self, data: str) -> Dict[str, List[Dict[str, Any]]]:
        ## @brief Parse all known hash sections from a Release body.
        ##
        ## Each entry is a dict with keys ``algorithm``, ``checksum``,
        ## ``size``, ``path``.
        ##
        ## @param data  Raw Release text.
        ## @return Dict mapping section name to list of entries.

        sections: Dict[str, List[Dict[str, Any]]] = {}
        for section in ("MD5Sum", "SHA1", "SHA256"):
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
                        "algorithm": section,
                        "checksum": checksum,
                        "size": size,
                        "path": path,
                    }
                )

            if entries:
                sections[section] = entries

        return sections

    def on_parse(self, *, config: Optional[Dict[str, Any]] = None) -> None:
        ## @brief Populate the Distribution and create child Release.
        ##
        ## In scan mode, fetches the Release body via HTTP, parses headers
        ## into ``self["metadata"]``, and creates a child Release node
        ## with the body pre-cached.
        ##
        ## In sync mode, skips the HTTP fetch (the Release will fetch
        ## through the pool when its own ``on_parse()`` runs) and creates
        ## the child Release directly with the correct path and dest.
        ##
        ## Architecture/component filters from the parent Repo are set on
        ## the child Release for index filtering.
        ##
        ## @param config  Optional config dict (suite/arch/component filters).
        ## @return None

        repo = getattr(self, "_repo", None)

        if self._repo_vars is not None and self._repo_vars.sync_mode:
            # Sync mode: Release downloads through the pool - no fetch needed here
            dest = repo.get("dest", "") if repo else ""
            from .release import Release as AptRelease
            self.release = AptRelease(
                url=self.get("uri"),
                upstream=self.upstream,
                suite=self.name,
                dest=dest,
            )
            self.release._repo_vars = self._repo_vars
            if repo is not None:
                self.release._arch_filter = getattr(repo, "_arch_filter", None)
                self.release._comp_filter = getattr(repo, "_comp_filter", None)
            return

        # Scan mode: fetch Release body via HTTP, parse headers, create Release
        try:
            text_bytes = self.fetch(uri=self.get("uri"), config=config)
        except ExceptionMsg:
            return
        if text_bytes is None:
            return
        text = text_bytes.decode("utf-8", errors="replace")

        # Parse Release file headers to extract components, architectures, fields
        parsed = self._parse_release_headers(text)
        components = parsed.get("components", Schema.Components())
        architectures = parsed.get("architectures", Schema.Architectures())
        release_fields = parsed.get("fields", {})

        # Parse hash sections (MD5Sum, SHA1, SHA256) for index integrity
        hash_sections = self._parse_hash_sections(text)

        release_metadata = self.Metadata(
            fields=release_fields,
            components=components,
            architectures=architectures,
            hash_sections=hash_sections if hash_sections else None,
            body=Schema.Release.Digest(text=text),
        )

        # Store parsed results back into the Distribution node
        self["has_release"] = True
        self["components"] = components
        self["architectures"] = architectures
        self["metadata"] = release_metadata

        # Create child Release - pre-cache the body so the Release
        # does not re-fetch it from upstream.
        from .release import Release as AptRelease

        self.release = AptRelease(
            url=self.get("uri"),
            upstream=self.upstream,
            suite=self.name,
        )
        self.release._repo_vars = self._repo_vars
        self.release._cache = text_bytes
        # Pass architecture/component filters from the parent Repo
        if repo is not None:
            self.release._arch_filter = getattr(repo, "_arch_filter", None)
            self.release._comp_filter = getattr(repo, "_comp_filter", None)
