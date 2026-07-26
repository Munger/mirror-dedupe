# Repository Configurations

Pre-configured repository definitions for `mirror-dedupe`.

## Enabling / Disabling

Enable via CLI (recommended) or manual symlink:

```bash
mirror-dedupe activate <name>
# or
ln -s /etc/mirror-dedupe/repos-available/<name>.conf /etc/mirror-dedupe/repos-enabled/
```

Disable:

```bash
mirror-dedupe deactivate <name>
# or
rm /etc/mirror-dedupe/repos-enabled/<name>.conf
```

## Config Format

```yaml
name: <name>
dest: <relative/path/from/mirror_root/repos>
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
  # anchor_filename: InRelease           # override the default "Release" anchor per repo
  # suite_anchor_exceptions:             # per-suite anchor overrides for mixed InRelease/Release repos
  #   bionic/mongodb-org/4.0: Release
  # exclude_packages:                    # skip packages matching glob patterns
  #   - "*nightly*"
  # exclude_paths:                       # skip files matching glob patterns
  #   - "*/debug/*"
```

### Distribution Expansion

The `expand_distributions` setting controls whether a base distribution is
automatically expanded into its pocket variants during sync.

When `true` (the default if the field is absent), a single entry like
`noble` becomes five suites at sync time: `noble`, `noble-updates`,
`noble-security`, `noble-backports`, and `noble-proposed`.  Expansion
only applies to distributions that do not already contain a hyphen
(e.g. `noble-pgdg` is left untouched).

By default, `mirror-dedupe scan` omits this field and leaves expansion at
its default (`true`) when it discovers a single base distribution (e.g.
`noble`). It only writes `expand_distributions: false` explicitly when
scanning every discovered suite without collapsing (`--no-collapse-dists`)
or when the sole distribution is `stable`. To disable automatic expansion
for a repo whose distributions already contain hyphens or don't map to
Ubuntu-style pockets (e.g. `ubuntu-cloud`'s `noble-proposed/dalmatian`):

```yaml
expand_distributions: false
```

### Anchor File Overrides

Most APT repos publish a `Release` file per suite as the anchor for
signature verification and index discovery. Some repos mix `InRelease`
and `Release` across suites, or use `InRelease` exclusively. Two
`params` control this:

```yaml
params:
  anchor_filename: InRelease            # default anchor for all suites in this repo
  suite_anchor_exceptions:              # per-suite overrides where the default doesn't apply
    bionic/mongodb-org/4.0: Release
```

### Package Filtering

Use `exclude_packages` and `exclude_paths` in the `params` block to skip
packages during sync.  Patterns use Python `fnmatch` syntax (shell-style
wildcards):

```yaml
params:
  exclude_packages:
    - "*nightly*"           # skip any package with "nightly" in its name
    - "*-dbg"               # skip debug symbol packages
  exclude_paths:
    - "*/debug/*"           # skip files under debug directories
```

Filtering is applied per-stanza when parsing `Packages`/`Sources` indices,
so excluded packages never enter the download pipeline.

## Generating Configs

Use `mirror-dedupe scan` to probe a repository and write a config file:

```bash
# Ubuntu main archive (noble)
mirror-dedupe scan \
  --name ubuntu \
  --dest ubuntu \
  --distribution noble \
  --out /etc/mirror-dedupe/repos-available \
  http://archive.ubuntu.com/ubuntu

# Your own APT repo with GPG key
mirror-dedupe scan \
  --name mungerware \
  --dest mungerware \
  --gpg-key-url https://apt.mungerware.com/key.asc \
  --out /etc/mirror-dedupe/repos-available \
  https://apt.mungerware.com/
```

See `mirror-dedupe scan --help` for all scan options (`-U`, `-d`/`-D`,
`--arch`, `--component`, `--repo-type`, `--collapse-dists`, etc.).

Live scans reflect exactly what the upstream currently serves — including
any inconsistent or typo'd suite names on the upstream's side. Always diff
scan output against the existing config before overwriting it; don't
promote blindly, especially for repos with large, hand-curated
distribution lists (e.g. `ubuntu-cloud`, `postgresql`) where a scan may
discover fewer suites/architectures than what's already configured.

The generated config overwrites `repos-available/<name>.conf` on each run.
Enable it with `mirror-dedupe activate <name>` to add it to the sync rotation.
