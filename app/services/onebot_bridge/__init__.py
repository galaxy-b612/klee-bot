"""OneBot v11 WebSocket Bridge for Klee Core.

Transparent proxy between NapCat (upstream) and AstrBot/Yunzai (downstream).

Phase 1: Transparent proxy — all events broadcast to all downstreams.
Phase 2: Semi-transparent — IntentRouter + ReplyGate control routing.
"""

from app.services.onebot_bridge.types import (
    DownstreamConfig,
    OneBotAPIRequest,
    OneBotAPIResponse,
    OneBotEvent,
)
from app.services.onebot_bridge.upstream import UpstreamNapCatClient
from app.services.onebot_bridge.downstream import DownstreamOneBotWSClient
from app.services.onebot_bridge.event_bus import OneBotEventBus
from app.services.onebot_bridge.api_proxy import OneBotAPIProxy

__all__ = [
    "DownstreamConfig",
    "OneBotEvent",
    "OneBotAPIRequest",
    "OneBotAPIResponse",
    "UpstreamNapCatClient",
    "DownstreamOneBotWSClient",
    "OneBotEventBus",
    "OneBotAPIProxy",
]
