<!--
README.md : Ubuntu mirror synchronisation with global deduplication

Copyright (c) 2025 Tim Hosking
Email: tim@mungerware.com
Website: https://github.com/munger
Licence: MIT
-->

## Available Repository Configurations

This directory contains pre-configured repository definitions.

### Enabling a Repository

Create a symlink in `/etc/mirror-dedupe/repos-enabled/`:

```bash
ln -s /etc/mirror-dedupe/repos-available/ubuntu.conf /etc/mirror-dedupe/repos-enabled/
```

### Disabling a Repository

Remove the symlink:

```bash
rm /etc/mirror-dedupe/repos-enabled/ubuntu.conf
```

### Customizing

You can edit the configurations in this directory or create your own `.conf` files.

## Configuration Format

Each repository config file should contain:

```yaml
name: repository-name
upstream: http://upstream.example.com/repo
dest: relative/path/from/repo_root
sync_method: rsync  # or https
gpg_key_url: http://upstream.example.com/gpg.key
gpg_key_path: relative/path/to/key
architectures:
  - amd64
  - arm64
components:
  - main
  - restricted
distributions:
  - noble
```

## Sync Methods

- **rsync**: Uses rsync protocol (faster, recommended for official Ubuntu mirrors)
- **https**: Uses HTTPS with curl (for repositories that don't support rsync)

## Distribution Expansion

By default, distributions are expanded to include variants:
- `noble` → `noble`, `noble-updates`, `noble-security`, `noble-backports`, `noble-proposed`

To disable expansion, add:
```yaml
expand_distributions: false
```
