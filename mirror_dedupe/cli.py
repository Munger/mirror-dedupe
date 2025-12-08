#!/usr/bin/env python3
"""
cli.py

  Ubuntu mirror synchronisation with global deduplication

Copyright (c) 2025 Tim Hosking
Email: tim@mungerware.com
Website: https://github.com/munger
Licence: MIT
"""

import os
import sys
import subprocess
import atexit
import signal
import argparse
import random
import shutil
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict

import yaml

from .config import load_config
from .utils import (acquire_lock, release_lock, signal_handler, 
                    get_disk_usage, format_bytes)
from .dedupe import expand_distributions
from .orchestrate import (run_orchestrator_mode, sync_mirrors, collect_files,
                          analyse_deduplication, check_existing_files, 
                          process_files, cleanup_mirrors, print_final_summary)


def main():
    """Main entry point for mirror-dedupe"""
    parser = argparse.ArgumentParser(
        description='Mirror repository with global deduplication',
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    
    parser.add_argument('--config', dest='config_dir', default='/etc/mirror-dedupe',
                       help='Path to configuration directory (default: /etc/mirror-dedupe)')
    parser.add_argument('--dry-run', action='store_true',
                       help='Show what would be done without actually doing it')
    parser.add_argument('--mirror', type=str,
                       help='Process only the specified mirror (by name)')
    parser.add_argument('--dedupe-only', action='store_true',
                       help='Only run deduplication phase (skip mirror sync)')
    parser.add_argument('--list', action='store_true',
                       help='List available mirrors (active and inactive)')
    parser.add_argument('--activate', metavar='MIRROR',
                       help='Activate a mirror by creating a symlink in repos-enabled')
    parser.add_argument('--deactivate', metavar='MIRROR',
                       help='Deactivate a mirror by removing its symlink from repos-enabled')
    parser.add_argument('--test', metavar='MIRROR',
                       help='Test a mirror configuration and summarise what it will fetch')
    parser.add_argument('--delete', metavar='MIRROR',
                       help='Deactivate a mirror and delete all its data (requires PIN confirmation)')
    
    args = parser.parse_args()
    
    # Configuration directory
    config_dir = args.config_dir

    # Management operations (mutually exclusive)
    management_ops = [
        bool(args.list),
        bool(args.activate),
        bool(args.deactivate),
        bool(args.test),
        bool(args.delete),
    ]
    if sum(1 for x in management_ops if x) > 1:
        print("ERROR: Only one of --list/--activate/--deactivate/--test/--delete may be used at a time", file=sys.stderr)
        sys.exit(1)

    repos_available = Path(config_dir) / 'repos-available'
    repos_enabled = Path(config_dir) / 'repos-enabled'

    if args.list:
        if not repos_available.exists():
            print(f"No repos-available directory at {repos_available}")
            sys.exit(1)

        available = {}
        for f in sorted(repos_available.glob('*.conf')):
            name = f.stem
            available[name] = f

        enabled = set()
        if repos_enabled.exists():
            for f in sorted(repos_enabled.glob('*.conf')):
                enabled.add(f.stem)

        if not available:
            print("No mirrors defined in repos-available")
            sys.exit(0)

        print(f"Mirrors in {config_dir}:")
        print("")
        for name in sorted(available.keys()):
            status = 'ACTIVE' if name in enabled else 'inactive'
            print(f"  {name:30s} {status}")
        sys.exit(0)

    if args.activate:
        name = args.activate
        src = repos_available / f"{name}.conf"
        dst = repos_enabled / f"{name}.conf"

        if not src.exists():
            print(f"ERROR: Mirror '{name}' does not exist in repos-available ({src})", file=sys.stderr)
            sys.exit(1)

        os.makedirs(repos_enabled, exist_ok=True)

        if dst.exists():
            print(f"Mirror '{name}' is already active ({dst})")
            sys.exit(0)

        os.symlink(os.path.relpath(src, repos_enabled), dst)
        print(f"Activated mirror '{name}' -> {dst}")
        sys.exit(0)

    if args.deactivate:
        name = args.deactivate
        dst = repos_enabled / f"{name}.conf"

        if not dst.exists():
            print(f"Mirror '{name}' is not active ({dst} not found)")
            sys.exit(0)

        dst.unlink()
        print(f"Deactivated mirror '{name}'")
        sys.exit(0)

    if args.test:
        name = args.test
        src = repos_available / f"{name}.conf"
        if not src.exists():
            print(f"ERROR: Mirror '{name}' does not exist in repos-available ({src})", file=sys.stderr)
            sys.exit(1)

        with open(src, 'r') as f:
            mirror_cfg = yaml.safe_load(f) or {}

        upstream = mirror_cfg.get('upstream')
        dest = mirror_cfg.get('dest')
        architectures = mirror_cfg.get('architectures', [])
        distributions = mirror_cfg.get('distributions', [])
        components = mirror_cfg.get('components', [])
        gpg_key_url = mirror_cfg.get('gpg_key_url')
        gpg_key_path = mirror_cfg.get('gpg_key_path')

        if not upstream:
            print(f"ERROR: Mirror '{name}' has no 'upstream' defined in {src}", file=sys.stderr)
            sys.exit(1)

        print(f"Testing mirror '{name}'")
        print(f"  Config file: {src}")
        print(f"  Upstream:   {upstream}")
        if dest:
            print(f"  Dest:       {dest}")

        print("  Connectivity check (HTTP HEAD)...")
        result = subprocess.run(
            ['curl', '-Isf', '--max-time', '10', upstream],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if result.returncode == 0:
            print("    OK: Upstream is reachable over HTTP/HTTPS")
        else:
            print("    ERROR: Upstream is not reachable over HTTP/HTTPS (curl failed)")
            sys.exit(1)

        if gpg_key_url:
            print("")
            print("  GPG key URL check (HTTP HEAD)...")
            key_result = subprocess.run(
                ['curl', '-Isf', '--max-time', '10', gpg_key_url],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            if key_result.returncode == 0:
                print(f"    OK: GPG key URL is reachable: {gpg_key_url}")
            else:
                print(f"    ERROR: GPG key URL is not reachable: {gpg_key_url}")
                sys.exit(1)

        if architectures:
            print(f"  Architectures: {', '.join(architectures)}")
        if distributions:
            print(f"  Distributions: {', '.join(distributions)}")
        if components:
            print(f"  Components:    {', '.join(components)}")
        if gpg_key_url or gpg_key_path:
            print("  GPG key:")
            if gpg_key_url:
                print(f"    URL:  {gpg_key_url}")
            if gpg_key_path:
                print(f"    Path: {gpg_key_path}")

        print("")
        print("Summary: This mirror appears reachable. It is configured to fetch the above")
        print("         distributions/components/architectures if enabled.")
        sys.exit(0)

    if args.delete:
        name = args.delete
        src = repos_available / f"{name}.conf"
        if not src.exists():
            print(f"ERROR: Mirror '{name}' does not exist in repos-available ({src})", file=sys.stderr)
            sys.exit(1)

        # Load main config for repo_root
        main_config_path = Path(config_dir) / 'mirror-dedupe.conf'
        repo_root = '/srv/mirror/repos'
        if main_config_path.exists():
            with open(main_config_path, 'r') as f:
                main_cfg = yaml.safe_load(f) or {}
                repo_root = main_cfg.get('repo_root', repo_root)

        with open(src, 'r') as f:
            mirror_cfg = yaml.safe_load(f) or {}

        dest = mirror_cfg.get('dest')
        if not dest:
            print(f"ERROR: Mirror '{name}' has no 'dest' defined in {src}", file=sys.stderr)
            sys.exit(1)

        if os.path.isabs(dest):
            data_path = dest
        else:
            data_path = os.path.join(repo_root, dest)

        if not os.path.abspath(data_path).startswith(os.path.abspath(repo_root)):
            print(f"ERROR: Refusing to delete data directory outside repo_root: {data_path}", file=sys.stderr)
            sys.exit(1)

        print(f"DELETE mirror '{name}'")
        print(f"  Config file:      {src}")
        print(f"  Data directory:   {data_path}")
        enabled_link = repos_enabled / f"{name}.conf"
        if enabled_link.exists():
            print(f"  Active symlink:   {enabled_link}")
        else:
            print("  Active symlink:   (not active)")

        pin = f"{random.randint(0, 9999):04d}"
        print("")
        print("This is a DESTRUCTIVE operation.")
        print("It will:")
        print("  - Deactivate the mirror (remove symlink in repos-enabled, if present)")
        print("  - Recursively delete ALL data under the data directory above")
        print("")
        print(f"To confirm, type the following PIN: {pin}")
        entered = input("PIN: ").strip()
        if entered != pin:
            print("PIN mismatch - aborting delete")
            sys.exit(1)

        if enabled_link.exists():
            enabled_link.unlink()
            print(f"Deactivated mirror '{name}' (removed {enabled_link})")

        if os.path.exists(data_path):
            shutil.rmtree(data_path)
            print(f"Deleted data directory: {data_path}")
        else:
            print(f"Data directory does not exist: {data_path}")

        print("Mirror delete completed.")
        sys.exit(0)
    
    # Determine mode and acquire appropriate lock
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
    
    # Load configuration
    config_dir = args.config_dir
    config = load_config(config_dir)
    mirrors = config.get('mirrors', [])
    
    if not mirrors:
        print("No mirrors defined in configuration")
        sys.exit(1)
    
    print(f"\n{'='*60}")
    print(f"Loaded {len(mirrors)} mirror(s) from configuration")
    print(f"{'='*60}")
    
    # Orchestrator mode: spawn subprocess for each mirror
    if not args.mirror and not args.dedupe_only:
        run_orchestrator_mode(mirrors, config_dir, args.dry_run)
        # This function exits, so we never reach here
    
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
        sync_mirrors(mirrors, args.dry_run)

    # If no_hardlinks is enabled in the main config, skip dedupe entirely
    if config.get('no_hardlinks'):
        print(f"\n{'='*60}")
        print("NO_HARDLINKS enabled in configuration - skipping deduplication phase")
        print("mirror-dedupe will behave as a plain mirror (no global hardlinking)")
        print(f"{'='*60}")
        print("Mirror sync completed.")
        sys.exit(0)

    # Collect all files needed across all mirrors
    global_files = collect_files(mirrors)
    
    # Analyse deduplication potential
    hash_to_files, unique_files = analyse_deduplication(global_files)
    
    # Check existing files
    check_existing_files(hash_to_files)
    
    # Get initial disk usage
    print(f"\n{'='*60}")
    print("Initial disk usage")
    print(f"{'='*60}")
    first_dest = mirrors[0]['dest']
    total, initial_used, free = get_disk_usage(first_dest)
    print(f"Overall mirror filesystem: Used: {format_bytes(initial_used)}, Free: {format_bytes(free)}")
    
    # In single-mirror mode, note that cross-mirror deduplication will be handled separately
    if args.mirror:
        print(f"\n{'='*60}")
        print(f"Single mirror mode: Deduplication will be handled separately")
        print(f"{'='*60}")
    
    # Process files (download and hardlink)
    downloaded, hardlinked, skipped = process_files(hash_to_files, unique_files, config, args.dry_run)
    
    # In single-mirror mode, exit after downloading (skip cleanup and cross-mirror dedup)
    if args.mirror:
        print(f"\nMirror '{args.mirror}' sync completed successfully!")
        sys.exit(0)
    
    # Cleanup mirrors
    cleanup_mirrors(mirrors, global_files, args.dry_run)
    
    # Print final summary
    print_final_summary(mirrors, downloaded, hardlinked, skipped, initial_used)


if __name__ == '__main__':
    main()
