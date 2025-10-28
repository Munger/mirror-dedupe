#!/usr/bin/env python3
"""
dedupe.py

  Ubuntu mirror synchronisation with global deduplication

Copyright (c) 2025 Tim Hosking
Email: tim@mungerware.com
Website: https://github.com/munger
Licence: MIT
"""

import os
from typing import Set, Tuple


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
