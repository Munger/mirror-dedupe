#!/usr/bin/env python3
"""End-to-end HTTP discovery test using the library HTTPDiscovery.

This script exercises the mirror_dedupe.lib HTTP discovery stack against a
handful of real upstream URLs and prints the discovered distributions,
components, and architectures, followed by a full JSON dump of the
RepoInfo. It is intentionally simple and is not part of the live scanner
codebase.
"""

from typing import List
import json
import sys
from pathlib import Path
from typing import List
from urllib.parse import urlparse

from mirror_dedupe import schema as Schema
from mirror_dedupe.lib.network_client import NetworkClient
from mirror_dedupe.lib.rsync_discovery import RsyncDiscovery
from mirror_dedupe.schema.repo import Repo, Repos
from mirror_dedupe.repos.apt.apt import Apt

def test_http_discovery() -> None:
    """Test HTTP discovery using HTTPDiscovery + Repo.Content."""

    test_urls: List[str] = [
        "http://archive.ubuntu.com/ubuntu",
        "http://ports.ubuntu.com/ubuntu-ports",
        "http://ubuntu-cloud.archive.canonical.com/ubuntu",
        "http://ftp.uk.debian.org/debian",
        "https://repo.zabbix.com/zabbix/8.0/release/ubuntu",
    ]

    # Global IPv6/IPv4 connectivity probe against a known dual-stack host.
    ipv6_probe, ipv4_probe = NetworkClient.test_remote("one.one.one.one", 53)
    print(
        "Global connectivity probe one.one.one.one:53 -> "
        f"ipv6_ok={ipv6_probe}, ipv4_ok={ipv4_probe}",
        file=sys.stderr,
    )
    global_ipv6_ok = ipv6_probe

    # Collect all Repo objects in case we still want an aggregate view.
    repos = Repos()

    output_dir = Path("test_output")
    output_dir.mkdir(parents=True, exist_ok=True)

    for url in test_urls:
        print(f"\n=== Testing {url} ===", file=sys.stderr)

        # Derive a stable snapshot filename per host so we can reuse
        # previously parsed schema trees without re-running HTTP discovery.
        host = urlparse(url).netloc or "unknown"
        snapshot_path = output_dir / f"{host}.json"

        if snapshot_path.exists():
            # Fast path: restore an Apt Repo (including HTTP client wiring)
            # from the existing JSON snapshot.
            with snapshot_path.open("r", encoding="utf-8") as f:
                snapshot = json.load(f)
            repo = Apt.restore(snapshot)
        else:
            # Slow path: construct a Repo bound to this URL, seeding ipv6_ok
            # from the global connectivity probe and pinning repo_type to
            # "apt" so that the Apt repo-type implementation is always used
            # while we stabilise the data model. Repo.from_url() owns the
            # HTTP client and parser wiring.
            repo_obj = Repo.from_url(
                url,
                ipv6_ok=global_ipv6_ok,
                repo_type="apt",
            )

            repo = repo_obj.parse()

            # Persist a snapshot of the parsed Repo for future runs.
            with snapshot_path.open("w", encoding="utf-8") as f:
                json.dump(repo.snapshot(), f, indent=2, sort_keys=True)

        if False:  # Disabled: verbose HTTP discovery noise for manual debugging.
            # distributions is now a list of Distribution nodes, not a mapping.
            suites = [dist.name for dist in repo.distributions]
            print(f"Distributions ({len(suites)}): {suites}", file=sys.stderr)

            # Show a bit more detail for the first few suites.
            for suite in suites[:3]:
                # Find the matching Distribution node by name.
                info = next((d for d in repo.distributions if d.name == suite), None)
                if info is None:
                    continue
                components = info.get("components", [])
                architectures = info.get("architectures", [])
                print(
                    f"  {suite}: components={components}, "
                    f"architectures={architectures}",
                    file=sys.stderr,
                )

        # Run rsync discovery for each upstream to find and persist
        # appropriate rsync roots based on the discovered schema.
        all_candidates = []
        for upstream in repo.upstreams:
            rsync_helper = RsyncDiscovery(repo, upstream)
            rsync_candidates = rsync_helper.discover()
            if rsync_candidates:
                all_candidates.extend(rsync_candidates)
        print("  rsync candidates:", all_candidates, file=sys.stderr)

        # Keep this Repo object for the final JSON array dump.
        repos.append(repo)

        # Emit a per-repo JSON file named after the upstream hostname.
        upstream = repo.upstreams[repo.upstream_idx].url
        hostname = urlparse(upstream).hostname or "unknown"
        out_path = output_dir / f"{hostname}.json"
        with out_path.open("w", encoding="utf-8") as fh:
            fh.write(repo.to_pretty_json())

    # Dump all Repo objects as a single JSON array for inspection.
    # print(repos.to_pretty_json())


if __name__ == "__main__":
    test_http_discovery()
