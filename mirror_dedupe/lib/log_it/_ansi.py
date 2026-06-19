## @file _ansi.py
##
## @brief ANSI SGR colour utilities for log-it.
##
## Named colour → numeric escape code resolution, wrapping helpers, and
## the canonical colour name tables.  Everything here is pure string
## manipulation with no I/O or state.
##
## Colour names are case-insensitive at the API boundary; the tables use
## upper-case keys internally.
##
## @copyright Copyright (c) 2026 Tim Hosking
## @see https://github.com/munger
## @par Licence: MIT

from __future__ import annotations

_FG: dict[str, str] = {
    "BLACK":          "30",
    "RED":            "31",
    "GREEN":          "32",
    "YELLOW":         "33",
    "BLUE":           "34",
    "MAGENTA":        "35",
    "CYAN":           "36",
    "WHITE":          "37",
    "BRIGHT_BLACK":   "90",
    "BRIGHT_RED":     "91",
    "BRIGHT_GREEN":   "92",
    "BRIGHT_YELLOW":  "93",
    "BRIGHT_BLUE":    "94",
    "BRIGHT_MAGENTA": "95",
    "BRIGHT_CYAN":    "96",
    "BRIGHT_WHITE":   "97",
    "GREY":           "90",
}

_BG: dict[str, str] = {
    "BLACK":          "40",
    "RED":            "41",
    "GREEN":          "42",
    "YELLOW":         "43",
    "BLUE":           "44",
    "MAGENTA":        "45",
    "CYAN":           "46",
    "WHITE":          "47",
    "BRIGHT_BLACK":   "100",
    "BRIGHT_RED":     "101",
    "BRIGHT_GREEN":   "102",
    "BRIGHT_YELLOW":  "103",
    "BRIGHT_BLUE":    "104",
    "BRIGHT_MAGENTA": "105",
    "BRIGHT_CYAN":    "106",
    "BRIGHT_WHITE":   "107",
    "GREY":           "100",
}

## @brief All valid named colours (case-normalised upper-case keys).
VALID: frozenset[str] = frozenset(_FG)


def fg_code(name: str | None) -> str | None:
    ## @brief Resolve a named foreground colour to its ANSI SGR numeric code.
    ## @param name  Colour name (case-insensitive) or None.
    ## @return Numeric string (e.g. ``"35"``) or None.
    return _FG.get(name.upper().strip()) if name else None


def bg_code(name: str | None) -> str | None:
    ## @brief Resolve a named background colour to its ANSI SGR numeric code.
    ## @param name  Colour name (case-insensitive) or None.
    ## @return Numeric string (e.g. ``"45"``) or None.
    return _BG.get(name.upper().strip()) if name else None


def build_code(fg: str | None, bg: str | None = None) -> str | None:
    ## @brief Combine foreground and optional background codes into one SGR string.
    ## @param fg  Numeric foreground code or None.
    ## @param bg  Numeric background code or None.
    ## @return ``"fg"`` or ``"fg;bg"`` or None.
    parts = [p for p in (fg, bg) if p]
    return ";".join(parts) if parts else None


def wrap(text: str, code: str, restore: str | None = None) -> str:
    ## @brief Surround *text* with an ANSI SGR open/close pair.
    ##
    ## When *restore* is given the closing sequence re-applies that code
    ## (e.g. to restore an enclosing row colour after a per-cell override)
    ## rather than emitting a full reset.
    ##
    ## @param text     Text to colour.
    ## @param code     SGR code string (e.g. ``"31"`` or ``"31;41"``).
    ## @param restore  Code to re-apply after *text*, or None for full reset.
    ## @return ANSI-escaped string.
    end = f"\033[{restore}m" if restore else "\033[0m"
    return f"\033[{code}m{text}{end}"
