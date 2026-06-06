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

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Tuple

from mirror_dedupe.lib.codenames import apt_codenames
from mirror_dedupe.lib.html_helpers import build_url, extract_href
from mirror_dedupe.lib.log import log
from mirror_dedupe.schema.node import Node


PROBE_TIMEOUT = 5  ## @brief Per-URL timeout (seconds) for codename probes.

# Cache: discovered distributions by (upstream, index_root, anchor, max_depth).
# Avoids a duplicate BFS when is_this_yours() probes the same URL the parser
# will later crawl.  Key does not include config/root_html since those are
# derived from upstream and don't affect which suites exist upstream.
_bfs_cache: Dict[Tuple[str, str, str, int], List[Tuple[str, str]]] = {}

# Cache: Release text by (upstream, index_root, path).  Populated during BFS
# probe and consumed by DistributionsParser so the same Release file is never
# fetched more than once per scan.
_release_text_cache: Dict[Tuple[str, str, str], str] = {}


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
    ## Apache sort links, parent/self entries, and URI scheme markers.
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
            continue

        name = raw.strip("/")
        if "/" in name:
            name = name.split("/")[-1]

        if name in (".", "..", "dists"):
            continue
        if "://" in name or ":" in name:
            continue

        if name:
            yield name


def discover_distribution_paths(
    upstream: str,
    *,
    config: Optional[Dict[str, Any]] = None,
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
    ## @param config          Optional network config dict (ipv6_ok, timeout).
    ## @param index_root      Root directory for suites (default ``"dists"``).
    ## @param anchor          Anchor filename (default ``"Release"``).
    ## @param max_depth       Maximum BFS depth.
    ## @param root_html       Pre-fetched HTML of the index root; fetched
    ##                        from upstream if omitted.
    ## @param _allow_child_prefix  Internal flag to allow one level of
    ##                             child prefix resolution.
    ## @return List of ``(path, effective_upstream)`` tuples.

    def _fetch_text(url: str) -> str | None:
        ## @brief Fetch a URL's body as decoded UTF-8 text.
        ## @param url  Fully-qualified URL.
        ## @return Decoded text, or ``None`` if the fetch failed.
        raw = Node.probe_url(url, config)
        if raw is not None:
            return raw.decode("utf-8", errors="replace")
        return None

    cache_key = (upstream, index_root, anchor, max_depth)
    cached = _bfs_cache.get(cache_key)
    if cached is not None:
        return list(cached)

    if root_html is None:
        dists_url = build_url(upstream, index_root)
        log(f"[apt] probing dists index: {dists_url}", level="DEBUG")
        root_html = _fetch_text(dists_url)

    if not root_html:
        log("[apt] no HTML content at /dists/; giving up on suite discovery", level="DEBUG")
        # Child prefix fallback: /dists/ may sit under a subdirectory (e.g. nodesource)
        if _allow_child_prefix:
            base_html = _fetch_text(upstream)
            if base_html:
                result: List[Tuple[str, str]] = []
                for child in _iter_href_names(base_html.splitlines(), dirs_only=True):
                    child_upstream = build_url(upstream, child)
                    child_results = discover_distribution_paths(
                        child_upstream,
                        config=config,
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
    log(f"[apt] /dists/ HTML line count: {len(lines)}", level="DEBUG")

    suites: List[str] = []
    for name in _iter_href_names(lines, dirs_only=False):
        if name and name not in suites:
            log(f"[apt] discovered suite under /dists: {name}", level="DEBUG")
            suites.append(name)

    if not suites:
        log(
            "[apt] no suites discovered in /dists/ HTML; attempting dirs-only fallback",
            level="DEBUG",
        )
        for name in _iter_href_names(lines, dirs_only=True):
            if name and name not in suites:
                log(f"[apt] discovered suite under /dists (dirs-only): {name}", level="DEBUG")
                suites.append(name)

    if not suites:
        log("[apt] no suites discovered in /dists/ HTML; giving up on suite discovery", level="DEBUG")
        _bfs_cache[cache_key] = []
        return []

    queue: List[tuple[str, int]] = [(name, 1) for name in suites]
    seen_paths = {name for name in suites}

    log(f"[apt] total top-level suites discovered: {len(queue)}", level="DEBUG")

    discovered_paths: List[str] = []

    # BFS walk suites under /dists/ up to max_depth
    while queue:
        path, depth = queue.pop(0)

        # Each candidate gets a Release probe before descending further
        release_url = build_url(upstream, index_root, path, anchor)
        text = _fetch_text(release_url)
        if text and looks_like_release(text):
            _release_text_cache[(upstream, index_root, path)] = text
            log(f"  {path}: fetched")
            discovered_paths.append(path)
            continue  # Leaf found — no need to descend
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

            log(f"[apt] discovered nested candidate under /dists: {child_path}", level="DEBUG")
            seen_paths.add(child_path)
            queue.append((child_path, depth + 1))

    result = [(path, upstream) for path in discovered_paths]
    _bfs_cache[cache_key] = list(result)
    return result


def _known_suites_to_probe() -> List[str]:
    ## @brief Return the list of suite names to probe in fallback order.
    ##
    ## Starts with ``stable`` (a common alias used by third-party repos)
    ## followed by all known Debian/Ubuntu codenames.  Order is stable
    ## first for fast detection in ``probe_any_suite()``.

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
    config: Optional[Dict[str, Any]] = None,
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
    ## @param config       Optional network config dict (ipv6_ok, timeout).
    ## @param extra_suites Optional suite names from config to probe first.
    ## @return True if any suite has a valid Release file.

    short_config = dict(config or {})
    short_config.setdefault("timeout", PROBE_TIMEOUT)

    suites_to_try: list[str] = list(extra_suites or [])
    suites_to_try.extend(_known_suites_to_probe())
    for suite in suites_to_try:
        rel_url = build_url(upstream, index_root, suite, anchor)
        try:
            text_bytes = Node.probe_url(rel_url, short_config)
            if text_bytes is None:
                continue
            text = text_bytes.decode("utf-8", errors="replace")
        except Exception:
            text = None

        if text and looks_like_release(text):
            claimed = None
            for line in text.splitlines():
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
    config: Optional[Dict[str, Any]] = None,
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
    ## @param config      Optional network config dict (ipv6_ok, timeout).
    ## @return List of suite names confirmed to have valid Release files.

    short_config = dict(config or {})
    short_config.setdefault("timeout", PROBE_TIMEOUT)

    candidates: List[str] = []
    for suite in _known_suites_to_probe():
        rel_url = build_url(upstream, index_root, suite, anchor)
        try:
            text_bytes = Node.probe_url(rel_url, short_config)
            if text_bytes is None:
                log(f"  {suite}: not found")
                continue
            text = text_bytes.decode("utf-8", errors="replace")
        except Exception:
            text = None

        if text and looks_like_release(text):
            claimed = None
            for line in text.splitlines():
                if line.startswith("Suite:") or line.startswith("Codename:"):
                    val = line.split(":", 1)[1].strip()
                    if val == suite:
                        claimed = suite
                        break
                    if claimed is None:
                        claimed = val
            if claimed == suite:
                log(f"  {suite}: found")
                candidates.append(suite)
                continue

        log(f"  {suite}: not found")

    return candidates
