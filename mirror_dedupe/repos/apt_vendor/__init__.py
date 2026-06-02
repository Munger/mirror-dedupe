## @file __init__.py
##
## @brief Vendor-flavoured APT Repo implementation.
##
## @copyright Copyright (c) 2026 Tim Hosking
## @see https://github.com/munger
## @par Licence: MIT

from __future__ import annotations

from .apt_vendor import AptVendor

__all__ = ["AptVendor"]
