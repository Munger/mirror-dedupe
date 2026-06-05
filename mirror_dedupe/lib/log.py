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

_ROOT_LOGGER: logging.Logger | None = None


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
        if lvl == "INFO":
            return f"  {msg}"
        if lvl == "WARNING":
            return f"  [WARN] {msg}"
        if lvl == "ERROR":
            return f"  [ERROR] {msg}"
        if lvl == "DEBUG":
            return f"  [DEBUG] {msg}"
        return f"  [{lvl}] {msg}"


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
