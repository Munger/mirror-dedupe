## @file utils.py
##
## @brief Shared helpers for APT repo detection and parsing.
##
## Centralises common heuristics such as ``looks_like_release`` and
## ``discover_distribution_paths`` so that ``Apt`` and its helpers share
## a single implementation for discovering ``dists/**/Release``
## entry-points.
##
## @copyright Copyright (c) 2026 Tim Hosking
## @see https://github.com/munger
## @par Licence: MIT

from __future__ import annotations

from typing import Any, Iterable, List
import sys

from mirror_dedupe.lib.html_helpers import build_url, extract_href


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


PROBE_TIMEOUT = 2


def discover_distribution_paths(
    upstream: str,
    http_client: Any,
    *,
    index_root: str = "dists",
    anchor: str = "Release",
    max_depth: int = 3,
    root_html: str | None = None,
    _allow_child_prefix: bool = True,
) -> List[str]:
    ## @brief Walk ``index_root`` and return all nested paths with plausible Releases.
    ##
    ## A *distribution path* is any relative path ``P`` such that
    ## ``index_root/P/anchor`` looks like a valid APT Release according
    ## to ``looks_like_release``.  Callers remain responsible for turning
    ## these paths into concrete ``Schema.Distribution`` nodes.
    ##
    ## @param upstream        Base upstream URL.
    ## @param http_client     HTTPClient for fetching.
    ## @param index_root      Root directory for suites (default ``"dists"``).
    ## @param anchor          Anchor filename (default ``"Release"``).
    ## @param max_depth       Maximum BFS depth.
    ## @param root_html       Pre-fetched HTML of the index root; fetched
    ##                        from upstream if omitted.
    ## @param _allow_child_prefix  Internal flag to allow one level of
    ##                             child prefix resolution.
    ## @return List of discovered distribution paths.

    if root_html is None:
        dists_url = build_url(upstream, index_root)
        print(f"[apt] probing dists index: {dists_url}", file=sys.stderr)
        root_html = http_client.fetch_text(dists_url, timeout=PROBE_TIMEOUT)

    if not root_html:
        print("[apt] no HTML content at /dists/; giving up on suite discovery", file=sys.stderr)
        if _allow_child_prefix:
            base_html = http_client.fetch_text(upstream, timeout=PROBE_TIMEOUT)
            if base_html:
                child_dirs = list(_iter_href_names(base_html.splitlines(), dirs_only=True))
                child_paths: List[str] = []
                for child in child_dirs:
                    child_upstream = build_url(upstream, child)
                    paths = discover_distribution_paths(
                        child_upstream,
                        http_client,
                        index_root=index_root,
                        anchor=anchor,
                        max_depth=max_depth,
                        root_html=None,
                        _allow_child_prefix=False,
                    )
                    if paths:
                        child_paths.extend(paths)
                if child_paths:
                    return child_paths
        return []

    lines = root_html.splitlines()
    print(f"[apt] /dists/ HTML line count: {len(lines)}", file=sys.stderr)

    suites: List[str] = []
    for name in _iter_href_names(lines, dirs_only=False):
        if name and name not in suites:
            print(f"[apt] discovered suite under /dists: {name}", file=sys.stderr)
            suites.append(name)

    if not suites:
        print(
            "[apt] no suites discovered in /dists/ HTML; attempting dirs-only fallback",
            file=sys.stderr,
        )
        for name in _iter_href_names(lines, dirs_only=True):
            if name and name not in suites:
                print(f"[apt] discovered suite under /dists (dirs-only): {name}", file=sys.stderr)
                suites.append(name)

    if not suites:
        print("[apt] no suites discovered in /dists/ HTML; giving up on suite discovery", file=sys.stderr)
        return []

    queue: List[tuple[str, int]] = [(name, 1) for name in suites]
    seen_paths = {name for name in suites}

    print(f"[apt] total top-level suites discovered: {len(queue)}", file=sys.stderr)

    discovered_paths: List[str] = []

    while queue:
        path, depth = queue.pop(0)

        release_url = build_url(upstream, index_root, path, anchor)
        print(f"[apt] probing Release for candidate {path}: {release_url}", file=sys.stderr)
        text = http_client.fetch_text(release_url, timeout=PROBE_TIMEOUT)
        if text and looks_like_release(text):
            discovered_paths.append(path)
            continue

        if depth >= max_depth:
            continue

        index_url = build_url(upstream, index_root, path)
        index_html = http_client.fetch_text(index_url, timeout=PROBE_TIMEOUT)
        if not index_html:
            continue

        for child_name in _iter_href_names(index_html.splitlines(), dirs_only=True):
            child_path = f"{path}/{child_name}"
            if child_path in seen_paths:
                continue

            print(f"[apt] discovered nested candidate under /dists: {child_path}", file=sys.stderr)
            seen_paths.add(child_path)
            queue.append((child_path, depth + 1))

    return discovered_paths
