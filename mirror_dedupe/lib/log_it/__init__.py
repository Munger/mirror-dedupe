## @file __init__.py
##
## @brief log-it — simple tabular terminal logging with ANSI colour support.
##
## Define column layouts once as a RowTemplate; call it like a function
## to emit rows.  Thread-context colouring, per-row overrides, and per-cell
## Coloured() wrappers are all supported with no extra ceremony at call sites.
##
## Quick start::
##
##     from log_it import RowTemplate, Col, Coloured, log, configure
##
##     configure(colour_source=my_colour_source)
##
##     ROW = RowTemplate(Col("status", 12), Col("size", 7, align="right"), Col("path"))
##
##     ROW("Downloaded", "450KB", "pool/main/pkg_1.0_amd64.deb")
##     ROW("Error", "",  "pool/main/pkg_1.0_amd64.deb", colour="RED")
##     ROW(Coloured("Hash Mismatch", "RED"), "450KB", path)
##     log("Free-form message")
##     log("Something failed", colour="RED")
##
## @copyright Copyright (c) 2026 Tim Hosking
## @see https://github.com/munger
## @par Licence: MIT


from typing import TYPE_CHECKING

from . import _state
from ._ansi    import fg_code, bg_code, build_code
from ._col      import Col, Coloured
from ._sink     import TerminalSink
from ._table    import Table
from ._template import RowTemplate

if TYPE_CHECKING:
    from ._sink   import Sink
    from ._source import ColourSource

# Initialise the default terminal sink once.
_state.sinks = [TerminalSink()]

__all__ = [
    "Col",
    "Coloured",
    "RowTemplate",
    "Table",
    "configure",
    "log",
]


def configure(
    colour_source: object | None = None,
    context_source: object | None = None,
    sinks: list | None = None,
    context_width: int = 16,
    prefix: str = "  ",
) -> None:
    ## @brief Configure the global log-it runtime.
    ##
    ## Call once at application startup before emitting any rows.
    ## All parameters are optional; omitted values retain their defaults.
    ##
    ## @param colour_source   A ColourSource instance that provides per-row
    ##                        colour based on context.
    ## @param context_source  A zero-argument callable returning a string label
    ##                        prepended to every row (e.g. a worker name).
    ##                        Return ``None`` or ``""`` to suppress the tag.
    ## @param sinks           List of Sink instances to receive emitted lines.
    ##                        Defaults to a single TerminalSink writing to stderr.
    ## @param context_width   Width reserved for the context tag (default 16).
    ## @param prefix          Leading indent prepended to every line (default "  ").

    _state.colour_source  = colour_source
    _state.context_source = context_source
    _state.context_width  = context_width
    _state.prefix         = prefix
    if sinks is not None:
        _state.sinks = list(sinks)


def log(msg: str, level: str | None = None, colour: str | None = None) -> None:
    ## @brief Emit a free-form line with optional level tag and colour.
    ##
    ## Uses the same prefix, context tag, and sinks as RowTemplate rows so
    ## free-form messages align visually with columnar output.
    ##
    ## @param msg    Message text.
    ## @param level  Optional severity label (e.g. ``"WARN"``, ``"ERROR"``).
    ##               INFO (default) produces no bracketed prefix.
    ## @param colour Optional foreground colour name override.

    # Row colour: explicit > colour source
    row_fg_name: str | None = None
    row_bg_name: str | None = None
    if colour:
        row_fg_name = colour
    elif _state.colour_source is not None:
        row_fg_name, row_bg_name = _state.colour_source.get_colour()

    row_code = build_code(fg_code(row_fg_name), bg_code(row_bg_name))

    ctx = _state.context_source() if _state.context_source is not None else None
    tag = f"{ctx}: ".ljust(_state.context_width) if ctx else ""

    lvl = (level or "").upper()
    level_tag = f"[{lvl}] " if lvl and lvl != "INFO" else ""

    line = f"{_state.prefix}{level_tag}{tag}{msg}"
    if row_code:
        line = f"\033[{row_code}m{line}\033[0m"

    for sink in _state.sinks:
        sink.emit(line)
