## @file gpg.py
##
## @brief GPG key management and Release file signature verification.
##
## Provides two public functions:
##
##   ``prepare_keyring(url, dest, connect_timeout)``
##     Fetch a GPG key from *url* and store it as a binary (dearmored)
##     keyring at *dest*.  If *dest* already exists the fetch is skipped.
##     Raises ``GpgKeyError`` if the key cannot be fetched or dearmored.
##
##   ``verify_release(release_path, sig_url, keyring_path, connect_timeout)``
##     Fetch the detached signature at *sig_url*, write it to a tempfile,
##     and run ``gpgv`` to verify *release_path* against the keyring at
##     *keyring_path*.  Returns ``True`` on success, ``False`` on failure.
##
## @copyright Copyright (c) 2026 Tim Hosking
## @see https://github.com/munger
## @par Licence: MIT


import os
import tempfile
from pathlib import Path

from . import LOG
from .http_download import HTTPGet
from .log import log
from .subproc import run_subprocess


class GpgKeyError(Exception):
    ## @brief Raised when a GPG key cannot be fetched or prepared.
    ## @param message  Human-readable explanation.

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


def prepare_keyring(
    url: str,
    dest: Path,
    connect_timeout: int = 10,
) -> None:
    ## @brief Fetch a GPG key and store it as a binary keyring at *dest*.
    ##
    ## If *dest* already exists the fetch is skipped - the cached key is
    ## reused.  To force a refresh, delete the file before calling this.
    ##
    ## ASCII-armored keys (``-----BEGIN PGP PUBLIC KEY BLOCK-----``) are
    ## dearmored via ``gpg --dearmor`` before storage.  Keys already in
    ## binary format are stored as-is.
    ##
    ## @param url              URL of the GPG key (``gpg_key_url``).
    ## @param dest             Destination path for the binary keyring.
    ## @param connect_timeout  Seconds to wait for TCP connect.
    ## @raises GpgKeyError     If the fetch or dearmor fails.
    ## @return None

    if dest.exists():
        return

    dest.parent.mkdir(parents=True, exist_ok=True)

    LOG("GPG key", "", url)
    key_bytes = HTTPGet(url, connect_timeout=connect_timeout)
    if not key_bytes:
        raise GpgKeyError(f"Failed to fetch GPG key from {url}")

    if key_bytes.lstrip().startswith(b"-----"):
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix=".asc") as tmp:
                tmp.write(key_bytes)
                tmp_path = tmp.name
            rc, _, err = run_subprocess([
                "gpg", "--batch", "--yes",
                "--dearmor", "--output", str(dest),
                tmp_path,
            ])
            if rc != 0:
                raise GpgKeyError(
                    f"gpg --dearmor failed for {url}: "
                    f"{err.decode(errors='replace').strip()}"
                )
        finally:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
    else:
        dest.write_bytes(key_bytes)

    LOG("GPG keyring", "", str(dest))


def verify_release(
    release_path: Path,
    sig_url: str,
    keyring_path: Path,
    connect_timeout: int = 10,
    *,
    inrelease_url: str = "",
    label: str = "",
) -> bool:
    ## @brief Verify a Release file against its GPG signature.
    ##
    ## Primary path: fetches the detached ``Release.gpg`` from *sig_url*
    ## and runs ``gpgv <sig> <release>``.
    ##
    ## Fallback: if *sig_url* returns nothing (repo publishes only an
    ## inline-signed ``InRelease``) and *inrelease_url* is provided, fetches
    ## that file and runs ``gpgv <inrelease>`` - gpgv handles clearsigned
    ## files natively without extra flags.
    ##
    ## Returns ``True`` if either path verifies successfully, ``False``
    ## otherwise.  Never raises for a failed signature.
    ##
    ## @param release_path    Path to the Release file on disk.
    ## @param sig_url         URL of the ``Release.gpg`` detached signature.
    ## @param keyring_path    Path to the binary keyring.
    ## @param connect_timeout Seconds to wait for TCP connect.
    ## @param inrelease_url   URL of ``InRelease`` to try if *sig_url* is absent.
    ## @return ``True`` on successful verification, ``False`` on failure.

    keyring_str = str(keyring_path.resolve())

    sig_bytes = HTTPGet(sig_url, connect_timeout=connect_timeout)
    if sig_bytes:
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix=".gpgsig") as tmp:
                tmp.write(sig_bytes)
                tmp_path = tmp.name
            rc, _, err = run_subprocess([
                "gpgv",
                "--keyring", keyring_str,
                tmp_path,
                str(release_path),
            ])
            if rc != 0:
                detail = err.decode(errors="replace").strip()
                LOG("GPG FAILED", "", f"{label}  {detail}" if label else detail, colour="RED")
                return False
            LOG("GPG verified", "", label)
            return True
        finally:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

    if not inrelease_url:
        LOG("GPG FAILED", "", f"{label}  no signature available" if label else "no signature available", colour="RED")
        return False

    log("  GPG: no detached sig, trying InRelease", level="DEBUG")
    ir_bytes = HTTPGet(inrelease_url, connect_timeout=connect_timeout)
    if not ir_bytes:
        LOG("GPG FAILED", "", f"{label}  no InRelease available" if label else "no InRelease available", colour="RED")
        return False

    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".InRelease") as tmp:
            tmp.write(ir_bytes)
            tmp_path = tmp.name
        rc, _, err = run_subprocess([
            "gpgv",
            "--keyring", keyring_str,
            tmp_path,
        ])
        if rc != 0:
            detail = err.decode(errors="replace").strip()
            LOG("GPG FAILED", "", f"{label}  {detail}" if label else detail, colour="RED")
            return False
        LOG("GPG verified", "", label)
        return True
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
