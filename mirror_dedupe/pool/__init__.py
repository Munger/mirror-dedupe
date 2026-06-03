## @file __init__.py
##
## @brief Shared content-addressable pool for deduplication.
##
## @copyright Copyright (c) 2026 Tim Hosking
## @see https://github.com/munger
## @par Licence: MIT

from __future__ import annotations

from .inventory import Inventory, build_inventory

__all__ = ["Inventory", "build_inventory"]
