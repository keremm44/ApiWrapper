"""API anahtarı doğrulama — sabit zamanlı karşılaştırma."""

from __future__ import annotations

import hmac

from fastapi import Request

from app.core.config import Settings, get_settings
from app.core.errors import AuthenticationError
from app.core.logging import mask_value

ANONYMOUS = "anonymous"


def extract_api_key(request: Request) -> str | None:
    """Authorization: Bearer <key> veya x-api-key başlığından anahtarı çıkarır."""
    auth = request.headers.get("authorization")
    if auth:
        scheme, _, value = auth.partition(" ")
        if scheme.lower() == "bearer" and value.strip():
            return value.strip()
        if not value and auth.strip():
            # "Authorization: sk-..." biçimi (bazı istemciler)
            return auth.strip()
    header_key = request.headers.get("x-api-key")
    if header_key and header_key.strip():
        return header_key.strip()
    return None


def verify_api_key(request: Request, settings: Settings | None = None) -> str:
    """Anahtarı doğrular ve maskelenmiş kimliğini döndürür.

    Doğrulama kapalıysa `ANONYMOUS` döner.
    """
    if settings is None:
        settings = getattr(request.app.state, "settings", None) or get_settings()
    if settings.auth_disabled:
        return ANONYMOUS

    key = extract_api_key(request)
    if not key:
        raise AuthenticationError(
            "Missing API key. Provide it via 'Authorization: Bearer <key>' header.",
            code="missing_api_key",
        )

    for allowed in settings.api_keys:
        if hmac.compare_digest(key, allowed):
            return mask_value(key)

    raise AuthenticationError("Incorrect API key provided.")


async def api_key_dependency(request: Request) -> str:
    """FastAPI bağımlılığı; doğrulanan anahtar kimliğini state'e de yazar."""
    identity = verify_api_key(request)
    request.state.api_identity = identity
    return identity
