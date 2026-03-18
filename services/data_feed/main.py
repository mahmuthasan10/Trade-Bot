"""
Data Feed Service - Ana Giriş Noktası
Kullanım:
    py -3 -m services.data_feed.main

Borsa WebSocket'lerine bağlanır, normalize veriyi Redis'e fırlatır.
SADECE veri akışı yapar — indikatör/mum/DB işlemi YAPMAZ.
"""

from __future__ import annotations

import asyncio
import signal
import sys
from typing import Optional

from config.settings import settings
from shared.utils.logger import get_logger
from services.data_feed.connectors.binance_feed import BinanceFeedConnector
from services.data_feed.feed_manager import FeedManager

logger = get_logger("data_feed.main")

# ── Dinlenecek varsayılan semboller ──────────────────────────────
DEFAULT_SYMBOLS: list[str] = [
    "BTC/USDT",
    "ETH/USDT",
]


def build_connectors() -> list:
    """Konfigürasyona göre exchange connector'ları oluşturur."""
    connectors = []

    # Binance (her zaman aktif)
    connectors.append(
        BinanceFeedConnector(
            symbols=DEFAULT_SYMBOLS,
            testnet=settings.binance.testnet,
        )
    )

    # Bybit connector eklenecek (Faz 1 opsiyonel)
    # connectors.append(BybitFeedConnector(...))

    return connectors


async def run() -> None:
    """Ana async döngü."""
    connectors = build_connectors()
    manager = FeedManager(connectors)

    loop = asyncio.get_running_loop()

    def _shutdown() -> None:
        logger.info("Kapatma sinyali alındı (SIGINT/SIGTERM)")
        asyncio.ensure_future(manager.stop())

    # Unix sinyalleri — Windows'ta sadece SIGINT çalışır
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _shutdown)
        except NotImplementedError:
            # Windows: signal handler desteklenmez, KeyboardInterrupt ile yakalanır
            pass

    logger.info("=" * 50)
    logger.info("Data Feed Service başlatılıyor...")
    logger.info("Semboller: %s", DEFAULT_SYMBOLS)
    logger.info("Testnet: %s", settings.binance.testnet)
    logger.info("Redis: %s:%s", settings.redis.host, settings.redis.port)
    logger.info("=" * 50)

    try:
        await manager.start()
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt yakalandı")
    finally:
        await manager.stop()


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
