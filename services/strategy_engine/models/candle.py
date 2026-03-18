"""
Strategy Engine - Veri Modelleri
OHLCV mum yapısı, indikatör sonuçları ve sinyal paketi.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

from shared.constants.enums import Exchange, Side, Strategy, Timeframe


@dataclass(slots=True)
class OHLCV:
    """Tek bir OHLCV mumu."""

    symbol: str                     # "BTC/USDT"
    exchange: Exchange
    timeframe: Timeframe            # M5 veya M15
    open: float
    high: float
    low: float
    close: float
    volume: float                   # Mum süresi boyunca toplam hacim
    vwap: float                     # Volume-weighted average price
    tick_count: int                 # Mumu oluşturan tick sayısı
    open_time: float                # Mum açılış zamanı (unix epoch)
    close_time: float               # Mum kapanış zamanı (unix epoch)
    is_closed: bool = False         # Mum kapandı mı?

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "exchange": self.exchange.value,
            "timeframe": self.timeframe.value,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
            "vwap": self.vwap,
            "tick_count": self.tick_count,
            "open_time": self.open_time,
            "close_time": self.close_time,
            "is_closed": self.is_closed,
        }


@dataclass(slots=True)
class IndicatorResult:
    """Bir mum kapanışında hesaplanan tüm indikatör değerleri."""

    symbol: str
    exchange: Exchange
    timeframe: Timeframe
    timestamp: float                # Hesaplama zamanı

    # Moving Averages
    ema_9: Optional[float] = None
    ema_21: Optional[float] = None
    ema_55: Optional[float] = None
    sma_50: Optional[float] = None
    sma_200: Optional[float] = None

    # MACD (12, 26, 9)
    macd_line: Optional[float] = None
    macd_signal: Optional[float] = None
    macd_histogram: Optional[float] = None

    # Trend & Volatilite
    adx: Optional[float] = None
    plus_di: Optional[float] = None
    minus_di: Optional[float] = None
    atr: Optional[float] = None

    # Momentum
    rsi: Optional[float] = None

    # Volume-Weighted
    vwap: Optional[float] = None

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "exchange": self.exchange.value,
            "timeframe": self.timeframe.value,
            "timestamp": self.timestamp,
            "ema_9": self.ema_9,
            "ema_21": self.ema_21,
            "ema_55": self.ema_55,
            "sma_50": self.sma_50,
            "sma_200": self.sma_200,
            "macd_line": self.macd_line,
            "macd_signal": self.macd_signal,
            "macd_histogram": self.macd_histogram,
            "adx": self.adx,
            "plus_di": self.plus_di,
            "minus_di": self.minus_di,
            "atr": self.atr,
            "rsi": self.rsi,
            "vwap": self.vwap,
        }


@dataclass(slots=True)
class SignalPacket:
    """
    Sinyal Motoru tarafından üretilen sinyal paketi.
    Risk Gatekeeper'a channel:signals üzerinden iletilir.

    Bu paket SADECE sinyali taşır — lot büyüklüğü, risk kontrolü,
    kasa yönetimi Faz 3'ün (Risk Gatekeeper) işidir.
    """

    symbol: str
    exchange: Exchange
    strategy: Strategy              # DAY_TRADING, FIRSAT, UNIVERSAL
    side: Side                      # BUY veya SELL
    timeframe: Timeframe            # Tetikleyen timeframe (5m)

    # Skorlama
    raw_points: int                 # Day Trading 12 puanlık ham skor (0-12)
    unified_score: float            # 0-100 arası Unified Sinyal Skoru
    min_threshold: int              # Bu strateji için minimum puan eşiği

    # Tetikleyen mum bilgisi
    entry_price: float              # Mum kapanış fiyatı (referans giriş)
    atr: Optional[float]            # ATR(14) — stop/TP hesaplaması için

    # Hangi bileşenler tetiklendi (şeffaflık)
    components: dict                # {"momentum": 2, "vwap": 2, ...}

    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "exchange": self.exchange.value,
            "strategy": self.strategy.value,
            "side": self.side.value,
            "timeframe": self.timeframe.value,
            "raw_points": self.raw_points,
            "unified_score": self.unified_score,
            "min_threshold": self.min_threshold,
            "entry_price": self.entry_price,
            "atr": self.atr,
            "components": self.components,
            "timestamp": self.timestamp,
        }
