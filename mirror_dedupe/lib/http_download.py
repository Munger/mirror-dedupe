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

import os
import platform
import subprocess
import threading
import time
from typing import Dict, List, Optional, Tuple, Type

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


_HTTP_REASONS: Dict[int, str] = {
    400: "Bad Request",
    401: "Unauthorized",
    403: "Forbidden",
    404: "Not Found",
    405: "Method Not Allowed",
    408: "Request Timeout",
    429: "Too Many Requests",
    500: "Internal Server Error",
    502: "Bad Gateway",
    503: "Service Unavailable",
    504: "Gateway Timeout",
}


def _http_reason(code: int) -> str:
    return _HTTP_REASONS.get(code, f"HTTP {code}")


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
        raise RuntimeError(f"FAILED {uri} - curl exit {rc} {msg}".strip())
    return out  # type: ignore[return-value]


def HTTPDownload(uri: str, output_path: str, retries: int = 2) -> str:
    ## @brief Download a URI to a local file and return its SHA-256 hash.
    ##
    ## Uses ``curl -sL`` with ``-C -`` to auto-resume partial downloads,
    ## and ``-w %{http_code}`` to detect HTTP-level errors (4xx/5xx) even
    ## on macOS where ``-f`` exits 56 for 4xx.  After a successful
    ## download, computes the hash via ``sha256sum`` (Linux) or
    ## ``shasum -a 256`` (macOS fallback).
    ##
    ## On transient curl failures (exit codes 7, 18, 35, 52, 55, 56),
    ## retries up to *retries* times with exponential backoff (2s, 4s).
    ## The partial staging file is preserved for ``-C -`` resume across
    ## retry attempts.
    ##
    ## @param uri          Fully-qualified URL to download.
    ## @param output_path  Path to write the downloaded content.
    ## @param retries      Number of retry attempts for transient errors.
    ## @return The SHA-256 hex digest of *output_path*.
    ## @raises RuntimeError  On curl failure, HTTP error, or hash tool missing.
    output_str = str(output_path)

    _TRANSIENT_EXITS: Tuple[int, ...] = (7, 18, 35, 52, 55, 56)

    def _compute_hash() -> str:
        rc, out, _ = _run_subprocess(["sha256sum", output_str])
        if rc == 0:
            return out.decode("utf-8").split()[0]
        rc, out, _ = _run_subprocess(["shasum", "-a", "256", output_str])
        if rc == 0:
            return out.decode("utf-8").split()[0]
        raise RuntimeError(
            f"Failed to hash {output_str}: neither sha256sum nor shasum available"
        )

    for attempt in range(1 + retries):
        rc, out, err = _run_subprocess(
            ["curl", "-s", "-L", "-C", "-", "-w", "%{http_code}", "-o", output_str, uri],
        )
        if rc == 0:
            if out:
                try:
                    http_code = int(out.decode().strip())
                    if http_code >= 400:
                        try:
                            os.unlink(output_str)
                        except OSError:
                            pass
                        reason = _HTTP_REASONS.get(http_code, "")
                        raise RuntimeError(
                            f"FAILED {uri} - {reason} ({http_code})" if reason
                            else f"FAILED {uri} - HTTP {http_code}"
                        )
                except ValueError:
                    pass
            return _compute_hash()

        msg = err.decode("utf-8", errors="replace").strip() if err else ""
        http_info = ""
        if out:
            try:
                code = int(out.decode().strip())
                if code >= 400:
                    reason = _HTTP_REASONS.get(code, "")
                    http_info = f"{reason} ({code})" if reason else f"HTTP {code}"
            except ValueError:
                pass
        if rc in _TRANSIENT_EXITS and attempt < retries:
            delay = 2 ** attempt
            time.sleep(delay)
            continue

        if http_info:
            raise RuntimeError(f"FAILED {uri} - {http_info}")
        raise RuntimeError(
            f"FAILED {uri} - curl exit {rc} {msg}".strip()
        )

    raise RuntimeError(f"FAILED {uri} - all {retries + 1} attempts failed")
