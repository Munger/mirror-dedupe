#!/usr/bin/env python3
## @file scan.py
##
## @brief Repository scanner for mirror-dedupe.
##
## Provides the ``scan()`` and ``generate_config()`` functions used by
## ``cli.py``'s ``--scan`` flag.  Can also run standalone via
## ``python3 -m mirror_dedupe.scan``.
##
## @copyright Copyright (c) 2025-2026 Tim Hosking
## @see https://github.com/munger
## @par Licence: MIT

import sys
import argparse
import datetime
from typing import List, Optional
import json
from pathlib import Path

from mirror_dedupe.schema.repo import Repo
from mirror_dedupe.config import Config
from mirror_dedupe.lib.html_helpers import url_hostname
from mirror_dedupe.lib.log import log
import mirror_dedupe.repos  # noqa: F401  # ensure Repo types are registered


def scan(name: str, upstreams: List[str],
         repo_type: Optional[str] = None,
         distribution_overrides: Optional[List[str]] = None) -> Repo:
    ## @brief Perform HTTP discovery and return a populated Repo.
    ##
    ## Creates a Repo via ``Repo.from_url``, optionally primes explicit
    ## distribution candidates, and runs the concrete parser to populate
    ## distributions, architectures, components, and releases.
    ##
    ## @param name                   Repository name.
    ## @param upstreams              Ordered list of upstream URLs (first is primary).
    ## @param repo_type              Force a specific Repo type (e.g. ``"apt"``).
    ## @param distribution_overrides Explicit distribution names to probe.
    ## @return A fully parsed Repo instance.

    primary_upstream = upstreams[0]
    log(f"Scanning {primary_upstream}...")

    # --distribution/--distributions are APT-specific concepts, so if the user
    # supplies explicit distribution names without also specifying a repo
    # type, assume APT rather than failing type detection.
    if repo_type is None and distribution_overrides:
        repo_type = "apt"

    repo = Repo.from_url(
        primary_upstream,
        upstream_urls=upstreams[1:],
        repo_type=repo_type,
    )
    if name:
        repo.name = name

    if distribution_overrides:
        object.__setattr__(repo, "distribution_candidates", distribution_overrides)

    repo = repo.analyse()

    return repo


def generate_config(repo: Repo, dest: str,
                    gpg_key_url: Optional[str] = None,
                    distribution_overrides: Optional[List[str]] = None,
                    arch_override: Optional[List[str]] = None,
                    component_override: Optional[List[str]] = None,
                    global_arch_mask: Optional[List[str]] = None) -> str:
    ## @brief Generate repository configuration from a fully-populated Repo.
    ##
    ## ``repo`` is expected to have already been parsed by its
    ## ``on_parse()`` method.  This function applies user-specified
    ## filters and emits a YAML configuration that mirror-dedupe can consume.
    ##
    ## GPG keys are no longer auto-discovered here.  If the caller supplies
    ## ``gpg_key_url``, it is passed through into the generated config
    ## unchanged; trust decisions and verification are left to the sync
    ## phase.
    ##
    ## @param repo                   Parsed Repo instance.
    ## @param dest                   Destination path (relative to repo_root).
    ## @param gpg_key_url            Optional GPG key URL.
    ## @param distribution_overrides Override distribution list.
    ## @param arch_override          Override architecture list.
    ## @param component_override     Override component list.
    ## @param global_arch_mask       Global architecture mask from config.
    ## @return YAML configuration string.

    if not gpg_key_url:
        gpg_key_url = repo.gpg_key_url

    total_steps = 3
    step = 1

    def next_step_label() -> str:
        ## @brief Return an incrementing ``[N/total]`` step label.
        ## @return Formatted label string.
        nonlocal step
        label = f"[{step}/{total_steps}]"
        step += 1
        return label

    log(f"  {next_step_label()} Examining discovered repository structure...")

    upstream_index = repo.upstream_idx  # which upstream was used for discovery
    upstream_node = repo.upstreams[upstream_index] if repo.upstreams else None

    discovered: List[str] = []
    if repo.distributions:
        for dist in repo.distributions:
            if dist.name and dist.name not in discovered:
                discovered.append(str(dist.name))
    if not discovered:
        log("      Warning: Could not auto-detect distributions", level="WARN")

    all_dists_mode = False

    if distribution_overrides:
        dists = [d for d in distribution_overrides if d]
        if any(d.lower() in ("*", "all") for d in dists):
            all_dists_mode = True
            distributions = discovered
        else:
            distributions = dists
    else:
        if not discovered:
            log("ERROR: No distributions were auto-detected and no --distribution/--distributions overrides were provided.\n       Please rerun with explicit --distributions (or --distribution) to choose which suites to mirror.", level="ERROR")
            sys.exit(1)
        all_dists_mode = True
        distributions = discovered

    log(f"      Using distributions: {', '.join(distributions)}")

    log(f"  {next_step_label()} Discovering architectures/components...")
    arch_set = set()
    comp_set = set()

    if repo.architectures:
        for arch in repo.architectures:
            if arch.name:
                arch_set.add(str(arch.name))

    if repo.components:
        for comp in repo.components:
            if comp.name:
                comp_set.add(str(comp.name))

    detected_arches = sorted(arch_set) if arch_set else ["amd64"]
    detected_components = sorted(comp_set) if comp_set else ["main"]

    if arch_override:
        architectures = [a for a in arch_override if a]
        detected_set = set(detected_arches)
        for a in architectures:
            if a not in detected_set:
                log(f"      Warning: architecture '{a}' was not found in Release metadata", level="WARN")
    else:
        architectures = detected_arches

    if component_override:
        components = [c for c in component_override if c]
        detected_set = set(detected_components)
        for c in components:
            if c not in detected_set:
                log(f"      Warning: component '{c}' was not found in Release metadata", level="WARN")
    else:
        components = detected_components

    if global_arch_mask:
        mask_set = set(global_arch_mask)
        before = architectures
        architectures = [a for a in architectures if a in mask_set]
        removed = [a for a in before if a not in architectures]
        if removed:
            log(f"      Note: architectures filtered by global mask: removed {', '.join(removed)}")

    log("")
    log(f"      Architectures: {', '.join(architectures)}")
    log(f"      Components: {', '.join(components)}")

    name = repo.name
    upstream_index = repo.upstream_idx
    upstream_entries = []
    for u in repo.upstreams:
        upstream_entries.append({"url": u.url})

    from .lib.datetimeutils import fmt_datetime

    cmd_parts = ["mirror-dedupe", "--scan", f"--name {name}"]
    if dest != name:
        cmd_parts.append(f"--dest {dest}")
    for u in repo.upstreams:
        cmd_parts.append(f"-U {u.url}")
    cmd = " ".join(cmd_parts)

    config_lines = [
        f"# Generated: {fmt_datetime()}",
        f"# Command: {cmd}",
    ]
    config_lines.append("")

    config_lines.append(f"name: {name}")
    config_lines.append(f"dest: {dest}")
    config_lines.append(f"repo_type: {repo.get('repo_type', repo.REPO_TYPE)}")

    if upstream_entries:
        config_lines.append("")
        config_lines.append("# During sync the first healthy upstream is used for each file.")
        config_lines.append("# Comment out an entry to disable that mirror.")
        config_lines.append("upstreams:")
        for upstream in upstream_entries:
            config_lines.append("  - url: " + upstream["url"])
        config_lines.append(f"upstream_idx: {upstream_index}  # prefer this mirror on sync - avoids mirror skew")

    if gpg_key_url:
        config_lines.append("")
        config_lines.append("# GPG key URL for Release file signature verification.")
        config_lines.append(f"gpg_key_url: {gpg_key_url}")
        log(f"      GPG key URL (user-supplied): {gpg_key_url}")

    config_lines.append("")
    config_lines.append("# Architectures to fetch.  Removing an architecture reduces")
    config_lines.append("# download size - only the listed ones are synced.")
    config_lines.append("# Use \"*\" or \"all\" to sync every available architecture.")
    config_lines.append("architectures:")
    for arch in architectures:
        config_lines.append(f"  - {arch}")

    sources_available = False
    if repo.distributions:
        for dist in repo.distributions:
            md = getattr(dist, "metadata", None)
            if md and hasattr(md, "hash_sections") and md.hash_sections:
                for _section, entries in md.hash_sections.items():
                    for entry in entries:
                        if "Sources" in entry.get("path", ""):
                            sources_available = True
                            break
                    if sources_available:
                        break
            if sources_available:
                break
    if sources_available:
        config_lines.append("  # - source")
        log("      Sources index detected upstream")

    config_lines.append("")
    config_lines.append("# Package components to mirror.  Comment out or remove any")
    config_lines.append("# that are not needed (e.g. \"non-free\", \"contrib\").")
    config_lines.append("components:")
    for comp in components:
        config_lines.append(f"  - {comp}")

    config_lines.append("")
    config_lines.append("# Distribution(s) / suites to mirror.  Each name corresponds to a")
    config_lines.append("# Release file on the upstream server.  Comment out a line to exclude")
    config_lines.append("# that suite from sync.")
    config_lines.append("#")
    config_lines.append("# Glob patterns (fnmatch) are supported and resolved at sync time")
    config_lines.append("# against what actually exists upstream, so new suites are picked up")
    config_lines.append("# automatically.  Examples:")
    config_lines.append('#   "noble*"              noble, noble-updates, noble-security, ...')
    config_lines.append('#   "noble/*"             noble/mongodb-org/8.0, noble/mongodb-org/8.2, ...')
    config_lines.append('#   "*/mongodb-org/7.0"   bionic/mongodb-org/7.0, focal/mongodb-org/7.0, ...')
    config_lines.append('#   "noble-2?.04"         noble-24.04, noble-26.04, ...')
    config_lines.append('#   "noble-2[024].04"     noble-20.04, noble-22.04, noble-24.04')
    config_lines.append('#   "*"                   everything upstream')
    config_lines.append("#")
    config_lines.append("# You can mix globs and explicit names freely.")
    config_lines.append("# Note: YAML requires quotes on values starting with * or other special characters")
    config_lines.append("distributions:")
    for dist in distributions:
        if dist[0] in ('*', '?', '[', '{', '&', '!', '|', '>', "'", '"', '%', '@', '`'):
            config_lines.append(f'  - "{dist}"')
        else:
            config_lines.append(f"  - {dist}")

    config_lines.append("")
    config_lines.append("# Per-repo parameter overrides.  The defaults from --scan are shown;")
    config_lines.append("# uncomment and change only what you need.")
    config_lines.append("#")
    config_lines.append("#   discovery_method    How upstream layout was discovered: \"html_bfs\"")
    config_lines.append("#                       (HTML index crawl) or \"explicit\" (dists/).")
    config_lines.append("#   log_colour          ANSI colour for this repo's log label.")
    config_lines.append("#                       Colours: BLACK, RED, GREEN, YELLOW, BLUE, MAGENTA,")
    config_lines.append("#                       CYAN, WHITE, BRIGHT_BLACK, BRIGHT_RED, BRIGHT_GREEN,")
    config_lines.append("#                       BRIGHT_YELLOW, BRIGHT_BLUE, BRIGHT_MAGENTA, BRIGHT_CYAN,")
    config_lines.append("#                       BRIGHT_WHITE, GREY, DEFAULT, NONE")
    config_lines.append("#   log_colour_bg       ANSI background colour for the label.")
    config_lines.append("#                       Same options as log_colour (DEFAULT = global setting,")
    config_lines.append("#                       NONE = no background)")
    config_lines.append("#   parallel_downloads  Per-repo worker pool size.  Inherits the global")
    config_lines.append("#                       default when commented out.")
    config_lines.append("params:")
    params = repo.get("params") or {}
    method = params.get("discovery_method", "html_bfs")
    config_lines.append(f"  discovery_method: {method}")
    nobrowse = params.get("nobrowse", False)
    if nobrowse:
        config_lines.append("  nobrowse: true")
    anchor_filename = params.get("anchor_filename", "Release")
    config_lines.append(f"  anchor_filename: {anchor_filename}")
    suite_anchor_exceptions = params.get("suite_anchor_exceptions", {})
    if suite_anchor_exceptions:
        config_lines.append("  suite_anchor_exceptions:")
        for suite, anchor in suite_anchor_exceptions.items():
            config_lines.append(f"    {suite}: {anchor}")
    config_lines.append(f"  log_colour: {params.get('log_colour', 'DEFAULT')}")
    config_lines.append(f"  log_colour_bg: {params.get('log_colour_bg', 'NONE')}")
    config_lines.append(f"  # parallel_downloads: N")

    config_lines.append("")

    return '\n'.join(config_lines)


def main() -> None:
    ## @brief Standalone entry point for ``python3 -m mirror_dedupe.scan``.
    ##
    ## Called directly when someone runs the module, or imported by
    ## ``cli.py``'s ``--scan`` flag (preferred).
    ## @return None

    parser = argparse.ArgumentParser(
        description='Scan a repository and generate mirror-dedupe configuration',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples:
  # Simple form: dest defaults to --name
  mirror-dedupe --scan --name ubuntu http://archive.ubuntu.com/ubuntu

  # Custom dest path
  mirror-dedupe --scan --name ubuntu --dest ubuntu/main http://archive.ubuntu.com/ubuntu

  # With an explicit GPG key override
  mirror-dedupe --scan --name mariadb \\
    --gpg-key-url https://mirror.mariadb.org/PublicKey \\
    https://mirror.mariadb.org/repo/10.11/ubuntu"""
    )

    parser.add_argument('--name', required=True,
                        help='Repository name (used for config filename and mirror-dedupe NAME)')
    parser.add_argument('--dest',
                        help='Destination path (relative to mirror_root/repos). Defaults to --name if omitted.')
    parser.add_argument('--out', dest='out_dir', required=True,
                        help='Output directory for scan results')
    parser.add_argument('-d', '--distribution', action='append', dest='distribution',
                        help='Override the primary distribution/suite (may be specified multiple times)')
    parser.add_argument('-D', '--distributions', dest='distributions',
                        help='Comma-separated list of distributions/suites to override')
    parser.add_argument('--arch', action='append', dest='arch',
                        help='Architecture to include (may be specified multiple times)')
    parser.add_argument('--architectures', dest='architectures',
                        help='Comma-separated list of architectures to include')
    parser.add_argument('--component', action='append', dest='component',
                        help='Component to include (may be specified multiple times)')
    parser.add_argument('--components', dest='components',
                        help='Comma-separated list of components to include')
    parser.add_argument(
        '-U',
        '--upstream',
        '--upstreams',
        dest='upstreams',
        nargs='+',
        help='Primary upstream URL followed by optional alternate upstreams',
    )
    parser.add_argument(
        '--emit-json',
        action='store_true',
        default=False,
        help='Emit a JSON snapshot file alongside the YAML config (default: no JSON)',
    )
    parser.add_argument(
        '--no-filter',
        action='store_true',
        default=False,
        help='Ignore architecture filters from mirror-dedupe.conf and emit every '
             'architecture discovered upstream (default: respect global config)',
    )
    parser.add_argument('--repo-type', dest='repo_type',
                        help='Force a specific Repo type (e.g. "apt") for unusual layouts')
    parser.add_argument('-G', '--gpg-key-url',
                        help='Explicit GPG key URL for this repository')
    parser.add_argument('upstream',
                        nargs='?',
                        help='Upstream repository URL')

    args = parser.parse_args()

    dest = args.dest or args.name

    cfg = Config.load(args.config_path)

    arch_mask = cfg.architectures

    def _normalize_arch_mask(value):
        ## @brief Normalise an architecture mask value (wildcard -> passthrough).
        ## @param value  Raw mask (``"*"``, string, or list).
        ## @return ``None`` for passthrough, a list of arch names, or ``None``.
        if isinstance(value, str):
            v = value.strip()
            if v.lower() in ('*', 'all') or not v:
                return None
            return [v]
        if isinstance(value, list):
            return value
        return None

    global_arch_mask = _normalize_arch_mask(arch_mask)

    if args.no_filter:
        global_arch_mask = None

    def _split_csv(values):
        ## @brief Split a list of comma-separated strings, deduplicated.
        ## @param values  List of strings (some may contain commas).
        ## @return Flat deduplicated list of individual items.
        items = []
        for v in values or []:
            if not v:
                continue
            parts = [p.strip() for p in v.split(',')]
            items.extend([p for p in parts if p])
        seen = set()
        result = []
        for item in items:
            if item not in seen:
                seen.add(item)
                result.append(item)
        return result

    arch_override = _split_csv((args.arch or []) + ([args.architectures] if args.architectures else []))
    if not arch_override:
        arch_override = None

    component_override = _split_csv((args.component or []) + ([args.components] if args.components else []))
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

    if args.upstreams:
        upstreams: List[str] = [u for u in args.upstreams if u]
    elif args.upstream:
        upstreams = [args.upstream]
    else:
        log("ERROR: No upstream URL provided. Supply either a positional upstream or --upstream/--upstreams.", level="ERROR")
        sys.exit(1)

    try:
        repo = scan(
            args.name,
            upstreams,
            repo_type=args.repo_type,
            distribution_overrides=distribution_overrides,
        )
    except NotImplementedError:
        log(f"ERROR: No supported Repo implementation could parse upstream {upstreams[0]!r}.\n       If this is an APT repository with an unusual layout, you may need to add\n       or extend a Repo implementation (e.g. Apt) rather than using scan.py directly.", level="ERROR")
        sys.exit(1)

    if args.gpg_key_url:
        repo.gpg_key_url = args.gpg_key_url

    config = generate_config(
        repo,
        dest,
        gpg_key_url=args.gpg_key_url,
        distribution_overrides=distribution_overrides,
        arch_override=arch_override,
        component_override=component_override,
        global_arch_mask=global_arch_mask,
    )

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    config_file = out_dir / f"{repo.name}.conf"
    config_file.write_text(config)
    if not config.endswith("\n"):
        with open(config_file, 'a') as f:
            f.write("\n")

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
    log(f"\nNext steps:\n  # Test the repository configuration before activating it\n  mirror-dedupe --test {args.name}\n\n  # If the test looks good, activate the repository:\n  mirror-dedupe --activate {args.name}\n\n  # Manual enable (equivalent to --activate) if you prefer:\n  ln -s {config_file} {Path(cfg.config_dir) / 'repos-enabled' / (args.name + '.conf')}\n\nOr simply:\n  cd {Path(cfg.config_dir) / 'repos-enabled'}\n  ln -s ../repos-available/{args.name}.conf .\n\nThis is my best guess and should give you a decent head start when mirroring this repo.\nHowever, I'm not perfect so you really should examine the config file carefully before activating it.")


if __name__ == '__main__':
    main()
