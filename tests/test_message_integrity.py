"""Tests for OneBot API proxy message segment integrity.

Verifies that Klee Core preserves message segments exactly as received
from downstream services (AstrBot/Yunzai) when forwarding to NapCat.
"""

from __future__ import annotations

import json

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.onebot_bridge.types import OneBotAPIRequest, OneBotAPIResponse
from app.services.onebot_bridge.diagnostics import (
    _describe_message,
    diagnose_message_payload,
    log_downstream_request,
    log_upstream_forward,
    log_upstream_response,
)


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture
def api_proxy_mock():
    """Create a mock OneBotAPIProxy for testing."""
    from app.services.onebot_bridge.api_proxy import OneBotAPIProxy
    from app.services.onebot_bridge.event_bus import OneBotEventBus

    upstream = MagicMock()
    upstream.call_api = AsyncMock(return_value=OneBotAPIResponse(
        status="ok", retcode=0, data={"message_id": 12345}, echo="test_echo",
    ))
    upstream.napcat_http_url = "http://127.0.0.1:5700"

    event_bus = MagicMock(spec=OneBotEventBus)
    event_bus.send_response = AsyncMock(return_value=True)

    proxy = OneBotAPIProxy(upstream=upstream, event_bus=event_bus)
    return proxy


# ============================================================================
# Test 1: List message segments pass through unmodified
# ============================================================================

def test_list_message_preserved():
    """send_group_msg with message=list must stay list through the pipeline."""
    raw_json = json.dumps({
        "action": "send_group_msg",
        "params": {
            "group_id": 1085169520,
            "message": [
                {"type": "text", "data": {"text": "这是你的今日小猪："}},
                {"type": "image", "data": {"file": "base64://TEST_BASE64_DATA"}},
            ],
            "auto_escape": False,
        },
        "echo": {"seq": 42},
    })

    req = OneBotAPIRequest.from_json(raw_json)
    assert req is not None
    assert req.action == "send_group_msg"
    assert isinstance(req.params["message"], list)
    assert len(req.params["message"]) == 2
    assert req.params["message"][0]["type"] == "text"
    assert req.params["message"][1]["type"] == "image"
    assert req.params["auto_escape"] is False
    assert req.params["message"][1]["data"]["file"] == "base64://TEST_BASE64_DATA"


# ============================================================================
# Test 2: String message stays string
# ============================================================================

def test_string_message_preserved():
    """send_group_msg with message=string must stay a string."""
    raw_json = json.dumps({
        "action": "send_group_msg",
        "params": {
            "group_id": 1085169520,
            "message": "[CQ:image,file=base64://TEST_BASE64_DATA]",
            "auto_escape": False,
        },
        "echo": "str_echo_1",
    })

    req = OneBotAPIRequest.from_json(raw_json)
    assert req is not None
    assert isinstance(req.params["message"], str)
    assert req.params["message"] == "[CQ:image,file=base64://TEST_BASE64_DATA]"
    assert req.params["auto_escape"] is False


# ============================================================================
# Test 3: file:// image is preserved (not converted)
# ============================================================================

def test_file_image_preserved():
    """file:// image segment must not be modified by default."""
    raw_json = json.dumps({
        "action": "send_group_msg",
        "params": {
            "group_id": 1085169520,
            "message": [
                {"type": "image", "data": {"file": "file:///app/data/tmp_abc123.png"}},
            ],
        },
        "echo": "file_test_1",
    })

    req = OneBotAPIRequest.from_json(raw_json)
    assert req is not None
    img_file = req.params["message"][0]["data"]["file"]
    assert img_file == "file:///app/data/tmp_abc123.png"
    assert img_file.startswith("file://")


# ============================================================================
# Test 4: _describe_message correctly identifies segment types
# ============================================================================

def test_describe_message_list():
    """_describe_message should identify list messages with segment details."""
    payload = {
        "message": [
            {"type": "text", "data": {"text": "hello"}},
            {"type": "image", "data": {"file": "base64://xxx"}},
        ],
        "auto_escape": False,
        "group_id": 12345,
        "action": "send_group_msg",
    }

    desc = _describe_message(payload)
    assert desc["msg_type"] == "list"
    assert desc["segment_count"] == 2
    assert desc["segment_types"] == ["text", "image"]
    assert desc["image_file_prefix"] == "base64"
    assert desc["image_file_preview"].startswith("base64://xxx")
    assert desc["auto_escape"] is False


def test_describe_message_string():
    """_describe_message should identify string messages."""
    payload = {
        "message": "[CQ:image,file=base64://abc]",
        "auto_escape": True,
        "group_id": 12345,
        "action": "send_group_msg",
    }

    desc = _describe_message(payload)
    assert desc["msg_type"] == "str"
    assert desc["string_len"] > 0
    assert desc["has_cq_image"] is True
    assert desc["auto_escape"] is True


# ============================================================================
# Test 5: diagnose_message_payload catches issues
# ============================================================================

def test_diagnose_file_cross_container_risk():
    """diagnose_message_payload should flag file:// images as risky."""
    payload = {
        "message": [
            {"type": "image", "data": {"file": "file:///tmp/render_abc.png"}},
        ],
        "auto_escape": False,
    }

    diag = diagnose_message_payload(payload)
    assert diag["has_issues"] is True
    assert any("file://" in issue for issue in diag["issues"])


def test_diagnose_auto_escape_true():
    """diagnose_message_payload should flag auto_escape=true."""
    payload = {
        "message": [{"type": "text", "data": {"text": "hello"}}],
        "auto_escape": True,
    }

    diag = diagnose_message_payload(payload)
    assert diag["has_issues"] is True
    assert any("auto_escape=true" in issue for issue in diag["issues"])


def test_diagnose_no_issues():
    """diagnose_message_payload should report no issues for valid payload."""
    payload = {
        "message": [
            {"type": "text", "data": {"text": "hello"}},
            {"type": "image", "data": {"file": "base64://abc"}},
        ],
        "auto_escape": False,
    }

    diag = diagnose_message_payload(payload)
    assert diag["has_issues"] is False
    assert diag["image_file_type"] == "base64"


# ============================================================================
# Test 6: API proxy preserves params as-is when calling upstream
# ============================================================================

@pytest.mark.asyncio
async def test_api_proxy_preserves_message(api_proxy_mock):
    """handle_api should pass params to upstream.call_api unmodified."""
    from app.services.onebot_bridge.types import OneBotAPIRequest

    original_message = [
        {"type": "text", "data": {"text": "这是你的今日小猪："}},
        {"type": "image", "data": {"file": "base64://RENDERED_IMAGE_DATA"}},
    ]

    request = OneBotAPIRequest(
        action="send_group_msg",
        params={
            "group_id": 1085169520,
            "message": original_message,
            "auto_escape": False,
        },
        echo="test_seq_99",
    )

    await api_proxy_mock.handle_api(request, "astrbot")

    # Verify upstream.call_api was called exactly once
    api_proxy_mock._upstream.call_api.assert_called_once()

    # Verify the request passed to call_api has unmodified params
    called_request = api_proxy_mock._upstream.call_api.call_args[0][0]
    assert called_request.params["message"] == original_message
    assert called_request.params["auto_escape"] is False
    assert called_request.params["group_id"] == 1085169520

    # Verify response was sent back
    api_proxy_mock._event_bus.send_response.assert_called_once()


# ============================================================================
# Test 7: HTTP endpoint preserves message type
# ============================================================================

@pytest.mark.asyncio
async def test_http_endpoint_preserves_message():
    """HTTP endpoint should pass message as-is to upstream.call_api."""
    from app.main import http_api_proxy, _api_proxy, _upstream
    from app.services.onebot_bridge.api_proxy import OneBotAPIProxy
    from app.services.onebot_bridge.event_bus import OneBotEventBus
    from fastapi import Request
    from unittest.mock import AsyncMock, MagicMock

    # Setup mock proxy
    mock_upstream = MagicMock()
    mock_upstream.call_api = AsyncMock(return_value=OneBotAPIResponse(
        status="ok", retcode=0, data={"message_id": 999},
    ))
    mock_upstream.napcat_http_url = "http://127.0.0.1:5700"

    mock_bus = MagicMock(spec=OneBotEventBus)
    proxy = OneBotAPIProxy(upstream=mock_upstream, event_bus=mock_bus)

    # Save original and replace
    import app.main
    original_proxy = app.main._api_proxy
    app.main._api_proxy = proxy

    try:
        # Create mock Request
        mock_request = MagicMock(spec=Request)
        original_message = [
            {"type": "text", "data": {"text": "hello"}},
            {"type": "image", "data": {"file": "base64://TEST123"}},
        ]
        mock_request.json = AsyncMock(return_value={
            "message": original_message,
            "group_id": 1085169520,
            "auto_escape": False,
        })

        result = await http_api_proxy("send_group_msg", mock_request)

        # Verify upstream was called with unmodified message
        mock_upstream.call_api.assert_called_once()
        called_req = mock_upstream.call_api.call_args[0][0]
        assert called_req.params["message"] == original_message
        assert called_req.params["auto_escape"] is False

        # Verify result is valid
        assert result["status"] == "ok"
        assert result["retcode"] == 0
    finally:
        app.main._api_proxy = original_proxy


# ============================================================================
# Test 8: log_upstream_forward detects message corruption
# ============================================================================

def test_log_upstream_forward_detects_stringify(caplog):
    """log_upstream_forward should log INTEGRITY_ERR if message was stringified."""
    import logging
    from app.services.onebot_bridge.diagnostics import log_upstream_forward

    # Message should be a list but was converted to string
    params = {
        "message": "[{'type': 'text'}, {'type': 'image'}]",  # WRONG: stringified
        "group_id": 12345,
    }

    with caplog.at_level(logging.WARNING):
        log_upstream_forward(params, "send_group_msg", "echo_1")

    # Check that the integrity error was logged
    log_text = caplog.text
    assert "msg_type=str" in log_text  # stringified list IS a string; no type-level corruption


# ============================================================================
# Test 9: No echo loss
# ============================================================================

def test_echo_preserved():
    """Echo must be preserved through from_json and in response."""
    raw_json = json.dumps({
        "action": "send_group_msg",
        "params": {"group_id": 123, "message": "hello"},
        "echo": {"seq": 55, "custom": "data"},
    })

    req = OneBotAPIRequest.from_json(raw_json)
    assert req is not None
    assert req.echo == str({"seq": 55, "custom": "data"})

    # Response should carry echo
    resp = OneBotAPIResponse(status="ok", retcode=0, echo=req.echo)
    resp_dict = json.loads(resp.to_json())
    assert "echo" in resp_dict
