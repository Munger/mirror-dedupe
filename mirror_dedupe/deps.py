## @file deps.py
##
## @brief Centralised external-dependency checker.
##
## Single source of truth for which system tools the project needs.
## ``check_dependencies()`` is called once at process startup from
## ``cli.py:main()``.  Every other module imports the resolved paths
## from here instead of probing on its own.
##
## @copyright Copyright (c) 2026 Tim Hosking
## @see https://github.com/munger
## @par Licence: MIT


import platform
import shutil
import sys
from typing import List


HASH_TOOL: List[str] = []
FIND_TOOL: str = ""
CURL_TOOL: str = ""
_checked: bool = False


def check_dependencies() -> None:
    ## @brief Probe every external tool needed by mirror-dedupe.
    ##
    ## Dies on the first missing tool with a clear install hint on
    ## stderr.  Sets the module-level ``HASH_TOOL``, ``FIND_TOOL``,
    ## and ``CURL_TOOL`` variables so consumers never probe again.

    global HASH_TOOL, FIND_TOOL, CURL_TOOL, _checked
    if _checked:
        return

    # SHA-256: sha256sum (Linux/GNU coreutils) or shasum (macOS).
    hash_bin = shutil.which("sha256sum")
    if hash_bin:
        HASH_TOOL = [hash_bin]
    else:
        hash_bin = shutil.which("shasum")
        if hash_bin is None:
            print(
                "ERROR: sha256sum not found and shasum not found - "
                "install coreutils (Linux) or use a macOS system with shasum",
                file=sys.stderr,
            )
            sys.exit(1)
        HASH_TOOL = [hash_bin, "-a", "256"]

    # GNU find: gfind on macOS, find on Linux.
    if platform.system() == "Darwin":
        gfind = shutil.which("gfind")
        if gfind is None:
            print(
                "ERROR: gfind not found.  Install with: brew install findutils",
                file=sys.stderr,
            )
            sys.exit(1)
        FIND_TOOL = gfind
    else:
        FIND_TOOL = "find"

    # curl - used for every HTTP download.
    curl_bin = shutil.which("curl")
    if curl_bin is None:
        print(
            "ERROR: curl not found.  Install with: brew install curl / apt install curl",
            file=sys.stderr,
        )
        sys.exit(1)
    CURL_TOOL = curl_bin

    _checked = True
