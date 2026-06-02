#!/usr/bin/env python3
## @file orchestrate.py
##
## @brief Mirror orchestration, sync, file collection, deduplication, and
##        cleanup pipeline.
##
## Provides the top-level pipeline functions called by ``cli.py``:
## orchestrator mode (subprocess-per-mirror), dists sync, local index
## parsing, deduplication analysis, parallel download, cleanup, and
## final summary.
##
## @copyright Copyright (c) 2025-2026 Tim Hosking
## @see https://github.com/munger
## @par Licence: MIT

import os
import sys
import subprocess
import time
import threading
import fnmatch
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

from .indices import get_packages_index, get_sources_index, parse_release_file
from .download import download_with_curl, verify_sha256
from .dedupe import hardlink_file, expand_distributions, cleanup_pool
from .sync import run_https_sync, download_gpg_key
from .utils import get_disk_usage, format_bytes, calculate_total_hardlink_savings, ipv6_available, get_optimal_url, classify_url_issue

COMPONENTS = ['main', 'restricted', 'universe', 'multiverse']

IPV6_TROUBLE_MIRRORS = set()


def run_orchestrator_mode(mirrors, config_dir, dry_run):
    ## @brief Orchestrator mode: spawn a subprocess for each mirror.
    ##
    ## Checks lock files to skip mirrors already being processed, then
    ## spawns parallel subprocesses and waits for all to complete.
    ## Finally runs the deduplication phase.
    ##
    ## @param mirrors     List of mirror configuration dicts.
    ## @param config_dir  Path to the configuration directory.
    ## @param dry_run     If True, pass ``--dry-run`` to subprocesses.

    print(f"\n{'='*60}")
    print("ORCHESTRATOR MODE: Spawning subprocesses for available mirrors")
    print(f"{'='*60}")

    processes = []
    skipped = []
    script_path = sys.argv[0]

    for mirror in mirrors:
        mirror_name = mirror['name']
        lock_file = f'/var/run/mirror_dedupe.{mirror_name}.pid'

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
                    os.remove(lock_file)
            except:
                pass

        cmd = [sys.executable, script_path, '--config', str(config_dir), '--mirror', mirror_name]
        if dry_run:
            cmd.append('--dry-run')

        print(f"\n[START] Spawning subprocess for mirror: {mirror_name}")
        proc = subprocess.Popen(cmd)
        processes.append((mirror_name, proc))

    if not processes:
        print(f"\n{'='*60}")
        if skipped:
            print(f"All {len(skipped)} mirror(s) are already being processed")
            print("Nothing to do")
        else:
            print("No mirrors to process")
        print(f"{'='*60}")
        sys.exit(0)

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
    ## @brief Sync ``dists/`` metadata for all mirrors.
    ##
    ## Analyses URL connectivity for each mirror, determines IPv6/IPv4
    ## policy, downloads GPG keys if configured, and runs the HTTPS sync.
    ##
    ## @param mirrors  List of mirror configuration dicts.
    ## @param dry_run  If True, print actions without downloading.

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
        gpg_key_url = mirror.get('gpg_key_url')
        gpg_key_path = mirror.get('gpg_key_path')

        optimal_url, connectivity_info = get_optimal_url(upstream)
        if optimal_url is None:
            print(f"[{name}] ERROR: URL connectivity failed for {upstream}")
            issue_type = classify_url_issue(upstream)
            print(f"[{name}] Issue type: {issue_type}")
            if connectivity_info.get('recommended_action'):
                print(f"[{name}] Recommendation: {connectivity_info['recommended_action']}")
            continue

        if optimal_url != upstream:
            print(f"[{name}] NOTE: URL corrected: {upstream} -> {optimal_url}")
            upstream = optimal_url
            mirror['upstream'] = upstream

        issue_type = classify_url_issue(upstream)
        if issue_type != 'working':
            print(f"[{name}] NOTE: URL issue detected: {issue_type}")
            if connectivity_info.get('recommended_action'):
                print(f"[{name}] Recommendation: {connectivity_info['recommended_action']}")

        cfg_disable_ipv6 = mirror.get('disable_ipv6', True)
        host_v6_ok = ipv6_available(upstream)
        force_ipv4 = cfg_disable_ipv6 or (not host_v6_ok)
        if (not cfg_disable_ipv6) and (not host_v6_ok):
            IPV6_TROUBLE_MIRRORS.add(name)

        if gpg_key_url and gpg_key_path:
            print(f"\n[{name}] Downloading GPG key...")
            if not download_gpg_key(gpg_key_url, dest, gpg_key_path, dry_run, force_ipv4=force_ipv4):
                print(f"  WARNING: GPG key download failed for {name}")

        print(f"\n[{name}] Syncing dists...")
        if not run_https_sync(distributions, dest, upstream, architectures, components, dry_run, force_ipv4=force_ipv4):
            print(f"  ERROR: HTTPS sync failed for {name}")
            sys.exit(1)


def collect_files(mirrors):
    ## @brief Collect all file entries across all mirrors from local indices.
    ##
    ## Parses local Packages.gz and Sources.gz files for each mirror,
    ## applying optional per-mirror storage filters (exclude_packages,
    ## exclude_paths).
    ##
    ## @param mirrors  List of mirror configuration dicts.
    ## @return Dict of ``{(mirror_idx, path): file_info}``.

    print(f"\n{'='*60}")
    print("Parsing local indices")
    print(f"{'='*60}")

    global_files = {}
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

        storage_filters = mirror.get('storage_filters', {})
        exclude_packages = storage_filters.get('exclude_packages', [])
        exclude_paths = storage_filters.get('exclude_paths', [])

        for dist in distributions:
            files = {}

            available_indices = parse_release_file(dest, dist)

            for component in components:
                for arch in architectures:
                    index_path = f"{component}/binary-{arch}/Packages.gz"
                    if index_path in available_indices:
                        packages = get_packages_index(dest, dist, component, arch)
                        files.update(packages)

            for component in components:
                index_path = f"{component}/source/Sources.gz"
                if index_path in available_indices:
                    sources = get_sources_index(dest, dist, component)
                    files.update(sources)

            for path, info in files.items():
                pkg_name = info.get('package', '')

                excluded = False
                for pattern in exclude_paths:
                    if fnmatch.fnmatch(path, pattern):
                        excluded = True
                        break

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
    ## @brief Group files by SHA-256 and analyse deduplication potential.
    ##
    ## @param global_files  Dict from ``collect_files``.
    ## @return Tuple of ``(hash_to_files, unique_files_count)``.

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
    ## @brief Check which files already exist with the correct size.
    ##
    ## Uses a quick size-based check (no hashing) to determine which
    ## files need downloading and how many hardlinks would be created.
    ##
    ## @param hash_to_files  Dict from ``analyse_deduplication``.
    ## @return Tuple of ``(downloaded, hardlinked, skipped)`` estimates.

    print(f"\nAnalysing existing files (checking size, trusting upstream hashes)...")

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

    last_update = time.time()
    for idx, (dest_path, expected_hash, expected_size, dup_count) in enumerate(files_to_check):
        now = time.time()
        if (idx > 0 and idx % 1000 == 0) or (now - last_update >= 2):
            percent = (idx / len(files_to_check)) * 100
            print(f"  Checking files: {idx}/{len(files_to_check)} ({percent:.1f}%) - found: {skipped}, need download: {downloaded}")
            last_update = now

        try:
            stat = os.stat(dest_path)
            if stat.st_size == expected_size:
                skipped += 1
                hardlinked += dup_count
            else:
                downloaded += 1
                hardlinked += dup_count
        except:
            downloaded += 1
            hardlinked += dup_count

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
    ## @brief Download and hardlink files in parallel.
    ##
    ## Processes each unique hash group: downloads the first occurrence
    ## (if not already present with correct size), then hardlinks it to
    ## all other occurrences across mirrors.  Uses a thread pool for
    ## parallel downloads.
    ##
    ## @param hash_to_files  Dict from ``analyse_deduplication``.
    ## @param unique_files   Number of unique hash groups.
    ## @param config         Config instance with tuning parameters.
    ## @param dry_run        If True, print actions without changes.
    ## @return Tuple of ``(downloaded, hardlinked, skipped)``.

    if dry_run:
        print("\nDRY RUN - no changes made")
        print("\nDone!")
        return

    buffer_size = config.get('buffer_size', 1048576)
    parallel_downloads = config.get('parallel_downloads', 10)
    curl_timeout = config.get('curl_timeout', 900)
    max_retries = config.get('max_retries', 3)
    progress_interval = config.get('progress_interval', 1000)
    ipv4_only = config.get('disable_ipv6', True)

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
        nonlocal downloaded, hardlinked, skipped, processed_count, last_milestone, milestone_start_time, show_dots

        first_key, first_info = file_list[0]
        _, first_path = first_key
        dest_path = os.path.join(first_info['dest_base'], first_path)
        expected_size = int(first_info.get('size', 0))

        file_downloaded = False
        file_exists = False
        try:
            stat = os.stat(dest_path)
            if stat.st_size == expected_size:
                file_exists = True
                with counter_lock:
                    skipped += 1
            else:
                url = f"{first_info['upstream']}/{first_path}"
                success = False
                force_ipv4 = ipv4_only
                for attempt in range(max_retries):
                    progress_info = f" - {unique_files - processed_count} files remaining"
                    ok, status = download_with_curl(url, dest_path, curl_timeout, progress_info, force_ipv4=force_ipv4)
                    if ok:
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
                        if status == 'not_found':
                            print(f"  [WARN] 404/403 Not Found, not retrying this run: {first_path}", flush=True)
                            break

                        if (not ipv4_only) and (not force_ipv4) and status == 'timeout':
                            print(f"  [INFO] Timeout detected, retrying with IPv4 only: {first_path}", flush=True)
                            force_ipv4 = True
                        elif attempt < max_retries - 1:
                            print(f"  [WARN] Download failed (attempt {attempt+1}/{max_retries}), retrying: {first_path}", flush=True)

                if not success:
                    print(f"  [ERROR] Download failed after {max_retries} attempts: {first_path}", flush=True)
        except:
            url = f"{first_info['upstream']}/{first_path}"
            success = False
            force_ipv4 = ipv4_only
            for attempt in range(max_retries):
                progress_info = f" - {unique_files - processed_count} files remaining"
                ok, status = download_with_curl(url, dest_path, curl_timeout, progress_info, force_ipv4=force_ipv4)
                if ok:
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
                    if status == 'not_found':
                        print(f"  [WARN] 404/403 Not Found, not retrying this run: {first_path}", flush=True)
                        break

                    if (not ipv4_only) and (not force_ipv4) and status == 'timeout':
                        print(f"  [INFO] Timeout detected, retrying with IPv4 only: {first_path}", flush=True)
                        force_ipv4 = True
                    elif attempt < max_retries - 1:
                        print(f"  [WARN] Download failed (attempt {attempt+1}/{max_retries}), retrying: {first_path}", flush=True)

            if not success:
                print(f"  [FAIL] Download failed after {max_retries} attempts: {first_path}", flush=True)
                with processed_lock:
                    processed_count += 1
                return

        local_hardlinked = 0
        for key, info in file_list[1:]:
            _, path = key
            other_dest = os.path.join(info['dest_base'], path)
            if hardlink_file(dest_path, other_dest, sha256):
                local_hardlinked += 1

        if local_hardlinked > 0:
            with counter_lock:
                hardlinked += local_hardlinked

        with processed_lock:
            processed_count += 1

            current_milestone = (processed_count // progress_interval) * progress_interval
            if current_milestone > last_milestone:
                last_milestone = current_milestone
                milestone_start_time = time.time()
                show_dots = False
            elif not show_dots and (time.time() - milestone_start_time) > 1.0:
                show_dots = True

            if show_dots and sys.stdout.isatty() and not file_downloaded:
                print(".", end="", flush=True)

            if processed_count % progress_interval == 0:
                if show_dots:
                    print()
                print(f"  Processed {processed_count}/{unique_files} files... (downloaded: {downloaded}, hardlinked: {hardlinked}, skipped: {skipped})")

    print(f"\nProcessing {unique_files} unique files with {parallel_downloads} parallel downloads...")

    with ThreadPoolExecutor(max_workers=parallel_downloads) as executor:
        futures = {executor.submit(process_hash_group, sha256, file_list): sha256
                   for sha256, file_list in hash_to_files.items()}

        for future in as_completed(futures):
            try:
                future.result()
            except Exception as e:
                sha256 = futures[future]
                print(f"  [ERROR] Error processing hash group {sha256[:16]}...: {e}")

    print(f"  Processed {processed_count}/{unique_files} files... (downloaded: {downloaded}, hardlinked: {hardlinked}, skipped: {skipped})")
    print(f"  Processing complete!")

    return downloaded, hardlinked, skipped


def cleanup_mirrors(mirrors, global_files, dry_run):
    ## @brief Re-sync ``dists/`` metadata and clean up the pool for each mirror.
    ##
    ## Rebuilds the expected file set from *global_files* for each mirror
    ## and removes files not in that set from the pool.
    ##
    ## @param mirrors       List of mirror configuration dicts.
    ## @param global_files  Dict from ``collect_files``.
    ## @param dry_run       If True, print actions without changing files.

    print(f"\n{'='*60}")
    print("Syncing dists metadata and cleaning up pool")
    print(f"{'='*60}")

    for idx, mirror in enumerate(mirrors):
        name = mirror['name']
        upstream = mirror['upstream']
        dest = mirror['dest']
        architectures = mirror['architectures']
        expand_dists = mirror.get('expand_distributions', True)
        distributions = expand_distributions(mirror['distributions']) if expand_dists else mirror['distributions']
        components = mirror.get('components', COMPONENTS)
        print(f"\n[{name}] Syncing dists...")
        if not run_https_sync(distributions, dest, upstream, architectures, components, dry_run, force_ipv4=force_ipv4):
            print(f"  ERROR: HTTPS sync failed for {name}")

        print(f"\n[{name}] Building expected files list...")
        expected_files = set()
        for key, info in global_files.items():
            mirror_idx, path = key
            if mirror_idx == idx:
                expected_files.add(path)

        print(f"  Expected {len(expected_files)} files in pool")

        print(f"\n[{name}] Cleaning up pool...")
        cleanup_pool(dest, expected_files, dry_run)


def print_final_summary(mirrors, downloaded, hardlinked, skipped, initial_used):
    ## @brief Print the final summary of all operations.
    ##
    ## Shows download/hardlink/skip counts, disk usage delta, total
    ## hardlink savings, and IPv6 health warnings.
    ##
    ## @param mirrors       List of mirror configuration dicts.
    ## @param downloaded    Number of files downloaded.
    ## @param hardlinked    Number of hardlinks created.
    ## @param skipped       Number of files already present.
    ## @param initial_used  Initial disk usage in bytes.

    first_dest = mirrors[0]['dest']
    total, final_used, free = get_disk_usage(first_dest)
    delta = final_used - initial_used

    print(f"\nCalculating total hardlink savings...")
    total_hardlinked_files, total_hardlinked_bytes = calculate_total_hardlink_savings(mirrors)

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

    if IPV6_TROUBLE_MIRRORS:
        print(f"\nIPv6 summary")
        print(f"{'='*60}")
        print("The following mirrors had unreliable IPv6 and were forced to IPv4:")
        for name in sorted(IPV6_TROUBLE_MIRRORS):
            print(f"  - {name}")
        print("Consider adding 'disable_ipv6: true' either globally in mirror-dedupe.conf")
        print("or in the corresponding repos-available/*.conf entries for these mirrors.")

    print("\nDone!")
