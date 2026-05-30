"""Message Pipeline — orchestrates intent routing for the WS Bridge main path.

Takes a raw OneBot WS event, runs it through the Phase 2+ intent pipeline
(intent detection → passive intent → observer policies → plugin routing →
target resolution → reply policy), and returns an ExecutionPlan that tells
the EventBus where to route.

Phase 2+ (observer convergence):
  - meta_event / notice_event: self_id_route, silent reply policy
  - message_event: command detection → primary_targets
                    passive intents → observer_targets
                    summary/repeat → observer_targets
                    reply_policy ← all of the above

Phase 3 (Klee Core repeat):
  - repeat_candidate marked in ExecutionPlan.internal_targets by PluginRouter
  - repeat_service called from main.py after pipeline returns
"""

from __future__ import annotations

import logging
from typing import Optional

from app.config import AppConfig, get_config
from app.services.group_activity import GroupActivityTracker
from app.services.intent_router import IntentRouter
from app.services.passive_intent import PassiveIntentDetector, PassiveIntentResult
from app.services.plugin_router import ExecutionPlan, PluginRouter, ReplyPolicy
from app.services.onebot_bridge.types import DownstreamConfig, OneBotEvent
from app.services.reply_gate import NormalizedMessage, ReplyGate, UserProfile

logger = logging.getLogger("klee.pipeline")


# ---------------------------------------------------------------------------
# Raw segment helpers (work on dict segments from event.data["message"])
# ---------------------------------------------------------------------------

def _extract_normalized_text(segments: list[dict]) -> str:
    """Extract text from raw message segments, stripping @mentions."""
    parts: list[str] = []
    for seg in segments:
        if seg.get("type") == "text":
            parts.append(seg.get("data", {}).get("text", ""))
    return "".join(parts).strip()


def _detect_mentions_bot(segments: list[dict], self_id: int) -> bool:
    """Check if any @at segment targets this bot."""
    for seg in segments:
        if seg.get("type") == "at":
            qq = str(seg.get("data", {}).get("qq", ""))
            if qq == "all" or qq == str(self_id):
                return True
    return False


def _extract_raw_text(segments: list[dict]) -> str:
    """Extract raw concatenated text (with @mentions for logging)."""
    parts: list[str] = []
    for seg in segments:
        if seg.get("type") == "text":
            parts.append(seg.get("data", {}).get("text", ""))
    return "".join(parts)


def _make_event_id(event: OneBotEvent) -> str:
    """Create a stable event_id from self_id + timestamp + post_type."""
    event_id = getattr(event, 'event_id', '') or ''
    if event_id:
        return event_id
    time_val = event.data.get("time", 0) or 0
    return f"{time_val}_{event.self_id}"


# ---------------------------------------------------------------------------
# MessagePipeline
# ---------------------------------------------------------------------------

class MessagePipeline:
    """Orchestrates intent routing for OneBot events.

    Lifecycle:
        pipeline = MessagePipeline(
            intent_router=IntentRouter(),
            plugin_router=PluginRouter(...),
            reply_gate=ReplyGate(tracker=GroupActivityTracker()),
            downstream_configs=downstream_configs,
            config=app_config,
        )
        plan = pipeline.process(event)

    Thread-safe: all internal components are read-only or single-event-loop.
    """

    def __init__(
        self,
        intent_router: IntentRouter,
        plugin_router: PluginRouter,
        reply_gate: ReplyGate,
        downstream_configs: dict[str, DownstreamConfig],
        config: Optional[AppConfig] = None,
    ):
        self._intent = intent_router
        self._plugin = plugin_router
        self._gate = reply_gate
        self._ds_configs = downstream_configs
        self._config = config or get_config()
        self._passive = PassiveIntentDetector()

        # Build self_id -> downstream_id map for astrbot routing
        self._self_id_to_ds: dict[int, str] = {}
        for name, cfg in downstream_configs.items():
            if cfg.self_id and cfg.self_id > 0 and cfg.type == "onebot_reverse_ws":
                self._self_id_to_ds[cfg.self_id] = name

        logger.info(
            "MessagePipeline: %d regex rules, %d bq keywords, %d downstreams, "
            "%d self_id mappings, summary_obs=%s astrbot_repeat=%s klee_repeat=%s",
            self._intent.pattern_count,
            self._intent.keyword_count,
            len(downstream_configs),
            len(self._self_id_to_ds),
            self._config.routing.enable_astrbot_summary_observer,
            self._config.routing.enable_astrbot_repeat_plugin,
            self._config.routing.klee_core_repeat.enable,
        )

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    def process(self, event: OneBotEvent) -> ExecutionPlan:
        """Process a raw OneBot event and return a routing plan.

        Args:
            event: Parsed OneBot event from upstream WS.

        Returns:
            ExecutionPlan with concrete targets and strategy.
        """
        # --- Meta events (lifecycle, heartbeat) -> self_id_route ---
        if event.post_type == "meta_event":
            meta_type = event.data.get("meta_event_type", "")
            logger.debug(
                "Pipeline: meta_event=%s self_id=%s -> self_id_route",
                meta_type, event.self_id,
            )
            plan = ExecutionPlan.self_id_route("meta_event")
            plan.event_id = _make_event_id(event)
            plan.event_type = "meta_event"
            return plan

        # --- Notice events -> self_id_route ---
        if event.post_type == "notice":
            logger.debug(
                "Pipeline: notice self_id=%s -> self_id_route",
                event.self_id,
            )
            plan = ExecutionPlan.self_id_route("notice_event")
            plan.event_id = _make_event_id(event)
            plan.event_type = "notice"
            return plan

        # --- Message events -> intent pipeline ---
        if event.post_type == "message":
            return self._process_message(event)

        # Unknown post_type -> self_id_route (safe fallback)
        logger.warning(
            "Pipeline: unknown post_type=%s self_id=%s -> self_id_route fallback",
            event.post_type, event.self_id,
        )
        plan = ExecutionPlan.self_id_route("unknown_post_type")
        plan.event_id = _make_event_id(event)
        return plan

    # ------------------------------------------------------------------
    # Message processing
    # ------------------------------------------------------------------

    def _process_message(self, event: OneBotEvent) -> ExecutionPlan:
        """Run full intent + passive + observer routing on a message event."""
        self_id = event.self_id
        segments: list[dict] = event.data.get("message", [])
        group_id = event.data.get("group_id", 0)
        user_id = event.data.get("user_id", 0)
        raw_message = event.data.get("raw_message", "")

        # Extract text + mentions
        text = _extract_normalized_text(segments)
        raw_text = _extract_raw_text(segments) or raw_message or ""
        mentions_bot = _detect_mentions_bot(segments, self_id)

        # ── 1. Command intent detection ──
        intent = self._intent.detect(text, mentions_bot)

        # ── 2. Passive intent detection ──
        passive: PassiveIntentResult = PassiveIntentResult()
        if self._config.routing.enable_passive_intents:
            passive = self._passive.detect(event)

        # ── 3. Plugin routing (command or observer plan) ──
        #     Phase 3: PluginRouter internally handles Klee Core repeat
        #     candidate marking (internal_targets=["klee_core.repeat"])
        plan = self._plugin.route(intent, mentions_bot, passive, event.data)

        # ── 4. Resolve abstract handler -> concrete downstream_ids ──
        if plan.primary_handler == "astrbot" and not plan.primary_targets:
            # Resolve astrbot target by self_id
            ds_name = self._self_id_to_ds.get(self_id)
            if ds_name and ds_name in self._ds_configs:
                plan.primary_targets = [ds_name]
                if plan.reply_policy and not plan.reply_policy.allow_targets:
                    plan.reply_policy.allow_targets = [ds_name]
            else:
                astrbots = [
                    n for n, c in self._ds_configs.items()
                    if n.startswith("astrbot") and c.type == "onebot_reverse_ws"
                ]
                plan.primary_targets = astrbots
                if plan.reply_policy:
                    plan.reply_policy.allow_targets = astrbots

        if plan.primary_handler == "yunzai" and not plan.primary_targets:
            if "yunzai" in self._ds_configs:
                plan.primary_targets = ["yunzai"]

        # ── 5. Resolve observer targets ──
        plan.observer_targets = [
            t for t in plan.observer_targets
            if t in self._ds_configs
        ]

        # ── 6. ReplyGate for non-command messages with @mention (Hermes trigger) ──
        if not intent.is_command and mentions_bot:
            gate_result = self._gate.score(
                NormalizedMessage(
                    text=text,
                    mentions_bot=mentions_bot,
                    is_reply_to_bot=False,
                    user_id=user_id,
                    group_id=group_id,
                ),
                user=UserProfile(user_id=user_id, favor_score=0.0),
            )

            logger.info(
                "ReplyGate: score=%d should=%s trigger=%s reasons=%s",
                gate_result.score,
                gate_result.should_reply,
                gate_result.trigger_level,
                gate_result.reason,
            )

            if gate_result.should_reply:
                plan.primary_handler = "hermes"
                if "hermes" in self._ds_configs:
                    plan.primary_targets = ["hermes"]
                    plan.strategy = "single"
                    plan.call_hermes = True
                    plan.reply_policy = ReplyPolicy(
                        mode="exclusive",
                        allow_targets=["hermes"],
                        reason="reply_gate_triggered",
                    )
                plan.route_reason = "reply_gate_triggered"
                self._gate.record_reply(group_id)

        # ── 7. Finalize targets ──
        plan.build_legacy_targets()

        # ── 8. Set event_id ──
        plan.event_id = _make_event_id(event)

        # ── 9. Route decision log ──
        self._log_route_decision(event, plan, intent, passive, raw_text)

        # ── 10. Activity tracking ──
        is_media = bool(
            segments and
            segments[0].get("type") in ("image", "record", "video")
            and len(segments) == 1
        )
        self._gate.record_message(group_id, is_media=is_media)

        return plan

    # ------------------------------------------------------------------
    # Logging
    # ------------------------------------------------------------------

    def _log_route_decision(
        self,
        event: OneBotEvent,
        plan: ExecutionPlan,
        intent,
        passive: PassiveIntentResult,
        raw_text: str,
    ):
        """Log ROUTE-DECISION with all new fields."""
        cmd = plan.command_intent or ("command->" + intent.primary_handler if intent.is_command else "none")
        passives = ",".join(plan.passive_intents) if plan.passive_intents else "none"
        rp = plan.reply_policy
        rp_mode = rp.mode if rp else "none"
        rp_allow = ",".join(rp.allow_targets) if rp and rp.allow_targets else "none"
        rp_suppress = ",".join(rp.suppress_targets) if rp and rp.suppress_targets else "none"

        logger.warning(
            "ROUTE-DECISION: event_id=%s self_id=%s group=%s user=%s "
            "raw=%.80s cmd=%s passive=[%s] "
            "primary=%s observer=%s internal=%s "
            "reply_mode=%s reply_allow=%s reply_suppress=%s reason=%s",
            plan.event_id,
            event.self_id,
            event.data.get("group_id", 0),
            event.data.get("user_id", 0),
            raw_text,
            cmd,
            passives,
            plan.primary_targets,
            plan.observer_targets,
            plan.internal_targets,
            rp_mode,
            rp_allow,
            rp_suppress,
            plan.route_reason,
        )

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def gate(self) -> ReplyGate:
        return self._gate


# ---------------------------------------------------------------------------
# Global singleton
# ---------------------------------------------------------------------------

_pipeline: Optional[MessagePipeline] = None


def get_pipeline() -> MessagePipeline:
    if _pipeline is None:
        raise RuntimeError("MessagePipeline not initialized — call set_pipeline() during startup")
    return _pipeline


def set_pipeline(p: MessagePipeline):
    global _pipeline
    _pipeline = p
