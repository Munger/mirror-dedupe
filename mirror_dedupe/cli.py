#!/usr/bin/env python3
## @file cli.py
##
## @brief CLI entry point and management operations for mirror-dedupe.
##
## Provides the ``main()`` entry point with subcommands for listing,
## activating, deactivating, testing, scanning, and deleting mirrors.
##
## The ``sync`` subcommand drives the schema-based sync pipeline via
## ``Repos.from_names().sync_all()``.
##
## The ``scan`` subcommand discovers upstream repository metadata and
## generates a config file (replacing the former ``mirror-dedupe-scan``
## separate executable).
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
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from . import __version__
from .config import Config, DEFAULT_CONFIG_DIR
from .lib.log import log


def _repo_completer(*, enabled_only: bool = False, available_only: bool = False):
    ## @brief Return an argcomplete completer for repo NAME arguments.
    ##
    ## @param enabled_only    Complete only from repos-enabled.
    ## @param available_only  Complete only from repos-available.
    def _completer(prefix, parsed_args, **_):
        try:
            config_dir = (
                getattr(parsed_args, 'config_dir', None)
                or os.environ.get('MIRROR_DEDUPE_CONFIG_DIR')
            )
            cfg = Config.load(config_dir)
            base = Path(cfg.config_dir)
            if available_only:
                names = sorted(f.stem for f in (base / 'repos-available').glob('*.conf'))
            elif enabled_only:
                names = sorted(f.stem for f in (base / 'repos-enabled').glob('*.conf'))
            else:
                names = cfg.list_repo_names()
            return [n for n in names if n.startswith(prefix)]
        except Exception:
            return []
    return _completer


def _snapshot_name_completer(prefix, parsed_args, **_):
    ## @brief Complete repo names that have at least one snapshot.
    try:
        config_dir = (
            getattr(parsed_args, 'config_dir', None)
            or os.environ.get('MIRROR_DEDUPE_CONFIG_DIR')
        )
        cfg = Config.load(config_dir)
        snap_base = Path(cfg.repo_root) / 'Snapshots'
        if not snap_base.is_dir():
            return []
        return [d.name for d in sorted(snap_base.iterdir()) if d.is_dir() and d.name.startswith(prefix)]
    except Exception:
        return []


def _snapshot_ts_completer(prefix, parsed_args, **_):
    ## @brief Complete snapshot timestamps for the repo named in parsed_args.name.
    try:
        name = getattr(parsed_args, 'name', None)
        if not name:
            return []
        config_dir = (
            getattr(parsed_args, 'config_dir', None)
            or os.environ.get('MIRROR_DEDUPE_CONFIG_DIR')
        )
        cfg = Config.load(config_dir)
        snap_dir = Path(cfg.repo_root) / 'Snapshots' / name
        if not snap_dir.is_dir():
            return []
        return [d.name for d in sorted(snap_dir.iterdir(), reverse=True)
                if d.is_dir() and d.name.startswith(prefix)]
    except Exception:
        return []


def _resolve_dest(name: str, cfg: Config) -> Optional[str]:
    ## @brief Resolve a repo *name* to its absolute ``dest`` path.
    dest = cfg.resolve_dest(name)
    if dest:
        return dest
    fallback = os.path.join(cfg.repo_root, name)
    return fallback if os.path.isdir(fallback) else None


def _list_dests(cfg: Config) -> List[str]:
    ## @brief Return all dest directories under ``repo_root``.
    dests: set[str] = set()
    root = Path(cfg.repo_root)
    if root.exists():
        for entry in root.iterdir():
            if not entry.is_dir():
                continue
            if entry.name.startswith(".") or entry.name == "Snapshots":
                continue
            dests.add(str(entry.resolve()))
    for adest in cfg.additional_repos.values():
        dests.add(adest)
    return sorted(dests)


def _timestamp() -> str:
    ## @brief Return an ISO-8601 compact timestamp string safe for directory names.
    from .lib.datetimeutils import fmt_compact_ts
    return fmt_compact_ts()


def _pin_confirm(action_desc: str, force: bool) -> bool:
    ## @brief Ask the user to confirm a destructive operation via a random PIN.
    if force:
        return True

    pin = f"{random.randint(0, 9999):04d}"
    print("")
    print(f"This is a DESTRUCTIVE operation: {action_desc}")
    print("")
    print(f"To confirm, type the following PIN: {pin}")
    entered = input("PIN: ").strip()
    if entered != pin:
        print("PIN mismatch - aborting")
        return False
    return True


def _resolve_snapshot_ts(snapshot_dir: str) -> Optional[str]:
    ## @brief Return the latest timestamp subdirectory under *snapshot_dir*.
    p = Path(snapshot_dir)
    if not p.exists():
        return None
    ts_dirs = sorted(
        (d.name for d in p.iterdir() if d.is_dir() and d.name[:8].isdigit()),
        reverse=True,
    )
    return ts_dirs[0] if ts_dirs else None


def _resolve_name_or_index(val: str, candidates: List[str], label: str = "name") -> str:
    ## @brief Resolve a value that may be a name or a numeric index into *candidates*.
    if val.isdigit():
        idx = int(val)
        if 0 <= idx < len(candidates):
            return candidates[idx]
        log(
            f"ERROR: Index {idx} is out of range for {label} "
            f"(valid indices: 0-{len(candidates) - 1})",
            level="ERROR",
        )
        sys.exit(1)
    return val


def _fmt_ts(raw: str) -> str:
    ## @brief Format a compact timestamp (``YYYYmmDDTHHMMSS``) as human-readable.
    if len(raw) >= 15 and raw[8] == "T":
        return (
            f"{raw[:4]}-{raw[4:6]}-{raw[6:8]} "
            f"{raw[9:11]}:{raw[11:13]}:{raw[13:15]}"
        )
    return raw


def _snapshot_size(snapshot_path: Path) -> str:
    ## @brief Return a human-readable total size for a snapshot directory.
    try:
        result = subprocess.run(
            ["du", "-sh", str(snapshot_path)],
            capture_output=True, text=True, timeout=30,
        )
        return result.stdout.split(maxsplit=1)[0] if result.returncode == 0 else "(unknown)"
    except Exception:
        return "(unknown)"


def main():
    ## @brief Main entry point for mirror-dedupe.
    ## @return None

    # SIGINT kills tracked curl subprocesses immediately.
    import signal as _signal

    from .lib.subproc import kill_active_subprocesses_signal_safe
    _signal.signal(
        _signal.SIGINT,
        lambda signum, frame: (
            kill_active_subprocesses_signal_safe(),
            os._exit(130),
        ),
    )

    from .deps import check_dependencies
    check_dependencies()

    parser = argparse.ArgumentParser(
        prog='mirror-dedupe',
        description='Mirror repository with global deduplication',
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # -- Global options ------------------------------------------------
    parser.add_argument('--version', '-v', action='version',
                        version=f'mirror-dedupe {__version__}')
    parser.add_argument('--config-dir', dest='config_dir', default=None, metavar='PATH',
                        help=f'Path to config directory (default: {DEFAULT_CONFIG_DIR})')
    parser.add_argument('--debug', action='store_true',
                        help='Enable debug-level log output')

    sub = parser.add_subparsers(dest='command', metavar='COMMAND')
    sub.required = True

    # -- sync ----------------------------------------------------------
    p = sub.add_parser('sync', help='Run the full sync pipeline for all enabled mirrors')
    p.add_argument('name', nargs='?', metavar='NAME',
                   help='Sync only this mirror (default: all enabled)'
                   ).completer = _repo_completer(enabled_only=True)
    p.add_argument('--sweep-pool', action='store_true', dest='sweep_pool',
                   help='Sweep orphaned pool entries after sync')
    p.add_argument('--dedupe-only', action='store_true', dest='dedupe_only',
                   help='Skip metadata sync; only hardlink pool entries into repo trees')

    # -- scan ----------------------------------------------------------
    p_scan = sub.add_parser('scan', help='Probe an upstream URL and generate a repo YAML config')
    p = p_scan
    p.add_argument('upstream', nargs='?', metavar='URL',
                   help='Upstream URL (alternative to -U/--upstream)')
    p.add_argument('-U', '--upstream', '--upstreams', dest='upstreams',
                   action='extend', nargs='+', metavar='URL',
                   help='Upstream URL(s); first is primary, subsequent are fallbacks')
    p.add_argument('--name', metavar='NAME',
                   help='Repository name')
    p.add_argument('--dest', metavar='DEST',
                   help='Destination path relative to mirror_root/repos (defaults to --name)')
    p.add_argument('--out', dest='out_dir', metavar='DIR',
                   help='Output directory for generated config (required)')
    p.add_argument('-d', '--distribution', action='append',
                   dest='distribution', metavar='SUITE',
                   help='Restrict to a specific distribution/suite (may be repeated)')
    p.add_argument('-D', '--distributions', dest='distributions', metavar='SUITES',
                   help='Comma-separated list of distributions/suites to mirror')
    p.add_argument('--arch', action='append', dest='arch', metavar='ARCH',
                   help='Architecture to include (may be repeated)')
    p.add_argument('--architectures', dest='architectures', metavar='ARCHES',
                   help='Comma-separated list of architectures')
    p.add_argument('--component', action='append', dest='component', metavar='COMP',
                   help='Component to include (may be repeated)')
    p.add_argument('--components', dest='components', metavar='COMPS',
                   help='Comma-separated list of components')
    p.add_argument('--repo-type', dest='repo_type', metavar='TYPE',
                   help='Force a repo type (e.g. "apt") for unusual layouts')
    p.add_argument('-G', '--gpg-key-url', dest='gpg_key_url', metavar='URL',
                   help='GPG key URL for Release file signature verification')
    collapse_scan = p.add_mutually_exclusive_group()
    collapse_scan.add_argument('--collapse-dists', dest='collapse_dists',
                               action='store_true',
                               help='Collapse discovered suites to base names')
    collapse_scan.add_argument('--no-collapse-dists', dest='collapse_dists',
                               action='store_false',
                               help='Emit all discovered suite variants explicitly')
    p.set_defaults(collapse_dists=None)
    p.add_argument('--emit-json', action='store_true', default=False,
                   help='Also write a JSON snapshot of the discovered structure')
    p.add_argument('--no-filter', action='store_true', default=False,
                   help='Ignore architecture filters from mirror-dedupe.conf; '
                        'emit all discovered arches')

    # -- sweep-pool ----------------------------------------------------
    sub.add_parser('sweep-pool', help='Remove orphaned pool entries (st_nlink == 1)')

    # -- list ----------------------------------------------------------
    sub.add_parser('list', help='List available mirrors (active and inactive)')

    # -- activate ------------------------------------------------------
    p = sub.add_parser('activate', help='Enable a mirror via symlink in repos-enabled')
    p.add_argument('name', metavar='NAME').completer = _repo_completer(available_only=True)

    # -- deactivate ----------------------------------------------------
    p = sub.add_parser('deactivate', help='Disable a mirror by removing its repos-enabled symlink')
    p.add_argument('name', metavar='NAME').completer = _repo_completer(enabled_only=True)

    # -- test ----------------------------------------------------------
    p = sub.add_parser('test', help='Check upstream reachability and summarise what will be synced')
    p.add_argument('name', metavar='NAME').completer = _repo_completer()

    # -- reinitialise --------------------------------------------------
    p = sub.add_parser('reinitialise',
                       help='Snapshot a repo and remove its data dir (leaves activation)')
    p.add_argument('name', metavar='NAME').completer = _repo_completer(enabled_only=True)
    p.add_argument('--force', action='store_true',
                   help='Bypass PIN confirmation')

    # -- relink-pool ---------------------------------------------------
    p = sub.add_parser('relink-pool',
                       help='Re-link pool hashes with all managed repos and snapshots')
    p.add_argument('--force', action='store_true',
                   help='Skip confirmation prompt')

    # -- snapshot (nested subcommands) ---------------------------------
    p_snap = sub.add_parser('snapshot', help='Manage repo snapshots')
    snap_sub = p_snap.add_subparsers(dest='snap_action', metavar='ACTION')
    snap_sub.required = True

    ps = snap_sub.add_parser('create', help='Create a hardlink snapshot of a repo dest')
    ps.add_argument('name', metavar='NAME', nargs='?', default='ALL',
                    help='Repo name (default: ALL repos)'
                    ).completer = _repo_completer(enabled_only=True)

    ps = snap_sub.add_parser('list', help='List available snapshots for a repo or ALL')
    ps.add_argument('name', metavar='NAME', nargs='?', default='ALL',
                    help='Repo name (default: ALL repos)'
                    ).completer = _snapshot_name_completer

    ps = snap_sub.add_parser('restore', help='Restore a snapshot to the repo dest')
    ps.add_argument('name', metavar='NAME',
                    help='Repo name').completer = _snapshot_name_completer
    ps.add_argument('snapshot', metavar='SNAPSHOT', nargs='?', default='',
                    help='Snapshot timestamp (default: latest)'
                    ).completer = _snapshot_ts_completer
    ps.add_argument('--force', action='store_true',
                    help='Bypass PIN confirmation')
    ps.add_argument('--no-backup', action='store_true', dest='no_backup',
                    help='Skip current-state backup before restore')

    ps = snap_sub.add_parser('delete', help='Delete a snapshot directory')
    ps.add_argument('name', metavar='NAME',
                    help='Repo name').completer = _snapshot_name_completer
    ps.add_argument('snapshot', metavar='SNAPSHOT', nargs='?', default='',
                    help='Snapshot timestamp (omit to delete entire snapshot group)'
                    ).completer = _snapshot_ts_completer
    ps.add_argument('--force', action='store_true',
                    help='Bypass PIN confirmation')

    # -- stats ---------------------------------------------------------
    p = sub.add_parser('stats',
                       help='Print or reset sync statistics')
    p.add_argument('args', nargs='*', metavar='[reset] [NAME|all]',
                   help='"stats" = latest per repo, "stats all" = full history, '
                        '"stats NAME" = history for one repo, '
                        '"stats reset [NAME]" = clear stats')
    p.add_argument('--force', action='store_true',
                   help='Bypass PIN confirmation on reset')

    # -- migrate -------------------------------------------------------
    sub.add_parser('migrate',
                   help='Migrate a legacy repo tree to the current layout (not yet implemented)')

    # argcomplete: context-aware tab completion (optional dependency)
    try:
        import argcomplete
        argcomplete.autocomplete(parser)
    except ImportError:
        pass

    args = parser.parse_args()

    if args.debug:
        from .lib.log import set_debug
        set_debug(True)

    if args.command == 'scan' and not args.upstream and not args.upstreams:
        p_scan.print_help()
        sys.exit(2)

    config_dir = args.config_dir or os.environ.get('MIRROR_DEDUPE_CONFIG_DIR')
    cfg_main = Config.load(config_dir)

    # ------------------------------------------------------------------
    # sync
    # ------------------------------------------------------------------
    if args.command == 'sync':
        from .schema.repo import Repos, pool_sweep_safe

        repo_names: List[str] = []
        if args.name:
            repo_names.append(args.name)
        else:
            enabled = Path(cfg_main.config_dir) / 'repos-enabled'
            if enabled.exists():
                repo_names = sorted(
                    f.stem for f in enabled.glob("*.conf")
                )
            if not repo_names:
                log(
                    "No active repos found. Use 'mirror-dedupe list' to see available mirrors "
                    "and 'mirror-dedupe activate NAME' to enable one.",
                    level="ERROR",
                )
                sys.exit(1)

        repos = Repos.from_names(repo_names, config_dir=config_dir)
        repos.sync_all(config_dir=config_dir)
        if args.sweep_pool or cfg_main.sweep_pool_after_sync:
            pool_sweep_safe(cfg_main, fail_if_locked=False)
        sys.exit(0)

    # ------------------------------------------------------------------
    # scan
    # ------------------------------------------------------------------
    if args.command == 'scan':
        if not args.out_dir:
            log("ERROR: --out <dir> is required for scan", level="ERROR")
            sys.exit(1)
        try:
            from .scan import scan as scan_repo, generate_config

            scan_upstreams: List[str] = []
            if args.upstreams:
                scan_upstreams = [u for u in args.upstreams if u]
            elif args.upstream:
                scan_upstreams = [args.upstream]
            else:
                log(
                    "ERROR: No upstream URL provided for scan. "
                    "Pass a URL as a positional argument or via --upstream.",
                    level="ERROR",
                )
                sys.exit(1)

            scan_name = args.name or scan_upstreams[0].split("://")[-1].split("/")[0]

            def _split_csv(values):
                items = []
                for v in values or []:
                    if not v:
                        continue
                    parts = [p.strip() for p in v.split(",")]
                    items.extend([p for p in parts if p])
                seen = set()
                result = []
                for item in items:
                    if item not in seen:
                        seen.add(item)
                        result.append(item)
                return result

            arch_override = _split_csv(
                (args.arch or []) + ([args.architectures] if args.architectures else [])
            )
            if not arch_override:
                arch_override = None

            component_override = _split_csv(
                (args.component or []) + ([args.components] if args.components else [])
            )
            if not component_override:
                component_override = None

            distribution_overrides: Optional[List[str]] = None
            distribution_values: List[str] = []
            if args.distribution:
                distribution_values.extend(args.distribution)
            if args.distributions:
                distribution_values.extend(_split_csv([args.distributions]))
            if distribution_values:
                seen_d = set()
                ordered: List[str] = []
                for d in distribution_values:
                    if d and d not in seen_d:
                        seen_d.add(d)
                        ordered.append(d)
                distribution_overrides = ordered or None

            try:
                repo = scan_repo(
                    scan_name,
                    scan_upstreams,
                    repo_type=args.repo_type,
                    distribution_overrides=distribution_overrides,
                )
            except NotImplementedError:
                log(
                    f"ERROR: No supported Repo implementation could parse upstream "
                    f"{scan_upstreams[0]!r}.",
                    level="ERROR",
                )
                sys.exit(1)

            if args.gpg_key_url:
                repo.gpg_key_url = args.gpg_key_url

            scan_dest = args.dest or scan_name

            global_collapse_dists = bool(cfg_main.collapse_distributions)

            def _normalize_arch_mask(value):
                if isinstance(value, str):
                    v = value.strip()
                    if v.lower() in ("*", "all") or not v:
                        return None
                    return [v]
                if isinstance(value, list):
                    return value
                return None

            config = generate_config(
                repo,
                scan_dest,
                gpg_key_url=args.gpg_key_url,
                distribution_overrides=distribution_overrides,
                arch_override=arch_override,
                component_override=component_override,
                global_arch_mask=(
                    None
                    if args.no_filter
                    else _normalize_arch_mask(cfg_main.architectures)
                ),
                collapse_dists=(
                    args.collapse_dists
                    if args.collapse_dists is not None
                    else global_collapse_dists
                ),
            )

            out_dir = Path(args.out_dir)
            out_dir.mkdir(parents=True, exist_ok=True)
            config_file = out_dir / f"{repo.name}.conf"
            config_file.write_text(config)

            if args.emit_json:
                try:
                    snapshot_path = out_dir / f"{repo.name}.json"
                    snapshot_path.write_text(
                        json.dumps(repo.snapshot(), indent=2)
                    )
                    log(f"Snapshot saved to: {snapshot_path}")
                except Exception as e:
                    log(f"Warning: failed to write snapshot: {e}", level="WARN")

            log(f"Configuration saved to: {config_file}")
            log(
                f"\nNext steps:\n"
                f"  # Test the repository configuration before activating it\n"
                f"  mirror-dedupe test {repo.name}\n\n"
                f"  # If the test looks good, activate the repository:\n"
                f"  mirror-dedupe activate {repo.name}\n"
            )
        except KeyboardInterrupt:
            print("")
            sys.exit(130)
        sys.exit(0)

    config_dir_path = Path(cfg_main.config_dir)
    repos_available = config_dir_path / 'repos-available'
    repos_enabled = config_dir_path / 'repos-enabled'

    # ------------------------------------------------------------------
    # sweep-pool
    # ------------------------------------------------------------------
    if args.command == 'sweep-pool':
        from .schema.repo import pool_sweep_safe
        if not pool_sweep_safe(cfg_main, fail_if_locked=True):
            sys.exit(1)
        sys.exit(0)

    # ------------------------------------------------------------------
    # list
    # ------------------------------------------------------------------
    if args.command == 'list':
        if not repos_available.exists() and not repos_enabled.exists():
            print(f"No repos-available or repos-enabled at {cfg_main.config_dir}")
            sys.exit(1)

        available: Dict[str, Path] = {}
        if repos_available.exists():
            for f in sorted(repos_available.glob("*.conf")):
                available[f.stem] = f

        enabled_stems: set[str] = set()
        enabled_orphans: set[str] = set()
        if repos_enabled.exists():
            for f in repos_enabled.glob("*.conf"):
                if f.is_symlink():
                    enabled_stems.add(f.stem)
                else:
                    enabled_orphans.add(f.stem)

        all_repos = sorted(set(available.keys()) | enabled_orphans)
        if not all_repos:
            print("No mirrors defined")
            sys.exit(0)

        print(f"Mirrors in {cfg_main.config_dir}:")
        print("")
        for i, name in enumerate(all_repos):
            if name in available:
                has_symlink = name in enabled_stems
                has_orphan = name in enabled_orphans
                if has_symlink or has_orphan:
                    status = "ACTIVE"
                    if has_orphan:
                        status += " [standalone]"
                else:
                    status = "inactive"
            else:
                status = "ACTIVE [enabled only]"
            print(f"  [{i}] {name:30s} {status}")
        sys.exit(0)

    # ------------------------------------------------------------------
    # activate
    # ------------------------------------------------------------------
    if args.command == 'activate':
        name = args.name
        if repos_available.exists():
            available_configs = sorted(f.stem for f in repos_available.glob("*.conf"))
            if available_configs:
                name = _resolve_name_or_index(name, available_configs, "mirror")
        src = repos_available / f"{name}.conf"
        dst = repos_enabled / f"{name}.conf"

        if not src.exists():
            log(f"ERROR: Mirror '{name}' does not exist in repos-available ({src})", level="ERROR")
            sys.exit(1)

        os.makedirs(repos_enabled, exist_ok=True)
        rel_src = os.path.relpath(src, repos_enabled)

        if dst.exists():
            if dst.is_symlink():
                if os.path.normpath(dst.resolve()) == os.path.normpath(src):
                    log(f"Mirror '{name}' is already active ({dst})", level="INFO")
                    sys.exit(0)
                log(
                    f"{dst} is a symlink but does not point to {src}. "
                    f"It must be manually removed from {repos_enabled} first.",
                    level="ERROR",
                )
                sys.exit(1)
            log(
                f"{dst} is not a symlink and cannot be activated over. "
                f"It must be manually removed from {repos_enabled} first.",
                level="ERROR",
            )
            sys.exit(1)

        os.symlink(rel_src, dst)
        log(f"Activated mirror '{name}' -> {dst}", level="INFO")
        sys.exit(0)

    # ------------------------------------------------------------------
    # deactivate
    # ------------------------------------------------------------------
    if args.command == 'deactivate':
        name = args.name
        candidates = cfg_main.list_repo_names()
        if candidates:
            name = _resolve_name_or_index(name, candidates, "mirror")
        dst = repos_enabled / f"{name}.conf"

        if not dst.exists():
            log(f"Mirror '{name}' is not active ({dst} not found)", level="INFO")
            sys.exit(0)

        if not dst.is_symlink():
            log(
                f"{dst} is not a symlink and cannot be deactivated. "
                f"It must be manually removed from {repos_enabled}.",
                level="ERROR",
            )
            sys.exit(1)

        dst.unlink()
        log(f"Deactivated mirror '{name}'", level="INFO")
        sys.exit(0)

    # ------------------------------------------------------------------
    # test
    # ------------------------------------------------------------------
    if args.command == 'test':
        name = args.name
        if repos_available.exists():
            available_configs = sorted(f.stem for f in repos_available.glob("*.conf"))
            if available_configs:
                name = _resolve_name_or_index(name, available_configs, "mirror")
        src = repos_available / f"{name}.conf"
        if not src.exists():
            log(f"ERROR: Mirror '{name}' does not exist in repos-available ({src})", level="ERROR")
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
            log(f"ERROR: Mirror '{name}' has no 'upstream' defined in {src}", level="ERROR")
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

        print("  Filesystem check (mirror_root/repos vs mirror_root/pool)...")
        try:
            pool_dev = os.stat(cfg_main.pool_root).st_dev
            repo_dev = os.stat(cfg_main.repo_root).st_dev
            same_fs = "yes" if pool_dev == repo_dev else "NO"
            print(f"    repos ({cfg_main.repo_root}) and pool ({cfg_main.pool_root}): "
                  f"same filesystem = {same_fs}")
            if pool_dev != repo_dev:
                print("    WARNING: A symlink or bind mount is redirecting one of these "
                      "directories to a foreign volume - hardlink deduplication will not work.")
        except OSError as e:
            print(f"    ERROR: Cannot stat mirror_root/repos or mirror_root/pool: {e}")

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

    # ------------------------------------------------------------------
    # reinitialise
    # ------------------------------------------------------------------
    if args.command == 'reinitialise':
        name = args.name
        if repos_available.exists():
            available_configs = sorted(f.stem for f in repos_available.glob("*.conf"))
            if available_configs:
                name = _resolve_name_or_index(name, available_configs, "mirror")
        data_path = _resolve_dest(name, cfg_main)
        if not data_path:
            log(f"ERROR: Cannot resolve '{name}' to a dest directory", level="ERROR")
            sys.exit(1)

        data_path = os.path.abspath(data_path)
        if not data_path.startswith(os.path.abspath(cfg_main.repo_root)):
            log(f"ERROR: Refusing to reinitialise data directory outside mirror_root/repos: {data_path}", level="ERROR")
            sys.exit(1)

        if not os.path.exists(data_path):
            log(f"Data directory does not exist (nothing to reinitialise): {data_path}", level="INFO")
            sys.exit(0)

        desc = f"Reinitialise '{name}' - snapshot and remove data at {data_path}"
        if not _pin_confirm(desc, args.force):
            sys.exit(1)

        snap_base = Path(cfg_main.repo_root) / "Snapshots"
        snap_dir = snap_base / name
        ts = _timestamp()
        target = snap_dir / ts
        os.makedirs(str(snap_dir), exist_ok=True)
        log(f"Reinitialising '{name}': {data_path} -> {target}", level="INFO")
        os.rename(data_path, str(target))
        print(f"Moved data to snapshot: {target}")

        enabled_link = repos_enabled / f"{name}.conf"
        if enabled_link.exists():
            print(f"Activation status unchanged (still active: {enabled_link})")
        else:
            print(f"Activation status unchanged (inactive)")

        print(f"Reinitialised '{name}' - next sync will re-download from upstream.")
        sys.exit(0)

    # ------------------------------------------------------------------
    # relink-pool
    # ------------------------------------------------------------------
    if args.command == 'relink-pool':
        script = Path(__file__).resolve().parents[1] / "scripts" / "sync-hashes.sh"
        if not script.exists():
            log(f"ERROR: Relink script not found at {script}", level="ERROR")
            sys.exit(1)

        managed_dests: set[str] = set()
        for name in cfg_main.list_repo_names():
            dest_abs = cfg_main.resolve_dest(name)
            if not dest_abs:
                continue
            if dest_abs.startswith(cfg_main.repo_root.rstrip("/") + "/"):
                managed_dests.add(dest_abs[len(cfg_main.repo_root.rstrip("/")) + 1:])
        managed_dests.add("Snapshots")

        include_dirs: list[str] = []
        if not os.path.isdir(cfg_main.repo_root):
            log(f"ERROR: mirror_root/repos '{cfg_main.repo_root}' does not exist", level="ERROR")
            sys.exit(1)

        for entry in sorted(os.listdir(cfg_main.repo_root)):
            if entry in managed_dests:
                include_dirs.append(entry)

        if not args.force:
            dirs_str = ", ".join(include_dirs)
            answer = input(
                f"This will regenerate pool entries for the following directories: "
                f"{dirs_str}. Continue? [y/N] "
            ).strip().lower()
            if answer != "y":
                log("Re-link cancelled.", level="INFO")
                sys.exit(0)

        cmd = [str(script), "-v"]
        for d in include_dirs:
            cmd.extend(["--include", d])
        cmd.extend([cfg_main.repo_root, cfg_main.pool_root])
        log(f"Re-linking {len(include_dirs)} directories under {cfg_main.repo_root}",
            level="INFO")
        result = subprocess.run(cmd)
        sys.exit(result.returncode)

    # ------------------------------------------------------------------
    # snapshot
    # ------------------------------------------------------------------
    if args.command == 'snapshot':
        snap_base = Path(cfg_main.repo_root) / "Snapshots"

        # ---- snapshot create ----
        if args.snap_action == 'create':
            name = args.name
            if name != "ALL":
                candidates = cfg_main.list_repo_names()
                if candidates:
                    name = _resolve_name_or_index(name, candidates, "repo")

            def _do_snapshot(dest_path: str, label: str) -> None:
                snap_dir = snap_base / label
                ts = _timestamp()
                target = snap_dir / ts
                if os.path.isdir(dest_path):
                    src_dev = os.stat(dest_path).st_dev
                    snap_dev = os.stat(str(snap_base)).st_dev
                    if src_dev != snap_dev:
                        log(
                            "ERROR: Snapshot source and Snapshots/ directory are on different "
                            "filesystems. cp -al (hardlinks) requires both to reside on the same "
                            f"logical volume. src st_dev={src_dev}, snap st_dev={snap_dev}.",
                            level="ERROR"
                        )
                        sys.exit(1)
                os.makedirs(str(snap_dir), exist_ok=True)
                log(f"Snapshotting '{label}' from {dest_path} -> {target}", level="INFO")
                subprocess.run(
                    ["cp", "-al", dest_path, str(target)],
                    check=True,
                )
                print(f"Created snapshot: {target}")

            if name == "ALL":
                dests = _list_dests(cfg_main)
                if not dests:
                    log("ERROR: No dest directories found under repo_root", level="ERROR")
                    sys.exit(1)
                for d in dests:
                    label = Path(d).name
                    _do_snapshot(d, label)
                sys.exit(0)

            dest = _resolve_dest(name, cfg_main)
            if not dest:
                log(f"ERROR: Cannot resolve '{name}' to a dest directory", level="ERROR")
                sys.exit(1)
            _do_snapshot(dest, Path(dest).name)
            sys.exit(0)

        # ---- snapshot list ----
        if args.snap_action == 'list':
            name = args.name
            if name != "ALL":
                candidates = cfg_main.list_repo_names()
                if candidates:
                    name = _resolve_name_or_index(name, candidates, "repo")
            if not snap_base.exists():
                print("No Snapshots directory")
                sys.exit(0)

            if name == "ALL":
                repos = sorted(d.name for d in snap_base.iterdir() if d.is_dir())
                if not repos:
                    print("No snapshots found")
                    sys.exit(0)
                for ri, r in enumerate(repos):
                    ts_list = sorted(
                        d.name for d in (snap_base / r).iterdir()
                        if d.is_dir() and d.name[:8].isdigit()
                    )
                    if ts_list:
                        print(f"  [{ri}] {r}:")
                        for ti, t in enumerate(ts_list):
                            sz = _snapshot_size(snap_base / r / t)
                            print(f"        [{ti}] {_fmt_ts(t)}  ({t})  {sz}")
                    else:
                        print(f"  [{ri}] {r}: (no snapshots)")
                sys.exit(0)

            repo_snap = snap_base / name
            if not repo_snap.exists():
                print(f"No snapshots for '{name}'")
                sys.exit(0)
            ts_list = sorted(
                d.name for d in repo_snap.iterdir()
                if d.is_dir() and d.name[:8].isdigit()
            )
            if not ts_list:
                print(f"No snapshots for '{name}'")
                sys.exit(0)
            print(f"Snapshots for '{name}':")
            for ti, t in enumerate(ts_list):
                sz = _snapshot_size(repo_snap / t)
                print(f"  [{ti}] {_fmt_ts(t)}  ({t})  {sz}")
            sys.exit(0)

        # ---- snapshot restore ----
        if args.snap_action == 'restore':
            snap_name = args.name
            snap_ts = args.snapshot  # '' if not provided

            all_repos = sorted(
                d.name for d in Path(cfg_main.repo_root).glob("Snapshots/*")
                if d.is_dir()
            )
            if all_repos:
                snap_name = _resolve_name_or_index(snap_name, all_repos, "snapshot repo")

            snap_repo_dir = snap_base / snap_name
            if not snap_repo_dir.exists():
                log(f"ERROR: Snapshot '{snap_name}' not found at {snap_repo_dir}", level="ERROR")
                sys.exit(1)

            if not snap_ts:
                snap_ts = _resolve_snapshot_ts(str(snap_repo_dir))
                if not snap_ts:
                    log(f"ERROR: No snapshots found under {snap_repo_dir}", level="ERROR")
                    sys.exit(1)
            elif snap_ts.isdigit():
                ts_candidates = sorted(
                    d.name for d in snap_repo_dir.iterdir()
                    if d.is_dir() and d.name[:8].isdigit()
                )
                snap_ts = _resolve_name_or_index(snap_ts, ts_candidates, "snapshot timestamp")

            snapshot_path = snap_repo_dir / snap_ts
            if not snapshot_path.exists():
                log(f"ERROR: Snapshot timestamp '{snap_ts}' not found in {snap_repo_dir}",
                    level="ERROR")
                sys.exit(1)

            dest = _resolve_dest(snap_name, cfg_main)
            if not dest:
                dest = os.path.join(cfg_main.repo_root, snap_name)
            dest = os.path.abspath(dest)

            if not dest.startswith(os.path.abspath(cfg_main.repo_root)):
                log(f"ERROR: Refusing to restore outside repo_root: {dest}", level="ERROR")
                sys.exit(1)

            if not os.path.isdir(dest):
                log(f"ERROR: Target dest does not exist: {dest}", level="ERROR")
                sys.exit(1)

            desc = f"Restore snapshot '{snap_name}:{snap_ts}' to {dest}"
            if not _pin_confirm(desc, args.force):
                sys.exit(1)

            ts = _timestamp()
            swap_old = dest + ".old-" + ts
            tmp_restore = dest + ".tmp-" + ts

            try:
                if not args.no_backup:
                    snap_target = snap_repo_dir / f".pre-restore-{ts}"
                    log(
                        f"Snapshotting current state: {dest} -> {snap_target}",
                        level="INFO",
                    )
                    subprocess.run(
                        ["cp", "-al", dest, str(snap_target)], check=True,
                    )
                    print(f"Created pre-restore snapshot: {snap_target}")

                log(f"Linking snapshot: {snapshot_path} -> {tmp_restore}", level="INFO")
                subprocess.run(["cp", "-al", str(snapshot_path), tmp_restore], check=True)

                os.rename(dest, swap_old)
                os.rename(tmp_restore, dest)

                log(f"Cleaning up old state: {swap_old}", level="INFO")
                shutil.rmtree(swap_old)

                print(f"Restored snapshot '{snap_name}:{snap_ts}' to {dest}")
                sys.exit(0)

            except Exception as e:
                log(f"ERROR during restore: {e}", level="ERROR")
                if os.path.exists(tmp_restore):
                    shutil.rmtree(tmp_restore, ignore_errors=True)
                if os.path.exists(dest + ".old-" + ts) and not os.path.exists(dest):
                    os.rename(dest + ".old-" + ts, dest)
                sys.exit(1)

        # ---- snapshot delete ----
        if args.snap_action == 'delete':
            snap_name = args.name
            snap_ts = args.snapshot  # '' if not provided

            all_snap_repos = sorted(
                d.name for d in snap_base.iterdir()
                if d.is_dir()
            ) if snap_base.exists() else []
            if all_snap_repos:
                snap_name = _resolve_name_or_index(snap_name, all_snap_repos, "snapshot repo")

            snap_repo_dir = snap_base / snap_name

            if snap_ts:
                if snap_ts.isdigit():
                    ts_candidates = sorted(
                        d.name for d in snap_repo_dir.iterdir()
                        if d.is_dir() and d.name[:8].isdigit()
                    )
                    snap_ts = _resolve_name_or_index(snap_ts, ts_candidates, "snapshot timestamp")
                target = snap_repo_dir / snap_ts
            else:
                target = snap_repo_dir

            if not target.exists():
                log(f"ERROR: Snapshot '{snap_name}' not found at {target}", level="ERROR")
                sys.exit(1)

            desc = f"Delete snapshot '{snap_name}' ({target})"
            if not _pin_confirm(desc, args.force):
                sys.exit(1)

            shutil.rmtree(str(target))
            print(f"Deleted snapshot: {target}")

            parent = target.parent
            try:
                next(parent.iterdir())
            except StopIteration:
                parent.rmdir()

            sys.exit(0)

    # ------------------------------------------------------------------
    # stats
    # ------------------------------------------------------------------
    if args.command == 'stats':
        from .stats import read_ndjson, format_row, print_summary_table

        stat_args = args.args  # list of positional words

        # "stats reset [NAME]"
        if stat_args and stat_args[0].lower() == 'reset':
            from .stats import clear_ndjson

            reset_name = stat_args[1] if len(stat_args) > 1 else "ALL"
            if reset_name != "ALL":
                candidates = cfg_main.list_repo_names()
                if candidates:
                    reset_name = _resolve_name_or_index(reset_name, candidates, "repo")

            def _reset_stats(repo_name: str) -> None:
                cleared = clear_ndjson(cfg_main.mirror_root, repo_name)
                if cleared is None:
                    p = Path(cfg_main.mirror_root) / "mirror-dedupe" / repo_name / "stats.ndjson"
                    print(f"[{repo_name}] No stats.ndjson at {p} (nothing to reset)")
                else:
                    print(f"[{repo_name}] Reset stats.ndjson at {cleared}")

            if reset_name == "ALL":
                desc = "Reset stats for ALL repos"
                if not _pin_confirm(desc, args.force):
                    sys.exit(1)
                names = cfg_main.list_repo_names()
                for n in names:
                    _reset_stats(n)
                sys.exit(0)

            desc = f"Reset stats for '{reset_name}'"
            if not _pin_confirm(desc, args.force):
                sys.exit(1)
            _reset_stats(reset_name)
            sys.exit(0)

        # "stats [NAME|all]"
        stat_name = stat_args[0] if stat_args else None

        def _enabled_names() -> List[str]:
            enabled_dir = Path(cfg_main.config_dir) / "repos-enabled"
            if not enabled_dir.is_dir():
                print("No active repos")
                sys.exit(0)
            ns = sorted(f.stem for f in enabled_dir.glob("*.conf"))
            if not ns:
                print("No active repos")
                sys.exit(0)
            return ns

        if stat_name is None:
            # No arg: one row per enabled repo showing their latest sync
            rows: List[Dict[str, str]] = []
            for n in _enabled_names():
                rec = next(read_ndjson(cfg_main.mirror_root, n), None)
                if rec:
                    rows.append(format_row(rec, n))
            if not rows:
                print("No stats records found")
                sys.exit(0)
            print_summary_table(rows)
            sys.exit(0)

        if stat_name.lower() == 'all':
            # Full history per enabled repo
            any_records = False
            for n in _enabled_names():
                records = list(read_ndjson(cfg_main.mirror_root, n))
                if not records:
                    continue
                any_records = True
                print_summary_table([format_row(r, n) for r in records],
                                    show_name=False, show_total=False, title=n)
            if not any_records:
                print("No stats records found")
            sys.exit(0)

        # Named repo: full history
        candidates = cfg_main.list_repo_names()
        if not candidates:
            print("No repos found")
            sys.exit(1)
        stat_name = _resolve_name_or_index(stat_name, candidates, "repo")
        records = list(read_ndjson(cfg_main.mirror_root, stat_name))
        if not records:
            print(f"No stats records for '{stat_name}'")
            sys.exit(0)
        print_summary_table([format_row(r, stat_name) for r in records],
                            show_name=False, show_total=False, title=stat_name)
        sys.exit(0)

    # ------------------------------------------------------------------
    # migrate
    # ------------------------------------------------------------------
    if args.command == 'migrate':
        print("'migrate' is not yet implemented. It will migrate a legacy mirror-dedupe")
        print("repo tree (pre-0.2.x layout without .mirror-dedupe/ structure) to the")
        print("current layout. Coming in a future release.")
        sys.exit(0)


if __name__ == '__main__':
    main()
