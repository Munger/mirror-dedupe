## @file datetimeutils.py
##
## @brief Date/time formatting helpers for mirror-dedupe.
##
## All functions accept an optional ``datetime`` instance (defaulting to
## current UTC time) and return a formatted string.  The three output
## formats are:
##
##   * ``fmt_date()``           - ISO date       -> ``"2026-06-08"``
##   * ``fmt_isotimestamp()``   - ISO-8601       -> ``"2026-06-08T21:59:10.899603+00:00"``
##   * ``fmt_compact_ts()``     - dir-safe       -> ``"20260608T215910"``
##   * ``fmt_datetime()``       - human-readable -> ``"2026-06-08 21:59:10"``
##
## @copyright Copyright (c) 2026 Tim Hosking
## @see https://github.com/munger
## @par Licence: MIT


from datetime import datetime as _datetime, timezone as _timezone


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
    ## @brief Return an ISO-8601 timestamp like ``"2026-06-08T13:47:04.123456+00:00"``.
    ##
    ## If *dt* is ``None``, the current UTC time is used.
    ## Equivalent to ``datetime.isoformat()`` on an aware datetime.
    ##
    ## @param dt  Optional ``datetime`` instance.
    ## @return ISO-8601 timestamp string.

    if dt is None:
        dt = _datetime.now(_timezone.utc)
    return dt.isoformat()


def fmt_compact_ts(dt: _datetime = None) -> str:
    ## @brief Return a compact timestamp safe for directory names.
    ##
    ## Format: ``20260608T215910`` - no colons, no spaces, no timezone.
    ## If *dt* is ``None``, the current UTC time is used.
    ##
    ## @param dt  Optional ``datetime`` instance.
    ## @return Compact timestamp string.

    if dt is None:
        dt = _datetime.now(_timezone.utc)
    return dt.strftime("%Y%m%dT%H%M%S")


def fmt_datetime(dt: _datetime = None) -> str:
    ## @brief Format a ``datetime`` as ``"YYYY-MM-DD HH:MM:SS"``.
    ##
    ## If *dt* is ``None``, the current UTC time is used.
    ##
    ## @param dt  Optional ``datetime`` instance (naive or aware).
    ## @return Formatted string.

    if dt is None:
        return _datetime.now(_timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    return dt.strftime("%Y-%m-%d %H:%M:%S")
