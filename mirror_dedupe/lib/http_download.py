## @file http_download.py
##
## @brief Unified HTTP fetch and download with subprocess tracking.
##
## Provides two public functions:
##
## * ``HTTPFetch``  — fetch a URI into memory (bytes).
## * ``HTTPDownload`` — download a URI to a local file, returning SHA-256.
##
## @copyright Copyright (c) 2026 Tim Hosking
## @see https://github.com/munger
## @par Licence: MIT

from __future__ import annotations

import subprocess
import threading
from typing import List, Optional, Tuple

# -- subprocess tracking (Ctrl-C support) ------------------------------------

_active_procs: set[subprocess.Popen] = set()
_active_lock = threading.Lock()


def _register(proc: subprocess.Popen) -> None:
    ## @brief Track a subprocess for Ctrl-C kill.
    ## @param proc  ``subprocess.Popen`` instance to track.
    ## @return None
    with _active_lock:
        _active_procs.add(proc)


def _unregister(proc: subprocess.Popen) -> None:
    ## @brief Stop tracking a subprocess (already finished).
    ## @param proc  ``subprocess.Popen`` instance to unregister.
    ## @return None
    with _active_lock:
        _active_procs.discard(proc)


def kill_active_subprocesses() -> None:
    ## @brief Kill all tracked subprocesses (called on Ctrl-C).
    ##
    ## Iterates a snapshot of the active set so concurrent modifications
    ## during kill don't race.  Silently ignores ``OSError`` for processes
    ## that already exited.
    ##
    ## @return None
    with _active_lock:
        for p in list(_active_procs):
            try:
                p.kill()
            except OSError:
                pass
        _active_procs.clear()


def _run_subprocess(args: List[str]) -> Tuple[int, Optional[bytes], bytes]:
    ## @brief Run a subprocess with stdout/stderr capture and Ctrl-C safety.
    ##
    ## Registers the process so that a SIGINT kills it immediately,
    ## then unregisters once ``communicate()`` returns.  If the Python
    ## process is interrupted mid-communicate the ``except`` block
    ## kills the child before re-raising.
    ##
    ## @param args  Argument list for ``subprocess.Popen``.
    ## @return Tuple of ``(returncode, stdout_bytes, stderr_bytes)``.
    stdout_dest: int | None = subprocess.PIPE
    proc = subprocess.Popen(args, stdout=stdout_dest, stderr=subprocess.PIPE)
    _register(proc)
    try:
        out, err = proc.communicate()
    except:  # noqa: E722
        try:
            proc.kill()
        except OSError:
            pass
        proc.wait()
        raise
    finally:
        _unregister(proc)
    return proc.returncode, out, err


# -- public API ---------------------------------------------------------------


def HTTPFetch(uri: str) -> bytes:
    ## @brief Fetch a URI into memory via ``curl -sL``.
    ##
    ## Returns the raw bytes.  Used in scan mode for Release and
    ## Packages.gz content.  Raises ``RuntimeError`` if curl exits
    ## non-zero (network error, DNS failure, SSL issue, etc.).
    ##
    ## @param uri  Fully-qualified URL to fetch.
    ## @return Raw response bytes.
    ## @raises RuntimeError  If the HTTP request fails.
    if not uri:
        raise RuntimeError("No URI provided")
    rc, out, err = _run_subprocess(["curl", "-s", "-L", uri])
    if rc != 0:
        msg = err.decode("utf-8", errors="replace").strip() if err else ""
        raise RuntimeError(f"Failed to fetch {uri}: curl exit {rc} {msg}")
    return out  # type: ignore[return-value]


def HTTPDownload(uri: str, output_path: str) -> str:
    ## @brief Download a URI to a local file and return its SHA-256 hash.
    ##
    ## Uses ``curl -sL`` with ``-w %{http_code}`` to detect HTTP-level
    ## errors (4xx/5xx) even on macOS where ``-f`` exits 56 for 4xx.
    ## After a successful download, computes the hash via ``sha256sum``
    ## (Linux) or ``shasum -a 256`` (macOS fallback).
    ##
    ## @param uri          Fully-qualified URL to download.
    ## @param output_path  Path to write the downloaded content.
    ## @return The SHA-256 hex digest of *output_path*.
    ## @raises RuntimeError  On curl failure, HTTP error, or hash tool missing.
    output_str = str(output_path)
    rc, out, err = _run_subprocess(
        ["curl", "-s", "-L", "-w", "%{http_code}", "-o", output_str, uri],
    )
    if rc != 0:
        msg = err.decode("utf-8", errors="replace").strip() if err else ""
        raise RuntimeError(f"Failed to download {uri}: curl exit {rc} {msg}")
    if out:
        try:
            http_code = int(out.decode().strip())
            if http_code >= 400:
                raise RuntimeError(
                    f"Failed to download {uri}: HTTP {http_code}"
                )
        except ValueError:
            pass
    rc, out, _ = _run_subprocess(["sha256sum", output_str])
    if rc == 0:
        return out.decode("utf-8").split()[0]
    rc, out, _ = _run_subprocess(["shasum", "-a", "256", output_str])
    if rc == 0:
        return out.decode("utf-8").split()[0]
    raise RuntimeError(
        f"Failed to hash {output_str}: neither sha256sum nor shasum available"
    )
