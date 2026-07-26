<!--
PUBLISHING.md : Ubuntu mirror synchronisation with global deduplication

Copyright (c) 2025 Tim Hosking
Email: tim@mungerware.com
Website: https://github.com/munger
Licence: MIT
-->

## Release & Deployment Pipeline

```
git tag vX.Y.Z
    │
    ▼
GitHub Release (create from tag)
    │
    ├── CI: build-deb.yml     → .deb attached to release
    ├── CI: publish-pypi.yml  → PyPI upload
    ├── CI: deploy-apt.yml    → .deb pushed to Munger/packages
    │
    ▼
apt.mungerware.com (auto-published via packages repo CI)
```

### Version Locations

All must match before tagging. `debian/changelog` is the source of truth.

| File | How version is set |
|---|---|
| `debian/changelog` | `dch -v X.Y.Z-1 "Release message"` |
| `setup.py` | Reads from `debian/changelog` via regex |
| `pyproject.toml` | `setuptools_scm` reads from git tags |
| `mirror_dedupe/__init__.py` | Static `__version__` string |
| `debian/mirror-dedupe.1` | Static troff header |
| `debian/mirror-dedupe-scan.1` | Static troff header |

### Creating a Release

```bash
# 1. Bump version in all locations (see table above)
dch -v X.Y.Z-1 "Release message"
# Also update: __init__.py, man pages, pyproject.toml, setup.py

# 2. Commit
git add -A && git commit -m "Release vX.Y.Z"

# 3. Tag and push
git tag -a vX.Y.Z -m "Release vX.Y.Z"
git push origin vX.Y.Z main

# 4. Create GitHub Release (UI or gh CLI)
gh release create vX.Y.Z --title "Release vX.Y.Z" --generate-notes
```

### CI Triggers Automatically

1. **build-deb.yml** — builds `.deb`, attaches to release
2. **publish-pypi.yml** — publishes to PyPI
3. **deploy-apt.yml** — pushes `.deb` to `Munger/packages` repo

### What Each CI Does

| Workflow | Trigger | Output |
|---|---|---|
| `build-deb.yml` | Release published | `.deb` attached to GitHub Release |
| `publish-pypi.yml` | Release published | Package on PyPI |
| `deploy-apt.yml` | Release published | `.deb` added to `Munger/packages/pool/` |

After `deploy-apt.yml` pushes, the `packages` repo's own CI (`publish.yml`)
regenerates apt metadata and deploys to `apt.mungerware.com` via GitHub Pages.

### Required Secrets

| Secret | Purpose |
|---|---|
| `PYPI_API_TOKEN` | PyPI publishing |
| `PACKAGES_REPO_TOKEN` | PAT with push access to `Munger/packages` |

## Manual Debian Package Build

```bash
dpkg-buildpackage -us -uc -b
```

The package will be created in the parent directory.

## Installation Methods for Users

### Debian/Ubuntu (from apt repo)
```bash
sudo mkdir -p /etc/apt/keyrings
curl -fsSL https://apt.mungerware.com/key.asc | sudo gpg --dearmor -o /etc/apt/keyrings/mungerware.gpg
echo "deb [signed-by=/etc/apt/keyrings/mungerware.gpg] https://apt.mungerware.com noble main" | sudo tee /etc/apt/sources.list.d/mungerware.list
sudo apt update && sudo apt install mirror-dedupe
```

### Debian/Ubuntu (from GitHub Release)
```bash
wget https://github.com/Munger/mirror-dedupe/releases/latest/download/mirror-dedupe_latest_all.deb
sudo dpkg -i mirror-dedupe_latest_all.deb
```

### PyPI (any Linux)
```bash
pip install mirror-dedupe
```
