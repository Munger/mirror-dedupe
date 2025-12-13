#!/usr/bin/env python3
"""
scan.py

  Repository scanner for mirror-dedupe

Copyright (c) 2025 Tim Hosking
Email: tim@mungerware.com
Website: https://github.com/munger
Licence: MIT
"""

import sys
import argparse
import subprocess
from urllib.parse import urlparse
from typing import List, Dict, Optional
import re
import socket
import os
import shutil
import tempfile
from pathlib import Path
import yaml
from concurrent.futures import ThreadPoolExecutor, as_completed

from .utils import analyze_url_connectivity, ipv6_available


def _build_rsync_candidates(upstream: str) -> List[str]:
    """Build a list of candidate rsync URLs for a given upstream HTTP/HTTPS URL.

    We try a few common forms so that well-known layouts (e.g. Ubuntu, Debian)
    are more likely to be detected without any manual hints.
    """

    # If the caller already passed an rsync URL, just use it as-is.
    if upstream.startswith("rsync://") or "::" in upstream:
        return [upstream]

    parsed = urlparse(upstream)
    host = parsed.hostname
    path = (parsed.path or "").lstrip("/")

    candidates: List[str] = []

    if not host:
        return candidates

    # Full path as a module/directory, with and without trailing slash.
    # For archive-style layouts like archive.ubuntu.com/ubuntu, the most
    # useful probe is usually the rsync daemon module (host::module), so we
    # try that *first* to avoid wasting time on unlikely URLs.
    if path:
        first_segment = path.split("/", 1)[0]

        if first_segment:
            # Rsync daemon module (fastest and most common for mirrors)
            candidates.append(f"{host}::{first_segment}")
            # Fallbacks using rsync://
            candidates.append(f"rsync://{host}/{first_segment}")
            candidates.append(f"rsync://{host}/{first_segment}/")

        full = f"rsync://{host}/{path}"
        candidates.append(full)
        if not full.endswith("/"):
            candidates.append(full + "/")

    else:
        # No path component: try host root
        base = f"rsync://{host}/"
        candidates.append(base)

    # Deduplicate while preserving order
    seen = set()
    unique: List[str] = []
    for c in candidates:
        if c not in seen:
            seen.add(c)
            unique.append(c)

    return unique


def http_fetch(url: str, force_ipv4: bool = False, timeout: int = 10,
               output_file: str = None, follow_redirects: bool = True) -> Optional[subprocess.CompletedProcess]:
    """Centralized HTTP function using curl.
    
    Handles all HTTP operations: text fetching, binary downloads, file output.
    """
    cmd = ["curl"]
    cmd.extend(["-4"] if force_ipv4 else ["-6"])
    cmd.extend(["-s", "-f"])
    if follow_redirects:
        cmd.append("-L")
    if output_file:
        cmd.extend(["-o", output_file])
    cmd.append(url)
    
    try:
        return subprocess.run(cmd, capture_output=True, timeout=timeout)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None


def rsync_list(host: str, path: str, force_ipv4: bool = False, timeout: int = 6) -> Optional[subprocess.CompletedProcess]:
    """Rsync listing function - always read-only (--list-only)"""
    cmd = ["rsync"]
    cmd.extend(["-4"] if force_ipv4 else ["-6"])
    cmd.extend(["--list-only", f"{host}::{path}"])
    
    try:
        return subprocess.run(cmd, capture_output=True, timeout=timeout)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None


def _tokenize(s: str) -> List[str]:
    """Split a string into lowercase tokens on non-alphanumeric boundaries."""

    return [t for t in re.split(r"[^A-Za-z0-9]+", s.lower()) if t]


def _discover_rsync_upstream(name: str, upstream: str, force_ipv4: bool) -> Optional[str]:
    """Best-effort discovery of an rsync base URL for metadata.

    This keeps HTTP/HTTPS *upstream* as the source of truth for curl, and only
    enables rsync when we can positively identify an rsync daemon module and a
    root under that module where a standard APT-style ``dists/`` directory
    exists.

    The algorithm is intentionally generic:

      * Extract the host and path segments from the HTTP upstream.
      * List rsync modules via ``rsync -4 host::``.
      * Score modules by shared tokens with the repository name and HTTP
        tail segment; if there is a unique highest-scoring module, treat it
        as a candidate.
      * For that module, derive a small set of candidate roots (module root,
        and optionally ``module/http_tail``) and probe ``root/dists/`` with
        ``rsync -4 --list-only``.
      * If any probe succeeds, return a concrete ``rsync://host/root``
        upstream; otherwise return None and the caller should fall back to
        HTTPS.
    """

    parsed = urlparse(upstream)
    host = parsed.hostname
    if not host:
        return None

    # Build token set from repo name + last HTTP path segment.
    path = (parsed.path or "").strip("/")
    path_parts = [p for p in path.split("/") if p]
    http_tail = path_parts[-1] if path_parts else ""

    tokens_repo: set = set(_tokenize(name))
    tokens_repo.update(_tokenize(http_tail))
    if not tokens_repo:
        return None

    # List rsync daemon modules: rsync host:: (try IPv6 first, fallback to IPv4)
    result = rsync_list(host, "", force_ipv4=force_ipv4)
    if result is None:
        print(
            "  NOTE: rsync binary not found on PATH; "
            "falling back to https for this scan.",
            file=sys.stderr,
        )
        return None

    if result.returncode != 0:
        return None

    lines = (result.stdout or b"").decode(errors="ignore").splitlines()
    modules: List[tuple[str, str]] = []  # (name, description)
    for line in lines:
        line = line.strip()
        if not line or line.startswith("This is an Ubuntu mirror"):
            continue
        parts = line.split(None, 1)
        if not parts:
            continue
        mod_name = parts[0]
        desc = parts[1].strip() if len(parts) > 1 else ""
        modules.append((mod_name, desc))

    if not modules:
        return None

    # Score modules by token overlap with repo name + HTTP tail.
    # We give extra weight to matches in the *module name* so that archives
    # like "ubuntu-cloud-archive" win over more generic modules such as
    # "cloud-images" that only match strongly in the description.
    best_score = 0
    best_modules: List[str] = []
    for mod_name, desc in modules:
        name_tokens = set(_tokenize(mod_name))
        desc_tokens = set(_tokenize(desc))
        if not (name_tokens or desc_tokens):
            continue

        name_overlap = len(name_tokens & tokens_repo)
        desc_overlap = len(desc_tokens & tokens_repo)

        # Weight name matches more heavily than description matches. This
        # keeps behaviour for simple cases the same, while breaking ties in
        # favour of modules whose *name* best matches the repo tokens.
        score = name_overlap * 2 + desc_overlap

        if score > best_score:
            best_score = score
            best_modules = [mod_name]
        elif score == best_score and score > 0:
            best_modules.append(mod_name)

    if best_score <= 0 or len(best_modules) != 1:
        # Either nothing in common or ambiguous; do not guess.
        return None

    module = best_modules[0]

    # Optionally inspect the module root listing itself and use that to infer
    # a more specific child directory (e.g. "ubuntu" under
    # ubuntu-cloud-archive). This keeps the discovery strictly based on what
    # the daemon exposes, rather than assuming layout from the HTTP path.
    chosen_child: Optional[str] = None
    mod_list = rsync_list(host, module, force_ipv4=force_ipv4)

    if mod_list is not None and mod_list.returncode == 0:
        child_dirs: List[str] = []
        for line in (mod_list.stdout or b"").decode(errors="ignore").splitlines():
            line = line.rstrip()
            if not line or line.startswith("This is an Ubuntu mirror"):
                continue
            # Expect rsync-style listings where directory entries start with
            # a leading "d" (e.g. "drwxr-xr-x ... ubuntu") or "l" for symlinks.
            if not line.startswith(("d", "l")):
                continue
            parts = line.split()
            if not parts:
                continue
            name = parts[-1]
            if name in {".", ".."}:
                continue
            child_dirs.append(name)

        # If we found any child directories, score them by token overlap with
        # the repo tokens (name + HTTP tail) and, if there is a unique best
        # match with a positive score, treat it as a candidate root.
        if child_dirs:
            best_child_score = 0
            best_children: List[str] = []
            for d in child_dirs:
                tokens_d = set(_tokenize(d))
                if not tokens_d:
                    continue
                score = len(tokens_d & tokens_repo)
                
                # Tie-breaker: prefer debian-* when upstream contains 'debian'
                if "debian" in upstream.lower() and d.startswith("debian-"):
                    score += 0.5  # Small advantage for debian directories
                elif "rpm" in upstream.lower() and d.startswith("rpm-"):
                    score += 0.5  # Small advantage for rpm directories
                
                if score > best_child_score:
                    best_child_score = score
                    best_children = [d]
                elif score == best_child_score and score > 0:
                    best_children.append(d)

            if best_child_score > 0 and len(best_children) == 1:
                chosen_child = best_children[0]

    # Derive a small set of candidate roots under the module where dists/ might live.
    roots: List[str] = [module]
    if chosen_child:
        roots.append(f"{module}/{chosen_child}")

    for root in roots:
        # Probe for an APT-style dists/ directory using rsync (try IPv6 first, fallback to IPv4)
        result = rsync_list(host, f"{root}/dists/", force_ipv4=force_ipv4)
        if result is not None and result.returncode == 0:
            # We have positively identified a module/root combination with a
            # working dists/ tree; construct a concrete rsync upstream.
            return f"rsync://{host}/{root}"

    # No candidate roots exposed a dists/ directory; fall back to HTTPS.
    return None


def fetch_url(url: str) -> Optional[str]:
    """Fetch small text content from URL, trying IPv6 first then IPv4 fallback.

    Uses centralized curl function - no urllib fallback.
    Binary or non-UTF8 content returns None.
    """

    # Try IPv6 first, then fallback to IPv4
    for force_ipv4 in [False, True]:
        result = http_fetch(url, force_ipv4=force_ipv4, timeout=10)
        if result is not None and result.returncode == 0:
            try:
                return result.stdout.decode("utf-8")
            except UnicodeDecodeError:
                return None

    return None


def url_exists(url: str, timeout: int = 1) -> bool:
    """Lightweight check: does this URL respond successfully?

    Try IPv6 first then IPv4 fallback to avoid flaky IPv6 behaviour
    while still preferring IPv6 when it works. This check is binary-safe;
    we only care about success/failure, not content.
    """

    # Try IPv6 first, then fallback to IPv4
    for family in ["-6", "-4"]:
        try:
            result = subprocess.run(
                [
                    "curl",
                    family,
                    "-s",
                    "-f",
                    "-I",
                    url,
                ],
                capture_output=True,
                timeout=timeout,
            )
            if result.returncode == 0:
                return True
            # If IPv6 fails, continue to IPv4
            if family == "-6":
                continue
        except FileNotFoundError:
            # curl not installed; fall back to urllib below.
            break
        except subprocess.TimeoutExpired:
            # If IPv6 times out, try IPv4
            if family == "-6":
                continue
            return False

    # Fallback: urllib with a short timeout. This may use IPv4 or IPv6
    # depending on system configuration.
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            _ = response.read(1)
            return True
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, socket.timeout, UnicodeError):
        return False


def _first_existing_url(candidates: List[str], max_workers: int = 4) -> Optional[str]:
    """Return the first URL that responds successfully.

    This runs a small number of ``url_exists`` probes in parallel with a
    bounded worker pool, so that a handful of 1s timeouts do not add up
    linearly. The order of *candidates* is a preference hint; we return the
    first URL (in that order) that succeeds.
    """

    if not candidates:
        return None

    # Preserve original preference ordering when picking a winner.
    index_by_url = {url: idx for idx, url in enumerate(candidates)}

    winner: Optional[str] = None
    with ThreadPoolExecutor(max_workers=min(max_workers, len(candidates))) as ex:
        future_to_url = {ex.submit(url_exists, url): url for url in candidates}
        for fut in as_completed(future_to_url):
            url = future_to_url[fut]
            try:
                if fut.result():
                    if winner is None or index_by_url[url] < index_by_url[winner]:
                        winner = url
            except Exception:
                # Treat any probing error as "does not exist" for this
                # candidate; other futures continue.
                continue

    return winner


def discover_distributions(upstream: str) -> List[str]:
    """Discover available distributions by checking dists/ directory"""
    dists_url = f"{upstream.rstrip('/')}/dists/"
    
    # Try to fetch the dists directory listing
    content = fetch_url(dists_url)
    if not content:
        return []
    
    # Parse HTML directory listing for distribution names.
    # This is deliberately simple and conservative: we only accept entries that
    # look like actual suite names (e.g. focal, jammy) rather than repo roots
    # such as "/ubuntu".
    distributions: List[str] = []

    # Derive the archive root name from the upstream path (e.g. "ubuntu" from
    # https://archive.ubuntu.com/ubuntu). We never want to treat this as a
    # "distribution" entry under dists/.
    parsed_upstream = urlparse(upstream)
    root_path = (parsed_upstream.path or "").strip("/")
    root_name = root_path.split("/", 1)[0] if root_path else ""
    for line in content.splitlines():
        # Look for href links to directories
        if 'href="' in line and '/"' in line:
            start = line.find('href="') + 6
            end = line.find('/"', start)
            if start > 5 and end > start:
                raw = line[start:end]
                # Normalise: strip leading/trailing slashes
                dist_name = raw.strip('/')
                if not dist_name:
                    continue
                # Skip obvious non-suite entries
                if dist_name in ['.', '..', 'stable', 'unstable', 'testing']:
                    continue
                # Also skip the archive root name itself (e.g. "ubuntu") and
                # duplicates we've already recorded.
                if dist_name == root_name or dist_name in distributions:
                    continue
                # Heuristic: ignore names that still contain a slash (paths such
                # as "ubuntu/dists" or "/ubuntu") – we want just the suite.
                if '/' in dist_name:
                    continue
                distributions.append(dist_name)
    
    return distributions


def parse_release_file(upstream: str, distribution: str, timeout: int = 10) -> Dict[str, any]:
    """Parse Release file to extract architectures and components.

    The timeout parameter allows callers that are only using the Version
    field (e.g. to pick the latest series) to use a shorter network timeout
    so we don't stall the whole scan on a few slow suites.
    """
    release_url = f"{upstream.rstrip('/')}/dists/{distribution}/Release"

    # Use centralized curl function - no urllib fallback
    content: Optional[str] = None
    result = http_fetch(release_url, force_ipv4=True, timeout=timeout)
    if result is not None and result.returncode == 0:
        try:
            content = result.stdout.decode("utf-8")
        except UnicodeDecodeError:
            content = None

    if content is None:
        return {}

    info: Dict[str, any] = {
        'architectures': [],
        'components': [],
        'version': None,
    }
    
    for line in content.split('\n'):
        line = line.strip()
        if line.startswith('Architectures:'):
            archs = line.split(':', 1)[1].strip().split()
            # Filter out 'all' and 'source'
            info['architectures'] = [a for a in archs if a not in ['all', 'source']]
        elif line.startswith('Components:'):
            info['components'] = line.split(':', 1)[1].strip().split()
        elif line.startswith('Version:'):
            info['version'] = line.split(':', 1)[1].strip()
    
    return info


def detect_gpg_key(upstream: str, distributions: Optional[List[str]] = None) -> Optional[tuple]:
    """Try to detect GPG key URL.

    In addition to a shallow scan of the upstream root and a few
    well-known locations, this can optionally use the discovered
    distributions to look for per-suite Release.gpg files such as:

        <upstream>/dists/<distribution>/Release.gpg

    This is particularly useful for vendors like MongoDB that publish
    keys alongside per-distribution Release files rather than at the
    archive root.
    """
    parsed = urlparse(upstream)
    base = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else upstream.rstrip('/')
    # First, optionally try to discover keys by scanning any HTML index at the
    # upstream root. This is cheap for small index pages and can pick up
    # vendor-specific names without hard-coding them all.
    #
    # For large archive-style roots such as archive.ubuntu.com/ubuntu, this
    # HTML page can be big and slow, so we skip this step entirely and rely on
    # known key locations instead.
    html = None
    if not (parsed.hostname and parsed.hostname.endswith("ubuntu.com")):
        html = fetch_url(upstream)

    if html and '<a ' in html:
        # Find href targets that look like key files.
        candidates: List[str] = []
        for href in re.findall(r'href=["\']([^"\']+)["\']', html):
            if not href:
                continue
            if not (href.endswith('.gpg') or href.endswith('.asc') or 'key' in href.lower()):
                continue
            # Build absolute URL
            if href.startswith('http://') or href.startswith('https://'):
                url = href
            elif href.startswith('/'):
                url = base.rstrip('/') + href
            else:
                url = upstream.rstrip('/') + '/' + href
            candidates.append(url)

        # Deduplicate while preserving order and try a small number of
        # candidates.
        seen: set = set()
        limited: List[str] = []
        for url in candidates:
            if url not in seen:
                seen.add(url)
                limited.append(url)
                if len(limited) >= 5:
                    break

        for url in limited:
            # Simple progress indicator instead of verbose per-URL logging
            print(".", end="", file=sys.stderr, flush=True)
            if url_exists(url):
                # Derive a relative path under repo_root for the config.
                rel = url
                if url.startswith(upstream.rstrip('/') + '/'):
                    rel = url[len(upstream.rstrip('/') + '/') :]
                return (url, rel)

    # If we know about distributions for this upstream, also try the
    # per-distribution Release.gpg location. This is a common pattern for
    # third-party archives (e.g. MongoDB) where the key is published next
    # to the suite's Release file instead of at the archive root. Skip
    # this for Ubuntu hosts, where we already have a well-known archive
    # key and want to avoid extra probes.
    if (
        distributions
        and not (parsed.hostname and parsed.hostname.endswith("ubuntu.com"))
    ):
        dist_candidates: List[str] = []
        for dist in distributions[:3]:  # keep this cheap
            dist_root = f"{upstream.rstrip('/')}/dists/{dist}"
            url = f"{dist_root}/Release.gpg"
            dist_candidates.append(url)

        winner = _first_existing_url(dist_candidates)
        if winner:
            # Indicate that one of the distribution-level candidates
            # succeeded without logging each URL.
            print(".", end="", file=sys.stderr, flush=True)
            rel = winner
            if winner.startswith(upstream.rstrip('/') + '/'):
                rel = winner[len(upstream.rstrip('/') + '/') :]
            return (winner, rel)

    # Next, try a small set of "obvious" key names at the upstream root
    # based on the repository name (either the last path segment or the
    # host). This captures patterns such as ``corretto.key`` without
    # baking host-specific logic into the code.
    repo_name = ""
    path = (parsed.path or "").strip("/")
    if path:
        # Prefer the last path segment if there is one (e.g. "code" in
        # https://packages.microsoft.com/repos/code).
        repo_name = path.split("/")[-1]
    elif parsed.hostname:
        # Fall back to the hostname. For multi-label hosts we treat very
        # generic prefixes like "apt"/"packages"/"repos"/"download" as
        # wrappers around the real repo name in the next label, e.g.:
        #   apt.corretto.aws        -> corretto
        #   repos.influxdata.com    -> influxdata
        #   download.virtualbox.org -> virtualbox
        labels = parsed.hostname.split(".")
        if labels:
            repo_name = labels[0]
            generic_prefixes = {"apt", "packages", "package", "repo", "repos", "download"}
            if len(labels) >= 2 and repo_name in generic_prefixes:
                repo_name = labels[1]

    if repo_name and not (parsed.hostname and parsed.hostname.endswith("ubuntu.com")):
        root_candidates: List[str] = []
        base_root = upstream.rstrip("/")
        for suffix in [
            f"{repo_name}.key",
            f"{repo_name}.gpg",
            f"{repo_name}-archive.key",
            f"{repo_name}-archive.gpg",
            f"{repo_name}-release.key",
            f"{repo_name}-release.gpg",
        ]:
            root_candidates.append(f"{base_root}/{suffix}")

        winner = _first_existing_url(root_candidates)
        if winner:
            print(".", end="", file=sys.stderr, flush=True)
            rel = winner
            if winner.startswith(base_root + "/"):
                rel = winner[len(base_root + "/") :]
            return (winner, rel)

    # Common GPG key locations
    common_paths = [
        'project/ubuntu-archive-keyring.gpg',
        'gpgkey/nodesource-repo.gpg.key',
    ]
    
    for path in common_paths:
        # Default: path relative to upstream
        urls_to_try = [f"{upstream.rstrip('/')}/{path}"]

        # Special case for Ubuntu-style hosts: the archive key is also
        # commonly exposed at /project/... under the host root, not just
        # beneath the archive path. This makes ports.ubuntu.com work the same
        # way as archive.ubuntu.com.
        if (
            path == 'project/ubuntu-archive-keyring.gpg'
            and parsed.hostname
            and parsed.hostname.endswith('ubuntu.com')
        ):
            root_url = f"{base.rstrip('/')}/project/ubuntu-archive-keyring.gpg"
            if root_url not in urls_to_try:
                urls_to_try.insert(0, root_url)

        for url in urls_to_try:
            print(".", end="", file=sys.stderr, flush=True)
            if url_exists(url):
                # For root-level URL, still use the relative project/... path
                return (url, path)
    
    return None


def _download_binary_to_path(url: str, dest_path: str, timeout: int = 10) -> bool:
    """Download binary content from URL into dest_path.

    Uses centralized curl function - no urllib fallback.
    Returns True on success, False otherwise.
    """

    result = http_fetch(url, force_ipv4=True, timeout=timeout, 
                     output_file=dest_path)
    return result is not None and result.returncode == 0


def _verify_gpg_key_for_suite(upstream: str, distribution: str, gpg_key_url: str) -> None:
    """Best-effort verification that gpg_key_url can validate this suite.

    This is intentionally non-fatal: failures are reported as warnings but do
    not abort the scan. We only attempt verification if gpg is available.
    """

    gpg_bin = shutil.which("gpg")
    if not gpg_bin:
        return

    # Build candidate URLs for InRelease or Release/Release.gpg
    dist_root = f"{upstream.rstrip('/')}/dists/{distribution}"
    inrelease_url = f"{dist_root}/InRelease"
    release_url = f"{dist_root}/Release"
    release_gpg_url = f"{dist_root}/Release.gpg"

    with tempfile.TemporaryDirectory() as tmpdir:
        key_path = os.path.join(tmpdir, "key.gpg")
        if not _download_binary_to_path(gpg_key_url, key_path):
            return

        keyring_path = os.path.join(tmpdir, "keyring.gpg")

        # Import the key into an isolated keyring
        try:
            result = subprocess.run(
                [
                    gpg_bin,
                    "--no-default-keyring",
                    "--keyring",
                    keyring_path,
                    "--import",
                    key_path,
                ],
                capture_output=True,
                timeout=10,
            )
        except subprocess.TimeoutExpired:
            return

        if result.returncode != 0:
            return

        # Try InRelease first
        verified = False
        if url_exists(inrelease_url, timeout=3):
            inrelease_path = os.path.join(tmpdir, "InRelease")
            if _download_binary_to_path(inrelease_url, inrelease_path):
                try:
                    v = subprocess.run(
                        [
                            gpg_bin,
                            "--no-default-keyring",
                            "--keyring",
                            keyring_path,
                            "--verify",
                            inrelease_path,
                        ],
                        capture_output=True,
                        timeout=10,
                    )
                    if v.returncode == 0:
                        print(
                            f"      GPG key verified against dists/{distribution}/InRelease",
                            file=sys.stderr,
                        )
                        verified = True
                except subprocess.TimeoutExpired:
                    pass

        # Fallback to Release/Release.gpg if InRelease is not verified
        if not verified and url_exists(release_url, timeout=3) and url_exists(release_gpg_url, timeout=3):
            rel_path = os.path.join(tmpdir, "Release")
            relsig_path = os.path.join(tmpdir, "Release.gpg")
            if _download_binary_to_path(release_url, rel_path) and _download_binary_to_path(release_gpg_url, relsig_path):
                try:
                    v = subprocess.run(
                        [
                            gpg_bin,
                            "--no-default-keyring",
                            "--keyring",
                            keyring_path,
                            "--verify",
                            relsig_path,
                            rel_path,
                        ],
                        capture_output=True,
                        timeout=10,
                    )
                    if v.returncode == 0:
                        print(
                            f"      GPG key verified against dists/{distribution}/Release",
                            file=sys.stderr,
                        )
                        verified = True
                except subprocess.TimeoutExpired:
                    pass

        if not verified:
            # Only warn if we actually had signing material but verification failed.
            # Missing InRelease/Release.gpg is common on some repos, so we stay quiet
            # in that case.
            if url_exists(inrelease_url, timeout=1) or url_exists(release_gpg_url, timeout=1):
                print(
                    f"      WARNING: GPG key could not be verified against dists/{distribution} metadata",
                    file=sys.stderr,
                )


def generate_config(name: str, dest: str, upstream: str,
                    gpg_key_url: Optional[str] = None,
                    gpg_key_path: Optional[str] = None,
                    dist_overrides: Optional[List[str]] = None,
                    arch_override: Optional[List[str]] = None,
                    component_override: Optional[List[str]] = None,
                    sync_method_override: Optional[str] = None,
                    force_ipv4: bool = False,
                    host_v6_ok: bool = True,
                    global_disable_ipv6: bool = True) -> str:
    """Generate repository configuration"""
    
    print(f"Scanning {upstream}...", file=sys.stderr)
    total_steps = 4
    step = 1

    def next_step_label() -> str:
        nonlocal step
        label = f"[{step}/{total_steps}]"
        step += 1
        return label
    
    # Detect or honour forced sync method. We always keep *upstream* as the
    # HTTP/HTTPS base for curl and only enable rsync when we can discover a
    # concrete rsync_upstream with a working dists/ tree.
    if not global_disable_ipv6:
        print(f"  {next_step_label()} Checking IPv6...", file=sys.stderr)
    else:
        print(f"  {next_step_label()} Detecting sync method...", file=sys.stderr)
    rsync_upstream: Optional[str] = None
    if sync_method_override:
        sync_method = sync_method_override
    else:
        rsync_upstream = _discover_rsync_upstream(name, upstream, force_ipv4=force_ipv4)
        sync_method = 'rsync' if rsync_upstream else 'https'
    print(f"      Sync method: {sync_method}", file=sys.stderr)
    
    # Discover distributions (suites) under dists/
    print(f"  {next_step_label()} Discovering distributions...", end="", file=sys.stderr, flush=True)
    discovered = discover_distributions(upstream)
    if not discovered:
        print("", file=sys.stderr)  # newline after dots
        print("      Warning: Could not auto-detect distributions", file=sys.stderr)
        # Fallback: use a synthetic 'stable' suite for parsing/metadata.
        discovered = ['stable']
    else:
        # Finish the [2/4] line after streaming base names from
        # discover_distributions.
        print("", file=sys.stderr)

    # Decide which distributions to use for this config. We never try to
    # "auto-select" a primary series by Version:
    #
    #   * If the user provides explicit --dist/--release/--releases values,
    #     we use them exactly (with a special "all" value meaning all
    #     discovered suites).
    #   * If the user provides nothing, we default to all discovered suites
    #     as if "--releases all" had been specified.

    all_dists_mode = False

    if dist_overrides:
        # Normalise and inspect for the special "all" token.
        dists = [d for d in dist_overrides if d]
        if any(d.lower() == "all" for d in dists):
            all_dists_mode = True
            distributions = discovered
        else:
            distributions = dists
            # Basic sanity check: warn if none of the specified suite parts
            # appear under dists/.
            discovered_set = set(discovered)
            for d in distributions:
                suite_part = d.split('/', 1)[0]
                if suite_part not in discovered_set:
                    print(
                        f"      Warning: dist '{d}' was not found under dists/ (check spelling)",
                        file=sys.stderr,
                    )
    else:
        # No explicit distributions were provided; default to all discovered
        # suites. This is equivalent to the user specifying "--releases all".
        all_dists_mode = True
        distributions = discovered

    # Use the resolved distribution list directly for parsing Release files
    # and reporting to the user.
    suites_for_primary = distributions

    print(f"      Using distributions: {', '.join(distributions)}", file=sys.stderr)

    # Parse Release files to discover architectures and components. This can
    # take a little time on large archives, so we make it an explicit step.
    print(f"  {next_step_label()} Discovering architectures/components...", file=sys.stderr)
    arch_set = set()
    comp_set = set()

    for dist in suites_for_primary:
        # Log each distribution cleanly on its own line while we parse its
        # Release metadata, instead of streaming names with trailing dots.
        print(f"      {dist}", file=sys.stderr)
        info = parse_release_file(upstream, dist)
        archs = info.get('architectures') or []
        comps = info.get('components') or []
        arch_set.update(archs)
        comp_set.update(comps)

    # Fallbacks if nothing useful was found
    detected_arches = sorted(arch_set) if arch_set else ['amd64']
    detected_components = sorted(comp_set) if comp_set else ['main']

    # Honour explicit architecture/component filters as hard restrictions.
    # We still use the detected sets for basic sanity warnings, but the
    # generated config reflects exactly what the user requested.
    if arch_override:
        architectures = [a for a in arch_override if a]
        detected_set = set(detected_arches)
        for a in architectures:
            if a not in detected_set:
                print(
                    f"      Warning: architecture '{a}' was not found in Release metadata",
                    file=sys.stderr,
                )
    else:
        architectures = detected_arches

    if component_override:
        components = [c for c in component_override if c]
        detected_set = set(detected_components)
        for c in components:
            if c not in detected_set:
                print(
                    f"      Warning: component '{c}' was not found in Release metadata",
                    file=sys.stderr,
                )
    else:
        components = detected_components

    print("", file=sys.stderr)  # newline after dots
    print(f"      Architectures: {', '.join(architectures)}", file=sys.stderr)
    print(f"      Components: {', '.join(components)}", file=sys.stderr)
    
    # Detect GPG key (unless provided explicitly). Pass the discovered
    # distributions so detect_gpg_key can also look for per-suite
    # Release.gpg files (common for some third-party archives).
    print(f"  {next_step_label()} Probing for GPG key...", end="", file=sys.stderr, flush=True)
    gpg_info = None
    if not (gpg_key_url and gpg_key_path):
        gpg_info = detect_gpg_key(upstream, distributions)
    # Finish the progress line before emitting any summary.
    print("", file=sys.stderr)
    
    # Decide whether to suggest per-repo IPv6 disabling based on host health.
    # We only do this when global IPv6 is allowed but this specific host
    # appears to have unreliable or non-existent IPv6 while IPv4 still works.
    disable_ipv6_for_repo = False
    if (not global_disable_ipv6) and (not host_v6_ok) and force_ipv4:
        disable_ipv6_for_repo = True

    # Generate YAML config
    config_lines = [
        f"# {name} repository",
        "",
        f"name: {name}",
        f"upstream: {upstream}",
        f"dest: {dest}",
        f"sync_method: {sync_method}",
    ]

    if rsync_upstream and sync_method == 'rsync':
        config_lines.append(f"rsync_upstream: {rsync_upstream}")

    if disable_ipv6_for_repo:
        config_lines.append("disable_ipv6: true")
    
    if gpg_key_url and gpg_key_path:
        # Explicit GPG key provided by user, use as-is
        config_lines.extend([
            f"gpg_key_url: {gpg_key_url}",
            f"gpg_key_path: {gpg_key_path}",
        ])
        print(f"      GPG key: {gpg_key_path}", file=sys.stderr)
        # Best-effort verification against one of the selected distributions
        if distributions:
            _verify_gpg_key_for_suite(upstream, distributions[0], gpg_key_url)
    elif gpg_info:
        gpg_url, gpg_path = gpg_info
        config_lines.extend([
            f"gpg_key_url: {gpg_url}",
            f"gpg_key_path: {gpg_path}",
        ])
        print(f"      GPG key: {gpg_path}", file=sys.stderr)
        if distributions:
            _verify_gpg_key_for_suite(upstream, distributions[0], gpg_url)
    else:
        example_key_url = f"{upstream.rstrip('/')}/path/to/key.gpg"
        config_lines.extend([
            "# GPG key not auto-detected - add manually if required:",
            f"# gpg_key_url: {example_key_url}",
            "# gpg_key_path: path/to/key.gpg",
        ])
        print(
            "  WARNING: No GPG key found at any common locations; "
            "mirror config will be written WITHOUT gpg_key_url/gpg_key_path.\n"
            "           Run with --gpg-key-url/--gpg-key-path if you know where to find them.",
            file=sys.stderr,
        )
    
    config_lines.append("architectures:")
    for arch in architectures:
        config_lines.append(f"  - {arch}")
    
    config_lines.append("components:")
    for comp in components:
        config_lines.append(f"  - {comp}")
    
    config_lines.append("distributions:")
    if all_dists_mode:
        # In all_dists_mode we emit every discovered suite literally. This is
        # intended for full/archival mirrors and is expected to be edited by
        # hand afterwards.
        for dist in discovered:
            config_lines.append(f"  - {dist}")
    else:
        for dist in distributions:
            config_lines.append(f"  - {dist}")
        if len(distributions) == 1 and distributions[0] not in ['stable', 'unstable', 'testing']:
            config_lines.append("# Distribution auto-expands to include variants (e.g., -updates, -security)")

    # Check if we should disable distribution expansion. If only one
    # distribution and it's 'stable', disable expansion. In all_dists_mode
    # we always disable expansion because the list already enumerates all
    # suites explicitly.
    if all_dists_mode or (len(distributions) == 1 and distributions[0] == 'stable'):
        config_lines.append("expand_distributions: false")

    config_lines.append("")  # Trailing newline
    
    return '\n'.join(config_lines)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description='Scan a repository and generate mirror-dedupe configuration',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples:
  # Simple form: dest defaults to --name
  mirror-dedupe-scan --name ubuntu http://archive.ubuntu.com/ubuntu

  # Custom dest path
  mirror-dedupe-scan --name ubuntu --dest ubuntu/main http://archive.ubuntu.com/ubuntu

  # With an explicit GPG key override
  mirror-dedupe-scan --name mariadb \\
    --gpg-key-url https://mirror.mariadb.org/PublicKey \\
    --gpg-key-path PublicKey \\
    https://mirror.mariadb.org/repo/10.11/ubuntu"""
    )
    
    parser.add_argument('--name', required=True,
                       help='Repository name (used for config filename and mirror-dedupe NAME)')
    parser.add_argument('--dest',
                       help='Destination path (relative to repo_root). Defaults to --name if omitted.')
    parser.add_argument('--config', '--config-dir', dest='config_dir', default='/etc/mirror-dedupe',
                       help='Configuration directory (default: /etc/mirror-dedupe)')
    parser.add_argument('-r', '--dist', '--release', action='append', dest='dist',
                       help='Override the primary distribution/suite (may be specified multiple times)')
    parser.add_argument('-R', '--releases', dest='releases',
                       help='Comma-separated list of distributions/suites to override')
    parser.add_argument('--arch', action='append', dest='arch',
                       help='Architecture to include (may be specified multiple times)')
    parser.add_argument('--architectures', dest='architectures',
                       help='Comma-separated list of architectures to include')
    parser.add_argument('--component', action='append', dest='component',
                       help='Component to include (may be specified multiple times)')
    parser.add_argument('--components', dest='components',
                       help='Comma-separated list of components to include')
    parser.add_argument('-G', '--gpg-key-url',
                       help='Explicit GPG key URL for this repository')
    parser.add_argument('-g', '--gpg-key-path',
                       help='Relative GPG key path under repo_root (e.g. keys/vendor.asc)')
    parser.add_argument('--method', dest='method',
                       help='Force sync method: rsync or https (default: auto-detect)')
    parser.add_argument('upstream', 
                       help='Upstream repository URL')
    
    args = parser.parse_args()

    # Load main config (if present) to obtain global defaults such as
    # disable_ipv6. In test environments without a mirror-dedupe.conf we
    # default to disabling IPv6, which keeps behaviour predictable.
    cfg_path = Path(args.config_dir) / 'mirror-dedupe.conf'
    global_disable_ipv6 = False
    if cfg_path.exists():
        try:
            with cfg_path.open('r') as f:
                cfg = yaml.safe_load(f) or {}
            global_disable_ipv6 = cfg.get('disable_ipv6', False)
        except Exception:
            # On any parse error, fall back to allowing IPv6 so we can still
            # detect and react to broken hosts.
            global_disable_ipv6 = False

    # Validate explicit GPG key parameters if provided
    if args.gpg_key_url or args.gpg_key_path:
        if not (args.gpg_key_url and args.gpg_key_path):
            print("ERROR: --gpg-key-url and --gpg-key-path must be provided together", file=sys.stderr)
            sys.exit(1)
        # Validate that the key URL is reachable, using the same
        # IPv4-preferring existence check as automatic probing but with a
        # slightly longer timeout to reduce spurious failures on slow
        # vendor endpoints.
        if not url_exists(args.gpg_key_url, timeout=3):
            print(f"ERROR: Could not fetch GPG key from {args.gpg_key_url}", file=sys.stderr)
            sys.exit(1)
    
    # Normalise dest: default to name if not provided explicitly.
    dest = args.dest or args.name

    # Derive IPv4/IPv6 behaviour for this upstream based on the global
    # disable_ipv6 setting and a per-host IPv6 health check. Before we do
    # that, perform a generic connectivity analysis so we can pick a stable
    # upstream URL and decide whether IPv6 is viable.
    conn_info = analyze_url_connectivity(args.upstream, timeout=5)

    if not conn_info["reachable"]:
        # No working path (neither IPv4 nor IPv6). There is no sensible
        # configuration we can write for this upstream, so abort early with
        # a concise explanation instead of generating a broken config.
        print(f"ERROR: Upstream {args.upstream} is unreachable over both IPv4 and IPv6.", file=sys.stderr)
        if conn_info.get("recommended_action"):
            print(f"Recommendation: {conn_info['recommended_action']}", file=sys.stderr)
        sys.exit(1)

    # If we discovered a redirected URL, prefer that in the generated
    # configuration so future runs do not depend on chasing the same
    # redirect again.
    final_url = conn_info.get("final_url") or args.upstream
    if final_url != args.upstream:
        print(f"NOTE: Upstream {args.upstream} redirects to {final_url}; using redirected URL in generated config.")
        args.upstream = final_url

    # If IPv6 is clearly not working but IPv4 is, treat this as an
    # "IPv6 broken, IPv4 OK" case. We keep the probe conservative: any
    # error at the IPv6 layer counts as "not usable" here, while a
    # working IPv4 path is considered sufficient for a robust config.
    ipv6_ok = bool(conn_info.get("ipv6_reachable"))
    ipv4_ok = bool(conn_info.get("ipv4_reachable"))

    parsed_upstream = urlparse(args.upstream)
    upstream_host = parsed_upstream.hostname or ''
    host_v6_ok = ipv6_ok or ipv6_available(args.upstream)

    # Determine whether we should force IPv4 for this upstream. We honour
    # the global disable_ipv6 first and then tighten further based on the
    # per-host health check.
    force_ipv4 = global_disable_ipv6 or (not host_v6_ok)

    if (not global_disable_ipv6) and (not host_v6_ok) and ipv4_ok:
        # IPv6 appears unreliable for this host but IPv4 is fine. We make a
        # point of telling the user why we will generate a config that
        # prefers IPv4 so they can choose to harden this by setting
        # disable_ipv6: true in the permanent repos-available config.
        print(
            f"NOTE: IPv6 appears unreliable for {upstream_host}, IPv4 is OK; "
            "this scan will prefer IPv4 for network operations."
        )

    # Normalise/validate forced sync method, if supplied
    sync_method_override: Optional[str] = None
    if args.method:
        m = args.method.lower()
        if m not in ('rsync', 'https'):
            print("ERROR: --method must be either 'rsync' or 'https'", file=sys.stderr)
            sys.exit(1)
        sync_method_override = m

    # Normalise arch/component overrides
    def _split_csv(values):
        items = []
        for v in values or []:
            if not v:
                continue
            parts = [p.strip() for p in v.split(',')]
            items.extend([p for p in parts if p])
        # De-duplicate while preserving order
        seen = set()
        result = []
        for item in items:
            if item not in seen:
                seen.add(item)
                result.append(item)
        return result

    arch_override = _split_csv((args.arch or []) + ([args.architectures] if args.architectures else []))
    if not arch_override:
        arch_override = None

    component_override = _split_csv((args.component or []) + ([args.components] if args.components else []))
    if not component_override:
        component_override = None

    # Dist overrides: singular flags (--dist/--release) are repeatable
    # single values; the plural form (--releases) is a comma-separated list.
    dist_overrides: Optional[List[str]] = None
    dist_values: List[str] = []
    if args.dist:
        dist_values.extend(args.dist)
    if args.releases:
        dist_values.extend(_split_csv([args.releases]))
    if dist_values:
        # De-duplicate while preserving order
        seen_d = set()
        ordered: List[str] = []
        for d in dist_values:
            if d and d not in seen_d:
                seen_d.add(d)
                ordered.append(d)
        dist_overrides = ordered or None

    # Generate configuration
    config = generate_config(
        args.name,
        dest,
        args.upstream,
        gpg_key_url=args.gpg_key_url,
        gpg_key_path=args.gpg_key_path,
        dist_overrides=dist_overrides,
        arch_override=arch_override,
        component_override=component_override,
        sync_method_override=sync_method_override,
        force_ipv4=force_ipv4,
        host_v6_ok=host_v6_ok,
        global_disable_ipv6=global_disable_ipv6,
    )
    
    # Save to repos-available
    import os
    repos_available = os.path.join(args.config_dir, 'repos-available')
    os.makedirs(repos_available, exist_ok=True)
    
    config_file = os.path.join(repos_available, f'{args.name}.conf')
    with open(config_file, 'w') as f:
        f.write(config)
    
    print(f"Configuration saved to: {config_file}", file=sys.stderr)
    print("", file=sys.stderr)
    print("Next steps:", file=sys.stderr)
    print("  # Test the repository configuration before activating it", file=sys.stderr)
    print(f"  mirror-dedupe --test {args.name}", file=sys.stderr)
    print("", file=sys.stderr)
    print("  # If the test looks good, activate the repository:", file=sys.stderr)
    print(f"  mirror-dedupe --activate {args.name}", file=sys.stderr)
    print("", file=sys.stderr)
    print("  # Manual enable (equivalent to --activate) if you prefer:", file=sys.stderr)
    print(f"  ln -s {config_file} {os.path.join(args.config_dir, 'repos-enabled', args.name + '.conf')}", file=sys.stderr)
    print(f"\nOr simply:", file=sys.stderr)
    print(f"  cd {args.config_dir}/repos-enabled", file=sys.stderr)
    print(f"  ln -s ../repos-available/{args.name}.conf .", file=sys.stderr)

    print("", file=sys.stderr)
    print("This is my best guess and should give you a decent head start when mirroring this repo.", file=sys.stderr)
    print("However, I'm not perfect so you really should examine the config file carefully before activating it.", file=sys.stderr)


if __name__ == '__main__':
    main()
