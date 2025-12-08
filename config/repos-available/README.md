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

You can enable a repository either via the CLI or by creating the symlink manually.

**Using the CLI (recommended):**

```bash
mirror-dedupe --activate ubuntu
```

**Manual symlink:**

```bash
ln -s /etc/mirror-dedupe/repos-available/ubuntu.conf /etc/mirror-dedupe/repos-enabled/
```

### Disabling a Repository

**Using the CLI (recommended):**

```bash
mirror-dedupe --deactivate ubuntu
```

**Manual removal:**

```bash
rm /etc/mirror-dedupe/repos-enabled/ubuntu.conf
```

### Customizing

You can edit the configurations in this directory or create your own `.conf` files.

If you use `mirror-dedupe-scan` to generate a configuration for a given `--name`, it will
unconditionally overwrite `/etc/mirror-dedupe/repos-available/<name>.conf` each time you
re-run it.

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

## mirror-dedupe-scan reference

You can generate or refresh these `.conf` files using the `mirror-dedupe-scan`
helper instead of writing them by hand.

### Synopsis

```bash
mirror-dedupe-scan \
  [--name NAME] \
  [--dest DEST] \
  [--config-dir DIR] \
  [-r DIST]... \
  [-R DISTS] \
  [-a ARCH]... \
  [-A ARCHES] \
  [-c COMPONENT]... \
  [-C COMPONENTS] \
  [-G URL] \
  [-g PATH] \
  [--method METHOD] \
  UPSTREAM-URL
```

`mirror-dedupe-scan` automatically scans a Debian/Ubuntu-style repository and
prints a config suitable for `repos-available/`. The generated file is
normally written to `/etc/mirror-dedupe/repos-available/NAME.conf` and can be
enabled by adding a symlink under `repos-enabled/`.

### Common usage

```bash
# Scan the Ubuntu archive (noble)
mirror-dedupe-scan \
  --name ubuntu \
  --dest ubuntu \
  --dist noble \
  http://archive.ubuntu.com/ubuntu > /etc/mirror-dedupe/repos-available/ubuntu.conf

# Scan the Debian archive (bookworm)
mirror-dedupe-scan \
  --name debian \
  --dest debian \
  --dist bookworm \
  http://deb.debian.org/debian > /etc/mirror-dedupe/repos-available/debian.conf

# Third‑party repo with explicit key
mirror-dedupe-scan \
  --name grafana \
  --dest grafana \
  --gpg-key-url https://apt.grafana.com/gpg.key \
  --gpg-key-path gpg.key \
  https://apt.grafana.com > /etc/mirror-dedupe/repos-available/grafana.conf
```

### Options

- **`--name NAME`**  
  Repository name (required). Used for the output filename and as the default
  `dest:` when `--dest` is omitted.

- **`--dest DEST`**  
  Destination path relative to `repo_root`. Defaults to `--name`.

- **`--config-dir DIR`**  
  Configuration directory (default: `/etc/mirror-dedupe`). Only relevant when
  you let the tool write the file directly instead of redirecting stdout.

- **`-r DIST`, `--dist DIST`, `--release DIST`**  
  Add a single distribution/suite. May be specified multiple times. When any
  `--dist/--release`/`--releases` flag is used, the scanner uses exactly those
  suites (plus any special handling for `all`, see below).

- **`-R DISTS`, `--releases DISTS`**  
  Comma‑separated list of distributions/suites, e.g.

  ```bash
  --releases noble,focal
  ```

  If any item is the literal `all`, the scanner writes *all* discovered
  suites from `dists/` into `distributions:` and disables automatic
  expansion.

- **`-a ARCH`, `--arch ARCH`**  
  Add a single architecture. May be specified multiple times.

- **`-A ARCHES`, `--architectures ARCHES`**  
  Comma‑separated list of architectures. When present, this list replaces any
  architectures parsed from the Release files.

- **`-c COMPONENT`, `--component COMPONENT`**  
  Add a single component (e.g. `main`, `universe`). May be specified multiple
  times.

- **`-C COMPONENTS`, `--components COMPONENTS`**  
  Comma‑separated list of components. When present, this list replaces any
  components parsed from the Release files.

- **`-G URL`, `--gpg-key-url URL`**  
  Explicit GPG key URL. Must be used together with `-g/--gpg-key-path`. The
  scanner checks that the URL is reachable before writing the config and will
  later verify that this key can validate the repository's Release
  metadata.

- **`-g PATH`, `--gpg-key-path PATH`**  
  Relative GPG key path under `repo_root` (for example
  `keys/vendor-archive.gpg`). Must be used together with `-G`.

- **`--method METHOD`**  
  Force the sync method to `rsync` or `https`. When omitted, the tool tests a
  few rsync candidates and falls back to HTTPS.

- **`UPSTREAM-URL`**  
  The upstream repository URL to scan, such as
  `http://archive.ubuntu.com/ubuntu` or `https://apt.grafana.com`.

### Auto‑detection behaviour

`mirror-dedupe-scan` infers several fields when you do not override them:

- **Sync method** – tries rsync (daemon/module and `rsync://` forms) and
  falls back to HTTPS if none succeed, or if `rsync` is not installed.

- **Distributions** – lists the `/dists/` directory.  
  * If you pass any `--dist/--release/--releases` flags, those values are
    used directly (with special handling for `all`).
  * If you pass nothing, the tool behaves as if `--releases all` was
    specified and writes every discovered suite into `distributions:` with
    `expand_distributions: false`.

- **Architectures / components** – parsed from the `Architectures:` and
  `Components:` fields in each selected suite's `Release` file, then
  combined.

- **GPG keys** – probes a few common locations at the archive root and under
  per‑suite `Release.gpg`. When a key is found (or explicitly provided via
  `-G/-g`), the tool uses `gpg` with a temporary keyring to verify the
  `InRelease` or `Release`+`Release.gpg` for one of the selected suites.

Here are some examples you may wish to use/modify. Each command writes a
config file directly to `CONFIG_DIR/repos-available/NAME.conf` (by default
`/etc/mirror-dedupe/repos-available/NAME.conf`); no redirection is needed.

```bash
# Ubuntu (noble)
mirror-dedupe-scan \
  --name ubuntu \
  --dest ubuntu \
  --dist noble \
  http://archive.ubuntu.com/ubuntu

# Ubuntu ports (noble)
mirror-dedupe-scan \
  --name ubuntu-ports \
  --dest ubuntu-ports \
  --dist noble \
  http://ports.ubuntu.com/ubuntu-ports

# Ubuntu Cloud Archive (UCA) – OpenStack tracks
mirror-dedupe-scan \
  --name ubuntu-cloud \
  --dest ubuntu-cloud \
  --releases noble-proposed/dalamation,noble-proposed/epoxy,noble-proposed/flamingo,noble-updates/dalamation,noble-updates/epoxy,noble-updates/flamingo \
  --gpg-key-url http://archive.ubuntu.com/ubuntu/project/ubuntu-archive-keyring.gpg \
  --gpg-key-path project/ubuntu-archive-keyring.gpg \
  http://ubuntu-cloud.archive.canonical.com/ubuntu

# Debian (bookworm)
mirror-dedupe-scan \
  --name debian \
  --dest debian \
  --dist bookworm \
  --gpg-key-url https://ftp-master.debian.org/keys/archive-key-12.asc \
  --gpg-key-path archive-key-12.asc \
  http://deb.debian.org/debian

# Grafana
mirror-dedupe-scan \
  --name grafana \
  --dest grafana \
  --gpg-key-url https://apt.grafana.com/gpg.key \
  --gpg-key-path gpg.key \
  https://apt.grafana.com

# InfluxDB
mirror-dedupe-scan \
  --name influxdb \
  --dest influxdb \
  --gpg-key-url https://repos.influxdata.com/influxdata-archive_compat.key \
  --gpg-key-path influxdata-archive_compat.key \
  https://repos.influxdata.com/debian

# Docker (Ubuntu, noble)
mirror-dedupe-scan \
  --name docker \
  --dest docker \
  --dist noble \
  --gpg-key-url https://download.docker.com/linux/ubuntu/gpg \
  --gpg-key-path gpg \
  https://download.docker.com/linux/ubuntu

# PostgreSQL (noble-pgdg)
mirror-dedupe-scan \
  --name postgresql \
  --dest postgresql \
  --dist noble-pgdg \
  --gpg-key-url https://www.postgresql.org/media/keys/ACCC4CF8.asc \
  --gpg-key-path keys/ACCC4CF8.asc \
  http://apt.postgresql.org/pub/repos/apt

# MariaDB (10.11 for Ubuntu noble)
mirror-dedupe-scan \
  --name mariadb \
  --dest mariadb \
  --dist noble \
  --gpg-key-url https://mirror.mariadb.org/PublicKey \
  --gpg-key-path PublicKey \
  https://mirror.mariadb.org/repo/10.11/ubuntu

# MongoDB (Ubuntu noble)
mirror-dedupe-scan \
  --name mongodb \
  --dest mongodb \
  --dist noble \
  --gpg-key-url https://pgp.mongodb.com/server-7.0.asc \
  --gpg-key-path keys/mongodb-server-7.0.asc \
  https://repo.mongodb.org/apt/ubuntu

# Node.js 22.x (NodeSource)
mirror-dedupe-scan \
  --name nodesource-node22 \
  --dest nodesource/node_22.x \
  --gpg-key-url https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
  --gpg-key-path gpgkey/nodesource-repo.gpg.key \
  https://deb.nodesource.com/node_22.x

# VS Code
mirror-dedupe-scan \
  --name vscode \
  --dest vscode \
  --gpg-key-url https://packages.microsoft.com/keys/microsoft.asc \
  --gpg-key-path keys/microsoft.asc \
  https://packages.microsoft.com/repos/code

# GitHub CLI
mirror-dedupe-scan \
  --name github-cli \
  --dest github-cli \
  --gpg-key-url https://cli.github.com/packages/githubcli-archive-keyring.gpg \
  --gpg-key-path githubcli-archive-keyring.gpg \
  https://cli.github.com/packages

# HashiCorp
mirror-dedupe-scan \
  --name hashicorp \
  --dest hashicorp \
  --gpg-key-url https://apt.releases.hashicorp.com/gpg \
  --gpg-key-path gpg \
  https://apt.releases.hashicorp.com

# Kubernetes (apt.kubernetes.io)
mirror-dedupe-scan \
  --name kubernetes \
  --dest kubernetes \
  --gpg-key-url https://packages.cloud.google.com/apt/doc/apt-key.gpg \
  --gpg-key-path keys/kubernetes-apt-key.gpg \
  https://apt.kubernetes.io

# Google Cloud SDK
mirror-dedupe-scan \
  --name google-cloud-sdk \
  --dest google-cloud-sdk \
  --gpg-key-url https://packages.cloud.google.com/apt/doc/apt-key.gpg \
  --gpg-key-path doc/apt-key.gpg \
  https://packages.cloud.google.com/apt

# Azure CLI
mirror-dedupe-scan \
  --name azure-cli \
  --dest azure-cli \
  --gpg-key-url https://packages.microsoft.com/keys/microsoft.asc \
  --gpg-key-path keys/microsoft.asc \
  https://packages.microsoft.com/repos/azure-cli/

# Ceph (reef, Debian-based)
mirror-dedupe-scan \
  --name ceph-reef \
  --dest ceph/reef \
  --gpg-key-url https://download.ceph.com/keys/release.asc \
  --gpg-key-path keys/release.asc \
  https://download.ceph.com/debian-reef/

# GitLab CE
mirror-dedupe-scan \
  --name gitlab-ce \
  --dest gitlab-ce \
  --gpg-key-url https://packages.gitlab.com/gitlab/gitlab-ce/gpgkey \
  --gpg-key-path gitlab/gitlab-ce/gpgkey \
  https://packages.gitlab.com/gitlab/gitlab-ce/debian/

# GitLab EE
mirror-dedupe-scan \
  --name gitlab-ee \
  --dest gitlab-ee \
  --gpg-key-url https://packages.gitlab.com/gitlab/gitlab-ee/gpgkey \
  --gpg-key-path gitlab/gitlab-ee/gpgkey \
  https://packages.gitlab.com/gitlab/gitlab-ee/debian/

# Amazon Corretto (OpenJDK)
mirror-dedupe-scan \
  --name corretto \
  --dest corretto \
  --gpg-key-url https://apt.corretto.aws/corretto.key \
  --gpg-key-path corretto.key \
  https://apt.corretto.aws

# Brave browser
mirror-dedupe-scan \
  --name brave \
  --dest brave \
  --gpg-key-url https://brave-browser-apt-release.s3.brave.com/brave-browser-archive-keyring.gpg \
  --gpg-key-path brave-browser-archive-keyring.gpg \
  https://brave-browser-apt-release.s3.brave.com

# NGINX official (Ubuntu, noble)
mirror-dedupe-scan \
  --name nginx \
  --dest nginx \
  --dist noble \
  --gpg-key-url https://nginx.org/keys/nginx_signing.key \
  --gpg-key-path keys/nginx_signing.key \
  https://nginx.org/packages/ubuntu

# Signal Desktop
mirror-dedupe-scan \
  --name signal-desktop \
  --dest signal-desktop \
  --gpg-key-url https://updates.signal.org/desktop/apt/keys.asc \
  --gpg-key-path keys.asc \
  https://updates.signal.org/desktop/apt

# Dovecot
mirror-dedupe-scan \
  --name dovecot \
  --dest dovecot \
  --gpg-key-url https://repo.dovecot.org/DOVECOT-REPO-GPG \
  --gpg-key-path DOVECOT-REPO-GPG \
  https://repo.dovecot.org/ce-2.3-latest/ubuntu

# Rspamd (noble)
mirror-dedupe-scan \
  --name rspamd \
  --dest rspamd \
  --dist noble \
  --gpg-key-url https://rspamd.com/apt-stable/gpg.key \
  --gpg-key-path gpg.key \
  https://rspamd.com/apt-stable/

# Zabbix 6.4 (Ubuntu)
mirror-dedupe-scan \
  --name zabbix \
  --dest zabbix \
  --gpg-key-url https://repo.zabbix.com/zabbix-official-repo.key \
  --gpg-key-path zabbix-official-repo.key \
  https://repo.zabbix.com/zabbix/6.4/ubuntu/

# VirtualBox (Ubuntu noble)
mirror-dedupe-scan \
  --name virtualbox \
  --dest virtualbox \
  --dist noble \
  --gpg-key-url https://www.virtualbox.org/download/oracle_vbox_2016.asc \
  --gpg-key-path oracle_vbox_2016.asc \
  https://download.virtualbox.org/virtualbox/debian

# Google Chrome
mirror-dedupe-scan \
  --name google-chrome \
  --dest google-chrome \
  --gpg-key-url https://dl.google.com/linux/linux_signing_key.pub \
  --gpg-key-path linux/linux_signing_key.pub \
  https://dl.google.com/linux/chrome/deb/

# Skype
mirror-dedupe-scan \
  --name skype \
  --dest skype \
  --gpg-key-url https://repo.skype.com/data/SKYPE-GPG-KEY \
  --gpg-key-path data/SKYPE-GPG-KEY \
  https://repo.skype.com/deb

# Slack
mirror-dedupe-scan \
  --name slack \
  --dest slack \
  --gpg-key-url https://packagecloud.io/slacktechnologies/slack/gpgkey \
  --gpg-key-path slacktechnologies/slack/gpgkey \
  https://packagecloud.io/slacktechnologies/slack/debian/

# TeamViewer
mirror-dedupe-scan \
  --name teamviewer \
  --dest teamviewer \
  https://linux.teamviewer.com/deb
```

