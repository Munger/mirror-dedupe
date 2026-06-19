## @file http_download.py
##
## @brief Unified HTTP fetch and download.
##
## Provides two public functions:
##
## * ``HTTPFetch``  - fetch a URI into memory (bytes).
## * ``HTTPDownload`` - download a URI to a local file, returning SHA-256.
##
## ``HTTPDownload`` is the primitive - it writes to a caller-specified path
## with retry, resume, HTTP status detection, and optional hash validation.
## ``HTTPFetch`` is a thin convenience wrapper that creates a temp file,
## delegates to ``HTTPDownload``, reads back the bytes, and cleans up.
##
## Transient curl failures (timeout, DNS, connection reset) are retried
## with exponential backoff.  HTTP 4xx/5xx responses and hash mismatches
## are always fatal and remove the output file before raising.
##
## Subprocess tracking lives in ``mirror_dedupe.lib.subproc``.
##
## @copyright Copyright (c) 2026 Tim Hosking
## @see https://github.com/munger
## @par Licence: MIT

from __future__ import annotations

import os
import tempfile
import time
from typing import List

from ..config import Config
from .. import deps
from ..lib.exceptions import ExceptionMsg
from .subproc import run_subprocess


class HTTPException(ExceptionMsg):
    ## @brief HTTP 4xx/5xx response.
    ##
    ## Construct with just the status *code* - the human-readable
    ## message is derived automatically.
    ##
    ## @param code  HTTP status code (e.g. 404, 503).

    _REASONS = {
        400: "Bad Request (400)",
        401: "Unauthorized (401)",
        403: "Forbidden (403)",
        404: "Not Found (404)",
        405: "Method Not Allowed (405)",
        408: "Request Timeout (408)",
        429: "Too Many Requests (429)",
        500: "Internal Server Error (500)",
        502: "Bad Gateway (502)",
        503: "Service Unavailable (503)",
        504: "Gateway Timeout (504)",
    }

    def __init__(self, code: int):
        super().__init__(code)

    def _format_message(self, code: int) -> str:
        return self._REASONS.get(code, f"HTTP {code}")

    @classmethod
    def reason(cls, code: int) -> str:
        return cls._REASONS.get(code, f"HTTP {code}")


class CurlException(ExceptionMsg):
    ## @brief curl subprocess failure.
    ##
    ## Construct with just the exit *code* - the human-readable
    ## message is derived automatically.  Named constants and
    ## ``TRANSIENT_EXITS`` are class members so callers can
    ## reference them without magic numbers.
    ##
    ## @param code  curl exit code (e.g. 7, 28, 56).

    UNSUPPORTED_PROTOCOL = 1
    RESOLVE_ERR          = 6
    CONNECT_ERR          = 7
    PARTIAL_FILE         = 18
    WRITE_ERR            = 23
    TIMEOUT              = 28
    SSL_CONNECT_ERR      = 35
    SERVER_NOTHING       = 52
    SEND_ERR             = 55
    RECV_ERR             = 56
    SSL_CA_ERR           = 77

    TRANSIENT_EXITS = (
        RESOLVE_ERR,
        CONNECT_ERR,
        PARTIAL_FILE,
        TIMEOUT,
        SSL_CONNECT_ERR,
        SERVER_NOTHING,
        SEND_ERR,
        RECV_ERR,
    )

    _REASONS = {
        UNSUPPORTED_PROTOCOL: "Unsupported protocol (curl exit 1)",
        RESOLVE_ERR:          "Could not resolve host (curl exit 6)",
        CONNECT_ERR:          "Failed to connect to host (curl exit 7)",
        PARTIAL_FILE:         "Partial file transferred (curl exit 18)",
        WRITE_ERR:            "Write error - permission denied or disk full (curl exit 23)",
        TIMEOUT:              "Operation timed out (curl exit 28)",
        SSL_CONNECT_ERR:      "TLS/SSL connect error (curl exit 35)",
        SERVER_NOTHING:       "Server replied with nothing (curl exit 52)",
        SEND_ERR:             "Send failure (curl exit 55)",
        RECV_ERR:             "Receive failure (curl exit 56)",
        SSL_CA_ERR:           "SSL CA cert problem - check certificates (curl exit 77)",
    }

    def __init__(self, code: int):
        super().__init__(code)

    def _format_message(self, code: int) -> str:
        return self._REASONS.get(code, f"curl exit {code}")

    @classmethod
    def reason(cls, code: int) -> str:
        return cls._REASONS.get(code, f"curl exit {code}")


class HostNotRespondingError(ExceptionMsg):
    ## @brief Raised when a transfer stalls or the connection times out.
    ##
    ## Triggered by curl exit code 28 (CURLE_OPERATION_TIMEDOUT), which
    ## covers both ``--connect-timeout`` expiry and ``--speed-time``
    ## stall detection (transfer rate below ``--speed-limit`` for too long).
    ##
    ## The sync coordinator counts consecutive occurrences and aborts the
    ## repo when the threshold is reached, avoiding all workers being tied
    ## up waiting on a dead upstream.

    def __init__(self) -> None:
        super().__init__(0, "Host not responding")

    def _format_message(self, code: int) -> str:
        return "Host not responding"


# -- public API ---------------------------------------------------------------


# -- Probe / lightweight helpers -----------------------------------------------
#
# These functions are designed for one-shot probing (scan discovery, health
# checks) where the caller only needs to know if a URL is reachable or wants
# a small file's content.  Unlike HTTPFetch / HTTPDownload they are:
#
#   * stateless - no resume, no retries, no temp file for HEAD
#   * bounded   - every call has a built-in 30-second total timeout
#   * safe      - never affect the sync pipeline's download machinery
#
# A probe is always a two-step dance:
#   1. HTTPPing  - HEAD request to see if the URL exists (fast, no body)
#   2. HTTPGet   - GET request to read the body (only when ping succeeds)
#
# Both functions return None on failure instead of raising, so the caller
# can safely skip unreachable URLs without exception handling.


def HTTPPing(uri: str, *, connect_timeout: int = 10, max_time: int = 30) -> bool:
    ## @brief Check whether a URL is reachable via HTTP HEAD.
    ##
    ## Issues ``curl -sI`` (HEAD request) with bounded timeouts and
    ## returns ``True`` when the remote responds with HTTP 200 or 405.
    ##
    ## HTTP 200 means the URL is reachable and HEAD is supported.
    ## HTTP 405 (Method Not Allowed) means the URL exists but the server
    ##     does not support HEAD - the caller should still attempt GET.
    ##
    ## All other outcomes (404, 5xx, DNS failure, timeout, connection
    ## refused) return ``False`` - the caller should treat them as
    ## "not worth fetching".
    ##
    ## No retry, no resume, no body.  Stderr is silently discarded so
    ## transient errors do not clutter the scan output.
    ##
    ## @param uri              Fully-qualified URL to probe.
    ## @param connect_timeout  Seconds to wait for TCP connect (default 10).
    ## @param max_time         Total seconds before curl gives up (default 30).
    ## @return ``True`` if the URL appears reachable, ``False`` otherwise.

    if not uri:
        return False

    args = [
        "curl", "-sI",
        "--connect-timeout", str(connect_timeout),
        "--max-time", str(max_time),
        "-o", "/dev/null",
        "-w", "%{http_code}",
        uri,
    ]
    try:
        rc, out, _err = run_subprocess(args)
        if rc != 0:
            return False
        if out:
            code = int(out.decode().strip())
            return code in (200, 405)
    except Exception:
        pass
    return False


def HTTPGet(uri: str, *, connect_timeout: int = 10, max_time: int = 30) -> bytes | None:
    ## @brief Fetch a URI's body into memory with bounded timeout.
    ##
    ## Lightweight alternative to ``HTTPFetch`` for small files (Release
    ## files, index pages) where retry/resume machinery is overkill.
    ##
    ## Unlike ``HTTPFetch``:
    ##   * No retries - a single failure returns ``None``.
    ##   * No resume  - ``-C -`` is not passed.
    ##   * No hash validation - the caller must verify content.
    ##   * No temp file - output goes directly to stdout.
    ##
    ## Uses ``curl -sL`` with ``--connect-timeout`` and ``--max-time``
    ## to guarantee bounded wall time.  HTTP 4xx/5xx, DNS failure,
    ## connection refused, and timeouts all return ``None`` without
    ## raising.
    ##
    ## @param uri              Fully-qualified URL to fetch.
    ## @param connect_timeout  Seconds to wait for TCP connect (default 10).
    ## @param max_time         Total seconds before curl gives up (default 30).
    ## @return The raw response bytes, or ``None`` on any failure.

    if not uri:
        return None

    args = [
        "curl", "-sL",
        "--connect-timeout", str(connect_timeout),
        "--max-time", str(max_time),
        uri,
    ]
    try:
        rc, out, _err = run_subprocess(args)
        if rc != 0:
            return None
        if out is None:
            return None
        return out
    except Exception:
        return None


# -- Full download machinery ---------------------------------------------------


def HTTPFetch(uri: str, *, expected_hash: str | None = None) -> bytes:
    ## @brief Fetch a URI into memory via ``curl -sL`` with retry and resume.
    ##
    ## Delegates to ``HTTPDownload`` using a temporary file, then reads
    ## the bytes back.  The temp file is always cleaned up.
    ##
    ## @param uri            Fully-qualified URL to fetch.
    ## @param expected_hash  Optional SHA-256 to validate against.
    ## @return Raw response bytes.
    ## @raises HTTPException  On HTTP 4xx/5xx response.
    ## @raises CurlException  On curl subprocess failure.

    if not uri:
        raise ExceptionMsg(0, "No URI provided")

    # Download to a temp file rather than stdout so we can separate
    # the body from curl's -w %{http_code} status line.  stdout is
    # reserved for the status code; the body goes into -o <file>.
    tmp = tempfile.NamedTemporaryFile(delete=False)
    try:
        tmp.close()
        HTTPDownload(uri, tmp.name, expected_hash=expected_hash)
        with open(tmp.name, "rb") as f:
            return f.read()
    finally:
        # Always clean up the temp file, even if HTTPDownload raised.
        try:
            os.unlink(tmp.name)
        except OSError:
            pass


def HTTPDownload(
    uri: str,
    output_path: str,
    *,
    expected_hash: str | None = None,
) -> str:
    ## @brief Download a URI to a local file and return its SHA-256 hash.
    ##
    ## Uses ``curl -sL`` with ``--connect-timeout`` (from
    ## ``Config.connect_timeout``), ``-C -`` for resume, and
    ## ``-w %{http_code}`` for HTTP error detection.
    ## Transient curl failures are retried per ``Config.max_retries``.
    ##
    ## When *expected_hash* is given, the file is validated after
    ## download and deleted on mismatch before raising.
    ##
    ## @param uri            Fully-qualified URL to download.
    ## @param output_path    Path to write the downloaded content.
    ## @param expected_hash  If given, SHA-256 must match this value.
    ## @return The SHA-256 hex digest of *output_path*.
    ## @raises HTTPException  On HTTP 4xx/5xx response.
    ## @raises CurlException  On curl subprocess failure.

    output_str = str(output_path)

    # Load settings once at the start so Config.load() isn't called
    # on every retry attempt.  connect_timeout prevents curl from
    # hanging for minutes on an unreachable upstream.
    config = Config.load()
    retries = config.max_retries
    _ct = str(config.connect_timeout)
    _st = str(config.stall_timeout)

    # -- inner helpers -------------------------------------------------------

    def _curl_args() -> List[str]:
        # Build the curl argument list.  -C - enables resume of
        # partial downloads; -w %{http_code} emits the HTTP status
        # on stdout so we can detect 4xx/5xx without -f (which is
        # broken on macOS curl 8.7.1 - exits 56 instead of 22).
        # --speed-limit 1 --speed-time _st aborts a stalled transfer
        # (less than 1 byte/sec for _st seconds) so workers are not
        # tied up indefinitely when the upstream stops responding.
        return ["curl", "-s", "-L",
                "--connect-timeout", _ct,
                "--speed-limit", "1", "--speed-time", _st,
                "-C", "-", "-w", "%{http_code}", "-o", output_str, uri]

    def _compute_hash() -> str:
        # Tool resolved by deps.check_dependencies() at startup.
        rc, out, _ = run_subprocess([*deps.HASH_TOOL, output_str])
        if rc == 0:
            return out.decode("utf-8").split()[0]
        raise ExceptionMsg(rc, "sha256sum failed")

    # -- retry loop ----------------------------------------------------------
    #
    # Transient curl failures are retried with exponential backoff.
    # HTTP-level errors and hash mismatches are always fatal and
    # remove the output file before raising so subsequent retries
    # (from the caller) start fresh rather than appending to a
    # corrupt partial.

    _range_retried = False

    for attempt in range(1 + retries):
        rc, out, err = run_subprocess(_curl_args())

        if rc == 0:
            # curl exited successfully.  Parse the HTTP status code
            # from -w %{http_code} (written to stdout).  If the value
            # is not an integer (very rare - may happen when curl
            # writes warnings to stdout) we proceed without checking.
            if out:
                try:
                    http_code = int(out.decode().strip())
                    if http_code >= 400:
                        # Fatal HTTP error - remove the partial file
                        # so it doesn't poison future resume attempts.
                        try:
                            os.unlink(output_str)
                        except OSError:
                            pass

                        # 416 (Range Not Satisfiable) means the
                        # server doesn't support range requests.
                        # Retry once without -C - to get the full
                        # file instead.
                        if http_code == 416 and not _range_retried and attempt < retries:
                            _range_retried = True
                            time.sleep(2 ** attempt)
                            continue

                        raise HTTPException(http_code)
                except ValueError:
                    pass

            # Download succeeded at the HTTP level.  Compute the
            # SHA-256 and validate against expected_hash if given.
            actual = _compute_hash()
            if expected_hash and actual != expected_hash:
                # Hash mismatch means corrupt content - remove the
                # file so the caller can retry from scratch.
                try:
                    os.unlink(output_str)
                except OSError:
                    pass
                raise ExceptionMsg(0, "Hash mismatch")
            return actual

        # -- curl exited non-zero --------------------------------------------
        # The stdout may still contain an HTTP status code (curl
        # sometimes prints one before dying) - capture it for a
        # better error message.
        http_info = ""
        _http_code: int | None = None
        if out:
            try:
                _http_code = int(out.decode().strip())
                if _http_code >= 400:
                    http_info = HTTPException.reason(_http_code)
            except ValueError:
                pass

        # Transient exits (timeout, DNS, connection reset) are
        # worth retrying.  Anything else is a hard failure.
        if rc in CurlException.TRANSIENT_EXITS and attempt < retries:
            time.sleep(2 ** attempt)
            continue

        # Fatal curl failure.  Prefer the HTTP status message
        # (when available) over the raw stderr from curl.
        # Exit 28 (timeout or stall) gets its own exception type so
        # the sync coordinator can count consecutive upstream failures.
        if http_info:
            raise HTTPException(_http_code)
        if rc == CurlException.TIMEOUT:
            raise HostNotRespondingError()
        raise CurlException(rc)

    # All attempts exhausted without a successful return.
    raise ExceptionMsg(0, "Retries exhausted")
