"""
Strategy Engine - Async Redis Subscriber
Data Feed servisinin Redis'e yayınladığı tick verilerini dinler.
Bu modül ASLA borsaya doğrudan bağlanmaz — sadece Redis Pub/Sub tüketir.
"""

from __future__ import annotations

import asyncio
import json
from typing import Callable, Awaitable, Optional

import redis.asyncio as aioredis
from redis.exceptions import RedisError

from config.settings import settings
from shared.constants import redis_channels as ch
from shared.utils.logger import get_logger

logger = get_logger("strategy_engine.subscriber")

# Bağlantı kopmasında üstel geri çekilme
INITIAL_BACKOFF_SEC: float = 1.0
MAX_BACKOFF_SEC: float = 60.0
BACKOFF_MULTIPLIER: float = 2.0

# Tick callback tipi: symbol, price, volume, bid, ask, timestamp
TickCallback = Callable[[dict], Awaitable[None]]


class RedisSubscriber:
    """
    Async Redis Pub/Sub subscriber.
    Belirtilen sembollerin tick kanallarını dinler ve her mesajda
    callback fonksiyonunu tetikler.
    """

    def __init__(
        self,
        symbols: list[str],
        on_tick: TickCallback,
    ) -> None:
        self._symbols = symbols
        self._on_tick = on_tick
        self._client: Optional[aioredis.Redis] = None
        self._pubsub: Optional[aioredis.client.PubSub] = None
        self._running: bool = False
        self._message_count: int = 0

    async def connect(self) -> None:
        """Redis bağlantısını kur ve kanallara abone ol."""
        self._client = aioredis.Redis(
            host=settings.redis.host,
            port=settings.redis.port,
            password=settings.redis.password or None,
            db=settings.redis.db,
            decode_responses=True,
            socket_connect_timeout=5,
        )
        await self._client.ping()

        self._pubsub = self._client.pubsub()

        # Her sembol için tick kanalına abone ol
        channels = [
            ch.TICK_STREAM.format(symbol=symbol) for symbol in self._symbols
        ]
        await self._pubsub.subscribe(*channels)

        logger.info(
            "Redis subscriber bağlandı | %d kanal | %s",
            len(channels),
            ", ".join(channels),
        )

    async def _reconnect(self) -> None:
        """Bağlantı kopmasında üstel geri çekilme ile yeniden bağlan."""
        backoff = INITIAL_BACKOFF_SEC

        while self._running:
            try:
                await self.close()
                await self.connect()
                logger.info("Redis subscriber yeniden bağlandı")
                return
            except (RedisError, OSError, ConnectionError) as exc:
                logger.warning(
                    "Yeniden bağlanma başarısız | %.1fs sonra tekrar | hata=%s",
                    backoff,
                    exc,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * BACKOFF_MULTIPLIER, MAX_BACKOFF_SEC)

    async def run(self) -> None:
        """
        Ana dinleme döngüsü.
        Redis Pub/Sub mesajlarını asenkron olarak tüketir.
        Bağlantı koparsa otomatik yeniden bağlanır.
        """
        await self.connect()
        self._running = True

        logger.info("Subscriber dinlemeye başladı | semboller=%s", self._symbols)

        while self._running:
            try:
                if self._pubsub is None:
                    await self._reconnect()
                    continue

                # Non-blocking mesaj okuma (asyncio döngüsünü bloklamaz)
                message = await self._pubsub.get_message(
                    ignore_subscribe_messages=True,
                    timeout=1.0,
                )

                if message is None:
                    continue

                if message["type"] != "message":
                    continue

                try:
                    tick_data: dict = json.loads(message["data"])
                except (json.JSONDecodeError, TypeError) as exc:
                    logger.warning("Geçersiz JSON mesajı | hata=%s", exc)
                    continue

                self._message_count += 1
                await self._on_tick(tick_data)

            except (RedisError, OSError, ConnectionError) as exc:
                logger.error("Subscriber bağlantı hatası | %s", exc)
                if self._running:
                    await self._reconnect()

    async def close(self) -> None:
        """Bağlantıları temiz kapat."""
        self._running = False

        if self._pubsub is not None:
            try:
                await self._pubsub.unsubscribe()
                await self._pubsub.aclose()
            except Exception:
                pass
            self._pubsub = None

        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:
                pass
            self._client = None

        logger.info(
            "Redis subscriber kapatıldı | toplam mesaj=%d", self._message_count
        )

    @property
    def message_count(self) -> int:
        return self._message_count

    @property
    def is_running(self) -> bool:
        return self._running
