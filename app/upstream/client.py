"""Upstream HTTP istemcisi: havuzlu httpx.AsyncClient, retry, devre kesici, streaming."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx

from app.core.config import Settings
from app.core.logging import get_logger
from app.core.metrics import metrics
from app.upstream.breaker import CircuitBreaker
from app.upstream.exceptions import (
    CircuitOpen,
    RecaptchaRejected,
    UpstreamAuthRejected,
    UpstreamHTTPError,
    UpstreamNetworkError,
    UpstreamTimeout,
)
from app.upstream.headers import build_page_headers, build_stream_headers
from app.utils.backoff import full_jitter_delay, parse_retry_after

logger = get_logger(__name__)

#: Yeniden denenebilir HTTP durum kodları.
RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504, 522, 524})
#: Kimlik/captcha reddi olabilecek durum kodları.
CAPTCHA_STATUS = frozenset({401, 403})
#: Gövdede captcha izi yokken oturum sorununa işaret eden kodlar.
AUTH_STATUS = frozenset({401})
#: Gövdede oturum/token sorununa işaret eden anahtar kelimeler.
AUTH_MARKERS = (
    "unauthorized",
    "unauthenticated",
    "invalid token",
    "invalid_token",
    "expired",
    "jwt",
    "access token",
    "access_token",
    "not signed in",
    "login",
    "session",
)


class UpstreamClient:
    """Hedef servisle tüm HTTP iletişimini yöneten tekil istemci."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.breaker = CircuitBreaker(
            failure_threshold=settings.breaker_failure_threshold,
            reset_timeout=settings.breaker_reset_timeout,
            enabled=settings.breaker_enabled,
        )
        self._semaphore = asyncio.Semaphore(max(1, settings.max_concurrent_upstream))
        self._client: httpx.AsyncClient | None = None

    # ----------------------------------------------------------- lifecycle
    async def startup(self) -> None:
        if self._client is not None:
            return
        timeout = httpx.Timeout(
            connect=self.settings.connect_timeout,
            read=self.settings.read_timeout,
            write=self.settings.write_timeout,
            pool=self.settings.pool_timeout,
        )
        limits = httpx.Limits(
            max_connections=self.settings.max_connections,
            max_keepalive_connections=self.settings.max_keepalive_connections,
            keepalive_expiry=30.0,
        )
        kwargs: dict[str, object] = {
            "timeout": timeout,
            "limits": limits,
            # Stream POST'unda yönlendirme takip edilmez (aşağıda send() override).
            # 301/302 POST→GET'e dönüşüp gövdeyi düşürür ve sahte 404 üretir.
            "follow_redirects": True,
            "verify": self.settings.upstream_verify_tls,
            "trust_env": False,
        }
        if self.settings.upstream_proxy:
            kwargs["proxy"] = self.settings.upstream_proxy
        try:
            self._client = httpx.AsyncClient(http2=self.settings.upstream_http2, **kwargs)
        except ImportError:  # h2 kurulu değilse HTTP/1.1'e düş
            logger.warning("http2_unavailable_fallback_http11")
            self._client = httpx.AsyncClient(http2=False, **kwargs)
        logger.info("upstream_client_started", base_url=self.settings.base_url)

    async def shutdown(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
            logger.info("upstream_client_closed")

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("UpstreamClient is not started. Call startup() first.")
        return self._client

    # ------------------------------------------------------------- helpers
    async def warmup(self, chat_id: str) -> None:
        """Sohbet sayfasını GET ederek çerezleri toplar. Hatalar yutulur."""
        url = self.settings.referer_url(chat_id)
        try:
            response = await self.client.get(
                url,
                headers=build_page_headers(self.settings),
                timeout=httpx.Timeout(self.settings.connect_timeout + 10.0),
            )
            logger.debug("upstream_warmup", status=response.status_code, chat_id=chat_id)
        except Exception as exc:  # pragma: no cover - ısıtma kritik değil
            logger.debug("upstream_warmup_failed", error=str(exc))

    @staticmethod
    def _is_captcha_body(status: int, body: str) -> bool:
        """403/401 yanıtının reCAPTCHA reddi olup olmadığını ayırt eder."""
        if status not in CAPTCHA_STATUS:
            return False
        lowered = body.lower()
        return any(
            marker in lowered
            for marker in ("recaptcha", "captcha", "verification", "bot", "forbidden")
        )

    @staticmethod
    def _is_auth_body(status: int, body: str) -> bool:
        """401 yanıtının oturum/`access_token` reddi olup olmadığını ayırt eder.

        Gövde boş bir 401 de kimlik sorunu sayılır: hedef servis `Authorization`
        başlığını zorunlu kıldığı için en olası neden token'ın eksik/süresi dolmuş
        olmasıdır.
        """
        if status not in AUTH_STATUS:
            return False
        lowered = body.lower()
        if not lowered.strip():
            return True
        return any(marker in lowered for marker in AUTH_MARKERS)

    # ------------------------------------------------------------- request
    @asynccontextmanager
    async def stream_completion(
        self, chat_id: str, payload: dict[str, object]
    ) -> AsyncIterator[AsyncIterator[bytes]]:
        """Upstream'e istek atıp bayt akışı döndürür (retry + breaker dahil).

        `--data-raw` ile birebir aynı gövde `text/plain` olarak gönderilir.
        """
        if not await self.breaker.allows():
            metrics.inc("apiwrapper_upstream_errors_total", labels={"reason": "circuit_open"})
            raise CircuitOpen(
                "Upstream circuit breaker is open.",
                status_code=503,
            )

        url = self.settings.stream_url(chat_id)
        headers = build_stream_headers(self.settings, chat_id)
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

        last_error: Exception | None = None
        attempts = max(1, self.settings.retry_max_attempts)

        async with self._semaphore:
            for attempt in range(attempts):
                metrics.inc("apiwrapper_upstream_requests_total")
                request = self.client.build_request(
                    "POST", url, headers=headers, content=body
                )
                response: httpx.Response | None = None
                try:
                    # POST gövdesini 301/302 ile GET'e çevirmemek için yönlendirme kapalı.
                    response = await self.client.send(
                        request, stream=True, follow_redirects=False
                    )
                except httpx.TimeoutException as exc:
                    last_error = UpstreamTimeout(f"Upstream timed out: {exc}", url=url)
                except httpx.HTTPError as exc:
                    last_error = UpstreamNetworkError(
                        f"Upstream network error: {exc}", url=url
                    )
                else:
                    status = response.status_code
                    if 300 <= status < 400:
                        location = response.headers.get("location", "")
                        error_body = await self._read_error_body(response)
                        await response.aclose()
                        metrics.inc(
                            "apiwrapper_upstream_errors_total",
                            labels={"status": str(status)},
                        )
                        logger.warning(
                            "upstream_redirect",
                            status=status,
                            url=url,
                            location=location or None,
                        )
                        await self.breaker.record_failure()
                        raise UpstreamHTTPError(
                            f"Upstream redirected HTTP {status} from {url} "
                            f"to {location or '(no Location header)'}.",
                            status_code=status,
                            body=error_body,
                            url=url,
                        )

                    if status < 400:
                        await self.breaker.record_success()
                        try:
                            yield response.aiter_bytes()
                        finally:
                            await response.aclose()
                        return

                    error_body = await self._read_error_body(response)
                    await response.aclose()
                    metrics.inc(
                        "apiwrapper_upstream_errors_total", labels={"status": str(status)}
                    )
                    logger.warning(
                        "upstream_http_error",
                        status=status,
                        url=url,
                        body=error_body[:300],
                    )

                    if self._is_auth_body(status, error_body) and not self._is_captcha_body(
                        status, error_body
                    ):
                        await self.breaker.record_failure()
                        raise UpstreamAuthRejected(
                            "Upstream rejected the session credentials.",
                            status_code=status,
                            body=error_body,
                            url=url,
                        )

                    if self._is_captcha_body(status, error_body):
                        await self.breaker.record_failure()
                        raise RecaptchaRejected(
                            "Upstream rejected the reCAPTCHA token.",
                            status_code=status,
                            body=error_body,
                            url=url,
                        )

                    last_error = UpstreamHTTPError(
                        f"Upstream returned HTTP {status} for {url}.",
                        status_code=status,
                        body=error_body,
                        url=url,
                    )
                    if status not in RETRYABLE_STATUS:
                        await self.breaker.record_failure()
                        raise last_error

                    retry_after = parse_retry_after(response.headers.get("retry-after"))
                    if attempt < attempts - 1:
                        await asyncio.sleep(
                            retry_after
                            if retry_after is not None
                            else full_jitter_delay(
                                attempt,
                                self.settings.retry_base_delay,
                                self.settings.retry_max_delay,
                            )
                        )
                        continue

                # Ağ/timeout hatası yolu
                if attempt < attempts - 1:
                    logger.warning(
                        "upstream_retry",
                        attempt=attempt + 1,
                        max_attempts=attempts,
                        error=str(last_error),
                    )
                    await asyncio.sleep(
                        full_jitter_delay(
                            attempt,
                            self.settings.retry_base_delay,
                            self.settings.retry_max_delay,
                        )
                    )

            await self.breaker.record_failure()
            raise last_error or UpstreamNetworkError("Upstream request failed.")

    @staticmethod
    async def _read_error_body(response: httpx.Response, limit: int = 4096) -> str:
        """Hata gövdesini sınırlı biçimde okur."""
        try:
            collected = bytearray()
            async for chunk in response.aiter_bytes():
                collected.extend(chunk)
                if len(collected) >= limit:
                    break
            return bytes(collected[:limit]).decode("utf-8", errors="replace")
        except Exception:  # pragma: no cover
            return ""
