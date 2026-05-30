"""Unit tests for the OneBot adapter and message normalization logic.

These are pure function tests — no HTTP or database involved.
"""

import pytest

from app.adapters.onebot_adapter import (
    detect_mentions_bot,
    detect_message_type,
    detect_reply_target,
    extract_attachments,
    extract_normalized_text,
    extract_text,
)
from app.schemas.onebot import OneBotMessageSegment


# ---------------------------------------------------------------------------
# Helpers to build segments quickly
# ---------------------------------------------------------------------------

def text_seg(text: str) -> OneBotMessageSegment:
    return OneBotMessageSegment(type="text", data={"text": text})


def at_seg(qq: str) -> OneBotMessageSegment:
    return OneBotMessageSegment(type="at", data={"qq": qq})


def image_seg(url: str = "", file: str = "") -> OneBotMessageSegment:
    return OneBotMessageSegment(type="image", data={"url": url, "file": file})


def voice_seg(duration: int = 5) -> OneBotMessageSegment:
    return OneBotMessageSegment(type="record", data={"duration": duration})


def video_seg() -> OneBotMessageSegment:
    return OneBotMessageSegment(type="video", data={})


def file_seg() -> OneBotMessageSegment:
    return OneBotMessageSegment(type="file", data={"name": "doc.pdf"})


def reply_seg(msg_id: str) -> OneBotMessageSegment:
    return OneBotMessageSegment(type="reply", data={"id": msg_id})


def face_seg(face_id: str = "123") -> OneBotMessageSegment:
    return OneBotMessageSegment(type="face", data={"id": face_id})


def forward_seg() -> OneBotMessageSegment:
    return OneBotMessageSegment(type="forward", data={"id": "fw_001"})


# ---------------------------------------------------------------------------
# detect_message_type
# ---------------------------------------------------------------------------

class TestDetectMessageType:
    def test_text(self):
        assert detect_message_type([text_seg("Hello")]) == "text"

    def test_empty_segments(self):
        assert detect_message_type([]) == "text"

    def test_image_only(self):
        assert detect_message_type([image_seg()]) == "image"

    def test_voice_only(self):
        assert detect_message_type([voice_seg()]) == "voice"

    def test_video_only(self):
        assert detect_message_type([video_seg()]) == "video"

    def test_file_only(self):
        assert detect_message_type([file_seg()]) == "file"

    def test_text_plus_image(self):
        assert detect_message_type([text_seg("看"), image_seg()]) == "mixed"

    def test_text_plus_face(self):
        """Face is decorative — should still be 'text'."""
        assert detect_message_type([face_seg(), text_seg("哈哈")]) == "text"

    def test_reply_with_text(self):
        assert detect_message_type([reply_seg("100"), text_seg("回复")]) == "reply"

    def test_reply_with_image(self):
        assert detect_message_type([reply_seg("100"), image_seg()]) == "mixed"

    def test_forward(self):
        assert detect_message_type([forward_seg()]) == "forward"

    def test_multiple_media_types(self):
        """Image + voice → mixed."""
        assert detect_message_type([image_seg(), voice_seg()]) == "mixed"

    def test_at_only(self):
        """@mention only → text."""
        assert detect_message_type([at_seg("123")]) == "text"

    def test_at_plus_text(self):
        assert detect_message_type([at_seg("123"), text_seg("你好")]) == "text"


# ---------------------------------------------------------------------------
# extract_text / extract_normalized_text
# ---------------------------------------------------------------------------

class TestTextExtraction:
    def test_simple_text(self):
        segs = [text_seg("Hello world")]
        assert extract_text(segs) == "Hello world"
        assert extract_normalized_text(segs) == "Hello world"

    def test_multiple_text_segments(self):
        segs = [text_seg("Hello "), text_seg("world")]
        assert extract_text(segs) == "Hello world"

    def test_mixed_with_media(self):
        segs = [text_seg("看这个"), image_seg(), text_seg("哈哈哈")]
        assert extract_text(segs) == "看这个哈哈哈"
        assert extract_normalized_text(segs) == "看这个哈哈哈"

    def test_at_segment_not_in_text(self):
        """@ segments are NOT text — they don't appear in extracted text."""
        segs = [at_seg("123"), text_seg(" 你好")]
        assert extract_text(segs) == " 你好"
        assert extract_normalized_text(segs) == "你好"

    def test_empty_segments(self):
        assert extract_text([]) == ""
        assert extract_normalized_text([]) == ""


# ---------------------------------------------------------------------------
# detect_mentions_bot
# ---------------------------------------------------------------------------

class TestMentionDetection:
    BOT_ID = 1432028231

    def test_bot_is_mentioned(self):
        segs = [at_seg(str(self.BOT_ID)), text_seg("hello")]
        assert detect_mentions_bot(segs, self.BOT_ID) is True

    def test_bot_not_mentioned(self):
        segs = [text_seg("hello")]
        assert detect_mentions_bot(segs, self.BOT_ID) is False

    def test_other_user_mentioned(self):
        segs = [at_seg("999999"), text_seg("hello")]
        assert detect_mentions_bot(segs, self.BOT_ID) is False

    def test_at_all(self):
        segs = [at_seg("all"), text_seg("大家好")]
        assert detect_mentions_bot(segs, self.BOT_ID) is True

    def test_multiple_at_segments(self):
        segs = [at_seg("999"), text_seg(" "), at_seg(str(self.BOT_ID))]
        assert detect_mentions_bot(segs, self.BOT_ID) is True


# ---------------------------------------------------------------------------
# detect_reply_target
# ---------------------------------------------------------------------------

class TestReplyDetection:
    def test_reply_found(self):
        segs = [reply_seg("12345"), text_seg("回复")]
        assert detect_reply_target(segs) == 12345

    def test_no_reply(self):
        segs = [text_seg("hello")]
        assert detect_reply_target(segs) is None

    def test_invalid_reply_id(self):
        segs = [reply_seg("not_a_number"), text_seg("x")]
        assert detect_reply_target(segs) is None

    def test_empty_reply_id(self):
        segs = [reply_seg(""), text_seg("x")]
        assert detect_reply_target(segs) is None


# ---------------------------------------------------------------------------
# extract_attachments
# ---------------------------------------------------------------------------

class TestAttachmentExtraction:
    def test_image_attachment(self):
        segs = [
            OneBotMessageSegment(
                type="image",
                data={"file": "img001", "url": "https://x.com/a.jpg",
                      "file_size": "102400", "width": "800", "height": "600"},
            ),
        ]
        atts = extract_attachments(segs)
        assert len(atts) == 1
        att = atts[0]
        assert att["attachment_type"] == "image"
        assert att["file_id"] == "img001"
        assert att["url"] == "https://x.com/a.jpg"
        assert att["file_size"] == 102400
        assert att["width"] == 800
        assert att["height"] == 600

    def test_voice_attachment(self):
        segs = [voice_seg(duration=10)]
        atts = extract_attachments(segs)
        assert len(atts) == 1
        assert atts[0]["attachment_type"] == "voice"
        assert atts[0]["duration"] == 10.0

    def test_video_attachment(self):
        segs = [video_seg()]
        atts = extract_attachments(segs)
        assert len(atts) == 1
        assert atts[0]["attachment_type"] == "video"

    def test_file_attachment(self):
        segs = [file_seg()]
        atts = extract_attachments(segs)
        assert len(atts) == 1
        assert atts[0]["attachment_type"] == "file"
        assert atts[0]["summary"] == "doc.pdf"

    def test_face_attachment(self):
        segs = [face_seg("178")]
        atts = extract_attachments(segs)
        assert len(atts) == 1
        assert atts[0]["attachment_type"] == "face"
        assert atts[0]["file_id"] == "178"

    def test_forward_attachment(self):
        segs = [forward_seg()]
        atts = extract_attachments(segs)
        assert len(atts) == 1
        assert atts[0]["attachment_type"] == "forward"

    def test_mixed_attachments(self):
        segs = [image_seg(), text_seg("hey"), voice_seg()]
        atts = extract_attachments(segs)
        assert len(atts) == 2
        types = {a["attachment_type"] for a in atts}
        assert types == {"image", "voice"}

    def test_no_attachments(self):
        segs = [text_seg("plain text only")]
        assert extract_attachments(segs) == []

    def test_none_data_handled(self):
        """Segment with no data dict."""
        seg = OneBotMessageSegment(type="image")
        atts = extract_attachments([seg])
        assert len(atts) == 1
        assert atts[0]["file_id"] == ""
        assert atts[0]["url"] == ""

    def test_file_size_string_conversion(self):
        """file_size as string should be converted to int."""
        segs = [
            OneBotMessageSegment(
                type="image", data={"file_size": "2048"}
            ),
        ]
        atts = extract_attachments(segs)
        assert atts[0]["file_size"] == 2048

    def test_invalid_file_size(self):
        """Non-numeric file_size should be ignored (None)."""
        segs = [
            OneBotMessageSegment(
                type="image", data={"file_size": "not_a_number"}
            ),
        ]
        atts = extract_attachments(segs)
        assert atts[0]["file_size"] is None
