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
import tempfile
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


_TRANSIENT_EXITS: Tuple[int, ...] = (7, 18, 28, 35, 52, 55, 56)


def HTTPFetch(uri: str) -> bytes:
    ## @brief Fetch a URI into memory via ``curl -sL`` with retry.
    ##
    ## Returns the raw bytes.  Raises ``RuntimeError`` on curl failure
    ## or HTTP 4xx/5xx.  Transient errors (timeout, DNS, connection
    ## reset) are retried according to ``Config.max_retries`` with
    ## exponential backoff.
    ##
    ## @param uri  Fully-qualified URL to fetch.
    ## @return Raw response bytes.
    ## @raises RuntimeError  If the HTTP request fails.
    if not uri:
        raise RuntimeError("No URI provided")
    config = Config.load()
    retries = config.max_retries
    _ct = str(config.connect_timeout)

    for attempt in range(1 + retries):
        tmp = tempfile.NamedTemporaryFile(delete=False)
        try:
            tmp.close()
            args = ["curl", "-s", "-L", "--connect-timeout", _ct,
                    "-w", "%{http_code}", "-o", tmp.name, uri]
            rc, out, err = run_subprocess(args)

            if rc != 0:
                msg = err.decode("utf-8", errors="replace").strip() if err else ""
                if rc in _TRANSIENT_EXITS and attempt < retries:
                    time.sleep(2 ** attempt)
                    continue
                raise RuntimeError(
                    f"FAILED {uri} - curl exit {rc} {msg}".strip()
                )

            if out:
                try:
                    http_code = int(out.decode().strip())
                    if http_code >= 400:
                        reason = _HTTP_REASONS.get(http_code, "")
                        raise RuntimeError(
                            f"FAILED {uri} - {reason} ({http_code})" if reason
                            else f"FAILED {uri} - HTTP {http_code}"
                        )
                except ValueError:
                    pass

            with open(tmp.name, "rb") as f:
                return f.read()

        finally:
            try:
                os.unlink(tmp.name)
            except OSError:
                pass

    raise RuntimeError(f"FAILED {uri} - all {retries + 1} attempts failed")


def HTTPDownload(uri: str, output_path: str) -> str:
    ## @brief Download a URI to a local file and return its SHA-256 hash.
    ##
    ## Uses ``curl -sL`` with ``--connect-timeout`` (from
    ## ``Config.connect_timeout``), ``-C -`` for resume, and
    ## ``-w %{http_code}`` for HTTP error detection.
    ## Transient curl failures are retried per ``Config.max_retries``.
    ##
    ## @param uri          Fully-qualified URL to download.
    ## @param output_path  Path to write the downloaded content.
    ## @return The SHA-256 hex digest of *output_path*.
    ## @raises RuntimeError  On curl failure or HTTP error.
    output_str = str(output_path)

    config = Config.load()
    retries = config.max_retries
    _ct = str(config.connect_timeout)

    def _curl_args() -> List[str]:
        return ["curl", "-s", "-L", "--connect-timeout", _ct, "-C", "-",
                "-w", "%{http_code}", "-o", output_str, uri]

    def _compute_hash() -> str:
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
                            time.sleep(2 ** attempt)
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
            time.sleep(2 ** attempt)
            continue

        if http_info:
            raise RuntimeError(f"FAILED {uri} - {http_info}")
        raise RuntimeError(
            f"FAILED {uri} - curl exit {rc} {msg}".strip()
        )

    raise RuntimeError(f"FAILED {uri} - all {retries + 1} attempts failed")
