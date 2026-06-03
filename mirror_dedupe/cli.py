#!/usr/bin/env python3
## @file cli.py
##
## @brief CLI entry point and management operations for mirror-dedupe.
##
## Provides the ``main()`` entry point with subcommands for listing,
## activating, deactivating, testing, and deleting mirrors.
##
## Sync operations (``--mirror``, ``--dedupe-only``, default mode) are
## defined here for interface documentation but are not yet wired — they
## will be connected to the schema-based sync pipeline in a future version.
##
## @copyright Copyright (c) 2025-2026 Tim Hosking
## @see https://github.com/munger
## @par Licence: MIT

import os
import sys
import subprocess
import argparse
import random
import shutil
from pathlib import Path

import yaml

from .config import Config, DEFAULT_CONFIG_DIR


def main():
    ## @brief Main entry point for mirror-dedupe.

    parser = argparse.ArgumentParser(
        description='Mirror repository with global deduplication',
        formatter_class=argparse.RawDescriptionHelpFormatter
    )

    parser.add_argument('--config', dest='config_dir', default=None,
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

    config_dir = args.config_dir or DEFAULT_CONFIG_DIR
    cfg_main = Config.load(config_dir)

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

    config_dir_path = Path(config_dir)
    repos_available = config_dir_path / 'repos-available'
    repos_enabled = config_dir_path / 'repos-enabled'

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
        if not upstream:
            upstreams = mirror_cfg.get('upstreams', [])
            if upstreams:
                upstream = upstreams[0].get('url', '') if isinstance(upstreams[0], dict) else ''
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

        repo_root = cfg_main.repo_root

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

    # Sync operations (--mirror, --dedupe-only, default mode) are not yet
    # wired — they will be connected to the schema-based sync pipeline
    # (Repo.sync() / Apt.sync()) in a future version.
    print("Mirror sync is not yet implemented in this version.", file=sys.stderr)
    print("Use `mirror-dedupe-scan` to discover repositories and", file=sys.stderr)
    print("run `--list`/`--activate`/`--deactivate`/`--test`/`--delete`", file=sys.stderr)
    print("to manage existing configurations.", file=sys.stderr)
    sys.exit(0)


if __name__ == '__main__':
    main()
