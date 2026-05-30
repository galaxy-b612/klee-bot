"""Downstream — WebSocket clients that connect to AstrBot/Yunzai.

Klee Core acts as a reverse-WS client: it connects to AstrBot/Yunzai's
WS servers and pushes OneBot events. API action requests received from
downstream are forwarded to the UpstreamNapCatClient for execution.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Callable, Optional

import websockets
from websockets.asyncio.client import ClientConnection

from app.services.onebot_bridge.diagnostics import log_downstream_request
from app.services.onebot_bridge.types import (
    DownstreamConfig,
    OneBotAPIRequest,
    OneBotAPIResponse,
    OneBotEvent,
)

logger = logging.getLogger(__name__)


class DownstreamOneBotWSClient:
    """Connects to one downstream service (AstrBot or Yunzai).

    Pushes OneBot events; receives API action requests and proxies them
    upstream. Auto-reconnects on disconnect with exponential backoff.

    Usage:
        client = DownstreamOneBotWSClient(
            config=DownstreamConfig(name="astrbot", url="ws://..."),
            on_api_request=handle_api,
        )
        await client.start()
        await client.push_event(event)
    """

    MAX_RECONNECT_DELAY = 60.0
    INITIAL_RECONNECT_DELAY = 1.0

    def __init__(
        self,
        config: DownstreamConfig,
        on_api_request: Callable[[OneBotAPIRequest], Any] | None = None,
    ):
        self.config = config
        self._on_api_request = on_api_request
        self._ws: Optional[ClientConnection] = None
        self._running = False
        self._connected = False
        self._reconnect_delay = self.INITIAL_RECONNECT_DELAY
        self._task: Optional[asyncio.Task[None]] = None
        self._handler_tasks: set[asyncio.Task] = set()

        # Track forwarded event IDs to avoid duplicates
        self._forwarded_ids: set[str] = set()
        self._max_forwarded = 1000

    # ── Lifecycle ──────────────────────────────────────────

    async def start(self):
        """Start the connection loop (non-blocking)."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._connection_loop())
        logger.info("Downstream[%s] starting — %s", self.config.name, self.config.url)

    async def stop(self):
        """Stop the connection loop."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        # Cancel all pending handler tasks
        for t in list(self._handler_tasks):
            t.cancel()
        if self._handler_tasks:
            await asyncio.gather(*self._handler_tasks, return_exceptions=True)
            self._handler_tasks.clear()
        await self._disconnect()
        logger.info("Downstream[%s] stopped", self.config.name)

    # ── Connection loop ────────────────────────────────────

    async def _connection_loop(self):
        """Main loop: connect, handle messages, reconnect on failure."""
        while self._running:
            try:
                await self._connect()
                self._reconnect_delay = self.INITIAL_RECONNECT_DELAY
                await self._message_loop()
            except (websockets.exceptions.ConnectionClosed, OSError) as exc:
                logger.warning(
                    "Downstream[%s] disconnected: %s", self.config.name, exc,
                )
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception(
                    "Downstream[%s] unexpected error", self.config.name,
                )

            if not self._running:
                break

            logger.info(
                "Downstream[%s] reconnecting in %.1fs...",
                self.config.name, self._reconnect_delay,
            )
            await asyncio.sleep(self._reconnect_delay)
            self._reconnect_delay = min(
                self._reconnect_delay * 2, self.MAX_RECONNECT_DELAY,
            )

    async def _connect(self):
        """Establish WS connection to downstream."""
        await self._disconnect()
        extra_headers = {}
        if self.config.access_token:
            extra_headers["Authorization"] = f"Bearer {self.config.access_token}"

        headers = {
            "X-Client-Role": "Universal",
            "X-Self-ID": str(self.config.self_id) if self.config.self_id else "0",
        }
        if self.config.access_token:
            headers["Authorization"] = f"Bearer {self.config.access_token}"
        self._ws = await websockets.connect(
            self.config.url,
            additional_headers=headers,
            ping_interval=30,
            ping_timeout=10,
            close_timeout=5,
            max_size=100 * 1024 * 1024,  # 100MB, match NapCat
        )
        self._connected = True
        logger.info("Downstream[%s] connected", self.config.name)

    async def _disconnect(self):
        """Close WS connection if open."""
        self._connected = False
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None

    # ── Message handling ───────────────────────────────────

    async def _message_loop(self):
        """Receive messages from downstream (concurrent API processing)."""
        assert self._ws is not None
        async for message in self._ws:
            if not self._running:
                break
            if isinstance(message, bytes):
                message = message.decode("utf-8")
            task = asyncio.create_task(self._handle_message_task(message))
            self._handler_tasks.add(task)
            task.add_done_callback(self._handler_tasks.discard)

    async def _handle_message_task(self, raw: str):
        """Wrapper for _handle_message that catches all exceptions."""
        try:
            await self._handle_message(raw)
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception(
                "Downstream[%s] message handler failed", self.config.name,
            )

    async def _handle_message(self, raw: str):
        """Process a message from downstream (event or API request)."""
        # Check if it's an API request
        req = OneBotAPIRequest.from_json(raw)
        if req is not None:
            log_downstream_request(raw, self.config.name)
            logger.warning(
                "WS-IN: [%s] action=%s echo=%s len=%d",
                self.config.name, req.action, req.echo, len(raw),
            )
            logger.debug(
                "Downstream[%s] API request: action=%s echo=%s",
                self.config.name, req.action, req.echo,
            )
            if self._on_api_request:
                try:
                    await self._on_api_request(req)
                except Exception:
                    logger.exception(
                        "on_api_request failed for %s/%s",
                        self.config.name, req.action,
                    )
            return

        # Otherwise it's an event from downstream (e.g. self-sent message echo)
        logger.debug(
            "Downstream[%s] received non-API message", self.config.name,
        )

    # ── Event pushing ──────────────────────────────────────

    async def push_event(self, event: OneBotEvent) -> bool:
        """Send a OneBot event to this downstream.

        Returns True if sent successfully.
        """
        if not self._connected or not self._ws:
            return False

        # Dedup: skip if we already forwarded this event
        if event.event_id and event.event_id in self._forwarded_ids:
            return True  # already sent
        if event.event_id:
            self._forwarded_ids.add(event.event_id)
            if len(self._forwarded_ids) > self._max_forwarded:
                # Trim old entries (simple: clear half)
                self._forwarded_ids = set(
                    list(self._forwarded_ids)[-self._max_forwarded // 2:]
                )

        try:
            await self._ws.send(event.raw_json)
            return True
        except websockets.exceptions.ConnectionClosed:
            self._connected = False
            logger.warning(
                "Downstream[%s] push failed (disconnected)", self.config.name,
            )
            return False

    async def send_response(self, response: OneBotAPIResponse) -> bool:
        """Send an API response back to this downstream."""
        if not self._connected or not self._ws:
            return False
        try:
            await self._ws.send(response.to_json())
            return True
        except websockets.exceptions.ConnectionClosed:
            self._connected = False
            return False

    # ── Properties ─────────────────────────────────────────

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def name(self) -> str:
        return self.config.name
