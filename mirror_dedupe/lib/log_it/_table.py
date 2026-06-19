## @file _table.py
##
## @brief Table — buffered tabular output with auto-sized columns, header, and footer.
##
## Unlike RowTemplate (which streams rows immediately), Table collects all rows
## then renders them as a unit via emit().  Column widths are computed
## automatically from the widest of header, content, and footer value.
##
## Footer aggregations are defined per-column as a callable that receives
## the list of raw (pre-formatted) values::
##
##     t = Table(
##         Col("repo",   footer=lambda v: f"{len(v)} repos"),
##         Col("size",   align="right", fmt=fmt.filesize, footer=sum),
##         Col("errors", align="right", footer=lambda v: sum(1 for x in v if x)),
##     )
##     t.add("widget", 460288, 0)
##     t.add("gadget", 1048576, 2)
##     t.emit()
##
## Output::
##
##     repo    size    errors
##     ──────────────────────
##     widget  450KB        0
##     gadget    1MB        2
##     ──────────────────────
##     2 repos 1.4MB        2
##
## @copyright Copyright (c) 2026 Tim Hosking
## @see https://github.com/munger
## @par Licence: MIT


from ._ansi    import fg_code, bg_code, build_code, wrap
from ._col     import Col, Coloured
from . import  _state

_COL_SEP  = "  "


class Table:
    ## @brief Collect rows then render as a formatted table.
    ##
    ## @param cols   Column definitions in display order.
    ## @param title  Optional title printed above the header.
    ## @param sep    Separator character used to draw divider lines (default ``"-"``).
    ##               Use ``"─"`` for a Unicode box-drawing line when the output
    ##               stream's encoding supports it.

    def __init__(self, *cols: Col, title: str | None = None, sep: str = "-") -> None:
        self._cols  = cols
        self._title = title
        self._sep   = sep
        self._rows: list[tuple[list[object], str | None]] = []

    def add(self, *args: object, colour: str | None = None, **kwargs: object) -> None:
        ## @brief Add a data row.
        ##
        ## Values are accepted positionally or by column name keyword.
        ## Missing columns default to ``None``.
        ##
        ## @param args    Column values in column order.
        ## @param colour  Optional row foreground colour name.
        ## @param kwargs  Column values by column name.

        values: list[object] = []
        for i, col in enumerate(self._cols):
            if i < len(args):
                values.append(args[i])
            elif col.name in kwargs:
                values.append(kwargs[col.name])
            else:
                values.append(None)
        self._rows.append((values, colour))

    def emit(self, sinks: list | None = None) -> None:
        ## @brief Render the table to all configured sinks.
        ##
        ## @param sinks  Optional sink list override.  When given, the table
        ##               is emitted to these sinks instead of the global ones.
        ##               Useful for directing table output to stdout while
        ##               streaming rows go to stderr.

        ncols = len(self._cols)

        def _format(col: Col, val: object) -> str:
            if val is None or val == "":
                return ""
            if col.fmt is not None:
                return col.fmt(val)
            return str(val)

        # Aggregate footer values from raw column data
        col_raw: list[list[object]] = [
            [row[0][i] for row in self._rows]
            for i in range(ncols)
        ]

        has_footer = any(col.footer is not None for col in self._cols)
        footer_cells: list[str] = []
        for i, col in enumerate(self._cols):
            if col.footer is None:
                footer_cells.append("")
            else:
                result = col.footer(col_raw[i])
                if isinstance(result, str):
                    footer_cells.append(result)
                elif col.fmt is not None and result not in (None, ""):
                    footer_cells.append(col.fmt(result))
                else:
                    footer_cells.append(str(result) if result is not None else "")

        # Auto-size: max of header label, all cell values, footer
        widths: list[int] = []
        for i, col in enumerate(self._cols):
            cell_widths = [len(_format(col, row[0][i])) for row in self._rows]
            w = max(
                [col.width, len(col.header or col.name), len(footer_cells[i])] + cell_widths
            )
            widths.append(w)

        total_width = sum(widths) + len(_COL_SEP) * (ncols - 1)
        sep = self._sep * total_width

        # Context tag captured once at render time
        ctx = _state.context_source() if _state.context_source is not None else None
        tag = f"{ctx}: ".ljust(_state.context_width) if ctx else ""

        def _pad(col: Col, text: str, width: int) -> str:
            return text.rjust(width) if col.align == "right" else text.ljust(width)

        _sinks = sinks if sinks is not None else _state.sinks

        def _emit(line: str, code: str | None = None) -> None:
            full = f"{_state.prefix}{tag}{line}"
            if code:
                full = f"\033[{code}m{full}\033[0m"
            for sink in _sinks:
                sink.emit(full)

        # Title
        if self._title:
            _emit(self._title)

        # Header + separator
        _emit(_COL_SEP.join(_pad(col, col.header or col.name, w) for col, w in zip(self._cols, widths)))
        _emit(sep)

        # Data rows
        for row_vals, row_colour in self._rows:
            row_fg: str | None = None
            row_bg: str | None = None
            if row_colour:
                row_fg = row_colour
            elif _state.colour_source is not None:
                row_fg, row_bg = _state.colour_source.get_colour()
            row_code = build_code(fg_code(row_fg), bg_code(row_bg))

            parts: list[str] = []
            for col, val, w in zip(self._cols, row_vals, widths):
                if isinstance(val, Coloured):
                    raw = _pad(col, val.value, w)
                    if val.colour:
                        cell_code = build_code(fg_code(val.colour))
                        if cell_code:
                            raw = wrap(raw, cell_code, restore=row_code or "0")
                else:
                    raw = _pad(col, _format(col, val), w)
                    if col.colour:
                        cell_code = build_code(fg_code(col.colour))
                        if cell_code:
                            raw = wrap(raw, cell_code, restore=row_code or "0")
                parts.append(raw)

            line = _COL_SEP.join(parts)
            if row_code:
                line = f"\033[{row_code}m{line}\033[0m"
            for sink in _sinks:
                sink.emit(f"{_state.prefix}{tag}{line}")

        # Footer
        if has_footer:
            _emit(sep)
            _emit(_COL_SEP.join(_pad(col, cell, w) for col, cell, w in zip(self._cols, footer_cells, widths)))
