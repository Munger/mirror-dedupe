#!/usr/bin/env python3
"""Quick smoke test for exclude_packages / exclude_paths in AptIndex.stream()"""

import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

from mirror_dedupe.repos.apt.index import AptIndex

# Simulated Packages index content (two stanzas)
PACKAGES_DATA = b"""Package: foo
Version: 1.0
Filename: pool/main/f/foo_1.0_amd64.deb
SHA256: abc123
Size: 1024

Package: foo-nightly
Version: 1.1~dev
Filename: pool/main/f/foo-nightly_1.1_amd64.deb
SHA256: def456
Size: 2048

Package: bar
Version: 2.0
Filename: pool/main/b/bar_2.0_amd64.deb
SHA256: ghi789
Size: 4096
"""


def _make_index(**kwargs):
    return AptIndex(path="test/Packages", **kwargs)


def test_no_filter():
    idx = _make_index(dest="test", kind="packages")
    pkgs = list(idx.stream(data=PACKAGES_DATA))
    names = [p.get("path", "").split("/")[-1] for p in pkgs]
    assert len(pkgs) == 3, f"Expected 3, got {len(pkgs)}: {names}"
    print("PASS: no filter — 3 packages")


def test_exclude_packages():
    idx = _make_index(dest="test", kind="packages")
    idx._exclude_packages = ["*nightly*"]
    pkgs = list(idx.stream(data=PACKAGES_DATA))
    names = [p.get("path", "").split("/")[-1] for p in pkgs]
    assert len(pkgs) == 2, f"Expected 2, got {len(pkgs)}: {names}"
    assert all("nightly" not in n for n in names), f"Nightly still present: {names}"
    print("PASS: exclude_packages — 2 packages, no nightly")


def test_exclude_paths():
    idx = _make_index(dest="test", kind="packages")
    idx._exclude_paths = ["*/pool/main/b/*"]
    pkgs = list(idx.stream(data=PACKAGES_DATA))
    paths = [p.get("path", "") for p in pkgs]
    assert len(pkgs) == 2, f"Expected 2, got {len(pkgs)}: {paths}"
    assert all("/b/" not in p for p in paths), f"bar still present: {paths}"
    print("PASS: exclude_paths — 2 packages, no bar")


def test_exclude_both():
    idx = _make_index(dest="test", kind="packages")
    idx._exclude_packages = ["*nightly*"]
    idx._exclude_paths = ["*/pool/main/b/*"]
    pkgs = list(idx.stream(data=PACKAGES_DATA))
    names = [p.get("path", "").split("/")[-1] for p in pkgs]
    assert len(pkgs) == 1, f"Expected 1, got {len(pkgs)}: {names}"
    assert names[0] == "foo_1.0_amd64.deb", f"Unexpected: {names}"
    print("PASS: exclude both — 1 package (foo only)")


if __name__ == "__main__":
    test_no_filter()
    test_exclude_packages()
    test_exclude_paths()
    test_exclude_both()
    print("\nAll tests passed.")
