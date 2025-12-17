"""Shared helpers for mirror_dedupe.repos.apt.

This module carries small, generic utilities that are reused across
Apt detection and parsing logic.
"""

from __future__ import annotations

from typing import Optional


def looks_like_release(body: str) -> bool:
    """Light-weight sanity check for Debian/Ubuntu Release text.

    We only care that the body looks *plausibly* like a suite Release,
    not that it is fully valid. Callers should treat this as a cheap
    heuristic, not a full validator.
    """

    markers = 0
    for line in body.splitlines():
        if line.startswith("Suite:") or line.startswith("Codename:"):
            markers += 1
        if line.startswith("Components:") or line.startswith("Architectures:"):
            markers += 1
        if markers >= 2:
            return True
    return False
