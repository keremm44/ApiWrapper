"""Upstream katmanına özgü istisnalar."""

from __future__ import annotations


class UpstreamException(Exception):
    """Upstream iletişim hatalarının tabanı."""

    def __init__(self, message: str, *, status_code: int | None = None, body: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.body = body[:2000]


class UpstreamHTTPError(UpstreamException):
    """Upstream 4xx/5xx döndürdü."""


class UpstreamNetworkError(UpstreamException):
    """Bağlantı/DNS/TLS düzeyinde hata."""


class UpstreamTimeout(UpstreamException):
    """Zaman aşımı."""


class UpstreamProtocolError(UpstreamException):
    """Akış protokolü bozuk veya beklenmedik biçimde sonlandı."""


class RecaptchaRejected(UpstreamException):
    """Upstream reCAPTCHA token'ını reddetti (yenilenip tekrar denenmeli)."""


class UpstreamAuthRejected(UpstreamException):
    """Upstream oturumu/`access_token`'ı reddetti (401). Yenilemek kullanıcıya düşer."""


class CircuitOpen(UpstreamException):
    """Devre kesici açık; istek gönderilmedi."""
