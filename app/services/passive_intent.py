"""Passive Intent Detector — detects URLs, rich cards, and observer-relevant content.

Scans OneBot message segments for passive intents: link parsing targets,
card parsing targets, repeat candidates, and summary observer candidates.

Compatible with both raw_message (str) and message (list[dict]) formats.
"""

from __future__ import annotations

import logging
import re
from typing import List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# URL / domain detection patterns
# ---------------------------------------------------------------------------

_URL_GENERIC = re.compile(r'https?://\S+', re.IGNORECASE)

_URL_BILIBILI = re.compile(
    r'(?:b23\.tv|bilibili\.com|www\.bilibili\.com|m\.bilibili\.com|t\.bilibili\.com|'
    r'哔哩哔哩|Bilibili)',
    re.IGNORECASE,
)

_URL_DOUYIN = re.compile(
    r'(?:douyin\.com|v\.douyin\.com|www\.douyin\.com|iesdouyin\.com|aweme|抖音)',
    re.IGNORECASE,
)

_URL_XIAOHONGSHU = re.compile(
    r'(?:xiaohongshu\.com|xhslink\.com|www\.xiaohongshu\.com|m\.xiaohongshu\.com|小红书)',
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Rich card type constants
# ---------------------------------------------------------------------------

RICH_CARD_TYPES = frozenset({"json", "xml", "share", "video"})


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

class PassiveIntentResult:
    """Result of passive intent detection on a message."""

    __slots__ = ("passive_intents", "raw_text", "reason")

    def __init__(
        self,
        passive_intents: Optional[list[str]] = None,
        raw_text: str = "",
        reason: str = "",
    ):
        self.passive_intents: list[str] = passive_intents or []
        self.raw_text: str = raw_text
        self.reason: str = reason

    def __repr__(self) -> str:
        return (
            f"PassiveIntentResult(intents={self.passive_intents}, "
            f"text={self.raw_text[:40]!r}, reason={self.reason})"
        )

    # ── Convenience query helpers ──

    def has_intent(self, intent: str) -> bool:
        return intent in self.passive_intents

    def has_any(self, *intents: str) -> bool:
        return any(i in self.passive_intents for i in intents)

    @property
    def has_link(self) -> bool:
        return self.has_any(
            "bilibili_link", "douyin_link", "xiaohongshu_link", "url"
        )

    @property
    def has_card(self) -> bool:
        return self.has_any(
            "rich_card", "bilibili_card", "xiaohongshu_card", "douyin_card"
        )

    @property
    def is_repeat_eligible(self) -> bool:
        """True if message could be a repeat candidate (not excluded by link/card)."""
        if self.has_link or self.has_card:
            return False
        return True


class PassiveIntentDetector:
    """Detects passive intents from OneBot message events.

    Usage:
        detector = PassiveIntentDetector()
        result = detector.detect(event)
        if result.has_intent("bilibili_link"):
            ...
    """

    def detect(self, event) -> PassiveIntentResult:
        """Analyze a OneBotEvent for passive intents.

        Args:
            event: OneBotEvent with data dict containing 'message' and/or 'raw_message'.

        Returns:
            PassiveIntentResult with detected intents and extracted text.
        """
        intents: list[str] = []
        reasons: list[str] = []

        # Extract all text from segments + card content
        combined_text = self._extract_all_text(event)

        segments: list[dict] = event.data.get("message", [])
        if isinstance(segments, str):
            # Message is already a plain string — treat as single text segment
            combined_text = segments
            segments = [{"type": "text", "data": {"text": segments}}]

        # ── URL detection ──
        has_generic_url = bool(_URL_GENERIC.search(combined_text))
        has_bilibili_url = bool(_URL_BILIBILI.search(combined_text))
        has_douyin_url = bool(_URL_DOUYIN.search(combined_text))
        has_xiaohongshu_url = bool(_URL_XIAOHONGSHU.search(combined_text))

        if has_generic_url:
            intents.append("url")
            reasons.append("generic_url_detected")
        if has_bilibili_url:
            intents.append("bilibili_link")
            reasons.append("bilibili_url")
        if has_douyin_url:
            intents.append("douyin_link")
            reasons.append("douyin_url")
        if has_xiaohongshu_url:
            intents.append("xiaohongshu_link")
            reasons.append("xiaohongshu_url")

        # Also detect video-specific links (e.g. bilibili video pages)
        if has_bilibili_url and "/video/" in combined_text:
            intents.append("video_link")
        if has_douyin_url and ("/video/" in combined_text or "aweme" in combined_text):
            intents.append("video_link")

        # ── Rich card detection ──
        rich_card_types = self._get_rich_card_types(segments)
        if rich_card_types:
            intents.append("rich_card")
            reasons.append(f"rich_card:{','.join(rich_card_types)}")

            # Card content analysis
            card_text = self._extract_card_text(segments)
            if _URL_BILIBILI.search(card_text):
                intents.append("bilibili_card")
                reasons.append("bilibili_in_card")
            if _URL_XIAOHONGSHU.search(card_text):
                intents.append("xiaohongshu_card")
                reasons.append("xiaohongshu_in_card")
            if _URL_DOUYIN.search(card_text):
                intents.append("douyin_card")
                reasons.append("douyin_in_card")

        # ── Repeat candidate eligibility ──
        raw_message = event.data.get("raw_message", "")
        if self._is_repeat_candidate(event, raw_message, intents):
            intents.append("repeat_candidate")
            reasons.append("repeat_candidate")

        # ── Summary observer candidate ──
        if event.data.get("message_type") == "group" and event.data.get("group_id"):
            intents.append("summary_observer_candidate")
            # Note: actual enabling is decided by pipeline config, not detector

        return PassiveIntentResult(
            passive_intents=list(dict.fromkeys(intents)),  # dedupe preserving order
            raw_text=combined_text,
            reason=";".join(reasons),
        )

    # ------------------------------------------------------------------
    # Text extraction (handles all OneBot segment formats)
    # ------------------------------------------------------------------

    def _extract_all_text(self, event) -> str:
        """Extract all searchable text from an event, across all segment types.

        Combines: text segments, json/xml/share content, raw_message.
        """
        parts: list[str] = []
        segments = event.data.get("message", [])

        if isinstance(segments, str):
            parts.append(segments)
            parts.append(event.data.get("raw_message", ""))
            return " ".join(p for p in parts if p)

        if not isinstance(segments, list):
            parts.append(event.data.get("raw_message", ""))
            return " ".join(p for p in parts if p)

        for seg in segments:
            if not isinstance(seg, dict):
                continue
            seg_type = seg.get("type", "")
            seg_data = seg.get("data", {})

            if seg_type == "text":
                parts.append(seg_data.get("text", ""))

            elif seg_type == "json":
                # JSON card: extract data.content or full data
                content = seg_data.get("data", "")
                if isinstance(content, str):
                    parts.append(content)
                elif isinstance(content, dict):
                    parts.append(str(content))

            elif seg_type == "xml":
                content = seg_data.get("data", "")
                if isinstance(content, str):
                    parts.append(content)

            elif seg_type == "share":
                parts.append(seg_data.get("url", ""))
                parts.append(seg_data.get("title", ""))
                parts.append(seg_data.get("content", ""))

            elif seg_type == "video":
                parts.append(seg_data.get("url", ""))
                parts.append(seg_data.get("file", ""))

        # Also include raw_message as fallback
        raw = event.data.get("raw_message", "")
        if raw:
            parts.append(raw)

        return " ".join(p for p in parts if p)

    def _extract_card_text(self, segments: list[dict]) -> str:
        """Extract text specifically from rich card segments (json/xml/share/video)."""
        parts: list[str] = []
        for seg in segments:
            if not isinstance(seg, dict):
                continue
            seg_type = seg.get("type", "")
            if seg_type not in RICH_CARD_TYPES:
                continue
            seg_data = seg.get("data", {})
            if seg_type == "share":
                parts.append(seg_data.get("url", ""))
                parts.append(seg_data.get("title", ""))
                parts.append(seg_data.get("content", ""))
            else:
                data_val = seg_data.get("data", "")
                if isinstance(data_val, str):
                    parts.append(data_val)
                elif isinstance(data_val, dict):
                    parts.append(str(data_val))
        return " ".join(parts)

    # ------------------------------------------------------------------
    # Rich card detection
    # ------------------------------------------------------------------

    def _get_rich_card_types(self, segments: list[dict]) -> list[str]:
        """Return list of rich card types found in segments."""
        return [
            seg.get("type", "")
            for seg in segments
            if isinstance(seg, dict) and seg.get("type", "") in RICH_CARD_TYPES
        ]

    # ------------------------------------------------------------------
    # Repeat candidate detection
    # ------------------------------------------------------------------

    def _is_repeat_candidate(
        self, event, raw_message: str, already_detected: list[str],
    ) -> bool:
        """Determine if a message could be a repeat candidate.

        Excludes:
            - Command messages (#, / prefix)
            - Messages with URLs or cards (link/card intents)
            - Bot self-messages
            - Empty text
            - Messages exceeding repeat_max_length (checked by pipeline)
        """
        # Already has link/card → not a repeat candidate
        link_card_intents = {
            "url", "video_link",
            "bilibili_link", "douyin_link", "xiaohongshu_link",
            "rich_card", "bilibili_card", "xiaohongshu_card", "douyin_card",
        }
        if any(i in already_detected for i in link_card_intents):
            return False

        text = raw_message.strip()
        if not text:
            return False

        # Exclude command-prefixed messages
        if text.startswith("#") or text.startswith("/"):
            return False

        # Exclude pure image/video/record messages (single media segment, no text)
        segments = event.data.get("message", [])
        if isinstance(segments, list) and len(segments) == 1:
            seg = segments[0]
            if isinstance(seg, dict) and seg.get("type") in ("image", "record", "video"):
                return False

        return True


# ---------------------------------------------------------------------------
# Convenience functions (stateless wrappers)
# ---------------------------------------------------------------------------

_detector = PassiveIntentDetector()


def detect_passive_intents(event) -> PassiveIntentResult:
    """One-shot passive intent detection (uses shared detector)."""
    return _detector.detect(event)


# Individual check functions for use in routing logic

def contains_url(event) -> bool:
    result = _detector.detect(event)
    return result.has_intent("url")

def contains_video_link(event) -> bool:
    result = _detector.detect(event)
    return result.has_intent("video_link")

def contains_bilibili_link(event) -> bool:
    result = _detector.detect(event)
    return result.has_intent("bilibili_link")

def contains_douyin_link(event) -> bool:
    result = _detector.detect(event)
    return result.has_intent("douyin_link")

def contains_xiaohongshu_link(event) -> bool:
    result = _detector.detect(event)
    return result.has_intent("xiaohongshu_link")

def contains_rich_card(event) -> bool:
    segments = event.data.get("message", [])
    if isinstance(segments, list):
        for seg in segments:
            if isinstance(seg, dict) and seg.get("type") in RICH_CARD_TYPES:
                return True
    return False

def contains_bilibili_card(event) -> bool:
    result = _detector.detect(event)
    return result.has_intent("bilibili_card")

def contains_xiaohongshu_card(event) -> bool:
    result = _detector.detect(event)
    return result.has_intent("xiaohongshu_card")

def contains_douyin_card(event) -> bool:
    result = _detector.detect(event)
    return result.has_intent("douyin_card")

def is_repeat_candidate(event) -> bool:
    result = _detector.detect(event)
    return result.is_repeat_eligible and "repeat_candidate" in result.passive_intents
