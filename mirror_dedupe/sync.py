#!/usr/bin/env python3
"""
sync.py

  Ubuntu mirror synchronisation with global deduplication

Copyright (c) 2025 Tim Hosking
Email: tim@mungerware.com
Website: https://github.com/munger
Licence: MIT
"""

import os
import subprocess
from .indices import parse_release_file, parse_release_metadata


def download_gpg_key(gpg_key_url: str, dest_base: str, gpg_key_path: str, dry_run: bool = False, force_ipv4: bool = False) -> bool:
    """Download GPG key to mirror"""
    dest_file = os.path.join(dest_base, gpg_key_path)
    dest_dir = os.path.dirname(dest_file)
    
    # Create directory if needed
    if not dry_run:
        os.makedirs(dest_dir, exist_ok=True)
    
    print(f"\n  Downloading GPG key: {gpg_key_url}")
    print(f"  Destination: {gpg_key_path}")
    
    if dry_run:
        print(f"  DRY RUN - would download GPG key")
        return True
    
    cmd = ['curl']
    if force_ipv4:
        cmd.append('-4')
    cmd.extend(['-fsSL', '-o', dest_file, gpg_key_url])
    result = subprocess.run(cmd, capture_output=True)
    
    if result.returncode == 0:
        print(f"  [OK] GPG key downloaded successfully")
        return True
    else:
        print(f"  [FAIL] Failed to download GPG key")
        return False


def run_rsync(distributions: list, dest_base: str, upstream_url: str, architectures: list = None, dry_run: bool = True, force_ipv4: bool = False, timeout: int = 0):
    """Run rsync for dists metadata and verify existing pool files
    
    Args:
        distributions: List of distribution names to sync (e.g. ['noble', 'noble-updates'])
        dest_base: Local destination base path (e.g. /srv/mirror/repos/ubuntu)
        upstream_url: HTTP/HTTPS URL that gets converted to rsync:// for transport
        architectures: Optional list of architectures to limit Contents file syncing
        dry_run: If True, only print what would be done
        force_ipv4: If True, force rsync to use IPv4 only
        timeout: Seconds without data before rsync aborts (0 = no timeout).
                 Prevents indefinite hangs on stalled upstream connections.
    """
    print(f"\n{'='*60}")
    print("Running rsync for dists metadata")
    print(f"{'='*60}")
    
    # Normalise dest_base - remove trailing slash if present
    dest_base = dest_base.rstrip('/')
    
    # Convert HTTP URL to rsync URL
    rsync_url = upstream_url.replace('http://', 'rsync://').replace('https://', 'rsync://')
    if not rsync_url.endswith('/'):
        rsync_url += '/'
    
    # Build rsync command for dists/ only
    # We don't sync all of pool/ because it contains files for all architectures
    # The curl/hardlink phase already downloaded the specific files we need
    cmd = ['rsync']
    if force_ipv4:
        cmd.append('-4')
    cmd.extend([
        '-rtl',  # recursive + preserve times + copy symlinks
        '--delete',
        '--compress',
        '--progress',
        '--stats',
    ])
    
    # Configurable timeout prevents rsync from hanging indefinitely
    # when an upstream connection stalls. The default (0) means no
    # timeout is set, preserving backward compatibility. A value in
    # seconds tells rsync to abort after that many seconds without
    # receiving any data from the upstream daemon.
    if timeout > 0:
        cmd.append(f'--timeout={timeout}')
    
    cmd.append('--include=/dists/')
    
    for dist in distributions:
        cmd.append(f'--include=/dists/{dist}/')
        cmd.append(f'--include=/dists/{dist}/**')
    
    # Filter Contents files by architecture if specified
    if architectures:
        for arch in architectures:
            cmd.append(f'--include=Contents-{arch}.gz')
        cmd.append('--exclude=Contents-*.gz')
    
    cmd.extend([
        '--exclude=*',
        rsync_url,
        dest_base + '/'
    ])
    
    if dry_run:
        cmd.insert(1, '--dry-run')
        print("\nDRY RUN - Would execute:")
    else:
        print("\nExecuting:")
    
    print(' '.join(cmd))
    print()
    
    if not dry_run:
        result = subprocess.run(cmd)
        return result.returncode == 0
    return True


def run_https_sync(distributions: list, dest_base: str, upstream_url: str, architectures: list = None, components: list = None, dry_run: bool = True, force_ipv4: bool = False):
    """Download dists metadata via HTTPS using curl"""
    print(f"\n{'='*60}")
    print("Downloading dists metadata via HTTPS")
    print(f"{'='*60}")
    
    # Normalise dest_base - remove trailing slash if present
    dest_base = dest_base.rstrip('/')
    
    # Ensure upstream URL ends with /
    if not upstream_url.endswith('/'):
        upstream_url += '/'
    
    # Create destination directory
    os.makedirs(dest_base, exist_ok=True)
    
    # Default to standard Debian components if not specified
    if components is None:
        components = ['main', 'contrib', 'non-free']
    
    success = True
    
    def _curl_cmd(dest_file: str, url: str):
        cmd = ['curl']
        if force_ipv4:
            cmd.append('-4')
        cmd.extend(['-fsSL', '-o', dest_file, url])
        return cmd
    
    for dist in distributions:
        dist_dir = f"{dest_base}/dists/{dist}"
        os.makedirs(dist_dir, exist_ok=True)
        
        # Download Release files first (always needed to learn what
        # indices are expected for this distribution).
        for filename in ['Release', 'Release.gpg', 'InRelease']:
            url = f"{upstream_url}dists/{dist}/{filename}"
            dest_file = f"{dist_dir}/{filename}"
            
            cmd = _curl_cmd(dest_file, url)
            
            if dry_run:
                print(f"DRY RUN - Would download: {url}")
            else:
                print(f"Downloading: {url}")
                result = subprocess.run(cmd, capture_output=True)
                # Silently skip optional files that don't exist
        
        # Parse Release to discover every index it references.
        # This is the authoritative list of files that should exist
        # under dists/ for this distribution. Using this set instead
        # of hardcoded file patterns makes HTTPS sync a true mirror —
        # it fetches ALL index types (Packages, Sources, Translations,
        # cnf, dep11, by-hash, etc.) rather than a hand-picked subset.
        if not dry_run:
            available_indices = parse_release_file(dest_base, dist)
        else:
            available_indices = set()  # In dry-run, assume everything exists
        
        # Parse size metadata from Release so we can skip unchanged
        # files with a simple stat() call (no hash computation needed).
        if not dry_run:
            release_metadata = parse_release_metadata(dest_base, dist)
        else:
            release_metadata = {}

        # Download every index referenced in Release, skipping files
        # whose local size matches the Release file's size. This avoids
        # redundant downloads while keeping index consistency.
        checked = 0
        downloaded = 0
        skipped = 0
        errors = 0
        for idx_path in sorted(available_indices):
            dest_file = f"{dist_dir}/{idx_path}"
            os.makedirs(os.path.dirname(dest_file), exist_ok=True)
            url = f"{upstream_url}dists/{dist}/{idx_path}"

            if not dry_run:
                expected = release_metadata.get(idx_path)
                if expected is not None:
                    expected_size, _ = expected
                    try:
                        stat = os.stat(dest_file)
                        if stat.st_size == expected_size:
                            skipped += 1
                            checked += 1
                            continue
                    except OSError:
                        pass  # file doesn't exist locally, download it

            cmd = _curl_cmd(dest_file, url)
            if dry_run:
                print(f"DRY RUN - Would download: {url}")
            else:
                print(f"  Downloading: {idx_path}")
                result = subprocess.run(cmd, capture_output=True)
                if result.returncode == 0:
                    downloaded += 1
                else:
                    errors += 1
                checked += 1
                # 404s are expected for some indices (e.g. .gz when
                # upstream only has .xz) — curl -f handles them silently.

        if not dry_run and checked > 0:
            print(f"  [{dist}] dists check complete: {skipped} unchanged, {downloaded} downloaded, {errors} errors")
        
        # Orphan cleanup: remove any local file under dists/ that is
        # NOT listed in the current Release. This prevents stale files
        # from a previous sync method (e.g. rsync leftovers after
        # switching to HTTPS) from accumulating and causing apt hash
        # mismatches. Release/* files are always kept.
        if not dry_run:
            orphaned = []
            for dirpath, dirnames, filenames in os.walk(dist_dir):
                for fname in filenames:
                    if fname in ('Release', 'Release.gpg', 'InRelease'):
                        continue
                    rel_path = os.path.relpath(os.path.join(dirpath, fname), dist_dir)
                    if rel_path not in available_indices:
                        orphaned.append(os.path.join(dirpath, fname))
            for fpath in orphaned:
                print(f"  Removing orphan: {os.path.relpath(fpath, dist_dir)}")
                os.remove(fpath)
        
        # Remove empty directories left behind by orphan cleanup.
        if not dry_run:
            for dirpath, dirnames, filenames in os.walk(dist_dir, topdown=False):
                if dirpath == dist_dir:
                    continue
                if not os.listdir(dirpath):
                    print(f"  Removing empty dir: {os.path.relpath(dirpath, dist_dir)}")
                    os.rmdir(dirpath)
    
    return success
