## @file component.py
##
## @brief Component descriptor and collection.
##
## A ``Component`` represents a single component label such as ``main``
## or ``universe``.  ``Components`` is the corresponding ``NodeList``
## wrapper.
##
## @copyright Copyright (c) 2026 Tim Hosking
## @see https://github.com/munger
## @par Licence: MIT

from __future__ import annotations

from typing import Any, Dict

from .node import Node, NodeList


class Component(Node):
    ## @brief Dict-backed descriptor for a single component label.
    ##
    ## As with Architecture, this is a hook for future per-component
    ## metadata while keeping Repo JSON-shaped.

    _restore_via_payload = True

    def __init__(
        self,
        *,
        name: str,
        metadata: Dict[str, Any] | None = None,
    ) -> None:
        data: Dict[str, Any] = {"name": name}
        if metadata is not None:
            data["metadata"] = metadata
        super().__init__(data)


class Components(NodeList[Component]):
    ## @brief Container for Component descriptors.
    ##
    ## This is just a plain list of ``Component`` nodes.  Any schema or
    ## metadata lives either on the individual ``Component`` instances or
    ## on the parent repo, not on this list type itself.
    pass
