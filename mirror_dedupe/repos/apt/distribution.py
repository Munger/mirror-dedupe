## @file distribution.py
##
## @brief APT-specific Distribution node with Release parsing.
##
## ``Distribution`` extends ``Loadable`` and ``Schema.Distribution``,
## adding the ability to fetch and parse a Release file to populate
## components, architectures, fields, and hash sections.
##
## @copyright Copyright (c) 2026 Tim Hosking
## @see https://github.com/munger
## @par Licence: MIT

from __future__ import annotations

from typing import Any, Dict, List

from mirror_dedupe import schema as Schema
from mirror_dedupe.lib.loadable import Loadable


class Distribution(Loadable, Schema.Distribution):
    ## @brief APT-specific Distribution node that can parse its own Release.
    ##
    ## Construct with ``url=`` (pointing at the Release file), an HTTP
    ## client, and the usual Distribution fields; then call ``parse()`` to
    ## populate its metadata from the Release body.

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
        http_client: Any,
        upstream: str,
        name: str,
    ) -> None:
        ## @brief Construct an APT Distribution bound to a Release URL and HTTP client.
        ##
        ## Initialises the underlying ``Schema.Distribution`` with empty
        ## Components/Architectures and wires an internal loader for the
        ## provided URL.
        ##
        ## @param url          URL pointing at the Release file.
        ## @param http_client  HTTPClient for fetching.
        ## @param upstream     Base upstream URL.
        ## @param name         Distribution name (e.g. ``"noble"``).

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

        def load(_url: str) -> str:
            return http_client.fetch_text(_url)

        self._load_text = load
        self.url = url
        self.upstream = upstream

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

    def parse(self) -> "Distribution":
        ## @brief Fetch and parse this distribution's Release, populating metadata.
        ## @return This Distribution (for chaining).

        text = self._load_text(self.url)
        if not text:
            return self

        parsed = self._parse_release_headers(text)
        components = parsed.get("components", Schema.Components())
        architectures = parsed.get("architectures", Schema.Architectures())
        release_fields = parsed.get("fields", {})

        hash_sections = self._parse_hash_sections(text)

        release_metadata = self.Metadata(
            fields=release_fields,
            components=components,
            architectures=architectures,
            hash_sections=hash_sections if hash_sections else None,
            body=Schema.Release.Digest(text=text),
        )

        self["has_release"] = True
        self["components"] = components
        self["architectures"] = architectures
        self["metadata"] = release_metadata

        return self
