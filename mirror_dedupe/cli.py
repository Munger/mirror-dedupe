#!/usr/bin/env python3
## @file cli.py
##
## @brief CLI entry point and management operations for mirror-dedupe.
##
## Provides the ``main()`` entry point with subcommands for listing,
## activating, deactivating, testing, scanning, and deleting mirrors.
##
## The ``--sync`` flag drives the schema-based sync pipeline via
## ``Repos.from_names().sync_all()``.
##
## The ``--scan`` flag discovers upstream repository metadata and
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


def _resolve_dest(name: str, cfg: Config) -> Optional[str]:
    ## @brief Resolve a repo *name* to its absolute ``dest`` path.
    ##
    ## Delegates to ``cfg.resolve_dest()``, then falls back to treating
    ## *name* as a literal subdirectory name under ``repo_root``.
    ##
    ## @param name  Repo name or dest directory name.
    ## @param cfg   Global ``Config`` singleton.
    ## @return The absolute dest path, or ``None`` if unresolvable.

    dest = cfg.resolve_dest(name)
    if dest:
        return dest
    fallback = os.path.join(cfg.repo_root, name)
    return fallback if os.path.isdir(fallback) else None


def _list_dests(cfg: Config) -> List[str]:
    ## @brief Return all dest directories under ``repo_root``.
    ##
    ## Iterates first-level subdirectories of ``repo_root``, excluding
    ## ``Snapshots/`` and any hidden directories, and adds any
    ## ``additional_repos`` dests that may not yet exist on disk.
    ##
    ## @param cfg  Global ``Config`` singleton.
    ## @return Sorted list of absolute dest paths.

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
    ## @brief Return an ISO-8601 timestamp string safe for directory names.
    ##
    ## Format: ``20260605T120000`` (no colons, no timezone).
    ##
    ## @return A compact timestamp string.

    from .lib.datetimeutils import fmt_compact_ts
    return fmt_compact_ts()


def _pin_confirm(action_desc: str, force: bool) -> bool:
    ## @brief Ask the user to confirm a destructive operation via a random PIN.
    ##
    ## If *force* is ``True``, the prompt is skipped and confirmation
    ## is granted immediately.
    ##
    ## @param action_desc  Human-readable description of the action.
    ## @param force        If ``True``, bypass the PIN prompt.
    ## @return ``True`` if confirmed, ``False`` otherwise.

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
    ##
    ## Timestamps are directory names matching ``YYYYMMDD*``.  The latest
    ## (highest sort order) is returned, which coincides with "most recent"
    ## for the ISO-compatible naming scheme used by ``_timestamp()``.
    ##
    ## @param snapshot_dir  Path to a snapshot repo directory.
    ## @return The latest timestamp string, or ``None`` if no snapshots.

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
    ##
    ## If *val* is a non-negative integer string, it is treated as an index
    ## into *candidates* (must be in range).  Otherwise it is returned as-is.
    ##
    ## @param val        Raw input from the user.
    ## @param candidates Ordered list of valid names.
    ## @param label      Human-readable label for error messages (e.g. "repo", "snapshot").
    ## @return The resolved name (either the original string or the looked-up candidate).
    ## @throws SystemExit if the index is out of range.

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
    ##
    ## @param raw  The raw timestamp string.
    ## @return A formatted string like ``"2026-06-05 13:47:04"``, or the
    ##         original string if it doesn't match the expected pattern.

    if len(raw) >= 15 and raw[8] == "T":
        return (
            f"{raw[:4]}-{raw[4:6]}-{raw[6:8]} "
            f"{raw[9:11]}:{raw[11:13]}:{raw[13:15]}"
        )
    return raw


def _snapshot_size(snapshot_path: Path) -> str:
    ## @brief Return a human-readable total size for a snapshot directory.
    ##
    ## Uses ``du -sh`` for a quick estimate.  Falls back to ``"(unknown)"``
    ## if the directory doesn't exist or the subprocess fails.
    ##
    ## @param snapshot_path  Path to a snapshot (timestamp) directory.
    ## @return A size string like ``"1.2G"`` or ``"(unknown)"``.

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

    # SIGINT kills tracked curl subprocesses immediately - avoids
    # KeyboardInterrupt corrupting Python's import state during cleanup.
    import signal as _signal

    from .lib.subproc import kill_active_subprocesses_signal_safe
    _signal.signal(
        _signal.SIGINT,
        lambda signum, frame: (
            kill_active_subprocesses_signal_safe(),
            os._exit(130),
        ),
    )

    # Check external dependencies before any real work.
    from .deps import check_dependencies
    check_dependencies()

    parser = argparse.ArgumentParser(
        description='Mirror repository with global deduplication',
        formatter_class=argparse.RawDescriptionHelpFormatter
    )

    # -- Global options ------------------------------------------------
    global_grp = parser.add_argument_group('Global options')
    global_grp.add_argument('--version', '-v', action='version',
                            version=f'mirror-dedupe {__version__}')
    global_grp.add_argument('--config-dir', dest='config_path', default=None,
                            help=f'Path to config directory (default: {DEFAULT_CONFIG_DIR})')

    # -- Sync options --------------------------------------------------
    sync_grp = parser.add_argument_group('Sync options')
    sync_grp.add_argument('--sync', action='store_true',
                          help='Run the full sync pipeline for all enabled mirrors')
    sync_grp.add_argument('--mirror', type=str, metavar='NAME',
                          help='Sync only the named mirror (used with --sync)')
    sync_grp.add_argument('--dedupe-only', action='store_true',
                          help='Skip metadata sync; only hardlink pool entries into repo trees')

    # -- Scan options --------------------------------------------------
    scan_grp = parser.add_argument_group('Scan options')
    scan_grp.add_argument('--scan', action='store_true',
                          help='Probe an upstream URL and generate a repo YAML config')
    scan_grp.add_argument('--name', metavar='NAME',
                          help='Repository name (required for --scan)')
    scan_grp.add_argument('--dest', metavar='DEST',
                          help='Destination path relative to mirror_root/repos (defaults to --name)')
    scan_grp.add_argument('--out', dest='out_dir', metavar='DIR',
                          help='Output directory for generated config (required for --scan)')
    scan_grp.add_argument('-U', '--upstream', '--upstreams', dest='upstreams',
                          action='extend', nargs='+', metavar='URL',
                          help='Upstream URL(s); first is primary, subsequent are fallbacks')
    scan_grp.add_argument('-r', '--dist', '--release', action='append',
                          dest='dist', metavar='SUITE',
                          help='Restrict to a specific suite (may be repeated)')
    scan_grp.add_argument('-R', '--releases', dest='releases', metavar='SUITES',
                          help='Comma-separated list of suites to mirror')
    scan_grp.add_argument('--arch', action='append', dest='arch',
                          metavar='ARCH',
                          help='Architecture to include (may be repeated)')
    scan_grp.add_argument('--architectures', dest='architectures',
                          metavar='ARCHES',
                          help='Comma-separated list of architectures')
    scan_grp.add_argument('--component', action='append', dest='component',
                          metavar='COMP',
                          help='Component to include (may be repeated)')
    scan_grp.add_argument('--components', dest='components',
                          metavar='COMPS',
                          help='Comma-separated list of components')
    scan_grp.add_argument('--repo-type', dest='repo_type', metavar='TYPE',
                          help='Force a repo type (e.g. "apt") for unusual layouts')
    scan_grp.add_argument('-G', '--gpg-key-url', dest='gpg_key_url',
                          metavar='URL',
                          help='GPG key URL for Release file signature verification')
    collapse_scan = scan_grp.add_mutually_exclusive_group()
    collapse_scan.add_argument('--collapse-dists', dest='collapse_dists',
                               action='store_true',
                               help='Collapse discovered suites to base names (e.g. noble-updates -> noble)')
    collapse_scan.add_argument('--no-collapse-dists', dest='collapse_dists',
                               action='store_false',
                               help='Emit all discovered suite variants explicitly')
    parser.set_defaults(collapse_dists=None)
    scan_grp.add_argument('--emit-json', action='store_true', default=False,
                          help='Also write a JSON snapshot of the discovered structure')
    scan_grp.add_argument('--no-filter', action='store_true', default=False,
                          help='Ignore architecture filters from mirror-dedupe.conf; '
                               'emit all discovered arches')
    scan_grp.add_argument('upstream', nargs='?',
                          help='Upstream URL (alternative to -U / --upstream)')

    # -- Repository management -----------------------------------------
    mgmt_grp = parser.add_argument_group('Repository management')
    mgmt_grp.add_argument('--list', action='store_true',
                          help='List available mirrors (active and inactive)')
    mgmt_grp.add_argument('--list-repos', action='store_true',
                          help='List all known repos (config-based and additional)')
    mgmt_grp.add_argument('--activate', metavar='NAME',
                          help='Enable a mirror via symlink in repos-enabled')
    mgmt_grp.add_argument('--deactivate', metavar='NAME',
                          help='Disable a mirror by removing its repos-enabled symlink')
    mgmt_grp.add_argument('--test', metavar='NAME',
                          help='Check upstream reachability and summarise what will be synced')
    mgmt_grp.add_argument('--reinitialise', metavar='NAME',
                          help='Snapshot a repo and remove its data dir (leaves activation; requires PIN)')
    mgmt_grp.add_argument('--force', action='store_true',
                          help='Bypass PIN confirmation on destructive operations')
    mgmt_grp.add_argument('--no-backup', action='store_true',
                          help='Skip current-state backup before restore')
    mgmt_grp.add_argument('--relink-pool', action='store_true',
                          help='Re-link pool hashes with all managed repos and snapshots')
    mgmt_grp.add_argument('--migrate', action='store_true',
                          help='Migrate a legacy repo tree to the current layout (not yet implemented)')
    mgmt_grp.add_argument('--stats', metavar='NAME', nargs='?', const='_SUMMARY',
                          help='Print sync statistics: latest per repo (no arg), full history per repo (ALL), or full history for NAME')
    mgmt_grp.add_argument('--stats-reset', metavar='NAME', nargs='?', const='ALL',
                          help='Truncate stats.ndjson for a repo or ALL (requires PIN)')

    # -- Snapshots and pool --------------------------------------------
    snap_grp = parser.add_argument_group('Snapshots and pool')
    snap_grp.add_argument('--snapshot', metavar='NAME', nargs='?', const='ALL',
                          help='Create a hardlink snapshot of a repo dest or ALL repos')
    snap_grp.add_argument('--list-snapshots', metavar='NAME', nargs='?', const='ALL',
                          help='List available snapshots for a repo or ALL')
    snap_grp.add_argument('--restore-snapshot', metavar='NAME',
                          help='Restore a snapshot (NAME[:TS] format; defaults to latest)')
    snap_grp.add_argument('--delete-snapshot', metavar='NAME',
                          help='Delete a snapshot directory (requires PIN)')
    snap_grp.add_argument('--sweep-pool', action='store_true', dest='sweep',
                          help='Remove orphaned pool entries (st_nlink == 1)')

    args = parser.parse_args()

    has_action = any([
        args.sync, args.scan, args.list, args.list_repos,
        args.activate, args.deactivate, args.test, args.reinitialise,
        args.relink_pool, args.snapshot, args.list_snapshots,
        args.restore_snapshot, args.delete_snapshot, args.stats,
        args.stats_reset, args.migrate, args.sweep,
    ])
    if not has_action:
        parser.print_help()
        sys.exit(2)

    cfg_main = Config.load(args.config_path)

    management_ops = [
        bool(args.scan),
        bool(args.list),
        bool(args.list_repos),
        bool(args.activate),
        bool(args.deactivate),
        bool(args.test),
        bool(args.reinitialise),
        bool(args.relink_pool),
        bool(args.snapshot),
        bool(args.list_snapshots),
        bool(args.restore_snapshot),
        bool(args.delete_snapshot),
        bool(args.stats),
        bool(args.stats_reset),
        bool(args.migrate),
        bool(args.sweep),
    ]
    if sum(1 for x in management_ops if x) > 1:
        names = [
            "--scan", "--list", "--list-repos", "--activate", "--deactivate",
            "--test", "--reinitialise", "--relink-pool", "--snapshot",
            "--list-snapshots", "--restore-snapshot", "--delete-snapshot",
            "--stats", "--stats-reset", "--migrate", "--sweep-pool",
        ]
        log(f"ERROR: Only one management flag may be used at a time. Choose one of: {', '.join(names)}", level="ERROR")
        sys.exit(1)

    if args.sync:
        from .schema.repo import Repos, pool_sweep_safe

        repo_names: List[str] = []
        if args.mirror:
            repo_names.append(args.mirror)
        else:
            enabled = Path(cfg_main.config_dir) / 'repos-enabled'
            if enabled.exists():
                repo_names = sorted(
                    f.stem for f in enabled.glob("*.conf")
                )
            if not repo_names:
                log(
                    "No active repos found. Use --list to see available mirrors "
                    "and --activate to enable one.",
                    level="ERROR",
                )
                sys.exit(1)

        repos = Repos.from_names(repo_names, config_dir=args.config_path)
        repos.sync_all(config_dir=args.config_path)
        if args.sweep or cfg_main.sweep_pool_after_sync:
            pool_sweep_safe(cfg_main, fail_if_locked=False)
        sys.exit(0)

    # ------------------------------------------------------------------
    # --scan : discover upstream and generate repo config
    # ------------------------------------------------------------------
    if args.scan:
        if not args.out_dir:
            log("ERROR: --out <dir> is required for --scan", level="ERROR")
            sys.exit(1)
        try:
            from .scan import scan as scan_repo, generate_config

            # Resolve upstream URLs: positional, --upstream, or error
            scan_upstreams: List[str] = []
            if args.upstreams:
                scan_upstreams = [u for u in args.upstreams if u]
            elif args.upstream:
                scan_upstreams = [args.upstream]
            else:
                log(
                    "ERROR: No upstream URL provided for --scan. "
                    "Pass a URL as a positional argument or via --upstream.",
                    level="ERROR",
                )
                sys.exit(1)

            scan_name = args.name or scan_upstreams[0].split("://")[-1].split("/")[0]

            # Normalise dist/arch/component overrides (same logic as scan.main())
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

            dist_overrides: Optional[List[str]] = None
            dist_values: List[str] = []
            if args.dist:
                dist_values.extend(args.dist)
            if args.releases:
                dist_values.extend(_split_csv([args.releases]))
            if dist_values:
                seen_d = set()
                ordered: List[str] = []
                for d in dist_values:
                    if d and d not in seen_d:
                        seen_d.add(d)
                        ordered.append(d)
                dist_overrides = ordered or None

            try:
                repo = scan_repo(
                    scan_name,
                    scan_upstreams,
                    repo_type=args.repo_type,
                    dist_overrides=dist_overrides,
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
                dist_overrides=dist_overrides,
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
                f"  mirror-dedupe --test {repo.name}\n\n"
                f"  # If the test looks good, activate the repository:\n"
                f"  To activate, copy to repos-enabled:\n"
                f"  cp {config_file} "
                f"{Path(cfg_main.config_dir) / 'repos-enabled' / (repo.name + '.conf')}\n\n"
                f"  Or from the config directory:\n"
                f"  cd {Path(cfg_main.config_dir) / 'repos-enabled'}\n"
                f"  ln -s ../repos-available/{repo.name}.conf .\n"
            )
        except KeyboardInterrupt:
            print("")
            sys.exit(130)
        sys.exit(0)

    config_dir_path = Path(cfg_main.config_dir)
    repos_available = config_dir_path / 'repos-available'
    repos_enabled = config_dir_path / 'repos-enabled'

    # ------------------------------------------------------------------
    # --list-repos : list all known repos (config + additional)
    # ------------------------------------------------------------------
    if args.list_repos:
        names = cfg_main.list_repo_names()
        if not names:
            print("No repos found (no configs in repos-enabled/ and no additional_repos)")
            sys.exit(0)
        print(f"Known repos under {cfg_main.repo_root}:")
        print("")
        for i, n in enumerate(names):
            dest = _resolve_dest(n, cfg_main)
            tag = " [additional]" if n in cfg_main.additional_repos else ""
            print(f"  [{i}] {n:30s} {dest or '(unresolved)'}{tag}")
        sys.exit(0)

    # ------------------------------------------------------------------
    # --relink-pool : re-link pool hashes with repo + snapshot trees
    # ------------------------------------------------------------------
    if args.relink_pool:
        script = Path(__file__).resolve().parents[1] / "scripts" / "sync-hashes.sh"
        if not script.exists():
            log(f"ERROR: Relink script not found at {script}", level="ERROR")
            sys.exit(1)

        # Build --include list from all managed repo dests + Snapshots.
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
    # --snapshot : cp -al dest -> Snapshots/<name>/<ts>/
    # ------------------------------------------------------------------
    if args.snapshot:
        name = args.snapshot
        if name != "ALL":
            candidates = cfg_main.list_repo_names()
            if candidates:
                name = _resolve_name_or_index(name, candidates, "repo")
        snap_base = Path(cfg_main.repo_root) / "Snapshots"

        def _do_snapshot(dest_path: str, label: str) -> None:
            ## @brief Create a hardlink snapshot of *dest_path* under ``Snapshots/<label>/<ts>/``.
            ## @param dest_path  Absolute path to the repo dest to snapshot.
            ## @param label      Name for the snapshot group (typically the repo name).
            ## @return None
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

    # ------------------------------------------------------------------
    # --list-snapshots : show snapshot timestamps for a repo or ALL
    # ------------------------------------------------------------------
    if args.list_snapshots:
        name = args.list_snapshots
        if name != "ALL":
            candidates = cfg_main.list_repo_names()
            if candidates:
                name = _resolve_name_or_index(name, candidates, "repo")
        snap_base = Path(cfg_main.repo_root) / "Snapshots"
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

    # ------------------------------------------------------------------
    # --restore-snapshot : atomic swap from snapshot
    # ------------------------------------------------------------------
    if args.restore_snapshot:
        raw = args.restore_snapshot
        if ":" in raw:
            snap_name, snap_ts = raw.split(":", 1)
        else:
            snap_name = raw
            snap_ts = ""

        # Resolve snapshot name by index if needed
        all_repos = sorted(
            d.name for d in Path(cfg_main.repo_root).glob("Snapshots/*")
            if d.is_dir()
        )
        if all_repos:
            snap_name = _resolve_name_or_index(snap_name, all_repos, "snapshot repo")

        snap_base = Path(cfg_main.repo_root) / "Snapshots"
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

        # Resolve target dest: try config/additional, then fallback to repo_root/<name>
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
            # 1. Snapshot current state (like --reinitialise) unless --no-backup
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

            # 2. Hardlink snapshot to temp
            log(f"Linking snapshot: {snapshot_path} -> {tmp_restore}", level="INFO")
            subprocess.run(["cp", "-al", str(snapshot_path), tmp_restore], check=True)

            # 3. Atomic swap
            os.rename(dest, swap_old)
            os.rename(tmp_restore, dest)

            # 4. Cleanup swap_old
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

    # ------------------------------------------------------------------
    # --delete-snapshot : rm -rf a snapshot directory
    # ------------------------------------------------------------------
    if args.delete_snapshot:
        raw = args.delete_snapshot
        if "/" in raw:
            snap_name, snap_ts = raw.split("/", 1)
        else:
            snap_name = raw
            snap_ts = ""

        snap_base = Path(cfg_main.repo_root) / "Snapshots"

        # Resolve repo name by index
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

        # Remove empty parent if this was the last snapshot
        parent = target.parent
        try:
            next(parent.iterdir())
        except StopIteration:
            parent.rmdir()

        sys.exit(0)

    # ------------------------------------------------------------------
    # --stats : print stats.ndjson for a repo or ALL
    # ------------------------------------------------------------------
    if args.stats:
        from .stats import read_ndjson, format_row, print_summary_table

        name = args.stats

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

        if name == "_SUMMARY":
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

        if name == "ALL":
            # Full history per enabled repo, one labelled table per repo
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
        name = _resolve_name_or_index(name, candidates, "repo")
        records = list(read_ndjson(cfg_main.mirror_root, name))
        if not records:
            print(f"No stats records for '{name}'")
            sys.exit(0)
        print_summary_table([format_row(r, name) for r in records],
                            show_name=False, show_total=False, title=name)
        sys.exit(0)

    # ------------------------------------------------------------------
    # --stats-reset : truncate stats.ndjson for a repo or ALL
    # ------------------------------------------------------------------
    if args.stats_reset:
        from .stats import clear_ndjson

        name = args.stats_reset
        if name != "ALL":
            candidates = cfg_main.list_repo_names()
            if candidates:
                name = _resolve_name_or_index(name, candidates, "repo")

        def _reset_stats(repo_name: str) -> None:
            ## @brief Truncate the NDJSON stats file for a repo.
            ## @param repo_name  Name of the repo (subdirectory under ``mirror-dedupe/``).
            ## @return None
            cleared = clear_ndjson(cfg_main.mirror_root, repo_name)
            if cleared is None:
                p = Path(cfg_main.mirror_root) / "mirror-dedupe" / repo_name / "stats.ndjson"
                print(f"[{repo_name}] No stats.ndjson at {p} (nothing to reset)")
            else:
                print(f"[{repo_name}] Reset stats.ndjson at {cleared}")

        if name == "ALL":
            desc = "Reset stats for ALL repos"
            if not _pin_confirm(desc, args.force):
                sys.exit(1)
            names = cfg_main.list_repo_names()
            for n in names:
                _reset_stats(n)
            sys.exit(0)

        desc = f"Reset stats for '{name}'"
        if not _pin_confirm(desc, args.force):
            sys.exit(1)
        _reset_stats(name)
        sys.exit(0)

    # ------------------------------------------------------------------
    # --migrate : placeholder for migration tool
    # ------------------------------------------------------------------
    if args.migrate:
        print("--migrate is not yet implemented. It will migrate a legacy mirror-dedupe")
        print("repo tree (pre-0.2.x layout without .mirror-dedupe/ structure) to the")
        print("current layout. Coming in a future release.")
        sys.exit(0)

    # ------------------------------------------------------------------
    # --sweep-pool : remove orphaned pool entries
    # ------------------------------------------------------------------
    if args.sweep:
        from .schema.repo import pool_sweep_safe
        if not pool_sweep_safe(cfg_main, fail_if_locked=True):
            sys.exit(1)
        sys.exit(0)

    # ------------------------------------------------------------------
    # Legacy management ops
    # ------------------------------------------------------------------

    if args.list:
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

    if args.activate:
        name = args.activate
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

    if args.deactivate:
        name = args.deactivate
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

    if args.test:
        name = args.test
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

    if args.reinitialise:
        name = args.reinitialise
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

        # Atomic rename: mv repo -> Snapshots/<name>/<ts>/
        snap_base = Path(cfg_main.repo_root) / "Snapshots"
        snap_dir = snap_base / name
        ts = _timestamp()
        target = snap_dir / ts
        os.makedirs(str(snap_dir), exist_ok=True)
        log(f"Reinitialising '{name}': {data_path} -> {target}", level="INFO")
        os.rename(data_path, str(target))
        print(f"Moved data to snapshot: {target}")

        # Leave activation status untouched
        enabled_link = repos_enabled / f"{name}.conf"
        if enabled_link.exists():
            print(f"Activation status unchanged (still active: {enabled_link})")
        else:
            print(f"Activation status unchanged (inactive)")

        print(f"Reinitialised '{name}' - next sync will re-download from upstream.")
        sys.exit(0)


if __name__ == '__main__':
    main()
