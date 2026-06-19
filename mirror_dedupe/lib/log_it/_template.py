## @file _template.py
##
## @brief RowTemplate — the callable column-layout object for log-it.
##
## @copyright Copyright (c) 2026 Tim Hosking
## @see https://github.com/munger
## @par Licence: MIT


from ._ansi import fg_code, bg_code, build_code, wrap
from ._col import Col, Coloured
from . import _state


class RowTemplate:
    ## @brief Define a fixed-width columnar log format and emit rows.
    ##
    ## A RowTemplate is constructed once with column definitions and then
    ## called like a function to emit rows::
    ##
    ##     ROW = RowTemplate(
    ##         Col("status", width=10),
    ##         Col("size",   width=8, align="right", fmt=fmt.filesize),
    ##         Col("name"),
    ##     )
    ##
    ##     ROW("OK",    460288, "widget-1.0.tar.gz")
    ##     ROW("Error", 0,      "widget-1.0.tar.gz", colour="RED")
    ##     ROW(Coloured("Failed", "RED"), 0, name)
    ##     ROW("OK", name=item)
    ##
    ## Column values can be passed positionally (by column index) or by
    ## keyword (by column name).  Missing columns default to an empty string.
    ## ``colour=`` applies to the whole row; ``Coloured()`` wraps one cell.
    ##
    ## @param cols   Column definitions in display order.
    ## @param level  Optional log level tag (``"WARN"``, ``"ERROR"``, etc.).
    ##               INFO (the default) produces no tag prefix.

    def __init__(self, *cols: Col, level: str | None = None) -> None:
        self._cols  = cols
        self._level = level

    def __call__(self, *args: object, colour: str | None = None, **kwargs: object) -> None:
        ## @brief Emit one row.
        ##
        ## @param args    Column values in column order.
        ## @param colour  Optional row-level foreground colour name.
        ## @param kwargs  Column values by column name.

        # Resolve values: positional first, then keyword, then empty
        values: list[object] = []
        for i, col in enumerate(self._cols):
            if i < len(args):
                values.append(args[i])
            elif col.name in kwargs:
                values.append(kwargs[col.name])
            else:
                values.append("")

        # Resolve row colour: explicit kwarg > colour source
        row_fg_name: str | None = None
        row_bg_name: str | None = None
        if colour:
            row_fg_name = colour
        elif _state.colour_source is not None:
            row_fg_name, row_bg_name = _state.colour_source.get_colour()

        row_code = build_code(
            fg_code(row_fg_name),
            bg_code(row_bg_name),
        )

        # Context tag — provided by the app via context_source, or absent
        ctx = _state.context_source() if _state.context_source is not None else None
        tag = f"{ctx}: ".ljust(_state.context_width) if ctx else ""

        # Level prefix (omitted for INFO)
        lvl = (self._level or "").upper()
        level_tag = f"[{lvl}] " if lvl and lvl != "INFO" else ""

        # Format each cell
        parts: list[str] = []
        for col, val in zip(self._cols, values):
            if isinstance(val, Coloured):
                cell_fg = val.colour
                raw     = val.value
            else:
                cell_fg = col.colour
                if col.fmt is not None and val not in (None, ""):
                    raw = col.fmt(val)
                else:
                    raw = str(val) if val is not None else ""

            # Width / alignment
            if col.width > 0:
                formatted = raw.rjust(col.width) if col.align == "right" else raw.ljust(col.width)
            else:
                formatted = raw

            # Per-cell colour wraps just this cell; restore row colour after
            if cell_fg:
                cell_code = build_code(fg_code(cell_fg))
                if cell_code:
                    formatted = wrap(formatted, cell_code, restore=row_code or "0")

            parts.append(formatted)

        # Join: 1 space between fixed-width cols, 2 spaces before variable col
        body_pieces: list[str] = []
        for i, (col, part) in enumerate(zip(self._cols, parts)):
            body_pieces.append(part)
            if i < len(self._cols) - 1:
                nxt = self._cols[i + 1]
                body_pieces.append("  " if nxt.width == 0 else " ")

        body = "".join(body_pieces)
        line = f"{_state.prefix}{level_tag}{tag}{body}"

        # Apply row colour to the whole line
        if row_code:
            line = f"\033[{row_code}m{line}\033[0m"

        for sink in _state.sinks:
            sink.emit(line)
