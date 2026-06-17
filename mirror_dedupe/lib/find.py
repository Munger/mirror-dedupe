## @file find.py
##
## @brief Streaming ``find`` wrapper for mirror-dedupe.
##
## Provides ``find_stream()`` — a generator that runs GNU ``find`` as a
## subprocess and yields null-delimited output fields one at a time as
## they arrive.  The underlying ``find`` process and Python parser run
## concurrently via the OS pipe; the Python side never buffers more than
## one read chunk regardless of how many files exist on disk.
##
## On macOS, GNU ``find`` (``gfind`` from Homebrew ``findutils``) is
## required because BSD ``find`` lacks ``-printf``.  On Linux the system
## ``find`` is GNU find.  The resolved binary path is cached at module
## level so the lookup runs only once per process.
##
## @copyright Copyright (c) 2026 Tim Hosking
## @see https://github.com/munger
## @par Licence: MIT

from __future__ import annotations

import platform
import shutil
import subprocess
from typing import Generator, List, Optional


from .exceptions import ExceptionMsg


_find_cmd: Optional[str] = None


def find_binary() -> str:
    ## @brief Resolve and cache the GNU ``find`` executable path.
    ##
    ## On macOS, ``gfind`` from Homebrew ``findutils`` is required; on
    ## Linux the system ``find`` is used directly.  The result is cached
    ## so subsequent calls pay no overhead.
    ##
    ## @return Absolute path to the GNU ``find`` executable.
    ## @raise RuntimeError  If GNU ``find`` is not available.

    global _find_cmd
    if _find_cmd is not None:
        return _find_cmd

    if platform.system() == "Darwin":
        gfind = shutil.which("gfind")
        if gfind is None:
            raise ExceptionMsg(0,
                "gfind not found.  Install with: brew install findutils",
            )
        _find_cmd = gfind
    else:
        _find_cmd = "find"

    return _find_cmd


def find_stream(
    root: str,
    fmt: str,
    *,
    extra_args: Optional[List[str]] = None,
) -> Generator[str, None, None]:
    ## @brief Yield null-delimited ``find`` output fields as a generator.
    ##
    ## Runs ``find <root> [extra_args] -type f -printf <fmt>`` and yields
    ## each null-delimited field as a decoded string, one at a time.
    ## ``find`` and the Python parser run concurrently via the OS pipe —
    ## the generator never holds more than one read chunk (64 KB) in memory
    ## regardless of how many files are found.
    ##
    ## Callers that need pairs (e.g. path + inode) should consume two
    ## yields per iteration:
    ##
    ## @code
    ## it = find_stream(root, r"%P\0%i\0")
    ## for rel, inode_str in zip(it, it):
    ##     process(rel, int(inode_str))
    ## @endcode
    ##
    ## The subprocess is always waited on before the generator exits,
    ## even if the caller breaks early or an exception is raised.
    ##
    ## @param root        Directory to search (passed as the first path
    ##                    argument to ``find``).
    ## @param fmt         ``-printf`` format string.  Use ``\0`` as the
    ##                    field separator for safe null-delimited output.
    ## @param extra_args  Optional additional ``find`` arguments inserted
    ##                    between ``root`` and ``-type f`` (e.g.
    ##                    ``["-xdev"]`` to stay on one filesystem).
    ## @yield Decoded string fields in the order produced by ``find``.

    cmd = [find_binary(), root]
    if extra_args:
        cmd.extend(extra_args)
    cmd.extend(["-type", "f", "-printf", fmt])

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    buf = b""
    try:
        assert proc.stdout is not None
        while True:
            chunk = proc.stdout.read(65536)
            if not chunk:
                break
            buf += chunk
            while b"\0" in buf:
                field, buf = buf.split(b"\0", 1)
                yield field.decode()
    finally:
        proc.stdout.close()
        proc.wait()
