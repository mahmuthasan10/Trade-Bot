"""
Risk Gatekeeper - Onaylanan ve Reddedilen Emir Modelleri

ApprovedOrder: Tüm risk filtrelerinden geçen emirler.
    → channel:approved_orders kanalına JSON olarak yayınlanır.
    → Execution Engine bu kanalı dinler ve borsaya iletir.

RejectedOrder: Risk filtresine takılan emirler.
    → channel:rejected_orders kanalına JSON olarak yayınlanır.
    → Loglama ve analiz amaçlıdır.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

from shared.constants.enums import (
    Exchange,
    OrderType,
    RiskLevel,
    Side,
    Strategy,
    Timeframe,
)


@dataclass(slots=True)
class ApprovedOrder:
    """
    Risk Gatekeeper tarafından onaylanmış emir.

    Execution Engine bu nesneyi alır ve borsaya iletir.
    İçinde lot büyüklüğü, stop/TP seviyeleri ve risk metadata'sı bulunur.
    """

    # ── Sinyal bilgisi (SignalPacket'ten aktarılır) ──
    symbol: str
    exchange: Exchange
    strategy: Strategy
    side: Side
    timeframe: Timeframe
    entry_price: float
    unified_score: float

    # ── Risk motoru tarafından hesaplanan değerler ──
    lot_size: float                  # Risk-adjusted pozisyon büyüklüğü
    lot_multiplier: float            # Uygulanan lot çarpanı (recovery, risk seviyesi vb.)
    stop_loss: float                 # Adaptif ATR-based stop seviyesi
    atr_multiplier: float            # Kullanılan ATR çarpanı (1.2 veya 1.5)
    atr_value: float                 # Hesaplanan ATR(14, 5m) değeri

    # ── Take Profit seviyeleri (Execution Engine yönetir) ──
    tp1_price: float                 # 1R — %50 kapat
    tp2_price: float                 # 2R — %35 kapat, trailing başlat
    tp3_trailing_atr: float          # Trailing stop mesafesi (ATR × çarpan)

    # ── Risk metadata ──
    risk_level: RiskLevel            # Emir anındaki portföy risk seviyesi
    risk_per_trade_pct: float        # Bu işlem için ayrılan risk yüzdesi
    order_type: OrderType = OrderType.MARKET

    # ── Zaman damgası ──
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "exchange": self.exchange.value,
            "strategy": self.strategy.value,
            "side": self.side.value,
            "timeframe": self.timeframe.value,
            "entry_price": self.entry_price,
            "unified_score": self.unified_score,
            "lot_size": self.lot_size,
            "lot_multiplier": self.lot_multiplier,
            "stop_loss": self.stop_loss,
            "atr_multiplier": self.atr_multiplier,
            "atr_value": self.atr_value,
            "tp1_price": self.tp1_price,
            "tp2_price": self.tp2_price,
            "tp3_trailing_atr": self.tp3_trailing_atr,
            "risk_per_trade_pct": self.risk_per_trade_pct,
            "risk_level": self.risk_level.name,
            "order_type": self.order_type.value,
            "timestamp": self.timestamp,
        }


@dataclass(slots=True)
class RejectedOrder:
    """
    Risk filtresine takılan emir.
    Loglama, analiz ve dashboard gösterimi için saklanır.
    """

    symbol: str
    exchange: Exchange
    strategy: Strategy
    side: Side
    unified_score: float
    entry_price: float

    # Reddedilme sebebi
    rejection_reason: str            # "SPREAD_GATE", "HARD_KILL", "LOT_ZERO" vb.
    rejection_detail: str            # İnsan tarafından okunabilir açıklama

    # Opsiyonel ek bilgi
    risk_level: Optional[RiskLevel] = None
    spread_ratio: Optional[float] = None

    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        result: dict = {
            "symbol": self.symbol,
            "exchange": self.exchange.value,
            "strategy": self.strategy.value,
            "side": self.side.value,
            "unified_score": self.unified_score,
            "entry_price": self.entry_price,
            "rejection_reason": self.rejection_reason,
            "rejection_detail": self.rejection_detail,
            "timestamp": self.timestamp,
        }
        if self.risk_level is not None:
            result["risk_level"] = self.risk_level.name
        if self.spread_ratio is not None:
            result["spread_ratio"] = self.spread_ratio
        return result
