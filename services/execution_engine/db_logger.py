"""
Execution Engine - Database Logger

Kapanan veya gerçekleşen işlemleri (fills/trades) asyncpg kullanarak
PostgreSQL'deki trades tablosuna asenkron olarak kaydeder.

Mimari Kuralı:
    - SADECE TradeRecord nesnelerini veritabanına yazar
    - Kendi başına karar ALMAZ — sadece kayıt tutar
    - Bağlantı havuzu (connection pool) ile çalışır
    - Tablo yoksa otomatik oluşturur (auto-migration)
"""

from __future__ import annotations

import asyncio
from typing import Optional

import asyncpg

from config.settings import settings
from shared.utils.logger import get_logger
from services.execution_engine.models.trade import TradeRecord

logger = get_logger("execution.db_logger")

# ── Tablo Şeması ────────────────────────────────────────────────
CREATE_TRADES_TABLE = """
CREATE TABLE IF NOT EXISTS trades (
    id                  BIGSERIAL PRIMARY KEY,
    order_id            TEXT NOT NULL,
    symbol              TEXT NOT NULL,
    exchange            TEXT NOT NULL,
    strategy            TEXT NOT NULL,
    side                TEXT NOT NULL,
    entry_price         DOUBLE PRECISION NOT NULL,
    exit_price          DOUBLE PRECISION NOT NULL,
    quantity            DOUBLE PRECISION NOT NULL,
    pnl                 DOUBLE PRECISION NOT NULL,
    pnl_pct             DOUBLE PRECISION NOT NULL,
    close_reason        TEXT NOT NULL,
    entry_time          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    exit_time           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    atr_value           DOUBLE PRECISION,
    stop_loss           DOUBLE PRECISION,
    tp1_price           DOUBLE PRECISION,
    tp2_price           DOUBLE PRECISION,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""

# TimescaleDB hypertable dönüşümü (varsa)
CREATE_HYPERTABLE = """
SELECT create_hypertable('trades', 'exit_time',
    if_not_exists => TRUE,
    migrate_data  => TRUE
);
"""

# İndeksler
CREATE_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades (symbol);
CREATE INDEX IF NOT EXISTS idx_trades_strategy ON trades (strategy);
CREATE INDEX IF NOT EXISTS idx_trades_order_id ON trades (order_id);
CREATE INDEX IF NOT EXISTS idx_trades_exit_time ON trades (exit_time DESC);
"""

# Insert sorgusu
INSERT_TRADE = """
INSERT INTO trades (
    order_id, symbol, exchange, strategy, side,
    entry_price, exit_price, quantity, pnl, pnl_pct,
    close_reason, entry_time, exit_time,
    atr_value, stop_loss, tp1_price, tp2_price
) VALUES (
    $1, $2, $3, $4, $5,
    $6, $7, $8, $9, $10,
    $11, to_timestamp($12), to_timestamp($13),
    $14, $15, $16, $17
)
RETURNING id;
"""

# ── Backoff sabitleri ────────────────────────────────────────────
INITIAL_BACKOFF_SEC: float = 1.0
MAX_BACKOFF_SEC: float = 30.0
BACKOFF_MULTIPLIER: float = 2.0
MAX_RETRIES: int = 5
POOL_MIN_SIZE: int = 2
POOL_MAX_SIZE: int = 10


class DbLogger:
    """
    Async PostgreSQL trade logger.

    Bağlantı havuzu (pool) ile verimli yazım yapar.
    Tablo yoksa otomatik oluşturur.
    """

    def __init__(self) -> None:
        self._pool: Optional[asyncpg.Pool] = None
        self._write_count: int = 0

    async def connect(self) -> None:
        """PostgreSQL bağlantı havuzunu oluştur ve tabloları hazırla."""
        self._pool = await asyncpg.create_pool(
            dsn=settings.postgres.dsn,
            min_size=POOL_MIN_SIZE,
            max_size=POOL_MAX_SIZE,
            command_timeout=30,
        )
        logger.info(
            "PostgreSQL bağlantı havuzu oluşturuldu | %s:%s/%s | pool=%d-%d",
            settings.postgres.host, settings.postgres.port,
            settings.postgres.database, POOL_MIN_SIZE, POOL_MAX_SIZE,
        )

        # Tabloları oluştur
        await self._init_schema()

    async def _init_schema(self) -> None:
        """trades tablosunu ve indeksleri oluştur."""
        if not self._pool:
            return

        async with self._pool.acquire() as conn:
            # Ana tablo
            await conn.execute(CREATE_TRADES_TABLE)
            logger.info("trades tablosu hazır")

            # TimescaleDB hypertable (opsiyonel — hata verirse devam et)
            try:
                await conn.execute(CREATE_HYPERTABLE)
                logger.info("TimescaleDB hypertable etkinleştirildi")
            except asyncpg.UndefinedFunctionError:
                logger.info("TimescaleDB yok — standart PostgreSQL tablosu kullanılıyor")
            except Exception as exc:
                logger.warning("Hypertable oluşturma atlandı: %s", exc)

            # İndeksler
            await conn.execute(CREATE_INDEXES)
            logger.info("trades indeksleri oluşturuldu")

    # ── Trade Kaydetme ───────────────────────────────────────────

    async def log_trade(self, record: TradeRecord) -> Optional[int]:
        """
        Tek bir TradeRecord'u veritabanına yaz.

        Exponential backoff ile yeniden deneme yapar.
        Başarılıysa eklenen satırın ID'sini döndürür.
        """
        backoff = INITIAL_BACKOFF_SEC

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                if not self._pool:
                    raise RuntimeError("PostgreSQL bağlantısı yok")

                async with self._pool.acquire() as conn:
                    row_id = await conn.fetchval(
                        INSERT_TRADE,
                        record.order_id,
                        record.symbol,
                        record.exchange,
                        record.strategy,
                        record.side,
                        record.entry_price,
                        record.exit_price,
                        record.quantity,
                        record.pnl,
                        record.pnl_pct,
                        record.close_reason,
                        record.entry_time,
                        record.exit_time,
                        record.atr_value,
                        record.stop_loss,
                        record.tp1_price,
                        record.tp2_price,
                    )

                self._write_count += 1
                logger.info(
                    "TRADE KAYDEDILDI | id=%s | %s %s | PnL=%.4f (%.2f%%) | neden=%s",
                    row_id, record.side, record.symbol,
                    record.pnl, record.pnl_pct, record.close_reason,
                )
                return row_id

            except (asyncpg.PostgresError, OSError, RuntimeError) as exc:
                if attempt == MAX_RETRIES:
                    logger.error(
                        "Trade kayıt BAŞARISIZ (tüm denemeler tükendi) | %s | %s",
                        record.order_id, exc,
                    )
                    return None

                logger.warning(
                    "Trade kayıt hatası (deneme %d/%d) | %ss sonra tekrar | hata=%s",
                    attempt, MAX_RETRIES, backoff, exc,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * BACKOFF_MULTIPLIER, MAX_BACKOFF_SEC)

        return None

    # ── Toplu Kayıt (Batch) ──────────────────────────────────────

    async def log_trades_batch(self, records: list[TradeRecord]) -> int:
        """
        Birden fazla TradeRecord'u tek seferde yaz (batch insert).
        Döndürülen değer: başarıyla yazılan kayıt sayısı.
        """
        if not records:
            return 0

        success_count = 0
        for record in records:
            result = await self.log_trade(record)
            if result is not None:
                success_count += 1
        return success_count

    # ── İstatistik ───────────────────────────────────────────────

    async def get_trade_stats(self) -> dict:
        """Günlük trade istatistiklerini döndür."""
        if not self._pool:
            return {}

        try:
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow("""
                    SELECT
                        COUNT(*) as total_trades,
                        COALESCE(SUM(pnl), 0) as total_pnl,
                        COALESCE(AVG(pnl_pct), 0) as avg_pnl_pct,
                        COUNT(*) FILTER (WHERE pnl > 0) as wins,
                        COUNT(*) FILTER (WHERE pnl <= 0) as losses
                    FROM trades
                    WHERE exit_time >= NOW() - INTERVAL '1 day'
                """)
                if row:
                    total = row["total_trades"]
                    return {
                        "total_trades": total,
                        "total_pnl": float(row["total_pnl"]),
                        "avg_pnl_pct": float(row["avg_pnl_pct"]),
                        "wins": row["wins"],
                        "losses": row["losses"],
                        "win_rate": (row["wins"] / total * 100) if total > 0 else 0.0,
                    }
        except Exception as exc:
            logger.error("Trade stats hatası: %s", exc)

        return {}

    # ── Yaşam Döngüsü ───────────────────────────────────────────

    async def close(self) -> None:
        """Bağlantı havuzunu kapat."""
        if self._pool:
            await self._pool.close()
            self._pool = None
            logger.info("PostgreSQL bağlantı havuzu kapatıldı | toplam kayıt=%d", self._write_count)
