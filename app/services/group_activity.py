"""Group Activity Tracker — per-group message stats for Reply Gate.

Tracks: message density, last bot reply time, hourly reply cap,
image/emoji spam ratio. All state is in-memory (resets on restart).
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field


@dataclass
class _GroupState:
    """Internal state for one group."""
    # Recent message records: (timestamp, is_media)
    recent: deque[tuple[float, bool]] = field(default_factory=deque)
    # Bot reply timestamps for hourly cap
    reply_times: deque[float] = field(default_factory=deque)
    # Last bot reply timestamp (for cooldown)
    last_reply_at: float = 0.0


class GroupActivityTracker:
    """Tracks per-group message activity for the ReplyGate scoring engine.

    Thread-safe for async usage (no locks needed — single event loop).

    Usage:
        tracker = GroupActivityTracker(window_seconds=10, max_recent=30)

        tracker.record_message(group_id, is_media=False)
        tracker.record_reply(group_id)

        density = tracker.get_density(group_id)       # msgs/sec in window
        cooldown = tracker.get_cooldown_remaining(group_id)  # seconds
        capped = tracker.is_hourly_capped(group_id, max_per_hour=5)
    """

    def __init__(self, window_seconds: int = 10, max_recent: int = 30):
        self._groups: dict[int, _GroupState] = {}
        self._window = window_seconds
        self._max_recent = max_recent

    def _get(self, group_id: int) -> _GroupState:
        if group_id not in self._groups:
            self._groups[group_id] = _GroupState()
        return self._groups[group_id]

    # ── Recording ───────────────────────────────────────

    def record_message(self, group_id: int, is_media: bool = False):
        """Record an incoming message (not from bot)."""
        gs = self._get(group_id)
        now = time.time()
        gs.recent.append((now, is_media))
        # Trim old entries
        cutoff = now - self._window
        while gs.recent and gs.recent[0][0] < cutoff:
            gs.recent.popleft()
        # Trim by max size
        while len(gs.recent) > self._max_recent:
            gs.recent.popleft()

    def record_reply(self, group_id: int):
        """Record that the bot sent a reply in this group."""
        gs = self._get(group_id)
        now = time.time()
        gs.last_reply_at = now
        gs.reply_times.append(now)
        # Clean old reply times (> 1 hour)
        cutoff = now - 3600
        while gs.reply_times and gs.reply_times[0] < cutoff:
            gs.reply_times.popleft()

    # ── Queries ─────────────────────────────────────────

    def get_density(self, group_id: int) -> float:
        """Messages per second within the recent window.

        Returns 0.0 if the group has no recorded messages.
        """
        gs = self._get(group_id)
        if not gs.recent:
            return 0.0
        now = time.time()
        cutoff = now - self._window
        count = sum(1 for t, _ in gs.recent if t >= cutoff)
        elapsed = now - cutoff
        if elapsed <= 0:
            return 0.0
        return count / elapsed

    def get_cooldown_remaining(self, group_id: int, cooldown_seconds: float = 30.0) -> float:
        """Seconds remaining before bot can reply again. 0 = ready."""
        gs = self._get(group_id)
        if gs.last_reply_at == 0:
            return 0.0
        elapsed = time.time() - gs.last_reply_at
        remaining = cooldown_seconds - elapsed
        return max(0.0, remaining)

    def is_in_cooldown(self, group_id: int, cooldown_seconds: float = 30.0) -> bool:
        """True if bot replied too recently."""
        return self.get_cooldown_remaining(group_id, cooldown_seconds) > 0

    def get_hourly_reply_count(self, group_id: int) -> int:
        """Number of bot replies in the last hour."""
        gs = self._get(group_id)
        now = time.time()
        cutoff = now - 3600
        return sum(1 for t in gs.reply_times if t >= cutoff)

    def is_hourly_capped(self, group_id: int, max_per_hour: int = 8) -> bool:
        """True if bot has exceeded the hourly reply cap."""
        return self.get_hourly_reply_count(group_id) >= max_per_hour

    def get_image_spam_ratio(self, group_id: int) -> float:
        """Ratio of media-only messages in the recent window.

        Returns 0.0–1.0. High values indicate image/emoji spam.
        """
        gs = self._get(group_id)
        if not gs.recent:
            return 0.0
        now = time.time()
        cutoff = now - self._window
        recent_in_window = [(t, m) for t, m in gs.recent if t >= cutoff]
        if not recent_in_window:
            return 0.0
        media_count = sum(1 for _, m in recent_in_window if m)
        return media_count / len(recent_in_window)

    # ── Reset (for tests) ───────────────────────────────

    def reset(self, group_id: int | None = None):
        """Reset state for one group or all groups."""
        if group_id is not None:
            self._groups.pop(group_id, None)
        else:
            self._groups.clear()
