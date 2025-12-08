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
import urllib.request
import urllib.error
from urllib.parse import urlparse
from typing import List, Dict, Optional
import re
import socket
import os
import shutil
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed


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


def check_rsync_available(upstream: str) -> bool:
    """Check if rsync appears to be usable for the given upstream.

    This tries multiple plausible rsync URL forms derived from the HTTP/HTTPS
    upstream and returns True on the first successful directory listing. If
    rsync is not installed or all attempts fail or time out, we fall back to
    HTTPS and emit a warning explaining why.
    """

    candidates = _build_rsync_candidates(upstream)
    if not candidates:
        return False

    last_error: Optional[str] = None

    for rsync_url in candidates:
        try:
            result = subprocess.run(
                ["rsync", "-4", "--list-only", rsync_url],
                capture_output=True,
                timeout=6,
            )
        except FileNotFoundError:
            # rsync is not installed; do not keep retrying.
            print(
                "  NOTE: rsync binary not found on PATH; "
                "falling back to https for this scan.",
                file=sys.stderr,
            )
            return False
        except subprocess.TimeoutExpired:
            # If the first candidate times out, further attempts are very
            # likely to do the same and just slow the scan down. Record the
            # timeout and stop probing for this upstream.
            last_error = f"timeout talking to {rsync_url}"
            break

        if result.returncode == 0:
            return True

        # Record a brief error summary for possible reporting later.
        stderr_snippet = (result.stderr or b"").decode(errors="ignore").strip().split("\n")
        if stderr_snippet and stderr_snippet[0]:
            last_error = f"rsync exited {result.returncode} for {rsync_url}: {stderr_snippet[0]}"
        else:
            last_error = f"rsync exited {result.returncode} for {rsync_url}"

    return False


def fetch_url(url: str) -> Optional[str]:
    """Fetch small text content from URL, preferring curl -4.

    This is used for HTML index pages (e.g. /dists/) and similar metadata.
    We try curl first for better IPv4 behaviour and timeout control, then
    fall back to urllib if curl is unavailable. Binary or non-UTF8 content
    returns None.
    """

    # Prefer curl -4 for consistency with Release parsing.
    try:
        result = subprocess.run(
            [
                "curl",
                "-4",
                "-s",
                "-f",
                url,
            ],
            capture_output=True,
            timeout=10,
        )
        if result.returncode == 0:
            try:
                return result.stdout.decode("utf-8")
            except UnicodeDecodeError:
                return None
    except FileNotFoundError:
        # curl not installed; fall back to urllib.
        pass
    except subprocess.TimeoutExpired:
        return None

    # Fallback: urllib
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            data = response.read()
            try:
                return data.decode("utf-8")
            except UnicodeDecodeError:
                return None
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, socket.timeout):
        return None


def url_exists(url: str, timeout: int = 1) -> bool:
    """Lightweight check: does this URL respond successfully?

    Prefer using the system ``curl`` with ``-4`` so we avoid flaky IPv6
    behaviour and let curl handle HTTP/HTTPS details (including TLS). This
    check is binary-safe; we only care about success/failure, not content.
    """

    # First try curl (IPv4-only, head request). ``-f`` makes curl exit
    # non-zero on 4xx/5xx, which is what we want for an existence check.
    try:
        result = subprocess.run(
            [
                "curl",
                "-4",
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
    except FileNotFoundError:
        # curl not installed; fall back to urllib below.
        pass
    except subprocess.TimeoutExpired:
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

    # Prefer curl -4 for fetching the small text Release file so we get
    # consistent IPv4 behaviour and tighter timeout control.
    content: Optional[str] = None
    try:
        result = subprocess.run(
            [
                "curl",
                "-4",
                "-s",
                "-f",
                release_url,
            ],
            capture_output=True,
            timeout=timeout,
        )
        if result.returncode == 0:
            try:
                content = result.stdout.decode("utf-8")
            except UnicodeDecodeError:
                content = None
    except FileNotFoundError:
        # curl not installed; fall back to urllib below.
        pass
    except subprocess.TimeoutExpired:
        content = None

    if content is None:
        try:
            with urllib.request.urlopen(release_url, timeout=timeout) as response:
                data = response.read()
                try:
                    content = data.decode("utf-8")
                except UnicodeDecodeError:
                    content = None
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, socket.timeout):
            content = None
    
    if not content:
        return {}
    
    info = {
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

    Prefer curl for robustness and binary safety, falling back to urllib.
    Returns True on success, False otherwise.
    """

    # Try curl first
    try:
        result = subprocess.run(
            [
                "curl",
                "-4",
                "-s",
                "-f",
                "-L",
                "-o",
                dest_path,
                url,
            ],
            capture_output=True,
            timeout=timeout,
        )
        if result.returncode == 0:
            return True
    except FileNotFoundError:
        # curl not installed; fall back to urllib.
        pass
    except subprocess.TimeoutExpired:
        return False

    # Fallback: urllib
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            with open(dest_path, "wb") as f:
                shutil.copyfileobj(response, f)
        return True
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, socket.timeout, OSError):
        return False


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
                    sync_method_override: Optional[str] = None) -> str:
    """Generate repository configuration"""
    
    print(f"Scanning {upstream}...", file=sys.stderr)
    
    # Detect or honour forced sync method
    print("  [1/4] Detecting sync method...", file=sys.stderr)
    if sync_method_override:
        sync_method = sync_method_override
    else:
        sync_method = 'rsync' if check_rsync_available(upstream) else 'https'
    print(f"      Sync method: {sync_method}", file=sys.stderr)
    
    # Discover distributions (suites) under dists/
    print("  [2/4] Discovering distributions...", end="", file=sys.stderr, flush=True)
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
    print("  [3/4] Discovering architectures/components...", file=sys.stderr)
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
    architectures = sorted(arch_set) if arch_set else ['amd64']
    components = sorted(comp_set) if comp_set else ['main']

    print("", file=sys.stderr)  # newline after dots
    print(f"      Architectures: {', '.join(architectures)}", file=sys.stderr)
    print(f"      Components: {', '.join(components)}", file=sys.stderr)
    
    # Detect GPG key (unless provided explicitly). Pass the discovered
    # distributions so detect_gpg_key can also look for per-suite
    # Release.gpg files (common for some third-party archives).
    print("  [4/4] Probing for GPG key...", end="", file=sys.stderr, flush=True)
    gpg_info = None
    if not (gpg_key_url and gpg_key_path):
        gpg_info = detect_gpg_key(upstream, distributions)
    # Finish the progress line before emitting any summary.
    print("", file=sys.stderr)
    
    # Generate YAML config
    config_lines = [
        f"# {name} repository",
        "",
        f"name: {name}",
        f"upstream: {upstream}",
        f"dest: {dest}",
        f"sync_method: {sync_method}",
    ]
    
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


def main():
    parser = argparse.ArgumentParser(
        description='Scan a repository and generate mirror-dedupe configuration',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Simple form: dest defaults to --name
  mirror-dedupe-scan --name ubuntu http://archive.ubuntu.com/ubuntu

  # Custom dest path
  mirror-dedupe-scan --name ubuntu --dest ubuntu/main http://archive.ubuntu.com/ubuntu

  # With an explicit GPG key override
  mirror-dedupe-scan --name mariadb \
    --gpg-key-url https://mirror.mariadb.org/PublicKey \
    --gpg-key-path PublicKey \
    https://mirror.mariadb.org/repo/10.11/ubuntu
        """
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
