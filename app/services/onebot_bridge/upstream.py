"""Upstream — WebSocket server that NapCat connects to, + HTTP API client.

Klee Core runs a WS server. NapCat's `websocketClients` config connects
to this server and pushes OneBot v11 events as JSON text frames.

API calls (send_group_msg, etc.) are proxied to NapCat's HTTP API.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable, Optional

import httpx
import websockets
from websockets.asyncio.server import ServerConnection

from app.services.onebot_bridge.types import OneBotAPIRequest, OneBotAPIResponse, OneBotEvent

logger = logging.getLogger(__name__)


class UpstreamNapCatClient:
    """Receives events from NapCat via WebSocket; proxies API calls via HTTP.

    Usage:
        upstream = UpstreamNapCatClient(
            host="0.0.0.0", port=8099,
            napcat_http_url="http://127.0.0.1:5700",
            napcat_token="notification_bot_2024",
            on_event=my_event_handler,
        )
        await upstream.start()
    """

    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 8099,
        path: str = "/onebot/v11",
        napcat_http_url: str = "http://127.0.0.1:5700",
        napcat_token: str = "",
        on_event: Callable[[OneBotEvent, str], Any] | None = None,
    ):
        self.host = host
        self.port = port
        self.path = path
        self.napcat_http_url = napcat_http_url.rstrip("/")
        self.napcat_token = napcat_token
        self._on_event = on_event

        self._server: Optional[websockets.WebSocketServer] = None
        self._http_client: Optional[httpx.AsyncClient] = None
        self._running = False

    # ── Lifecycle ──────────────────────────────────────────

    async def start(self):
        """Start the WS server (non-blocking)."""
        self._http_client = httpx.AsyncClient(timeout=httpx.Timeout(30))
        self._running = True
        self._server = await websockets.serve(
            self._handle_connection, self.host, self.port,
            ping_interval=30, ping_timeout=10,
            max_size=100 * 1024 * 1024,  # 100MB, match NapCat
        )
        logger.info(
            "Upstream WS server listening on ws://%s:%d%s",
            self.host, self.port, self.path,
        )

    async def stop(self):
        """Stop the WS server and close HTTP client."""
        self._running = False
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        if self._http_client:
            await self._http_client.aclose()
            self._http_client = None
        logger.info("Upstream stopped")

    # ── WS connection handler ──────────────────────────────

    async def _handle_connection(self, ws: ServerConnection):
        """Handle one NapCat WS connection."""
        remote = ws.remote_address
        logger.info("NapCat connected: %s", remote)
        try:
            async for message in ws:
                if not self._running:
                    break
                if isinstance(message, bytes):
                    message = message.decode("utf-8")

                # Parse as OneBot event
                event = OneBotEvent.from_json(message)
                logger.debug(
                    "Upstream event: self_id=%s post_type=%s id=%s",
                    event.self_id, event.post_type, event.event_id,
                )

                if self._on_event:
                    try:
                        await self._on_event(event, "upstream_ws")
                    except Exception:
                        logger.exception("on_event handler failed")
        except websockets.exceptions.ConnectionClosed:
            logger.info("NapCat disconnected: %s", remote)
        except Exception:
            logger.exception("NapCat connection error: %s", remote)

    # ── API Proxy to NapCat ────────────────────────────────

    async def call_api(self, request: OneBotAPIRequest) -> OneBotAPIResponse:
        """Proxy an API action request to NapCat's HTTP API.

        Args:
            request: Parsed API request from downstream.

        Returns:
            OneBotAPIResponse with status, retcode, data, and echo.
        """
        if not self._http_client:
            return OneBotAPIResponse(
                status="failed", retcode=1,
                wording="upstream not connected", echo=request.echo,
            )

        url = f"{self.napcat_http_url}/{request.action}"
        headers = {"Content-Type": "application/json"}
        if self.napcat_token:
            headers["Authorization"] = f"Bearer {self.napcat_token}"

        try:
            resp = await self._http_client.post(
                url, json=request.params, headers=headers,
            )
            if resp.status_code == 200:
                data = resp.json()
                return OneBotAPIResponse(
                    status=data.get("status", "ok"),
                    retcode=data.get("retcode", 0),
                    data=data.get("data"),
                    echo=request.echo,
                )
            else:
                body = await resp.aread()
                logger.warning(
                    "NapCat API %s returned %d: %s",
                    request.action, resp.status_code, body[:200],
                )
                return OneBotAPIResponse(
                    status="failed", retcode=1,
                    wording=f"NapCat returned {resp.status_code}",
                    echo=request.echo,
                )
        except httpx.TimeoutException:
            logger.warning("NapCat API timeout: %s", request.action)
            return OneBotAPIResponse(
                status="failed", retcode=1,
                wording="request timeout", echo=request.echo,
            )
        except Exception as exc:
            logger.exception("NapCat API error: %s", request.action)
            return OneBotAPIResponse(
                status="failed", retcode=1,
                wording=str(exc)[:200], echo=request.echo,
            )

    @property
    def is_running(self) -> bool:
        return self._running
