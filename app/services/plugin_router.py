"""Plugin Router — builds an execution plan from intent detection results.

Determines which downstream service should handle the message, whether
observer services should see it, and what reply policy should apply.

Phase 2+ rules:
  - Plugin command  -> primary route to matched downstream
  - Passive intents -> observer route for link/card parsers
  - Summary observer -> observer route for summary plugins
  - Klee Core repeat -> internal route (replaces AstrBot repeat plugin)
  - Reply policy     -> exclusive / first_valid / gated / silent / allow_all

Phase 3 (Klee Core repeat):
  - enable_astrbot_repeat_plugin=false by default (legacy compat only)
  - Repeat candidate messages route to Klee Core internal, not AstrBot observer
  - AstrBot observer reserved for link/card/summary only
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from app.config import KleeCoreRepeatConfig
from app.services.intent_router import IntentResult
from app.services.passive_intent import PassiveIntentResult

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ReplyPolicy
# ---------------------------------------------------------------------------

@dataclass
class ReplyPolicy:
    """Controls which downstreams may reply and under what conditions.

    Attributes:
        mode: Policy mode.
            "exclusive"   — only allow_targets may reply, all others fake_success
            "first_valid" — allow_targets may reply, first wins, others cooldown-gated
            "gated"       — allow_targets may reply, subject to cooldown/rate-limit
            "silent"      — ALL downstreams get fake_success (meta/notice events)
            "allow_all"   — ALL downstreams may reply freely (debug only)
        allow_targets: Downstream IDs allowed to reply.
        suppress_targets: Downstream IDs explicitly suppressed (fake_success).
        cooldown_seconds: Min seconds between replies (for first_valid/gated).
        reason: Human-readable reason for this policy.
    """
    mode: str = "silent"
    allow_targets: list[str] = field(default_factory=list)
    suppress_targets: list[str] = field(default_factory=list)
    cooldown_seconds: Optional[int] = None
    reason: str = ""


# ---------------------------------------------------------------------------
# ExecutionPlan (upgraded)
# ---------------------------------------------------------------------------

@dataclass
class ExecutionPlan:
    """Routing decision for a single event.

    target management:
        primary_targets:  explicit command targets
        observer_targets: passive monitoring targets (link parsers, summary, repeat)
        internal_targets: Klee Core internal processing targets
        blocked_targets:  explicitly excluded targets

    legacy compat:
        primary_handler: abstract handler type
        targets:         derived from primary_targets + observer_targets
    """

    # Core identity
    event_id: str = ""
    event_type: str = "message"

    # Strategy
    strategy: str = "silent"

    # NEW target groups
    primary_targets: list[str] = field(default_factory=list)
    observer_targets: list[str] = field(default_factory=list)
    internal_targets: list[str] = field(default_factory=list)
    blocked_targets: list[str] = field(default_factory=list)

    # Legacy compat
    primary_handler: str = "none"
    targets: list[str] = field(default_factory=list)

    # Intents
    command_intent: Optional[str] = None
    passive_intents: list[str] = field(default_factory=list)

    # Reply
    reply_policy: Optional[ReplyPolicy] = None
    call_hermes: bool = False

    # Metadata
    route_reason: str = ""
    secondary_actions: list[str] = field(default_factory=list)

    # ------------------------------------------------------------------
    # Factory methods
    # ------------------------------------------------------------------

    @staticmethod
    def broadcast(reason: str = "broadcast_all") -> "ExecutionPlan":
        return ExecutionPlan(
            event_type="meta_event",
            strategy="broadcast",
            primary_handler="__broadcast__",
            reply_policy=ReplyPolicy(mode="silent", reason="meta_event_no_reply"),
            route_reason=reason,
        )

    @staticmethod
    def self_id_route(reason: str = "self_id_routing") -> "ExecutionPlan":
        return ExecutionPlan(
            event_type="notice",
            strategy="self_id_route",
            primary_handler="__self_id_route__",
            reply_policy=ReplyPolicy(mode="silent", reason="notice_no_reply"),
            route_reason=reason,
        )

    @staticmethod
    def silent(reason: str = "") -> "ExecutionPlan":
        return ExecutionPlan(
            strategy="silent",
            primary_handler="none",
            reply_policy=ReplyPolicy(mode="silent", reason=reason or "silent"),
            route_reason=reason or "silent",
        )

    # ------------------------------------------------------------------
    # Convenience
    # ------------------------------------------------------------------

    def merge_observer_targets(self, targets: list[str]):
        for t in targets:
            if t not in self.observer_targets and t not in self.primary_targets:
                self.observer_targets.append(t)

    def merge_passive_intents(self, intents: list[str]):
        for i in intents:
            if i not in self.passive_intents:
                self.passive_intents.append(i)

    def merge_route_reason(self, reason: str):
        if self.route_reason:
            self.route_reason += "+" + reason
        else:
            self.route_reason = reason

    def build_legacy_targets(self):
        seen = set()
        result: list[str] = []
        for t in self.primary_targets + self.observer_targets:
            if t not in seen:
                seen.add(t)
                result.append(t)
        self.targets = result

    @property
    def send_targets(self) -> list[str]:
        seen: set[str] = set()
        result: list[str] = []
        for t in self.primary_targets + self.observer_targets:
            if t not in seen:
                seen.add(t)
                result.append(t)
        return result

    @property
    def has_observer(self) -> bool:
        return bool(self.observer_targets)

    @property
    def has_primary(self) -> bool:
        return bool(self.primary_targets)


# ---------------------------------------------------------------------------
# PluginRouter (upgraded)
# ---------------------------------------------------------------------------

class PluginRouter:
    """Routes messages based on intent + passive intent detection.

    Phase 2+ routing logic:
        1. Plugin commands  -> primary_targets only
        2. Passive intents  -> observer_targets
        3. Summary observer -> observer_targets (config-driven)
        4. Klee Core repeat -> internal_targets (replaces AstrBot repeat)
        5. Reply policy     -> generated per mode

    Phase 3 (Klee Core repeat):
        - enable_astrbot_repeat_plugin defaults to false (legacy compat)
        - Repeat candidates route to Klee Core internal, NOT AstrBot observer
        - AstrBot observer only for link/card/summary
    """

    DOWNSTREAM_HANDLERS = frozenset({"astrbot", "yunzai", "hermes", "meme"})

    def __init__(
        self,
        observer_links_enabled: bool = True,
        observer_cards_enabled: bool = True,
        link_parser_targets: Optional[list[str]] = None,
        card_parser_targets: Optional[list[str]] = None,
        link_parser_cooldown: int = 10,
        card_parser_cooldown: int = 10,
        summary_observer_enabled: bool = False,
        summary_observer_targets: Optional[list[str]] = None,
        summary_observer_groups: Optional[set[int]] = None,
        repeat_observer_enabled: bool = False,
        repeat_observer_targets: Optional[list[str]] = None,
        repeat_observer_groups: Optional[set[int]] = None,
        observer_reply_cooldown: int = 30,
        # Phase 3: Klee Core repeat
        klee_repeat_enabled: bool = True,
        klee_repeat_groups: Optional[set[int]] = None,
        klee_repeat_cfg: Optional[KleeCoreRepeatConfig] = None,
    ):
        self._links_enabled = observer_links_enabled
        self._cards_enabled = observer_cards_enabled
        self._link_targets = link_parser_targets or ["astrbot_main"]
        self._card_targets = card_parser_targets or ["astrbot_main"]
        self._link_cooldown = link_parser_cooldown
        self._card_cooldown = card_parser_cooldown
        self._summary_enabled = summary_observer_enabled
        self._summary_targets = summary_observer_targets or ["astrbot_main"]
        self._summary_groups = summary_observer_groups or set()
        self._repeat_enabled = repeat_observer_enabled
        self._repeat_targets = repeat_observer_targets or ["astrbot_main"]
        self._repeat_groups = repeat_observer_groups or set()
        self._reply_cooldown = observer_reply_cooldown
        # Phase 3
        self._klee_repeat_enabled = klee_repeat_enabled
        self._klee_repeat_groups = klee_repeat_groups or set()
        self._klee_repeat_cfg = klee_repeat_cfg

    # ------------------------------------------------------------------
    # Main route method
    # ------------------------------------------------------------------

    def route(
        self,
        intent: IntentResult,
        mentions_bot: bool = False,
        passive: Optional[PassiveIntentResult] = None,
        event_data: Optional[dict] = None,
    ) -> ExecutionPlan:
        """Build a complete execution plan from intent + passive detection."""

        group_id = int(event_data.get("group_id", 0)) if event_data else 0
        passive = passive or PassiveIntentResult()

        # Command routing (exclusive)
        if intent.is_command:
            return self._route_command(intent)

        # Non-command: build observer-targeted plan
        plan = ExecutionPlan(
            event_type="message",
            strategy="silent",
            primary_handler="none",
            secondary_actions=["record"],
            call_hermes=False,
            route_reason="normal_chat",
        )
        plan.merge_passive_intents(passive.passive_intents)

        # Link parser observer
        if self._links_enabled and passive.has_link:
            plan.merge_observer_targets(self._link_targets)
            plan.merge_route_reason("astrbot_passive_link_parser")
            self._apply_observer_policy(plan, "first_valid", self._link_cooldown)

        # Card parser observer
        if self._cards_enabled and passive.has_card:
            plan.merge_observer_targets(self._card_targets)
            plan.merge_route_reason("astrbot_passive_card_parser")
            self._apply_observer_policy(plan, "first_valid", self._card_cooldown)

        # Summary observer
        if self._summary_enabled and group_id in self._summary_groups:
            plan.merge_observer_targets(self._summary_targets)
            plan.merge_route_reason("astrbot_summary_observer")
            self._apply_observer_policy(plan, "gated")

        # ── Phase 3: Klee Core internal repeat ──
        if self._klee_repeat_enabled and group_id in self._klee_repeat_groups:
            is_candidate = self._is_klee_repeat_candidate(
                event_data, passive, intent,
            )
            plan.passive_intents.append(
                "repeat_candidate" if is_candidate else "repeat_excluded"
            )
            if is_candidate:
                plan.internal_targets.append("klee_core.repeat")
                plan.merge_route_reason("klee_core_repeat_candidate")
            else:
                plan.merge_route_reason("klee_core_repeat_excluded")

        # ── Legacy AstrBot repeat observer (default off, backward compat) ──
        if self._repeat_enabled and group_id in self._repeat_groups:
            if passive.is_repeat_eligible:
                plan.merge_observer_targets(self._repeat_targets)
                plan.merge_route_reason("astrbot_repeat_observer_legacy")
                self._apply_observer_policy(plan, "gated", self._reply_cooldown)

        # Final strategy
        if plan.has_observer or plan.has_primary:
            plan.strategy = "multi" if plan.has_primary else "observer"
        else:
            plan.strategy = "silent"

        plan.build_legacy_targets()
        return plan

    # ------------------------------------------------------------------
    # Command routing
    # ------------------------------------------------------------------

    def _route_command(self, intent: IntentResult) -> ExecutionPlan:
        handler = intent.primary_handler

        if handler == "yunzai":
            return ExecutionPlan(
                event_type="message", strategy="single",
                primary_targets=["yunzai"],
                primary_handler="yunzai",
                command_intent="yunzai_command",
                reply_policy=ReplyPolicy(
                    mode="exclusive",
                    allow_targets=["yunzai"],
                    suppress_targets=["astrbot_main", "astrbot_secondary", "astrbot_third", "hermes"],
                    reason="yunzai_exclusive",
                ),
                route_reason="yunzai_command",
                secondary_actions=["record"],
            )

        if handler == "astrbot":
            return ExecutionPlan(
                event_type="message", strategy="single",
                primary_targets=[],  # resolved by pipeline
                primary_handler="astrbot",
                command_intent="astrbot_command",
                reply_policy=ReplyPolicy(
                    mode="exclusive",
                    allow_targets=[],  # resolved by pipeline
                    suppress_targets=["yunzai", "hermes"],
                    reason="astrbot_exclusive",
                ),
                route_reason="astrbot_command",
                secondary_actions=["record"],
            )

        if handler == "klee_core":
            return ExecutionPlan(
                event_type="message", strategy="silent",
                internal_targets=["klee_core"],
                primary_handler="klee_core",
                command_intent="klee_core_internal",
                reply_policy=ReplyPolicy(
                    mode="exclusive",
                    allow_targets=["klee_core"],
                    reason="klee_core_internal",
                ),
                route_reason="klee_core_internal",
                secondary_actions=["record"],
            )

        if handler == "hermes":
            return ExecutionPlan(
                event_type="message", strategy="single",
                primary_targets=["hermes"],
                primary_handler="hermes",
                command_intent="hermes_command",
                reply_policy=ReplyPolicy(
                    mode="exclusive",
                    allow_targets=["hermes"],
                    reason="hermes_exclusive",
                ),
                route_reason="hermes_command",
                secondary_actions=["record"],
            )

        logger.warning("PluginRouter: unknown handler '%s', silent", handler)
        plan = ExecutionPlan.silent("unknown_handler_" + handler)
        plan.secondary_actions = ["record"]
        return plan

    # ------------------------------------------------------------------
    # Observer policy helper
    # ------------------------------------------------------------------

    def _apply_observer_policy(
        self, plan: ExecutionPlan, mode: str, cooldown: Optional[int] = None,
    ):
        rp = plan.reply_policy
        if rp is None:
            rp = ReplyPolicy(mode=mode, cooldown_seconds=cooldown)
            plan.reply_policy = rp

        mode_order = {"silent": 0, "gated": 1, "first_valid": 2, "exclusive": 3, "allow_all": 4}
        if mode_order.get(mode, 0) > mode_order.get(rp.mode, 0):
            rp.mode = mode

        for t in plan.observer_targets:
            if t not in rp.allow_targets:
                rp.allow_targets.append(t)

        if cooldown is not None:
            if rp.cooldown_seconds is None:
                rp.cooldown_seconds = cooldown
            else:
                rp.cooldown_seconds = min(rp.cooldown_seconds, cooldown)

    # ------------------------------------------------------------------
    # Klee Core repeat candidate check
    # ------------------------------------------------------------------

    def _is_klee_repeat_candidate(
        self,
        event_data: Optional[dict],
        passive: PassiveIntentResult,
        intent: IntentResult,
    ) -> bool:
        """Check if message is a valid Klee Core repeat candidate.

        Excludes commands, links, cards, media-only, and invalid-length messages.
        """
        if not event_data:
            return False

        raw_message = event_data.get("raw_message", "")
        text = raw_message.strip()
        if not text:
            return False

        # Not a command
        if intent.is_command:
            return False
        if text.startswith("#") or text.startswith("/"):
            return False

        # No links or cards
        if passive.has_link or passive.has_card:
            return False

        # Not media-only
        segments = event_data.get("message", [])
        if isinstance(segments, list) and len(segments) == 1:
            if segments[0].get("type") in ("image", "record", "video"):
                return False

        # Length check
        if self._klee_repeat_cfg:
            text_len = len(text)
            if text_len < self._klee_repeat_cfg.min_length:
                return False
            if text_len > self._klee_repeat_cfg.max_length:
                return False

        return True
