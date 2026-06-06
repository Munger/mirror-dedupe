#!/usr/bin/env python3
## @file repo_vars.py
##
## @brief Per-repo variables shared across all nodes in a repo tree.
##
## ``RepoVars`` holds everything that every child node needs access to:
## the per-repo inventory, the global pool inventory, network settings,
## root paths, and sync mode.  A single ``RepoVars`` instance is created
## per repo and shared via ``Node._repo_vars`` through the entire tree.
##
## No node should ever call ``Config.load()`` — all runtime state lives
## here, propagated automatically during tree construction.
##
## @copyright Copyright (c) 2026 Tim Hosking
## @see https://github.com/munger
## @par Licence: MIT

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .inventory import Inventory


@dataclass
class RepoVars:
    ## @brief Per-repo variables passed to every node in the schema tree.
    ##
    ## ``inv``     — the per-repo ``Inventory`` (hash→inode for files
    ##               hardlinked into this repo's dest directory).
    ## ``pool_inv`` — the global pool ``Inventory`` (hash→inode for every
    ##                file in the content-addressed pool).
    ## ``ipv6_ok``  — whether IPv6 is enabled for this repo.
    ## ``repo_root`` — root of the repository tree on disk.
    ## ``pool_root`` — root of the content-addressed pool on disk.
    ## ``sync_mode`` — ``True`` during a sync run (set on the specific
    ##                 repo before sync begins, cleared after).

    inv: Optional[Inventory] = None
    pool_inv: Optional[Inventory] = None
    ipv6_ok: bool = True
    repo_root: str = ""
    pool_root: str = ""
    sync_mode: bool = False
