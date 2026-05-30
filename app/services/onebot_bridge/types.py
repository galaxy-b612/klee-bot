"""OneBot v11 Bridge types — shared data structures."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


def _make_event_id(data: dict) -> str:
    """Build unique event_id. For message events use message_id.
    For meta events (lifecycle, heartbeat) combine time + self_id so
    multiple bots' lifecycle events in the same second are NOT deduped.
    """
    msg_id = data.get("message_id", "")
    if msg_id:
        return str(msg_id)
    time_val = data.get("time", "")
    self_id = data.get("self_id", "")
    if self_id:
        return f"{time_val}_{self_id}"
    return str(time_val)


@dataclass
class OneBotEvent:
    """A received OneBot v11 event."""
    raw_json: str
    data: dict[str, Any]
    self_id: int
    post_type: str = ""
    event_id: str = ""

    @staticmethod
    def from_json(raw: str | bytes) -> OneBotEvent:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        data = json.loads(raw)
        return OneBotEvent(
            raw_json=raw,
            data=data,
            self_id=data.get("self_id", 0),
            post_type=data.get("post_type", ""),
            event_id=_make_event_id(data),
        )


@dataclass
class OneBotAPIRequest:
    """An API action request from a downstream service."""
    action: str
    params: dict[str, Any] = field(default_factory=dict)
    echo: Any = ""

    @staticmethod
    def from_json(raw: str | bytes) -> OneBotAPIRequest | None:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        data = json.loads(raw)
        if "action" not in data:
            return None  # not an API request (probably an event)
        return OneBotAPIRequest(
            action=data["action"],
            params=data.get("params", {}),
            echo=data.get("echo", ""),
        )


@dataclass
class OneBotAPIResponse:
    """An API response to return to a downstream service."""
    status: str = "ok"
    retcode: int = 0
    data: Any = None
    echo: Any = ""
    wording: str = ""

    def to_json(self) -> str:
        result: dict[str, Any] = {
            "status": self.status,
            "retcode": self.retcode,
            "data": self.data,
        }
        if self.echo or self.echo == 0:
            # Preserve echo in its original type (dict, int, str, etc.)
            # AstrBot/aiocqhttp expects echo={"seq": N} as a dict,
            # not its str() representation
            result["echo"] = self.echo
        if self.wording:
            result["wording"] = self.wording
        return json.dumps(result, ensure_ascii=False)


@dataclass
class DownstreamConfig:
    """Configuration for one downstream service."""
    name: str
    type: str           # "onebot_reverse_ws" | "http"
    url: str
    access_token: str = ""
    self_id: int = 0    # QQ number for X-Self-ID header
    enabled: bool = True
