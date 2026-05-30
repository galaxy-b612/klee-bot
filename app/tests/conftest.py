"""Test fixtures for Klee Core — includes bridge test support."""

from __future__ import annotations

import socket
from pathlib import Path
from typing import AsyncGenerator

import pytest
from httpx import ASGITransport, AsyncClient

from app.database import init_db, override_db_url, reset_engine
from app.main import app


# ---------------------------------------------------------------------------
# TCP port fixtures for WS tests
# ---------------------------------------------------------------------------

def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def unused_tcp_port() -> int:
    return _find_free_port()


@pytest.fixture
def unused_tcp_port_factory():
    def _factory() -> int:
        return _find_free_port()
    return _factory


# ---------------------------------------------------------------------------
# Config override
# ---------------------------------------------------------------------------

TEST_CONFIG_DIR = Path(__file__).resolve().parent.parent.parent / "config"


@pytest.fixture(autouse=True)
def _override_config(monkeypatch):
    """Force config loader to use the project config/ directory."""
    monkeypatch.setenv("KLEE_CORE_CONFIG_DIR", str(TEST_CONFIG_DIR))
    import app.config as cfg
    cfg._config = None
    cfg._config_dir = None
    cfg.load_config(TEST_CONFIG_DIR)


# ---------------------------------------------------------------------------
# Database — in-memory SQLite
# ---------------------------------------------------------------------------

TEST_DB_URL = "sqlite+aiosqlite:///:memory:"


@pytest.fixture(autouse=True)
async def _test_database():
    """Set up in-memory SQLite database before each test module."""
    override_db_url(TEST_DB_URL)
    await reset_engine()
    await init_db()
    yield
    await reset_engine()


# ---------------------------------------------------------------------------
# Forward service — no-op
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _mock_forward(monkeypatch):
    """Prevent tests from making actual HTTP calls."""
    from app.services import forward_service
    monkeypatch.setattr(forward_service, "schedule_forward", lambda *a, **kw: None)
    monkeypatch.setattr(forward_service, "forward_event", _mock_forward_fn)


async def _mock_forward_fn(*args, **kwargs):
    return {"astrbot": True, "yunzai": True, "hermes": True, "meme": True}


# ---------------------------------------------------------------------------
# Singleton reset
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_singletons(monkeypatch):
    """Reset all router singletons so tests get fresh state."""
    import app.routers.onebot as ob
    monkeypatch.setattr(ob, "_intent_router", None)
    monkeypatch.setattr(ob, "_plugin_router", None)
    monkeypatch.setattr(ob, "_reply_gate", None)
    monkeypatch.setattr(ob, "_tracker", None)


# ---------------------------------------------------------------------------
# bq.json path override for API-level tests
# ---------------------------------------------------------------------------

SAMPLE_BQ_JSON = (
    b'{"accelerate":{"keywords":["\xe5\x8a\xa0\xe9\x80\x9f"]},'
    b'"hug":{"keywords":["\xe6\x8a\xb1"]},'
    b'"eat":{"keywords":["\xe5\x90\x83"]}}'
)


@pytest.fixture(autouse=True)
def _override_bq_path(tmp_path, monkeypatch):
    """Provide a minimal bq.json for API tests so IntentRouter loads cleanly."""
    bq_file = tmp_path / "bq.json"
    bq_file.write_bytes(SAMPLE_BQ_JSON)
    monkeypatch.setenv("KLEE_CORE_BQ_PATH", str(bq_file))


# ---------------------------------------------------------------------------
# Async HTTP client
# ---------------------------------------------------------------------------

@pytest.fixture
async def client() -> AsyncGenerator[AsyncClient, None]:
    """Async HTTP client for testing the FastAPI app."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
