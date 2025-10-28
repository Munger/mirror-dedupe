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


def download_gpg_key(gpg_key_url: str, dest_base: str, gpg_key_path: str, dry_run: bool = False) -> bool:
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
    
    cmd = ['curl', '-fsSL', '-o', dest_file, gpg_key_url]
    result = subprocess.run(cmd, capture_output=True)
    
    if result.returncode == 0:
        print(f"  [OK] GPG key downloaded successfully")
        return True
    else:
        print(f"  [FAIL] Failed to download GPG key")
        return False


def run_rsync(distributions: list, dest_base: str, upstream_url: str, architectures: list = None, dry_run: bool = True):
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
    cmd = [
        'rsync',
        '-rtl',  # recursive + preserve times + copy symlinks
        '--delete',
        '--compress',
        '--progress',
        '--stats',
    ]
    
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


def run_https_sync(distributions: list, dest_base: str, upstream_url: str, architectures: list = None, components: list = None, dry_run: bool = True):
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
    
    for dist in distributions:
        dist_dir = f"{dest_base}/dists/{dist}"
        os.makedirs(dist_dir, exist_ok=True)
        
        # Download Release files first
        for filename in ['Release', 'Release.gpg', 'InRelease']:
            url = f"{upstream_url}dists/{dist}/{filename}"
            dest_file = f"{dist_dir}/{filename}"
            
            cmd = ['curl', '-fsSL', '-o', dest_file, url]
            
            if dry_run:
                print(f"DRY RUN - Would download: {url}")
            else:
                print(f"Downloading: {url}")
                result = subprocess.run(cmd, capture_output=True)
                # Silently skip optional files that don't exist
        
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
                            
                            cmd = ['curl', '-fsSL', '-o', dest_file, url]
                            
                            if dry_run:
                                print(f"DRY RUN - Would download: {url}")
                            else:
                                result = subprocess.run(cmd, capture_output=True)
                                # Silently skip if Packages file doesn't exist
        
        # Download Sources files (only if listed in Release)
        for component in components:
            if dry_run or f"{component}/source/Sources.gz" in available_indices:
                comp_dir = f"{dist_dir}/{component}/source"
                os.makedirs(comp_dir, exist_ok=True)
                
                for filename in ['Sources.gz', 'Sources', 'Release']:
                    url = f"{upstream_url}dists/{dist}/{component}/source/{filename}"
                    dest_file = f"{comp_dir}/{filename}"
                    
                    cmd = ['curl', '-fsSL', '-o', dest_file, url]
                    
                    if dry_run:
                        print(f"DRY RUN - Would download: {url}")
                    else:
                        result = subprocess.run(cmd, capture_output=True)
                        # Silently skip if Sources file doesn't exist
        
        # Download Contents files if architectures specified
        if architectures:
            for arch in architectures:
                filename = f"Contents-{arch}.gz"
                url = f"{upstream_url}dists/{dist}/{filename}"
                dest_file = f"{dist_dir}/{filename}"
                
                cmd = ['curl', '-fsSL', '-o', dest_file, url]
                
                if dry_run:
                    print(f"DRY RUN - Would download: {url}")
                else:
                    result = subprocess.run(cmd, capture_output=True)
                    # Silently skip optional Contents files
    
    return success
