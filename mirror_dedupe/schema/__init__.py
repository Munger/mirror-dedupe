## @file __init__.py
##
## @brief Schema types for the mirror-dedupe repository model.
##
## @copyright Copyright (c) 2026 Tim Hosking
## @see https://github.com/munger
## @par Licence: MIT

from __future__ import annotations

from .node import Node
from .repo import Repo, Repos
from .architecture import Architecture, Architectures
from .component import Component, Components
from .suite import Suite, Suites
from .distribution import Distribution, Distributions
from .index import Index, VariantIndex, Indices
from .package import Package, Packages
from .release import Release, Releases
from .upstream import Upstream, Upstreams
from .vars import Vars
