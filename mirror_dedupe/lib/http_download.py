## @file http_download.py
##
## @brief Unified HTTP fetch and download.
##
## Provides two public functions:
##
## * ``HTTPFetch``  — fetch a URI into memory (bytes).
## * ``HTTPDownload`` — download a URI to a local file, returning SHA-256.
##
## Subprocess tracking lives in ``mirror_dedupe.lib.subproc``.
##
## @copyright Copyright (c) 2026 Tim Hosking
## @see https://github.com/munger
## @par Licence: MIT

from __future__ import annotations

import os
import platform
import time
from typing import Dict, List, Tuple

from ..config import Config
from .subproc import run_subprocess


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
    ## @brief Return a human-readable HTTP status description.
    ## @param code  HTTP status code.
    ## @return Description string (e.g. ``"Not Found"``) or ``"HTTP {code}"``.
    return _HTTP_REASONS.get(code, f"HTTP {code}")


def HTTPFetch(uri: str) -> bytes:
    ## @brief Fetch a URI into memory via ``curl -sL``.
    ##
    ## Returns the raw bytes.  Used in scan mode for Release and
    ## Packages.gz content.  Raises ``RuntimeError`` if curl exits
    ## non-zero (network error, DNS failure, SSL issue, etc.).
    ##
    ## Uses ``--connect-timeout`` from ``Config.connect_timeout`` (default
    ## 10) so the probe doesn't hang forever on an unresponsive upstream.
    ##
    ## curl's built-in Happy Eyeballs (RFC 8305) handles transparent
    ## IPv6/IPv4 fallback.
    ##
    ## @param uri  Fully-qualified URL to fetch.
    ## @return Raw response bytes.
    ## @raises RuntimeError  If the HTTP request fails.
    if not uri:
        raise RuntimeError("No URI provided")
    config = Config.load()
    args = ["curl", "-s", "-L", "--connect-timeout", str(config.connect_timeout), uri]
    rc, out, err = run_subprocess(args)
    if rc != 0:
        msg = err.decode("utf-8", errors="replace").strip() if err else ""
        raise RuntimeError(f"FAILED {uri} - curl exit {rc} {msg}".strip())
    return out  # type: ignore[return-value]


def HTTPDownload(uri: str, output_path: str, retries: int = 2) -> str:
    ## @brief Download a URI to a local file and return its SHA-256 hash.
    ##
    ## Uses ``curl -sL`` with ``--connect-timeout`` (from
    ## ``Config.connect_timeout``) and ``-C -`` to auto-resume partial
    ## downloads, and ``-w %{http_code}`` to detect HTTP-level errors
    ## (4xx/5xx) even on macOS where ``-f`` exits 56 for 4xx.  After a
    ## successful download, computes the hash via ``sha256sum`` (Linux) or
    ## ``shasum -a 256`` (macOS fallback).
    ##
    ## On transient curl failures (exit codes 7, 18, 35, 52, 55, 56),
    ## retries up to *retries* times with exponential backoff (2s, 4s).
    ## The partial staging file is preserved for ``-C -`` resume across
    ## retry attempts.
    ##
    ## curl's built-in Happy Eyeballs (RFC 8305) handles transparent
    ## IPv6/IPv4 fallback.
    ##
    ## @param uri          Fully-qualified URL to download.
    ## @param output_path  Path to write the downloaded content.
    ## @param retries      Number of retry attempts for transient errors.
    ## @return The SHA-256 hex digest of *output_path*.
    ## @raises RuntimeError  On curl failure, HTTP error, or hash tool missing.
    output_str = str(output_path)

    _TRANSIENT_EXITS: Tuple[int, ...] = (7, 18, 35, 52, 55, 56)
    _connect_timeout = str(Config.load().connect_timeout)

    def _curl_args() -> List[str]:
        ## @brief Build curl argument list.
        ## @return Argument list for ``subprocess.Popen``.
        return ["curl", "-s", "-L", "--connect-timeout", _connect_timeout, "-C", "-", "-w", "%{http_code}", "-o", output_str, uri]

    def _compute_hash() -> str:
        ## @brief Compute the SHA-256 hash of *output_str*.
        ##
        ## Tries ``sha256sum`` first (Linux), then ``shasum -a 256``
        ## (macOS).  Raises ``RuntimeError`` if neither tool is found.
        ##
        ## @return SHA-256 hex digest as a 64-character string.
        rc, out, _ = run_subprocess(["sha256sum", output_str])
        if rc == 0:
            return out.decode("utf-8").split()[0]
        rc, out, _ = run_subprocess(["shasum", "-a", "256", output_str])
        if rc == 0:
            return out.decode("utf-8").split()[0]
        raise RuntimeError(
            f"Failed to hash {output_str}: neither sha256sum nor shasum available"
        )

    _range_retried = False

    for attempt in range(1 + retries):
        rc, out, err = run_subprocess(_curl_args())
        if rc == 0:
            if out:
                try:
                    http_code = int(out.decode().strip())
                    if http_code >= 400:
                        try:
                            os.unlink(output_str)
                        except OSError:
                            pass
                        if http_code == 416 and not _range_retried and attempt < retries:
                            _range_retried = True
                            delay = 2 ** attempt
                            time.sleep(delay)
                            continue
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
