## @file _state.py
##
## @brief Module-level runtime state for log-it.
##
## All mutable global configuration lives here so that _template.py and
## other internals can import this module once and always see the current
## values without circular imports.  configure() in __init__.py mutates
## these attributes directly.
##
## @copyright Copyright (c) 2026 Tim Hosking
## @see https://github.com/munger
## @par Licence: MIT


from typing import Callable, TYPE_CHECKING

if TYPE_CHECKING:
    from ._sink import Sink
    from ._source import ColourSource

sinks:          list                          = []     # populated by __init__ on first import
colour_source:  "ColourSource | None"         = None   # ColourSource | None
context_source: "Callable[[], str | None] | None" = None   # callable() -> str | None
context_width:  int                           = 16     # chars reserved for context tag
prefix:         str                           = "  "   # leading indent on every line
