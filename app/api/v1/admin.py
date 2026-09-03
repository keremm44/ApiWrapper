"""Yönetim uç noktaları: oturum/captcha/devre kesici kontrolü."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request

from app.api.deps import get_recaptcha, get_sessions, get_upstream
from app.core.security import api_key_dependency
from app.services.recaptcha.base import RecaptchaProvider
from app.services.session_manager import SessionManager
from app.upstream.breaker import BreakerState
from app.upstream.client import UpstreamClient

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(api_key_dependency)])


@router.post("/sessions/clear", summary="Clear cached upstream sessions")
async def clear_sessions(sessions: SessionManager = Depends(get_sessions)) -> dict[str, Any]:
    before = await sessions.size()
    await sessions.clear()
    return {"cleared": before}


@router.post("/recaptcha/invalidate", summary="Force a new reCAPTCHA token")
async def invalidate_recaptcha(
    recaptcha: RecaptchaProvider = Depends(get_recaptcha),
) -> dict[str, Any]:
    recaptcha.invalidate()
    return {"status": "invalidated", "provider": recaptcha.name}


@router.post("/breaker/reset", summary="Reset the upstream circuit breaker")
async def reset_breaker(upstream: UpstreamClient = Depends(get_upstream)) -> dict[str, Any]:
    upstream.breaker.reset()
    return {"status": "closed"}


@router.get("/config", summary="Effective non-sensitive configuration")
async def effective_config(request: Request) -> dict[str, Any]:
    settings = request.app.state.settings
    upstream: UpstreamClient = request.app.state.upstream
    return {
        "base_url": settings.base_url,
        "stream_path": settings.upstream_stream_path,
        "http2": settings.upstream_http2,
        "proxy_configured": bool(settings.upstream_proxy),
        "cookie_configured": bool(settings.upstream_cookie),
        "recaptcha_provider": settings.recaptcha_provider,
        "unsupported_params_policy": settings.unsupported_params,
        "stateless_sessions": settings.session_stateless,
        "rate_limit": {
            "enabled": settings.rate_limit_enabled,
            "rpm": settings.rate_limit_rpm,
            "burst": settings.rate_limit_burst,
        },
        "retry": {
            "max_attempts": settings.retry_max_attempts,
            "base_delay": settings.retry_base_delay,
            "max_delay": settings.retry_max_delay,
        },
        "circuit_breaker": {
            "enabled": settings.breaker_enabled,
            "state": BreakerState(upstream.breaker.state).name,
            "failure_threshold": settings.breaker_failure_threshold,
            "reset_timeout": settings.breaker_reset_timeout,
        },
        "limits": {
            "max_body_bytes": settings.max_body_bytes,
            "max_messages": settings.max_messages,
            "max_prompt_chars": settings.max_prompt_chars,
            "max_concurrent_upstream": settings.max_concurrent_upstream,
        },
    }
