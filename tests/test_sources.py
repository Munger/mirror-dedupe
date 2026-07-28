#!/usr/bin/env python3
"""Tests for Sources stanza parsing in AptIndex.stream()"""

import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

from mirror_dedupe.repos.apt.index import AptIndex

SOURCES_DATA = b"""Package: aalib
Format: 3.0 (quilt)
Binary: libaa1-dev, libaa1, libaa-bin
Architecture: any
Version: 1.4p5-51.1
Priority: optional
Section: libs
Maintainer: Jonathan Carter <jcc@debian.org>
Homepage: http://aa-project.sourceforge.net/aalib/
Directory: pool/main/a/aalib
Checksums-Sha256:
 f4c63901ad54b05d088a2bf7e637f7c032330b0b96a71272954afd811300dbbe 2020 aalib_1.4p5-51.1.dsc
 fbddda9230cf6ee2a4f5706b4b11e2190ae45f5eda1f0409dc4f99b35e0a70ee 391028 aalib_1.4p5.orig.tar.gz
 15ca4a195c02f23c11bc6b6d372c07ed16a8f76fe306a2e4123bf5f939f70332 17368 aalib_1.4p5-51.1.debian.tar.xz

Package: acct
Format: 3.0 (native)
Binary: acct
Architecture: any
Version: 6.6.4-5build1
Directory: pool/main/a/acct
Checksums-Sha256:
 a1b2c3d4e5f6 1234 acct_6.6.4-5build1.dsc
 b2c3d4e5f6a7 567890 acct_6.6.4.orig.tar.gz
 c3d4e5f6a7b8 9012 acct_6.6.4-5build1.debian.tar.xz
 d4e5f6a7b8c9 456 acct_6.6.4-5build1.orig.tar.gz.asc
"""


def _make_index(**kwargs):
    return AptIndex(path="test/Sources", **kwargs)


def test_sources_stanza():
    """Basic Sources stanza parsing: Directory + Checksums-Sha256 block."""
    idx = _make_index(dest="test", kind="sources")
    pkgs = list(idx.stream(data=SOURCES_DATA))
    assert len(pkgs) == 7, f"Expected 7 files (3+4), got {len(pkgs)}: {[p.get('path') for p in pkgs]}"
    paths = [p.get("path", "") for p in pkgs]
    assert "test/pool/main/a/aalib/aalib_1.4p5-51.1.dsc" in paths
    assert "test/pool/main/a/aalib/aalib_1.4p5.orig.tar.gz" in paths
    assert "test/pool/main/a/aalib/aalib_1.4p5-51.1.debian.tar.xz" in paths
    assert "test/pool/main/a/acct/acct_6.6.4-5build1.dsc" in paths
    assert "test/pool/main/a/acct/acct_6.6.4.orig.tar.gz" in paths
    assert "test/pool/main/a/acct/acct_6.6.4-5build1.debian.tar.xz" in paths
    assert "test/pool/main/a/acct/acct_6.6.4-5build1.orig.tar.gz.asc" in paths
    print("PASS: sources_stanza — 7 source files from 2 packages")


def test_sources_checksums():
    """Verify SHA256 hashes are correctly extracted from Sources stanza."""
    idx = _make_index(dest="test", kind="sources")
    pkgs = list(idx.stream(data=SOURCES_DATA))
    dsc = [p for p in pkgs if p.get("path", "").endswith(".dsc")][0]
    assert dsc.get("hash") == "f4c63901ad54b05d088a2bf7e637f7c032330b0b96a71272954afd811300dbbe"
    assert dsc.get("size") == 2020
    print("PASS: sources_checksums — hash and size correct")


def test_sources_sizes():
    """Verify file sizes are correctly parsed from Sources stanza."""
    idx = _make_index(dest="test", kind="sources")
    pkgs = list(idx.stream(data=SOURCES_DATA))
    sizes = {p.get("path", "").rsplit("/", 1)[-1]: p.get("size") for p in pkgs}
    assert sizes["aalib_1.4p5-51.1.dsc"] == 2020
    assert sizes["aalib_1.4p5.orig.tar.gz"] == 391028
    assert sizes["aalib_1.4p5-51.1.debian.tar.xz"] == 17368
    print("PASS: sources_sizes — all sizes correct")


def test_sources_exclude_packages():
    """Exclusion works for Sources packages."""
    idx = _make_index(dest="test", kind="sources")
    idx._exclude_packages = ["aalib"]
    pkgs = list(idx.stream(data=SOURCES_DATA))
    paths = [p.get("path", "") for p in pkgs]
    assert not any("aalib" in p for p in paths), f"aalib still present: {paths}"
    assert len(pkgs) == 4, f"Expected 4 (acct only), got {len(pkgs)}: {paths}"
    print("PASS: sources_exclude_packages — aalib excluded, 4 acct files remain")


def test_sources_exclude_paths():
    """Path exclusion works for Sources packages."""
    idx = _make_index(dest="test", kind="sources")
    idx._exclude_paths = ["*/aalib/*"]
    pkgs = list(idx.stream(data=SOURCES_DATA))
    paths = [p.get("path", "") for p in pkgs]
    assert not any("aalib" in p for p in paths), f"aalib still present: {paths}"
    assert len(pkgs) == 4, f"Expected 4 (acct only), got {len(pkgs)}: {paths}"
    print("PASS: sources_exclude_paths — aalib excluded by path")


def test_sources_no_dest():
    """Sources parsing works with empty dest prefix."""
    idx = _make_index(dest="", kind="sources")
    pkgs = list(idx.stream(data=SOURCES_DATA))
    paths = [p.get("path", "") for p in pkgs]
    assert "pool/main/a/aalib/aalib_1.4p5-51.1.dsc" in paths
    assert not any(p.startswith("test/") for p in paths)
    print("PASS: sources_no_dest — paths are relative (no dest prefix)")


def test_sources_uri():
    """URI reconstruction works for Sources packages."""
    idx = _make_index(dest="test", kind="sources")
    idx["uri"] = "https://example.com/ubuntu/dists/noble/main/source/Sources.xz"
    pkgs = list(idx.stream(data=SOURCES_DATA))
    dsc = [p for p in pkgs if p.get("path", "").endswith(".dsc")][0]
    uri = dsc.get("uri", "")
    assert "https://example.com/ubuntu" in uri
    assert "aalib_1.4p5-51.1.dsc" in uri
    print(f"PASS: sources_uri — URI correct: {uri}")


def test_mixed_stanza_types():
    """Binary stanzas still work alongside Sources stanzas."""
    mixed_data = b"""Package: aalib
Format: 3.0 (quilt)
Architecture: any
Version: 1.4p5-51.1
Directory: pool/main/a/aalib
Checksums-Sha256:
 abc123 2020 aalib_1.4p5-51.1.dsc
 def456 391028 aalib_1.4p5.orig.tar.gz
 ghi789 17368 aalib_1.4p5-51.1.debian.tar.xz

Package: foo
Version: 1.0
Filename: pool/main/f/foo_1.0_amd64.deb
SHA256: aaa111
Size: 1024
"""
    idx = _make_index(dest="test", kind="packages")
    pkgs = list(idx.stream(data=mixed_data))
    paths = [p.get("path", "") for p in pkgs]
    # Sources stanza: 3 files via Directory/Checksums-Sha256
    assert "test/pool/main/a/aalib/aalib_1.4p5-51.1.dsc" in paths
    assert "test/pool/main/a/aalib/aalib_1.4p5.orig.tar.gz" in paths
    assert "test/pool/main/a/aalib/aalib_1.4p5-51.1.debian.tar.xz" in paths
    # Binary stanza: 1 file via Filename/SHA256/Size
    assert "test/pool/main/f/foo_1.0_amd64.deb" in paths
    assert len(pkgs) == 4, f"Expected 4, got {len(pkgs)}: {paths}"
    print("PASS: mixed_stanza_types — Sources + binary stanzas parsed correctly")


if __name__ == "__main__":
    test_sources_stanza()
    test_sources_checksums()
    test_sources_sizes()
    test_sources_exclude_packages()
    test_sources_exclude_paths()
    test_sources_no_dest()
    test_sources_uri()
    test_mixed_stanza_types()
    print("\nAll tests passed.")
