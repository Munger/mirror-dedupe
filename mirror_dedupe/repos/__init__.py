"""Repository ecosystem implementations (APT, Yum, etc.).

This package provides concrete Repo implementations (e.g. Apt) and
re-exports the shared schema ``Repo`` type for convenience.
"""

from mirror_dedupe.schema import Repo  # re-export schema.Repo

__all__ = ["Repo"]
