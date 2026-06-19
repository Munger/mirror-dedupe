## @file codenames.py
##
## @brief Helpers for working with Debian/Ubuntu codenames.
##
## This module currently provides a thin wrapper around the
## ``scripts/list-codenames.sh`` helper so Python code can obtain the
## unified set of Debian/Ubuntu short codenames ("series" column) as a
## plain list.
##
## @copyright Copyright (c) 2026 Tim Hosking
## @see https://github.com/munger
## @par Licence: MIT


from pathlib import Path
from subprocess import run, CalledProcessError
from typing import List


def apt_codenames() -> List[str]:
    ## @brief Return a sorted list of unique Debian/Ubuntu short codenames.
    ##
    ## This calls ``scripts/list-codenames.sh`` from the project root and
    ## returns its output as a list of strings (one codename per element),
    ## with empty lines automatically filtered out.
    ##
    ## @return A list of codename strings.

    # Resolve the project root as the parent of this file's directory.
    here = Path(__file__).resolve()
    project_root = here.parents[2]
    script = project_root / "scripts" / "list-codenames.sh"

    result = run([str(script)], check=True, capture_output=True, text=True)
    lines = [line.strip() for line in result.stdout.splitlines()]
    return [ln for ln in lines if ln]
