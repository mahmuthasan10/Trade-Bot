"""
Data Feed Service - Normalize Edilmiş Veri Modelleri
Borsadan gelen ham veri bu yapılara dönüştürülür, sonra Redis'e gönderilir.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

from shared.constants.enums import Exchange


@dataclass(slots=True)
class NormalizedTick:
    """Borsadan gelen her fiyat güncellemesi için standart yapı."""

    symbol: str                     # "BTC/USDT"
    exchange: Exchange
    price: float                    # Son işlem fiyatı (last)
    bid: float                      # En iyi alış
    ask: float                      # En iyi satış
    volume_24h: float               # 24s hacim
    timestamp: float = field(default_factory=time.time)  # Unix epoch
    bid_volume: Optional[float] = None
    ask_volume: Optional[float] = None

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "exchange": self.exchange.value,
            "price": self.price,
            "bid": self.bid,
            "ask": self.ask,
            "volume_24h": self.volume_24h,
            "timestamp": self.timestamp,
            "bid_volume": self.bid_volume,
            "ask_volume": self.ask_volume,
        }


@dataclass(slots=True)
class SpreadData:
    """Anlık spread bilgisi — Risk Gatekeeper bu veriye bakar."""

    symbol: str
    exchange: Exchange
    bid: float
    ask: float
    spread_abs: float               # ask - bid
    spread_pct: float               # (ask - bid) / mid * 100
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "exchange": self.exchange.value,
            "bid": self.bid,
            "ask": self.ask,
            "spread_abs": self.spread_abs,
            "spread_pct": self.spread_pct,
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_tick(cls, tick: NormalizedTick) -> SpreadData:
        mid = (tick.bid + tick.ask) / 2 if (tick.bid + tick.ask) > 0 else 1
        spread_abs = tick.ask - tick.bid
        spread_pct = (spread_abs / mid) * 100
        return cls(
            symbol=tick.symbol,
            exchange=tick.exchange,
            bid=tick.bid,
            ask=tick.ask,
            spread_abs=round(spread_abs, 8),
            spread_pct=round(spread_pct, 6),
            timestamp=tick.timestamp,
        )


@dataclass(slots=True)
class OrderBookData:
    """Top-of-book ve ilk seviyeler için normalize orderbook yapısı."""

    symbol: str
    exchange: Exchange
    bids: list[list[float]]
    asks: list[list[float]]
    bid: float
    ask: float
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "exchange": self.exchange.value,
            "bids": self.bids,
            "asks": self.asks,
            "bid": self.bid,
            "ask": self.ask,
            "timestamp": self.timestamp,
        }


@dataclass(slots=True)
class MarketDataPacket:
    """Connector'dan gelen tekil market veri paketi."""

    tick: NormalizedTick
    orderbook: OrderBookData
