"""Shared fixtures. Tests never touch the network or the real data/ directory."""
from __future__ import annotations

import gc
import os
import sqlite3
from pathlib import Path

import pytest

from auction_agent.config import Settings

FIXTURES = Path(__file__).parent / "fixtures"
PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Settings the app (or the OpenAI SDK) reads from the environment: never inherited.
_ENV_PREFIXES = ("AUCTION_AGENT_", "OPENAI_", "LLM_", "GEMINI_", "TIER_")
_ENV_VARS = ["IDENTITY_HMAC_KEY", "BUYER_PROFILE", "ENGINE_RISK_MAX_TIER", "RELIST_DETAIL_CHECK", "LATE_CT_BUDGET"]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch, tmp_path):
    for name in list(os.environ):
        if name.startswith(_ENV_PREFIXES) or name in _ENV_VARS:
            monkeypatch.delenv(name, raising=False)
    # The owner's real .env (with their API key) is never read by tests.
    monkeypatch.setenv("AUCTION_AGENT_ENV_FILE", str(tmp_path / "absent.env"))


@pytest.fixture(autouse=True)
def connection_leak_guard(monkeypatch):
    """Fail any test that leaves a sqlite3 connection open."""
    opened: list = []
    real_connect = sqlite3.connect

    class TrackedConnection(sqlite3.Connection):
        def close(self):  # noqa: D401
            self._closed_by_us = True
            super().close()

    def tracking_connect(*args, **kwargs):
        kwargs.setdefault("factory", TrackedConnection)
        conn = real_connect(*args, **kwargs)
        opened.append(conn)
        return conn

    monkeypatch.setattr(sqlite3, "connect", tracking_connect)
    yield
    gc.collect()
    leaked = [c for c in opened if not getattr(c, "_closed_by_us", False)]
    for c in leaked:
        c.close()
    assert not leaked, f"{len(leaked)} sqlite connection(s) left open"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings.from_env(
        data_dir=tmp_path / "data",
        reports_dir=tmp_path / "reports",
        min_request_interval=0.0,
        respect_robots=True,
    )


def read_fixture(*parts: str) -> str:
    return (FIXTURES.joinpath(*parts)).read_text(encoding="utf-8")
