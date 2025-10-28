<!--
README.md : Ubuntu mirror synchronisation with global deduplication

Copyright (c) 2025 Tim Hosking
Email: tim@mungerware.com
Website: https://github.com/munger
Licence: MIT
-->

Ubuntu/Debian mirror synchronisation with intelligent deduplication using hardlinks.

## Features

- Downloads from upstream and hardlinks duplicate files (same SHA256 hash) to save bandwidth and disk space
- Supports multiple mirrors with global deduplication across all mirrors
- Uses curl for duplicate files, rsync for unique files and metadata
- Configurable via YAML files
- Systemd timer support for automated synchronisation

## Installation

### Option 1: Debian/Ubuntu Package (Recommended)

Download the latest `.deb` package from [GitHub Releases](https://github.com/munger/mirror-dedupe/releases):

```bash
wget https://github.com/munger/mirror-dedupe/releases/download/v0.1.0/mirror-dedupe_0.1.0-1_all.deb
sudo dpkg -i mirror-dedupe_0.1.0-1_all.deb
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

- `mirror-dedupe.conf` - Global settings
- `repos.d/*.conf` - Individual repository definitions

See the `config/` directory for examples.

## Usage

```bash
# Sync all mirrors
mirror-dedupe

# Sync specific mirror
mirror-dedupe --mirror ubuntu

# Dry run
mirror-dedupe --dry-run

# Dedupe only (no sync)
mirror-dedupe --dedupe-only
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

MIT License - see LICENSE file for details.

## Author

Tim Hosking <tim@mungerware.com>

https://github.com/munger/mirror-dedupe
