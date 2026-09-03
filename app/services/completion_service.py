"""Sohbet tamamlama orkestrasyonu: stream ve non-stream akışlar."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator

from app.adapters.openai_to_upstream import build_upstream_request
from app.adapters.upstream_to_openai import (
    build_usage,
    make_completion,
    make_content_chunk,
    make_finish_chunk,
    make_role_chunk,
    make_usage_chunk,
)
from app.core.config import Settings
from app.core.errors import (
    APIWrapperError,
    RecaptchaError,
    UpstreamError,
    UpstreamTimeoutError,
    UpstreamUnavailableError,
)
from app.core.logging import get_logger
from app.core.metrics import metrics
from app.schemas.openai import ChatCompletionRequest, ChatCompletionResponse
from app.schemas.upstream import EventType, StreamEvent
from app.services.model_registry import ModelRegistry
from app.services.recaptcha.base import RecaptchaProvider
from app.services.session_manager import SessionManager
from app.upstream.client import UpstreamClient
from app.upstream.exceptions import (
    CircuitOpen,
    RecaptchaRejected,
    UpstreamAuthRejected,
    UpstreamHTTPError,
    UpstreamNetworkError,
    UpstreamProtocolError,
    UpstreamTimeout,
)
from app.upstream.stream_parser import parse_stream
from app.utils.ids import new_completion_id, now_ts
from app.utils.sse import sse_done, sse_event
from app.utils.tokens import count_tokens, tiktoken_available

logger = get_logger(__name__)


def _dump(chunk) -> dict:
    """Chunk'ı OpenAI'ye yakın biçimde serialize eder (boş alanlar atlanır).

    `delta.content` bir istisnadır: içerik parçası her zaman görünmelidir.
    """
    data = chunk.model_dump(exclude_none=True)
    for choice in data.get("choices", []):
        delta = choice.get("delta")
        if isinstance(delta, dict) and "content" not in delta and choice.get(
            "finish_reason"
        ) is None:
            delta["content"] = ""
    return data


class StopSequenceTracker:
    """Yerel stop-sequence kesme mantığı (upstream desteklemediği için)."""

    def __init__(self, stops: list[str]) -> None:
        self.stops = [s for s in stops if s]
        self.max_len = max((len(s) for s in self.stops), default=0)
        self._pending = ""
        self.triggered = False

    def process(self, chunk: str) -> str:
        """Yayınlanabilir metni döndürür; stop bulunursa `triggered` işaretlenir."""
        if not self.stops or self.triggered:
            return "" if self.triggered else chunk

        buffer = self._pending + chunk
        earliest = -1
        for stop in self.stops:
            idx = buffer.find(stop)
            if idx != -1 and (earliest == -1 or idx < earliest):
                earliest = idx
        if earliest != -1:
            self.triggered = True
            self._pending = ""
            return buffer[:earliest]

        # Kısmi eşleşme olabilecek kuyruğu beklet.
        hold = max(0, self.max_len - 1)
        if hold and len(buffer) > hold:
            emit, self._pending = buffer[:-hold], buffer[-hold:]
            return emit
        if hold:
            self._pending = buffer
            return ""
        self._pending = ""
        return buffer

    def flush(self) -> str:
        if self.triggered:
            return ""
        pending, self._pending = self._pending, ""
        return pending


class CompletionService:
    """OpenAI isteğini upstream'e taşıyan ana servis."""

    def __init__(
        self,
        settings: Settings,
        upstream: UpstreamClient,
        registry: ModelRegistry,
        sessions: SessionManager,
        recaptcha: RecaptchaProvider,
    ) -> None:
        self.settings = settings
        self.upstream = upstream
        self.registry = registry
        self.sessions = sessions
        self.recaptcha = recaptcha

    # ------------------------------------------------------------ helpers
    @property
    def system_fingerprint(self) -> str:
        suffix = "tiktoken" if tiktoken_available() else "estimate"
        return f"fp_{self.settings.app_version.replace('.', '')}_{suffix}"

    @staticmethod
    def _translate(exc: Exception) -> APIWrapperError:
        """Upstream istisnalarını API hatalarına çevirir."""
        if isinstance(exc, APIWrapperError):
            return exc
        if isinstance(exc, CircuitOpen):
            return UpstreamUnavailableError(
                "Upstream service is temporarily unavailable (circuit breaker open). "
                "Please retry shortly."
            )
        if isinstance(exc, UpstreamAuthRejected):
            return APIWrapperError(
                "The upstream service rejected the session credentials (401). "
                "Refresh UPSTREAM_COOKIE (and/or UPSTREAM_ACCESS_TOKEN) in the "
                "environment; the extracted access_token is missing or expired.",
                status_code=502,
                err_type="upstream_error",
                code="upstream_unauthorized",
            )
        if isinstance(exc, RecaptchaRejected):
            return RecaptchaError(
                "The upstream service rejected the reCAPTCHA token. "
                "Refresh RECAPTCHA_STATIC_TOKEN or use the 'browser'/'external' provider.",
                code="recaptcha_rejected",
            )
        if isinstance(exc, UpstreamTimeout):
            return UpstreamTimeoutError(str(exc))
        if isinstance(exc, UpstreamHTTPError):
            status = exc.status_code or 502
            if status == 429:
                return APIWrapperError(
                    "Upstream rate limit reached. Please retry later.",
                    status_code=429,
                    err_type="rate_limit_error",
                    code="upstream_rate_limited",
                )
            return UpstreamError(
                f"Upstream request failed with HTTP {status}.",
                status_code=502 if status >= 500 else 502,
            )
        if isinstance(exc, (UpstreamNetworkError, UpstreamProtocolError)):
            return UpstreamError(str(exc))
        return UpstreamError(f"Unexpected upstream failure: {exc}")

    async def _prepare(self, request: ChatCompletionRequest):
        """Model çözümü, oturum, captcha token'ı ve gövde inşası."""
        entry = self.registry.resolve(request.model)
        session = await self.sessions.acquire(request.conversation_id)
        token = await self.recaptcha.get_token()
        built = build_upstream_request(
            request,
            settings=self.settings,
            upstream_model_id=entry.upstream_id,
            chat_id=session.chat_id,
            recaptcha_token=token,
        )
        return entry, built

    async def _events(
        self, request: ChatCompletionRequest, allow_captcha_retry: bool = True
    ) -> AsyncIterator[StreamEvent]:
        """Upstream olay akışı; captcha reddinde bir kez yeniler ve tekrar dener."""
        entry, built = await self._prepare(request)
        logger.info(
            "upstream_request",
            model=entry.id,
            upstream_model=entry.upstream_id,
            chat_id=built.chat_id,
            prompt_chars=len(built.prompt),
        )
        try:
            async with self.upstream.stream_completion(
                built.chat_id, built.payload.to_wire(self.settings.upstream_recaptcha_field)
            ) as chunks:
                idle = self.settings.stream_idle_timeout
                iterator = parse_stream(chunks).__aiter__()
                while True:
                    try:
                        event = await asyncio.wait_for(iterator.__anext__(), timeout=idle)
                    except StopAsyncIteration:
                        break
                    except TimeoutError as exc:
                        raise UpstreamTimeout(
                            f"No data received from upstream for {idle}s."
                        ) from exc
                    yield event
        except RecaptchaRejected:
            self.recaptcha.invalidate()
            if not allow_captcha_retry:
                raise
            logger.warning("recaptcha_rejected_retrying")
            async for event in self._events(request, allow_captcha_retry=False):
                yield event

    # --------------------------------------------------------- non-stream
    async def create_completion(
        self, request: ChatCompletionRequest
    ) -> ChatCompletionResponse:
        """Tam yanıtı biriktirip tek seferde döndürür."""
        started = time.monotonic()
        completion_id = new_completion_id()
        entry = self.registry.resolve(request.model)
        stopper = StopSequenceTracker(request.stop_sequences())

        parts: list[str] = []
        finish_reason = "stop"
        reported_usage: dict[str, int] = {}
        first_token_at: float | None = None

        metrics.inc("apiwrapper_requests_total", labels={"endpoint": "completions",
                                                          "stream": "false"})
        try:
            async for event in self._events(request):
                if event.type is EventType.ERROR:
                    raise UpstreamError(f"Upstream error: {event.text}")
                if event.type in (EventType.TEXT,) and event.text:
                    if first_token_at is None:
                        first_token_at = time.monotonic()
                        metrics.observe(
                            "apiwrapper_time_to_first_token_seconds", first_token_at - started
                        )
                    emitted = stopper.process(event.text)
                    if emitted:
                        parts.append(emitted)
                    if stopper.triggered:
                        finish_reason = "stop"
                        break
                elif event.type in (EventType.FINISH, EventType.STEP_FINISH):
                    if event.finish_reason:
                        finish_reason = event.finish_reason
                    if event.usage:
                        reported_usage.update(event.usage)
        except Exception as exc:
            raise self._translate(exc) from exc

        tail = stopper.flush()
        if tail:
            parts.append(tail)
        content = "".join(parts)

        prompt_tokens = count_tokens(self._prompt_preview(request))
        completion_tokens = count_tokens(content)
        usage = build_usage(prompt_tokens, completion_tokens, reported_usage or None)

        metrics.observe(
            "apiwrapper_request_duration_seconds",
            time.monotonic() - started,
            {"endpoint": "completions"},
        )
        metrics.inc("apiwrapper_tokens_total", usage.total_tokens, {"model": entry.id})

        return make_completion(
            completion_id=completion_id,
            model=entry.id,
            content=content,
            usage=usage,
            finish_reason=finish_reason,
            created=now_ts(),
            system_fingerprint=self.system_fingerprint,
        )

    # ------------------------------------------------------------- stream
    async def stream_completion(
        self, request: ChatCompletionRequest, is_disconnected=None
    ) -> AsyncIterator[str]:
        """SSE gövdesini parça parça üretir. `[DONE]` her koşulda gönderilir."""
        started = time.monotonic()
        completion_id = new_completion_id()
        entry = self.registry.resolve(request.model)
        created = now_ts()
        stopper = StopSequenceTracker(request.stop_sequences())

        finish_reason = "stop"
        reported_usage: dict[str, int] = {}
        completion_text: list[str] = []
        first_token_at: float | None = None
        finished_cleanly = False

        metrics.inc("apiwrapper_requests_total", labels={"endpoint": "completions",
                                                          "stream": "true"})
        metrics.inc("apiwrapper_active_streams", 1.0)

        try:
            yield sse_event(_dump(make_role_chunk(completion_id, entry.id, created)))

            try:
                async for event in self._events(request):
                    if is_disconnected is not None and await is_disconnected():
                        logger.info("client_disconnected", completion_id=completion_id)
                        return

                    if event.type is EventType.ERROR:
                        raise UpstreamError(f"Upstream error: {event.text}")

                    if event.type is EventType.TEXT and event.text:
                        if first_token_at is None:
                            first_token_at = time.monotonic()
                            metrics.observe(
                                "apiwrapper_time_to_first_token_seconds",
                                first_token_at - started,
                            )
                        emitted = stopper.process(event.text)
                        if emitted:
                            completion_text.append(emitted)
                            yield sse_event(
                                _dump(
                                    make_content_chunk(
                                        completion_id, entry.id, created, emitted
                                    )
                                )
                            )
                        if stopper.triggered:
                            finish_reason = "stop"
                            break
                    elif event.type in (EventType.FINISH, EventType.STEP_FINISH):
                        if event.finish_reason:
                            finish_reason = event.finish_reason
                        if event.usage:
                            reported_usage.update(event.usage)

                tail = stopper.flush()
                if tail:
                    completion_text.append(tail)
                    yield sse_event(
                        _dump(make_content_chunk(completion_id, entry.id, created, tail))
                    )
                finished_cleanly = True

            except Exception as exc:
                api_error = self._translate(exc)
                logger.warning(
                    "stream_failed", error=api_error.message, status=api_error.status_code
                )
                yield sse_event(api_error.to_payload())
                return

            yield sse_event(
                _dump(make_finish_chunk(completion_id, entry.id, created, finish_reason))
            )

            if request.wants_usage():
                content = "".join(completion_text)
                usage = build_usage(
                    count_tokens(self._prompt_preview(request)),
                    count_tokens(content),
                    reported_usage or None,
                )
                metrics.inc("apiwrapper_tokens_total", usage.total_tokens, {"model": entry.id})
                yield sse_event(
                    _dump(make_usage_chunk(completion_id, entry.id, created, usage))
                )

        finally:
            metrics.inc("apiwrapper_active_streams", -1.0)
            metrics.observe(
                "apiwrapper_request_duration_seconds",
                time.monotonic() - started,
                {"endpoint": "completions"},
            )
            # `[DONE]` her durumda gönderilir; istemciler buna güvenir.
            del finished_cleanly
            yield sse_done()

    # ------------------------------------------------------------ private
    def _prompt_preview(self, request: ChatCompletionRequest) -> str:
        """Token sayımı için prompt metnini yeniden üretir (ucuz yaklaşım)."""
        return "\n".join(m.text_content() for m in request.messages)
