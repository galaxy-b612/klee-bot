"""Protocol diagnostics — structured logging and message inspection for OneBot API proxy."""

from __future__ import annotations

import json
import logging
from typing import Any

from app.services.onebot_bridge.types import OneBotAPIRequest, OneBotAPIResponse

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Message segment diagnostics
# ---------------------------------------------------------------------------

def _describe_message(payload: dict[str, Any] | None) -> dict[str, Any]:
    """Describe a message payload for structured logging. Returns a compact dict."""
    if payload is None:
        return {'type': 'none'}
    msg = payload.get('message')
    result: dict[str, Any] = {
        'msg_type': type(msg).__name__ if msg is not None else 'none',
    }
    if isinstance(msg, list):
        result['segment_count'] = len(msg)
        result['segment_types'] = [seg.get('type', '?') if isinstance(seg, dict) else '?' for seg in msg]
        image_segs = [seg for seg in msg if isinstance(seg, dict) and seg.get('type') == 'image']
        if image_segs:
            img = image_segs[0].get('data', {}).get('file', '')
            prefix = img.split('://')[0] if '://' in img else ('unknown' if img else 'empty')
            result['image_file_prefix'] = prefix
            result['image_file_preview'] = img[:60] if img else '(empty)'
    elif isinstance(msg, str):
        result['string_len'] = len(msg)
        result['has_cq_image'] = '[CQ:image' in msg
        result['preview'] = msg[:60]
    result['auto_escape'] = payload.get('auto_escape', 'not_set')
    result['group_id'] = payload.get('group_id', '?')
    result['action'] = payload.get('action', '?')
    return result


def diagnose_message_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Diagnose a OneBot send_group_msg payload for potential issues.

    Returns a dict with:
        - type: message type (string/list)
        - segment_count: number of segments (if list)
        - segment_types: ordered list of segment types
        - image_file_type: file prefix for image segments (file/http/base64/unknown)
        - cross_container_file_risk: True if file:// and may be inaccessible across containers
        - auto_escape_concern: True if auto_escape=true which prevents CQ code parsing
        - issues: list of potential problems
    """
    result = _describe_message(payload)
    issues: list[str] = []

    msg = payload.get('message')
    auto_escape = payload.get('auto_escape', None)

    # Check auto_escape
    if auto_escape is True:
        issues.append('auto_escape=true may prevent CQ code parsing on target')
    elif auto_escape is False:
        result['auto_escape_note'] = 'auto_escape=false (CQ codes will be parsed)'
    else:
        result['auto_escape_note'] = 'auto_escape not set (default depends on implementation)'

    # Check for cross-container file:// risk
    if isinstance(msg, list):
        for seg in msg:
            if isinstance(seg, dict) and seg.get('type') == 'image':
                file_val = seg.get('data', {}).get('file', '')
                if file_val.startswith('file://'):
                    issues.append(f'file:// image may be inaccessible across containers: {file_val[:80]}')
                elif file_val.startswith('base64://'):
                    result['image_file_type'] = 'base64'
                elif file_val.startswith('http'):
                    result['image_file_type'] = 'http'
    elif isinstance(msg, str):
        if '[CQ:image,file=file://' in msg:
            issues.append('string message contains file:// CQ code may be inaccessible across containers')

    result['issues'] = issues
    result['has_issues'] = len(issues) > 0

    return result


# ---------------------------------------------------------------------------
# Structured protocol logging
# ---------------------------------------------------------------------------

def log_downstream_request(raw_json: str, source: str) -> dict[str, Any]:
    """Log and describe a downstream API request.

    Returns the description dict for use in subsequent logs.
    """
    try:
        data = json.loads(raw_json)
    except json.JSONDecodeError:
        logger.warning(
            'DOWNSTREAM_RAW_REQUEST [%s] action=? echo=? Malformed JSON: %.200s',
            source, raw_json,
        )
        return {'action': '?', 'echo': '?'}

    action = data.get('action', '?')
    echo = str(data.get('echo', '?'))
    params = data.get('params', {})

    # Get message description
    msg_desc = _describe_message(params)
    msg_desc['action'] = action
    msg_desc['echo'] = echo

    # Log line
    parts = [
        f'DOWNSTREAM_RAW_REQUEST [{source}]',
        f'action={action}',
        f'echo={echo}',
        f'msg_type={msg_desc.get("msg_type", "?")}',
    ]
    if 'segment_count' in msg_desc:
        parts.append(f'segments={msg_desc["segment_count"]}')
        parts.append(f'segment_types={msg_desc["segment_types"]}')
    if 'image_file_prefix' in msg_desc:
        parts.append(f'image_prefix={msg_desc["image_file_prefix"]}')
        parts.append(f'image_preview={msg_desc["image_file_preview"]}')
    if 'string_len' in msg_desc:
        parts.append(f'msg_len={msg_desc["string_len"]}')
    if params.get('auto_escape') is not None:
        parts.append(f'auto_escape={params["auto_escape"]}')

    logger.warning(' | '.join(parts))

    return msg_desc


def log_upstream_forward(params: dict[str, Any], action: str, echo: str) -> None:
    """Log what Klee Core is forwarding to NapCat."""
    msg_desc = _describe_message({**params, 'action': action})
    parts = [
        'UPSTREAM_FORWARD_REQUEST',
        f'action={action}',
        f'echo={echo}',
        f'msg_type={msg_desc.get("msg_type", "?")}',
    ]
    if 'segment_count' in msg_desc:
        parts.append(f'segments={msg_desc["segment_count"]}')
        parts.append(f'segment_types={msg_desc["segment_types"]}')
        # Verify message integrity: list should stay list
        if not isinstance(params.get('message'), list):
            parts.append('INTEGRITY_ERR: message was stringified!')
    elif 'string_len' in msg_desc:
        parts.append(f'msg_len={msg_desc["string_len"]}')
        if not isinstance(params.get('message'), str):
            parts.append('INTEGRITY_ERR: message was converted from string!')
    if params.get('auto_escape') is not None:
        parts.append(f'auto_escape={params["auto_escape"]}')
    if 'image_file_prefix' in msg_desc:
        parts.append(f'image_prefix={msg_desc["image_file_prefix"]}')

    logger.warning(' | '.join(parts))


def log_upstream_response(response: OneBotAPIResponse, action: str, echo: str) -> None:
    """Log the response from NapCat."""
    parts = [
        'UPSTREAM_RESPONSE',
        f'action={action}',
        f'echo={echo}',
        f'status={response.status}',
        f'retcode={response.retcode}',
    ]
    if response.wording:
        parts.append(f'wording={str(response.wording)[:100]}')
    logger.warning(' | '.join(parts))
