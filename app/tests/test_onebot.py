"""Tests for POST /onebot/event endpoint."""

import pytest
from httpx import AsyncClient


# ---------------------------------------------------------------------------
# Sample OneBot v11 message event (group text message)
# ---------------------------------------------------------------------------

SAMPLE_GROUP_TEXT = {
    "time": 1700000000,
    "self_id": 1432028231,       # klee_main
    "post_type": "message",
    "message_type": "group",
    "sub_type": "normal",
    "message_id": 10001,
    "group_id": 915443332,
    "user_id": 123456789,
    "anonymous": None,
    "message": [
        {"type": "text", "data": {"text": "Hello, Klee!"}},
    ],
    "raw_message": "Hello, Klee!",
    "font": 0,
    "sender": {
        "user_id": 123456789,
        "nickname": "TestUser",
        "card": "TestCard",
        "sex": "unknown",
        "age": 0,
    },
}


SAMPLE_GROUP_TEXT_ALT_GROUP = {
    **SAMPLE_GROUP_TEXT,
    "group_id": 29459043,       # klee_secondary's group
    "self_id": 3750475856,       # klee_secondary
    "message_id": 10002,
}


SAMPLE_AT_BOT = {
    **SAMPLE_GROUP_TEXT,
    "message_id": 10003,
    "message": [
        {"type": "at", "data": {"qq": "1432028231"}},
        {"type": "text", "data": {"text": " 你好呀"}},
    ],
    "raw_message": "@Klee 你好呀",
}


SAMPLE_IMAGE = {
    **SAMPLE_GROUP_TEXT,
    "message_id": 10004,
    "message": [
        {
            "type": "image",
            "data": {
                "file": "abc123.image",
                "url": "https://example.com/image.jpg",
                "file_size": "102400",
                "width": "800",
                "height": "600",
            },
        },
    ],
    "raw_message": "[图片]",
}


SAMPLE_MIXED = {
    **SAMPLE_GROUP_TEXT,
    "message_id": 10005,
    "message": [
        {"type": "text", "data": {"text": "看这个"}},
        {
            "type": "image",
            "data": {"file": "xyz.image", "url": "https://example.com/pic.png"},
        },
        {"type": "text", "data": {"text": "哈哈"}},
    ],
    "raw_message": "看这个[图片]哈哈",
}


SAMPLE_REPLY = {
    **SAMPLE_GROUP_TEXT,
    "message_id": 10006,
    "message": [
        {"type": "reply", "data": {"id": "10001"}},
        {"type": "text", "data": {"text": "我回复了你"}},
    ],
    "raw_message": "[回复]我回复了你",
}


SAMPLE_VOICE = {
    **SAMPLE_GROUP_TEXT,
    "message_id": 10007,
    "message": [
        {
            "type": "record",
            "data": {
                "file": "voice.file",
                "url": "https://example.com/voice.ogg",
                "duration": 5,
            },
        },
    ],
    "raw_message": "[语音]",
}


SAMPLE_FACE = {
    **SAMPLE_GROUP_TEXT,
    "message_id": 10008,
    "message": [
        {"type": "face", "data": {"id": "123"}},
        {"type": "text", "data": {"text": "笑死"}},
    ],
    "raw_message": "[表情]笑死",
}


SAMPLE_UNMANAGED_GROUP = {
    **SAMPLE_GROUP_TEXT,
    "group_id": 999999999,       # Not in any bot's groups
    "message_id": 10009,
}


SAMPLE_UNKNOWN_BOT = {
    **SAMPLE_GROUP_TEXT,
    "self_id": 999999999,       # Unknown self_id
    "message_id": 10010,
}


SAMPLE_NON_MESSAGE = {
    **SAMPLE_GROUP_TEXT,
    "post_type": "notice",
    "message_type": "group",
    "message_id": 10011,
}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_receive_simple_text_message(client: AsyncClient):
    response = await client.post("/onebot/event", json=SAMPLE_GROUP_TEXT)
    assert response.status_code == 200

    data = response.json()
    assert data["status"] == "ok"
    assert data["message_type"] == "text"
    assert data["bot_id"] == "klee_main"
    assert isinstance(data["message_id"], int)


@pytest.mark.asyncio
async def test_receive_at_bot_message(client: AsyncClient):
    response = await client.post("/onebot/event", json=SAMPLE_AT_BOT)
    assert response.status_code == 200

    data = response.json()
    assert data["status"] == "ok"
    assert data["message_type"] == "text"


@pytest.mark.asyncio
async def test_receive_image_message(client: AsyncClient):
    response = await client.post("/onebot/event", json=SAMPLE_IMAGE)
    assert response.status_code == 200

    data = response.json()
    assert data["message_type"] == "image"


@pytest.mark.asyncio
async def test_receive_mixed_message(client: AsyncClient):
    response = await client.post("/onebot/event", json=SAMPLE_MIXED)
    assert response.status_code == 200

    data = response.json()
    assert data["message_type"] == "mixed"


@pytest.mark.asyncio
async def test_receive_reply_message(client: AsyncClient):
    response = await client.post("/onebot/event", json=SAMPLE_REPLY)
    assert response.status_code == 200

    data = response.json()
    assert data["message_type"] == "reply"


@pytest.mark.asyncio
async def test_receive_voice_message(client: AsyncClient):
    response = await client.post("/onebot/event", json=SAMPLE_VOICE)
    assert response.status_code == 200

    data = response.json()
    assert data["message_type"] == "voice"


@pytest.mark.asyncio
async def test_receive_face_message(client: AsyncClient):
    response = await client.post("/onebot/event", json=SAMPLE_FACE)
    assert response.status_code == 200

    data = response.json()
    assert data["message_type"] == "text"  # face + text = text type (face is decorative)


@pytest.mark.asyncio
async def test_alt_group_goes_to_secondary_bot(client: AsyncClient):
    response = await client.post("/onebot/event", json=SAMPLE_GROUP_TEXT_ALT_GROUP)
    assert response.status_code == 200

    data = response.json()
    assert data["bot_id"] == "klee_secondary"


@pytest.mark.asyncio
async def test_unmanaged_group_is_ignored(client: AsyncClient):
    response = await client.post("/onebot/event", json=SAMPLE_UNMANAGED_GROUP)
    assert response.status_code == 200

    data = response.json()
    assert data["status"] == "ignored"


@pytest.mark.asyncio
async def test_unknown_bot_returns_404(client: AsyncClient):
    response = await client.post("/onebot/event", json=SAMPLE_UNKNOWN_BOT)
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_non_message_event_is_ignored(client: AsyncClient):
    response = await client.post("/onebot/event", json=SAMPLE_NON_MESSAGE)
    assert response.status_code == 200

    data = response.json()
    assert data["status"] == "ignored"
    assert data["reason"] == "post_type=notice"


@pytest.mark.asyncio
async def test_empty_message_body(client: AsyncClient):
    """Event with empty message array should still be accepted."""
    payload = {
        **SAMPLE_GROUP_TEXT,
        "message_id": 10020,
        "message": [],
        "raw_message": "",
    }
    response = await client.post("/onebot/event", json=payload)
    assert response.status_code == 200

    data = response.json()
    assert data["message_type"] == "text"  # empty → text
