"""
Deployment Service - Historical Data Fetcher

Binance'den belirli tarih aralığındaki OHLCV verisini çeker,
eksik mumları tespit edip tamamlar ve CSV / TimescaleDB'ye kaydeder.

Kullanım:
    python -m services.deployment.fetcher \
        --symbol BTC/USDT \
        --timeframe 5m \
        --days 30 \
        --output csv          # veya "db" ya da "both"

Mimari Kuralı:
    - SADECE geçmiş veri çeker — sinyal, risk, emir İŞLEMEZ
    - Tüm ağ çağrıları async + exponential backoff
    - API anahtarları .env'den okunur
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import ccxt.pro as ccxtpro
import asyncpg

from config.settings import settings
from shared.utils.logger import get_logger

logger = get_logger("deployment.fetcher")

# ── Sabitler ─────────────────────────────────────────────────────
BATCH_LIMIT: int = 1000            # ccxt tek seferde döndürdüğü mum sayısı
INITIAL_BACKOFF_SEC: float = 1.0
MAX_BACKOFF_SEC: float = 30.0
BACKOFF_MULTIPLIER: float = 2.0
MAX_RETRIES: int = 5
RATE_LIMIT_SLEEP_SEC: float = 0.5  # API rate-limit koruması

# Timeframe → milisaniye çevirisi
TF_MS: dict[str, int] = {
    "1m": 60_000,
    "3m": 180_000,
    "5m": 300_000,
    "15m": 900_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
    "1d": 86_400_000,
}

# CSV çıktı dizini
DATA_DIR = Path(__file__).resolve().parent / "data"


class HistoricalDataFetcher:
    """
    ccxt.pro üzerinden Binance OHLCV verisini sayfalayarak çeker.

    Akış:
        1. Başlangıç ve bitiş timestamp'lerini hesapla
        2. Sayfalı (paginated) fetch ile tüm mumları topla
        3. Eksik mumları tespit et ve tekrar çek
        4. CSV'ye ve/veya TimescaleDB'ye yaz
    """

    def __init__(
        self,
        symbol: str,
        timeframe: str = "5m",
        days: int = 30,
        output: str = "csv",
    ) -> None:
        if timeframe not in TF_MS:
            raise ValueError(f"Desteklenmeyen timeframe: {timeframe}. Desteklenen: {list(TF_MS.keys())}")

        self.symbol = symbol
        self.timeframe = timeframe
        self.days = days
        self.output = output  # "csv", "db", "both"

        self._exchange: Optional[ccxtpro.Exchange] = None
        self._db_pool: Optional[asyncpg.Pool] = None
        self._candles: list[list] = []  # [[ts, o, h, l, c, v], ...]

    # ── Bağlantılar ──────────────────────────────────────────────

    async def _connect_exchange(self) -> None:
        """Binance bağlantısını kur (sadece public endpoint, API key opsiyonel)."""
        self._exchange = ccxtpro.binance({
            "enableRateLimit": True,
            "options": {"defaultType": "spot"},
        })
        await self._exchange.load_markets()
        if self.symbol not in self._exchange.markets:
            raise ValueError(f"Sembol bulunamadı: {self.symbol}")
        logger.info("Binance bağlantısı kuruldu | piyasa sayısı=%d", len(self._exchange.markets))

    async def _connect_db(self) -> None:
        """TimescaleDB bağlantı havuzu oluştur."""
        self._db_pool = await asyncpg.create_pool(
            dsn=settings.postgres.dsn,
            min_size=2,
            max_size=5,
        )
        logger.info("TimescaleDB bağlantı havuzu oluşturuldu")

    async def _ensure_table(self) -> None:
        """OHLCV hypertable yoksa oluştur."""
        assert self._db_pool is not None
        async with self._db_pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS ohlcv_history (
                    time        TIMESTAMPTZ NOT NULL,
                    symbol      TEXT        NOT NULL,
                    timeframe   TEXT        NOT NULL,
                    open        DOUBLE PRECISION,
                    high        DOUBLE PRECISION,
                    low         DOUBLE PRECISION,
                    close       DOUBLE PRECISION,
                    volume      DOUBLE PRECISION,
                    PRIMARY KEY (time, symbol, timeframe)
                );
            """)
            # TimescaleDB hypertable (idempotent)
            await conn.execute("""
                SELECT create_hypertable(
                    'ohlcv_history', 'time',
                    if_not_exists => TRUE
                );
            """)
        logger.info("ohlcv_history tablosu hazır")

    # ── Veri Çekme ───────────────────────────────────────────────

    async def _fetch_batch(self, since: int) -> list[list]:
        """Tek bir sayfa OHLCV verisi çek — exponential backoff ile."""
        assert self._exchange is not None
        backoff = INITIAL_BACKOFF_SEC

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                ohlcv = await self._exchange.fetch_ohlcv(
                    self.symbol,
                    self.timeframe,
                    since=since,
                    limit=BATCH_LIMIT,
                )
                return ohlcv
            except Exception as exc:
                if attempt == MAX_RETRIES:
                    raise
                logger.warning(
                    "fetch_ohlcv hatası (deneme %d/%d) | %ss sonra tekrar | %s",
                    attempt, MAX_RETRIES, backoff, exc,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * BACKOFF_MULTIPLIER, MAX_BACKOFF_SEC)

        return []  # Ulaşılmamalı

    async def fetch_all(self) -> list[list]:
        """
        Tüm OHLCV verisini sayfalayarak çek.

        Returns:
            [[timestamp_ms, open, high, low, close, volume], ...]
        """
        await self._connect_exchange()

        tf_ms = TF_MS[self.timeframe]
        end_ms = int(time.time() * 1000)
        start_ms = end_ms - (self.days * 86_400_000)
        current_ms = start_ms

        all_candles: list[list] = []
        total_expected = (end_ms - start_ms) // tf_ms

        logger.info(
            "Veri çekimi başlıyor | %s %s | %s → %s | beklenen ~%d mum",
            self.symbol,
            self.timeframe,
            datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d"),
            datetime.fromtimestamp(end_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d"),
            total_expected,
        )

        while current_ms < end_ms:
            batch = await self._fetch_batch(since=current_ms)
            if not batch:
                break

            all_candles.extend(batch)

            # Sonraki sayfanın başlangıcı: son mumun ts + 1 tf
            last_ts = batch[-1][0]
            current_ms = last_ts + tf_ms

            logger.info(
                "Çekilen: %d mum | son=%s",
                len(all_candles),
                datetime.fromtimestamp(last_ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M"),
            )

            # Rate limit koruması
            await asyncio.sleep(RATE_LIMIT_SLEEP_SEC)

        # Duplikatları temizle (timestamp bazlı)
        seen: set[int] = set()
        unique: list[list] = []
        for candle in all_candles:
            ts = candle[0]
            if ts not in seen:
                seen.add(ts)
                unique.append(candle)

        unique.sort(key=lambda c: c[0])
        self._candles = unique

        # Eksik mum kontrolü
        gap_count = self._detect_gaps(unique, tf_ms, start_ms, end_ms)
        logger.info(
            "Çekim tamamlandı | toplam=%d mum | eksik=%d boşluk",
            len(unique), gap_count,
        )

        return unique

    def _detect_gaps(
        self,
        candles: list[list],
        tf_ms: int,
        start_ms: int,
        end_ms: int,
    ) -> int:
        """Eksik mumları tespit et ve logla."""
        if len(candles) < 2:
            return 0

        gap_count = 0
        for i in range(1, len(candles)):
            expected_ts = candles[i - 1][0] + tf_ms
            actual_ts = candles[i][0]
            if actual_ts > expected_ts + tf_ms:
                gap_start = datetime.fromtimestamp(expected_ts / 1000, tz=timezone.utc)
                gap_end = datetime.fromtimestamp(actual_ts / 1000, tz=timezone.utc)
                missing = (actual_ts - expected_ts) // tf_ms
                logger.warning(
                    "BOŞLUK TESPİTİ | %s → %s | ~%d eksik mum",
                    gap_start.strftime("%Y-%m-%d %H:%M"),
                    gap_end.strftime("%Y-%m-%d %H:%M"),
                    missing,
                )
                gap_count += 1

        return gap_count

    # ── Kaydetme ─────────────────────────────────────────────────

    async def save_csv(self) -> Path:
        """Veriyi CSV dosyasına yaz."""
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        safe_symbol = self.symbol.replace("/", "_")
        filename = f"{safe_symbol}_{self.timeframe}_{self.days}d.csv"
        filepath = DATA_DIR / filename

        with open(filepath, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["timestamp", "open", "high", "low", "close", "volume"])
            for candle in self._candles:
                writer.writerow(candle)

        logger.info("CSV kaydedildi | %s | %d satır", filepath, len(self._candles))
        return filepath

    async def save_db(self) -> int:
        """Veriyi TimescaleDB'ye yaz (UPSERT — duplikat güvenli)."""
        await self._connect_db()
        await self._ensure_table()
        assert self._db_pool is not None

        rows = [
            (
                datetime.fromtimestamp(c[0] / 1000, tz=timezone.utc),
                self.symbol,
                self.timeframe,
                c[1],  # open
                c[2],  # high
                c[3],  # low
                c[4],  # close
                c[5],  # volume
            )
            for c in self._candles
        ]

        async with self._db_pool.acquire() as conn:
            inserted = await conn.executemany("""
                INSERT INTO ohlcv_history (time, symbol, timeframe, open, high, low, close, volume)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                ON CONFLICT (time, symbol, timeframe) DO UPDATE SET
                    open   = EXCLUDED.open,
                    high   = EXCLUDED.high,
                    low    = EXCLUDED.low,
                    close  = EXCLUDED.close,
                    volume = EXCLUDED.volume
            """, rows)

        logger.info("TimescaleDB'ye yazıldı | %d satır", len(rows))
        return len(rows)

    # ── Ana Akış ─────────────────────────────────────────────────

    async def run(self) -> None:
        """Tam pipeline: çek → kaydet."""
        try:
            await self.fetch_all()

            if self.output in ("csv", "both"):
                await self.save_csv()

            if self.output in ("db", "both"):
                await self.save_db()

        finally:
            await self.close()

    async def close(self) -> None:
        """Tüm bağlantıları kapat."""
        if self._exchange:
            await self._exchange.close()
            self._exchange = None
        if self._db_pool:
            await self._db_pool.close()
            self._db_pool = None


# ── CLI Giriş Noktası ────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Binance OHLCV geçmiş veri çekici",
    )
    parser.add_argument("--symbol", type=str, default="BTC/USDT", help="İşlem çifti (ör: BTC/USDT)")
    parser.add_argument("--timeframe", type=str, default="5m", help="Mum periyodu (1m, 5m, 15m, 1h, 4h, 1d)")
    parser.add_argument("--days", type=int, default=30, help="Geriye dönük gün sayısı")
    parser.add_argument("--output", type=str, default="csv", choices=["csv", "db", "both"], help="Çıktı hedefi")

    args = parser.parse_args()

    fetcher = HistoricalDataFetcher(
        symbol=args.symbol,
        timeframe=args.timeframe,
        days=args.days,
        output=args.output,
    )
    asyncio.run(fetcher.run())


if __name__ == "__main__":
    main()
