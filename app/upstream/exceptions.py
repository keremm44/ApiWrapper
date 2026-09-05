"""Upstream katmanına özgü istisnalar."""

from __future__ import annotations


class UpstreamException(Exception):
    """Upstream iletişim hatalarının tabanı."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        body: str = "",
        url: str = "",
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.body = body[:2000]
        self.url = url


class UpstreamHTTPError(UpstreamException):
    """Upstream 4xx/5xx döndürdü."""


class UpstreamNetworkError(UpstreamException):
    """Bağlantı/DNS/TLS düzeyinde hata."""


class UpstreamTimeout(UpstreamException):
    """Zaman aşımı."""


class UpstreamProtocolError(UpstreamException):
    """Akış protokolü bozuk veya beklenmedik biçimde sonlandı."""


class UpstreamQuotaExceeded(UpstreamException):
    """Upstream hesabı kısıtladı/kilitledi (kota penceresi dolu).

    Yeniden denemek kilit süresini uzatacağı için bu istisna **retry edilmez**;
    hesabın penceresinin dolması beklenmelidir. `retry_after` biliniyorsa
    istemciye `Retry-After` başlığı olarak taşınır.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        body: str = "",
        url: str = "",
        retry_after: float | None = None,
        marker: str | None = None,
    ) -> None:
        super().__init__(message, status_code=status_code, body=body, url=url)
        self.retry_after = retry_after
        self.marker = marker


class RecaptchaRejected(UpstreamException):
    """Upstream reCAPTCHA token'ını reddetti (yenilenip tekrar denenmeli)."""


class UpstreamAuthRejected(UpstreamException):
    """Upstream oturumu/`access_token`'ı reddetti (401). Yenilemek kullanıcıya düşer."""


class CircuitOpen(UpstreamException):
    """Devre kesici açık; istek gönderilmedi."""
