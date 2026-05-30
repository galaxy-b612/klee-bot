"""Tests for GET /health endpoint."""

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_health_returns_ok(client: AsyncClient):
    response = await client.get("/health")
    assert response.status_code == 200

    data = response.json()
    assert data["status"] == "ok"
    assert data["service"] == "klee-core"
    assert "version" in data
    assert "timestamp" in data


@pytest.mark.asyncio
async def test_health_content_type(client: AsyncClient):
    response = await client.get("/health")
    assert response.headers["content-type"].startswith("application/json")
