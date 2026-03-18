"""
Execution Engine - Ana Giriş Noktası

channel:approved_orders kanalından gelen onaylı emirleri borsaya iletir,
pozisyonları takip eder, kademeli TP/SL yönetir ve kapanan işlemleri
PostgreSQL'e kaydeder.

Çalıştırma:
    python -m services.execution_engine.main

Veri Akışı:
    channel:approved_orders → OrderExecutor
                                  ↓ (fill)
                              PositionManager
                                  ├── TP1 (%50 kapat, stop → BE)
                                  ├── TP2 (%35 kapat, trailing başlat)
                                  ├── TP3 (trailing stop ile sür)
                                  └── Time Exit (30dk hareketsizlik)
                                  ↓ (close)
                              DbLogger → PostgreSQL (trades tablosu)
                                  ↓ (fill bilgisi)
                              channel:fills → Risk Gatekeeper (PnL günceller)

Mimari Kuralı:
    - Bu servis SADECE onaylı emirleri yürütür
    - Risk kararları Risk Gatekeeper'a aittir
    - Tüm I/O asyncio ile non-blocking yapılır
"""

from __future__ import annotations

import asyncio
import signal
import sys

from config.settings import settings
from shared.utils.logger import get_logger
from services.execution_engine.executor import OrderExecutor
from services.execution_engine.position_manager import PositionManager
from services.execution_engine.db_logger import DbLogger

logger = get_logger("execution_engine", level=settings.log_level)


async def main() -> None:
    """Ana async entry point."""

    logger.info("=" * 60)
    logger.info("EXECUTION ENGINE BAŞLATILIYOR")
    logger.info("Ortam: %s | Binance Testnet: %s", settings.environment, settings.binance.testnet)
    logger.info("=" * 60)

    # ── Bileşenleri oluştur ──────────────────────────────────────
    db_logger = DbLogger()
    position_manager = PositionManager()
    executor = OrderExecutor()

    # ── Bileşenleri birbirine bağla (dependency injection) ───────
    # Executor → PositionManager: yeni pozisyon açıldığında bildir
    executor._position_callback = position_manager.add_position

    # PositionManager → Executor: pozisyon kapatmak için
    position_manager._close_callback = executor.close_position

    # PositionManager → DbLogger: trade kaydı yazmak için
    position_manager._trade_callback = db_logger.log_trade

    # ── Bağlantıları başlat ──────────────────────────────────────
    try:
        await db_logger.connect()
        logger.info("PostgreSQL bağlantısı hazır")
    except Exception as exc:
        logger.error("PostgreSQL bağlantı hatası: %s — veritabanı olmadan devam ediliyor", exc)

    # ── Graceful shutdown ────────────────────────────────────────
    loop = asyncio.get_running_loop()
    shutdown_event = asyncio.Event()

    def _signal_handler() -> None:
        logger.info("Kapatma sinyali alındı, durduruluyor...")
        shutdown_event.set()

    if sys.platform != "win32":
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, _signal_handler)

    # ── Arka plan görevlerini başlat ─────────────────────────────
    executor_task = asyncio.create_task(executor.start(), name="executor")
    position_task = asyncio.create_task(position_manager.start(), name="position_manager")

    logger.info("Tüm bileşenler çalışıyor — emir bekleniyor...")

    try:
        if sys.platform == "win32":
            # Windows'ta signal handler çalışmaz, KeyboardInterrupt'a güven
            await asyncio.gather(executor_task, position_task)
        else:
            await shutdown_event.wait()
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt — kapatılıyor...")

    # ── Temiz kapatma ────────────────────────────────────────────
    logger.info("Bileşenler durduruluyor...")

    await executor.stop()
    await position_manager.stop()
    await db_logger.close()

    # Task'ları iptal et
    for task in (executor_task, position_task):
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    logger.info("Execution Engine tamamen durduruldu.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
