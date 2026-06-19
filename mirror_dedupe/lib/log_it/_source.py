## @file _source.py
##
## @brief ColourSource protocol for log-it.
##
## A ColourSource is anything that can return an (fg_name, bg_name) pair for
## the current emission context.  The protocol is intentionally minimal so
## callers can supply any callable object or class instance.
##
## @copyright Copyright (c) 2026 Tim Hosking
## @see https://github.com/munger
## @par Licence: MIT


from typing import Protocol, runtime_checkable


@runtime_checkable
class ColourSource(Protocol):
    ## @brief Protocol for pluggable row-colour resolution.
    ##
    ## Implementations are called once per emitted row.  The return value
    ## is a pair of named colour strings (e.g. ``("MAGENTA", None)``) or
    ## ``(None, None)`` when no colour should be applied.
    ##
    ## @return ``(fg_name, bg_name)`` — either element may be None.

    def get_colour(self) -> tuple[str | None, str | None]: ...
