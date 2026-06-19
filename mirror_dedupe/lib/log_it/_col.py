## @file _col.py
##
## @brief Column definition and per-cell colour wrapper for log-it.
##
## @copyright Copyright (c) 2026 Tim Hosking
## @see https://github.com/munger
## @par Licence: MIT

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Col:
    ## @brief Define one column in a RowTemplate.
    ##
    ## @param name    Column name — used to accept keyword arguments at call time.
    ## @param width   Fixed display width in characters.  Pass ``0`` for the
    ##                last (variable-width) column which fills the remainder of
    ##                the line.
    ## @param align   ``"left"`` (default) or ``"right"``.
    ## @param colour  Optional default foreground colour name for every cell
    ##                in this column (e.g. ``"CYAN"``).  Overridden per-cell
    ##                by ``Coloured()``.
    ## @param header  Optional display label for the column header.  Defaults
    ##                to ``name`` when not set.  Useful when the kwarg key
    ##                must differ from the heading (e.g. key ``"files"``
    ##                heading ``"Files"``, or key ``"ts"`` heading
    ##                ``"Date/Time"``).
    ## @param fmt     Optional formatter: a callable ``(value) -> str`` applied
    ##                to raw values before width/alignment.  Use the
    ##                ``log_it.fmt`` module for ready-made formatters.
    ##                Ignored when the value is already empty or a ``Coloured``.
    ## @param footer  Optional aggregation callable for Table footers.
    ##                Receives the list of raw column values and returns a
    ##                display value.  If the result is not already a string,
    ##                ``fmt`` is applied.  Examples: ``sum``, ``len``,
    ##                ``lambda v: f"{len(v)} items"``.
    name:   str
    width:  int        = 0
    align:  str        = "left"
    colour: str | None = None
    header: str | None = None
    fmt:    object     = None
    footer: object     = None


class Coloured:
    ## @brief Wrap a cell value with an explicit per-cell colour override.
    ##
    ## Pass as a positional argument to RowTemplate.__call__ to colour just
    ## that cell independently of the row colour and column default::
    ##
    ##     ROW(Coloured("Hash Mismatch", "RED"), size, path)
    ##
    ## @param value   The cell value (converted to str automatically).
    ## @param colour  Foreground colour name (e.g. ``"RED"``).

    __slots__ = ("value", "colour")

    def __init__(self, value: object, colour: str) -> None:
        self.value  = str(value)
        self.colour = colour

    def __repr__(self) -> str:
        return f"Coloured({self.value!r}, {self.colour!r})"
