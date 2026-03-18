"""
Strategy Engine - Sinyal Publisher
Üretilen sinyal paketlerini Redis channel:signals kanalına yayınlar.

Data Feed'deki RedisPublisher ile aynı pattern:
exponential backoff + auto-reconnect.
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
from services.strategy_engine.models.candle import SignalPacket

logger = get_logger("strategy_engine.signal_publisher")

INITIAL_BACKOFF_SEC: float = 1.0
MAX_BACKOFF_SEC: float = 15.0
BACKOFF_MULTIPLIER: float = 2.0
MAX_RETRIES: int = 5
HEARTBEAT_INTERVAL_SEC: float = 30.0


class SignalPublisher:
    """
    Async Redis publisher — sinyal paketlerini channel:signals'a gönderir.
    Heartbeat ile kendi sağlığını raporlar.
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
        await self._client.ping()
        logger.info(
            "Signal publisher bağlandı | %s:%s",
            settings.redis.host,
            settings.redis.port,
        )

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
                    "Redis publish hatası (deneme %d/%d) | channel=%s | "
                    "%.1fs sonra tekrar | hata=%s",
                    attempt,
                    MAX_RETRIES,
                    channel,
                    backoff,
                    exc,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * BACKOFF_MULTIPLIER, MAX_BACKOFF_SEC)

    async def publish_signal(self, signal: SignalPacket) -> None:
        """Sinyal paketini channel:signals kanalına JSON olarak yayınla."""
        payload = json.dumps(signal.to_dict())
        await self._safe_publish(ch.SIGNAL_CHANNEL, payload)
        self._publish_count += 1

        logger.info(
            "Sinyal yayınlandı | %s %s %s | puan=%d | unified=%.1f | "
            "kanal=%s | toplam=%d",
            signal.symbol,
            signal.side.value,
            signal.strategy.value,
            signal.raw_points,
            signal.unified_score,
            ch.SIGNAL_CHANNEL,
            self._publish_count,
        )

    async def send_heartbeat(self) -> None:
        """Strategy Engine heartbeat gönder."""
        payload = json.dumps({
            "service": "strategy_engine",
            "timestamp": time.time(),
            "signal_count": self._publish_count,
        })
        await self._safe_publish(ch.HEARTBEAT, payload)

    async def close(self) -> None:
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:
                pass
            self._client = None
            logger.info("Signal publisher kapatıldı | toplam=%d", self._publish_count)

    @property
    def publish_count(self) -> int:
        return self._publish_count
