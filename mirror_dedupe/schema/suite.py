## @file suite.py
##
## @brief Suite descriptor and collection.
##
## A ``Suite`` models a single logical suite label such as ``"noble"`` or
## ``"jammy"``.  ``Suites`` is the corresponding ``NodeList`` wrapper.
##
## @copyright Copyright (c) 2026 Tim Hosking
## @see https://github.com/munger
## @par Licence: MIT

from __future__ import annotations

from typing import Any, Dict

from .mdnode import MDNode as Node
from ..lib.node_x import SerialisableNodeList as NodeList


class Suite(Node):
    ## @brief Single suite label (e.g. ``"noble"``, ``"jammy"``).

    _restore_via_payload = True

    def __init__(self, *, name: str) -> None:
        ## @brief Initialise a Suite descriptor.
        ##
        ## @param name  The suite label (e.g. ``"noble"``).
        ## @return None
        data: Dict[str, Any] = {"name": name}
        super().__init__(data)


class Suites(NodeList[Suite]):
    ## @brief Collection of Suite nodes.
    ##
    ## This models the set of logical suites discovered for a repository
    ## (e.g. ``"noble"``, ``"jammy"``).  It does not encode pockets;
    ## those can be layered on separately in other nodes.
    pass
