"""Klee Core Repeat Service — 内置群聊复读功能。

Replaces the AstrBot repeat plugin. Tracks recent group messages in memory,
applies threshold + window + cooldown rules, and sends repeated text via
the NapCat HTTP API directly (bypassing WS downstream and Reply Arbiter).

Phase 3 (Klee Core internal repeat):
  - Memory-based message window (deque per group, max 50 entries)
  - Threshold detection: same normalized text appears >= N times in window
  - Cooldown: per-group timer prevents excessive repeats
  - Deduplication: same text not repeated again within cooldown period
  - Exclusions: commands, links, cards, images, bot messages, empty/long text
"""

from __future__ import annotations

import logging
import re
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Callable, Optional

from app.config import KleeCoreRepeatConfig
from app.services.plugin_router import ExecutionPlan
from app.services.onebot_bridge.types import OneBotAPIRequest, OneBotEvent

logger = logging.getLogger("klee.repeat")


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class RecentMessage:
    """A single message entry in the rolling window cache."""
    event_id: str = ""
    group_id: int = 0
    user_id: int = 0
    self_id: int = 0
    raw_text: str = ""
    normalized_text: str = ""
    message_id: str = ""
    timestamp: float = 0.0
    is_bot: bool = False
    command_intent: Optional[str] = None
    passive_intents: list[str] = field(default_factory=list)


@dataclass
class RepeatDecision:
    """Result of repeat eligibility check."""
    should_repeat: bool
    reason: str
    text: Optional[str] = None       # raw_text to send (None if should_repeat=False)
    count_in_window: int = 0
    cooldown_remaining: float = -1.0


# ---------------------------------------------------------------------------
# Repeat Service
# ---------------------------------------------------------------------------

class KleeCoreRepeatService:
    """Internal repeat engine.

    Lifecycle:
        svc = KleeCoreRepeatService(config, upstream_call_api)
        svc.add_message(event, plan)       # called from pipeline
        decision = svc.should_repeat(event) # called after pipeline
        if decision.should_repeat:
            await svc.send_repeat(event, decision)

    Thread-safe: single-event-loop only (all methods sync/async in same loop).
    """

    # Bot self_id accounts — messages from these are ignored
    BOT_SELF_IDS: frozenset[int] = frozenset({1432028231, 3750475856, 1825905764})

    # Link/card passive intents — repeat candidates with these are excluded
    LINK_INTENTS: frozenset[str] = frozenset({
        "url", "video_link",
        "bilibili_link", "douyin_link", "xiaohongshu_link",
    })
    CARD_INTENTS: frozenset[str] = frozenset({
        "rich_card", "bilibili_card", "xiaohongshu_card", "douyin_card",
    })

    def __init__(
        self,
        config: KleeCoreRepeatConfig,
        upstream_call_api: Callable,
    ):
        self._cfg = config
        self._call_api = upstream_call_api

        # Per-group rolling message window
        self._recent: dict[int, deque[RecentMessage]] = defaultdict(
            lambda: deque(maxlen=50)
        )

        # Per-group cooldown: group_id -> last_repeat_timestamp
        self._cooldowns: dict[int, float] = {}

        # Per-group deduplication: group_id -> set of already-repeated normalized texts
        self._repeated_texts: dict[int, set[str]] = defaultdict(set)

    # ------------------------------------------------------------------
    # Message window management
    # ------------------------------------------------------------------

    def add_message(self, event: OneBotEvent, plan: Optional[ExecutionPlan] = None) -> None:
        """Add current message to the per-group rolling window.

        Automatically filters out messages that should never be repeat
        candidates (bot messages, commands, links, cards, media-only, etc.).

        Called from the pipeline BEFORE should_repeat(), so the current
        message is included in the window for threshold counting.
        """
        group_id = int(event.data.get("group_id", 0) or 0)
        if not group_id:
            return

        raw_text = event.data.get("raw_message", "") or ""
        passive = plan.passive_intents if plan else []
        command_intent = plan.command_intent if plan else None

        # Apply exclusion filters
        if self._cfg.ignore_bot_messages and self._is_bot(event):
            return
        if self._cfg.ignore_commands:
            if command_intent:
                return
            raw_stripped = raw_text.strip()
            if raw_stripped.startswith("#") or raw_stripped.startswith("/"):
                return
        if self._cfg.ignore_links and self._has_intent(passive, self.LINK_INTENTS):
            return
        if self._cfg.ignore_cards and self._has_intent(passive, self.CARD_INTENTS):
            return
        if self._cfg.ignore_images and self._is_media_only(event):
            return

        normalized = self._normalize(raw_text)
        if not self._valid_length(normalized):
            return

        msg = RecentMessage(
            event_id=plan.event_id if plan else "",
            group_id=group_id,
            user_id=int(event.data.get("user_id", 0) or 0),
            self_id=event.self_id,
            raw_text=raw_text,
            normalized_text=normalized,
            message_id=str(event.data.get("message_id", "")),
            timestamp=time.time(),
            is_bot=self._is_bot(event),
            command_intent=command_intent,
            passive_intents=list(passive),
        )
        self._recent[group_id].append(msg)

    # ------------------------------------------------------------------
    # Repeat decision
    # ------------------------------------------------------------------

    def should_repeat(self, event: OneBotEvent) -> RepeatDecision:
        """Check whether the current message should trigger a repeat.

        Must be called AFTER add_message() for the same event, so the
        current message is already in the window.
        """
        group_id = int(event.data.get("group_id", 0) or 0)

        # 1. Group not enabled
        if group_id not in self._cfg.groups:
            return RepeatDecision(False, "group_not_enabled")

        # 2. Cooldown check
        now = time.time()
        last = self._cooldowns.get(group_id, 0)
        remaining = self._cfg.cooldown_seconds - (now - last)
        if remaining > 0:
            return RepeatDecision(
                False, "cooldown", cooldown_remaining=remaining,
            )

        # 3. Get normalized text of current message
        raw_text = event.data.get("raw_message", "") or ""
        normalized = self._normalize(raw_text)
        if not self._valid_length(normalized):
            return RepeatDecision(False, "invalid_length")

        # 4. Count occurrences in the rolling window
        window = list(self._recent[group_id])
        window_slice = window[-self._cfg.window:]  # last N entries
        count = sum(1 for m in window_slice if m.normalized_text == normalized)

        if count < self._cfg.threshold:
            return RepeatDecision(
                False, "below_threshold", count_in_window=count,
            )

        # 5. Dedup: same normalized text already repeated this cooldown cycle
        repeated_set = self._repeated_texts[group_id]
        if normalized in repeated_set:
            return RepeatDecision(
                False, "already_repeated", count_in_window=count,
            )

        # 6. Trigger!
        repeated_set.add(normalized)
        self._cooldowns[group_id] = now
        return RepeatDecision(
            True, "threshold_met",
            text=raw_text.strip(),
            count_in_window=count,
        )

    # ------------------------------------------------------------------
    # Send repeat message
    # ------------------------------------------------------------------

    async def send_repeat(self, event: OneBotEvent, decision: RepeatDecision) -> bool:
        """Send the repeated text via NapCat HTTP API.

        Args:
            event: Original upstream event (for group_id and self_id).
            decision: RepeatDecision with text to send.

        Returns:
            True if send succeeded, False otherwise.
        """
        group_id = int(event.data.get("group_id", 0) or 0)
        self_id = event.self_id
        text = decision.text or ""

        api_request = OneBotAPIRequest(
            action="send_group_msg",
            params={
                "group_id": group_id,
                "message": text,
                "self_id": self_id,
            },
        )

        try:
            response = await self._call_api(api_request)
            logger.warning(
                "KLEE-REPEAT: group=%s event_id=%s text=%.40s repeated=true "
                "reason=%s count=%d cooldown=%ds status=%s",
                group_id,
                getattr(event, "event_id", "") or "",
                text,
                decision.reason,
                decision.count_in_window,
                self._cfg.cooldown_seconds,
                getattr(response, "status", "?") if response else "?",
            )
            return True
        except Exception as exc:
            logger.error(
                "KLEE-REPEAT: group=%s text=%.40s repeated=false error=%s",
                group_id, text, exc,
            )
            return False

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize(text: str) -> str:
        """Normalize text for comparison: strip + collapse whitespace."""
        return re.sub(r"\s+", " ", text.strip())

    @staticmethod
    def _is_bot(event: OneBotEvent) -> bool:
        uid = int(event.data.get("user_id", 0) or 0)
        return uid in KleeCoreRepeatService.BOT_SELF_IDS

    @staticmethod
    def _has_intent(passive_intents: list[str], targets: frozenset[str]) -> bool:
        return any(i in targets for i in passive_intents)

    @staticmethod
    def _is_media_only(event: OneBotEvent) -> bool:
        segments = event.data.get("message", [])
        if isinstance(segments, list) and len(segments) == 1:
            return segments[0].get("type") in ("image", "record", "video")
        return False

    def _valid_length(self, text: str) -> bool:
        return self._cfg.min_length <= len(text) <= self._cfg.max_length

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def cache_size(self) -> int:
        return sum(len(dq) for dq in self._recent.values())

    @property
    def group_count(self) -> int:
        return len(self._recent)
