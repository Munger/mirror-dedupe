## @file __init__.py
##
## @brief Repository-type implementations (APT, Yum, etc.).
##
## Importing this package registers all built-in Repo implementations
## with the shared ``Repo`` registry so that ``Repo.from_url(...)`` can
## auto-detect the appropriate concrete type via ``is_this_yours()``.
##
## @copyright Copyright (c) 2026 Tim Hosking
## @see https://github.com/munger
## @par Licence: MIT


from mirror_dedupe.schema import Repo
from mirror_dedupe.repos.apt.apt import Apt  # noqa: F401  # register Apt

__all__ = ["Repo", "Apt"]
