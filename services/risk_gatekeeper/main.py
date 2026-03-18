"""
Risk Gatekeeper - Ana Giriş Noktası
channel:signals kanalından gelen sinyalleri dinler, risk filtrelerinden geçirir,
onaylanan emirleri channel:approved_orders kanalına yayınlar.

Çalıştırma:
    python -m services.risk_gatekeeper.main

Veri Akışı:
    channel:signals → RiskGatekeeper
                          ├── Portfolio Risk Gate (Net PnL)
                          ├── Spread Gate (anlık/60dk oran)
                          ├── Adaptif ATR Stop (volatilite rejimi)
                          └── Recovery Lot Çarpanı
                              ↓
                    channel:approved_orders → [Faz 4: Execution Engine dinler]
                    channel:rejected_orders → [Loglama / Dashboard]

Mimari Kuralı:
    - Bu servis ASLA borsaya doğrudan bağlanmaz
    - ASLA emir göndermez — sadece karar verir
    - Tüm I/O asyncio ile non-blocking yapılır
"""

from __future__ import annotations

import asyncio
import signal
import sys

from config.settings import settings
from shared.utils.logger import get_logger
from services.risk_gatekeeper.gatekeeper import RiskGatekeeper

logger = get_logger("risk_gatekeeper", level=settings.log_level)

# Dinlenecek semboller
DEFAULT_SYMBOLS: list[str] = ["BTC/USDT", "ETH/USDT"]


async def main() -> None:
    """Ana async entry point."""
    engine = RiskGatekeeper(symbols=DEFAULT_SYMBOLS)

    # Graceful shutdown sinyalleri
    loop = asyncio.get_running_loop()
    shutdown_event = asyncio.Event()

    def _signal_handler() -> None:
        logger.info("Kapatma sinyali alındı, durduruluyor...")
        shutdown_event.set()

    if sys.platform != "win32":
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, _signal_handler)

    engine_task = asyncio.create_task(engine.start())

    try:
        if sys.platform == "win32":
            await engine_task
        else:
            await shutdown_event.wait()
            await engine.stop()
            engine_task.cancel()
            try:
                await engine_task
            except asyncio.CancelledError:
                pass
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt — kapatılıyor...")
        await engine.stop()
        engine_task.cancel()
        try:
            await engine_task
        except asyncio.CancelledError:
            pass


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
