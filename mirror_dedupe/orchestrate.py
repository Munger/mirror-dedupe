#!/usr/bin/env python3
"""
orchestrate.py

  Ubuntu mirror synchronisation with global deduplication

Copyright (c) 2025 Tim Hosking
Email: tim@mungerware.com
Website: https://github.com/munger
Licence: MIT
"""

import os
import sys
import subprocess
import time
import threading
import fnmatch
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

from .indices import get_packages_index, get_sources_index, parse_release_file
from .download import download_with_curl, verify_sha256
from .dedupe import hardlink_file, expand_distributions, cleanup_pool
from .sync import run_rsync, run_https_sync, download_gpg_key
from .utils import get_disk_usage, format_bytes, calculate_total_hardlink_savings

# Components to process
COMPONENTS = ['main', 'restricted', 'universe', 'multiverse']


def run_orchestrator_mode(mirrors, config_dir, dry_run):
    """Orchestrator mode: spawn subprocess for each mirror"""
    print(f"\n{'='*60}")
    print("ORCHESTRATOR MODE: Spawning subprocesses for available mirrors")
    print(f"{'='*60}")
    
    processes = []
    skipped = []
    script_path = sys.argv[0]  # Entry point script
    
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
                    print(f"\n[SKIP] Skipping '{mirror_name}' - already being processed (PID {pid})")
                    skipped.append(mirror_name)
                    continue
                except OSError:
                    # Stale lock file, remove it
                    os.remove(lock_file)
            except:
                pass
        
        # Mirror is available, spawn subprocess
        cmd = [sys.executable, script_path, '--config', str(config_dir), '--mirror', mirror_name]
        if dry_run:
            cmd.append('--dry-run')
        
        print(f"\n[START] Spawning subprocess for mirror: {mirror_name}")
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
            print(f"\n[FAIL] Mirror '{mirror_name}' failed with exit code {returncode}")
            failed.append(mirror_name)
        else:
            print(f"\n[OK] Mirror '{mirror_name}' completed successfully")
    
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
    
    cmd = [sys.executable, script_path, '--config', str(config_dir), '--dedupe-only']
    if dry_run:
        cmd.append('--dry-run')
    
    proc = subprocess.Popen(cmd)
    returncode = proc.wait()
    
    if returncode != 0:
        print(f"\n[FAIL] Deduplication failed with exit code {returncode}")
        sys.exit(1)
    
    print(f"\n{'='*60}")
    print("ALL OPERATIONS COMPLETED SUCCESSFULLY")
    print(f"{'='*60}")
    sys.exit(0)


def sync_mirrors(mirrors, dry_run):
    """Sync dists metadata for all mirrors"""
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
            if not download_gpg_key(gpg_key_url, dest, gpg_key_path, dry_run):
                print(f"  WARNING: GPG key download failed for {name}")
        
        print(f"\n[{name}] Syncing dists...")
        
        if sync_method == 'https':
            if not run_https_sync(distributions, dest, upstream, architectures, components, dry_run):
                print(f"  ERROR: HTTPS sync failed for {name}")
                sys.exit(1)
        else:
            if not run_rsync(distributions, dest, upstream, architectures, dry_run):
                print(f"  ERROR: rsync failed for {name}")
                sys.exit(1)


def collect_files(mirrors):
    """Collect all files needed across all mirrors from local indices"""
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
        
        # Optional per-mirror storage filters (do not affect indices).
        # These control which files are downloaded/kept, but indices
        # remain an exact copy of upstream.
        storage_filters = mirror.get('storage_filters', {})
        exclude_packages = storage_filters.get('exclude_packages', [])
        exclude_paths = storage_filters.get('exclude_paths', [])

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
            
            # Add to global collection, applying optional per-mirror
            # storage filters so that some files are not planned or kept.
            for path, info in files.items():
                pkg_name = info.get('package', '')

                excluded = False
                # Path-based filters first
                for pattern in exclude_paths:
                    if fnmatch.fnmatch(path, pattern):
                        excluded = True
                        break

                # Package-name-based filters
                if not excluded:
                    for pattern in exclude_packages:
                        if fnmatch.fnmatch(pkg_name, pattern):
                            excluded = True
                            break

                if excluded:
                    continue

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
    
    return global_files


def analyse_deduplication(global_files):
    """Group files by SHA256 and analyse deduplication potential"""
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
    
    return hash_to_files, unique_files


def check_existing_files(hash_to_files):
    """Check which files already exist with correct size"""
    print(f"\nAnalysing existing files (checking size, trusting upstream hashes)...")
    
    # Build list of files to check with expected size from upstream
    files_to_check = []
    for sha256, file_list in hash_to_files.items():
        first_key, first_info = file_list[0]
        _, first_path = first_key
        dest_path = os.path.join(first_info['dest_base'], first_path)
        expected_size = int(first_info.get('size', 0))
        files_to_check.append((dest_path, sha256, expected_size, len(file_list) - 1))
    
    print(f"  Checking {len(files_to_check)} files...")
    
    downloaded = 0
    hardlinked = 0
    skipped = 0
    
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
    
    return downloaded, hardlinked, skipped


def process_files(hash_to_files, unique_files, config, dry_run):
    """Download and hardlink files"""
    if dry_run:
        print("\nDRY RUN - no changes made")
        print("\nDone!")
        return
    
    buffer_size = config.get('buffer_size', 1048576)
    parallel_downloads = config.get('parallel_downloads', 10)
    curl_timeout = config.get('curl_timeout', 900)
    max_retries = config.get('max_retries', 3)
    progress_interval = config.get('progress_interval', 1000)
    
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
        file_exists = False
        try:
            stat = os.stat(dest_path)
            if stat.st_size == expected_size:
                file_exists = True
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
                            print(f"  [ERROR] Hash mismatch after download (attempt {attempt+1}/{max_retries}): {first_path}", flush=True)
                            os.remove(dest_path)
                    else:
                        if attempt < max_retries - 1:
                            print(f"  [WARN] Download failed (attempt {attempt+1}/{max_retries}), retrying: {first_path}", flush=True)
                
                if not success:
                    print(f"  [ERROR] Download failed after {max_retries} attempts: {first_path}", flush=True)
                    # Don't return - still try to hardlink if file exists elsewhere
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
                        print(f"  [FAIL] Hash mismatch after download (attempt {attempt+1}/{max_retries}): {first_path}", flush=True)
                        try:
                            os.remove(dest_path)
                        except:
                            pass
                else:
                    if attempt < max_retries - 1:
                        print(f"  [WARN] Download failed (attempt {attempt+1}/{max_retries}), retrying: {first_path}", flush=True)
            
            if not success:
                print(f"  [FAIL] Download failed after {max_retries} attempts: {first_path}", flush=True)
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
                print(f"  [ERROR] Error processing hash group {sha256[:16]}...: {e}")
    
    # Print final summary
    print(f"  Processed {processed_count}/{unique_files} files... (downloaded: {downloaded}, hardlinked: {hardlinked}, skipped: {skipped})")
    print(f"  Processing complete!")
    
    return downloaded, hardlinked, skipped


def cleanup_mirrors(mirrors, global_files, dry_run):
    """Sync dists metadata and cleanup pool for each mirror"""
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
        components = mirror.get('components', COMPONENTS)
        sync_method = mirror.get('sync_method', 'rsync')
        
        print(f"\n[{name}] Syncing dists...")
        if sync_method == 'https':
            if not run_https_sync(distributions, dest, upstream, architectures, components, dry_run):
                print(f"  ERROR: HTTPS sync failed for {name}")
        else:
            if not run_rsync(distributions, dest, upstream, architectures, dry_run):
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
        cleanup_pool(dest, expected_files, dry_run)


def print_final_summary(mirrors, downloaded, hardlinked, skipped, initial_used):
    """Print final summary of operations"""
    first_dest = mirrors[0]['dest']
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
