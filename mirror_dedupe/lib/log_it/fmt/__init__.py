## @file __init__.py
##
## @brief Standard value formatters for log-it column definitions.
##
## All formatters here use the Python standard library only and are always
## available.  Optional extension modules (``currency``, ``science``) are
## not imported automatically — load them explicitly when needed::
##
##     from log_it import fmt
##     from log_it.fmt import currency   # explicit opt-in
##
## Usage::
##
##     from log_it import Col, RowTemplate, fmt
##
##     ROW = RowTemplate(
##         Col("status",  width=12),
##         Col("size",    width=7,  align="right", fmt=fmt.filesize),
##         Col("elapsed", width=8,  align="right", fmt=fmt.duration),
##         Col("when",    width=10,                fmt=fmt.strftime("%Y-%m-%d")),
##         Col("path"),
##     )
##
##     ROW("Downloaded", 460288, 1.23, now, "pool/main/pkg_1.0_amd64.deb")
##
## @copyright Copyright (c) 2026 Tim Hosking
## @see https://github.com/munger
## @par Licence: MIT

from __future__ import annotations


def filesize(value: int) -> str:
    ## @brief Format a byte count as a human-readable string.
    ## @param value  Byte count.
    ## @return e.g. ``"450KB"``, ``"1.2GB"``.
    b = int(value)
    if b >= 1_073_741_824:
        return f"{b / 1_073_741_824:.1f}GB"
    if b >= 1_048_576:
        return f"{b / 1_048_576:.0f}MB"
    if b >= 1_024:
        return f"{b / 1_024:.0f}KB"
    return f"{b}B"


def duration(value: float) -> str:
    ## @brief Format a duration in seconds as a human-readable string.
    ## @param value  Duration in seconds.
    ## @return e.g. ``"6s"``, ``"1m 23s"``, ``"1h 14m 48s"``.
    secs = int(value)
    if secs < 1:
        return "<1s"
    hrs  = secs // 3600
    mins = (secs % 3600) // 60
    s    = secs % 60
    if hrs > 0:
        return f"{hrs}h {mins:02d}m {s:02d}s"
    if mins > 0:
        return f"{mins}m {s:02d}s"
    return f"{secs}s"


def strftime(pattern: str):
    ## @brief Return a formatter that applies *pattern* to a ``datetime`` value.
    ## @param pattern  A ``strftime``-style format string (e.g. ``"%Y-%m-%d"``).
    ## @return Callable ``(datetime) -> str``.
    def _fmt(value) -> str:
        return value.strftime(pattern)
    return _fmt


def number(spec: str):
    ## @brief Return a formatter that applies a Python format spec to a numeric value.
    ##
    ## Example::
    ##
    ##     Col("count", width=8, align="right", fmt=fmt.number(",d"))
    ##     Col("ratio", width=6, align="right", fmt=fmt.number(".1%"))
    ##
    ## @param spec  Python format-spec string (e.g. ``",.2f"``, ``",d"``).
    ## @return Callable ``(number) -> str``.
    def _fmt(value) -> str:
        return format(value, spec)
    return _fmt
