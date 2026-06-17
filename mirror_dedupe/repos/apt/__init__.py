## @file __init__.py
##
## @brief APT repository implementation for mirror-dedupe.
##
## @copyright Copyright (c) 2026 Tim Hosking
## @see https://github.com/munger
## @par Licence: MIT

from __future__ import annotations

from .apt import Apt

__all__ = ["Apt"]
