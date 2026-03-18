"""
Master Trading Bot v3.0 - Altyapı Sağlık Kontrolü
Docker servislerinin (Redis + TimescaleDB) ayakta olduğunu doğrular.

Kullanım:
    python scripts/check_infra.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# Proje kökünü Python path'ine ekle
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import settings
from shared.utils.logger import get_logger

logger = get_logger("infra_check")


async def check_redis() -> bool:
    """Redis bağlantısını test et."""
    try:
        import redis.asyncio as aioredis

        client = aioredis.Redis(
            host=settings.redis.host,
            port=settings.redis.port,
            password=settings.redis.password or None,
            db=settings.redis.db,
            decode_responses=True,
        )
        pong = await client.ping()
        info = await client.info("server")
        logger.info(
            f"Redis OK | v{info.get('redis_version', '?')} | "
            f"{settings.redis.host}:{settings.redis.port}"
        )
        await client.aclose()
        return pong
    except Exception as e:
        logger.error(f"Redis HATA: {e}")
        return False


async def check_postgres() -> bool:
    """TimescaleDB bağlantısını test et."""
    try:
        import asyncpg

        conn = await asyncpg.connect(dsn=settings.postgres.dsn)

        # PostgreSQL versiyonu
        pg_version = await conn.fetchval("SELECT version();")
        logger.info(f"PostgreSQL OK | {pg_version[:60]}...")

        # TimescaleDB eklentisi kontrol
        ts_version = await conn.fetchval(
            "SELECT extversion FROM pg_extension WHERE extname = 'timescaledb';"
        )
        if ts_version:
            logger.info(f"TimescaleDB OK | v{ts_version}")
        else:
            logger.warning("TimescaleDB eklentisi BULUNAMADI!")

        # Tabloları kontrol et
        tables = await conn.fetch(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public';"
        )
        table_names = [t["tablename"] for t in tables]
        logger.info(f"Tablolar: {table_names}")

        expected = {"ticks", "candles", "trades"}
        missing = expected - set(table_names)
        if missing:
            logger.warning(f"Eksik tablolar: {missing}")
        else:
            logger.info("Tüm tablolar mevcut!")

        await conn.close()
        return True
    except Exception as e:
        logger.error(f"PostgreSQL HATA: {e}")
        return False


async def main() -> None:
    logger.info("=" * 50)
    logger.info("Altyapı Sağlık Kontrolü Başlatılıyor...")
    logger.info("=" * 50)

    redis_ok = await check_redis()
    pg_ok = await check_postgres()

    logger.info("=" * 50)
    if redis_ok and pg_ok:
        logger.info("TÜM SERVİSLER HAZIR - Faz 1 altyapısı çalışıyor!")
    else:
        logger.error("SORUN VAR - Yukarıdaki hataları kontrol et.")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
