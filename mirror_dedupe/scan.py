#!/usr/bin/env python3
## @file scan.py
##
## @brief Repository scanner for mirror-dedupe.
##
## Provides the ``mirror-dedupe-scan`` CLI entry point for HTTP
## discovery of repository metadata (distributions, architectures,
## components) and generation of ``repos-available/*.conf`` files.
##
## @copyright Copyright (c) 2025-2026 Tim Hosking
## @see https://github.com/munger
## @par Licence: MIT

import sys
import argparse
from typing import List, Optional
import os
import json
import yaml

from mirror_dedupe.schema.repo import Repo
from mirror_dedupe.config import Config
from mirror_dedupe.lib.html_helpers import url_hostname
import mirror_dedupe.repos  # noqa: F401  # ensure Repo types are registered


def scan(name: str, upstreams: List[str],
         ipv6_ok: Optional[bool] = None,
         repo_type: Optional[str] = None,
         dist_overrides: Optional[List[str]] = None,
         config_dir: Optional[str] = None) -> Repo:
    ## @brief Perform HTTP discovery and return a populated Repo.
    ##
    ## Creates a Repo via ``Repo.from_url``, optionally primes explicit
    ## distribution candidates, and runs the concrete parser to populate
    ## distributions, architectures, components, and releases.
    ##
    ## If ``config_dir`` is provided and a previous scan result exists at
    ## ``<config_dir>/repos-available/<name>.conf``, cached discovery params
    ## are loaded into the Repo before parsing to accelerate re-scans.
    ##
    ## @param name            Repository name.
    ## @param upstreams       Ordered list of upstream URLs (first is primary).
    ## @param ipv6_ok         Whether IPv6 is considered usable.
    ## @param repo_type       Force a specific Repo type (e.g. ``"apt"``).
    ## @param dist_overrides  Explicit distribution names to probe.
    ## @param config_dir      Config directory for loading cached params.
    ## @return A fully parsed Repo instance.

    primary_upstream = upstreams[0]
    print(f"Scanning {primary_upstream}...", file=sys.stderr)

    # --release/--dist/--releases are APT-specific concepts, so if the user
    # supplies explicit distribution names without also specifying a repo
    # type, assume APT rather than failing type detection.
    if repo_type is None and dist_overrides:
        repo_type = "apt"

    repo = Repo.from_url(
        primary_upstream,
        upstream_urls=upstreams[1:],
        ipv6_ok=ipv6_ok,
        repo_type=repo_type,
    )
    if name:
        repo.name = name

    if dist_overrides:
        repo.dist_candidates = dist_overrides

    # Load cached discovery params from a previous scan, if available
    if config_dir:
        existing = os.path.join(config_dir, "repos-available", f"{name}.conf")
        if os.path.exists(existing):
            try:
                with open(existing) as f:
                    data = yaml.safe_load(f) or {}
                cached = data.get("params")
                if cached:
                    repo.setdefault("params", {}).update(cached)
            except Exception:
                pass

    repo = repo.parse()

    return repo


def generate_config(repo: Repo, dest: str,
                    gpg_key_url: Optional[str] = None,
                    dist_overrides: Optional[List[str]] = None,
                    arch_override: Optional[List[str]] = None,
                    component_override: Optional[List[str]] = None,
                    global_arch_mask: Optional[List[str]] = None,
                    collapse_dists: bool = False) -> str:
    ## @brief Generate repository configuration from a fully-populated Repo.
    ##
    ## ``repo`` is expected to have already been parsed by its concrete
    ## ``Repo.Parser`` implementation.  This function applies user-specified
    ## filters and emits a YAML configuration that mirror-dedupe can consume.
    ##
    ## GPG keys are no longer auto-discovered here.  If the caller supplies
    ## ``gpg_key_url``, it is passed through into the generated config
    ## unchanged; trust decisions and verification are left to the sync
    ## phase.
    ##
    ## @param repo               Parsed Repo instance.
    ## @param dest               Destination path (relative to repo_root).
    ## @param gpg_key_url        Optional GPG key URL.
    ## @param dist_overrides     Override distribution list.
    ## @param arch_override      Override architecture list.
    ## @param component_override Override component list.
    ## @param global_arch_mask   Global architecture mask from config.
    ## @param collapse_dists     Whether to collapse pocket variants.
    ## @return YAML configuration string.

    if not gpg_key_url:
        gpg_key_url = repo.gpg_key_url

    total_steps = 3
    step = 1

    def next_step_label() -> str:
        nonlocal step
        label = f"[{step}/{total_steps}]"
        step += 1
        return label

    print(f"  {next_step_label()} Examining discovered repository structure...", file=sys.stderr)

    upstream_index = repo.upstream_idx
    upstream_node = repo.upstreams[upstream_index] if repo.upstreams else None

    sync_method = upstream_node.sync_method if upstream_node else None

    discovered: List[str] = []
    if repo.distributions:
        for dist in repo.distributions:
            if dist.name and dist.name not in discovered:
                discovered.append(str(dist.name))
    if not discovered:
        print("      Warning: Could not auto-detect distributions", file=sys.stderr)

    all_dists_mode = False
    collapsed_from_all = False

    if dist_overrides:
        dists = [d for d in dist_overrides if d]
        if any(d.lower() == "all" for d in dists):
            all_dists_mode = True
            distributions = discovered
        else:
            distributions = dists
    else:
        if not discovered:
            print(
                "ERROR: No distributions were auto-detected and no --dist/--release/--releases overrides were provided.",
                file=sys.stderr,
            )
            print(
                "       Please rerun with explicit --releases (or --dist) to choose which suites to mirror.",
                file=sys.stderr,
            )
            sys.exit(1)
        all_dists_mode = True
        distributions = discovered

    if all_dists_mode and collapse_dists and discovered:
        pocket_suffixes = (
            "-updates",
            "-security",
            "-backports",
            "-proposed",
        )
        base_to_seen: dict[str, set[str]] = {}
        for d in discovered:
            if "/" in d:
                continue
            base = d
            for suf in pocket_suffixes:
                if base.endswith(suf):
                    base = base[: -len(suf)]
                    break
            base_to_seen.setdefault(base, set()).add(d)

        if base_to_seen and len(base_to_seen) < len(discovered):
            distributions = sorted(base_to_seen.keys())
            all_dists_mode = False
            collapsed_from_all = True

    print(f"      Using distributions: {', '.join(distributions)}", file=sys.stderr)

    print(f"  {next_step_label()} Discovering architectures/components...", file=sys.stderr)
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
                print(
                    f"      Warning: architecture '{a}' was not found in Release metadata",
                    file=sys.stderr,
                )
    else:
        architectures = detected_arches

    if component_override:
        components = [c for c in component_override if c]
        detected_set = set(detected_components)
        for c in components:
            if c not in detected_set:
                print(
                    f"      Warning: component '{c}' was not found in Release metadata",
                    file=sys.stderr,
                )
    else:
        components = detected_components

    if global_arch_mask:
        mask_set = set(global_arch_mask)
        before = architectures
        architectures = [a for a in architectures if a in mask_set]
        removed = [a for a in before if a not in architectures]
        if removed:
            print(
                f"      Note: architectures filtered by global mask: removed {', '.join(removed)}",
                file=sys.stderr,
            )

    print("", file=sys.stderr)
    print(f"      Architectures: {', '.join(architectures)}", file=sys.stderr)
    print(f"      Components: {', '.join(components)}", file=sys.stderr)

    name = repo.name
    upstream_index = repo.upstream_idx
    upstream_entries = []
    for u in repo.upstreams:
        entry = {"url": u.url}
        if u.sync_method is not None:
            entry["sync_method"] = u.sync_method
        upstream_entries.append(entry)

    config_lines = [
        f"# {name} repository",
        "",
        f"name: {name}",
        f"dest: {dest}",
    ]

    if upstream_entries:
        config_lines.append("upstreams:")
        for upstream in upstream_entries:
            config_lines.append("  - url: " + upstream["url"])
            if "sync_method" in upstream:
                config_lines.append(f"    sync_method: {upstream['sync_method']}")
        config_lines.append(f"upstream_idx: {upstream_index}")

    if gpg_key_url:
        config_lines.append(f"gpg_key_url: {gpg_key_url}")
        print(f"      GPG key URL (user-supplied): {gpg_key_url}", file=sys.stderr)

    config_lines.append("architectures:")
    for arch in architectures:
        config_lines.append(f"  - {arch}")

    config_lines.append("components:")
    for comp in components:
        config_lines.append(f"  - {comp}")

    config_lines.append("distributions:")
    if all_dists_mode:
        for dist in discovered:
            config_lines.append(f"  - {dist}")
    else:
        for dist in distributions:
            config_lines.append(f"  - {dist}")
        if collapsed_from_all or (len(distributions) == 1 and distributions[0] not in ['stable', 'unstable', 'testing']):
            config_lines.append("# Distribution auto-expands to include variants (e.g., -updates, -security)")

    if (all_dists_mode and not collapse_dists) or (len(distributions) == 1 and distributions[0] == 'stable'):
        config_lines.append("expand_distributions: false")

    params = repo.get("params")
    if params:
        config_lines.append("params:")
        method = params.get("discovery_method", "html_bfs")
        config_lines.append(f"  discovery_method: {method}")
        nobrowse = params.get("nobrowse", False)
        if nobrowse:
            config_lines.append("  nobrowse: true")

    config_lines.append("")

    return '\n'.join(config_lines)


def main() -> None:
    ## @brief CLI entry point for ``mirror-dedupe-scan``.

    parser = argparse.ArgumentParser(
        description='Scan a repository and generate mirror-dedupe configuration',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples:
  # Simple form: dest defaults to --name
  mirror-dedupe-scan --name ubuntu http://archive.ubuntu.com/ubuntu

  # Custom dest path
  mirror-dedupe-scan --name ubuntu --dest ubuntu/main http://archive.ubuntu.com/ubuntu

  # With an explicit GPG key override
  mirror-dedupe-scan --name mariadb \
    --gpg-key-url https://mirror.mariadb.org/PublicKey \
    https://mirror.mariadb.org/repo/10.11/ubuntu"""
    )

    parser.add_argument('--name', required=True,
                        help='Repository name (used for config filename and mirror-dedupe NAME)')
    parser.add_argument('--dest',
                        help='Destination path (relative to repo_root). Defaults to --name if omitted.')
    parser.add_argument('--config', '--config-dir', dest='config_dir', default='/etc/mirror-dedupe',
                        help='Configuration directory (default: /etc/mirror-dedupe)')
    parser.add_argument('-r', '--dist', '--release', action='append', dest='dist',
                        help='Override the primary distribution/suite (may be specified multiple times)')
    parser.add_argument('-R', '--releases', dest='releases',
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
    collapse_group = parser.add_mutually_exclusive_group()
    collapse_group.add_argument(
        '--collapse-dists',
        dest='collapse_dists',
        action='store_true',
        help='Collapse discovered distributions to base suites where possible',
    )
    collapse_group.add_argument(
        '--no-collapse-dists',
        dest='collapse_dists',
        action='store_false',
        help='Do not collapse discovered distributions; emit all variants explicitly',
    )
    parser.set_defaults(collapse_dists=None)
    parser.add_argument('--repo-type', dest='repo_type',
                        help='Force a specific Repo type (e.g. "apt") for unusual layouts')
    parser.add_argument('-G', '--gpg-key-url',
                        help='Explicit GPG key URL for this repository')
    parser.add_argument('upstream',
                        nargs='?',
                        help='Upstream repository URL')

    args = parser.parse_args()

    dest = args.dest or args.name

    cfg = Config.load(args.config_dir)
    global_disable_ipv6 = bool(cfg.disable_ipv6)
    ipv6_ok = not global_disable_ipv6

    arch_mask = cfg.architectures
    global_collapse_dists = bool(cfg.collapse_distributions)

    def _normalize_arch_mask(value):
        if isinstance(value, str):
            v = value.strip()
            if v.lower() in ('*', 'all') or not v:
                return None
            return [v]
        if isinstance(value, list):
            return value
        return None

    global_arch_mask = _normalize_arch_mask(arch_mask)

    def _split_csv(values):
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

    if args.upstreams:
        upstreams: List[str] = [u for u in args.upstreams if u]
    elif args.upstream:
        upstreams = [args.upstream]
    else:
        print("ERROR: No upstream URL provided. Supply either a positional upstream or --upstream/--upstreams.", file=sys.stderr)
        sys.exit(1)

    try:
        repo = scan(
            args.name,
            upstreams,
            ipv6_ok=ipv6_ok,
            repo_type=args.repo_type,
            dist_overrides=dist_overrides,
            config_dir=args.config_dir,
        )
    except NotImplementedError:
        print(
            f"ERROR: No supported Repo implementation could parse upstream {upstreams[0]!r}.",
            file=sys.stderr,
        )
        print(
            "       If this is an APT repository with an unusual layout, you may need to add",
            file=sys.stderr,
        )
        print(
            "       or extend a Repo implementation (e.g. Apt) rather than using scan.py directly.",
            file=sys.stderr,
        )
        sys.exit(1)

    if args.gpg_key_url:
        repo.gpg_key_url = args.gpg_key_url

    config = generate_config(
        repo,
        dest,
        gpg_key_url=args.gpg_key_url,
        dist_overrides=dist_overrides,
        arch_override=arch_override,
        component_override=component_override,
        global_arch_mask=global_arch_mask,
        collapse_dists=(
            args.collapse_dists
            if args.collapse_dists is not None
            else global_collapse_dists
        ),
    )

    config_dir = os.path.join(args.config_dir, 'repos-available')
    config_file = os.path.join(config_dir, f"{repo.name}.conf")

    os.makedirs(config_dir, exist_ok=True)
    with open(config_file, 'w', encoding='utf-8') as f:
        f.write(config)
        if not config.endswith("\n"):
            f.write("\n")

    try:
        snapshot_basename = repo.name
        snapshot_path = os.path.join(config_dir, f"{snapshot_basename}.json")
        with open(snapshot_path, 'w', encoding='utf-8') as sf:
            json.dump(repo.snapshot(), sf, indent=2)
        print(f"Snapshot saved to: {snapshot_path}", file=sys.stderr)
    except Exception as e:
        print(f"Warning: failed to write snapshot: {e}", file=sys.stderr)

    print(f"Configuration saved to: {config_file}", file=sys.stderr)
    print("", file=sys.stderr)
    print("Next steps:", file=sys.stderr)
    print("  # Test the repository configuration before activating it", file=sys.stderr)
    print(f"  mirror-dedupe --test {args.name}", file=sys.stderr)
    print("", file=sys.stderr)
    print("  # If the test looks good, activate the repository:", file=sys.stderr)
    print(f"  mirror-dedupe --activate {args.name}", file=sys.stderr)
    print("", file=sys.stderr)
    print("  # Manual enable (equivalent to --activate) if you prefer:", file=sys.stderr)
    print(f"  ln -s {config_file} {os.path.join(args.config_dir, 'repos-enabled', args.name + '.conf')}", file=sys.stderr)
    print(f"\nOr simply:", file=sys.stderr)
    print(f"  cd {args.config_dir}/repos-enabled", file=sys.stderr)
    print(f"  ln -s ../repos-available/{args.name}.conf .", file=sys.stderr)

    print("", file=sys.stderr)
    print("This is my best guess and should give you a decent head start when mirroring this repo.", file=sys.stderr)
    print("However, I'm not perfect so you really should examine the config file carefully before activating it.", file=sys.stderr)


if __name__ == '__main__':
    main()
