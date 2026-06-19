## @file _sink.py
##
## @brief Sink protocol and stock implementations for log-it.
##
## A Sink receives a fully-formatted line string and dispatches it to its
## destination.  ANSI escape sequences are already embedded by the time
## emit() is called; sinks that target non-ANSI destinations should strip
## them before writing.
##
## @copyright Copyright (c) 2026 Tim Hosking
## @see https://github.com/munger
## @par Licence: MIT


import sys
from typing import Protocol, runtime_checkable


@runtime_checkable
class Sink(Protocol):
    ## @brief Protocol for pluggable output destinations.
    ## @param line  Fully-formatted line (may contain ANSI escapes).

    def emit(self, line: str) -> None: ...


class TerminalSink:
    ## @brief Write coloured lines to a stream (default: stderr).
    ##
    ## Each emitted line is written with a trailing newline and the stream
    ## is flushed immediately so output appears in real time.
    ##
    ## @param stream  Writable text stream.  Defaults to ``sys.stderr``.

    def __init__(self, stream=None) -> None:
        self._stream = stream or sys.stderr

    def emit(self, line: str) -> None:
        self._stream.write(line + "\n")
        self._stream.flush()
