<!--
README.md : Ubuntu mirror synchronisation with global deduplication

Copyright (c) 2025 Tim Hosking
Email: tim@mungerware.com
Website: https://github.com/munger
Licence: MIT
-->

Pool-based content-addressable APT repository mirror sync with global deduplication via hardlinks.

## Features

- Pool-based content-addressable storage: every unique file is stored once by SHA-256 hash
- Hardlink deduplication across all mirrored repositories
- HTTPS-based sync with automatic upstream failover
- Auto-discovery of distributions, architectures, and components via HTTP probing
- Snapshot/restore system for safe repository migrations
- Per-repo distribution expansion toggle (e.g. `noble` → `noble-updates`, `noble-security`, etc.)
- GPG key URL tracking in per-repo configuration
- Stale pool orphan detection and cleanup (`--sweep-pool`)

## Installation

### Option 1: Debian/Ubuntu Package (Recommended)

Download the latest `.deb` package from [GitHub Releases](https://github.com/munger/mirror-dedupe/releases/latest):

```bash
curl -fsSL https://api.github.com/repos/munger/mirror-dedupe/releases/latest \
  | grep browser_download_url | grep all.deb | head -1 | cut -d'"' -f4 \
  | xargs wget
sudo dpkg -i mirror-dedupe_*.deb
```

This includes systemd integration, man pages, and proper package management.

### Option 2: PyPI (All Linux Distributions)

```bash
pip install mirror-dedupe
```

Then install systemd files manually:

```bash
sudo ./install.sh --pip
```

### Option 3: From Source

```bash
git clone https://github.com/munger/mirror-dedupe.git
cd mirror-dedupe
sudo ./install.sh
```

## Configuration

Configuration files are located in `/etc/mirror-dedupe/`:

- `mirror-dedupe.conf` — Global settings (repo_root, pool_root, concurrency, etc.)
- `repos-available/` — Available repository configurations
- `repos-enabled/` — Enabled repositories (symlinks to repos-available)

Pool and repository must reside on the same filesystem; hardlinks are a fundamental requirement.

### Adding a Repository

Use `--scan` to auto-generate configuration for a repository:

```bash
mirror-dedupe --scan --name grafana --out /etc/mirror-dedupe/repos-available https://apt.grafana.com
```

See `config/repos-available/README.md` for the full scan reference and
ready-made commands for all packaged example repositories.

Then test and enable it:

```bash
mirror-dedupe --test grafana
mirror-dedupe --activate grafana
```

### Advanced: Alternative config paths

Override the default config path with `--config`:

```bash
mirror-dedupe --config /tmp/mirror/mirror-dedupe.conf --test grafana
```

### Pre-configured Repositories

The package includes pre-configured repository definitions in `config/repos-available/`.

## Usage

```bash
# Sync all enabled mirrors
mirror-dedupe --sync

# Sync a specific mirror
mirror-dedupe --sync --mirror ubuntu

# Scan a repository and generate config (single release)
mirror-dedupe --scan --name ubuntu --release noble --out /etc/mirror-dedupe/repos-available http://archive.ubuntu.com/ubuntu

# Scan with all discovered architectures and distributions
mirror-dedupe --scan --name postgresql --no-filter --out /etc/mirror-dedupe/repos-available http://apt.postgresql.org/pub/repos/apt

# Scan without JSON snapshot
mirror-dedupe --scan --name grafana --out /etc/mirror-dedupe/repos-available https://apt.grafana.com

# Pool orphan sweep
mirror-dedupe --sweep-pool

# Create a hardlink snapshot of all repos
mirror-dedupe --snapshot
```

## Systemd Integration

If installed via Debian package, systemd is already configured. Otherwise:

```bash
sudo systemctl enable --now mirror-dedupe.timer
sudo systemctl status mirror-dedupe.timer
```

View logs:

```bash
journalctl -u mirror-dedupe.service
```

## Nginx Configuration

See `nginx/mirror.conf` for an example nginx configuration.

## License

MIT License — see LICENSE file for details.

## Author

Tim Hosking <tim@mungerware.com>

https://github.com/munger/mirror-dedupe
