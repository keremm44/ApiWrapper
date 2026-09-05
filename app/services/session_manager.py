"""Sohbet oturumu yönetimi: chat_id üretimi, yeniden kullanım ve ısıtma."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from app.core.config import Settings
from app.core.logging import get_logger
from app.core.metrics import metrics
from app.utils.cache import LRUTTLCache
from app.utils.ids import new_chat_id

logger = get_logger(__name__)


@dataclass(slots=True)
class Session:
    """Tek bir upstream sohbet oturumu."""

    chat_id: str
    created_at: float = field(default_factory=time.time)
    warmed: bool = False
    message_count: int = 0

    def touch(self) -> None:
        self.message_count += 1

    def age(self, now: float | None = None) -> float:
        return (time.time() if now is None else now) - self.created_at


class SessionManager:
    """İstemci → upstream `chat_id` eşlemesini yönetir.

    Üç mod vardır:

    * **Durumsuz** (varsayılan): her istek yeni bir `chat_id` alır.
    * **`conversation_id` ile**: istemci kendi sohbet kimliğini gönderir.
    * **`session_reuse` ile**: OpenAI-uyumlu istemciler şema dışı `conversation_id`
      gönderemediği için anahtar, API anahtarının parmak izi olur. Aynı istemci tek
      upstream sohbetinden ilerler; eşik aşılınca (`session_rotate_after_messages` /
      `session_rotate_after_seconds`) yeni sohbete geçilir.
    """

    def __init__(self, settings: Settings, warmup=None) -> None:
        self.settings = settings
        self._warmup = warmup
        self._rotations = 0
        self._cache: LRUTTLCache[str, Session] = LRUTTLCache(
            maxsize=settings.session_cache_size, ttl=settings.session_ttl
        )

    # ------------------------------------------------------------ rotation
    def _rotation_reason(self, session: Session) -> str | None:
        """Sohbet değiştirilmeli mi? Gerekçeyi döndürür, gerek yoksa `None`."""
        limit = self.settings.session_rotate_after_messages
        if limit > 0 and session.message_count >= limit:
            return f"message_count={session.message_count} >= {limit}"
        max_age = self.settings.session_rotate_after_seconds
        if max_age > 0 and session.age() >= max_age:
            return f"age={session.age():.0f}s >= {max_age:.0f}s"
        return None

    @property
    def rotations(self) -> int:
        """Süreç boyunca yapılan sohbet rotasyonu sayısı."""
        return self._rotations

    async def acquire(
        self,
        conversation_id: str | None = None,
        client_identity: str | None = None,
    ) -> Session:
        """İstek için bir oturum döndürür (gerekirse oluşturur/yeniler)."""
        key = self._session_key(conversation_id, client_identity)
        if key is None:
            # Durumsuz yol: eskisi gibi her istekte yeni sohbet.
            session = Session(chat_id=new_chat_id())
            await self._maybe_warm(session)
            session.touch()
            return session

        cached = await self._cache.get(key)
        if cached is not None:
            reason = self._rotation_reason(cached)
            if reason is None:
                cached.touch()
                logger.debug(
                    "session_reused", key=key, chat_id=cached.chat_id,
                    message_count=cached.message_count,
                )
                return cached
            logger.info(
                "session_rotated", key=key, old_chat_id=cached.chat_id,
                message_count=cached.message_count, reason=reason,
            )
            self._rotations += 1
            metrics.inc("apiwrapper_sessions_rotated_total")
            await self._cache.delete(key)

        session = Session(chat_id=new_chat_id())
        await self._maybe_warm(session)
        session.touch()
        await self._cache.set(key, session)
        logger.debug(
            "session_created", key=key, chat_id=session.chat_id,
            conversation_id=conversation_id,
        )
        return session

    def _session_key(
        self, conversation_id: str | None, client_identity: str | None
    ) -> str | None:
        """Önbellek anahtarını seçer; `None` durumsuz modu ifade eder.

        Öncelik sırası önemlidir: `session_reuse` açıkken `session_stateless` yok
        sayılır; aksi halde `session_stateless=True` her şeyi durumsuza zorlar
        (istemci `conversation_id` gönderse bile).
        """
        if self.settings.session_reuse:
            if conversation_id:
                return f"conv:{conversation_id}"
            if client_identity:
                return f"client:{client_identity}"
            return None
        if self.settings.session_stateless:
            return None
        if conversation_id:
            return f"conv:{conversation_id}"
        if client_identity:
            # conversation_id bekleyen modda istemci kimliği yedek anahtar olur.
            return f"client:{client_identity}"
        return None

    async def invalidate(self, conversation_id: str, client_identity: str | None = None) -> None:
        """Verilen anahtarlara bağlı oturumları düşürür (sonraki istek yeni sohbet alır)."""
        await self._cache.delete(conversation_id)
        await self._cache.delete(f"conv:{conversation_id}")
        if client_identity:
            await self._cache.delete(f"client:{client_identity}")

    async def _maybe_warm(self, session: Session) -> None:
        if session.warmed or self._warmup is None:
            return
        await self._warmup(session.chat_id)
        session.warmed = True

    async def size(self) -> int:
        return await self._cache.size()

    async def clear(self) -> None:
        await self._cache.clear()
