"""
Risk Gatekeeper - Spread Kontrolü

MASTER_BOT_v3.md Bölüm 5.5:
    Anlık spread / 60dk ortalama spread > 2.0×  →  İŞLEM REDDEDİLİR
    7 puanlık mükemmel sinyal bile olsa, spread şişmişse girilmez.

Veri Kaynağı:
    Data Feed servisi her tick'te spread verisini şu kanala yayınlar:
        stream:spread:{symbol}  →  {spread_abs, spread_pct, timestamp}

Bu modül Redis'ten son 60 dakikanın spread verilerini okur ve
güncel spread'i ortalamaya kıyaslar.
"""

from __future__ import annotations

import json
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Optional

import redis.asyncio as aioredis
from redis.exceptions import RedisError

from config.settings import settings
from shared.constants import redis_channels as ch
from shared.utils.logger import get_logger

logger = get_logger("risk_gatekeeper.spread_gate")

# Spread reddedilme eşiği: anlık spread > ortalama × bu çarpan → RED
SPREAD_REJECTION_MULTIPLIER: float = 2.0

# Ortalama penceresi: 60 dakika (saniye cinsinden)
SPREAD_AVG_WINDOW_SEC: float = 3600.0

# Minimum geçerli spread verisi sayısı (yetersiz veri varsa geçir)
MIN_SPREAD_SAMPLES: int = 10


@dataclass(slots=True)
class SpreadSample:
    """Tek bir spread ölçümü."""
    spread_pct: float
    timestamp: float


class SpreadTracker:
    """
    Sembol bazlı spread geçmişini bellekte tutar (60dk sliding window).
    Redis Pub/Sub'dan gelen spread verileriyle güncellenir.
    """

    def __init__(self, window_sec: float = SPREAD_AVG_WINDOW_SEC) -> None:
        self._window_sec = window_sec
        # symbol → deque of SpreadSample (son 60dk)
        self._history: dict[str, deque[SpreadSample]] = defaultdict(deque)

    def record(self, symbol: str, spread_pct: float, timestamp: float) -> None:
        """Yeni spread verisi ekle ve eski verileri temizle."""
        buf = self._history[symbol]
        buf.append(SpreadSample(spread_pct=spread_pct, timestamp=timestamp))
        self._evict_old(buf, timestamp)

    def _evict_old(self, buf: deque[SpreadSample], now: float) -> None:
        """Window dışına çıkan eski örnekleri temizle."""
        cutoff = now - self._window_sec
        while buf and buf[0].timestamp < cutoff:
            buf.popleft()

    def get_avg_spread(self, symbol: str) -> Optional[float]:
        """
        Son 60dk ortalama spread'i hesapla.
        Yeterli veri yoksa None döner.
        """
        buf = self._history.get(symbol)
        if not buf or len(buf) < MIN_SPREAD_SAMPLES:
            return None

        # Mevcut zamana göre eski verileri temizle
        self._evict_old(buf, time.time())

        if len(buf) < MIN_SPREAD_SAMPLES:
            return None

        total = sum(s.spread_pct for s in buf)
        return total / len(buf)

    def get_current_spread(self, symbol: str) -> Optional[float]:
        """En son spread değerini döndür."""
        buf = self._history.get(symbol)
        if not buf:
            return None
        return buf[-1].spread_pct

    @property
    def tracked_symbols(self) -> list[str]:
        return list(self._history.keys())


class SpreadGatekeeper:
    """
    Spread filtresi — sinyal geldiğinde anlık/ortalama spread oranını kontrol eder.

    Kontrol:
        ratio = anlık_spread / 60dk_ortalama_spread
        ratio > 2.0  → REJECT (spread şişmiş, giriş yapmak riskli)
        ratio ≤ 2.0  → PASS

    Ayrıca stream:spread:{symbol} kanalına subscribe olarak spread geçmişini
    sürekli güncel tutar.
    """

    def __init__(self) -> None:
        self._tracker = SpreadTracker()
        self._client: Optional[aioredis.Redis] = None
        self._pubsub: Optional[aioredis.client.PubSub] = None
        self._reject_count: int = 0
        self._pass_count: int = 0

    async def connect(self, symbols: list[str]) -> None:
        """Redis bağlantısı kur ve spread kanallarına abone ol."""
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
        channels = [ch.SPREAD_STREAM.format(symbol=s) for s in symbols]
        await self._pubsub.subscribe(*channels)

        logger.info(
            "SpreadGatekeeper baglandi | %d kanal | esik=%.1fx",
            len(channels),
            SPREAD_REJECTION_MULTIPLIER,
        )

    async def listen_spread_updates(self) -> None:
        """
        Redis Pub/Sub'dan spread güncellemelerini sürekli dinle.
        Bu metot bir asyncio.Task olarak çalıştırılmalıdır.
        """
        if self._pubsub is None:
            raise RuntimeError("connect() çağrılmadan listen başlatılamaz")

        logger.info("Spread dinleme döngüsü başladı")

        while True:
            try:
                message = await self._pubsub.get_message(
                    ignore_subscribe_messages=True,
                    timeout=1.0,
                )
                if message is None or message["type"] != "message":
                    continue

                data = json.loads(message["data"])
                symbol = data.get("symbol", "")
                spread_pct = float(data.get("spread_pct", 0.0))
                ts = float(data.get("timestamp", time.time()))

                self._tracker.record(symbol, spread_pct, ts)

            except (json.JSONDecodeError, KeyError, ValueError) as exc:
                logger.warning("Geçersiz spread mesajı | %s", exc)
            except (RedisError, OSError) as exc:
                logger.error("Spread dinleme hatası | %s", exc)
                break

    def check(self, symbol: str) -> tuple[bool, float, str]:
        """
        Spread kontrolü yap.

        Dönüş: (passed, ratio, detail_message)
            passed: True → geçti, False → reddedildi
            ratio: anlık/ortalama oranı (veri yoksa 0.0)
            detail_message: İnsan tarafından okunabilir açıklama
        """
        current = self._tracker.get_current_spread(symbol)
        avg = self._tracker.get_avg_spread(symbol)

        # Yeterli spread verisi yoksa geçir (yeni sembol, data henüz dolmadı)
        if current is None or avg is None:
            self._pass_count += 1
            return (
                True,
                0.0,
                f"Yetersiz spread verisi ({symbol}), kontrol atlandı",
            )

        # Sıfıra bölme koruması
        if avg <= 0:
            self._pass_count += 1
            return (True, 0.0, f"Ortalama spread=0 ({symbol}), kontrol atlandı")

        ratio = current / avg

        if ratio > SPREAD_REJECTION_MULTIPLIER:
            self._reject_count += 1
            detail = (
                f"SPREAD_BLOCK: {symbol} | anlık={current:.4f}% | "
                f"60dk_ort={avg:.4f}% | oran={ratio:.1f}x > "
                f"esik={SPREAD_REJECTION_MULTIPLIER:.1f}x"
            )
            logger.warning(detail)
            return (False, ratio, detail)

        self._pass_count += 1
        return (
            True,
            ratio,
            f"Spread OK: {symbol} | oran={ratio:.1f}x <= "
            f"{SPREAD_REJECTION_MULTIPLIER:.1f}x",
        )

    async def close(self) -> None:
        """Bağlantıları kapat."""
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
            "SpreadGatekeeper kapatıldı | geçen=%d | reddedilen=%d",
            self._pass_count,
            self._reject_count,
        )

    @property
    def reject_count(self) -> int:
        return self._reject_count

    @property
    def pass_count(self) -> int:
        return self._pass_count
