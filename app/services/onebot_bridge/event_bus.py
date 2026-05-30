"""OneBot Event Bus — routes events from upstream to downstream services.

Manages multiple DownstreamOneBotWSClient instances. Routes events to
downstreams based on ExecutionPlan (Phase 2+) with support for:
  - broadcast: send to ALL
  - self_id_route: match by self_id
  - single: send to specific downstreams
  - multi: send to primary + observer targets (deduplicated)
  - observer: send to observer targets only
  - silent: no forwarding
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from app.services.onebot_bridge.downstream import DownstreamOneBotWSClient
from app.services.onebot_bridge.types import (
    DownstreamConfig,
    OneBotAPIRequest,
    OneBotAPIResponse,
    OneBotEvent,
)
from app.services.plugin_router import ExecutionPlan

logger = logging.getLogger(__name__)


class OneBotEventBus:
    """Event bus that manages downstream connections."""

    def __init__(
        self,
        on_api_request: Callable[[OneBotAPIRequest, str], Any] | None = None,
    ):
        self._clients: dict[str, DownstreamOneBotWSClient] = {}
        self._on_api_request = on_api_request

    # ------------------------------------------------------------------
    # Downstream management
    # ------------------------------------------------------------------

    async def add_downstream(self, config: DownstreamConfig):
        if not config.enabled:
            logger.info("Downstream[%s] disabled, skipping", config.name)
            return

        async def api_handler(req: OneBotAPIRequest):
            if self._on_api_request:
                await self._on_api_request(req, config.name)

        client = DownstreamOneBotWSClient(
            config=config, on_api_request=api_handler,
        )
        self._clients[config.name] = client
        await client.start()

    async def remove_downstream(self, name: str):
        client = self._clients.pop(name, None)
        if client:
            await client.stop()

    async def stop_all(self):
        for name in list(self._clients.keys()):
            await self.remove_downstream(name)

    # ------------------------------------------------------------------
    # Event routing
    # ------------------------------------------------------------------

    async def broadcast(self, event: OneBotEvent) -> dict[str, bool]:
        """Send an event to ALL connected downstreams."""
        results: dict[str, bool] = {}
        for name, client in self._clients.items():
            ok = await client.push_event(event)
            results[name] = ok
            self._log_send(event, name, "broadcast", ok)
        return results

    async def route_event(
        self,
        event: OneBotEvent,
        plan: Optional[ExecutionPlan] = None,
    ) -> dict[str, bool]:
        """Route event based on ExecutionPlan (Phase 2+).

        strategy support: broadcast, self_id_route, single, multi, observer, silent
        """
        if plan is None:
            return await self._route_by_self_id(event)

        strategy = plan.strategy

        if strategy == "silent":
            logger.debug("EventBus: silent — skipping event_id=%s", plan.event_id)
            return {}

        if strategy == "broadcast":
            return await self.broadcast(event)

        if strategy == "self_id_route":
            return await self._route_by_self_id(event)

        if strategy == "single":
            return await self._route_to_targets(
                event, plan.primary_targets, role="primary",
            )

        if strategy in ("multi", "observer"):
            targets = plan.send_targets
            if not targets:
                logger.debug("EventBus: %s strategy, no targets — silent", strategy)
                return {}

            results: dict[str, bool] = {}
            primary_set = set(plan.primary_targets)
            observer_set = set(plan.observer_targets)

            for name in targets:
                client = self._clients.get(name)
                if client is None:
                    logger.warning("EventBus: target '%s' not connected", name)
                    results[name] = False
                    continue

                # Determine role
                if name in primary_set and name in observer_set:
                    role = "primary+observer"
                elif name in primary_set:
                    role = "primary"
                elif name in observer_set:
                    role = "observer"
                else:
                    role = "unknown"

                ok = await client.push_event(event)
                results[name] = ok
                self._log_send(event, name, role, ok)

            return results

        logger.warning("EventBus: unknown strategy=%s -> self_id_route", strategy)
        return await self._route_by_self_id(event)

    # ------------------------------------------------------------------
    # Internal routing helpers
    # ------------------------------------------------------------------

    async def _route_by_self_id(self, event: OneBotEvent) -> dict[str, bool]:
        """Route event to downstream matching event self_id.

        Sends to matching self_id downstreams + wildcard (self_id=0) downstreams.
        """
        results: dict[str, bool] = {}
        target = event.self_id
        matched = False
        for name, client in self._clients.items():
            ds_sid = client.config.self_id
            if ds_sid == 0 or ds_sid == target:
                ok = await client.push_event(event)
                results[name] = ok
                role = "wildcard" if ds_sid == 0 else "self_id"
                self._log_send(event, name, role, ok)
                if ds_sid == target:
                    matched = True
        if not matched:
            for name, client in self._clients.items():
                if name not in results:
                    ok = await client.push_event(event)
                    results[name] = ok
                    self._log_send(event, name, "fallback", ok)
        return results

    async def _route_to_targets(
        self, event: OneBotEvent, targets: list[str], role: str = "primary",
    ) -> dict[str, bool]:
        """Route event to specific downstreams by name."""
        results: dict[str, bool] = {}
        for name in targets:
            client = self._clients.get(name)
            if client is None:
                logger.warning("EventBus: target '%s' not connected", name)
                results[name] = False
                continue
            ok = await client.push_event(event)
            results[name] = ok
            self._log_send(event, name, role, ok)
        return results

    # ------------------------------------------------------------------
    # Send logging
    # ------------------------------------------------------------------

    def _log_send(self, event, downstream_id: str, role: str, ok: bool):
        """Log EVENTBUS-SEND per requirement."""
        event_id = getattr(event, 'event_id', '') or ''
        group_id = event.data.get("group_id", 0) if hasattr(event, 'data') else 0
        self_id = event.self_id if hasattr(event, 'self_id') else 0
        result = "sent" if ok else "failed"
        reason = "ok" if ok else "connection_error"

        logger.warning(
            "EVENTBUS-SEND: event_id=%s downstream=%s role=%s "
            "self_id=%s group=%s result=%s reason=%s",
            event_id, downstream_id, role, self_id, group_id, result, reason,
        )

    # ------------------------------------------------------------------
    # Targeted send
    # ------------------------------------------------------------------

    async def send_to(self, target: str, event: OneBotEvent) -> bool:
        client = self._clients.get(target)
        if client is None:
            logger.warning("Target '%s' not found", target)
            return False
        return await client.push_event(event)

    # ------------------------------------------------------------------
    # API response routing
    # ------------------------------------------------------------------

    async def send_response(self, source: str, response: OneBotAPIResponse) -> bool:
        client = self._clients.get(source)
        if client is None:
            return False
        return await client.send_response(response)

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def client_count(self) -> int:
        return len(self._clients)

    @property
    def client_names(self) -> list[str]:
        return list(self._clients.keys())

    @property
    def configs(self) -> dict[str, DownstreamConfig]:
        return {name: client.config for name, client in self._clients.items()}

    def get_client(self, name: str) -> DownstreamOneBotWSClient | None:
        return self._clients.get(name)
