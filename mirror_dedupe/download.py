#!/usr/bin/env python3
## @file download.py
##
## @brief Download and verification helpers for mirror-dedupe.
##
## Provides ``download_with_curl`` for HTTP downloads with resume and
## retry, and ``verify_sha256`` for hash verification using either the
## system ``sha256sum`` utility or Python's hashlib.
##
## @copyright Copyright (c) 2025-2026 Tim Hosking
## @see https://github.com/munger
## @par Licence: MIT

import os
import subprocess
import hashlib
from .utils import download_lock, active_downloads

USE_SHA256SUM = False
try:
    result = subprocess.run(['sha256sum', '--version'], capture_output=True)
    USE_SHA256SUM = result.returncode == 0
except:
    pass


def download_with_curl(url: str, dest_path: str, timeout: int = 300,
                       progress_info: str = "", force_ipv4: bool = False):
    ## @brief Download a file with curl, supporting resuming partial downloads.
    ##
    ## Returns a tuple ``(success: bool, status: str)`` where *status* is
    ## one of ``"ok"``, ``"timeout"``, ``"not_found"``, or ``"error"``.
    ##
    ## @param url            Remote URL to download.
    ## @param dest_path      Local destination path.
    ## @param timeout        Maximum time in seconds for the transfer.
    ## @param progress_info  Extra text appended to the status line.
    ## @param force_ipv4     If True, use ``curl -4`` (IPv4 only).
    ## @return Tuple of ``(success, status)``.

    from . import utils

    dest_dir = os.path.dirname(dest_path)
    os.makedirs(dest_dir, exist_ok=True)

    filename = os.path.basename(dest_path)
    with utils.download_lock:
        utils.active_downloads += 1
        current_active = utils.active_downloads
    print(f"  -> Downloading: {filename} ({current_active} active){progress_info}", flush=True)

    try:
        cmd = ['curl']
        if force_ipv4:
            cmd.append('-4')
        cmd.extend([
            '-s',
            '-f',
            '-L',
            '-C', '-',
            '--max-time', str(timeout),
            '-w', '%{http_code}',
            '-o', dest_path,
            url,
        ])
        result = subprocess.run(cmd, capture_output=True)

        with utils.download_lock:
            utils.active_downloads -= 1
            remaining = utils.active_downloads

        http_code = (result.stdout or b'').decode(errors='ignore').strip()

        if result.returncode == 0:
            print(f"  [OK] Completed: {filename} ({remaining} remaining)", flush=True)
            return True, 'ok'

        status = 'error'
        if result.returncode == 28:
            status = 'timeout'
        elif http_code in ('403', '404'):
            status = 'not_found'

        print(f"  [FAIL] Failed: {filename} ({remaining} remaining)", flush=True)
        return False, status
    except Exception as e:
        with utils.download_lock:
            utils.active_downloads -= 1
            remaining = utils.active_downloads
        print(f"  [ERROR] Error: {filename} ({remaining} remaining) - {e}", flush=True)
        return False, 'error'


def verify_sha256(file_path: str, expected_hash: str, buffer_size: int = 1048576) -> bool:
    ## @brief Verify a file's SHA-256 hash.
    ##
    ## Uses the system ``sha256sum`` utility if available (faster),
    ## otherwise falls back to Python's hashlib.
    ##
    ## @param file_path       Path to the file to verify.
    ## @param expected_hash   Expected SHA-256 hex digest.
    ## @param buffer_size     Read buffer size for hashlib fallback.
    ## @return True if the hash matches.

    if USE_SHA256SUM:
        try:
            result = subprocess.run(['sha256sum', file_path],
                                    capture_output=True, text=True)
            if result.returncode == 0:
                actual_hash = result.stdout.split()[0]
                return actual_hash == expected_hash
            return False
        except:
            return False
    else:
        try:
            sha256 = hashlib.sha256()
            with open(file_path, 'rb') as f:
                for chunk in iter(lambda: f.read(buffer_size), b''):
                    sha256.update(chunk)
            return sha256.hexdigest() == expected_hash
        except:
            return False
