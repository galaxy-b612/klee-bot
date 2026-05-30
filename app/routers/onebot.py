"""OneBot event receiver router — HTTP debug endpoint.

POST /onebot/event — Receives OneBot v11 message events from NapCat via HTTP.
This is a DEBUG / testing endpoint, NOT the primary production path.

Production events flow through the WS Bridge (main.py -> upstream.py ->
MessagePipeline -> EventBus -> downstream WS).

This endpoint uses the shared MessagePipeline for consistent routing behavior.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_bot_by_self_id
from app.database import get_session
from app.schemas.onebot import OneBotEvent as PydanticOneBotEvent
from app.services.forward_service import schedule_forward
from app.services.group_activity import GroupActivityTracker
from app.services.message_pipeline import MessagePipeline, get_pipeline
from app.services.plugin_router import ExecutionPlan
from app.services.onebot_bridge.types import OneBotEvent as BridgeEvent
from app.services.reply_gate import NormalizedMessage, ReplyGate, UserProfile

logger = logging.getLogger(__name__)

router = APIRouter(tags=["onebot"])


@router.post("/onebot/event", status_code=status.HTTP_200_OK)
async def receive_onebot_event(
    request: Request,
    event: PydanticOneBotEvent,
    session: AsyncSession = Depends(get_session),
):
    """HTTP debug entry for OneBot events. Uses shared MessagePipeline.

    This endpoint validates and routes events through the same intent pipeline
    as the production WS Bridge, but uses HTTP POST instead of WebSocket.
    Useful for testing and debugging.
    """
    # --- Validation ---

    if event.post_type != "message":
        logger.debug("Ignoring non-message event: post_type=%s", event.post_type)
        return {"status": "ignored", "reason": "post_type=" + event.post_type}

    if event.message_type not in ("group", "private"):
        logger.debug("Ignoring message_type=%s", event.message_type)
        return {"status": "ignored", "reason": "message_type=" + event.message_type}

    bot = get_bot_by_self_id(event.self_id)
    if bot is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No enabled bot found for self_id=" + str(event.self_id),
        )

    if event.message_type == "group" and event.group_id:
        if event.group_id not in bot.groups:
            return {
                "status": "ignored",
                "reason": "group " + str(event.group_id) + " not managed by " + bot.bot_id,
            }

    # --- Build Bridge-compatible event for pipeline ---
    raw_json = event.model_dump_json(exclude_none=True)
    bridge_event = BridgeEvent(
        raw_json=raw_json,
        data=event.model_dump(exclude_none=True),
        self_id=event.self_id,
        post_type=event.post_type,
        event_id=str(event.message_id or 0),
    )

    # --- Run through shared MessagePipeline ---
    pipeline = get_pipeline()
    plan = pipeline.process(bridge_event)

    # --- Build event payload for HTTP forwarding (Hermes only) ---
    event_payload = event.model_dump(exclude_none=True, by_alias=False)
    event_payload["_klee_bot_id"] = bot.bot_id
    event_payload["_klee_primary_handler"] = plan.primary_handler

    # --- HTTP forwarding for Hermes (if triggered) ---
    reply_gate_result = None
    if plan.call_hermes:
        schedule_forward(event_payload, bot, targets=["hermes"])
        logger.info("HTTP debug: routing to Hermes via forward_service")

    # --- Respond with routing decision ---
    is_command = plan.primary_handler not in ("none", "__broadcast__", "__self_id_route__")
    return {
        "status": "ok",
        "bot_id": bot.bot_id,
        "message_type": event.message_type,
        "routing": {
            "is_command": is_command,
            "primary_handler": plan.primary_handler,
            "targets": plan.targets,
            "strategy": plan.strategy,
            "call_hermes": plan.call_hermes,
            "reason": plan.reason,
        },
    }
