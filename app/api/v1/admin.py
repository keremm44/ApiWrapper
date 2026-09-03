"""Yönetim uç noktaları: oturum/captcha/devre kesici kontrolü."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request

from app.api.deps import get_recaptcha, get_sessions, get_upstream
from app.core.logging import mask_value
from app.core.security import api_key_dependency
from app.services.recaptcha.base import RecaptchaProvider
from app.services.session_manager import SessionManager
from app.upstream.auth import (
    extract_access_token,
    looks_like_jwt,
    parse_cookie_header,
    token_seconds_remaining,
)
from app.upstream.breaker import BreakerState
from app.upstream.client import UpstreamClient
from app.upstream.headers import build_authorization

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


@router.get("/auth", summary="Diagnose upstream credential extraction")
async def auth_diagnostics(request: Request) -> dict[str, Any]:
    """Cookie'den `access_token` ayıklamasının sonucunu **token'ı ifşa etmeden** gösterir.

    401 sorunlarını teşhis etmek için: token bulundu mu, hangi çerezler mevcut,
    JWT mi, ne zaman doluyor?
    """
    settings = request.app.state.settings
    cookies = parse_cookie_header(settings.upstream_cookie)
    token = extract_access_token(
        settings.upstream_cookie, cookie_names=settings.upstream_token_cookie_names
    ) if settings.upstream_auth_from_cookie else None
    effective = settings.upstream_access_token.strip() or token
    header_value = build_authorization(settings)

    remaining = token_seconds_remaining(effective) if effective else None
    return {
        "authorization_header_will_be_sent": bool(header_value),
        "auth_scheme": settings.upstream_auth_scheme,
        "source": (
            "UPSTREAM_ACCESS_TOKEN"
            if settings.upstream_access_token.strip()
            else ("cookie" if token else None)
        ),
        "extract_from_cookie_enabled": settings.upstream_auth_from_cookie,
        "configured_cookie_names": settings.upstream_token_cookie_names,
        "cookie_names_present": sorted(cookies),
        "cookie_count": len(cookies),
        "token": {
            "found": bool(effective),
            "masked": mask_value(effective) if effective else None,
            "length": len(effective) if effective else 0,
            "is_jwt": looks_like_jwt(effective) if effective else False,
            "expires_in_seconds": int(remaining) if remaining is not None else None,
            "expired": (remaining is not None and remaining <= 0),
        },
    }
