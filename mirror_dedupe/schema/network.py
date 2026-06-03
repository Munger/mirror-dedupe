## @file network.py
##
## @brief Per-repo network policy and configuration.
##
## ``NetworkConfig`` captures durable network-related settings (IPv6
## policy, timeouts, user-agent) while keeping transport implementations
## free of persistence concerns.
##
## @copyright Copyright (c) 2026 Tim Hosking
## @see https://github.com/munger
## @par Licence: MIT

from __future__ import annotations

from typing import Any, Dict

from .node import Node


class NetworkConfig(Node):
    ## @brief Per-repo network policy and configuration.
    ##
    ## Captures durable network-related settings (IPv6 policy, timeouts,
    ## user-agent, etc.) while keeping transport implementations (HTTP,
    ## etc.) free of persistence concerns.  Runtime-only state such as
    ## connection pools lives on the respective clients, not here.

    _restore_via_payload = True

    def __init__(
        self,
        *,
        ipv6_ok: bool = True,
        timeout: float | None = None,
        user_agent: str | None = None,
    ) -> None:
        ## @brief Initialise a NetworkConfig descriptor.
        ##
        ## @param ipv6_ok    Whether IPv6 is enabled (default ``True``).
        ## @param timeout    Optional connection timeout in seconds.
        ## @param user_agent Optional HTTP User-Agent string.
        ## @return None
        data: Dict[str, Any] = {"ipv6_ok": ipv6_ok}
        if timeout is not None:
            data["timeout"] = timeout
        if user_agent is not None:
            data["user_agent"] = user_agent
        super().__init__(data)
