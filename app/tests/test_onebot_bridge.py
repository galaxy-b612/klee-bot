"""Tests for OneBot v11 WebSocket Bridge.

Covers: UpstreamNapCatClient WS server, DownstreamOneBotWSClient with
reconnection, OneBotEventBus broadcast/routing, OneBotAPIProxy request/response.
"""

from __future__ import annotations

import asyncio
import json
from typing import Optional

import pytest
import websockets
from websockets.asyncio.client import ClientConnection
from websockets.asyncio.server import ServerConnection, serve

from app.services.onebot_bridge.types import (
    DownstreamConfig,
    OneBotAPIRequest,
    OneBotAPIResponse,
    OneBotEvent,
)
from app.services.onebot_bridge.upstream import UpstreamNapCatClient
from app.services.onebot_bridge.downstream import DownstreamOneBotWSClient
from app.services.onebot_bridge.event_bus import OneBotEventBus
from app.services.onebot_bridge.api_proxy import OneBotAPIProxy


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SAMPLE_GROUP_MSG = json.dumps({
    "time": 1700000000,
    "self_id": 1432028231,
    "post_type": "message",
    "message_type": "group",
    "sub_type": "normal",
    "message_id": 10001,
    "group_id": 915443332,
    "user_id": 123456789,
    "message": [{"type": "text", "data": {"text": "Hello"}}],
    "raw_message": "Hello",
    "sender": {"user_id": 123456789, "nickname": "Test"},
})


SAMPLE_API_REQ = json.dumps({
    "action": "send_group_msg",
    "params": {"group_id": 915443332, "message": "test reply"},
    "echo": "req_001",
})


async def start_mock_downstream_server(port: int) -> tuple[asyncio.Task, list[str]]:
    """Start a mock AstrBot/Yunzai WS server. Returns (task, received_messages)."""
    received: list[str] = []

    async def handler(ws: ServerConnection):
        try:
            async for msg in ws:
                received.append(msg if isinstance(msg, str) else msg.decode())
        except websockets.exceptions.ConnectionClosed:
            pass

    server = await serve(handler, "127.0.0.1", port)
    task = asyncio.create_task(server.serve_forever())
    # Give it a moment to start
    await asyncio.sleep(0.05)
    return task, received


# ---------------------------------------------------------------------------
# UpstreamNapCatClient
# ---------------------------------------------------------------------------

class TestUpstreamClient:
    @pytest.mark.asyncio
    async def test_start_stop(self, unused_tcp_port):
        """Upstream server starts and stops cleanly."""
        events: list[OneBotEvent] = []

        async def handler(ev, src):
            events.append(ev)

        upstream = UpstreamNapCatClient(
            host="127.0.0.1", port=unused_tcp_port,
            on_event=handler,
        )
        await upstream.start()
        assert upstream.is_running
        await upstream.stop()
        assert not upstream.is_running

    @pytest.mark.asyncio
    async def test_receive_event_from_napcat(self, unused_tcp_port):
        """NapCat connects and pushes an event — it reaches our handler."""
        events: list[OneBotEvent] = []
        ready = asyncio.Event()

        async def handler(ev, src):
            events.append(ev)
            ready.set()

        upstream = UpstreamNapCatClient(
            host="127.0.0.1", port=unused_tcp_port,
            on_event=handler,
        )
        await upstream.start()

        # Simulate NapCat connecting and sending an event
        async with websockets.connect(f"ws://127.0.0.1:{unused_tcp_port}") as ws:
            await ws.send(SAMPLE_GROUP_MSG)
            await asyncio.wait_for(ready.wait(), timeout=5)

        assert len(events) == 1
        assert events[0].self_id == 1432028231
        assert events[0].post_type == "message"

        await upstream.stop()

    @pytest.mark.asyncio
    async def test_call_api_http_proxy(self, unused_tcp_port):
        """API call returns proper response structure."""
        upstream = UpstreamNapCatClient(
            host="127.0.0.1", port=unused_tcp_port,
        )
        await upstream.start()

        # The HTTP call will fail (no real NapCat), but should not crash
        req = OneBotAPIRequest(
            action="send_group_msg",
            params={"group_id": 915, "message": "hi"},
            echo="test_echo",
        )
        resp = await upstream.call_api(req)
        assert resp.status == "failed"
        assert resp.echo == "test_echo"
        assert resp.retcode == 1

        await upstream.stop()


# ---------------------------------------------------------------------------
# DownstreamOneBotWSClient
# ---------------------------------------------------------------------------

class TestDownstreamClient:
    @pytest.mark.asyncio
    async def test_push_event_to_downstream(self, unused_tcp_port):
        """Klee Core pushes an event to downstream mock server."""
        task, received = await start_mock_downstream_server(unused_tcp_port)
        try:
            config = DownstreamConfig(
                name="astrbot", type="onebot_reverse_ws",
                url=f"ws://127.0.0.1:{unused_tcp_port}",
            )
            client = DownstreamOneBotWSClient(config=config)
            await client.start()

            # Wait for connection
            for _ in range(50):
                if client.is_connected:
                    break
                await asyncio.sleep(0.1)

            assert client.is_connected

            event = OneBotEvent.from_json(SAMPLE_GROUP_MSG)
            ok = await client.push_event(event)
            assert ok

            # Give it time to deliver
            await asyncio.sleep(0.2)
            assert len(received) == 1
            received_data = json.loads(received[0])
            assert received_data["message_type"] == "group"

            await client.stop()
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    @pytest.mark.asyncio
    async def test_dedup_skips_duplicate_event(self, unused_tcp_port):
        """Same event_id pushed twice → only received once."""
        task, received = await start_mock_downstream_server(unused_tcp_port)
        try:
            config = DownstreamConfig(
                name="yunzai", type="onebot_reverse_ws",
                url=f"ws://127.0.0.1:{unused_tcp_port}",
            )
            client = DownstreamOneBotWSClient(config=config)
            await client.start()

            for _ in range(50):
                if client.is_connected:
                    break
                await asyncio.sleep(0.1)

            event = OneBotEvent.from_json(SAMPLE_GROUP_MSG)
            await client.push_event(event)
            await client.push_event(event)  # duplicate
            await asyncio.sleep(0.2)

            assert len(received) == 1  # dedup worked

            await client.stop()
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    @pytest.mark.asyncio
    async def test_reconnect_on_disconnect(self, unused_tcp_port):
        """Client reconnects after server goes away and comes back."""
        task, received = await start_mock_downstream_server(unused_tcp_port)
        try:
            config = DownstreamConfig(
                name="astrbot", type="onebot_reverse_ws",
                url=f"ws://127.0.0.1:{unused_tcp_port}",
            )
            client = DownstreamOneBotWSClient(config=config)
            client.INITIAL_RECONNECT_DELAY = 0.1
            client.MAX_RECONNECT_DELAY = 0.5
            await client.start()

            for _ in range(50):
                if client.is_connected:
                    break
                await asyncio.sleep(0.1)
            assert client.is_connected

            # Kill the server
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

            await asyncio.sleep(0.3)
            # Client should have detected disconnect
            assert not client.is_connected

            # Restart the server
            task, received = await start_mock_downstream_server(unused_tcp_port)
            await asyncio.sleep(1.0)  # Wait for reconnect

            # Should be reconnected
            assert client.is_connected

            await client.stop()
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


# ---------------------------------------------------------------------------
# OneBotEventBus
# ---------------------------------------------------------------------------

class TestEventBus:
    @pytest.mark.asyncio
    async def test_broadcast_to_multiple_downstreams(self, unused_tcp_port, unused_tcp_port_factory):
        port_a = unused_tcp_port
        port_b = unused_tcp_port_factory()

        task_a, recv_a = await start_mock_downstream_server(port_a)
        task_b, recv_b = await start_mock_downstream_server(port_b)
        try:
            bus = OneBotEventBus()

            await bus.add_downstream(DownstreamConfig(
                name="astrbot", type="onebot_reverse_ws",
                url=f"ws://127.0.0.1:{port_a}",
            ))
            await bus.add_downstream(DownstreamConfig(
                name="yunzai", type="onebot_reverse_ws",
                url=f"ws://127.0.0.1:{port_b}",
            ))

            # Wait for connections
            for _ in range(100):
                if bus.get_client("astrbot") and bus.get_client("astrbot").is_connected:
                    if bus.get_client("yunzai") and bus.get_client("yunzai").is_connected:
                        break
                await asyncio.sleep(0.1)

            event = OneBotEvent.from_json(SAMPLE_GROUP_MSG)
            results = await bus.broadcast(event)
            assert results.get("astrbot") is True
            assert results.get("yunzai") is True

            await asyncio.sleep(0.3)
            assert len(recv_a) == 1
            assert len(recv_b) == 1

            await bus.stop_all()
        finally:
            for t in [task_a, task_b]:
                t.cancel()
                try:
                    await t
                except asyncio.CancelledError:
                    pass

    @pytest.mark.asyncio
    async def test_send_to_specific_downstream(self, unused_tcp_port, unused_tcp_port_factory):
        port_a = unused_tcp_port
        port_b = unused_tcp_port_factory()

        task_a, recv_a = await start_mock_downstream_server(port_a)
        task_b, recv_b = await start_mock_downstream_server(port_b)
        try:
            bus = OneBotEventBus()
            await bus.add_downstream(DownstreamConfig(
                name="astrbot", type="onebot_reverse_ws",
                url=f"ws://127.0.0.1:{port_a}",
            ))
            await bus.add_downstream(DownstreamConfig(
                name="yunzai", type="onebot_reverse_ws",
                url=f"ws://127.0.0.1:{port_b}",
            ))

            for _ in range(100):
                if bus.get_client("astrbot") and bus.get_client("astrbot").is_connected:
                    break
                await asyncio.sleep(0.1)

            event = OneBotEvent.from_json(SAMPLE_GROUP_MSG)
            ok = await bus.send_to("astrbot", event)
            assert ok

            await asyncio.sleep(0.3)
            assert len(recv_a) == 1
            assert len(recv_b) == 0  # yunzai should NOT receive

            await bus.stop_all()
        finally:
            for t in [task_a, task_b]:
                t.cancel()
                try:
                    await t
                except asyncio.CancelledError:
                    pass


# ---------------------------------------------------------------------------
# OneBotAPIProxy
# ---------------------------------------------------------------------------

class TestAPIProxy:
    @pytest.mark.asyncio
    async def test_handle_api_preserves_echo(self, unused_tcp_port, unused_tcp_port_factory):
        """API request → proxy → response with echo preserved."""
        up_port = unused_tcp_port
        ds_port = unused_tcp_port_factory()

        # Start upstream (acts as NapCat)
        upstream = UpstreamNapCatClient(
            host="127.0.0.1", port=up_port,
        )
        await upstream.start()

        # Start mock downstream WS server
        task, received = await start_mock_downstream_server(ds_port)
        try:
            bus = OneBotEventBus()
            proxy = OneBotAPIProxy(upstream=upstream, event_bus=bus)
            bus._on_api_request = proxy.handle_api  # type: ignore

            await bus.add_downstream(DownstreamConfig(
                name="astrbot", type="onebot_reverse_ws",
                url=f"ws://127.0.0.1:{ds_port}",
            ))

            for _ in range(100):
                if bus.get_client("astrbot") and bus.get_client("astrbot").is_connected:
                    break
                await asyncio.sleep(0.1)

            # Simulate downstream sending an API request
            # We need to send it through the WS connection...
            # The proxy.handle_api is called by the downstream client
            # when it receives an action request.
            # For testing, call it directly
            req = OneBotAPIRequest(
                action="send_group_msg",
                params={"group_id": 915, "message": "hi"},
                echo="echo_42",
            )
            await proxy.handle_api(req, "astrbot")

            # Wait for response to arrive at mock downstream
            await asyncio.sleep(0.5)

            # The response should be in received messages
            assert len(received) >= 1
            resp_data = json.loads(received[-1])
            assert resp_data["status"] == "failed"  # no real NapCat
            assert resp_data["echo"] == "echo_42"

            await bus.stop_all()
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            await upstream.stop()

    @pytest.mark.asyncio
    async def test_respond_error(self, unused_tcp_port, unused_tcp_port_factory):
        """respond_error sends a failed response without calling NapCat."""
        up_port = unused_tcp_port
        ds_port = unused_tcp_port_factory()

        upstream = UpstreamNapCatClient(host="127.0.0.1", port=up_port)
        await upstream.start()

        task, received = await start_mock_downstream_server(ds_port)
        try:
            bus = OneBotEventBus()
            proxy = OneBotAPIProxy(upstream=upstream, event_bus=bus)
            bus._on_api_request = proxy.handle_api  # type: ignore

            await bus.add_downstream(DownstreamConfig(
                name="astrbot", type="onebot_reverse_ws",
                url=f"ws://127.0.0.1:{ds_port}",
            ))

            for _ in range(100):
                if bus.get_client("astrbot") and bus.get_client("astrbot").is_connected:
                    break
                await asyncio.sleep(0.1)

            req = OneBotAPIRequest(action="unsupported_action", echo="echo_err")
            await proxy.respond_error(req, "astrbot", "unsupported action")
            await asyncio.sleep(0.3)

            assert len(received) >= 1
            resp = json.loads(received[-1])
            assert resp["status"] == "failed"
            assert resp["echo"] == "echo_err"
            assert "unsupported" in resp["wording"]

            await bus.stop_all()
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            await upstream.stop()


# ---------------------------------------------------------------------------
# OneBotEvent parsing
# ---------------------------------------------------------------------------

class TestOneBotEventParsing:
    def test_from_json_group_message(self):
        event = OneBotEvent.from_json(SAMPLE_GROUP_MSG)
        assert event.self_id == 1432028231
        assert event.post_type == "message"
        assert event.event_id == "10001"
        assert event.data["group_id"] == 915443332

    def test_from_json_bytes(self):
        event = OneBotEvent.from_json(SAMPLE_GROUP_MSG.encode())
        assert event.self_id == 1432028231

    def test_api_request_parsing(self):
        req = OneBotAPIRequest.from_json(SAMPLE_API_REQ)
        assert req is not None
        assert req.action == "send_group_msg"
        assert req.echo == "req_001"
        assert req.params["group_id"] == 915443332

    def test_event_is_not_api_request(self):
        req = OneBotAPIRequest.from_json(SAMPLE_GROUP_MSG)
        assert req is None  # not an API request — it's an event


# ---------------------------------------------------------------------------
# OneBotAPIResponse
# ---------------------------------------------------------------------------

class TestAPIResponse:
    def test_to_json_with_echo(self):
        resp = OneBotAPIResponse(
            status="ok", retcode=0,
            data={"message_id": 12345}, echo="echo_1",
        )
        j = json.loads(resp.to_json())
        assert j["status"] == "ok"
        assert j["echo"] == "echo_1"
        assert j["data"]["message_id"] == 12345

    def test_to_json_failed(self):
        resp = OneBotAPIResponse(
            status="failed", retcode=1,
            wording="timeout", echo="echo_err",
        )
        j = json.loads(resp.to_json())
        assert j["status"] == "failed"
        assert j["echo"] == "echo_err"
        assert j["wording"] == "timeout"


# ---------------------------------------------------------------------------
# DownstreamConfig
# ---------------------------------------------------------------------------

class TestDownstreamConfig:
    def test_defaults(self):
        dc = DownstreamConfig(name="test", type="http", url="http://x")
        assert dc.enabled is True
        assert dc.access_token == ""
