"""Repository ecosystem implementations (APT, Yum, etc.).

Importing this package registers all built-in Repo implementations with
the shared :class:`mirror_dedupe.schema.repo.Repo` registry so that
``Repo.from_url(...)`` can auto-detect the appropriate concrete type
via ``is_this_yours()``.
"""

from mirror_dedupe.schema import Repo  # re-export schema.Repo
from mirror_dedupe.repos.apt.apt import Apt  # noqa: F401  # register Apt
from mirror_dedupe.repos.apt_vendor import AptVendor  # noqa: F401  # register AptVendor

__all__ = ["Repo", "Apt", "AptVendor"]
