<!--
README.md : Ubuntu mirror synchronisation with global deduplication

Copyright (c) 2025 Tim Hosking
Email: tim@mungerware.com
Website: https://github.com/munger
Licence: MIT
-->

Place your repository configuration files here. Each file should be named `<name>.conf`.

## Example

Copy `ubuntu.conf.example` to `ubuntu.conf` and customise:

```bash
cp ubuntu.conf.example ubuntu.conf
```

Then edit `ubuntu.conf` to match your requirements:
- Change `upstream` URL if needed
- Adjust `dest` path (relative to `repo_root`)
- Select architectures to mirror
- Choose components
- Specify distributions

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
