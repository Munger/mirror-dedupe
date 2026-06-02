#!/usr/bin/env python3
## @file sync.py
##
## @brief HTTPS sync helpers for mirror-dedupe.
##
## Provides ``download_gpg_key`` for GPG key retrieval with IPv6/IPv4
## fallback, and ``run_https_sync`` for downloading ``dists/`` metadata
## via HTTPS using curl.
##
## @copyright Copyright (c) 2025-2026 Tim Hosking
## @see https://github.com/munger
## @par Licence: MIT

import os
import subprocess
from .indices import parse_release_file
from .utils import ipv6_available


def download_gpg_key(gpg_key_url: str, dest_base: str, gpg_key_path: str,
                     dry_run: bool = False, force_ipv4: bool = False) -> bool:
    ## @brief Download a GPG key to the mirror destination.
    ##
    ## Attempts IPv6 first with automatic fallback to IPv4 on failure or
    ## timeout.
    ##
    ## @param gpg_key_url   Remote URL of the GPG key.
    ## @param dest_base     Base destination directory for the mirror.
    ## @param gpg_key_path  Relative path under *dest_base* to save the key.
    ## @param dry_run       If True, print actions without downloading.
    ## @param force_ipv4    If True, skip IPv6 and use IPv4 directly.
    ## @return True if the key was downloaded successfully.

    dest_file = os.path.join(dest_base, gpg_key_path)
    dest_dir = os.path.dirname(dest_file)

    if not dry_run:
        os.makedirs(dest_dir, exist_ok=True)

    print(f"\n  Downloading GPG key: {gpg_key_url}")
    print(f"  Destination: {gpg_key_path}")

    if dry_run:
        print(f"  DRY RUN - would download GPG key")
        return True

    def run_curl_gpg(url: str, dest_file: str, use_ipv4: bool = False) -> bool:
        cmd = ['curl']
        if use_ipv4:
            cmd.append('-4')
        else:
            cmd.append('-6')
        cmd.extend(['-fsSL', '-o', dest_file, url])

        family = "IPv4" if use_ipv4 else "IPv6"
        print(f"  Trying ({family}): {url}")

        try:
            result = subprocess.run(cmd, capture_output=True, timeout=30)
            if result.returncode == 0:
                print(f"  [OK] GPG key downloaded successfully via {family}")
                return True
            if not use_ipv4 and not force_ipv4:
                print(f"  IPv6 GPG download failed, trying IPv4 fallback...")
                return run_curl_gpg(url, dest_file, use_ipv4=True)
            print(f"  [FAIL] Failed to download GPG key via {family}")
            return False
        except subprocess.TimeoutExpired:
            if not use_ipv4 and not force_ipv4:
                print(f"  IPv6 GPG download timed out, trying IPv4 fallback...")
                return run_curl_gpg(url, dest_file, use_ipv4=True)
            print(f"  [FAIL] GPG key download timed out via {family}")
            return False

    return run_curl_gpg(gpg_key_url, dest_file, use_ipv4=force_ipv4)


def run_https_sync(distributions: list, dest_base: str, upstream_url: str,
                   architectures: list = None, components: list = None,
                   dry_run: bool = True, force_ipv4: bool = False):
    ## @brief Download ``dists/`` metadata via HTTPS using curl.
    ##
    ## Downloads Release files, Packages/Sources indices, and Contents
    ## files for each distribution/component/architecture combination.
    ## Skips files not listed in the Release file (when available) and
    ## provides IPv6-to-IPv4 fallback per file.
    ##
    ## @param distributions  List of distribution names.
    ## @param dest_base      Base destination directory for the mirror.
    ## @param upstream_url   Upstream repository URL.
    ## @param architectures  List of architectures (optional).
    ## @param components     List of components (defaults to standard Debian).
    ## @param dry_run        If True, print actions without downloading.
    ## @param force_ipv4     If True, skip IPv6 fallback.
    ## @return True on success.

    print(f"\n{'='*60}")
    print("Downloading dists metadata via HTTPS")
    print(f"{'='*60}")

    dest_base = dest_base.rstrip('/')

    if not upstream_url.endswith('/'):
        upstream_url += '/'

    os.makedirs(dest_base, exist_ok=True)

    if components is None:
        components = ['main', 'contrib', 'non-free']

    success = True

    def run_curl_download(url: str, dest_file: str, use_ipv4: bool = False) -> bool:
        cmd = ['curl']
        if use_ipv4:
            cmd.append('-4')
        else:
            cmd.append('-6')
        cmd.extend(['-fsSL', '-o', dest_file, url])

        if dry_run:
            family = "IPv4" if use_ipv4 else "IPv6"
            print(f"DRY RUN - Would download ({family}): {url}")
            return True
        else:
            family = "IPv4" if use_ipv4 else "IPv6"
            print(f"Downloading ({family}): {url}")
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=30)
                if result.returncode == 0:
                    return True
                if not use_ipv4 and not force_ipv4:
                    print(f"IPv6 download failed, trying IPv4 fallback: {url}")
                    return run_curl_download(url, dest_file, use_ipv4=True)
                return False
            except subprocess.TimeoutExpired:
                if not use_ipv4 and not force_ipv4:
                    print(f"IPv6 download timed out, trying IPv4 fallback: {url}")
                    return run_curl_download(url, dest_file, use_ipv4=True)
                return False

    for dist in distributions:
        dist_dir = f"{dest_base}/dists/{dist}"
        os.makedirs(dist_dir, exist_ok=True)

        for filename in ['Release', 'Release.gpg', 'InRelease']:
            url = f"{upstream_url}dists/{dist}/{filename}"
            dest_file = f"{dist_dir}/{filename}"

            if not run_curl_download(url, dest_file, use_ipv4=force_ipv4):
                pass

        if not dry_run:
            available_indices = parse_release_file(dest_base, dist)
        else:
            available_indices = set()

        for component in components:
            if architectures:
                for arch in architectures:
                    if dry_run or f"{component}/binary-{arch}/Packages.gz" in available_indices:
                        comp_dir = f"{dist_dir}/{component}/binary-{arch}"
                        os.makedirs(comp_dir, exist_ok=True)

                        for filename in ['Packages.gz', 'Packages', 'Release']:
                            url = f"{upstream_url}dists/{dist}/{component}/binary-{arch}/{filename}"
                            dest_file = f"{comp_dir}/{filename}"

                            if not run_curl_download(url, dest_file, use_ipv4=force_ipv4):
                                pass

        for component in components:
            if dry_run or f"{component}/source/Sources.gz" in available_indices:
                comp_dir = f"{dist_dir}/{component}/source"
                os.makedirs(comp_dir, exist_ok=True)

                for filename in ['Sources.gz', 'Sources', 'Release']:
                    url = f"{upstream_url}dists/{dist}/{component}/source/{filename}"
                    dest_file = f"{comp_dir}/{filename}"

                    if not run_curl_download(url, dest_file, use_ipv4=force_ipv4):
                        pass

        if architectures:
            for arch in architectures:
                filename = f"Contents-{arch}.gz"
                url = f"{upstream_url}dists/{dist}/{filename}"
                dest_file = f"{dist_dir}/{filename}"

                if not run_curl_download(url, dest_file, use_ipv4=force_ipv4):
                    pass

    return success
