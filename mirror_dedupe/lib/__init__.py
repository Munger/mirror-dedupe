## @file __init__.py
##
## @brief Shared library modules for mirror-dedupe.
##
## Low-level clients live in their respective modules and are not
## re-exported here.
##
## @copyright Copyright (c) 2026 Tim Hosking
## @see https://github.com/munger
## @par Licence: MIT

from __future__ import annotations

from datetime import datetime as _datetime, timezone as _timezone

## @brief Column width for sync status labels (Downloaded, Unchanged, etc.).
## Paths start at a fixed offset regardless of label length.
LOG_LABEL_W = 12


def fmt_date(dt: _datetime = None) -> str:
    ## @brief Return an ISO date string like ``"2026-06-08"``.
    ##
    ## If *dt* is ``None``, the current UTC date is used.
    ##
    ## @param dt  Optional ``datetime`` instance.
    ## @return ISO date string.

    if dt is None:
        return _datetime.now(_timezone.utc).strftime("%Y-%m-%d")
    return dt.strftime("%Y-%m-%d")


def fmt_isotimestamp(dt: _datetime = None) -> str:
    ## @brief Return an ISO-8601 timestamp like ``"2026-06-08T13:47:04.123456"``.
    ##
    ## If *dt* is ``None``, the current UTC time is used.
    ## Equivalent to ``datetime.isoformat()`` but always UTC.
    ##
    ## @param dt  Optional ``datetime`` instance.
    ## @return ISO-8601 timestamp string.

    if dt is None:
        dt = _datetime.now(_timezone.utc)
    return dt.isoformat()


def fmt_compact_ts(dt: _datetime = None) -> str:
    ## @brief Return a compact timestamp like ``"20260608T134704"`` safe for
    ##        directory names.
    ##
    ## If *dt* is ``None``, the current UTC time is used.
    ## No colons, no spaces, no timezone — safe in filenames on all
    ## platforms.
    ##
    ## @param dt  Optional ``datetime`` instance.
    ## @return Compact timestamp string.

    if dt is None:
        dt = _datetime.now(_timezone.utc)
    return dt.strftime("%Y%m%dT%H%M%S")


def fmt_datetime(dt: _datetime) -> str:
    ## @brief Format a ``datetime`` as ``"YYYY-MM-DD HH:MM:SS"``.
    ##
    ## @param dt  ``datetime`` instance (naive or aware).
    ## @return Formatted string.
    return dt.strftime("%Y-%m-%d %H:%M:%S")


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
