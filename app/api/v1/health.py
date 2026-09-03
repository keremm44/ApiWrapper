"""Sağlık kontrolü ve metrik uç noktaları (kimlik doğrulama gerektirmez)."""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import PlainTextResponse

from app.api.deps import get_registry, get_settings_dep
from app.core.config import Settings
from app.core.metrics import metrics
from app.services.model_registry import ModelRegistry
from app.upstream.breaker import BreakerState

router = APIRouter(tags=["system"])

_STARTED_AT = time.time()


@router.get("/health", summary="Liveness & readiness probe")
async def health(
    request: Request,
    settings: Settings = Depends(get_settings_dep),
    registry: ModelRegistry = Depends(get_registry),
) -> dict[str, Any]:
    upstream = request.app.state.upstream
    breaker_state = BreakerState(upstream.breaker.state).name
    sessions = await request.app.state.sessions.size()
    return {
        "status": "ok" if breaker_state != "OPEN" else "degraded",
        "version": settings.app_version,
        "uptime_seconds": round(time.time() - _STARTED_AT, 2),
        "upstream": {
            "base_url": settings.base_url,
            "circuit_breaker": breaker_state,
            "http2": settings.upstream_http2,
        },
        "models": len(registry),
        "sessions_cached": sessions,
        "recaptcha_provider": settings.recaptcha_provider,
    }


@router.get("/healthz", include_in_schema=False)
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/metrics", response_class=PlainTextResponse, summary="Prometheus metrics")
async def prometheus_metrics(
    settings: Settings = Depends(get_settings_dep),
) -> PlainTextResponse:
    if not settings.metrics_enabled:
        return PlainTextResponse("# metrics disabled\n", status_code=404)
    return PlainTextResponse(metrics.render(), media_type="text/plain; version=0.0.4")


@router.get("/", include_in_schema=False)
async def index(settings: Settings = Depends(get_settings_dep)) -> dict[str, Any]:
    return {
        "name": settings.app_name,
        "version": settings.app_version,
        "docs": "/docs",
        "endpoints": [
            "/v1/chat/completions",
            "/v1/completions",
            "/v1/models",
            "/health",
            "/metrics",
        ],
    }
