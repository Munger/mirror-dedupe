#!/usr/bin/env python3
"""Mirror Ubuntu repository with intelligent deduplication.

Downloads from upstream and hardlinks duplicate files (same SHA256 hash)
to save bandwidth. Uses curl for duplicates, rsync for unique files.

Strategy:
1. Parse Packages/Sources indices from upstream
2. Group files by SHA256 hash to identify duplicates
3. Download one copy of each duplicate group with curl
4. Hardlink to all other paths needing that hash
5. Rsync for unique files and dists/ metadata
"""

import os
import sys
import subprocess
import gzip
import hashlib
import yaml
import threading
import shutil
import atexit
import signal
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, Set, Tuple, List
from collections import defaultdict
from pathlib import Path

# Components to process
COMPONENTS = ['main', 'restricted', 'universe', 'multiverse']

# PID file location (will be set based on mode)
PID_FILE = None

# Global counters for download tracking
active_downloads = 0
download_lock = threading.Lock()

# Check if sha256sum is available at startup
USE_SHA256SUM = False
try:
    result = subprocess.run(['sha256sum', '--version'], capture_output=True)
    USE_SHA256SUM = result.returncode == 0
except:
    pass

def acquire_lock(lock_name='main'):
    """Acquire PID file lock to prevent multiple instances"""
    global PID_FILE
    PID_FILE = f'/var/run/mirror_dedupe.{lock_name}.pid'
    
    if os.path.exists(PID_FILE):
        try:
            with open(PID_FILE, 'r') as f:
                old_pid = int(f.read().strip())
            # Check if process is still running
            try:
                os.kill(old_pid, 0)
                print(f"ERROR: Another instance is already running for '{lock_name}' (PID {old_pid})")
                print(f"If this is incorrect, remove {PID_FILE} and try again.")
                return False
            except OSError:
                # Process not running, remove stale PID file
                print(f"Removing stale PID file for '{lock_name}' (PID {old_pid} not running)")
                os.remove(PID_FILE)
        except (ValueError, IOError):
            print(f"Warning: Invalid PID file for '{lock_name}', removing")
            os.remove(PID_FILE)
    
    # Write our PID
    with open(PID_FILE, 'w') as f:
        f.write(str(os.getpid()))
    print(f"Acquired lock for '{lock_name}' (PID {os.getpid()})")
    return True

def release_lock():
    """Release PID file lock"""
    global PID_FILE
    try:
        if PID_FILE and os.path.exists(PID_FILE):
            os.remove(PID_FILE)
            print(f"Released lock")
    except:
        pass

def signal_handler(signum, frame):
    """Handle termination signals"""
    print(f"\nReceived signal {signum}, cleaning up...")
    release_lock()
    sys.exit(1)

def get_disk_usage(path: str) -> Tuple[int, int, int]:
    """Get disk usage for a path"""
    try:
        stat = shutil.disk_usage(path)
        return (stat.total, stat.used, stat.free)
    except Exception as e:
        print(f"  Warning: Could not get disk usage for {path}: {e}")
        return (0, 0, 0)

def format_bytes(bytes_val: int) -> str:
    """Format bytes as human-readable string"""
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if bytes_val < 1024.0:
            return f"{bytes_val:.2f} {unit}"
        bytes_val /= 1024.0
    return f"{bytes_val:.2f} PB"

def calculate_total_hardlink_savings(mirrors: list) -> Tuple[int, int]:
    """Calculate total space saved by existing hardlinks across all mirrors"""
    total_files = 0
    total_bytes = 0
    
    # Collect all mirror dest paths
    paths = [mirror['dest'] for mirror in mirrors]
    
    # Find all files with link count > 1
    for dest in paths:
        if not os.path.exists(dest):
            continue
        
        try:
            # Use find to get files with multiple hardlinks
            result = subprocess.run(
                ['find', dest, '-type', 'f', '-links', '+1', '-printf', '%s\\n'],
                capture_output=True,
                text=True,
                timeout=300
            )
            
            if result.returncode == 0:
                for line in result.stdout.strip().split('\n'):
                    if line:
                        total_files += 1
                        total_bytes += int(line)
        except:
            pass
    
    return (total_files, total_bytes)

def read_gzipped_file(filepath: str) -> str:
    """Read and decompress a gzipped file from local filesystem"""
    try:
        with gzip.open(filepath, 'rt', encoding='utf-8') as f:
            return f.read()
    except Exception as e:
        print(f"  Error reading {filepath}: {e}")
        return None

def parse_packages_file(content: str) -> Dict[str, Dict[str, str]]:
    """
    Parse Packages file and extract package info.
    Returns: {filename: {'sha256': hash, 'size': size, 'package': name}}
    """
    packages = {}
    current_package = {}
    
    for line in content.split('\n'):
        line = line.rstrip()
        
        if not line:
            if 'filename' in current_package and 'sha256' in current_package:
                filename = current_package['filename']
                packages[filename] = {
                    'sha256': current_package['sha256'],
                    'size': current_package.get('size', '0'),
                    'package': current_package.get('package', 'unknown')
                }
            current_package = {}
            continue
        
        if ':' in line and not line.startswith(' '):
            key, value = line.split(':', 1)
            key = key.strip().lower()
            value = value.strip()
            
            if key == 'package':
                current_package['package'] = value
            elif key == 'filename':
                current_package['filename'] = value
            elif key == 'sha256':
                current_package['sha256'] = value
            elif key == 'size':
                current_package['size'] = value
    
    return packages

def parse_sources_file(content: str) -> Dict[str, Dict[str, str]]:
    """
    Parse Sources file and extract source package info.
    Returns: {filename: {'sha256': hash, 'size': size, 'package': name}}
    """
    sources = {}
    current_package = {}
    current_files = []
    
    for line in content.split('\n'):
        line = line.rstrip()
        
        if not line:
            if 'directory' in current_package and current_files:
                directory = current_package['directory']
                for filename, size, sha256 in current_files:
                    full_path = f"{directory}/{filename}"
                    sources[full_path] = {
                        'sha256': sha256,
                        'size': size,
                        'package': current_package.get('package', 'unknown')
                    }
            current_package = {}
            current_files = []
            continue
        
        if ':' in line and not line.startswith(' '):
            key, value = line.split(':', 1)
            key = key.strip().lower()
            value = value.strip()
            
            if key == 'package':
                current_package['package'] = value
                current_package['in_checksums'] = False
            elif key == 'directory':
                current_package['directory'] = value
                current_package['in_checksums'] = False
            elif key == 'checksums-sha256':
                current_package['in_checksums'] = True
            else:
                current_package['in_checksums'] = False
        elif line.startswith(' ') and current_package.get('in_checksums'):
            parts = line.strip().split()
            if len(parts) >= 3:
                sha256 = parts[0]
                size = parts[1]
                filename = ' '.join(parts[2:])
                if filename and not filename.endswith(')') and '(' not in filename:
                    current_files.append((filename, size, sha256))
    
    if 'directory' in current_package and current_files:
        directory = current_package['directory']
        for filename, size, sha256 in current_files:
            full_path = f"{directory}/{filename}"
            sources[full_path] = {
                'sha256': sha256,
                'size': size,
                'package': current_package.get('package', 'unknown')
            }
    
    return sources

def parse_release_file(dest_base: str, distribution: str) -> Set[str]:
    """
    Parse Release file and return set of available index files.
    Returns: set of relative paths like 'main/binary-amd64/Packages.gz'
    """
    release_path = f"{dest_base}/dists/{distribution}/Release"
    
    if not os.path.exists(release_path):
        return set()
    
    available_files = set()
    in_sha256_section = False
    
    try:
        with open(release_path, 'r') as f:
            for line in f:
                line = line.rstrip()
                
                # Look for SHA256 section
                if line.startswith('SHA256:'):
                    in_sha256_section = True
                    continue
                elif line and not line.startswith(' '):
                    # New section started
                    in_sha256_section = False
                
                # Parse file entries in SHA256 section
                if in_sha256_section and line.startswith(' '):
                    parts = line.split()
                    if len(parts) >= 3:
                        # Format: " <hash> <size> <path>"
                        file_path = parts[2]
                        available_files.add(file_path)
    except Exception as e:
        print(f"  Warning: Could not parse Release file: {e}")
        return set()
    
    return available_files

def get_packages_index(dest_base: str, distribution: str, component: str, arch: str) -> Dict[str, Dict[str, str]]:
    """Read and parse local Packages.gz for a specific component/arch"""
    packages_path = f"{dest_base}/dists/{distribution}/{component}/binary-{arch}/Packages.gz"
    content = read_gzipped_file(packages_path)
    
    if content is None:
        return {}
    
    return parse_packages_file(content)

def get_sources_index(dest_base: str, distribution: str, component: str) -> Dict[str, Dict[str, str]]:
    """Read and parse local Sources.gz for a specific component"""
    sources_path = f"{dest_base}/dists/{distribution}/{component}/source/Sources.gz"
    content = read_gzipped_file(sources_path)
    
    if content is None:
        return {}
    
    return parse_sources_file(content)

def download_with_curl(url: str, dest_path: str, timeout: int = 300, progress_info: str = "") -> bool:
    """Download file with curl, supports resuming partial downloads"""
    global active_downloads
    
    dest_dir = os.path.dirname(dest_path)
    os.makedirs(dest_dir, exist_ok=True)
    
    # Show what we're downloading
    filename = os.path.basename(dest_path)
    with download_lock:
        active_downloads += 1
        current_active = active_downloads
    print(f"  ⬇ Downloading: {filename} ({current_active} active){progress_info}", flush=True)
    
    try:
        # -C - enables automatic resume of partial downloads
        cmd = ['curl', '-f', '-L', '-C', '-', '--max-time', str(timeout), '-o', dest_path, url]
        result = subprocess.run(cmd, capture_output=True)
        
        with download_lock:
            active_downloads -= 1
            remaining = active_downloads
        
        if result.returncode == 0:
            print(f"  ✓ Completed: {filename} ({remaining} remaining)", flush=True)
        else:
            print(f"  ✗ Failed: {filename} ({remaining} remaining)", flush=True)
        
        return result.returncode == 0
    except Exception as e:
        with download_lock:
            active_downloads -= 1
            remaining = active_downloads
        print(f"  ✗ Error: {filename} ({remaining} remaining) - {e}", flush=True)
        return False

def verify_sha256(file_path: str, expected_hash: str, buffer_size: int = 1048576) -> bool:
    """Verify file SHA256 hash using sha256sum or Python hashlib"""
    if USE_SHA256SUM:
        # Use fast sha256sum command
        try:
            result = subprocess.run(['sha256sum', file_path], 
                                  capture_output=True, text=True)
            if result.returncode == 0:
                actual_hash = result.stdout.split()[0]
                return actual_hash == expected_hash
            return False
        except:
            return False
    else:
        # Use Python hashlib
        try:
            sha256 = hashlib.sha256()
            with open(file_path, 'rb') as f:
                for chunk in iter(lambda: f.read(buffer_size), b''):
                    sha256.update(chunk)
            return sha256.hexdigest() == expected_hash
        except:
            return False

def hardlink_file(source: str, dest: str, expected_hash: str = None) -> bool:
    """Create hardlink from source to dest"""
    dest_dir = os.path.dirname(dest)
    os.makedirs(dest_dir, exist_ok=True)
    
    try:
        # Check if source and dest are already the same file (hardlinked)
        if os.path.exists(dest) and os.path.samefile(source, dest):
            return True  # Already hardlinked, nothing to do
        
        # Remove dest if it exists (whether it's a file, different hardlink, or corrupted)
        if os.path.exists(dest):
            os.remove(dest)
        
        # Create hardlink
        os.link(source, dest)
        return True
    except Exception as e:
        print(f"  Error hardlinking {source} -> {dest}: {e}")
        return False

def expand_distributions(distributions: list) -> list:
    """Expand distribution names to include variants"""
    expanded = []
    for dist in distributions:
        expanded.append(dist)
        if '-' not in dist:
            expanded.extend([
                f"{dist}-updates",
                f"{dist}-security",
                f"{dist}-backports",
                f"{dist}-proposed"
            ])
    return expanded

def process_distribution(distribution: str, architectures: list, dest_base: str, 
                        upstream_url: str, dry_run: bool = True) -> Tuple[int, int, int]:
    """
    Process a single distribution.
    Returns: (downloaded_count, hardlinked_count, skipped_count)
    """
    print(f"\n{'='*60}")
    print(f"Processing distribution: {distribution}")
    print(f"{'='*60}")
    
    all_files = {}  # {path: {'sha256': ..., 'size': ..., 'package': ...}}
    
    # Collect all binary packages
    for component in COMPONENTS:
        for arch in architectures:
            print(f"\nCollecting {component}/binary-{arch}...")
            packages = get_packages_index(upstream_url, distribution, component, arch)
            print(f"  Found {len(packages)} packages")
            all_files.update(packages)
    
    # Collect all sources
    for component in COMPONENTS:
        print(f"\nCollecting {component}/source...")
        sources = get_sources_index(upstream_url, distribution, component)
        print(f"  Found {len(sources)} source files")
        all_files.update(sources)
    
    # Group files by SHA256 hash
    hash_groups = defaultdict(list)
    for path, info in all_files.items():
        sha256 = info['sha256']
        hash_groups[sha256].append(path)
    
    # Analyze duplicates
    unique_files = [paths for paths in hash_groups.values() if len(paths) == 1]
    duplicate_groups = [paths for paths in hash_groups.values() if len(paths) > 1]
    
    total_unique = len(unique_files)
    total_duplicate_groups = len(duplicate_groups)
    total_duplicate_files = sum(len(paths) for paths in duplicate_groups)
    
    print(f"\n{'='*60}")
    print("Analysis:")
    print(f"  Unique files: {total_unique}")
    print(f"  Duplicate groups: {total_duplicate_groups}")
    print(f"  Total duplicate files: {total_duplicate_files}")
    print(f"  Bandwidth savings: {total_duplicate_files - total_duplicate_groups} files")
    print(f"{'='*60}")
    
    if dry_run:
        print("\nDRY RUN - would download and hardlink files")
        return (0, 0, len(all_files))
    
    # Process duplicate groups
    downloaded = 0
    hardlinked = 0
    skipped = 0
    
    print(f"\nProcessing {total_duplicate_groups} duplicate groups...")
    for paths in duplicate_groups:
        # Get SHA256 for this group
        sha256 = all_files[paths[0]]['sha256']
        primary_path = paths[0]
        dest_path = os.path.join(dest_base, primary_path)
        
        # Check if already exists and valid
        if os.path.exists(dest_path) and verify_sha256(dest_path, sha256):
            print(f"  ✓ Already exists: {primary_path}")
            skipped += 1
        else:
            # Download primary file
            url = f"{upstream_url}/{primary_path}"
            print(f"  ⬇ Downloading: {primary_path}")
            if download_with_curl(url, dest_path):
                if verify_sha256(dest_path, sha256):
                    print(f"    ✓ Verified")
                    downloaded += 1
                else:
                    print(f"    ✗ Hash mismatch!")
                    continue
            else:
                print(f"    ✗ Download failed")
                continue
        
        # Hardlink to other paths
        for other_path in paths[1:]:
            other_dest = os.path.join(dest_base, other_path)
            if hardlink_file(dest_path, other_dest, sha256):
                hardlinked += 1
    
    print(f"\nDuplicate processing: {downloaded} downloaded, {hardlinked} hardlinked, {skipped} skipped")
    
    return (downloaded, hardlinked, skipped)

def cleanup_pool(dest_base: str, expected_files: Set[str], dry_run: bool = False) -> Tuple[int, int]:
    """Remove files from pool/ that aren't in the expected list"""
    print(f"\n{'='*60}")
    print("Cleaning up pool directory")
    print(f"{'='*60}")
    
    pool_path = os.path.join(dest_base, 'pool')
    if not os.path.exists(pool_path):
        print("  No pool directory found")
        return (0, 0)
    
    removed_files = 0
    removed_dirs = 0
    
    # Walk through pool/ and find files to remove
    for root, dirs, files in os.walk(pool_path, topdown=False):
        for filename in files:
            full_path = os.path.join(root, filename)
            # Get relative path from dest_base
            rel_path = os.path.relpath(full_path, dest_base)
            
            if rel_path not in expected_files:
                if dry_run:
                    print(f"  Would remove: {rel_path}")
                    removed_files += 1
                else:
                    try:
                        os.remove(full_path)
                        removed_files += 1
                        
                        if removed_files % 100 == 0:
                            print(f"  Removed {removed_files} files...")
                    except Exception as e:
                        print(f"  Error removing {rel_path}: {e}")
        
        # Remove empty directories
        for dirname in dirs:
            dir_path = os.path.join(root, dirname)
            try:
                if not os.listdir(dir_path):  # Directory is empty
                    if dry_run:
                        removed_dirs += 1
                    else:
                        os.rmdir(dir_path)
                        removed_dirs += 1
            except:
                pass  # Directory not empty or other issue
    
    if dry_run:
        print(f"\nWould remove: {removed_files} files, {removed_dirs} directories")
    else:
        print(f"\nRemoved: {removed_files} files, {removed_dirs} directories")
    
    return (removed_files, removed_dirs)

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
        print(f"  ✓ GPG key downloaded successfully")
        return True
    else:
        print(f"  ✗ Failed to download GPG key")
        return False

def run_rsync(distributions: list, dest_base: str, upstream_url: str, architectures: list = None, dry_run: bool = True):
    """Run rsync for dists metadata and verify existing pool files"""
    print(f"\n{'='*60}")
    print("Running rsync for dists metadata")
    print(f"{'='*60}")
    
    # Normalize dest_base - remove trailing slash if present
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
    
    # Normalize dest_base - remove trailing slash if present
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
                if result.returncode != 0:
                    print(f"  Warning: Failed to download {filename} (may not exist)")
        
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
                                if result.returncode != 0 and filename.startswith('Packages'):
                                    print(f"  Warning: Failed to download {component}/{arch}/{filename}")
        
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
                        if result.returncode != 0 and filename.startswith('Sources'):
                            print(f"  Warning: Failed to download {component}/source/{filename}")
        
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
                    if result.returncode != 0:
                        print(f"  Warning: Failed to download {filename}")
    
    return success

def load_config(config_path: str) -> dict:
    """Load mirror configuration from YAML file"""
    try:
        with open(config_path, 'r') as f:
            return yaml.safe_load(f)
    except Exception as e:
        print(f"Error loading config: {e}")
        sys.exit(1)

def find_existing_file(sha256: str, search_paths: List[str]) -> str:
    """Search for a file with matching hash in any of the search paths"""
    for base_path in search_paths:
        # This would need a hash->path index for efficiency
        # For now, we'll just check during processing
        pass
    return None

def main():
    import argparse
    
    parser = argparse.ArgumentParser(
        description='Mirror Ubuntu repository with global deduplication',
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    
    parser.add_argument('config_path', nargs='?', default='mirror_dedupe.yaml',
                       help='Path to configuration file (default: mirror_dedupe.yaml)')
    parser.add_argument('--dry-run', action='store_true',
                       help='Show what would be done without actually doing it')
    parser.add_argument('--mirror', type=str,
                       help='Process only the specified mirror (by name)')
    parser.add_argument('--dedupe-only', action='store_true',
                       help='Only run deduplication phase (skip mirror sync)')
    
    args = parser.parse_args()
    
    # Determine mode and acquire appropriate lock
    # Orchestrator mode doesn't need a lock - it checks per-mirror locks
    if args.dedupe_only:
        if not acquire_lock('dedupe'):
            sys.exit(1)
        atexit.register(release_lock)
        signal.signal(signal.SIGTERM, signal_handler)
        signal.signal(signal.SIGINT, signal_handler)
    elif args.mirror:
        if not acquire_lock(args.mirror):
            sys.exit(1)
        atexit.register(release_lock)
        signal.signal(signal.SIGTERM, signal_handler)
        signal.signal(signal.SIGINT, signal_handler)
    else:
        # Orchestrator mode - no lock needed, no cleanup handlers
        pass
    
    # Find config file
    script_dir = Path(__file__).parent
    config_path = args.config_path if os.path.isabs(args.config_path) else script_dir / args.config_path
    
    # Load configuration
    config = load_config(config_path)
    mirrors = config.get('mirrors', [])
    
    # Load global settings with defaults
    settings = config.get('config', {})
    buffer_size = settings.get('buffer_size', 1048576)
    parallel_downloads = settings.get('parallel_downloads', 10)
    curl_timeout = settings.get('curl_timeout', 300)
    max_retries = settings.get('max_retries', 3)
    progress_interval = settings.get('progress_interval', 1000)
    
    if not mirrors:
        print("No mirrors defined in configuration")
        sys.exit(1)
    
    print(f"\n{'='*60}")
    print(f"Loaded {len(mirrors)} mirror(s) from configuration")
    print(f"{'='*60}")
    
    # Orchestrator mode: spawn subprocess for each mirror
    if not args.mirror and not args.dedupe_only:
        print(f"\n{'='*60}")
        print("ORCHESTRATOR MODE: Spawning subprocesses for available mirrors")
        print(f"{'='*60}")
        
        processes = []
        skipped = []
        script_path = os.path.abspath(__file__)
        
        # Check which mirrors are available (not locked)
        for mirror in mirrors:
            mirror_name = mirror['name']
            lock_file = f'/var/run/mirror_dedupe.{mirror_name}.pid'
            
            # Check if mirror is already being processed
            if os.path.exists(lock_file):
                try:
                    with open(lock_file, 'r') as f:
                        pid = int(f.read().strip())
                    try:
                        os.kill(pid, 0)
                        print(f"\n⊘ Skipping '{mirror_name}' - already being processed (PID {pid})")
                        skipped.append(mirror_name)
                        continue
                    except OSError:
                        # Stale lock file, remove it
                        os.remove(lock_file)
                except:
                    pass
            
            # Mirror is available, spawn subprocess
            cmd = [sys.executable, script_path, str(config_path), '--mirror', mirror_name]
            if args.dry_run:
                cmd.append('--dry-run')
            
            print(f"\n→ Spawning subprocess for mirror: {mirror_name}")
            proc = subprocess.Popen(cmd)
            processes.append((mirror_name, proc))
        
        # If no mirrors were available, exit
        if not processes:
            print(f"\n{'='*60}")
            if skipped:
                print(f"All {len(skipped)} mirror(s) are already being processed")
                print("Nothing to do")
            else:
                print("No mirrors to process")
            print(f"{'='*60}")
            sys.exit(0)
        
        # Wait for all mirror processes to complete
        print(f"\n{'='*60}")
        print(f"Waiting for {len(processes)} mirror subprocess(es) to complete...")
        if skipped:
            print(f"(Skipped {len(skipped)} already-running: {', '.join(skipped)})")
        print(f"{'='*60}")
        
        failed = []
        for mirror_name, proc in processes:
            returncode = proc.wait()
            if returncode != 0:
                print(f"\n✗ Mirror '{mirror_name}' failed with exit code {returncode}")
                failed.append(mirror_name)
            else:
                print(f"\n✓ Mirror '{mirror_name}' completed successfully")
        
        if failed:
            print(f"\n{'='*60}")
            print(f"ERROR: {len(failed)} mirror(s) failed:")
            for name in failed:
                print(f"  - {name}")
            print(f"{'='*60}")
            sys.exit(1)
        
        # All mirrors completed, now run dedupe
        print(f"\n{'='*60}")
        print("All mirrors completed. Running deduplication...")
        print(f"{'='*60}")
        
        cmd = [sys.executable, script_path, str(config_path), '--dedupe-only']
        if args.dry_run:
            cmd.append('--dry-run')
        
        proc = subprocess.Popen(cmd)
        returncode = proc.wait()
        
        if returncode != 0:
            print(f"\n✗ Deduplication failed with exit code {returncode}")
            sys.exit(1)
        
        print(f"\n{'='*60}")
        print("ALL OPERATIONS COMPLETED SUCCESSFULLY")
        print(f"{'='*60}")
        sys.exit(0)
    
    # Filter mirrors if --mirror specified
    if args.mirror:
        filtered_mirrors = [m for m in mirrors if m['name'] == args.mirror]
        if not filtered_mirrors:
            print(f"ERROR: Mirror '{args.mirror}' not found in configuration")
            sys.exit(1)
        mirrors = filtered_mirrors
        print(f"\n{'='*60}")
        print(f"SINGLE MIRROR MODE: Processing '{args.mirror}'")
        print(f"{'='*60}")
    
    # Skip mirror sync if --dedupe-only
    if args.dedupe_only:
        print(f"\n{'='*60}")
        print("DEDUPE-ONLY MODE: Skipping mirror sync")
        print(f"{'='*60}")
    else:
        # First, rsync dists for all mirrors to get fresh indices
        print(f"\n{'='*60}")
        print("Syncing dists metadata for all mirrors")
        print(f"{'='*60}")
        
        for idx, mirror in enumerate(mirrors):
            name = mirror['name']
            upstream = mirror['upstream']
            dest = mirror['dest']
            expand_dists = mirror.get('expand_distributions', True)
            distributions = expand_distributions(mirror['distributions']) if expand_dists else mirror['distributions']
            architectures = mirror.get('architectures', [])
            components = mirror.get('components', COMPONENTS)
            sync_method = mirror.get('sync_method', 'rsync')
            gpg_key_url = mirror.get('gpg_key_url')
            gpg_key_path = mirror.get('gpg_key_path')
            
            # Download GPG key if specified
            if gpg_key_url and gpg_key_path:
                print(f"\n[{name}] Downloading GPG key...")
                if not download_gpg_key(gpg_key_url, dest, gpg_key_path, args.dry_run):
                    print(f"  WARNING: GPG key download failed for {name}")
            
            print(f"\n[{name}] Syncing dists...")
            
            if sync_method == 'https':
                if not run_https_sync(distributions, dest, upstream, architectures, components, args.dry_run):
                    print(f"  ERROR: HTTPS sync failed for {name}")
                    sys.exit(1)
            else:
                if not run_rsync(distributions, dest, upstream, architectures, args.dry_run):
                    print(f"  ERROR: rsync failed for {name}")
                    sys.exit(1)
    
    # Now collect all files needed across all mirrors from local indices
    print(f"\n{'='*60}")
    print("Parsing local indices")
    print(f"{'='*60}")
    
    global_files = {}  # {(mirror_idx, path): file_info}
    all_search_paths = []
    
    for idx, mirror in enumerate(mirrors):
        name = mirror['name']
        upstream = mirror['upstream']
        dest = mirror['dest']
        architectures = mirror['architectures']
        components = mirror.get('components', COMPONENTS)
        expand_dists = mirror.get('expand_distributions', True)
        distributions = expand_distributions(mirror['distributions']) if expand_dists else mirror['distributions']
        
        all_search_paths.append(dest)
        
        print(f"\n[{name}] {upstream}")
        print(f"  Dest: {dest}")
        print(f"  Arch: {', '.join(architectures)}")
        print(f"  Comp: {', '.join(components)}")
        print(f"  Dist: {', '.join(distributions)}")
        
        for dist in distributions:
            files = {}
            
            # Parse Release file to see what's available
            available_indices = parse_release_file(dest, dist)
            
            # Collect binary packages from local indices
            for component in components:
                for arch in architectures:
                    # Check if this index exists in Release file
                    index_path = f"{component}/binary-{arch}/Packages.gz"
                    if index_path in available_indices:
                        packages = get_packages_index(dest, dist, component, arch)
                        files.update(packages)
            
            # Collect sources from local indices (only if they exist)
            for component in components:
                index_path = f"{component}/source/Sources.gz"
                if index_path in available_indices:
                    sources = get_sources_index(dest, dist, component)
                    files.update(sources)
            
            # Add to global collection
            for path, info in files.items():
                key = (idx, path)
                global_files[key] = {
                    **info,
                    'mirror_idx': idx,
                    'mirror_name': name,
                    'dest_base': dest,
                    'upstream': upstream
                }
    
    print(f"\n{'='*60}")
    print(f"Collected {len(global_files)} file entries across all mirrors")
    print(f"{'='*60}")
    
    # Group by SHA256 globally
    hash_to_files = defaultdict(list)
    for key, info in global_files.items():
        sha256 = info['sha256']
        hash_to_files[sha256].append((key, info))
    
    unique_hashes = len([h for h, files in hash_to_files.items() if len(files) == 1])
    duplicate_hashes = len([h for h, files in hash_to_files.items() if len(files) > 1])
    total_entries = len(global_files)
    unique_files = unique_hashes + duplicate_hashes
    
    print(f"\nGlobal deduplication analysis:")
    print(f"  Total file references: {total_entries}")
    print(f"  Unique SHA256 hashes: {unique_files}")
    print(f"    - Appear once: {unique_hashes}")
    print(f"    - Appear 2+ times: {duplicate_hashes}")
    print(f"  Extra copies to hardlink: {total_entries - unique_files}")
    
    # Check existing files for accurate dry-run or processing
    downloaded = 0
    hardlinked = 0
    skipped = 0
    
    print(f"\nAnalyzing existing files (checking size, trusting upstream hashes)...")
    
    # Build list of files to check with expected size from upstream
    files_to_check = []
    for sha256, file_list in hash_to_files.items():
        first_key, first_info = file_list[0]
        _, first_path = first_key
        dest_path = os.path.join(first_info['dest_base'], first_path)
        expected_size = int(first_info.get('size', 0))
        files_to_check.append((dest_path, sha256, expected_size, len(file_list) - 1))
    
    print(f"  Checking {len(files_to_check)} files...")
    
    # Quick size-based check - no hashing needed!
    last_update = time.time()
    for idx, (dest_path, expected_hash, expected_size, dup_count) in enumerate(files_to_check):
        # Update progress every 1000 files or every 2 seconds
        now = time.time()
        if (idx > 0 and idx % 1000 == 0) or (now - last_update >= 2):
            percent = (idx / len(files_to_check)) * 100
            print(f"  Checking files: {idx}/{len(files_to_check)} ({percent:.1f}%) - found: {skipped}, need download: {downloaded}")
            last_update = now
        
        try:
            stat = os.stat(dest_path)
            # Trust upstream hash if file exists with correct size
            if stat.st_size == expected_size:
                skipped += 1
                hardlinked += dup_count
            else:
                # Wrong size, need to re-download
                downloaded += 1
                hardlinked += dup_count
        except:
            # File doesn't exist
            downloaded += 1
            hardlinked += dup_count
    
    # Print final status
    print(f"  Checking files: {len(files_to_check)}/{len(files_to_check)} (100.0%) - found: {skipped}, need download: {downloaded}")
    print(f"  Check complete!")
    
    print(f"\n{'='*60}")
    print("Estimated actions:")
    print(f"{'='*60}")
    print(f"  Files to download: {downloaded}")
    print(f"  Files to skip (already present): {skipped}")
    print(f"  Hardlinks to create: {hardlinked}")
    
    if args.dry_run:
        print("\nDRY RUN - no changes made")
        print("\nDone!")
        return
    
    # Reset counters for actual processing with thread-safe locks
    downloaded = 0
    hardlinked = 0
    skipped = 0
    counter_lock = threading.Lock()
    processed_count = 0
    processed_lock = threading.Lock()
    last_milestone = 0
    milestone_start_time = time.time()
    show_dots = False
    
    def process_hash_group(sha256, file_list):
        """Process one hash group: download first file and hardlink duplicates"""
        nonlocal downloaded, hardlinked, skipped, processed_count, last_milestone, milestone_start_time, show_dots
        
        first_key, first_info = file_list[0]
        _, first_path = first_key
        dest_path = os.path.join(first_info['dest_base'], first_path)
        expected_size = int(first_info.get('size', 0))
        
        # Check if already exists with correct size (trust upstream hash)
        file_downloaded = False
        try:
            stat = os.stat(dest_path)
            if stat.st_size == expected_size:
                with counter_lock:
                    skipped += 1
            else:
                # Wrong size, need to download
                url = f"{first_info['upstream']}/{first_path}"
                success = False
                for attempt in range(max_retries):
                    progress_info = f" - {unique_files - processed_count} files remaining"
                    if download_with_curl(url, dest_path, curl_timeout, progress_info):
                        if verify_sha256(dest_path, sha256, buffer_size):
                            with counter_lock:
                                downloaded += 1
                            file_downloaded = True
                            success = True
                            break
                        else:
                            print(f"  ✗ Hash mismatch after download (attempt {attempt+1}/{max_retries}): {first_path}", flush=True)
                            os.remove(dest_path)
                    else:
                        if attempt < max_retries - 1:
                            print(f"  ⚠ Download failed (attempt {attempt+1}/{max_retries}), retrying: {first_path}", flush=True)
                
                if not success:
                    print(f"  ✗ Download failed after {max_retries} attempts: {first_path}", flush=True)
                    # Still increment processed_count even on failure
                    with processed_lock:
                        processed_count += 1
                    return
        except:
            # File doesn't exist, download it
            url = f"{first_info['upstream']}/{first_path}"
            success = False
            for attempt in range(max_retries):
                progress_info = f" - {unique_files - processed_count} files remaining"
                if download_with_curl(url, dest_path, curl_timeout, progress_info):
                    if verify_sha256(dest_path, sha256, buffer_size):
                        with counter_lock:
                            downloaded += 1
                        file_downloaded = True
                        success = True
                        break
                    else:
                        print(f"  ✗ Hash mismatch after download (attempt {attempt+1}/{max_retries}): {first_path}", flush=True)
                        try:
                            os.remove(dest_path)
                        except:
                            pass
                else:
                    if attempt < max_retries - 1:
                        print(f"  ⚠ Download failed (attempt {attempt+1}/{max_retries}), retrying: {first_path}", flush=True)
            
            if not success:
                print(f"  ✗ Download failed after {max_retries} attempts: {first_path}", flush=True)
                # Still increment processed_count even on failure
                with processed_lock:
                    processed_count += 1
                return
        
        # Hardlink to all other occurrences
        local_hardlinked = 0
        for key, info in file_list[1:]:
            _, path = key
            other_dest = os.path.join(info['dest_base'], path)
            if hardlink_file(dest_path, other_dest, sha256):
                local_hardlinked += 1
        
        if local_hardlinked > 0:
            with counter_lock:
                hardlinked += local_hardlinked
        
        # Update progress counter
        with processed_lock:
            processed_count += 1
            
            # Check if we've been working on this milestone for >1 second
            current_milestone = (processed_count // progress_interval) * progress_interval
            if current_milestone > last_milestone:
                # New milestone - reset timer
                last_milestone = current_milestone
                milestone_start_time = time.time()
                show_dots = False
            elif not show_dots and (time.time() - milestone_start_time) > 1.0:
                # Been working for >1 second, start showing dots
                show_dots = True
            
            # Show dot for each file checked (not downloaded) if enabled and in terminal
            if show_dots and sys.stdout.isatty() and not file_downloaded:
                print(".", end="", flush=True)
            
            # Print milestone summary
            if processed_count % progress_interval == 0:
                if show_dots:
                    print()  # Newline after dots
                print(f"  Processed {processed_count}/{unique_files} files... (downloaded: {downloaded}, hardlinked: {hardlinked}, skipped: {skipped})")
    
    # Get initial disk usage for first mirror destination (all mirrors typically on same filesystem)
    print(f"\n{'='*60}")
    print("Initial disk usage")
    print(f"{'='*60}")
    first_dest = mirrors[0]['dest']
    total, initial_used, free = get_disk_usage(first_dest)
    print(f"Overall mirror filesystem: Used: {format_bytes(initial_used)}, Free: {format_bytes(free)}")
    
    # In single-mirror mode, skip deduplication (will be done by dedupe-only subprocess)
    # In dedupe-only mode, only do deduplication
    if args.mirror:
        print(f"\n{'='*60}")
        print(f"Single mirror mode: Deduplication will be handled separately")
        print(f"{'='*60}")
        print(f"\nMirror '{args.mirror}' sync completed successfully!")
        sys.exit(0)
    
    print(f"\nProcessing {unique_files} unique files with {parallel_downloads} parallel downloads...")
    
    # Process hash groups in parallel
    with ThreadPoolExecutor(max_workers=parallel_downloads) as executor:
        # Submit all tasks
        futures = {executor.submit(process_hash_group, sha256, file_list): sha256 
                   for sha256, file_list in hash_to_files.items()}
        
        # Wait for completion
        for future in as_completed(futures):
            try:
                future.result()
            except Exception as e:
                sha256 = futures[future]
                print(f"  ✗ Error processing hash group {sha256[:16]}...: {e}")
    
    # Print final summary
    print(f"  Processed {processed_count}/{unique_files} files... (downloaded: {downloaded}, hardlinked: {hardlinked}, skipped: {skipped})")
    print(f"  Processing complete!")
    
    # Run rsync for dists and cleanup pool on each mirror
    print(f"\n{'='*60}")
    print("Syncing dists metadata and cleaning up pool")
    print(f"{'='*60}")
    
    # Rebuild expected files per mirror
    for idx, mirror in enumerate(mirrors):
        name = mirror['name']
        upstream = mirror['upstream']
        dest = mirror['dest']
        architectures = mirror['architectures']
        expand_dists = mirror.get('expand_distributions', True)
        distributions = expand_distributions(mirror['distributions']) if expand_dists else mirror['distributions']
        architectures = mirror.get('architectures', [])
        components = mirror.get('components', COMPONENTS)
        sync_method = mirror.get('sync_method', 'rsync')
        
        print(f"\n[{name}] Syncing dists...")
        if sync_method == 'https':
            if not run_https_sync(distributions, dest, upstream, architectures, components, args.dry_run):
                print(f"  ERROR: HTTPS sync failed for {name}")
        else:
            if not run_rsync(distributions, dest, upstream, architectures, args.dry_run):
                print(f"  ERROR: rsync failed for {name}")
        
        # Build expected files list for this mirror
        print(f"\n[{name}] Building expected files list...")
        expected_files = set()
        for key, info in global_files.items():
            mirror_idx, path = key
            if mirror_idx == idx:
                expected_files.add(path)
        
        print(f"  Expected {len(expected_files)} files in pool")
        
        # Cleanup unwanted files
        print(f"\n[{name}] Cleaning up pool...")
        cleanup_pool(dest, expected_files, args.dry_run)
    
    # Get final disk usage and calculate delta
    total, final_used, free = get_disk_usage(first_dest)
    delta = final_used - initial_used
    
    # Calculate total hardlink savings
    print(f"\nCalculating total hardlink savings...")
    total_hardlinked_files, total_hardlinked_bytes = calculate_total_hardlink_savings(mirrors)
    
    # Final summary at the end
    print(f"\n{'='*60}")
    print("OVERALL SUMMARY")
    print(f"{'='*60}")
    print(f"Downloaded: {downloaded} files")
    print(f"Hardlinked: {hardlinked} duplicate files (this run)")
    print(f"Skipped (already present): {skipped} files")
    print(f"")
    print(f"Total hardlinked files across all mirrors: {total_hardlinked_files}")
    print(f"Total space saved by all hardlinks: {format_bytes(total_hardlinked_bytes)}")
    print(f"")
    if delta > 0:
        print(f"Mirror filesystem grew by {format_bytes(delta)}")
    elif delta < 0:
        print(f"Mirror filesystem shrunk by {format_bytes(abs(delta))}")
    else:
        print(f"Mirror filesystem size unchanged")
    print(f"Current usage: {format_bytes(final_used)} used, {format_bytes(free)} free")
    
    print("\nDone!")

if __name__ == "__main__":
    main()
