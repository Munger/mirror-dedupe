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
from .indices import parse_release_file
from .utils import ipv6_available


def download_gpg_key(gpg_key_url: str, dest_base: str, gpg_key_path: str,
                     dry_run: bool = False, force_ipv4: bool = False) -> bool:
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

    def run_curl_gpg(url: str, dest_file: str, use_ipv4: bool = False) -> bool:
        """Run curl GPG download with IPv6/IPv4 fallback logic"""
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
            # If IPv6 failed and we weren't already using IPv4, try IPv4 fallback
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


def run_rsync(distributions: list, dest_base: str, upstream_url: str,
              architectures: list = None, dry_run: bool = True,
              force_ipv4: bool = False):
    """Run rsync for dists metadata and verify existing pool files"""
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
    def build_rsync_cmd(use_ipv4: bool = False):
        cmd = ['rsync']
        if use_ipv4:
            cmd.append('-4')
        else:
            cmd.append('-6')
        cmd.extend([
            '-rtl',  # recursive + preserve times + copy symlinks
            '--delete',
            '--compress',
            '--progress',
            '--stats',
        ])
        
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
        
        return cmd
    
    # Try IPv6 first unless force_ipv4 is set
    if not force_ipv4:
        cmd = build_rsync_cmd(use_ipv4=False)
        if dry_run:
            print("\nDRY RUN - Would execute (IPv6):")
        else:
            print("\nExecuting (IPv6):")
        print(' '.join(cmd))
        print()
        
        if not dry_run:
            try:
                result = subprocess.run(cmd, timeout=300)  # 5 minute timeout
                if result.returncode == 0:
                    return True
                # If IPv6 failed, try IPv4 fallback
                print("IPv6 rsync failed, trying IPv4 fallback...")
            except subprocess.TimeoutExpired:
                print("IPv6 rsync timed out, trying IPv4 fallback...")
    
    # IPv4 fallback or forced IPv4
    cmd = build_rsync_cmd(use_ipv4=True)
    if dry_run:
        print("\nDRY RUN - Would execute (IPv4):")
    else:
        print("\nExecuting (IPv4):")
    print(' '.join(cmd))
    print()
    
    if not dry_run:
        result = subprocess.run(cmd)
        return result.returncode == 0
    return True


def run_https_sync(distributions: list, dest_base: str, upstream_url: str,
                   architectures: list = None, components: list = None,
                   dry_run: bool = True, force_ipv4: bool = False):
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
    
    def run_curl_download(url: str, dest_file: str, use_ipv4: bool = False) -> bool:
        """Run curl download with IPv6/IPv4 fallback logic"""
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
                # If IPv6 failed and we weren't already using IPv4, try IPv4 fallback
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
        
        # Download Release files first
        for filename in ['Release', 'Release.gpg', 'InRelease']:
            url = f"{upstream_url}dists/{dist}/{filename}"
            dest_file = f"{dist_dir}/{filename}"
            
            if not run_curl_download(url, dest_file, use_ipv4=force_ipv4):
                # Silently skip optional files that don't exist
                pass
        
        # Parse Release file to see what indices are available
        if not dry_run:
            available_indices = parse_release_file(dest_base, dist)
        else:
            available_indices = set()  # In dry-run, assume everything exists
        
        # Download Packages files for each architecture (only if listed in Release)
        for component in components:
            if architectures:
                for arch in architectures:
                    # Check if Packages.gz exists in Release
                    if dry_run or f"{component}/binary-{arch}/Packages.gz" in available_indices:
                        comp_dir = f"{dist_dir}/{component}/binary-{arch}"
                        os.makedirs(comp_dir, exist_ok=True)
                        
                        for filename in ['Packages.gz', 'Packages', 'Release']:
                            url = f"{upstream_url}dists/{dist}/{component}/binary-{arch}/{filename}"
                            dest_file = f"{comp_dir}/{filename}"
                            
                            if not run_curl_download(url, dest_file, use_ipv4=force_ipv4):
                                # Silently skip if Packages file doesn't exist
                                pass
        
        # Download Sources files (only if listed in Release)
        for component in components:
            if dry_run or f"{component}/source/Sources.gz" in available_indices:
                comp_dir = f"{dist_dir}/{component}/source"
                os.makedirs(comp_dir, exist_ok=True)
                
                for filename in ['Sources.gz', 'Sources', 'Release']:
                    url = f"{upstream_url}dists/{dist}/{component}/source/{filename}"
                    dest_file = f"{comp_dir}/{filename}"
                    
                    if not run_curl_download(url, dest_file, use_ipv4=force_ipv4):
                        # Silently skip if Sources file doesn't exist
                        pass
        
        # Download Contents files if architectures specified
        if architectures:
            for arch in architectures:
                filename = f"Contents-{arch}.gz"
                url = f"{upstream_url}dists/{dist}/{filename}"
                dest_file = f"{dist_dir}/{filename}"
                
                if not run_curl_download(url, dest_file, use_ipv4=force_ipv4):
                    # Silently skip optional Contents files
                    pass
    
    return success
