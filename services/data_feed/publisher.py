"""
Data Feed Service - Redis Publisher
Normalize edilmiş tick ve spread verilerini Redis Pub/Sub kanallarına yayınlar.
Heartbeat gönderir, bağlantı kopmasında otomatik yeniden bağlanır.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Optional

import redis.asyncio as aioredis
from redis.exceptions import RedisError

from config.settings import settings
from shared.constants import redis_channels as ch
from shared.utils.logger import get_logger
from services.data_feed.models.tick import NormalizedTick, OrderBookData, SpreadData

logger = get_logger("data_feed.publisher")

HEARTBEAT_INTERVAL_SEC: float = 10.0
INITIAL_BACKOFF_SEC: float = 1.0
MAX_BACKOFF_SEC: float = 15.0
BACKOFF_MULTIPLIER: float = 2.0
MAX_RETRIES: int = 5


class RedisPublisher:
    """
    Async Redis Pub/Sub publisher.
    Tick ve Spread verilerini ilgili kanallara JSON olarak gönderir.
    """

    def __init__(self) -> None:
        self._client: Optional[aioredis.Redis] = None
        self._publish_count: int = 0

    async def connect(self) -> None:
        self._client = aioredis.Redis(
            host=settings.redis.host,
            port=settings.redis.port,
            password=settings.redis.password or None,
            db=settings.redis.db,
            decode_responses=True,
            socket_connect_timeout=5,
        )
        # Bağlantı testi
        await self._client.ping()
        logger.info("Redis publisher bağlandı | %s:%s", settings.redis.host, settings.redis.port)

    async def _ensure_connection(self) -> None:
        if self._client is None:
            await self.connect()

    async def _safe_publish(self, channel: str, payload: str) -> None:
        backoff = INITIAL_BACKOFF_SEC

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                await self._ensure_connection()
                if self._client is None:
                    raise RuntimeError("Redis bağlantısı kurulamadı")
                await self._client.publish(channel, payload)
                return
            except (RedisError, OSError, RuntimeError) as exc:
                if self._client is not None:
                    await self.close()

                if attempt == MAX_RETRIES:
                    raise

                logger.warning(
                    "Redis publish hatası (deneme %d/%d) | channel=%s | %ss sonra tekrar denenecek | hata=%s",
                    attempt,
                    MAX_RETRIES,
                    channel,
                    backoff,
                    exc,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * BACKOFF_MULTIPLIER, MAX_BACKOFF_SEC)

    async def publish_tick(self, tick: NormalizedTick) -> None:
        """Tick verisini TICK_STREAM kanalına yayınlar."""
        channel = ch.TICK_STREAM.format(symbol=tick.symbol)
        payload = json.dumps(tick.to_dict())
        await self._safe_publish(channel, payload)
        self._publish_count += 1

    async def publish_spread(self, spread: SpreadData) -> None:
        """Spread verisini SPREAD_STREAM kanalına yayınlar."""
        channel = ch.SPREAD_STREAM.format(symbol=spread.symbol)
        payload = json.dumps(spread.to_dict())
        await self._safe_publish(channel, payload)

    async def publish_orderbook(self, orderbook: OrderBookData) -> None:
        """Orderbook verisini ORDERBOOK_STREAM kanalına yayınlar."""
        channel = ch.ORDERBOOK_STREAM.format(symbol=orderbook.symbol)
        payload = json.dumps(orderbook.to_dict())
        await self._safe_publish(channel, payload)

    async def send_heartbeat(self, service_name: str = "data_feed") -> None:
        """Periyodik heartbeat gönderir."""
        payload = json.dumps({
            "service": service_name,
            "timestamp": time.time(),
            "publish_count": self._publish_count,
        })
        await self._safe_publish(ch.HEARTBEAT, payload)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
            logger.info("Redis publisher kapatıldı")
