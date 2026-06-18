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

from typing import Any, Dict, List, Optional
from urllib.parse import quote

from mirror_dedupe import schema as Schema
from mirror_dedupe.schema.package import Package, Packages


class AptIndex(Schema.Index):
    ## @brief APT-specific Index that parses RFC822 stanza text.

    def __init__(self, *, dest: str = "", size: int = 0, **kwargs: Any) -> None:
        ## @brief Construct an APT Index with a dest prefix for Package paths.
        ## @param dest    Repo dest prefix (empty in scan mode).
        ## @param size    File size in bytes (0 if unknown).
        ## @param kwargs  Forwarded to ``Schema.Index.__init__``.
        super().__init__(**kwargs, size=size)
        self._dest = dest

    def stream(self, data: Optional[bytes] = None):
        ## @brief Yield Package children by parsing RFC822 stanzas.
        ##
        ## Reads this Index file (from *data* bytes or disk) and yields
        ## ``Package`` nodes for each stanza that contains a valid
        ## ``Filename``, ``SHA256``, and ``Size`` field.
        ##
        ## ``self.packages`` is set to the full ``Packages`` NodeList so
        ## that ``_tree_iter()`` can walk the children for stats/sweep.
        ##
        ## @param data  Optional bytes to parse (scan mode).
        ## @yield Package child nodes.

        kind = self.get("kind")
        if kind not in ("packages", "sources"):
            return iter([])

        dest = self._dest
        uri = self.get("uri", "")
        base = uri.split("/dists/", 1)[0] if "/dists/" in uri else ""

        def _generate():
            ## @brief True generator: yield one Package per parsed stanza.
            ##
            ## Packages are yielded as each stanza is parsed so the
            ## coordinator receives them one at a time rather than receiving
            ## all packages from this index in a single burst.  The
            ## pre-built ``packages`` list and ``self.packages`` assignment
            ## are gone - stats are accumulated by ``SyncStats`` in
            ## ``Node.sync()`` and stale paths are discarded there too, so
            ## nothing needs to walk a retained package list after streaming.
            ##
            ## @yield Package child nodes.
            stanza_lines: List[str] = []
            for line in self._iter_lines(data):
                if not line and stanza_lines:
                    stanza: Dict[str, str] = {}
                    for sl in stanza_lines:
                        if sl and sl[0] in (" ", "\t"):
                            continue
                        if ":" in sl:
                            k, v = sl.split(":", 1)
                            stanza[k.strip()] = v.strip()

                    filename = stanza.get("Filename")
                    sha256_val = stanza.get("SHA256")
                    size_str = stanza.get("Size")

                    if filename and sha256_val and size_str:
                        try:
                            size_int = int(size_str)
                        except ValueError:
                            stanza_lines = []
                            continue
                        pkg_uri = f"{base.rstrip('/')}/{quote(filename, safe='/')}" if base else ""
                        pkg_path = f"{dest}/{filename}" if dest else filename
                        pkg = Package(
                            path=pkg_path,
                            hash=sha256_val,
                            size=size_int,
                            uri=pkg_uri,
                        )
                        pkg._repo_vars = self._repo_vars
                        yield pkg
                    stanza_lines = []
                elif line:
                    stanza_lines.append(line)

            if stanza_lines:
                stanza = {}
                for sl in stanza_lines:
                    if sl and sl[0] in (" ", "\t"):
                        continue
                    if ":" in sl:
                        k, v = sl.split(":", 1)
                        stanza[k.strip()] = v.strip()
                filename = stanza.get("Filename")
                sha256_val = stanza.get("SHA256")
                size_str = stanza.get("Size")
                if filename and sha256_val and size_str:
                    try:
                        size_int = int(size_str)
                    except ValueError:
                        return
                    pkg_uri = f"{base.rstrip('/')}/{filename}" if base else ""
                    pkg_path = f"{dest}/{filename}" if dest else filename
                    pkg = Package(
                        path=pkg_path,
                        hash=sha256_val,
                        size=size_int,
                        uri=pkg_uri,
                    )
                    pkg._repo_vars = self._repo_vars
                    yield pkg

        return _generate()
