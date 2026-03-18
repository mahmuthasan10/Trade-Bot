"""
Strategy Engine - Candle Builder (Mum İnşası)
Gelen ham tick verilerinden bellekte dinamik 5m ve 15m OHLCV mumları oluşturur.
Bir mum kapandığında callback ile bildirim yapar.

Mimari Kuralı: Bu modül ASLA borsaya bağlanmaz.
Yalnızca subscriber'dan gelen tick dict'leri işler.
"""

from __future__ import annotations

import math
import time
from typing import Callable, Awaitable, Optional

from shared.constants.enums import Exchange, Timeframe
from shared.utils.logger import get_logger
from services.strategy_engine.models.candle import OHLCV

logger = get_logger("strategy_engine.candle_builder")

# Mum kapanış callback tipi
CandleCallback = Callable[[OHLCV], Awaitable[None]]

# Timeframe → saniye çevirimi
TIMEFRAME_SECONDS: dict[Timeframe, int] = {
    Timeframe.M5: 300,    # 5 dakika
    Timeframe.M15: 900,   # 15 dakika
}


def _floor_timestamp(ts: float, interval_sec: int) -> float:
    """Timestamp'i verilen aralığın başlangıcına yuvarla (floor).

    Örn: 10:03:27 → interval=300 → 10:00:00
    """
    return math.floor(ts / interval_sec) * interval_sec


class _CandleAccumulator:
    """Tek bir sembol + tek bir timeframe için aktif mum biriktirici.

    Gelen her tick ile OHLCV değerlerini günceller.
    VWAP = Σ(price × volume) / Σ(volume) — tick bazlı yaklaşım.
    """

    __slots__ = (
        "symbol", "exchange", "timeframe", "interval_sec",
        "open_time", "close_time",
        "_open", "_high", "_low", "_close",
        "_volume", "_vwap_numerator", "_tick_count",
    )

    def __init__(
        self,
        symbol: str,
        exchange: Exchange,
        timeframe: Timeframe,
        open_time: float,
    ) -> None:
        self.symbol = symbol
        self.exchange = exchange
        self.timeframe = timeframe
        self.interval_sec = TIMEFRAME_SECONDS[timeframe]
        self.open_time = open_time
        self.close_time = open_time + self.interval_sec

        # OHLCV başlangıç değerleri — ilk tick'te set edilecek
        self._open: Optional[float] = None
        self._high: float = -math.inf
        self._low: float = math.inf
        self._close: float = 0.0
        self._volume: float = 0.0
        self._vwap_numerator: float = 0.0  # Σ(price × volume)
        self._tick_count: int = 0

    def update(self, price: float, volume: float) -> None:
        """Tick verisi ile mumu güncelle."""
        if self._open is None:
            self._open = price

        if price > self._high:
            self._high = price
        if price < self._low:
            self._low = price

        self._close = price
        self._volume += volume
        self._vwap_numerator += price * volume
        self._tick_count += 1

    def to_ohlcv(self, is_closed: bool = False) -> OHLCV:
        """Mevcut durumu OHLCV dataclass'ına dönüştür."""
        vwap = (
            self._vwap_numerator / self._volume
            if self._volume > 0
            else (self._open or 0.0)
        )

        return OHLCV(
            symbol=self.symbol,
            exchange=self.exchange,
            timeframe=self.timeframe,
            open=self._open or 0.0,
            high=self._high if self._high != -math.inf else 0.0,
            low=self._low if self._low != math.inf else 0.0,
            close=self._close,
            volume=round(self._volume, 8),
            vwap=round(vwap, 8),
            tick_count=self._tick_count,
            open_time=self.open_time,
            close_time=self.close_time,
            is_closed=is_closed,
        )

    def is_tick_in_window(self, tick_ts: float) -> bool:
        """Tick bu mum penceresine ait mi?"""
        return self.open_time <= tick_ts < self.close_time

    @property
    def is_empty(self) -> bool:
        return self._tick_count == 0


class CandleBuilder:
    """
    Çoklu sembol ve timeframe için mum inşa edici.

    Her (symbol, exchange, timeframe) kombinasyonu için ayrı bir
    accumulator tutar. Tick geldiğinde:
      1. Tick'in ait olduğu mum penceresini hesapla
      2. Eğer mevcut mum penceresi geçmişse → mumu kapat, callback çağır
      3. Tick'i yeni/mevcut muma ekle
    """

    def __init__(
        self,
        timeframes: list[Timeframe],
        on_candle_closed: CandleCallback,
    ) -> None:
        self._timeframes = timeframes
        self._on_candle_closed = on_candle_closed

        # Anahtar: (symbol, exchange, timeframe) → _CandleAccumulator
        self._accumulators: dict[
            tuple[str, str, Timeframe], _CandleAccumulator
        ] = {}

        self._total_candles_closed: int = 0

        logger.info(
            "CandleBuilder başlatıldı | timeframes=%s",
            [tf.value for tf in timeframes],
        )

    async def process_tick(self, tick_data: dict) -> None:
        """
        Gelen tick verisini işle, gerekirse mum kapat.

        tick_data formatı (Data Feed publisher JSON):
        {
            "symbol": "BTC/USDT",
            "exchange": "binance",
            "price": 67500.25,
            "volume_24h": 1200000000.0,
            "timestamp": 1710777600.123,
            ...
        }
        """
        symbol: str = tick_data["symbol"]
        exchange_str: str = tick_data["exchange"]
        price: float = tick_data["price"]
        timestamp: float = tick_data["timestamp"]

        # Tick başına hacim tahmini:
        # Data feed 24h volume verir, tick bazlı volume yok.
        # Mum hacmi olarak tick_count kullanılacak,
        # ancak VWAP hesabı için her tick'e birim hacim (1.0) atanır.
        # Gerçek trade volume geldiğinde bu değiştirilebilir.
        tick_volume: float = 1.0

        for tf in self._timeframes:
            key = (symbol, exchange_str, tf)
            interval_sec = TIMEFRAME_SECONDS[tf]
            tick_open_time = _floor_timestamp(timestamp, interval_sec)

            acc = self._accumulators.get(key)

            # Mevcut accumulator bu pencereye ait değilse → kapat
            if acc is not None and not acc.is_tick_in_window(timestamp):
                if not acc.is_empty:
                    closed_candle = acc.to_ohlcv(is_closed=True)
                    self._total_candles_closed += 1

                    logger.debug(
                        "Mum kapandı | %s %s %s | O=%.2f H=%.2f L=%.2f C=%.2f | ticks=%d",
                        symbol,
                        exchange_str,
                        tf.value,
                        closed_candle.open,
                        closed_candle.high,
                        closed_candle.low,
                        closed_candle.close,
                        closed_candle.tick_count,
                    )

                    await self._on_candle_closed(closed_candle)

                # Yeni accumulator başlat
                acc = None

            # Accumulator yoksa oluştur
            if acc is None:
                try:
                    exchange = Exchange(exchange_str)
                except ValueError:
                    logger.warning("Bilinmeyen exchange: %s", exchange_str)
                    continue

                acc = _CandleAccumulator(
                    symbol=symbol,
                    exchange=exchange,
                    timeframe=tf,
                    open_time=tick_open_time,
                )
                self._accumulators[key] = acc

            acc.update(price, tick_volume)

    def get_active_candle(
        self, symbol: str, exchange: str, timeframe: Timeframe
    ) -> Optional[OHLCV]:
        """Henüz kapanmamış aktif mumu döndür (debug/monitoring)."""
        key = (symbol, exchange, timeframe)
        acc = self._accumulators.get(key)
        if acc is None or acc.is_empty:
            return None
        return acc.to_ohlcv(is_closed=False)

    @property
    def total_candles_closed(self) -> int:
        return self._total_candles_closed

    @property
    def active_accumulator_count(self) -> int:
        return len(self._accumulators)
