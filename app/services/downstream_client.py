"""Downstream Client — abstract interface and mock implementation.

Phase 2 uses a mock client for testing. Real HTTP forwarding will replace
the mock in Phase 3 once the execution plan is stable.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class DownstreamClient(ABC):
    """Abstract interface for forwarding messages to downstream services.

    Each handler (astrbot, yunzai, hermes, meme) is a downstream service
    reachable via the bot's configured endpoint URL.
    """

    @abstractmethod
    async def send_message(
        self,
        event_payload: dict[str, Any],
        endpoint_url: str,
        bot_id: str,
    ) -> bool:
        """Forward a message event to a downstream endpoint.

        Args:
            event_payload: Full OneBot event dict (with _klee_* metadata).
            endpoint_url: Target service URL.
            bot_id: Originating bot identifier.

        Returns:
            True if the message was sent successfully.
        """
        ...


class MockDownstreamClient(DownstreamClient):
    """Mock downstream client that records sent messages for test assertions.

    Messages are appended to `sent_messages` list. No actual HTTP calls.
    """

    def __init__(self):
        self.sent_messages: list[dict[str, Any]] = []

    async def send_message(
        self,
        event_payload: dict[str, Any],
        endpoint_url: str,
        bot_id: str,
    ) -> bool:
        self.sent_messages.append({
            "payload": event_payload,
            "endpoint": endpoint_url,
            "bot_id": bot_id,
        })
        return True

    def clear(self):
        """Reset recorded messages."""
        self.sent_messages.clear()

    def messages_to(self, endpoint_pattern: str) -> int:
        """Count messages sent to endpoints matching a substring."""
        return sum(
            1 for m in self.sent_messages
            if endpoint_pattern in m["endpoint"]
        )
