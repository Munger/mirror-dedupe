from __future__ import annotations

from typing import Any, Dict

from .node import Node


class NetworkConfig(Node):
    """Per-repo network policy/configuration.

    This captures durable network-related settings (IPv6 policy, timeouts,
    user-agent, etc.) while keeping transport implementations (HTTP, rsync,
    etc.) free of persistence concerns. Runtime-only state such as
    connection pools lives on the respective clients, not here.
    """

    _restore_via_payload = True

    def __init__(
        self,
        *,
        ipv6_ok: bool = True,
        timeout: float | None = None,
        user_agent: str | None = None,
    ) -> None:
        data: Dict[str, Any] = {"ipv6_ok": ipv6_ok}
        if timeout is not None:
            data["timeout"] = timeout
        if user_agent is not None:
            data["user_agent"] = user_agent
        super().__init__(data)
