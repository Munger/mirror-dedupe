## @file discovery.py
##
## @brief Suite discovery strategies for APT repositories.
##
## Consolidates all APT-specific upstream probing logic:
## HTML directory listing BFS, child prefix resolution, and
## codename-based fallback probing.
##
## @copyright Copyright (c) 2026 Tim Hosking
## @see https://github.com/munger
## @par Licence: MIT


from collections import OrderedDict
from threading import Lock
from typing import Dict, Iterable, List, Optional, Tuple

from mirror_dedupe.lib.codenames import apt_codenames
from mirror_dedupe.lib.html_helpers import build_url, extract_href
from mirror_dedupe.lib.log import log
from mirror_dedupe.schema.mdnode import MDNode as Node
from .release import strip_pgp_wrapper


class _LRUCache:
    ## @brief Bounded LRU cache with thread safety.
    ##
    ## Drops oldest entries when maxsize is exceeded.  Supports ``.get()``
    ## and ``__setitem__`` for drop-in replacement of module-level dicts
    ## that would otherwise leak memory over long-running sessions.
    ##
    ## @param maxsize  Maximum number of entries (default 128).

    def __init__(self, maxsize: int = 128):
        self._maxsize = maxsize
        self._lock = Lock()
        self._data: OrderedDict = OrderedDict()

    def get(self, key: object) -> object | None:
        ## @brief Return cached value or ``None``.
        ##
        ## Promotes *key* to MRU position on hit.
        ##
        ## @param key  Cache key.
        ## @return Cached value or ``None``.

        with self._lock:
            if key in self._data:
                self._data.move_to_end(key)
                return self._data[key]
            return None

    def __setitem__(self, key: object, value: object) -> None:
        ## @brief Insert or update *key* with *value*.
        ##
        ## Evicts the LRU entry when the cache exceeds maxsize.
        ##
        ## @param key    Cache key.
        ## @param value  Value to cache.

        with self._lock:
            if key in self._data:
                self._data.move_to_end(key)
            self._data[key] = value
            while len(self._data) > self._maxsize:
                self._data.popitem(last=False)


# Bounded LRU caches.  Formerly bare module-level dicts that grew unbounded
# over the process lifetime.  BFS cache keyed by (upstream, index_root,
# anchor, max_depth); Release text cache keyed by (upstream, index_root, path).
_bfs_cache: _LRUCache = _LRUCache(maxsize=128)
_release_text_cache: _LRUCache = _LRUCache(maxsize=512)


def looks_like_release(body: str) -> bool:
    ## @brief Lightweight sanity check for Debian/Ubuntu Release text.
    ##
    ## Only cares that the body looks plausibly like a suite Release,
    ## not that it is fully valid.  Treat as a cheap heuristic, not a
    ## full validator.
    ##
    ## @param body  Raw Release text.
    ## @return True if at least two of the expected Release markers
    ##         (Suite, Codename, Components, Architectures) are present.

    markers = 0
    for line in body.splitlines():
        if line.startswith("Suite:") or line.startswith("Codename:"):
            markers += 1
        if line.startswith("Components:") or line.startswith("Architectures:"):
            markers += 1
        if markers >= 2:
            return True
    return False


def _iter_href_names(lines: Iterable[str], *, dirs_only: bool = False) -> Iterable[str]:
    ## @brief Yield normalised names from href attributes in directory listings.
    ##
    ## Used for both top-level ``/dists/`` discovery and nested walks
    ## under ``/dists/<path>/``.  Deliberately ignores absolute URLs,
    ## absolute server paths, Apache sort links, parent/self entries,
    ## and URI scheme markers.
    ##
    ## @param lines      Iterable of HTML lines.
    ## @param dirs_only  If True, only yield names ending with ``/``.
    ## @yield Normalised name strings.

    for line in lines:
        raw = extract_href(line)
        if not raw:
            continue

        if dirs_only and not raw.endswith("/"):
            continue

        if raw.startswith("http://") or raw.startswith("https://") or "?" in raw:
            # Skip absolute URLs, external links, and Apache sort
            # query params (?C=N&amp;O=D).  Only relative paths matter.
            continue

        if raw.startswith("/"):
            # Skip absolute server paths (e.g. /.headers/main.css,
            # /debian/).  These are not directory entries.
            continue

        # Extract the last path segment from the href.  In Apache-style
        # directory listings, hrefs are bare names ("bionic/") or nested
        # paths ("/dists/bionic/"), not full URLs.
        name = raw.strip("/")
        if "/" in name:
            name = name.split("/")[-1]

        if name in (".", "..", "dists"):
            # Skip parent/self directory links and the dists root
            # itself (already being parsed, not a candidate).
            continue
        if "://" in name or ":" in name:
            # Skip any remaining URI-scheme fragments that made it
            # past the absolute-URL check (rare but possible when
            # path stripping exposes a colon).
            continue

        if name:
            yield name


def discover_distribution_paths(
    upstream: str,
    *,
    index_root: str = "dists",
    anchor: str = "Release",
    max_depth: int = 3,
    root_html: str | None = None,
    _allow_child_prefix: bool = True,
) -> List[Tuple[str, str]]:
    ## @brief Walk ``index_root`` and return discovered ``(path, upstream)`` pairs.
    ##
    ## Each tuple is ``(distribution_path, effective_upstream)`` where
    ## *effective_upstream* is the URL under which ``/dists/`` was
    ## successfully browsed for that specific path (may differ per path
    ## when multiple child prefixes exist).  Callers should use the per-path
    ## *effective_upstream* when constructing Release URLs.
    ##
    ## A *distribution_path* is any relative path ``P`` such that
    ## ``index_root/P/anchor`` looks like a valid APT Release according
    ## to ``looks_like_release``.  Returns an empty list if no
    ## distributions are found.
    ##
    ## @param upstream        Base upstream URL.
    ## @param index_root      Root directory for suites (default ``"dists"``).
    ## @param anchor          Anchor filename (default ``"Release"``).
    ## @param max_depth       Maximum BFS depth.
    ## @param root_html       Pre-fetched HTML of the index root; fetched
    ##                        from upstream if omitted.
    ## @param _allow_child_prefix  Internal flag to allow one level of
    ##                             child prefix resolution.
    ## @return List of ``(path, effective_upstream, anchor_filename)`` tuples.

    def _fetch_text(url: str) -> str | None:
        ## @brief Fetch a URL's body as decoded UTF-8 text.
        ## @param url  Fully-qualified URL.
        ## @return Decoded text, or ``None`` if the fetch failed.
        raw = Node.probe_url(url)
        if raw is not None:
            return raw.decode("utf-8", errors="replace")
        return None

    cache_key = (upstream, index_root, max_depth)
    cached = _bfs_cache.get(cache_key)
    if cached is not None:
        return list(cached)

    if root_html is None:
        dists_url = build_url(upstream, index_root)
        log(f"[apt] probing dists index: {dists_url}")
        root_html = _fetch_text(dists_url)

    if not root_html:
        log("[apt] no HTML content at /dists/; giving up on suite discovery")
        # Child prefix fallback: /dists/ may sit under a subdirectory (e.g. nodesource)
        if _allow_child_prefix:
            base_html = _fetch_text(upstream)
            if base_html:
                result: List[Tuple[str, str]] = []
                for child in _iter_href_names(base_html.splitlines(), dirs_only=True):
                    child_upstream = build_url(upstream, child)
                    child_results = discover_distribution_paths(
                        child_upstream,
                        index_root=index_root,
                        anchor=anchor,
                        max_depth=max_depth,
                        root_html=None,
                        _allow_child_prefix=False,
                    )
                    if child_results:
                        result.extend(child_results)
                if result:
                    _bfs_cache[cache_key] = list(result)
                    return result
        _bfs_cache[cache_key] = []
        return []

    lines = root_html.splitlines()
    log(f"[apt] /dists/ HTML line count: {len(lines)}")

    suites: List[str] = []
    # First pass: accept all relative hrefs.  Some Apache-style listings
    # omit trailing slashes on directory entries, so dirs_only=False is
    # used to catch everything, with file-like names filtered in _iter_href_names.
    for name in _iter_href_names(lines, dirs_only=False):
        if name and name not in suites:
            log(f"[apt] discovered suite under /dists: {name}")
            suites.append(name)

    if not suites:
        log(
            "[apt] no suites discovered in /dists/ HTML; attempting dirs-only fallback",
        )
        for name in _iter_href_names(lines, dirs_only=True):
            if name and name not in suites:
                log(f"[apt] discovered suite under /dists (dirs-only): {name}")
                suites.append(name)

    if not suites:
        log("[apt] no suites discovered in /dists/ HTML; giving up on suite discovery")
        _bfs_cache[cache_key] = []
        return []

    queue: List[tuple[str, int]] = [(name, 1) for name in suites]
    seen_paths = {name for name in suites}

    log(f"[apt] total top-level suites discovered: {len(queue)}")

    discovered_paths: List[Tuple[str, str]] = []

    # BFS walk suites under /dists/ up to max_depth
    while queue:
        path, depth = queue.pop(0)

        # Probe InRelease first (self-verifying); fall back to Release.
        # Cache the full upstream content — PGP wrapper intact.  Stripping
        # is a parse step, not a storage step.
        path_anchor = None
        path_full = None

        ir_text = _fetch_text(build_url(upstream, index_root, path, "InRelease"))
        if ir_text and looks_like_release(strip_pgp_wrapper(ir_text)):
            path_anchor = "InRelease"
            path_full = ir_text

        if path_anchor is None:
            rel_text = _fetch_text(build_url(upstream, index_root, path, "Release"))
            if rel_text and looks_like_release(rel_text):
                path_anchor = "Release"
                path_full = rel_text

        if path_anchor:
            _release_text_cache[(upstream, index_root, path)] = path_full
            log(f"  {path}: fetched ({path_anchor})")
            discovered_paths.append((path, path_anchor))
            continue  # Leaf found - no need to descend
        log(f"  {path}: not found")

        if depth >= max_depth:
            continue

        # Fetch directory listing to discover nested sub-suites
        index_url = build_url(upstream, index_root, path)
        index_html = _fetch_text(index_url)
        if not index_html:
            continue

        for child_name in _iter_href_names(index_html.splitlines(), dirs_only=True):
            child_path = f"{path}/{child_name}"
            if child_path in seen_paths:
                continue

            log(f"[apt] discovered nested candidate under /dists: {child_path}")
            seen_paths.add(child_path)
            queue.append((child_path, depth + 1))

    result = [(path, upstream, anchor_fn) for path, anchor_fn in discovered_paths]
    _bfs_cache[cache_key] = list(result)
    return result


def _known_suites_to_probe() -> List[str]:
    ## @brief Return the list of suite names to probe in fallback order.
    ##
    ## Starts with ``stable`` (a common alias used by third-party repos)
    ## followed by all known Debian/Ubuntu codenames.  Order is stable
    ## first for fast detection in ``probe_any_suite()``.
    ##
    ## @return Ordered list of suite names (``stable`` first, then codenames).

    try:
        known = apt_codenames()
    except Exception:
        known = []
    result: List[str] = ["stable"]
    result.extend(known)
    return result


def probe_any_suite(
    upstream: str,
    index_root: str = "dists",
    anchor: str = "Release",
    extra_suites: Optional[List[str]] = None,
) -> bool:
    ## @brief Return ``True`` if a known suite has a valid Release at *upstream*.
    ##
    ## Short-circuits on the first confirmed match.  Intended for
    ## type detection (``is_this_yours``) where a single hit is
    ## sufficient.  Tries *extra_suites* (from config ``distributions``)
    ## before falling back to the static known-suite list.
    ##
    ## @param upstream     Upstream URL.
    ## @param index_root   Index root directory (default ``"dists"``).
    ## @param anchor       Anchor filename (default ``"Release"``).
    ## @param extra_suites Optional suite names from config to probe first.
    ## @return True if any suite has a valid Release file.

    suites_to_try: list[str] = list(extra_suites or [])
    suites_to_try.extend(_known_suites_to_probe())
    for suite in suites_to_try:
        body = None
        for filename in ("InRelease", anchor):
            url = build_url(upstream, index_root, suite, filename)
            try:
                raw = Node.probe_url(url)
                if raw is None:
                    continue
                text = raw.decode("utf-8", errors="replace")
                candidate = strip_pgp_wrapper(text) if filename == "InRelease" else text
                if looks_like_release(candidate):
                    body = candidate
                    break
            except Exception:
                continue

        if not body:
            continue

        # Verify Suite/Codename matches — guards against redirect false positives.
        claimed = None
        for line in body.splitlines():
            if line.startswith("Suite:") or line.startswith("Codename:"):
                val = line.split(":", 1)[1].strip()
                if val == suite:
                    return True
                if claimed is None:
                    claimed = val
        if claimed == suite:
            return True

    return False


def probe_fallback_suites(
    upstream: str,
    index_root: str = "dists",
    anchor: str = "Release",
) -> List[str]:
    ## @brief Probe all known suite names and return confirmed matches.
    ##
    ## Fallback strategy for repos whose ``/dists/`` directory is not
    ## browsable (e.g. S3-hosted repos like Brave).  Probes ``stable``
    ## plus all known APT codenames.  Each probe result is printed to
    ## stderr so the user can see progress during the sequential scan.
    ##
    ## @param upstream    Upstream URL.
    ## @param index_root  Index root directory (default ``"dists"``).
    ## @param anchor      Anchor filename (default ``"Release"``).
    ## @return List of suite names confirmed to have valid Release files.

    candidates: List[Tuple[str, str]] = []
    for suite in _known_suites_to_probe():
        suite_anchor = None
        suite_full = None
        suite_body = None

        try:
            ir_bytes = Node.probe_url(build_url(upstream, index_root, suite, "InRelease"))
            if ir_bytes:
                ir_text = ir_bytes.decode("utf-8", errors="replace")
                stripped = strip_pgp_wrapper(ir_text)
                if looks_like_release(stripped):
                    suite_anchor = "InRelease"
                    suite_full = ir_text
                    suite_body = stripped
        except Exception:
            pass

        if suite_anchor is None:
            try:
                rel_bytes = Node.probe_url(build_url(upstream, index_root, suite, anchor))
                if rel_bytes:
                    rel_text = rel_bytes.decode("utf-8", errors="replace")
                    if looks_like_release(rel_text):
                        suite_anchor = "Release"
                        suite_full = rel_text
                        suite_body = rel_text
            except Exception:
                pass

        if not suite_anchor:
            log(f"  {suite}: not found")
            continue

        # Confirm the file's Suite/Codename matches what we probed —
        # guards against false positives from redirects or child prefixes.
        claimed = None
        for line in suite_body.splitlines():
            if line.startswith("Suite:") or line.startswith("Codename:"):
                val = line.split(":", 1)[1].strip()
                if val == suite:
                    claimed = suite
                    break
                if claimed is None:
                    claimed = val

        if claimed == suite:
            _release_text_cache[(upstream, index_root, suite)] = suite_full
            log(f"  {suite}: found ({suite_anchor})")
            candidates.append((suite, suite_anchor))
        else:
            log(f"  {suite}: not found")

    return candidates
