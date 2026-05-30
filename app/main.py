"""Klee Core — FastAPI application entry point with OneBot WS Bridge.

Phase 2+ (observer convergence):
  - WS Bridge is the primary event path (NapCat -> Klee Core -> AstrBot/Yunzai)
  - MessagePipeline handles intent + passive + observer routing inline
  - EventBus routes selectively based on ExecutionPlan
  - API Proxy provides Reply Arbiter for send actions
  - Flow: pipeline → api_proxy.record_event → event_bus.route_event → DB insert

Phase 3 (Klee Core repeat):
  - KleeCoreRepeatService tracks recent messages and handles internal repeat
  - Flow extended: pipeline → repeat_service.add_message →
                   repeat_service.should_repeat → repeat_service.send_repeat →
                   api_proxy.record_event → event_bus.route_event → DB insert
"""

from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from sqlalchemy import select

from app import __version__
from app.config import DownstreamTransportConfig, get_config, load_config
from app.database import _get_session_factory, init_db
from app.models import BotInstance, Message
from app.routers import health, messages, onebot
from app.services.forward_service import close_client
from app.services.group_activity import GroupActivityTracker
from app.services.intent_router import IntentRouter
from app.services.message_pipeline import MessagePipeline, set_pipeline
from app.services.passive_intent import PassiveIntentDetector
from app.services.plugin_router import PluginRouter
from app.services.repeat_service import KleeCoreRepeatService
from app.services.reply_gate import ReplyGate
from app.services.onebot_bridge.diagnostics import (
    log_downstream_request,
    log_upstream_forward,
    log_upstream_response,
)
from app.services.onebot_bridge import (
    DownstreamConfig,
    OneBotAPIRequest,
    OneBotAPIProxy,
    OneBotEvent,
    OneBotEventBus,
    UpstreamNapCatClient,
)


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def _setup_logging():
    cfg = load_config()
    log_cfg = cfg.logging
    logging.basicConfig(
        level=getattr(logging, log_cfg.level.upper(), logging.INFO),
        format=log_cfg.format,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
    logging.getLogger("websockets").setLevel(logging.WARNING)


# ---------------------------------------------------------------------------
# Bridge globals
# ---------------------------------------------------------------------------

_upstream: UpstreamNapCatClient | None = None
_event_bus: OneBotEventBus | None = None
_api_proxy: OneBotAPIProxy | None = None
_pipeline: MessagePipeline | None = None
_repeat_service: KleeCoreRepeatService | None = None

logger = logging.getLogger("klee")


# ---------------------------------------------------------------------------
# Event handler — called when NapCat pushes an event via WS
# ---------------------------------------------------------------------------

async def _on_upstream_event(event: OneBotEvent, source: str):
    """Handle a OneBot event from NapCat.

    Phase 3 flow (reordered for repeat):
        1. UPSTREAM-EVENT log
        2. pipeline.process(event)
        3. Klee Core repeat check (add to cache → should_repeat → send)
        4. api_proxy.record_event(event, plan)
        5. event_bus.route_event(event, plan)
        6. DB insert (non-blocking)
    """
    try:
        # ── 1. UPSTREAM-EVENT log ──
        event_id = event.event_id if hasattr(event, 'event_id') else ''
        logger.warning(
            "UPSTREAM-EVENT: self_id=%s post_type=%s meta=%s id=%s",
            event.self_id, event.post_type,
            event.data.get("meta_event_type", ""), event_id,
        )

        # Filter out events from bot accounts to prevent circular triggering
        bot_self_ids = {1432028231, 3750475856, 1825905764}
        sender_id = int(event.data.get("user_id", 0) or 0)
        if sender_id in bot_self_ids:
            logger.debug(
                "MSG-FILTER: skipping bot self-message sender=%s self=%s",
                sender_id, event.self_id,
            )
            return

        # ── 2. Intent pipeline ──
        plan = None
        if _pipeline and _event_bus:
            plan = _pipeline.process(event)

        # ── 3. Klee Core repeat check ──
        if _repeat_service and plan and "klee_core.repeat" in plan.internal_targets:
            _repeat_service.add_message(event, plan)
            decision = _repeat_service.should_repeat(event)
            if decision.should_repeat:
                # Send repeat via internal NapCat HTTP API (bypasses WS + Reply Arbiter)
                await _repeat_service.send_repeat(event, decision)

        # ── 4. Record event context for Reply Arbiter (BEFORE route_event) ──
        if _api_proxy:
            _api_proxy.record_event(event, plan)

        # ── 5. Route to downstreams ──
        if _event_bus:
            results = await _event_bus.route_event(event, plan)

            if plan is None:
                logger.warning(
                    "ROUTE-EVENT: self_id=%s post_type=%s (no pipeline, self_id_route)",
                    event.self_id, event.post_type,
                )
            else:
                logger.warning(
                    "ROUTE-EVENT: self_id=%s post_type=%s strategy=%s targets=%s results=%s",
                    event.self_id, event.post_type,
                    plan.strategy, plan.send_targets,
                    {k: v for k, v in results.items()},
                )

        # ── 6. DB insert (non-blocking, fire-and-forget) ──
        try:
            factory = _get_session_factory()
            async with factory() as session:
                msg = Message(
                    platform_message_id=int(event.event_id) if (event.event_id and event.event_id.isdigit()) else None,
                    bot_id="",
                    group_id=event.data.get("group_id", 0),
                    user_id=event.data.get("user_id", 0),
                    nickname=event.data.get("sender", {}).get("nickname", ""),
                    message_type=event.post_type,
                    text_content=event.data.get("raw_message", ""),
                    normalized_content=event.data.get("raw_message", ""),
                    mentions_bot=False,
                    raw_event_json=event.raw_json,
                )
                session.add(msg)
                await session.commit()
        except Exception:
            logger.exception("DB insert failed for event (non-fatal)")

    except Exception:
        logger.exception("on_upstream_event failed for event_id=%s", event.event_id)


def _make_downstream_config(dc: DownstreamTransportConfig, self_id: int = 0) -> DownstreamConfig:
    """Convert config model to bridge DownstreamConfig."""
    return DownstreamConfig(
        name=dc.name,
        type=dc.type,
        url=dc.url,
        access_token=dc.access_token,
        self_id=self_id,
        enabled=dc.enabled,
    )


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _upstream, _event_bus, _api_proxy, _pipeline, _repeat_service

    # Startup
    _setup_logging()
    cfg = load_config()
    logger.info("Klee Core v%s starting — %d bots, %d downstreams",
                __version__, len(cfg.bots), len(cfg.downstreams))

    await init_db()

    # Build self_id -> napcat_http_url mapping
    _bot_http_map: dict[int, str] = {}
    for bot in cfg.bots:
        if bot.napcat_http_url:
            _bot_http_map[bot.self_id] = bot.napcat_http_url

    # Start upstream WS server
    _upstream = UpstreamNapCatClient(
        host="0.0.0.0",
        port=8099,
        path="/onebot/v11",
        napcat_http_url="http://127.0.0.1:5700",
        napcat_token="notification_bot_2024",
        on_event=_on_upstream_event,
    )
    await _upstream.start()
    logger.info("Upstream WS server started on :8099")

    # Create event bus + API proxy
    _event_bus = OneBotEventBus()
    _api_proxy = OneBotAPIProxy(upstream=_upstream, event_bus=_event_bus, bot_http_map=_bot_http_map)
    _event_bus._on_api_request = _api_proxy.handle_api  # type: ignore

    # Connect to all configured downstreams
    for name, dc in cfg.downstreams.items():
        ds_config = _make_downstream_config(dc, self_id=dc.self_id)
        await _event_bus.add_downstream(ds_config)

    logger.info("Klee Core ready — %d downstreams connected", _event_bus.client_count)

    # Initialize KleeCoreRepeatService (Phase 3)
    _repeat_service = KleeCoreRepeatService(
        config=cfg.routing.klee_core_repeat,
        upstream_call_api=_upstream.call_api,
    )
    logger.info(
        "KleeCoreRepeatService initialized — enabled=%s groups=%s threshold=%d window=%d cooldown=%ds",
        cfg.routing.klee_core_repeat.enable,
        cfg.routing.klee_core_repeat.groups,
        cfg.routing.klee_core_repeat.threshold,
        cfg.routing.klee_core_repeat.window,
        cfg.routing.klee_core_repeat.cooldown_seconds,
    )

    # Initialize MessagePipeline with observer config
    routing = cfg.routing
    _pipeline = MessagePipeline(
        intent_router=IntentRouter(),
        plugin_router=PluginRouter(
            observer_links_enabled=routing.enable_astrbot_observer_links,
            observer_cards_enabled=routing.enable_astrbot_observer_cards,
            link_parser_targets=routing.link_parser_targets,
            card_parser_targets=routing.card_parser_targets,
            link_parser_cooldown=routing.link_parser_cooldown_seconds,
            card_parser_cooldown=routing.card_parser_cooldown_seconds,
            summary_observer_enabled=routing.enable_astrbot_summary_observer,
            summary_observer_targets=routing.summary_observer_targets,
            summary_observer_groups=set(routing.summary_observer_groups),
            repeat_observer_enabled=routing.enable_astrbot_repeat_plugin,
            repeat_observer_targets=routing.repeat_observer_targets,
            repeat_observer_groups=set(routing.repeat_observer_groups),
            observer_reply_cooldown=routing.observer_reply_cooldown_seconds,
            # Phase 3: Klee Core repeat
            klee_repeat_enabled=routing.klee_core_repeat.enable,
            klee_repeat_groups=set(routing.klee_core_repeat.groups),
            klee_repeat_cfg=routing.klee_core_repeat,
        ),
        reply_gate=ReplyGate(tracker=GroupActivityTracker()),
        downstream_configs=_event_bus.configs,
        config=cfg,
    )
    set_pipeline(_pipeline)
    logger.info("MessagePipeline initialized with observer config")

    yield

    # Shutdown
    logger.info("Klee Core shutting down")
    if _event_bus:
        await _event_bus.stop_all()
    if _upstream:
        await _upstream.stop()
    await close_client()


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Klee Core",
    description="Unified message dispatch layer with OneBot WS Bridge",
    version=__version__,
    lifespan=lifespan,
)

app.include_router(health.router)
app.include_router(onebot.router)
app.include_router(messages.router)

# ---------------------------------------------------------------------------
# HTTP API fallback
# ---------------------------------------------------------------------------

@app.post("/onebot/v11/{action:path}")
async def http_api_proxy(action: str, request: Request):
    """Receive API calls via HTTP POST (fallback when WS fails for large payloads)."""
    global _api_proxy

    if _api_proxy is None:
        return {"status": "failed", "retcode": 1, "wording": "bridge not ready"}

    body = await request.json()

    log_downstream_request(json.dumps({
        "action": action,
        "params": body,
        "echo": body.get("echo", ""),
    }), "http")

    self_id = body.get("self_id", 0)
    try:
        self_id = int(self_id)
    except (TypeError, ValueError):
        self_id = 0
    if not self_id:
        group_id = body.get("group_id", 0)
        self_id = _api_proxy._group_bot.get(group_id, 0)
    if self_id and self_id in _api_proxy._bot_http_map:
        correct_url = _api_proxy._bot_http_map[self_id]
        if correct_url != _api_proxy._upstream.napcat_http_url:
            logger.warning("ROUTING: action=%s self_id=%s -> %s (source=http)", action, self_id, correct_url)
            _api_proxy._upstream.napcat_http_url = correct_url
    else:
        logger.warning("ROUTING-FAIL: action=%s self_id=%s not in _bot_http_map (source=http)", action, self_id)

    api_request = OneBotAPIRequest(action=action, params=body)
    log_upstream_forward(api_request.params, action, str(body.get("echo", "")))
    response = await _api_proxy._upstream.call_api(api_request)
    log_upstream_response(response, action, str(body.get("echo", "")))

    return json.loads(response.to_json())


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True, log_level="info")
