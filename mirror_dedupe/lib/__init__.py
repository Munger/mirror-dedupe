## @file __init__.py
##
## @brief Shared library modules for mirror-dedupe.
##
## Low-level clients live in their respective modules and are not
## re-exported here.
##
## Date/time formatting has moved to ``lib.datetimeutils``.
##
## @copyright Copyright (c) 2026 Tim Hosking
## @see https://github.com/munger
## @par Licence: MIT

from __future__ import annotations

## @brief Column width for sync status labels (Downloaded, Unchanged, etc.).
## Paths start at a fixed offset regardless of label length.
LOG_LABEL_W = 12


def fmt_duration(seconds: float) -> str:
    ## @brief Format a duration in seconds as a human-readable string.
    ##
    ## Examples: ``"6s"``, ``"1m 23s"``, ``"1h 14m 48s"``.
    ##
    ## @param seconds  Duration in seconds (float, truncated to int).
    ## @return Formatted string.
    secs = int(seconds)
    if secs < 1:
        return "<1s"
    hrs = secs // 3600
    mins = (secs % 3600) // 60
    s = secs % 60
    if hrs > 0:
        return f"{hrs}h {mins:02d}m {s:02d}s"
    if mins > 0:
        return f"{mins}m {s:02d}s"
    return f"{secs}s"


def fmt_size(b: int) -> str:
    ## @brief Format a byte count as a human-readable string.
    ##
    ## @param b  Byte count.
    ## @return Formatted string (e.g. ``"450KB"``, ``"1.2GB"``).
    if b >= 1073741824:
        return f"{b / 1073741824:.1f}GB"
    if b >= 1048576:
        return f"{b / 1048576:.0f}MB"
    if b >= 1024:
        return f"{b / 1024:.0f}KB"
    return f"{b}B"
