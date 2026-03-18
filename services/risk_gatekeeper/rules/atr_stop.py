"""
Risk Gatekeeper - Adaptif ATR Stop Hesaplayıcı

MASTER_BOT_v3.md Bölüm 3.4:

    ATR medyanı = son 100 periyodun medyan ATR(14, 5m) değeri

    Eğer ATR şu an < medyan  → çarpan = 1.2 (dar stop, yüksek R:R)
    Eğer ATR şu an ≥ medyan  → çarpan = 1.5 (geniş stop, sweep koruması)

    Pozisyon büyüklüğü ATR çarpanına ters orantılıdır — geniş stop
    kullanıldığında lot otomatik küçülür, %0.30 sabit risk korunur.

Bu modül SignalPacket'teki ATR değerini alır, medyan geçmişiyle kıyaslar
ve uygun çarpanı belirler.
"""

from __future__ import annotations

import statistics
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Optional

from shared.constants.enums import Side
from shared.utils.logger import get_logger

logger = get_logger("risk_gatekeeper.atr_stop")

# ATR medyan hesaplaması için lookback penceresi
ATR_LOOKBACK_PERIODS: int = 100

# Volatilite rejimine göre çarpanlar
ATR_MULTIPLIER_LOW_VOL: float = 1.2    # Sakin piyasa — dar stop
ATR_MULTIPLIER_HIGH_VOL: float = 1.5   # Çalkantılı — geniş stop

# Take Profit çarpanları (R cinsinden)
TP1_R_MULTIPLE: float = 1.0   # 1R → %50 kapat
TP2_R_MULTIPLE: float = 2.0   # 2R → %35 kapat, trailing başlat
TP3_TRAILING_ATR_MULT: float = 0.8  # Trailing stop mesafesi


@dataclass(frozen=True, slots=True)
class StopLevels:
    """Hesaplanan stop ve TP seviyeleri."""

    stop_loss: float           # Stop-loss fiyatı
    tp1_price: float           # TP1 seviyesi (1R)
    tp2_price: float           # TP2 seviyesi (2R)
    tp3_trailing_atr: float    # Trailing stop mesafesi (ATR × çarpan)
    atr_multiplier: float      # Kullanılan ATR çarpanı (1.2 veya 1.5)
    atr_value: float           # Ham ATR değeri
    risk_distance: float       # Giriş ile stop arası mesafe (fiyat cinsinden)


class AdaptiveATRStop:
    """
    Volatilite rejimine göre dinamik ATR çarpanı hesaplar.

    ATR geçmişini sembol bazında biriktirerek medyanı takip eder.
    Her sinyal geldiğinde güncel ATR'yi medyanla karşılaştırır:
        - ATR < medyan → dar stop (çarpan=1.2), daha büyük lot
        - ATR ≥ medyan → geniş stop (çarpan=1.5), daha küçük lot
    """

    def __init__(self) -> None:
        # symbol → son N adet ATR değeri
        self._atr_history: dict[str, deque[float]] = defaultdict(
            lambda: deque(maxlen=ATR_LOOKBACK_PERIODS)
        )

    def record_atr(self, symbol: str, atr_value: float) -> None:
        """ATR geçmişine yeni değer ekle."""
        if atr_value > 0:
            self._atr_history[symbol].append(atr_value)

    def get_multiplier(self, symbol: str, current_atr: float) -> float:
        """
        Volatilite rejimine göre ATR çarpanını belirle.

        Yeterli geçmiş yoksa varsayılan olarak yüksek volatilite çarpanı
        kullanılır (muhafazakâr yaklaşım — daha geniş stop).
        """
        history = self._atr_history.get(symbol)

        if not history or len(history) < 10:
            # Yeterli veri yok — muhafazakâr ol
            logger.debug(
                "%s: Yetersiz ATR geçmişi (%d/%d), geniş stop kullanılıyor",
                symbol,
                len(history) if history else 0,
                ATR_LOOKBACK_PERIODS,
            )
            return ATR_MULTIPLIER_HIGH_VOL

        median_atr = statistics.median(history)

        if current_atr < median_atr:
            logger.debug(
                "%s: Düşük volatilite | ATR=%.4f < medyan=%.4f | çarpan=%.1f",
                symbol,
                current_atr,
                median_atr,
                ATR_MULTIPLIER_LOW_VOL,
            )
            return ATR_MULTIPLIER_LOW_VOL

        logger.debug(
            "%s: Yüksek volatilite | ATR=%.4f ≥ medyan=%.4f | çarpan=%.1f",
            symbol,
            current_atr,
            median_atr,
            ATR_MULTIPLIER_HIGH_VOL,
        )
        return ATR_MULTIPLIER_HIGH_VOL

    def calculate_levels(
        self,
        symbol: str,
        side: Side,
        entry_price: float,
        atr_value: float,
    ) -> Optional[StopLevels]:
        """
        Stop-loss ve Take Profit seviyelerini hesapla.

        Args:
            symbol: İşlem sembolü
            side: BUY veya SELL
            entry_price: Giriş fiyatı
            atr_value: SignalPacket'ten gelen ATR(14, 5m) değeri

        Dönüş: StopLevels veya None (ATR geçersizse)
        """
        if atr_value is None or atr_value <= 0:
            logger.warning(
                "%s: Geçersiz ATR değeri (%.4f), stop hesaplanamadı",
                symbol,
                atr_value if atr_value is not None else 0.0,
            )
            return None

        # ATR geçmişine ekle
        self.record_atr(symbol, atr_value)

        # Çarpan belirle
        multiplier = self.get_multiplier(symbol, atr_value)
        risk_distance = atr_value * multiplier

        if side == Side.BUY:
            stop_loss = entry_price - risk_distance
            tp1_price = entry_price + (risk_distance * TP1_R_MULTIPLE)
            tp2_price = entry_price + (risk_distance * TP2_R_MULTIPLE)
        else:  # SELL
            stop_loss = entry_price + risk_distance
            tp1_price = entry_price - (risk_distance * TP1_R_MULTIPLE)
            tp2_price = entry_price - (risk_distance * TP2_R_MULTIPLE)

        tp3_trailing = atr_value * TP3_TRAILING_ATR_MULT

        return StopLevels(
            stop_loss=stop_loss,
            tp1_price=tp1_price,
            tp2_price=tp2_price,
            tp3_trailing_atr=tp3_trailing,
            atr_multiplier=multiplier,
            atr_value=atr_value,
            risk_distance=risk_distance,
        )

    def get_median_atr(self, symbol: str) -> Optional[float]:
        """Debug/monitoring: Sembolün medyan ATR değeri."""
        history = self._atr_history.get(symbol)
        if not history or len(history) < 10:
            return None
        return statistics.median(history)
