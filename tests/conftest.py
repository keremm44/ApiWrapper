"""Ortak test fixture'ları."""

from __future__ import annotations

import json
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app

TEST_API_KEY = "sk-test-key"
UPSTREAM_DOMAIN = "upstream.test"


def make_settings(**overrides) -> Settings:
    base = {
        "target_domain": UPSTREAM_DOMAIN,
        "api_keys": [TEST_API_KEY],
        "recaptcha_provider": "noop",
        "rate_limit_enabled": False,
        "log_json": True,
        "log_level": "WARNING",
        "models_file": "tests/data/models.yaml",
        "session_stateless": True,
        "upstream_http2": False,
        "retry_max_attempts": 1,
        "retry_base_delay": 0.01,
        "retry_max_delay": 0.02,
        "stream_idle_timeout": 5.0,
        "breaker_enabled": False,
    }
    base.update(overrides)
    return Settings(**base)


@pytest.fixture
def settings() -> Settings:
    return make_settings()


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    app = create_app(settings)
    with TestClient(app) as test_client:
        test_client.headers.update({"Authorization": f"Bearer {TEST_API_KEY}"})
        yield test_client


@pytest.fixture
def auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {TEST_API_KEY}"}


def ai_stream(*chunks: str, finish: bool = True, usage: dict | None = None) -> bytes:
    """Vercel AI SDK data-stream gövdesi üretir."""
    lines = ['f:{"messageId":"msg-test"}']
    lines.extend(f"0:{json.dumps(chunk, ensure_ascii=False)}" for chunk in chunks)
    if finish:
        payload = {
            "finishReason": "stop",
            "usage": usage or {"promptTokens": 11, "completionTokens": 7},
        }
        lines.append("e:" + json.dumps(payload))
        lines.append("d:" + json.dumps(payload))
    return ("\n".join(lines) + "\n").encode("utf-8")
