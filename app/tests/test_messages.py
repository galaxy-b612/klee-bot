"""Tests for GET /messages/recent endpoint."""

import pytest
from httpx import AsyncClient

from app.tests.test_onebot import (
    SAMPLE_AT_BOT,
    SAMPLE_GROUP_TEXT,
    SAMPLE_IMAGE,
    SAMPLE_MIXED,
    SAMPLE_VOICE,
)


@pytest.mark.asyncio
async def test_recent_messages_empty(client: AsyncClient):
    response = await client.get("/messages/recent", params={"group_id": 915443332})
    assert response.status_code == 200

    data = response.json()
    assert data["group_id"] == 915443332
    assert data["messages"] == []
    assert data["count"] == 0


@pytest.mark.asyncio
async def test_recent_messages_after_events(client: AsyncClient):
    # Post several messages first
    events = [
        SAMPLE_GROUP_TEXT,
        SAMPLE_AT_BOT,
        SAMPLE_IMAGE,
        SAMPLE_MIXED,
        SAMPLE_VOICE,
    ]
    for ev in events:
        resp = await client.post("/onebot/event", json=ev)
        assert resp.status_code == 200

    # Query recent messages
    response = await client.get("/messages/recent", params={"group_id": 915443332})
    assert response.status_code == 200

    data = response.json()
    assert data["group_id"] == 915443332
    assert data["count"] == len(events)  # 5 messages
    assert len(data["messages"]) == len(events)

    # Messages should be in descending order (newest first)
    timestamps = [m["created_at"] for m in data["messages"]]
    assert timestamps == sorted(timestamps, reverse=True)


@pytest.mark.asyncio
async def test_recent_messages_with_limit(client: AsyncClient):
    # Post 5 messages
    for ev in [SAMPLE_GROUP_TEXT] * 5:
        await client.post("/onebot/event", json=ev)

    # Query with limit=3
    response = await client.get("/messages/recent", params={
        "group_id": 915443332,
        "limit": 3,
    })
    assert response.status_code == 200

    data = response.json()
    assert data["count"] == 3


@pytest.mark.asyncio
async def test_recent_messages_different_groups(client: AsyncClient):
    """Messages from group A should not appear in group B queries."""
    # Post to group A
    await client.post("/onebot/event", json=SAMPLE_GROUP_TEXT)

    # Post to group B (using klee_secondary)
    await client.post("/onebot/event", json={
        **SAMPLE_GROUP_TEXT,
        "self_id": 3750475856,
        "group_id": 29459043,
        "message_id": 20001,
    })

    # Query group A → should have 1 message
    resp_a = await client.get("/messages/recent", params={"group_id": 915443332})
    assert resp_a.json()["count"] == 1

    # Query group B → should have 1 message
    resp_b = await client.get("/messages/recent", params={"group_id": 29459043})
    assert resp_b.json()["count"] == 1


@pytest.mark.asyncio
async def test_recent_messages_include_attachments(client: AsyncClient):
    """Image messages should have attachment records in the response."""
    await client.post("/onebot/event", json=SAMPLE_IMAGE)

    response = await client.get("/messages/recent", params={"group_id": 915443332})
    data = response.json()

    msg = data["messages"][0]
    assert msg["message_type"] == "image"
    assert len(msg["attachments"]) == 1
    att = msg["attachments"][0]
    assert att["attachment_type"] == "image"
    assert att["url"] == "https://example.com/image.jpg"
    assert att["width"] == 800
    assert att["height"] == 600
    assert att["file_size"] == 102400


@pytest.mark.asyncio
async def test_recent_messages_missing_group_id(client: AsyncClient):
    """Missing required group_id parameter should return 422."""
    response = await client.get("/messages/recent")
    assert response.status_code == 422
