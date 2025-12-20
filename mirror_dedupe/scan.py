#!/usr/bin/env python3
"""
scan.py

  Repository scanner for mirror-dedupe

Copyright (c) 2025 Tim Hosking
Email: tim@mungerware.com
Website: https://github.com/munger
Licence: MIT
"""

import sys
import argparse
from typing import List, Optional
import os
import json

from mirror_dedupe.schema.repo import Repo
from mirror_dedupe.lib.rsync_discovery import RsyncDiscovery
from mirror_dedupe.config import load_config
from mirror_dedupe.lib.html_helpers import url_hostname
import mirror_dedupe.repos  # noqa: F401  # ensure Repo types are registered


def scan(name: str, upstreams: List[str],
         ipv6_ok: Optional[bool] = None,
         repo_type: Optional[str] = None) -> Repo:
    """Perform HTTP and rsync discovery and return a populated Repo.

    This mirrors the behaviour of test_discovery.py: construct a Repo
    from a single upstream URL, run its parser, then run
    RsyncDiscovery(repo).discover() to detect rsync capabilities and
    annotate the Repo accordingly.
    """

    primary_upstream = upstreams[0]
    print(f"Scanning {primary_upstream}...", file=sys.stderr)

    # Let Repo.from_url consult the registered Repo types (Apt, etc.) via
    # is_this_yours() while still honouring the ipv6_ok hint. Callers may
    # optionally supply repo_type to force a specific implementation (e.g.
    # "apt") for repositories with unusual layouts.
    repo = Repo.from_url(
        primary_upstream,
        upstream_urls=upstreams[1:],
        ipv6_ok=ipv6_ok,
        repo_type=repo_type,
    )
    if name:
        repo.name = name
    repo = repo.parse()

    # Run rsync discovery for each upstream in the collection.
    for upstream in repo.upstreams:
        discovery = RsyncDiscovery(repo, upstream)
        discovery.discover()

    return repo


def generate_config(repo: Repo, dest: str,
                    gpg_key_url: Optional[str] = None,
                    dist_overrides: Optional[List[str]] = None,
                    arch_override: Optional[List[str]] = None,
                    component_override: Optional[List[str]] = None,
                    global_arch_mask: Optional[List[str]] = None,
                    collapse_dists: bool = False) -> str:
    """Generate repository configuration from a fully-populated Repo.

    ``repo`` is expected to have already been parsed by its concrete
    ``Repo.Parser`` implementation and annotated by ``RsyncDiscovery``.
    This function applies user-specified filters and emits a YAML
    configuration that mirror-dedupe can consume.

    GPG keys are no longer auto-discovered here. If the caller supplies
    ``gpg_key_url``, it is passed through into the generated config
    unchanged; trust decisions and verification are left to the sync
    phase.
    """

    # Prefer values already on the Repo payload; fall back to caller override.
    if not gpg_key_url:
        gpg_key_url = repo.gpg_key_url

    total_steps = 3
    step = 1

    def next_step_label() -> str:
        nonlocal step
        label = f"[{step}/{total_steps}]"
        step += 1
        return label

    # Step 1: assume *repo* has already been fully discovered by
    # scan(), including any rsync-related mutations.
    print(f"  {next_step_label()} Examining discovered repository structure...", file=sys.stderr)
    
    # Use the selected upstream index to pull method and IPv6 state.
    upstream_index = repo.upstream_idx
    upstream_node = repo.upstreams[upstream_index] if repo.upstreams else None

    sync_method = upstream_node.sync_method if upstream_node else None
    ipv6_ok = upstream_node.ipv6_ok if upstream_node else True
    
    # Derive discovered distributions (suite/pocket names) from the
    # Repo.distributions collection populated by the parser. Fall back to
    # a synthetic "stable" distribution if nothing was found so we can
    # still produce a minimal, editable config.
    discovered: List[str] = []
    if repo.distributions:
        for dist in repo.distributions:
            if dist.name and dist.name not in discovered:
                discovered.append(str(dist.name))
    if not discovered:
        # Discovery could not identify any suites/pockets under dists/.
        # Do not invent a synthetic "stable" default; the user must
        # provide explicit --dist/--release/--releases values in this
        # situation so the generated config reflects an intentional
        # choice rather than a guess.
        print("      Warning: Could not auto-detect distributions", file=sys.stderr)

    # Step 2: decide which distributions to use for this config. We never try to
    # "auto-select" a primary series by Version:
    #
    #   * If the user provides explicit --dist/--release/--releases values,
    #     we use them exactly (with a special "all" value meaning all
    #     discovered suites).
    #   * If the user provides nothing, we default to all discovered suites
    #     as if "--releases all" had been specified.

    all_dists_mode = False
    collapsed_from_all = False

    if dist_overrides:
        # Normalise and inspect for the special "all" token.
        dists = [d for d in dist_overrides if d]
        if any(d.lower() == "all" for d in dists):
            all_dists_mode = True
            distributions = discovered
        else:
            # When the user provides explicit distributions, trust the
            # list as-is. Previous versions attempted to warn about
            # potential spelling mistakes by checking the discovered
            # suites under dists/, but this proved too noisy for
            # advanced layouts and synthetic pockets, so we no longer
            # emit those warnings here.
            distributions = dists
    else:
        # No explicit distributions were provided.
        if not discovered:
            # With no auto-detected suites and no overrides, we cannot
            # safely guess a default. Require the user to specify
            # --dist/--release/--releases explicitly.
            print(
                "ERROR: No distributions were auto-detected and no --dist/--release/--releases overrides were provided.",
                file=sys.stderr,
            )
            print(
                "       Please rerun with explicit --releases (or --dist) to choose which suites to mirror.",
                file=sys.stderr,
            )
            sys.exit(1)
        # Otherwise, default to all discovered suites. This is
        # equivalent to the user specifying "--releases all".
        all_dists_mode = True
        distributions = discovered

    # Optional collapse: when enabled and operating in "all" mode,
    # collapse pocket variants back to their base suites (e.g.
    # noble[-updates/-security/-backports/-proposed] => noble) so that
    # sync-time expansion can regenerate the standard pockets.
    if all_dists_mode and collapse_dists and discovered:
        pocket_suffixes = (
            "-updates",
            "-security",
            "-backports",
            "-proposed",
        )
        base_to_seen: dict[str, set[str]] = {}
        for d in discovered:
            # Only consider simple suite names; leave more complex
            # paths like "noble-proposed/dalamation" untouched.
            if "/" in d:
                continue
            base = d
            for suf in pocket_suffixes:
                if base.endswith(suf):
                    base = base[: -len(suf)]
                    break
            base_to_seen.setdefault(base, set()).add(d)

        # If collapsing would actually reduce the list, switch to the
        # base names and treat this as non-all_dists_mode so that
        # expand_distributions remains enabled. Record that this list
        # was derived from a collapsed set of variants so we can add a
        # helpful auto-expansion comment in the generated config.
        if base_to_seen and len(base_to_seen) < len(discovered):
            distributions = sorted(base_to_seen.keys())
            all_dists_mode = False
            collapsed_from_all = True

    print(f"      Using distributions: {', '.join(distributions)}", file=sys.stderr)

    # Step 3: derive architectures and components from the parsed Repo
    # instead of re-parsing Release files here. The Apt parser populates
    # repo.architectures and repo.components with unique schema nodes.
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

    # Fallbacks if nothing useful was found.
    detected_arches = sorted(arch_set) if arch_set else ["amd64"]
    detected_components = sorted(comp_set) if comp_set else ["main"]

    # Honour explicit architecture/component filters as hard restrictions.
    # We still use the detected sets for basic sanity warnings, but the
    # generated config reflects exactly what the user requested.
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

    # Apply a global architectures mask from mirror-dedupe.conf, if any, so
    # the generated config matches the effective sync-time policy.
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

    print("", file=sys.stderr)  # newline after dots
    print(f"      Architectures: {', '.join(architectures)}", file=sys.stderr)
    print(f"      Components: {', '.join(components)}", file=sys.stderr)

    # Generate YAML config. In this code path a blank name is treated as a
    # bug: --name is required at the CLI and scan() always sets repo.name.
    name = repo.name
    upstream_index = repo.upstream_idx
    upstream_entries = []
    for u in repo.upstreams:
        entry = {"url": u.url}
        if u.sync_method is not None:
            entry["sync_method"] = u.sync_method
        if u.ipv6_ok is not None:
            entry["ipv6_ok"] = u.ipv6_ok
        if getattr(u, "rsync_roots", None):
            entry["rsync"] = u.rsync_roots
        upstream_entries.append(entry)

    config_lines = [
        f"# {name} repository",
        "",
        f"name: {name}",
        f"dest: {dest}",
    ]

    # Persist ordered upstreams and the selected primary index.
    if upstream_entries:
        config_lines.append("upstreams:")
        for upstream in upstream_entries:
            config_lines.append("  - url: " + upstream["url"])
            if "sync_method" in upstream:
                config_lines.append(f"    sync_method: {upstream['sync_method']}")
            if "ipv6_ok" in upstream:
                config_lines.append(f"    ipv6_ok: {'true' if upstream['ipv6_ok'] else 'false'}")
            if "rsync" in upstream:
                config_lines.append("    rsync:")
                for root in upstream["rsync"]:
                    config_lines.append(f"      - {root}")
        config_lines.append(f"upstream_idx: {upstream_index}")

    if gpg_key_url:
        # Explicit GPG key URL provided by user; pass through unchanged.
        # Trust decisions and any signature verification are deferred to
        # the sync phase rather than being handled here.
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
        # In all_dists_mode we emit every discovered suite literally. This is
        # intended for full/archival mirrors and is expected to be edited by
        # hand afterwards.
        for dist in discovered:
            config_lines.append(f"  - {dist}")
    else:
        for dist in distributions:
            config_lines.append(f"  - {dist}")
        if collapsed_from_all or (len(distributions) == 1 and distributions[0] not in ['stable', 'unstable', 'testing']):
            config_lines.append("# Distribution auto-expands to include variants (e.g., -updates, -security)")

    # Check if we should disable distribution expansion. If only one
    # distribution and it's 'stable', disable expansion. In all_dists_mode
    # we always disable expansion because the list already enumerates all
    # suites explicitly.
    if (all_dists_mode and not collapse_dists) or (len(distributions) == 1 and distributions[0] == 'stable'):
        config_lines.append("expand_distributions: false")

    config_lines.append("")  # Trailing newline
    
    return '\n'.join(config_lines)


def main() -> None:
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

    # No GPG key auto-detection or reachability checks here; if provided,
    # gpg_key_url is simply passed through into the generated config.

    # Normalise dest: default to name if not provided explicitly.
    dest = args.dest or args.name

    # Load global config once so we can honour IPv6 policy when scanning
    # and apply the same global architectures mask that mirror-dedupe
    # will later enforce at sync time.
    cfg = load_config(args.config_dir)
    global_disable_ipv6 = bool(cfg.get('disable_ipv6', False))
    ipv6_ok = not global_disable_ipv6

    # Normalise the global architectures mask. Behaviour mirrors
    # mirror_dedupe.config._normalize_arch_mask: "*"/"all" => no mask,
    # list => list, other => None.
    arch_mask = cfg.get('architectures', '*')
    global_collapse_dists = bool(cfg.get('collapse_distributions', False))

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

    # Normalise arch/component overrides
    def _split_csv(values):
        items = []
        for v in values or []:
            if not v:
                continue
            parts = [p.strip() for p in v.split(',')]
            items.extend([p for p in parts if p])
        # De-duplicate while preserving order
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

    # Dist overrides: singular flags (--dist/--release) are repeatable
    # single values; the plural form (--releases) is a comma-separated list.
    dist_overrides: Optional[List[str]] = None
    dist_values: List[str] = []
    if args.dist:
        dist_values.extend(args.dist)
    if args.releases:
        dist_values.extend(_split_csv([args.releases]))
    if dist_values:
        # De-duplicate while preserving order
        seen_d = set()
        ordered: List[str] = []
        for d in dist_values:
            if d and d not in seen_d:
                seen_d.add(d)
                ordered.append(d)
        dist_overrides = ordered or None

    # Determine ordered upstreams. The first URL is used for discovery; the
    # rest remain available for selection in the generated config.
    if args.upstreams:
        upstreams: List[str] = [u for u in args.upstreams if u]
    elif args.upstream:
        upstreams = [args.upstream]
    else:
        print("ERROR: No upstream URL provided. Supply either a positional upstream or --upstream/--upstreams.", file=sys.stderr)
        sys.exit(1)

    # Perform discovery first so we have a fully populated Repo, then
    # generate configuration based solely on that Repo plus CLI filters.
    try:
        repo = scan(
            args.name,
            upstreams,
            ipv6_ok=ipv6_ok,
            repo_type=args.repo_type,
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

    # Persist any user-supplied GPG key URL on the Repo so it survives
    # snapshotting alongside the generated config.
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
 
    # Derive the config path and write the generated configuration to disk
    # so it can be used directly by mirror-dedupe. The stderr guidance
    # below is kept unchanged.
    config_dir = os.path.join(args.config_dir, 'repos-available')
    config_file = os.path.join(config_dir, f"{repo.name}.conf")

    os.makedirs(config_dir, exist_ok=True)
    with open(config_file, 'w', encoding='utf-8') as f:
        f.write(config)
        if not config.endswith("\n"):
            f.write("\n")

    # Temporary: write a JSON snapshot of the Repo alongside the config,
    # using the repo name as the basename. This makes it easier to
    # correlate snapshots with generated configs regardless of which
    # upstream or mirror was used during discovery.
    #
    # Repo inherits from Node, which provides ``snapshot()`` as the
    # canonical way to obtain a plain JSON-serialisable payload. Use that
    # here instead of any older ``to_payload`` helpers.
    try:
        snapshot_basename = repo.name
        snapshot_path = os.path.join(config_dir, f"{snapshot_basename}.json")
        with open(snapshot_path, 'w', encoding='utf-8') as sf:
            # Preserve the key order produced by Repo.snapshot() (an
            # OrderedDict) instead of re-sorting alphabetically.
            json.dump(repo.snapshot(), sf, indent=2)
        print(f"Snapshot saved to: {snapshot_path}", file=sys.stderr)
    except Exception as e:  # pragma: no cover - best-effort debug aid
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
