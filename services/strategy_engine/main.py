"""
Strategy Engine - Ana Giriş Noktası
Redis'ten tick verilerini dinler, mumları inşa eder, indikatörleri hesaplar,
sinyal üretir ve channel:signals kanalına yayınlar.

Çalıştırma:
    python -m services.strategy_engine.main

Veri Akışı:
    Redis Tick → Subscriber → CandleBuilder → IndicatorCalculator
                                                      ↓
                                              SignalGenerator
                                                      ↓
                                              SignalPublisher → channel:signals
                                                      ↓
                                          [Faz 3: Risk Gatekeeper dinler]

Mimari Kuralı:
    - Bu servis ASLA borsaya doğrudan bağlanmaz
    - Sadece Redis Pub/Sub'dan veri tüketir ve sinyal yayınlar
    - Risk kontrolü, lot büyüklüğü, kasa yönetimi YAPMAZ
    - Tüm I/O asyncio ile non-blocking yapılır
"""

from __future__ import annotations

import asyncio
import signal
import sys
from typing import Optional

from config.settings import settings
from shared.constants.enums import Strategy, Timeframe
from shared.utils.logger import get_logger
from services.strategy_engine.subscriber import RedisSubscriber
from services.strategy_engine.candle_builder import CandleBuilder
from services.strategy_engine.indicators.calculator import IndicatorCalculator
from services.strategy_engine.signal_generator import SignalGenerator
from services.strategy_engine.signal_publisher import SignalPublisher
from services.strategy_engine.models.candle import OHLCV, IndicatorResult, SignalPacket

logger = get_logger("strategy_engine", level=settings.log_level)

# Dinlenecek semboller (config'den gelebilir, şimdilik sabit)
DEFAULT_SYMBOLS: list[str] = ["BTC/USDT", "ETH/USDT"]

# Kullanılacak timeframe'ler
ACTIVE_TIMEFRAMES: list[Timeframe] = [Timeframe.M5, Timeframe.M15]

# Heartbeat aralığı (saniye)
HEARTBEAT_INTERVAL_SEC: float = 30.0


class StrategyEngine:
    """
    Strateji Motorunun orkestratörü.

    Veri akışı:
        Redis Tick → Subscriber → CandleBuilder → IndicatorCalculator
                                                          ↓
                                                  SignalGenerator
                                                          ↓
                                                  SignalPublisher → channel:signals
    """

    def __init__(self, symbols: Optional[list[str]] = None) -> None:
        self._symbols = symbols or DEFAULT_SYMBOLS

        # İndikatör hesaplayıcı
        self._calculator = IndicatorCalculator()

        # Sinyal üretici (Day Trading 5m puan + Unified skor)
        self._signal_generator = SignalGenerator(strategy=Strategy.DAY_TRADING)

        # Sinyal yayıncısı (Redis channel:signals)
        self._signal_publisher = SignalPublisher()

        # Candle builder: mum kapandığında indikatör hesapla
        self._candle_builder = CandleBuilder(
            timeframes=ACTIVE_TIMEFRAMES,
            on_candle_closed=self._on_candle_closed,
        )

        # Redis subscriber: tick geldiğinde candle builder'a aktar
        self._subscriber = RedisSubscriber(
            symbols=self._symbols,
            on_tick=self._candle_builder.process_tick,
        )

        self._running: bool = False
        self._heartbeat_task: Optional[asyncio.Task] = None

        logger.info(
            "StrategyEngine başlatıldı | semboller=%s | timeframes=%s",
            self._symbols,
            [tf.value for tf in ACTIVE_TIMEFRAMES],
        )

    async def _on_candle_closed(self, candle: OHLCV) -> None:
        """Mum kapandığında: indikatör hesapla → sinyal değerlendir → yayınla."""
        # 1) İndikatörleri hesapla
        indicators: Optional[IndicatorResult] = (
            await self._calculator.on_candle_closed(candle)
        )

        if indicators is None:
            return

        logger.debug(
            "İndikatörler hazır | %s %s | EMA9=%s RSI=%s ADX=%s",
            candle.symbol,
            candle.timeframe.value,
            f"{indicators.ema_9:.2f}" if indicators.ema_9 else "-",
            f"{indicators.rsi:.2f}" if indicators.rsi else "-",
            f"{indicators.adx:.2f}" if indicators.adx else "-",
        )

        # 2) Sinyal değerlendir
        signal_packet: Optional[SignalPacket] = (
            await self._signal_generator.evaluate(candle, indicators)
        )

        if signal_packet is None:
            return

        # 3) Sinyali Redis'e yayınla
        try:
            await self._signal_publisher.publish_signal(signal_packet)
        except Exception as exc:
            logger.error(
                "Sinyal yayınlama hatası | %s | %s", signal_packet.symbol, exc
            )

    async def _heartbeat_loop(self) -> None:
        """Periyodik heartbeat gönderir."""
        while self._running:
            try:
                await self._signal_publisher.send_heartbeat()
            except Exception as exc:
                logger.warning("Heartbeat gönderilemedi | %s", exc)

            await asyncio.sleep(HEARTBEAT_INTERVAL_SEC)

    async def start(self) -> None:
        """Motoru başlat — publisher bağlan, subscriber dinlemeye geç."""
        self._running = True
        logger.info("Strategy Engine başlatılıyor...")

        # Publisher bağlantısını kur
        await self._signal_publisher.connect()

        # Heartbeat döngüsünü başlat
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

        try:
            await self._subscriber.run()
        except asyncio.CancelledError:
            logger.info("Strategy Engine durduruldu (CancelledError)")
        except Exception:
            logger.exception("Strategy Engine beklenmeyen hata")
            raise

    async def stop(self) -> None:
        """Motoru durdur — temiz kapatma."""
        self._running = False

        # Heartbeat durdur
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass

        # Subscriber kapat
        await self._subscriber.close()

        # Publisher kapat
        await self._signal_publisher.close()

        logger.info(
            "Strategy Engine kapatıldı | "
            "mumlar=%d | hesaplama=%d | sinyal=%d | yayın=%d",
            self._candle_builder.total_candles_closed,
            self._calculator.calculation_count,
            self._signal_generator.signal_count,
            self._signal_publisher.publish_count,
        )


async def main() -> None:
    """Ana async entry point."""
    engine = StrategyEngine(symbols=DEFAULT_SYMBOLS)

    # Graceful shutdown sinyalleri
    loop = asyncio.get_running_loop()
    shutdown_event = asyncio.Event()

    def _signal_handler() -> None:
        logger.info("Kapatma sinyali alındı, durduruluyor...")
        shutdown_event.set()

    # Windows'ta signal handler farklı çalışır
    if sys.platform != "win32":
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, _signal_handler)

    # Engine'i bir task olarak başlat
    engine_task = asyncio.create_task(engine.start())

    try:
        if sys.platform == "win32":
            # Windows'ta KeyboardInterrupt ile durdur
            await engine_task
        else:
            # Unix'te sinyal ile durdur
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
