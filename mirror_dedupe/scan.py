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
import subprocess
import urllib.request
import urllib.error
from typing import List, Dict, Optional


def check_rsync_available(url: str) -> bool:
    """Check if rsync is available for the given URL"""
    # Convert HTTP URL to rsync URL
    rsync_url = url.replace('http://', 'rsync://').replace('https://', 'rsync://')
    if not rsync_url.endswith('/'):
        rsync_url += '/'
    
    try:
        # Try to list the directory with rsync
        result = subprocess.run(
            ['rsync', '--list-only', rsync_url],
            capture_output=True,
            timeout=10
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def fetch_url(url: str) -> Optional[str]:
    """Fetch content from URL"""
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            return response.read().decode('utf-8')
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError):
        return None


def discover_distributions(upstream: str) -> List[str]:
    """Discover available distributions by checking dists/ directory"""
    dists_url = f"{upstream.rstrip('/')}/dists/"
    
    # Try to fetch the dists directory listing
    content = fetch_url(dists_url)
    if not content:
        return []
    
    # Parse HTML directory listing for distribution names
    # This is a simple approach - look for common patterns
    distributions = []
    for line in content.split('\n'):
        # Look for href links to directories
        if 'href="' in line and '/"' in line:
            start = line.find('href="') + 6
            end = line.find('/"', start)
            if start > 5 and end > start:
                dist_name = line[start:end]
                # Filter out parent directory and common non-distribution entries
                if dist_name not in ['.', '..', 'stable', 'unstable', 'testing']:
                    distributions.append(dist_name)
    
    return distributions


def parse_release_file(upstream: str, distribution: str) -> Dict[str, any]:
    """Parse Release file to extract architectures and components"""
    release_url = f"{upstream.rstrip('/')}/dists/{distribution}/Release"
    content = fetch_url(release_url)
    
    if not content:
        return {}
    
    info = {
        'architectures': [],
        'components': [],
    }
    
    for line in content.split('\n'):
        line = line.strip()
        if line.startswith('Architectures:'):
            archs = line.split(':', 1)[1].strip().split()
            # Filter out 'all' and 'source'
            info['architectures'] = [a for a in archs if a not in ['all', 'source']]
        elif line.startswith('Components:'):
            info['components'] = line.split(':', 1)[1].strip().split()
    
    return info


def detect_gpg_key(upstream: str) -> Optional[tuple]:
    """Try to detect GPG key URL"""
    # Common GPG key locations
    common_paths = [
        'gpg.key',
        'Release.key',
        'archive.key',
        'project/ubuntu-archive-keyring.gpg',
        'gpgkey/nodesource-repo.gpg.key',
        'KEY.gpg',
        'pubkey.gpg',
        'apt.gpg',
    ]
    
    for path in common_paths:
        url = f"{upstream.rstrip('/')}/{path}"
        if fetch_url(url):
            return (url, path)
    
    return None


def generate_config(name: str, dest: str, upstream: str) -> str:
    """Generate repository configuration"""
    
    print(f"Scanning {upstream}...", file=sys.stderr)
    
    # Detect sync method
    sync_method = 'rsync' if check_rsync_available(upstream) else 'https'
    print(f"  Sync method: {sync_method}", file=sys.stderr)
    
    # Discover distributions
    distributions = discover_distributions(upstream)
    if not distributions:
        print("  Warning: Could not auto-detect distributions", file=sys.stderr)
        distributions = ['stable']  # Default fallback
    else:
        print(f"  Found distributions: {', '.join(distributions[:5])}", file=sys.stderr)
        # Use first distribution as default
        distributions = [distributions[0]]
    
    # Parse Release file for first distribution
    release_info = parse_release_file(upstream, distributions[0])
    architectures = release_info.get('architectures', ['amd64'])
    components = release_info.get('components', ['main'])
    
    print(f"  Architectures: {', '.join(architectures)}", file=sys.stderr)
    print(f"  Components: {', '.join(components)}", file=sys.stderr)
    
    # Detect GPG key
    gpg_info = detect_gpg_key(upstream)
    
    # Generate YAML config
    config_lines = [
        f"# {name} repository",
        "",
        f"name: {name}",
        f"upstream: {upstream}",
        f"dest: {dest}",
        f"sync_method: {sync_method}",
    ]
    
    if gpg_info:
        gpg_url, gpg_path = gpg_info
        config_lines.extend([
            f"gpg_key_url: {gpg_url}",
            f"gpg_key_path: {gpg_path}",
        ])
        print(f"  GPG key: {gpg_path}", file=sys.stderr)
    else:
        config_lines.extend([
            "# GPG key not auto-detected - add manually if required:",
            "# gpg_key_url: https://example.com/path/to/key.gpg",
            "# gpg_key_path: path/to/key.gpg",
        ])
        print(f"  Warning: GPG key not found - please add manually if required", file=sys.stderr)
    
    # Check if we should disable distribution expansion
    # If only one distribution and it's 'stable', disable expansion
    if len(distributions) == 1 and distributions[0] == 'stable':
        config_lines.append("expand_distributions: false")
    
    config_lines.append("architectures:")
    for arch in architectures:
        config_lines.append(f"  - {arch}")
    
    config_lines.append("components:")
    for comp in components:
        config_lines.append(f"  - {comp}")
    
    config_lines.append("distributions:")
    for dist in distributions:
        config_lines.append(f"  - {dist}")
    
    if len(distributions) == 1 and distributions[0] not in ['stable', 'unstable', 'testing']:
        config_lines.append("# Distribution auto-expands to include variants (e.g., -updates, -security)")
    
    config_lines.append("")  # Trailing newline
    
    return '\n'.join(config_lines)


def main():
    parser = argparse.ArgumentParser(
        description='Scan a repository and generate mirror-dedupe configuration',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  mirror-dedupe-scan --name ubuntu --dest ubuntu/main http://archive.ubuntu.com/ubuntu
  mirror-dedupe-scan --name grafana --dest grafana https://apt.grafana.com
        """
    )
    
    parser.add_argument('--name', required=True,
                       help='Repository name')
    parser.add_argument('--dest', required=True,
                       help='Destination path (relative to repo_root)')
    parser.add_argument('--config-dir', default='/etc/mirror-dedupe',
                       help='Configuration directory (default: /etc/mirror-dedupe)')
    parser.add_argument('upstream', 
                       help='Upstream repository URL')
    
    args = parser.parse_args()
    
    # Generate configuration
    config = generate_config(args.name, args.dest, args.upstream)
    
    # Save to repos-available
    import os
    repos_available = os.path.join(args.config_dir, 'repos-available')
    os.makedirs(repos_available, exist_ok=True)
    
    config_file = os.path.join(repos_available, f'{args.name}.conf')
    with open(config_file, 'w') as f:
        f.write(config)
    
    print(f"\nConfiguration saved to: {config_file}", file=sys.stderr)
    print(f"\nTo enable this repository:", file=sys.stderr)
    print(f"  ln -s {config_file} {os.path.join(args.config_dir, 'repos-enabled', args.name + '.conf')}", file=sys.stderr)
    print(f"\nOr simply:", file=sys.stderr)
    print(f"  cd {args.config_dir}/repos-enabled", file=sys.stderr)
    print(f"  ln -s ../repos-available/{args.name}.conf .", file=sys.stderr)


if __name__ == '__main__':
    main()
