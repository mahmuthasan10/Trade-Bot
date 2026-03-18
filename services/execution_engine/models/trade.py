"""
Execution Engine - Trade ve Position Modelleri

Position: Bellekte takip edilen aktif pozisyon.
TradeRecord: PostgreSQL'e kaydedilecek kapanmış/gerçekleşmiş işlem.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from shared.constants.enums import Exchange, OrderType, Side, Strategy, Timeframe


class PositionStatus(str, Enum):
    """Pozisyon yaşam döngüsü durumları."""
    PENDING = "PENDING"          # Emir gönderildi, fill bekleniyor
    OPEN = "OPEN"                # Fill geldi, pozisyon açık
    TP1_HIT = "TP1_HIT"         # TP1 tetiklendi, %50 kapatıldı
    TP2_HIT = "TP2_HIT"         # TP2 tetiklendi, %35 kapatıldı (trailing aktif)
    TRAILING = "TRAILING"        # TP3 trailing stop aktif
    CLOSED = "CLOSED"            # Tamamen kapandı


class CloseReason(str, Enum):
    """Pozisyon kapanma nedeni."""
    TP1 = "TP1"
    TP2 = "TP2"
    TP3_TRAILING = "TP3_TRAILING"
    STOP_LOSS = "STOP_LOSS"
    TIME_EXIT = "TIME_EXIT"          # 30dk hareketsizlik
    MANUAL = "MANUAL"
    KILL_SWITCH = "KILL_SWITCH"


@dataclass
class Position:
    """
    Bellekte takip edilen aktif pozisyon.

    Execution Engine bunu kullanarak kademeli TP, trailing stop
    ve time-in-trade kontrollerini yönetir.
    """

    # ── Kimlik ve sinyal ──
    order_id: str                     # Benzersiz emir ID'si
    symbol: str
    exchange: Exchange
    strategy: Strategy
    side: Side
    timeframe: Timeframe

    # ── Fiyat ve miktar ──
    entry_price: float
    total_quantity: float             # Toplam pozisyon büyüklüğü
    remaining_quantity: float         # Kalan (kapatılmamış) miktar

    # ── Risk seviyeleri (ApprovedOrder'dan aktarılır) ──
    stop_loss: float
    tp1_price: float
    tp2_price: float
    tp3_trailing_atr: float           # Trailing mesafe (ATR x çarpan)
    atr_value: float

    # ── Durum ──
    status: PositionStatus = PositionStatus.PENDING
    trailing_stop_price: Optional[float] = None   # TP2 sonrası dinamik stop
    trailing_high: Optional[float] = None          # Trailing için en yüksek/düşük

    # ── Zaman ──
    entry_time: float = field(default_factory=time.time)
    last_price_update: float = field(default_factory=time.time)

    # ── Realized PnL (kısmi kapanışlar toplamı) ──
    realized_pnl: float = 0.0

    @property
    def is_long(self) -> bool:
        return self.side == Side.BUY

    @property
    def tp1_quantity(self) -> float:
        """TP1: Toplam miktarın %50'si."""
        return self.total_quantity * 0.50

    @property
    def tp2_quantity(self) -> float:
        """TP2: Toplam miktarın %35'i."""
        return self.total_quantity * 0.35

    @property
    def tp3_quantity(self) -> float:
        """TP3 (trailing): Toplam miktarın %15'i."""
        return self.total_quantity * 0.15


@dataclass
class TradeRecord:
    """
    Kapanan veya kısmen gerçekleşen işlem — PostgreSQL'e yazılır.
    Her kısmi kapanış (TP1, TP2, TP3) ayrı bir TradeRecord üretir.
    """

    order_id: str
    symbol: str
    exchange: str
    strategy: str
    side: str
    entry_price: float
    exit_price: float
    quantity: float
    pnl: float                        # Realized PnL (bu kısım için)
    pnl_pct: float                    # PnL yüzdesi
    close_reason: str                 # CloseReason.value
    entry_time: float
    exit_time: float = field(default_factory=time.time)

    # Ek metadata
    atr_value: Optional[float] = None
    stop_loss: Optional[float] = None
    tp1_price: Optional[float] = None
    tp2_price: Optional[float] = None
