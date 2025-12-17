from __future__ import annotations

from typing import Any, Dict

from .node import Node, NodeList
from .component import Components
from .architecture import Architectures


class Distribution(Node):
    """Per-distribution node.

    Represents a logical suite or suite/pocket entry discovered under
    ``/dists`` for repo types that follow an APT-like layout. Captures
    high-level facts about the suite and the location/shape of its
    Release-like metadata.
    """

    # On restore we want to seed the underlying mapping directly from the
    # snapshot payload, bypassing the keyword-only constructor.
    _restore_via_payload = True

    def __init__(
        self,
        *,
        name: str,
        has_release: bool,
        components: Components,
        architectures: Architectures,
        release_url: str,
        release_path: str,
        metadata: "Distribution.Metadata" | None = None,
    ) -> None:
        data: Dict[str, Any] = {
            "name": name,
            "has_release": has_release,
            "components": components,
            "architectures": architectures,
            "release_url": release_url,
            "release_path": release_path,
        }
        if metadata is not None:
            data["metadata"] = metadata
        super().__init__(data)

    class Metadata(Node):
        """Per-distribution metadata envelope.

        Repo-specific implementations (e.g. APT Release metadata)
        should subclass this type to make their ownership explicit
        while keeping the outer Distribution envelope generic.
        """

        def __init__(self, **fields: Any) -> None:
            super().__init__(dict(fields))


class Distributions(NodeList[Distribution]):
    """Container for Distribution descriptors.

    This is just a plain list of ``Distribution`` nodes. Any schema or
    metadata lives either on the individual ``Distribution`` instances
    or on the parent repo, not on this list type itself.
    """
