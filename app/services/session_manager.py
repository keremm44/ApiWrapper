"""Sohbet oturumu yönetimi: chat_id üretimi, yeniden kullanım ve ısıtma."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from app.core.config import Settings
from app.core.logging import get_logger
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


class SessionManager:
    """`conversation_id` → upstream `chat_id` eşlemesini yönetir.

    `session_stateless=True` iken her istek için yeni bir chat_id üretilir
    (upstream tarafında durum tutulmaz, prompt tam geçmişi içerir).
    """

    def __init__(self, settings: Settings, warmup=None) -> None:
        self.settings = settings
        self._warmup = warmup
        self._cache: LRUTTLCache[str, Session] = LRUTTLCache(
            maxsize=settings.session_cache_size, ttl=settings.session_ttl
        )

    async def acquire(self, conversation_id: str | None = None) -> Session:
        """İstek için bir oturum döndürür (gerekirse oluşturur)."""
        if self.settings.session_stateless or not conversation_id:
            session = Session(chat_id=new_chat_id())
            await self._maybe_warm(session)
            session.touch()
            return session

        cached = await self._cache.get(conversation_id)
        if cached is not None:
            cached.touch()
            logger.debug(
                "session_reused", conversation_id=conversation_id, chat_id=cached.chat_id
            )
            return cached

        session = Session(chat_id=new_chat_id())
        await self._maybe_warm(session)
        session.touch()
        await self._cache.set(conversation_id, session)
        logger.debug("session_created", conversation_id=conversation_id, chat_id=session.chat_id)
        return session

    async def _maybe_warm(self, session: Session) -> None:
        if session.warmed or self._warmup is None:
            return
        await self._warmup(session.chat_id)
        session.warmed = True

    async def invalidate(self, conversation_id: str) -> None:
        await self._cache.delete(conversation_id)

    async def size(self) -> int:
        return await self._cache.size()

    async def clear(self) -> None:
        await self._cache.clear()
