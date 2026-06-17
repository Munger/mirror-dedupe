# Repository Configurations

Pre-configured repository definitions for `mirror-dedupe`.

## Enabling / Disabling

Enable via CLI (recommended) or manual symlink:

```bash
mirror-dedupe --activate <name>
# or
ln -s /etc/mirror-dedupe/repos-available/<name>.conf /etc/mirror-dedupe/repos-enabled/
```

Disable:

```bash
mirror-dedupe --deactivate <name>
# or
rm /etc/mirror-dedupe/repos-enabled/<name>.conf
```

## Config Format

```yaml
name: <name>
dest: <relative/path/from/repo_root>
repo_type: apt
upstreams:
  - url: https://upstream.example.com/repo
gpg_key_url: https://upstream.example.com/gpg.key
architectures:
  - amd64
components:
  - main
distributions:
  - noble
expand_distributions: false

params:
  discovery_method: html_bfs
  log_colour: DEFAULT
  log_colour_bg: NONE
  # parallel_downloads: N
```

### Distribution Expansion

The `expand_distributions` setting controls whether a base distribution is
automatically expanded into its pocket variants during sync.

When `true` (the default if the field is absent), a single entry like
`noble` becomes five suites at sync time: `noble`, `noble-updates`,
`noble-security`, `noble-backports`, and `noble-proposed`.  Expansion
only applies to distributions that do not already contain a hyphen
(e.g. `noble-pgdg` is left untouched).

By default, `mirror-dedupe --scan` sets `expand_distributions: false` and
writes every discovered suite explicitly.  To enable automatic expansion:

```yaml
expand_distributions: true
```

## Generating Configs

Use `mirror-dedupe --scan` to probe a repository and write a config file:

```bash
# Ubuntu main archive (noble)
mirror-dedupe --scan \
  --name ubuntu \
  --dest ubuntu \
  --release noble \
  --out /etc/mirror-dedupe/repos-available \
  http://archive.ubuntu.com/ubuntu

# Your own APT repo with GPG key
mirror-dedupe --scan \
  --name mungerware \
  --dest mungerware \
  --gpg-key-url https://apt.mungerware.com/key.asc \
  --out /etc/mirror-dedupe/repos-available \
  https://apt.mungerware.com/
```

See `mirror-dedupe --help` for all scan options (`-U`, `-r`/`-R`, `--arch`,
`--component`, `--repo-type`, `--collapse-dists`, etc.).

The generated config overwrites `repos-available/<name>.conf` on each run.
Enable it with `--activate <name>` to add it to the sync rotation.
