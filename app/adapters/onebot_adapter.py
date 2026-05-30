"""OneBot v11 message adapter — converts raw OneBot events into unified Message objects.

Handles all common segment types: text, image, face (emoji), at, record (voice),
video, file, reply, forward, mface, json, dice, rps, poke, music, share, contact,
location, etc.
"""

from __future__ import annotations

from typing import Any

from app.models import Attachment
from app.schemas.onebot import OneBotEvent, OneBotMessageSegment


# ---------------------------------------------------------------------------
# Segment-type helpers
# ---------------------------------------------------------------------------

TEXT_LIKE = frozenset({"text"})
MEDIA_SEGMENTS = frozenset({"image", "record", "video", "file"})
ATTACHMENT_SEGMENTS = frozenset({"image", "record", "video", "file", "face", "mface", "forward"})


def _segment_has_content(seg: OneBotMessageSegment) -> bool:
    """Return True if the segment carries meaningful content (not pure metadata)."""
    if seg.type in TEXT_LIKE:
        return bool(seg.data.get("text", "").strip())
    if seg.type in ATTACHMENT_SEGMENTS:
        return True
    if seg.type in ("at", "reply"):
        return True
    # poke, dice, rps, music, json, share, contact, location — count as content
    return seg.type not in ("", None)


# ---------------------------------------------------------------------------
# Type detection
# ---------------------------------------------------------------------------

def detect_message_type(segments: list[OneBotMessageSegment]) -> str:
    """Determine the unified message_type from a list of OneBot segments.

    Priority order: forward > reply > mixed > single-media > text

    Returns one of: text, image, voice, video, file, mixed, reply, forward, other
    """
    if not segments:
        return "text"

    content_segments = [s for s in segments if _segment_has_content(s)]
    types = {s.type for s in content_segments}

    # Forwarded messages
    if "forward" in types:
        return "forward"

    # Reply (quoted reply to another message)
    if "reply" in types:
        # If the message also contains media or text AFTER the reply, it's "mixed"
        non_reply_types = types - {"reply", "at", "text"}
        if non_reply_types:
            return "mixed"
        # Reply with only text following is still "reply"
        remaining = [s for s in content_segments if s.type == "text"]
        if remaining and remaining[0].data.get("text", "").strip():
            return "reply"
        # Bare reply with no extra text = "reply"
        return "reply"

    # Multiple distinct content types → mixed
    content_types = types - {"at", "text", "face", "mface", "poke", "dice", "rps"}
    if len(content_types) > 1:
        return "mixed"

    # Single media type
    if "image" in types and "text" not in types:
        return "image"
    if "record" in types and "text" not in types:
        return "voice"
    if "video" in types and "text" not in types:
        return "video"
    if "file" in types and "text" not in types:
        return "file"

    # Mixed: text + media
    media_overlap = types & MEDIA_SEGMENTS
    text_overlap = types & TEXT_LIKE
    if media_overlap and text_overlap:
        return "mixed"

    # Default: text (includes face/emoji-only, at-only, poke, etc.)
    return "text"


# ---------------------------------------------------------------------------
# Text extraction
# ---------------------------------------------------------------------------

def extract_text(segments: list[OneBotMessageSegment]) -> str:
    """Concatenate all text segments into a single string."""
    parts: list[str] = []
    for seg in segments:
        if seg.type == "text":
            parts.append(seg.data.get("text", ""))
    return "".join(parts)


def extract_normalized_text(segments: list[OneBotMessageSegment]) -> str:
    """Extract text with @mentions stripped (for intent detection)."""
    parts: list[str] = []
    for seg in segments:
        if seg.type == "text":
            parts.append(seg.data.get("text", ""))
    return "".join(parts).strip()


# ---------------------------------------------------------------------------
# Attachment extraction
# ---------------------------------------------------------------------------

def extract_attachments(segments: list[OneBotMessageSegment]) -> list[dict[str, Any]]:
    """Extract attachment metadata from media/emoji/forward segments.

    Returns a list of dicts suitable for creating Attachment ORM objects.
    """
    attachments: list[dict[str, Any]] = []

    for seg in segments:
        data = seg.data or {}

        if seg.type == "image":
            attachments.append({
                "attachment_type": "image",
                "file_id": str(data.get("file", "")),
                "url": str(data.get("url", "")),
                "mime_type": str(data.get("type", "image/jpeg")),
                "file_size": _safe_int(data.get("file_size")),
                "width": _safe_int(data.get("width")),
                "height": _safe_int(data.get("height")),
                "summary": str(data.get("summary", "")),
            })

        elif seg.type == "record":  # voice / audio
            attachments.append({
                "attachment_type": "voice",
                "file_id": str(data.get("file", "")),
                "url": str(data.get("url", "")),
                "mime_type": "audio/ogg",
                "file_size": _safe_int(data.get("file_size")),
                "duration": _safe_float(data.get("duration")),
            })

        elif seg.type == "video":
            attachments.append({
                "attachment_type": "video",
                "file_id": str(data.get("file", "")),
                "url": str(data.get("url", "")),
                "mime_type": "video/mp4",
                "file_size": _safe_int(data.get("file_size")),
                "duration": _safe_float(data.get("duration")),
                "width": _safe_int(data.get("width")),
                "height": _safe_int(data.get("height")),
            })

        elif seg.type == "file":
            attachments.append({
                "attachment_type": "file",
                "file_id": str(data.get("file", "")),
                "url": str(data.get("url", "")),
                "mime_type": str(data.get("type", "application/octet-stream")),
                "file_size": _safe_int(data.get("file_size")),
                "summary": str(data.get("name", "")),
            })

        elif seg.type == "face":  # QQ emoji
            attachments.append({
                "attachment_type": "face",
                "file_id": str(data.get("id", "")),
                "summary": str(data.get("id", "")),
            })

        elif seg.type == "mface":  # market face / sticker
            attachments.append({
                "attachment_type": "mface",
                "file_id": str(data.get("id", data.get("emoji_id", ""))),
                "url": str(data.get("url", "")),
                "summary": str(data.get("summary", "")),
                "width": _safe_int(data.get("width")),
                "height": _safe_int(data.get("height")),
            })

        elif seg.type == "forward":
            attachments.append({
                "attachment_type": "forward",
                "file_id": str(data.get("id", "")),
            })

    return attachments


# ---------------------------------------------------------------------------
# @mention detection
# ---------------------------------------------------------------------------

def detect_mentions_bot(segments: list[OneBotMessageSegment], self_id: int) -> bool:
    """Check whether the bot's self_id appears in any @at segment."""
    for seg in segments:
        if seg.type == "at":
            qq_str = str(seg.data.get("qq", ""))
            if qq_str == "all" or qq_str == str(self_id):
                return True
    return False


def detect_reply_target(segments: list[OneBotMessageSegment]) -> int | None:
    """Extract the message id being replied to, if any."""
    for seg in segments:
        if seg.type == "reply":
            reply_id = seg.data.get("id", "")
            if reply_id:
                try:
                    return int(reply_id)
                except (ValueError, TypeError):
                    return None
    return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (ValueError, TypeError):
        return None


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None
