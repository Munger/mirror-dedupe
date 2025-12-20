"""Shared helpers for :mod:`mirror_dedupe.repos.apt`.

This module carries small, generic utilities that are reused across APT
repo detection and parsing logic. The goal is to centralise common
heuristics so that :class:`Apt` and its helpers share a single
implementation for discovering ``dists/**/Release`` entry-points.
"""

from __future__ import annotations

from typing import Any, Iterable, List
import sys

from mirror_dedupe.lib.html_helpers import build_url, extract_href


def looks_like_release(body: str) -> bool:
    """Light-weight sanity check for Debian/Ubuntu Release text.

    We only care that the body looks *plausibly* like a suite Release,
    not that it is fully valid. Callers should treat this as a cheap
    heuristic, not a full validator.
    """

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
    """Yield normalised names from href attributes in directory listings.

    This helper is used for both top-level ``/dists/`` discovery and for
    nested walks under ``/dists/<path>/``. It deliberately ignores:

    * absolute URLs (marketing links)
    * Apache "?C=N;O=D" style sort links
    * parent/self entries (".", "..") and obvious index names
      like "dists"
    * any href whose final segment still contains a URI scheme
      marker
    """

    for line in lines:
        raw = extract_href(line)
        if not raw:
            continue

        # When crawling nested paths we may be looking for
        # directory-style hrefs only; bare filenames such as
        # InRelease/Release/Release.gpg are handled entirely by
        # Release parsing.
        if dirs_only and not raw.endswith("/"):
            continue

        # Ignore absolute URLs and Apache sort/query links.
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
    http_client: Any,
    *,
    index_root: str = "dists",
    anchor: str = "Release",
    max_depth: int = 3,
    root_html: str | None = None,
) -> List[str]:
    """Walk ``index_root`` and return all nested paths with plausible Releases.

    A *distribution path* here is any relative path ``P`` such that
    ``index_root/P/anchor`` looks like a valid APT Release according to
    :func:`looks_like_release`. Callers remain responsible for turning
    these paths into concrete :class:`Schema.Distribution` nodes.
    """

    # Fetch the top-level /dists index if the caller did not supply it.
    if root_html is None:
        dists_url = build_url(upstream, index_root)
        print(f"[apt] probing dists index: {dists_url}", file=sys.stderr)
        root_html = http_client.fetch_text(dists_url)

    if not root_html:
        print("[apt] no HTML content at /dists/; giving up on suite discovery", file=sys.stderr)
        return []

    lines = root_html.splitlines()
    print(f"[apt] /dists/ HTML line count: {len(lines)}", file=sys.stderr)

    # Seed the breadth-first walk with any top-level names discovered in
    # the /dists/ index. These correspond to the logical suites or
    # series (e.g. noble, bookworm, node_22.x) that live directly under
    # index_root.
    suites: List[str] = []
    for name in _iter_href_names(lines, dirs_only=False):
        if name and name not in suites:
            print(f"[apt] discovered suite under /dists: {name}", file=sys.stderr)
            suites.append(name)

    if not suites:
        # Some vendors expose directory-style entries without clean
        # names in the generic href parser. As a second attempt, look
        # explicitly for directory hrefs (trailing "/") before giving
        # up on suite discovery entirely.
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

    # Walk the /dists hierarchy breadth-first starting from the top-level
    # suite names we just discovered. Any nested path of the form
    # "foo" or "foo/bar/..." that has a plausible
    # index_root/<path>/anchor is treated as a distribution
    # entry-point; deeper component/arch indices remain internal to the
    # Release/Packages parsing.
    queue: List[tuple[str, int]] = [(name, 1) for name in suites]
    seen_paths = {name for name in suites}

    print(f"[apt] total top-level suites discovered: {len(queue)}", file=sys.stderr)

    discovered_paths: List[str] = []

    while queue:
        path, depth = queue.pop(0)

        # 1) Validate this candidate via index_root/<path>/anchor; only
        #    accept it as a distribution if the Release looks plausible
        #    (has at least Suite/Codename metadata).
        release_url = build_url(upstream, index_root, path, anchor)
        print(f"[apt] probing Release for candidate {path}: {release_url}", file=sys.stderr)
        text = http_client.fetch_text(release_url)
        if text and looks_like_release(text):
            discovered_paths.append(path)
            # We found a real distribution entry-point; do not descend
            # into nested paths under it (avoid walking into
            # component/arch trees such as main/binary-amd64).
            continue

        # Stop descending once we reach the maximum depth; this bounds
        # discovery cost even for unusual layouts.
        if depth >= max_depth:
            continue

        index_url = build_url(upstream, index_root, path)
        index_html = http_client.fetch_text(index_url)
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

