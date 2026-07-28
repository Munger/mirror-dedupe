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


from fnmatch import fnmatch
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
        excl_pkgs = getattr(self, "_exclude_packages", None)
        excl_paths = getattr(self, "_exclude_paths", None)

        def _make_pkg(filename: str, sha256_val: str, size_int: int, pkg_name: str = ""):
            ## @brief Construct a Package node with exclusion checks.
            if filename.startswith("./"):
                filename = filename[2:]
            pkg_uri = f"{base.rstrip('/')}/{quote(filename, safe='/')}" if base else ""
            pkg_path = f"{dest}/{filename}" if dest else filename
            if excl_pkgs and any(fnmatch(pkg_name, p) for p in excl_pkgs):
                return None
            if excl_paths and any(fnmatch(pkg_path, p) for p in excl_paths):
                return None
            pkg = Package(
                path=pkg_path,
                hash=sha256_val,
                size=size_int,
                uri=pkg_uri,
            )
            pkg._repo_vars = self._repo_vars
            return pkg

        def _parse_stanza(stanza_lines: List[str]):
            ## @brief Parse a stanza, yielding Package nodes.
            ##
            ## Handles two formats:
            ## - Binary Packages: flat ``Filename``/``SHA256``/``Size`` fields → 1 Package.
            ## - Source Packages: ``Directory`` + ``Checksums-Sha256`` multi-line
            ##   block → 1 Package per source file (.dsc, .orig.tar.*, .debian.tar.xz).

            stanza: Dict[str, str] = {}
            blocks: Dict[str, List[tuple]] = {}
            current_block: str | None = None

            for sl in stanza_lines:
                if sl and sl[0] in (" ", "\t"):
                    if current_block:
                        parts = sl.split()
                        if len(parts) >= 3:
                            try:
                                blocks[current_block].append(
                                    (parts[0], int(parts[1]), parts[2])
                                )
                            except ValueError:
                                pass
                    continue
                current_block = None
                if ":" in sl:
                    k, v = sl.split(":", 1)
                    k = k.strip()
                    v = v.strip()
                    stanza[k] = v
                    if k in ("Checksums-Sha256", "Files", "Checksums-Sha1"):
                        current_block = k
                        blocks.setdefault(k, [])

            directory = stanza.get("Directory")
            checksums = blocks.get("Checksums-Sha256")
            pkg_name = stanza.get("Package", "")

            if directory and checksums:
                for _hash, size_int, filename in checksums:
                    full_path = f"{directory}/{filename}"
                    pkg = _make_pkg(full_path, _hash, size_int, pkg_name)
                    if pkg:
                        yield pkg
                return

            filename = stanza.get("Filename")
            sha256_val = stanza.get("SHA256")
            size_str = stanza.get("Size")
            if filename and sha256_val and size_str:
                try:
                    size_int = int(size_str)
                except ValueError:
                    return
                pkg = _make_pkg(filename, sha256_val, size_int, pkg_name)
                if pkg:
                    yield pkg

        def _generate():
            ## @brief True generator: yield Package nodes per parsed stanza.
            ##
            ## Packages are yielded as each stanza is parsed so the
            ## coordinator receives them one at a time rather than receiving
            ## all packages from this index in a single burst.
            ##
            ## @yield Package child nodes.
            stanza_lines: List[str] = []
            for line in self._iter_lines(data):
                if not line and stanza_lines:
                    yield from _parse_stanza(stanza_lines)
                    stanza_lines = []
                elif line:
                    stanza_lines.append(line)

            if stanza_lines:
                yield from _parse_stanza(stanza_lines)

        return _generate()
