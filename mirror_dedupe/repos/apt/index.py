## @file index.py
##
## @brief APT-specific Index subclass with RFC822 Packages parsing.
##
## ``AptIndex`` extends ``Schema.Index``, replacing the no-op
## ``_parse_packages`` with a stanza-based parser that handles the
## Debian/Ubuntu ``Packages`` / ``Sources`` format (RFC822-style
## key-value blocks separated by blank lines).
##
## @copyright Copyright (c) 2026 Tim Hosking
## @see https://github.com/munger
## @par Licence: MIT

from __future__ import annotations

from typing import Any, Dict

from mirror_dedupe import schema as Schema
from mirror_dedupe.schema.package import Package, Packages


class AptIndex(Schema.Index):
    ## @brief APT-specific Index that parses RFC822 stanza text.

    def __init__(self, *, dest: str = "", **kwargs: Any) -> None:
        ## @brief Construct an APT Index with a dest prefix for Package paths.
        ## @param dest    Repo dest prefix (empty in scan mode).
        ## @param kwargs  Forwarded to ``Schema.Index.__init__``.
        super().__init__(**kwargs)
        self._dest = dest

    def _parse_packages(self, text: str, uri: str = "") -> Packages:
        ## @brief Parse RFC822 stanzas into Package children.
        ##
        ## Skips non-package/sources indices, parses ``Filename``,
        ## ``SHA256``, and ``Size`` from each stanza, and constructs
        ## ``Package`` nodes with a full download URI and repo-root-relative
        ## path (prefixed with ``self._dest``).
        ##
        ## @param text  Decompressed Packages text.
        ## @param uri   The URI of this index.
        ## @return A ``Packages`` NodeList.

        kind = self.get("kind")
        if kind not in ("packages", "sources"):
            return Packages()

        dest = self._dest
        base = uri.split("/dists/", 1)[0] if "/dists/" in uri else ""
        packages = Packages()

        for stanza_text in text.split("\n\n"):
            stanza_text = stanza_text.strip()
            if not stanza_text:
                continue

            stanza: Dict[str, str] = {}
            for line in stanza_text.split("\n"):
                if line and line[0] in (" ", "\t"):
                    continue
                if ":" in line:
                    k, v = line.split(":", 1)
                    stanza[k.strip()] = v.strip()

            filename = stanza.get("Filename")
            sha256 = stanza.get("SHA256")
            size = stanza.get("Size")

            if filename and sha256 and size:
                try:
                    size_int = int(size)
                except ValueError:
                    continue
                pkg_uri = f"{base.rstrip('/')}/{filename}" if base else ""
                pkg_path = f"{dest}/{filename}" if dest else filename
                packages.append(Package(
                    path=pkg_path,
                    hash=sha256,
                    size=size_int,
                    uri=pkg_uri,
                ))

        return packages
