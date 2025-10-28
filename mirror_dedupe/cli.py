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
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict

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
    
    parser.add_argument('config_dir', nargs='?', default='/etc/mirror-dedupe',
                       help='Path to configuration directory (default: /etc/mirror-dedupe)')
    parser.add_argument('--dry-run', action='store_true',
                       help='Show what would be done without actually doing it')
    parser.add_argument('--mirror', type=str,
                       help='Process only the specified mirror (by name)')
    parser.add_argument('--dedupe-only', action='store_true',
                       help='Only run deduplication phase (skip mirror sync)')
    
    args = parser.parse_args()
    
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
