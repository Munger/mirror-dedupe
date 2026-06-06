## @file log.py
##
## @brief Centralised logging for mirror-dedupe via Python's ``logging`` module.
##
## Provides a module-level ``log()`` convenience function and direct access
## to the ``get_logger()`` function for callers that want a named logger.
## Output goes to stderr with ``[LEVEL]`` prefix, matching the existing
## CLI output style.
##
## Usage::
##
##     from mirror_dedupe.lib.log import log
##     log("Pool sweep complete", level="INFO")
##     log("Failed to fetch {url}", level="ERROR")
##
##     # Or use a named logger directly:
##     from mirror_dedupe.lib.log import get_logger
##     logger = get_logger(__name__)
##     logger.info("Pool sweep complete")
##
## @copyright Copyright (c) 2026 Tim Hosking
## @see https://github.com/munger
## @par Licence: MIT

from __future__ import annotations

import logging
import re
import sys

## @brief Default log format used for all output.
## @see ``_CustomFormatter`` for level-dependent formatting.
_LEVEL_MAP = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARN": logging.WARNING,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
}

## @brief ANSI foreground colour codes (numeric portion of SGR escape).
_FG: dict[str, str] = {
    "BLACK": "30",
    "RED": "31",
    "GREEN": "32",
    "YELLOW": "33",
    "BLUE": "34",
    "MAGENTA": "35",
    "CYAN": "36",
    "WHITE": "37",
    "BRIGHT_BLACK": "90",
    "BRIGHT_RED": "91",
    "BRIGHT_GREEN": "92",
    "BRIGHT_YELLOW": "93",
    "BRIGHT_BLUE": "94",
    "BRIGHT_MAGENTA": "95",
    "BRIGHT_CYAN": "96",
    "BRIGHT_WHITE": "97",
    "GREY": "90",
}

## @brief ANSI background colour codes (numeric portion of SGR escape).
_BG: dict[str, str] = {
    "BLACK": "40",
    "RED": "41",
    "GREEN": "42",
    "YELLOW": "43",
    "BLUE": "44",
    "MAGENTA": "45",
    "CYAN": "46",
    "WHITE": "47",
    "BRIGHT_BLACK": "100",
    "BRIGHT_RED": "101",
    "BRIGHT_GREEN": "102",
    "BRIGHT_YELLOW": "103",
    "BRIGHT_BLUE": "104",
    "BRIGHT_MAGENTA": "105",
    "BRIGHT_CYAN": "106",
    "BRIGHT_WHITE": "107",
    "GREY": "100",
}

_VALID_COLOURS: frozenset[str] = frozenset(_FG.keys())

_SENTINEL_NONE = "NONE"
_SENTINEL_DEFAULT = "DEFAULT"

## @brief Module-level colour state.  Populated by ``setup_log_colours()``.
_COLOURED_LOGGING: bool = False
_DEFAULT_FG: str | None = None
_DEFAULT_BG: str | None = None
_REPO_COLOURS: dict[str, tuple[str | None, str | None]] = {}  # name -> (fg, bg)

_THREAD_RE = re.compile(r"^(.*)\s+\d+$")

_ROOT_LOGGER: logging.Logger | None = None


def _parse_repo_name(thread_name: str) -> str | None:
    ## @brief Extract the repo name from a thread name.
    ##
    ## Thread names have the format ``"<reponame> <N>"`` (e.g.
    ## ``"postgresql 3"``).  Strips the trailing counter to yield
    ## the bare repo name.
    ##
    ## @param thread_name  ``threading.current_thread().name``.
    ## @return Repo name or ``None`` if the pattern doesn't match.
    m = _THREAD_RE.match(thread_name)
    return m.group(1) if m else None


def _get_record_colour(record: logging.LogRecord) -> str | None:
    ## @brief Resolve the ANSI colour code for a log record's thread.
    ##
    ## Looks up the repo name in ``_REPO_COLOURS``.  Falls back to
    ## ``_DEFAULT_FG`` / ``_DEFAULT_BG``.  Returns ``None`` when
    ## coloured logging is disabled.
    ##
    ## @param record  A ``logging.LogRecord`` with ``threadName`` set.
    ## @return ANSI ``"fg;bg"`` string or ``None``.
    if not _COLOURED_LOGGING:
        return None
    repo_name = _parse_repo_name(record.threadName or "")
    if repo_name is not None and repo_name in _REPO_COLOURS:
        fg, bg = _REPO_COLOURS[repo_name]
    else:
        fg, bg = _DEFAULT_FG, _DEFAULT_BG
    parts = [p for p in (fg, bg) if p is not None]
    return ";".join(parts) if parts else None


def setup_log_colours(
    global_colour: str | None,
    global_bg: str | None,
    mirrors: list[dict],
) -> None:
    ## @brief Configure per-repo ANSI colour mapping for log output.
    ##
    ## Reads the global foreground colour (``"DEFAULT"`` or a named colour)
    ## and an optional global background colour.  Per-repo overrides are
    ## parsed from the *mirrors* list.  ``"NONE"`` on any value disables
    ## coloured logging entirely.
    ##
    ## @param global_colour  Global foreground colour name or ``None``.
    ## @param global_bg      Global background colour name or ``None``.
    ## @param mirrors        List of per-repo config dicts with optional
    ##                       ``params.log_colour`` and
    ##                       ``params.log_colour_bg`` fields.
    ## @return None
    global _COLOURED_LOGGING, _DEFAULT_FG, _DEFAULT_BG, _REPO_COLOURS

    _REPO_COLOURS = {}

    name = (global_colour or "").strip().upper() or _SENTINEL_NONE
    if name == _SENTINEL_NONE:
        _COLOURED_LOGGING = False
        _DEFAULT_FG = None
        _DEFAULT_BG = None
        return

    _COLOURED_LOGGING = True
    if name in _VALID_COLOURS:
        _DEFAULT_FG = _FG[name]
        bg_name = (global_bg or "").strip().upper() or _SENTINEL_NONE
        _DEFAULT_BG = _BG.get(bg_name) if bg_name in _BG else None
    else:
        _DEFAULT_FG = None
        _DEFAULT_BG = None

    for mirror in mirrors:
        mname = mirror.get("name", "")
        if not mname:
            continue
        params = mirror.get("params") or {}
        fg_val = params.get("log_colour")
        bg_val = params.get("log_colour_bg")
        fg: str | None = None
        bg: str | None = None

        n = (fg_val or "").strip().upper() if fg_val else _SENTINEL_DEFAULT
        if n == _SENTINEL_DEFAULT:
            fg = _DEFAULT_FG
        elif n == _SENTINEL_NONE:
            fg = None
        elif n in _VALID_COLOURS:
            fg = _FG[n]

        m = (bg_val or "").strip().upper() if bg_val else _SENTINEL_DEFAULT
        if m == _SENTINEL_DEFAULT:
            bg = _DEFAULT_BG
        elif m == _SENTINEL_NONE:
            bg = None
        elif m in _BG:
            bg = _BG[m]

        _REPO_COLOURS[mname] = (fg, bg)


class _CustomFormatter(logging.Formatter):
    ## @brief Formatter that shows ``[LEVEL]`` prefix except for blank messages.

    def format(self, record: logging.LogRecord) -> str:
        ## @brief Format a log record with a ``[LEVEL]`` prefix.
        ##
        ## INFO messages are printed bare (``"  message"``).  All other
        ## levels get a bracketed prefix: ``"  [WARN] ..."``.  Blank
        ## messages produce an empty string.
        ##
        ## @param record  The log record to format.
        ## @return The formatted string.
        msg = record.getMessage()
        if not msg:
            return ""
        lvl = record.levelname
        tag = (
            f"{record.threadName}: ".ljust(16)
            if record.threadName and record.threadName != "MainThread"
            else ""
        )
        if lvl == "INFO":
            prefix = f"  {tag}"
        elif lvl == "WARNING":
            prefix = f"  [WARN] {tag}"
        elif lvl == "ERROR":
            prefix = f"  [ERROR] {tag}"
        elif lvl == "DEBUG":
            prefix = f"  [DEBUG] {tag}"
        else:
            prefix = f"  [{lvl}] {tag}"

        code = _get_record_colour(record)
        line = f"{prefix}{msg}"
        if code:
            line = f"\033[{code}m{line}\033[0m"
        return line


def get_logger(name: str | None = None) -> logging.Logger:
    ## @brief Return a named logger under the ``mirror-dedupe`` hierarchy.
    ##
    ## All loggers share the same root handler configured in this module.
    ## Callers that want fine-grained control over logging can use this
    ## instead of the ``log()`` convenience function.
    ##
    ## @param name  Logger name (e.g. ``"mirror_dedupe.repos.apt.release"``).
    ##              Pass ``__name__`` from the calling module.
    ## @return A ``logging.Logger`` instance.

    global _ROOT_LOGGER

    if _ROOT_LOGGER is None:
        _ROOT_LOGGER = logging.getLogger("mirror-dedupe")
        _ROOT_LOGGER.setLevel(logging.DEBUG)

        if not _ROOT_LOGGER.handlers:
            handler = logging.StreamHandler(sys.stderr)
            handler.setLevel(logging.DEBUG)
            handler.setFormatter(_CustomFormatter())
            _ROOT_LOGGER.addHandler(handler)

    if name:
        return logging.getLogger(f"mirror-dedupe.{name}")
    return _ROOT_LOGGER


def log(msg: str, level: str | None = None) -> None:
    ## @brief Write a message to stderr at the given severity level.
    ##
    ## When *level* is ``None`` (the default), the message is logged at
    ## INFO level.  When *level* is set (e.g. ``"WARN"``, ``"ERROR"``),
    ## the corresponding severity is used.
    ##
    ## @param msg    The message text.
    ## @param level  Optional severity label (``"DEBUG"``, ``"INFO"``,
    ##               ``"WARN"``, ``"ERROR"``).

    logger = get_logger()
    lvl = _LEVEL_MAP.get(level.upper() if level else "", logging.INFO)
    logger.log(lvl, "%s", msg)
