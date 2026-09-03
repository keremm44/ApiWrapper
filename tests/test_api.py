"""Uçtan uca API testleri (upstream respx ile mock'lanır)."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from tests.conftest import TEST_API_KEY, UPSTREAM_DOMAIN, ai_stream

STREAM_URL = f"https://{UPSTREAM_DOMAIN}/nextjs-api/stream/post-to-evaluation/"


def stream_route(router: respx.Router):
    return router.post(url__startswith=STREAM_URL)


# ------------------------------------------------------------------ system
def test_health_is_public(client):
    response = client.get("/health", headers={"Authorization": ""})
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_metrics_endpoint(client):
    client.get("/health")
    response = client.get("/metrics")
    assert response.status_code == 200
    assert "apiwrapper_requests_total" in response.text


def test_request_id_header_echoed(client):
    response = client.get("/health", headers={"x-request-id": "my-id"})
    assert response.headers["x-request-id"] == "my-id"


# -------------------------------------------------------------------- auth
def test_missing_api_key_rejected(client):
    response = client.get("/v1/models", headers={"Authorization": ""})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "missing_api_key"


def test_wrong_api_key_rejected(client):
    response = client.get("/v1/models", headers={"Authorization": "Bearer nope"})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_api_key"


# ------------------------------------------------------------------ models
def test_list_models(client):
    response = client.get("/v1/models")
    assert response.status_code == 200
    body = response.json()
    assert body["object"] == "list"
    assert {m["id"] for m in body["data"]} == {"test-model", "second-model"}


def test_retrieve_model_by_alias(client):
    response = client.get("/v1/models/alias-model")
    assert response.status_code == 200
    assert response.json()["id"] == "test-model"


def test_unknown_model_returns_404(client):
    response = client.post(
        "/v1/chat/completions",
        json={"model": "ghost", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "model_not_found"


# ------------------------------------------------------------- completions
@respx.mock
def test_non_streaming_completion(client):
    route = stream_route(respx).mock(
        return_value=httpx.Response(200, content=ai_stream("Merhaba", " dünya"))
    )
    response = client.post(
        "/v1/chat/completions",
        json={"model": "test-model", "messages": [{"role": "user", "content": "selam"}]},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["object"] == "chat.completion"
    assert body["choices"][0]["message"]["content"] == "Merhaba dünya"
    assert body["choices"][0]["finish_reason"] == "stop"
    assert body["usage"]["prompt_tokens"] == 11
    assert body["usage"]["completion_tokens"] == 7
    assert body["model"] == "test-model"

    sent = json.loads(route.calls[0].request.content.decode())
    assert sent["modelAId"] == "upstream-model-a"
    assert sent["userMessage"]["content"] == "selam"
    assert sent["modality"] == "chat"
    request_headers = route.calls[0].request.headers
    assert request_headers["content-type"] == "text/plain;charset=UTF-8"
    assert request_headers["origin"] == f"https://{UPSTREAM_DOMAIN}"
    assert request_headers["referer"].startswith(f"https://{UPSTREAM_DOMAIN}/c/")


@respx.mock
def test_streaming_completion_sse_shape(client):
    stream_route(respx).mock(
        return_value=httpx.Response(200, content=ai_stream("Bir", " iki"))
    )
    with client.stream(
        "POST",
        "/v1/chat/completions",
        json={
            "model": "test-model",
            "stream": True,
            "messages": [{"role": "user", "content": "say"}],
        },
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        raw = "".join(response.iter_text())

    payloads = [
        line[len("data: ") :]
        for line in raw.splitlines()
        if line.startswith("data: ")
    ]
    assert payloads[-1] == "[DONE]"
    chunks = [json.loads(p) for p in payloads[:-1]]
    assert chunks[0]["choices"][0]["delta"]["role"] == "assistant"
    text = "".join(
        c["choices"][0]["delta"].get("content") or ""
        for c in chunks
        if c.get("choices")
    )
    assert text == "Bir iki"
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"


@respx.mock
def test_streaming_include_usage(client):
    stream_route(respx).mock(return_value=httpx.Response(200, content=ai_stream("x")))
    with client.stream(
        "POST",
        "/v1/chat/completions",
        json={
            "model": "test-model",
            "stream": True,
            "stream_options": {"include_usage": True},
            "messages": [{"role": "user", "content": "say"}],
        },
    ) as response:
        raw = "".join(response.iter_text())

    chunks = [
        json.loads(line[len("data: ") :])
        for line in raw.splitlines()
        if line.startswith("data: ") and "[DONE]" not in line
    ]
    usage_chunks = [c for c in chunks if c.get("usage")]
    assert usage_chunks
    assert usage_chunks[-1]["usage"]["total_tokens"] > 0


@respx.mock
def test_stop_sequence_truncates_output(client):
    stream_route(respx).mock(
        return_value=httpx.Response(200, content=ai_stream("hello ", "STOP", " tail"))
    )
    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "test-model",
            "messages": [{"role": "user", "content": "hi"}],
            "stop": ["STOP"],
        },
    )
    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == "hello "


@respx.mock
def test_upstream_error_becomes_502(client):
    stream_route(respx).mock(return_value=httpx.Response(500, content=b"boom"))
    response = client.post(
        "/v1/chat/completions",
        json={"model": "test-model", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert response.status_code == 502
    assert response.json()["error"]["type"] == "upstream_error"


@respx.mock
def test_upstream_captcha_rejection_surfaces_clear_error(client):
    stream_route(respx).mock(
        return_value=httpx.Response(403, content=b'{"error":"invalid recaptcha token"}')
    )
    response = client.post(
        "/v1/chat/completions",
        json={"model": "test-model", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "recaptcha_rejected"


@respx.mock
def test_stream_error_is_delivered_inside_sse(client):
    stream_route(respx).mock(
        return_value=httpx.Response(200, content=b'3:"upstream exploded"\n')
    )
    with client.stream(
        "POST",
        "/v1/chat/completions",
        json={
            "model": "test-model",
            "stream": True,
            "messages": [{"role": "user", "content": "hi"}],
        },
    ) as response:
        raw = "".join(response.iter_text())
    assert "upstream exploded" in raw
    assert raw.rstrip().endswith("[DONE]")


@respx.mock
def test_legacy_completions_endpoint(client):
    stream_route(respx).mock(return_value=httpx.Response(200, content=ai_stream("ok")))
    response = client.post(
        "/v1/completions", json={"model": "test-model", "prompt": "merhaba"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["object"] == "text_completion"
    assert body["choices"][0]["text"] == "ok"


# ------------------------------------------------------------- validation
def test_empty_messages_rejected(client):
    response = client.post("/v1/chat/completions", json={"model": "test-model", "messages": []})
    assert response.status_code == 400
    assert response.json()["error"]["param"] == "messages"


def test_n_greater_than_one_rejected(client):
    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "test-model",
            "messages": [{"role": "user", "content": "hi"}],
            "n": 3,
        },
    )
    assert response.status_code == 400


def test_malformed_body_returns_422(client):
    response = client.post("/v1/chat/completions", json={"messages": []})
    assert response.status_code == 422
    assert response.json()["error"]["type"] == "invalid_request_error"


# ------------------------------------------------------------------ admin
def test_admin_config_endpoint(client):
    response = client.get("/v1/admin/config")
    assert response.status_code == 200
    body = response.json()
    assert body["base_url"] == f"https://{UPSTREAM_DOMAIN}"
    assert "recaptcha_provider" in body
    # Hassas alanlar sızmamalı
    assert "cookie" not in json.dumps(body).lower() or body["cookie_configured"] is False


def test_admin_breaker_reset(client):
    assert client.post("/v1/admin/breaker/reset").json()["status"] == "closed"


def test_admin_recaptcha_invalidate(client):
    assert client.post("/v1/admin/recaptcha/invalidate").json()["status"] == "invalidated"


# -------------------------------------------------------------- rate limit
@pytest.mark.parametrize("limit", [2])
def test_rate_limit_returns_429(limit):
    from fastapi.testclient import TestClient

    from app.main import create_app
    from tests.conftest import make_settings

    app = create_app(
        make_settings(rate_limit_enabled=True, rate_limit_rpm=limit, rate_limit_burst=limit)
    )
    with TestClient(app) as local:
        local.headers.update({"Authorization": f"Bearer {TEST_API_KEY}"})
        statuses = [local.get("/v1/models").status_code for _ in range(limit + 3)]
    assert 429 in statuses
