## @file __init__.py
##
## @brief Shared content-addressable pool for deduplication.
##
## @copyright Copyright (c) 2026 Tim Hosking
## @see https://github.com/munger
## @par Licence: MIT

from __future__ import annotations

from .fetcher import Fetcher
from .inventory import Inventory, build_inventory, get_inventory

__all__ = ["Fetcher", "Inventory", "build_inventory", "get_inventory"]
