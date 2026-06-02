#!/usr/bin/env python3
## @file dedupe.py
##
## @brief Deduplication helpers for mirror-dedupe.
##
## Provides ``hardlink_file`` for creating hardlinks, ``expand_distributions``
## for expanding base suite names to include pocket variants, and
## ``cleanup_pool`` for removing stale files from per-mirror pool directories.
##
## @copyright Copyright (c) 2025-2026 Tim Hosking
## @see https://github.com/munger
## @par Licence: MIT

import os
from typing import Set, Tuple


def hardlink_file(source: str, dest: str, expected_hash: str = None) -> bool:
    ## @brief Create a hardlink from *source* to *dest*.
    ##
    ## Creates parent directories as needed.  If *dest* already exists and
    ## shares the same inode as *source*, it is a no-op.  Otherwise *dest*
    ## is removed and replaced with a new hardlink.
    ##
    ## @param source          Existing file to link from.
    ## @param dest            Target path for the hardlink.
    ## @param expected_hash   Ignored (for future use).
    ## @return True if the hardlink was created or already exists.

    dest_dir = os.path.dirname(dest)
    os.makedirs(dest_dir, exist_ok=True)

    try:
        if os.path.exists(dest) and os.path.samefile(source, dest):
            return True

        if os.path.exists(dest):
            os.remove(dest)

        os.link(source, dest)
        return True
    except Exception as e:
        print(f"  Error hardlinking {source} -> {dest}: {e}")
        return False


def expand_distributions(distributions: list) -> list:
    ## @brief Expand distribution names to include standard pocket variants.
    ##
    ## For each base suite (e.g. ``"noble"``), adds ``-updates``,
    ## ``-security``, ``-backports``, and ``-proposed`` suffixes unless
    ## the name already contains a hyphen.
    ##
    ## @param distributions  List of distribution names to expand.
    ## @return Expanded list of distribution names.

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
    ## @brief Remove files from the pool directory that are not in the expected set.
    ##
    ## Walks the ``pool/`` subtree under *dest_base*, removes any file
    ## whose relative path is not in *expected_files*, and cleans up empty
    ## directories.
    ##
    ## @param dest_base       Base destination directory for the mirror.
    ## @param expected_files  Set of relative paths that should be kept.
    ## @param dry_run         If True, only print what would be done.
    ## @return Tuple of ``(removed_files, removed_dirs)``.

    print(f"\n{'='*60}")
    print("Cleaning up pool directory")
    print(f"{'='*60}")

    pool_path = os.path.join(dest_base, 'pool')
    if not os.path.exists(pool_path):
        print("  No pool directory found")
        return (0, 0)

    removed_files = 0
    removed_dirs = 0

    for root, dirs, files in os.walk(pool_path, topdown=False):
        for filename in files:
            full_path = os.path.join(root, filename)
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

        for dirname in dirs:
            dir_path = os.path.join(root, dirname)
            try:
                if not os.listdir(dir_path):
                    if dry_run:
                        removed_dirs += 1
                    else:
                        os.rmdir(dir_path)
                        removed_dirs += 1
            except:
                pass

    if dry_run:
        print(f"\nWould remove: {removed_files} files, {removed_dirs} directories")
    else:
        print(f"\nRemoved: {removed_files} files, {removed_dirs} directories")

    return (removed_files, removed_dirs)
