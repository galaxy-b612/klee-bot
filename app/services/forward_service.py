"""Forward service — Hermes-only HTTP forwarding utility.

DOWNGRADED: No longer used for AstrBot/Yunzai event forwarding.
AstrBot and Yunzai receive events exclusively via WebSocket (OneBot WS Bridge).
This module is kept for Hermes HTTP webhook integration and future HTTP endpoints.

Supports parallel (concurrent) and ordered (sequential) forwarding strategies.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

import httpx

from app.config import BotConfig, get_config

logger = logging.getLogger(__name__)

_client: Optional[httpx.AsyncClient] = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        timeout = get_config().routing.timeout_seconds
        _client = httpx.AsyncClient(timeout=httpx.Timeout(timeout))
    return _client


async def close_client():
    global _client
    if _client and not _client.is_closed:
        await _client.aclose()
        _client = None


# ---------------------------------------------------------------------------
# Forward to a single endpoint
# ---------------------------------------------------------------------------

async def _forward_to_endpoint(
    endpoint_url: str,
    event_payload: dict[str, Any],
    bot_id: str,
) -> bool:
    """POST the event JSON to a single downstream endpoint (Hermes HTTP only)."""
    if not endpoint_url:
        return True

    try:
        client = _get_client()
        resp = await client.post(
            endpoint_url,
            json=event_payload,
            headers={
                "Content-Type": "application/json",
                "X-Klee-Bot-Id": bot_id,
            },
        )
        if resp.status_code < 400:
            logger.debug("Forwarded to %s -> %d", endpoint_url, resp.status_code)
            return True

        logger.warning(
            "Forward to %s returned %d: %s",
            endpoint_url, resp.status_code,
            (await resp.aread()).decode()[:200],
        )
        return False

    except httpx.TimeoutException:
        logger.warning("Forward timeout to %s", endpoint_url)
        return False
    except Exception:
        logger.exception("Forward error to %s", endpoint_url)
        return False


# ---------------------------------------------------------------------------
# Dispatch to configured endpoints (Hermes-only in production)
# ---------------------------------------------------------------------------

async def forward_event(
    event_payload: dict[str, Any],
    bot: BotConfig,
    targets: Optional[list[str]] = None,
) -> dict[str, bool]:
    """Forward an event payload to HTTP endpoints.

    NOTE: AstrBot and Yunzai receive events via WebSocket (OneBot WS Bridge),
    NOT via HTTP POST. This function is kept for Hermes HTTP webhook and
    future HTTP-based integrations.

    Args:
        event_payload: Event JSON as a dict.
        bot: BotConfig with downstream endpoints.
        targets: Optional list of endpoint names to target.
                 If None, all enabled endpoints are used.

    Returns:
        Dict mapping endpoint name to success/failure boolean.
    """
    cfg = get_config()
    strategy = cfg.routing.strategy
    enabled = cfg.routing.enabled_endpoints

    downstream = bot.downstream
    all_endpoints: dict[str, str] = {}

    if "astrbot" in enabled and downstream.astrbot_url:
        all_endpoints["astrbot"] = downstream.astrbot_url
    if "yunzai" in enabled and downstream.yunzai_url:
        all_endpoints["yunzai"] = downstream.yunzai_url
    if "hermes" in enabled and downstream.hermes_url:
        all_endpoints["hermes"] = downstream.hermes_url
    if "meme" in enabled and downstream.meme_url:
        all_endpoints["meme"] = downstream.meme_url

    # Filter to targets if specified
    if targets is not None:
        endpoints = {k: v for k, v in all_endpoints.items() if k in targets}
        if not endpoints:
            logger.debug("No matching targets in %s for bot %s", targets, bot.bot_id)
            return {}
    else:
        endpoints = all_endpoints

    if not endpoints:
        return {}

    if strategy == "parallel":
        return await _forward_parallel(event_payload, endpoints, bot.bot_id)
    else:
        return await _forward_ordered(event_payload, endpoints, bot.bot_id)


async def _forward_parallel(
    payload: dict[str, Any],
    endpoints: dict[str, str],
    bot_id: str,
) -> dict[str, bool]:
    tasks = {
        name: _forward_to_endpoint(url, payload, bot_id)
        for name, url in endpoints.items()
    }
    results = await asyncio.gather(*tasks.values(), return_exceptions=True)
    return {
        name: (r is True)
        for name, r in zip(tasks.keys(), results)
    }


async def _forward_ordered(
    payload: dict[str, Any],
    endpoints: dict[str, str],
    bot_id: str,
) -> dict[str, bool]:
    results: dict[str, bool] = {}
    for name, url in endpoints.items():
        ok = await _forward_to_endpoint(url, payload, bot_id)
        results[name] = ok
        if ok:
            break
    return results


# ---------------------------------------------------------------------------
# Background fire-and-forget (Hermes-only)
# ---------------------------------------------------------------------------

def schedule_forward(
    event_payload: dict[str, Any],
    bot: BotConfig,
    targets: Optional[list[str]] = None,
):
    """Schedule HTTP forwarding as a background task.

    Used for Hermes HTTP webhook. NOT used for AstrBot/Yunzai WS forwarding.
    """
    asyncio.create_task(_forward_safe(event_payload, bot, targets))


async def _forward_safe(
    event_payload: dict[str, Any],
    bot: BotConfig,
    targets: Optional[list[str]] = None,
):
    try:
        results = await forward_event(event_payload, bot, targets=targets)
        failures = [k for k, v in results.items() if not v]
        if failures:
            logger.info(
                "Forward results for bot=%s: successes=%d failures=%s",
                bot.bot_id,
                len(results) - len(failures),
                failures,
            )
    except Exception:
        logger.exception("Background forward failed for bot=%s", bot.bot_id)
