"""
Deployment Service - Tick Replayer

Kaydedilen geçmiş OHLCV verisini (CSV veya TimescaleDB) okuyup,
Data Feed Service formatında Redis'e publish eder.

Böylece Strateji, Risk ve Execution servislerini hiçbir kod değişikliği
yapmadan geçmiş veri ile stres testine sokabiliriz.

Kullanım:
    python -m services.deployment.replayer \
        --symbol BTC/USDT \
        --source csv \
        --speed 1.0 \
        --file services/deployment/data/BTC_USDT_5m_30d.csv

    python -m services.deployment.replayer \
        --symbol BTC/USDT \
        --source db \
        --timeframe 5m \
        --speed 10.0

Mimari Kuralı:
    - Data Feed Service ile BİREBİR aynı Redis kanallarını kullanır
    - NormalizedTick ve SpreadData formatında publish eder
    - stream:ticks:{symbol} ve stream:spread:{symbol} kanallarına yazar
    - Diğer servisler canlı veri mi replay mi olduğunu AYIRT EDEMEZ
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import asyncpg
import redis.asyncio as aioredis
from redis.exceptions import RedisError

from config.settings import settings
from shared.constants import redis_channels as ch
from shared.constants.enums import Exchange
from shared.utils.logger import get_logger

logger = get_logger("deployment.replayer")

# ── Sabitler ─────────────────────────────────────────────────────
INITIAL_BACKOFF_SEC: float = 1.0
MAX_BACKOFF_SEC: float = 15.0
BACKOFF_MULTIPLIER: float = 2.0
HEARTBEAT_INTERVAL: int = 50  # Her N mumda bir heartbeat


class TickReplayer:
    """
    Geçmiş veriyi canlı piyasa gibi Redis'e yeniden oynatır.

    Akış:
        1. CSV veya DB'den OHLCV verisini yükle
        2. Her mumu NormalizedTick + SpreadData formatına çevir
        3. Ayarlanabilir hızda (speed) Redis kanallarına publish et
        4. Heartbeat ve ilerleme bilgisi gönder
    """

    def __init__(
        self,
        symbol: str,
        source: str = "csv",
        speed: float = 1.0,
        csv_file: Optional[str] = None,
        timeframe: str = "5m",
        days: int = 30,
    ) -> None:
        self.symbol = symbol
        self.source = source       # "csv" veya "db"
        self.speed = max(0.01, speed)
        self.csv_file = csv_file
        self.timeframe = timeframe
        self.days = days

        self._redis: Optional[aioredis.Redis] = None
        self._db_pool: Optional[asyncpg.Pool] = None
        self._candles: list[dict] = []
        self._running: bool = False
        self._replayed_count: int = 0

    # ── Bağlantılar ──────────────────────────────────────────────

    async def _connect_redis(self) -> None:
        """Redis bağlantısı kur."""
        self._redis = aioredis.Redis(
            host=settings.redis.host,
            port=settings.redis.port,
            password=settings.redis.password or None,
            db=settings.redis.db,
            decode_responses=True,
            socket_connect_timeout=5,
        )
        await self._redis.ping()
        logger.info("Redis bağlantısı kuruldu")

    async def _connect_db(self) -> None:
        """TimescaleDB bağlantı havuzu oluştur."""
        self._db_pool = await asyncpg.create_pool(
            dsn=settings.postgres.dsn,
            min_size=1,
            max_size=3,
        )
        logger.info("TimescaleDB bağlantısı kuruldu")

    # ── Veri Yükleme ─────────────────────────────────────────────

    async def _load_from_csv(self) -> list[dict]:
        """CSV dosyasından OHLCV verisini oku."""
        if not self.csv_file:
            # Varsayılan dosya yolu
            safe_symbol = self.symbol.replace("/", "_")
            data_dir = Path(__file__).resolve().parent / "data"
            self.csv_file = str(data_dir / f"{safe_symbol}_{self.timeframe}_{self.days}d.csv")

        filepath = Path(self.csv_file)
        if not filepath.exists():
            raise FileNotFoundError(f"CSV dosyası bulunamadı: {filepath}")

        candles: list[dict] = []
        with open(filepath, newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                candles.append({
                    "timestamp": float(row["timestamp"]),
                    "open": float(row["open"]),
                    "high": float(row["high"]),
                    "low": float(row["low"]),
                    "close": float(row["close"]),
                    "volume": float(row["volume"]),
                })

        candles.sort(key=lambda c: c["timestamp"])
        logger.info("CSV'den %d mum yüklendi | %s", len(candles), filepath.name)
        return candles

    async def _load_from_db(self) -> list[dict]:
        """TimescaleDB'den OHLCV verisini oku."""
        await self._connect_db()
        assert self._db_pool is not None

        async with self._db_pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT
                    EXTRACT(EPOCH FROM time) * 1000 AS timestamp,
                    open, high, low, close, volume
                FROM ohlcv_history
                WHERE symbol = $1 AND timeframe = $2
                ORDER BY time ASC
            """, self.symbol, self.timeframe)

        candles = [
            {
                "timestamp": float(r["timestamp"]),
                "open": float(r["open"]),
                "high": float(r["high"]),
                "low": float(r["low"]),
                "close": float(r["close"]),
                "volume": float(r["volume"]),
            }
            for r in rows
        ]

        logger.info("TimescaleDB'den %d mum yüklendi", len(candles))
        return candles

    # ── Replay Motoru ────────────────────────────────────────────

    def _candle_to_tick_payload(self, candle: dict) -> dict:
        """
        Bir OHLCV mumunu NormalizedTick formatına çevir.

        Gerçek canlı veriye yakınsamak için:
            - price = close fiyatı (mumun kapanışı)
            - bid = close * 0.9999 (simüle spread)
            - ask = close * 1.0001
        """
        close = candle["close"]
        spread_factor = 0.0001  # %0.01 simüle spread

        bid = round(close * (1 - spread_factor), 8)
        ask = round(close * (1 + spread_factor), 8)
        mid = (bid + ask) / 2

        tick_payload = {
            "symbol": self.symbol,
            "exchange": Exchange.BINANCE.value,
            "price": close,
            "bid": bid,
            "ask": ask,
            "volume_24h": candle["volume"],
            "timestamp": candle["timestamp"] / 1000,  # Unix epoch saniye
            "bid_volume": None,
            "ask_volume": None,
        }

        spread_payload = {
            "symbol": self.symbol,
            "exchange": Exchange.BINANCE.value,
            "bid": bid,
            "ask": ask,
            "spread_abs": round(ask - bid, 8),
            "spread_pct": round((ask - bid) / mid * 100, 6) if mid > 0 else 0.0,
            "timestamp": candle["timestamp"] / 1000,
        }

        return tick_payload, spread_payload

    async def _publish(self, channel: str, data: dict) -> None:
        """Redis'e JSON publish et — backoff ile."""
        backoff = INITIAL_BACKOFF_SEC
        for attempt in range(1, 4):
            try:
                if self._redis:
                    await self._redis.publish(channel, json.dumps(data))
                    return
            except (RedisError, OSError) as exc:
                if attempt == 3:
                    logger.error("Redis publish başarısız | channel=%s | %s", channel, exc)
                    return
                logger.warning("Redis publish hatası (deneme %d) | %s", attempt, exc)
                await asyncio.sleep(backoff)
                backoff = min(backoff * BACKOFF_MULTIPLIER, MAX_BACKOFF_SEC)

                # Yeniden bağlan
                try:
                    await self._connect_redis()
                except Exception:
                    pass

    async def replay(self) -> None:
        """
        Ana replay döngüsü.

        Her mum arasında (1 / speed) saniye bekler.
        Örnek: speed=10 → saniyede 10 mum, speed=0.5 → 2 saniyede 1 mum
        """
        await self._connect_redis()

        # Veri yükle
        if self.source == "csv":
            self._candles = await self._load_from_csv()
        elif self.source == "db":
            self._candles = await self._load_from_db()
        else:
            raise ValueError(f"Bilinmeyen kaynak: {self.source}")

        if not self._candles:
            logger.error("Oynatılacak veri bulunamadı!")
            return

        total = len(self._candles)
        interval = 1.0 / self.speed
        self._running = True

        tick_channel = ch.TICK_STREAM.format(symbol=self.symbol)
        spread_channel = ch.SPREAD_STREAM.format(symbol=self.symbol)

        first_dt = datetime.fromtimestamp(
            self._candles[0]["timestamp"] / 1000, tz=timezone.utc
        ).strftime("%Y-%m-%d %H:%M")
        last_dt = datetime.fromtimestamp(
            self._candles[-1]["timestamp"] / 1000, tz=timezone.utc
        ).strftime("%Y-%m-%d %H:%M")

        logger.info(
            "REPLAY BAŞLIYOR | %s | %d mum | %s → %s | hız=%.1fx | aralık=%.3fs",
            self.symbol, total, first_dt, last_dt, self.speed, interval,
        )

        replay_start = time.monotonic()

        for idx, candle in enumerate(self._candles):
            if not self._running:
                logger.info("Replay durduruldu (kullanıcı talebi)")
                break

            tick_payload, spread_payload = self._candle_to_tick_payload(candle)

            # Timestamp'i "şimdi" olarak güncelle — servisler stale veri reddetmesin
            now = time.time()
            tick_payload["timestamp"] = now
            spread_payload["timestamp"] = now

            # Canlı veri gibi her iki kanala da publish et
            await self._publish(tick_channel, tick_payload)
            await self._publish(spread_channel, spread_payload)

            self._replayed_count = idx + 1

            # Heartbeat
            if (idx + 1) % HEARTBEAT_INTERVAL == 0:
                elapsed = time.monotonic() - replay_start
                pct = (idx + 1) / total * 100
                candle_dt = datetime.fromtimestamp(
                    candle["timestamp"] / 1000, tz=timezone.utc
                ).strftime("%Y-%m-%d %H:%M")
                logger.info(
                    "REPLAY İLERLEME | %d/%d (%%%.1f) | tarih=%s | geçen=%.1fs | fiyat=%.4f",
                    idx + 1, total, pct, candle_dt, elapsed, candle["close"],
                )

                # Sistem heartbeat
                heartbeat = {
                    "service": "replayer",
                    "timestamp": now,
                    "replayed": idx + 1,
                    "total": total,
                    "progress_pct": round(pct, 2),
                    "speed": self.speed,
                }
                await self._publish(ch.HEARTBEAT, heartbeat)

            # Hız kontrolü — mumlar arası bekleme
            await asyncio.sleep(interval)

        elapsed_total = time.monotonic() - replay_start
        logger.info(
            "REPLAY TAMAMLANDI | %s | %d/%d mum | toplam süre=%.1fs",
            self.symbol, self._replayed_count, total, elapsed_total,
        )

    async def stop(self) -> None:
        """Replay'i durdur."""
        self._running = False

    async def close(self) -> None:
        """Tüm bağlantıları kapat."""
        self._running = False
        if self._redis:
            await self._redis.aclose()
            self._redis = None
        if self._db_pool:
            await self._db_pool.close()
            self._db_pool = None

    async def run(self) -> None:
        """Tam pipeline: bağlan → oynat → kapat."""
        try:
            await self.replay()
        finally:
            await self.close()


# ── CLI Giriş Noktası ────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Geçmiş veri replay aracı — Redis Pub/Sub üzerinden canlı simülasyon",
    )
    parser.add_argument("--symbol", type=str, default="BTC/USDT", help="İşlem çifti")
    parser.add_argument("--source", type=str, default="csv", choices=["csv", "db"], help="Veri kaynağı")
    parser.add_argument("--speed", type=float, default=1.0, help="Oynatma hızı (1.0 = saniyede 1 mum)")
    parser.add_argument("--file", type=str, default=None, dest="csv_file", help="CSV dosya yolu (source=csv ise)")
    parser.add_argument("--timeframe", type=str, default="5m", help="Mum periyodu (source=db ise)")
    parser.add_argument("--days", type=int, default=30, help="Geriye dönük gün (source=db ise)")

    args = parser.parse_args()

    replayer = TickReplayer(
        symbol=args.symbol,
        source=args.source,
        speed=args.speed,
        csv_file=args.csv_file,
        timeframe=args.timeframe,
        days=args.days,
    )
    asyncio.run(replayer.run())


if __name__ == "__main__":
    main()
