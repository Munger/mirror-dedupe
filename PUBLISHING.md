<!--
PUBLISHING.md : Ubuntu mirror synchronisation with global deduplication

Copyright (c) 2025 Tim Hosking
Email: tim@mungerware.com
Website: https://github.com/munger
Licence: MIT
-->

1. Create account on https://pypi.org
2. Generate API token at https://pypi.org/manage/account/token/
3. Add token to GitHub secrets as `PYPI_API_TOKEN`

### Manual Publishing

```bash
# Install build tools
pip install build twine

# Build package
python -m build

# Upload to PyPI
twine upload dist/*
```

### Automatic Publishing

The GitHub Actions workflow will automatically publish to PyPI when you create a release.

## Creating a Release

1. Update version in `setup.py` and `pyproject.toml`
2. Update `debian/changelog`:
   ```bash
   dch -v 0.1.1-1 "New release"
   ```
3. Commit changes
4. Create and push tag:
   ```bash
   git tag -a v0.1.1 -m "Release v0.1.1"
   git push origin v0.1.1
   ```
5. Create GitHub release from tag
6. GitHub Actions will:
   - Build and publish to PyPI
   - Build `.deb` package
   - Attach `.deb` to release

## Manual Debian Package Build

```bash
dpkg-buildpackage -us -uc -b
```

The package will be created in the parent directory.

## Installation Methods for Users

### Debian/Ubuntu
```bash
wget https://github.com/munger/mirror-dedupe/releases/download/v0.1.0/mirror-dedupe_0.1.0-1_all.deb
sudo dpkg -i mirror-dedupe_0.1.0-1_all.deb
```

### PyPI (any Linux)
```bash
pip install mirror-dedupe
sudo ./install.sh --pip
```

### From Source
```bash
git clone https://github.com/munger/mirror-dedupe.git
cd mirror-dedupe
sudo ./install.sh
```
