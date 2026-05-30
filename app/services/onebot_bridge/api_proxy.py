"""API Proxy — routes API action requests from downstream to NapCat HTTP API.

When AstrBot/Yunzai sends an action request via WebSocket, the proxy:
  1. Identifies downstream, action type, and context
  2. Non-send actions → proxy directly to NapCat
  3. Send actions → Reply Arbiter checks reply_policy from event context
  4. Summary scheduled whitelist for authorized downstreams
  5. Blocked requests → fake OneBot success response

Phase 2+ (Reply Arbiter):
  - Matches event context from record_event()
  - Applies reply_policy: exclusive / first_valid / gated / silent / allow_all
  - Summary scheduled whitelist for trusted downstreams
  - Unsolicited messages → fake success (blocked_unsolicited_message)

Phase 3 (Klee Core repeat):
  - Klee Core internal sends (source="klee_core") always allowed
  - AstrBot unsolicited without observer context → blocked_astrbot_repeat_legacy
  - AstrBot unsolicited WITH observer context (link/card/summary) → allowed

Phase 4 (Scheduled system task whitelist):
  - Yunzai xiaoyao scheduled sign-in messages (no upstream context)
  - Checked before summary whitelist
  - Time-window gated + per-group rate limiting
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Optional

from app.config import get_config
from app.services.onebot_bridge.types import OneBotAPIRequest, OneBotAPIResponse
from app.services.onebot_bridge.upstream import UpstreamNapCatClient
from app.services.onebot_bridge.event_bus import OneBotEventBus
from app.services.onebot_bridge.diagnostics import log_upstream_forward, log_upstream_response
from app.services.plugin_router import ExecutionPlan, ReplyPolicy

logger = logging.getLogger(__name__)

# Actions that DO NOT require reply arbitration (read-only / non-send)
NON_SEND_ACTIONS = frozenset({
    "get_group_msg_history", "get_msg", "get_forward_msg",
    "get_group_member_info", "get_group_member_list",
    "get_group_info", "get_login_info", "get_status",
    "get_version_info", "get_friend_list", "get_group_list",
    "get_stranger_info", "get_cookies", "get_csrf_token",
})

# Actions that DO require reply arbitration (message sending)
SEND_ACTIONS = frozenset({
    "send_msg", "send_group_msg", "send_private_msg",
    "send_forward_msg", "send_group_forward_msg",
})


# ---------------------------------------------------------------------------
# Event context record (short-lived, TTL 60s)
# ---------------------------------------------------------------------------

class _EventContext:
    """Per-event routing context stored for reply arbitration."""
    __slots__ = (
        "event_id", "self_id", "group_id", "user_id", "message_id",
        "raw_message", "primary_targets", "observer_targets",
        "internal_targets", "passive_intents", "command_intent",
        "reply_policy", "route_reason", "created_at",
    )

    def __init__(
        self,
        event_id: str = "",
        self_id: int = 0,
        group_id: int = 0,
        user_id: int = 0,
        message_id: str = "",
        raw_message: str = "",
        primary_targets: Optional[list[str]] = None,
        observer_targets: Optional[list[str]] = None,
        internal_targets: Optional[list[str]] = None,
        passive_intents: Optional[list[str]] = None,
        command_intent: Optional[str] = None,
        reply_policy: Optional[ReplyPolicy] = None,
        route_reason: str = "",
    ):
        self.event_id = event_id
        self.self_id = self_id
        self.group_id = group_id
        self.user_id = user_id
        self.message_id = message_id
        self.raw_message = raw_message
        self.primary_targets = primary_targets or []
        self.observer_targets = observer_targets or []
        self.internal_targets = internal_targets or []
        self.passive_intents = passive_intents or []
        self.command_intent = command_intent
        self.reply_policy = reply_policy
        self.route_reason = route_reason
        self.created_at = time.time()

    def is_expired(self, ttl: float = 60.0) -> bool:
        return (time.time() - self.created_at) > ttl

    def has_observer_reason(self, pattern: str) -> bool:
        """Check if route_reason contains a specific observer pattern."""
        return pattern in self.route_reason


class OneBotAPIProxy:
    """Handles API action requests from downstream services.

    Phase 2+ additions:
      - record_event() stores execution plan for reply arbitration
      - handle_api() applies Reply Arbiter rules
      - Summary scheduled whitelist for trusted downstreams
      - Fake success for blocked requests

    Phase 3 additions:
      - Klee Core internal sends (source="klee_core") bypass arbitration
      - AstrBot unsolicited messages blocked as legacy repeat unless
        there's a recent observer context (link/card/summary)
    """

    def __init__(
        self,
        upstream: UpstreamNapCatClient,
        event_bus: OneBotEventBus,
        bot_http_map: dict[int, str] | None = None,
    ):
        self._upstream = upstream
        self._event_bus = event_bus
        self._bot_http_map = bot_http_map or {}
        # Track last known self_id per group for API routing
        self._group_bot: dict[int, int] = {}
        # Event context store: group_id -> latest event context (short-lived, TTL 60s)
        self._event_contexts: dict[int, _EventContext] = {}
        # Per-group reply cooldown tracker: (group_id, downstream_id) -> last_reply_time
        self._reply_cooldowns: dict[tuple[int, str], float] = {}

        # Scheduled system task rate limiter
        # Maps (group_id, window_key) -> count within current window
        self._scheduled_task_counts: dict[tuple[int, str], int] = {}
        self._scheduled_task_window_ttl: str = ""

    # ------------------------------------------------------------------
    # Event context recording (called from main.py BEFORE route_event)
    # ------------------------------------------------------------------

    def record_event(
        self,
        event,
        plan: Optional[ExecutionPlan] = None,
    ):
        """Record event context for later reply arbitration.

        Must be called BEFORE event_bus.route_event(), so that when
        downstream receives event and immediately sends API request,
        the context is already available.

        Args:
            event: OneBotEvent with self_id, group_id, user_id, etc.
            plan: ExecutionPlan from MessagePipeline (optional for Phase 1).
        """
        group_id = event.data.get("group_id", 0)
        if group_id and event.self_id:
            self._group_bot[group_id] = event.self_id

        # Build event context from plan
        if plan:
            ctx = _EventContext(
                event_id=plan.event_id,
                self_id=event.self_id,
                group_id=group_id,
                user_id=event.data.get("user_id", 0),
                message_id=event.data.get("message_id", ""),
                raw_message=event.data.get("raw_message", ""),
                primary_targets=list(plan.primary_targets),
                observer_targets=list(plan.observer_targets),
                internal_targets=list(plan.internal_targets),
                passive_intents=list(plan.passive_intents),
                command_intent=plan.command_intent,
                reply_policy=plan.reply_policy,
                route_reason=plan.route_reason,
            )

            # Store by group_id for quick lookup
            if group_id:
                self._event_contexts[group_id] = ctx

            # Also store by user_id in private chat
            user_id = event.data.get("user_id", 0)
            if user_id and not group_id:
                self._event_contexts[user_id] = ctx

    # ------------------------------------------------------------------
    # API handler
    # ------------------------------------------------------------------

    async def handle_api(self, request: OneBotAPIRequest, source: str):
        """Handle an API action request from a downstream.

        Phase 3 flow:
          1. Identify downstream, action type
          2. Non-send actions → proxy directly
          3. Send actions → Reply Arbiter
          4. Klee Core internal → always allowed
        """
        action = request.action
        params = request.params
        self_id = self._resolve_self_id(params)
        group_id = params.get("group_id", 0)

        # Route to correct NapCat
        self._route_napcat(self_id, group_id, action, source)

        # ── Klee Core internal sends: always allowed ──
        if source == "klee_core":
            logger.info("API-PROXY-REQUEST: internal klee_core -> direct proxy")
            # fall through to proxy below
        else:
            # ── Non-send actions: proxy directly ──
            # ── Send actions: Reply Arbiter ──
            original_action = action
            if action == "send_msg":
                if "group_id" in params:
                    action = "send_group_msg"
                elif "user_id" in params:
                    action = "send_private_msg"

            if action in SEND_ACTIONS:
                allowed = self._arbitrate(source, action, params)
                if not allowed:
                    await self._fake_success(request, source, "blocked_by_reply_arbiter")
                    return

            if original_action == "send_msg" and action != original_action:
                request.action = action

        # ── Proxy to NapCat ──
        log_upstream_forward(params, action, str(request.echo))
        response = await self._upstream.call_api(request)
        log_upstream_response(response, action, str(request.echo))

        response.echo = request.echo
        ok = await self._event_bus.send_response(source, response)
        if not ok:
            logger.warning(
                "Failed to send API response to %s for %s", source, action,
            )

    # ------------------------------------------------------------------
    # Reply Arbiter
    # ------------------------------------------------------------------

    def _arbitrate(self, source: str, action: str, params: dict) -> bool:
        """Decide whether a downstream may send a message.

        Phase 3 additions:
          - Explicit context match for observer targets (link/card/summary)
          - AstrBot without valid context → blocked_astrbot_repeat_legacy

        Returns:
            True: proxy to NapCat
            False: fake success
        """
        group_id = params.get("group_id", 0)
        user_id = params.get("user_id", 0)
        target_id = group_id or user_id

        # Try to find matching event context
        ctx = self._event_contexts.get(target_id)

        # ── Log API-PROXY-REQUEST ──
        matched_event_id = ctx.event_id if ctx else "none"
        rp_mode = ctx.reply_policy.mode if ctx and ctx.reply_policy else "none"
        logger.warning(
            "API-PROXY-REQUEST: downstream=%s action=%s group=%s user=%s "
            "matched_event=%s reply_mode=%s",
            source, action, group_id, user_id, matched_event_id, rp_mode,
        )

        # ── No event context: try summary whitelist ──
        if ctx is None or ctx.is_expired():
            return self._try_summary_or_observer_context(source, action, params)

        # ── Has event context: apply reply_policy ──
        rp = ctx.reply_policy
        if rp is None:
            logger.info("API-PROXY-REQUEST: no reply_policy for %s -> allowed", source)
            return True

        mode = rp.mode
        allow = rp.allow_targets

        # ── Exclusive mode ──
        if mode == "exclusive":
            if source in allow:
                logger.warning(
                    "API-PROXY-REQUEST: exclusive allow %s reason=%s",
                    source, rp.reason,
                )
                return True
            else:
                logger.warning(
                    "API-PROXY-REQUEST: exclusive block %s allow=%s",
                    source, allow,
                )
                return False

        # ── First_valid mode ──
        if mode == "first_valid":
            if source in allow:
                cooldown = rp.cooldown_seconds or 10
                key = (target_id, source)
                last_time = self._reply_cooldowns.get(key, 0)
                if time.time() - last_time < cooldown:
                    logger.warning(
                        "API-PROXY-REQUEST: first_valid cooldown %s (%.1fs < %ds) -> blocked",
                        source, time.time() - last_time, cooldown,
                    )
                    return False
                self._reply_cooldowns[key] = time.time()
                logger.warning("API-PROXY-REQUEST: first_valid allow %s", source)
                return True
            else:
                logger.warning(
                    "API-PROXY-REQUEST: first_valid block %s allow=%s",
                    source, allow,
                )
                return False

        # ── Gated mode ──
        if mode == "gated":
            if source in allow:
                cooldown = rp.cooldown_seconds or get_config().routing.observer_reply_cooldown_seconds
                key = (target_id, source)
                last_time = self._reply_cooldowns.get(key, 0)
                if time.time() - last_time < cooldown:
                    logger.warning(
                        "API-PROXY-REQUEST: gated cooldown %s (%.1fs < %ds) -> blocked",
                        source, time.time() - last_time, cooldown,
                    )
                    return False
                self._reply_cooldowns[key] = time.time()
                logger.warning("API-PROXY-REQUEST: gated allow %s", source)
                return True
            else:
                logger.warning("API-PROXY-REQUEST: gated block %s", source)
                return False

        # ── Silent mode ──
        if mode == "silent":
            logger.warning("API-PROXY-REQUEST: silent block %s", source)
            return False

        # ── Allow_all mode ──
        if mode == "allow_all":
            logger.warning("API-PROXY-REQUEST: allow_all %s", source)
            return True

        # Unknown mode → block
        logger.warning("API-PROXY-REQUEST: unknown reply mode=%s, blocking %s", mode, source)
        return False

    # ------------------------------------------------------------------
    # Scheduled system task whitelist (Phase 4: Yunzai xiaoyao sign-in)
    # ------------------------------------------------------------------

    def _check_scheduled_system_task(
        self, source: str, action: str, params: dict,
    ) -> bool:
        """Check if this unsolicited API call matches a scheduled system task.

        This is higher priority than the summary whitelist — designed for
        system plugins (e.g. Yunzai xiaoyao) that send scheduled messages
        without upstream event context.

        Returns:
            True if allowed via scheduled system task whitelist, False otherwise.
        """
        arbiter_cfg = get_config().reply_arbiter

        if not arbiter_cfg.allow_scheduled_system_tasks:
            return False

        downstream_id = self._normalize_source(source)
        group_id = params.get("group_id", 0)

        # ── Check: downstream in whitelist ──
        in_downstream = downstream_id in arbiter_cfg.scheduled_system_task_downstreams

        # ── Check: group in whitelist ──
        in_group = group_id in arbiter_cfg.scheduled_system_task_allowed_groups

        # ── Check: action in whitelist ──
        in_action = action in arbiter_cfg.scheduled_system_task_allowed_actions

        # ── Check: time window ──
        in_window, window_key = self._resolve_scheduled_time_window()

        # Always detect window changes and reset rate limiter
        if window_key != self._scheduled_task_window_ttl:
            self._scheduled_task_counts.clear()
            self._scheduled_task_window_ttl = window_key

        # ── Check: rate limit ──
        rate_ok = True
        rate_remaining = -1
        if window_key and group_id:
            max_per_window = arbiter_cfg.scheduled_system_task_max_per_group_per_window
            cache_key = (group_id, window_key)
            current_count = self._scheduled_task_counts.get(cache_key, 0)

            rate_ok = current_count < max_per_window
            rate_remaining = max_per_window - current_count

        # ── Allowed? ──
        allowed = all([in_downstream, in_group, in_action, in_window, rate_ok])

        # Build reason string
        if not in_downstream:
            reason = "downstream_not_in_whitelist"
        elif not in_group:
            reason = "group_not_in_whitelist"
        elif not in_action:
            reason = "action_not_in_whitelist"
        elif not in_window:
            reason = "outside_time_window"
        elif not rate_ok:
            reason = "rate_limit_exceeded"
        else:
            reason = "trusted_scheduled_system_task"

        logger.warning(
            "API-PROXY-SCHEDULED-SYSTEM: downstream=%s action=%s group=%s "
            "allowed=%s reason=%s in_time_window=%s rate_limit_remaining=%s",
            downstream_id, action, group_id,
            str(allowed).lower(), reason,
            str(in_window).lower(), rate_remaining if rate_remaining >= 0 else "N/A",
        )

        # ── Increment rate limiter on successful allow ──
        if allowed and window_key and group_id:
            self._scheduled_task_counts[cache_key] = current_count + 1

        return allowed

    def _resolve_scheduled_time_window(self) -> tuple[bool, str]:
        """Check current time against configured system task time windows.

        Returns:
            (in_window, window_key) where window_key is a stable identifier
            for the matching window (used for rate-limit caching).
        """
        now = datetime.now().strftime("%H:%M")
        for tw in get_config().reply_arbiter.scheduled_system_task_time_windows:
            if tw.start <= tw.end:
                # Normal window: e.g. 00:00 - 01:30
                if tw.start <= now <= tw.end:
                    return True, f"{tw.start}_{tw.end}"
            else:
                # Overnight window: e.g. 23:00 - 02:00
                if now >= tw.start or now <= tw.end:
                    return True, f"{tw.start}_{tw.end}"
        return False, ""

    @staticmethod
    def _normalize_source(source: str) -> str:
        """Normalize downstream source identifier for config matching.

        Config uses short names like 'yunzai', but internal source may be
        'yunzai_main' or similar. This strips suffix variants.
        """
        for prefix in ("yunzai", "astrbot", "hermes", "meme"):
            if source.startswith(prefix):
                return prefix
        return source

    # ------------------------------------------------------------------
    # Summary scheduled whitelist + observer context check (Phase 3)
    # ------------------------------------------------------------------

    def _try_summary_or_observer_context(
        self, source: str, action: str, params: dict,
    ) -> bool:
        """Phase 3+4: Check whitelists OR recent observer context.

        Priority:
          0. Scheduled system task whitelist (Yunzai xiaoyao sign-in etc.)
          1. Summary scheduled whitelist → allow if matched
          2. Recent observer context (link/card/summary, not repeat legacy) → allow
          3. AstrBot without valid context → blocked_astrbot_repeat_legacy
        """
        # ── Priority 0: Scheduled system task whitelist ──
        if self._check_scheduled_system_task(source, action, params):
            return True

        arbiter_cfg = get_config().reply_arbiter

        # ── Priority 1: Summary scheduled whitelist ──
        if arbiter_cfg.allow_summary_scheduled_messages:
            if source in arbiter_cfg.trusted_summary_downstreams:
                group_id = params.get("group_id", 0)
                if group_id in arbiter_cfg.summary_message_allowed_groups:
                    if action in arbiter_cfg.summary_message_allowed_actions:
                        in_window = (
                            self._is_in_summary_time_window()
                            or arbiter_cfg.allow_summary_outside_time_window
                        )
                        if in_window:
                            logger.warning(
                                "API-PROXY-SUMMARY: downstream=%s action=%s group=%s "
                                "allowed=true reason=trusted_summary_scheduled_message",
                                source, action, group_id,
                            )
                            return True

        # ── Priority 2: Recent observer context (link/card/summary) ──
        matched = self._find_observer_context(
            source, action, params.get("group_id", 0),
        )
        if matched:
            logger.warning(
                "API-PROXY-OBSERVER: downstream=%s action=%s group=%s "
                "allowed=true reason=%s",
                source, action, params.get("group_id", 0),
                matched.route_reason,
            )
            return True

        # ── Priority 3: Blocked — likely legacy repeat or unsolicited ──
        if source.startswith("astrbot"):
            logger.warning(
                "API-PROXY-BLOCKED-LEGACY-REPEAT: downstream=%s action=%s "
                "group=%s allowed=false reason=blocked_astrbot_repeat_legacy",
                source, action, params.get("group_id", 0),
            )
        else:
            logger.warning(
                "API-PROXY-BLOCKED-UNSOLICITED: downstream=%s action=%s "
                "group=%s allowed=false reason=no_event_context_no_observer",
                source, action, params.get("group_id", 0),
            )
        return False

    def _find_observer_context(
        self, source: str, action: str, group_id: int = 0, lookback_seconds: float = 30.0,
    ) -> Optional[_EventContext]:
        """Find a recent event context with valid observer routing.

        A valid observer context is one where the source is in observer_targets
        or primary_targets AND the route_reason does NOT indicate legacy
        repeat observer (which is now handled by Klee Core internally).

        Args:
            source: Downstream name (e.g. "astrbot_main").
            action: API action being performed.
            group_id: Target group for context matching.
            lookback_seconds: Max age of valid context.

        Returns:
            Matching _EventContext or None.
        """
        now = time.time()
        for gid, ctx in list(self._event_contexts.items()):
            if ctx.is_expired(lookback_seconds):
                continue
            if group_id and ctx.group_id != group_id:
                continue

            # Source must be in the routing targets
            if source not in ctx.primary_targets and source not in ctx.observer_targets:
                continue

            # Exclude legacy repeat observer contexts
            # These are from the old enable_astrbot_repeat_plugin=true mode
            if "astrbot_repeat_observer" in ctx.route_reason:
                if "astrbot_repeat_observer_legacy" not in ctx.route_reason:
                    # Even legacy context, skip — Klee Core handles repeat now
                    continue

            # Valid observer context found
            return ctx

        return None

    def _is_in_summary_time_window(self) -> bool:
        """Check if current time falls within any summary time window."""
        now = datetime.now().strftime("%H:%M")
        for tw in get_config().reply_arbiter.summary_message_time_windows:
            if tw.start <= now <= tw.end:
                return True
        return False

    # ------------------------------------------------------------------
    # Fake success
    # ------------------------------------------------------------------

    async def _fake_success(
        self,
        request: OneBotAPIRequest,
        source: str,
        reason: str = "blocked",
    ):
        """Send a fake OneBot success response to the downstream."""
        response = OneBotAPIResponse(
            status="ok",
            retcode=0,
            data={"message_id": 0},
            message="",
            wording="",
            echo=request.echo,
        )
        await self._event_bus.send_response(source, response)
        logger.warning(
            "API-PROXY-FAKE-SUCCESS: downstream=%s action=%s echo=%s reason=%s",
            source, request.action, request.echo, reason,
        )

    # ------------------------------------------------------------------
    # NapCat routing
    # ------------------------------------------------------------------

    def _resolve_self_id(self, params: dict) -> int:
        self_id = params.get("self_id", 0)
        try:
            return int(self_id)
        except (TypeError, ValueError):
            return 0

    def _route_napcat(self, self_id: int, group_id: int, action: str, source: str):
        """Route API call to correct NapCat instance."""
        if not self_id:
            self_id = self._group_bot.get(group_id, 0)
        if self_id and self_id in self._bot_http_map:
            correct_url = self._bot_http_map[self_id]
            if correct_url != self._upstream.napcat_http_url:
                logger.warning(
                    "ROUTING: action=%s self_id=%s -> %s (source=%s)",
                    action, self_id, correct_url, source,
                )
                self._upstream.napcat_http_url = correct_url
        else:
            logger.warning(
                "ROUTING-FAIL: action=%s self_id=%s not in _bot_http_map (source=%s)",
                action, self_id, source,
            )

    # ------------------------------------------------------------------
    # Deprecated / backward compat
    # ------------------------------------------------------------------

    async def respond_error(
        self,
        request: OneBotAPIRequest,
        source: str,
        wording: str,
    ):
        """Send an error response back without calling NapCat."""
        response = OneBotAPIResponse(
            status="failed", retcode=1,
            wording=wording, echo=request.echo,
        )
        await self._event_bus.send_response(source, response)
