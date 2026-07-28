#!/usr/bin/env python3
"""Tests for i18n/Translation-* and cnf/Commands-* index support in Release parsing."""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from mirror_dedupe.repos.apt.index import AptIndex
from mirror_dedupe.repos.apt.release import Release
from mirror_dedupe.schema import VariantIndex


TRANSLATION_DATA = b"""Package: foo
Description-md5: abc123
Description-en: A test package

Package: bar
Description-md5: def456
Description-en: Another test package
"""


def test_translations_stream_empty():
    """AptIndex.stream() returns empty iterator for kind 'translations'."""
    idx = AptIndex(path="test/main/i18n/Translation-en.xz", dest="test", kind="translations")
    pkgs = list(idx.stream(data=TRANSLATION_DATA))
    assert len(pkgs) == 0, f"Expected 0 packages, got {len(pkgs)}"
    print("PASS: translations_stream_empty — no packages yielded from Translation index")


def test_commands_stream_empty():
    """AptIndex.stream() returns empty iterator for kind 'commands'."""
    idx = AptIndex(path="test/main/cnf/Commands-amd64.xz", dest="test", kind="commands")
    pkgs = list(idx.stream(data=b"foo\tbar\n"))
    assert len(pkgs) == 0, f"Expected 0 packages, got {len(pkgs)}"
    print("PASS: commands_stream_empty — no packages yielded from Commands index")


RELEASE_BODY = """SHA256:
 aaaaa 1000 main/binary-amd64/Packages.xz
 bbbbb 500 main/binary-amd64/Translation-en.xz
 ccccc 200 main/cnf/Commands-amd64.xz
 ddddd 300 main/source/Sources.xz
 eeeee 600 main/binary-arm64/Packages.xz
 fffff 400 main/binary-arm64/Translation-fr.xz
 ggggg 150 main/cnf/Commands-arm64.xz
"""


def test_release_yields_translations():
    """Release.stream() yields Index nodes with kind 'translations' for Translation-* entries."""
    rel = Release(
        url="https://example.com/ubuntu/dists/noble/Release",
        upstream="https://example.com/ubuntu",
        suite="noble",
        dest="test",
    )
    indices = list(rel.stream(data=RELEASE_BODY.encode()))
    trans_indices = [i for i in indices if i.get("kind") == "translations"]
    assert len(trans_indices) == 2, f"Expected 2 Translation indices, got {len(trans_indices)}: {[i.get('path') for i in trans_indices]}"
    paths = {i.get("path") for i in trans_indices}
    assert "test/dists/noble/main/binary-amd64/Translation-en.xz" in paths, f"Missing Translation-en: {paths}"
    assert "test/dists/noble/main/binary-arm64/Translation-fr.xz" in paths, f"Missing Translation-fr: {paths}"
    print("PASS: release_yields_translations — 2 Translation indices created")


def test_release_yields_commands():
    """Release.stream() yields Index nodes with kind 'commands' for Commands-* entries."""
    rel = Release(
        url="https://example.com/ubuntu/dists/noble/Release",
        upstream="https://example.com/ubuntu",
        suite="noble",
        dest="test",
    )
    indices = list(rel.stream(data=RELEASE_BODY.encode()))
    cmd_indices = [i for i in indices if i.get("kind") == "commands"]
    assert len(cmd_indices) == 2, f"Expected 2 Commands indices, got {len(cmd_indices)}: {[i.get('path') for i in cmd_indices]}"
    paths = {i.get("path") for i in cmd_indices}
    assert "test/dists/noble/main/cnf/Commands-amd64.xz" in paths, f"Missing Commands-amd64: {paths}"
    assert "test/dists/noble/main/cnf/Commands-arm64.xz" in paths, f"Missing Commands-arm64: {paths}"
    print("PASS: release_yields_commands — 2 Commands indices created")


def test_release_preserves_packages_and_sources():
    """Release.stream() still yields Packages and Sources alongside new types."""
    rel = Release(
        url="https://example.com/ubuntu/dists/noble/Release",
        upstream="https://example.com/ubuntu",
        suite="noble",
        dest="test",
    )
    indices = list(rel.stream(data=RELEASE_BODY.encode()))
    pkg_indices = [i for i in indices if i.get("kind") == "packages"]
    src_indices = [i for i in indices if i.get("kind") == "sources"]
    assert len(pkg_indices) == 2, f"Expected 2 Packages, got {len(pkg_indices)}"
    assert len(src_indices) == 1, f"Expected 1 Sources, got {len(src_indices)}"
    print("PASS: release_preserves_packages_and_sources — Packages and Sources unchanged")


def test_release_arch_filter_packages_only():
    """Arch filter only applies to Packages/Sources, not Translation/Commands."""
    rel = Release(
        url="https://example.com/ubuntu/dists/noble/Release",
        upstream="https://example.com/ubuntu",
        suite="noble",
        dest="test",
    )
    rel._arch_filter = ["amd64"]
    indices = list(rel.stream(data=RELEASE_BODY.encode()))
    pkg = [i for i in indices if i.get("kind") == "packages"]
    cmd = [i for i in indices if i.get("kind") == "commands"]
    tran = [i for i in indices if i.get("kind") == "translations"]
    # Packages should be filtered to amd64 only
    assert len(pkg) == 1, f"Expected 1 Packages, got {len(pkg)}: {[i.get('path') for i in pkg]}"
    # Translation/Commands should NOT be filtered by arch
    assert len(cmd) == 2, f"Expected 2 Commands, got {len(cmd)}: {[i.get('path') for i in cmd]}"
    assert len(tran) == 2, f"Expected 2 Translations, got {len(tran)}: {[i.get('path') for i in tran]}"
    print("PASS: release_arch_filter_packages_only — arch filter skips Translation/Commands")


RELEASE_WITH_VARIANTS = """SHA256:
 aaaaa 1000 main/binary-amd64/Packages.xz
 bbbbb 2000 main/binary-amd64/Packages
 ccccc 3000 main/binary-amd64/Packages.gz
 ddddd 500 main/binary-amd64/Translation-en.xz
 eeeee 600 main/binary-amd64/Translation-en.gz
"""


def test_variant_index_has_optional():
    """VariantIndex nodes are marked optional=True for tolerant 404 handling."""
    rel = Release(
        url="https://example.com/ubuntu/dists/noble/Release",
        upstream="https://example.com/ubuntu",
        suite="noble",
        dest="test",
    )
    indices = list(rel.stream(data=RELEASE_WITH_VARIANTS.encode()))
    # Primary should be Packages.xz (xz ranks highest)
    pkg_primaries = [i for i in indices if not isinstance(i, VariantIndex) and i.get("kind") == "packages"]
    pkg_variants = [i for i in indices if isinstance(i, VariantIndex) and i.get("kind") == "packages"]
    trans_variants = [i for i in indices if isinstance(i, VariantIndex) and i.get("kind") == "translations"]
    assert len(pkg_primaries) == 1, f"Expected 1 primary, got {len(pkg_primaries)}"
    assert len(pkg_variants) == 2, f"Expected 2 Package variants, got {len(pkg_variants)}: {[v.get('path') for v in pkg_variants]}"
    assert len(trans_variants) == 1, f"Expected 1 Translation variant, got {len(trans_variants)}"
    # Primary should NOT be optional
    assert not pkg_primaries[0].get("optional"), "Primary index should not be optional"
    # All variants should be optional
    for v in pkg_variants + trans_variants:
        assert v.get("optional"), f"VariantIndex should be optional: {v.get('path')}"
    print("PASS: variant_index_has_optional — variants marked optional, primary is not")


def test_variant_index_optional_stream():
    """VariantIndex.stream() is a no-op regardless of optional flag."""
    v = VariantIndex(path="test/Packages.xz", kind="packages")
    assert list(v.stream()) == []
    v["optional"] = True
    assert list(v.stream()) == []
    print("PASS: variant_index_optional_stream — stream() empty for VariantIndex")


if __name__ == "__main__":
    test_translations_stream_empty()
    test_commands_stream_empty()
    test_release_yields_translations()
    test_release_yields_commands()
    test_release_preserves_packages_and_sources()
    test_release_arch_filter_packages_only()
    test_variant_index_has_optional()
    test_variant_index_optional_stream()
    print("\nAll tests passed.")
