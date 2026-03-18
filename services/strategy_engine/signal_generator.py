"""
Strategy Engine - Unified Sinyal Motoru (Signal Generator)

MASTER_BOT_v3.md referansları:
  - 3.2: Yön Belirleme (15m) → EMA yapısı, VWAP, ADX
  - 3.3: Giriş Sinyalleri (5m) → 12 puanlık sistem
  - 6.1: Unified Sinyal Skoru → 0-100 arası, strateji bazlı ağırlık/eşik

Mimari Kuralı:
  - SADECE sinyal üretir ve Redis'e fırlatır
  - Risk kontrolü, lot büyüklüğü, kasa yönetimi YAPMAZ (Faz 3 - Risk Gatekeeper)
  - Borsaya ASLA doğrudan bağlanmaz
"""

from __future__ import annotations

from collections import deque
from typing import Optional

from shared.constants.enums import Exchange, Side, Strategy, Timeframe
from shared.utils.logger import get_logger
from services.strategy_engine.models.candle import (
    OHLCV,
    IndicatorResult,
    SignalPacket,
)

logger = get_logger("strategy_engine.signal_generator")

# ── Day Trading 5m Puan Sistemi (MASTER_BOT 3.3) ────────────────
# Her bileşen bağımsız olarak 0-2 arası puan üretir.
# Toplam: 0-12 puan. Minimum 4 puan = işlem sinyali.
MIN_POINTS_DAY_TRADING: int = 4
MAX_POINTS: int = 12

# ── Unified Skor eşikleri (MASTER_BOT 6.1) ──────────────────────
# Day Trading / Fırsat için:
#   > 70 → AL (Momentum eşiği, 6p)
#   65-70 → AL (Agresif eşiği, 5p)
#   58-65 → AL (Normal/Serbest, 4p)
#   45-58 → Bekle
#   < 35 → SAT / Kapat
UNIFIED_THRESHOLD_STRONG: float = 70.0
UNIFIED_THRESHOLD_MODERATE: float = 65.0
UNIFIED_THRESHOLD_MINIMUM: float = 58.0
UNIFIED_THRESHOLD_SELL: float = 35.0

# ── Ağırlıklar (Day Trading / Fırsat — MASTER_BOT 6.1) ──────────
# Teknik Analiz: %55, Piyasaya Özgü: %25, Makro: %10, Rejim: %10
# Şu an sadece teknik analiz verisi var, diğer katmanlar NOP.
# Teknik katman 100 üzerinden normalize edilir.
WEIGHT_TECHNICAL: float = 0.55
WEIGHT_MARKET_SPECIFIC: float = 0.25
WEIGHT_MACRO: float = 0.10
WEIGHT_REGIME: float = 0.10


class _IndicatorHistory:
    """Son N indikatör sonucunu tutar — kesişim ve ivme tespiti için."""

    __slots__ = ("_results",)

    def __init__(self, max_size: int = 5) -> None:
        self._results: deque[IndicatorResult] = deque(maxlen=max_size)

    def append(self, result: IndicatorResult) -> None:
        self._results.append(result)

    @property
    def current(self) -> Optional[IndicatorResult]:
        return self._results[-1] if self._results else None

    @property
    def previous(self) -> Optional[IndicatorResult]:
        return self._results[-2] if len(self._results) >= 2 else None

    @property
    def count(self) -> int:
        return len(self._results)


class SignalGenerator:
    """
    Day Trading 5m puan sistemi + Unified 0-100 sinyal skoru.

    İndikatör sonuçlarını alır, bileşen bazlı puanlama yapar,
    eşik aşılırsa SignalPacket üretir.

    Pipeline:
        IndicatorResult → score_components() → check_threshold() → SignalPacket
    """

    def __init__(self, strategy: Strategy = Strategy.DAY_TRADING) -> None:
        self._strategy = strategy

        # (symbol, exchange, timeframe) → _IndicatorHistory
        self._histories: dict[tuple[str, str, Timeframe], _IndicatorHistory] = {}
        self._signal_count: int = 0

        logger.info("SignalGenerator başlatıldı | strateji=%s", strategy.value)

    def _get_history(
        self, symbol: str, exchange: str, timeframe: Timeframe
    ) -> _IndicatorHistory:
        key = (symbol, exchange, timeframe)
        if key not in self._histories:
            self._histories[key] = _IndicatorHistory()
        return self._histories[key]

    async def evaluate(
        self,
        candle: OHLCV,
        indicators: IndicatorResult,
    ) -> Optional[SignalPacket]:
        """
        İndikatör sonucunu değerlendir, gerekirse sinyal üret.

        Returns:
            SignalPacket eşik aşılırsa, None aksi halde.
        """
        exchange_str = indicators.exchange.value
        history = self._get_history(
            indicators.symbol, exchange_str, indicators.timeframe
        )
        history.append(indicators)

        # Kesişim tespiti için en az 2 periyod gerekli
        if history.count < 2:
            return None

        current = history.current
        previous = history.previous
        if current is None or previous is None:
            return None

        # ── Bileşen Puanlama (Day Trading 5m — MASTER_BOT 3.3) ──
        components: dict[str, int] = {}
        total_points: int = 0

        # 1) Momentum — EMA9/21 kesişim (2 puan)
        momentum_pts = self._score_momentum(current, previous)
        components["momentum"] = momentum_pts
        total_points += momentum_pts

        # 2) VWAP konumu (2 puan)
        vwap_pts = self._score_vwap(candle, current)
        components["vwap"] = vwap_pts
        total_points += vwap_pts

        # 3) RSI momentum (1 puan)
        rsi_pts = self._score_rsi(current, previous)
        components["rsi"] = rsi_pts
        total_points += rsi_pts

        # 4) MACD histogram yön değişimi (2 puan)
        macd_pts = self._score_macd(current, previous)
        components["macd"] = macd_pts
        total_points += macd_pts

        # 5) ADX trend gücü (2 puan)
        adx_pts = self._score_adx(current)
        components["adx"] = adx_pts
        total_points += adx_pts

        # 6) DI+/DI- kesişim (1 puan)
        di_pts = self._score_di_cross(current, previous)
        components["di_cross"] = di_pts
        total_points += di_pts

        # ── Yön Belirleme ────────────────────────────────────────
        side = self._determine_side(current, previous, candle, components)
        if side is None:
            return None  # Net yön belirlenemedi

        # ── Unified Skor (0-100) ─────────────────────────────────
        # Teknik katman: ham puanı 0-100'e normalize et
        technical_score = (total_points / MAX_POINTS) * 100.0

        # Diğer katmanlar şimdilik nötr (%50 varsayılan)
        # Faz ilerledikçe on-chain, makro, rejim verileri eklenecek
        market_specific_score: float = 50.0
        macro_score: float = 50.0
        regime_score: float = 50.0

        unified_score = (
            technical_score * WEIGHT_TECHNICAL
            + market_specific_score * WEIGHT_MARKET_SPECIFIC
            + macro_score * WEIGHT_MACRO
            + regime_score * WEIGHT_REGIME
        )

        # ── Eşik Kontrolü ────────────────────────────────────────
        min_threshold = MIN_POINTS_DAY_TRADING

        passes_point_threshold = total_points >= min_threshold
        passes_unified_threshold = unified_score >= UNIFIED_THRESHOLD_MINIMUM

        # SAT sinyali: skor < 35 ve açık long varsa
        is_sell_signal = (
            side == Side.SELL and unified_score < UNIFIED_THRESHOLD_SELL
        )

        if not passes_point_threshold and not is_sell_signal:
            logger.debug(
                "Sinyal eşik altı | %s %s | puan=%d/%d | unified=%.1f | bileşenler=%s",
                indicators.symbol,
                indicators.timeframe.value,
                total_points,
                min_threshold,
                unified_score,
                components,
            )
            return None

        if not passes_unified_threshold and not is_sell_signal:
            logger.debug(
                "Unified skor eşik altı | %s | puan=%d | unified=%.1f < %.1f",
                indicators.symbol,
                total_points,
                unified_score,
                UNIFIED_THRESHOLD_MINIMUM,
            )
            return None

        # ── Sinyal Üret ──────────────────────────────────────────
        self._signal_count += 1

        packet = SignalPacket(
            symbol=indicators.symbol,
            exchange=indicators.exchange,
            strategy=self._strategy,
            side=side,
            timeframe=indicators.timeframe,
            raw_points=total_points,
            unified_score=round(unified_score, 2),
            min_threshold=min_threshold,
            entry_price=candle.close,
            atr=indicators.atr,
            components=components,
        )

        logger.info(
            "SİNYAL ÜRETİLDİ | %s %s %s | yön=%s | puan=%d | unified=%.1f | "
            "fiyat=%.2f | ATR=%s | bileşenler=%s",
            packet.symbol,
            packet.strategy.value,
            packet.timeframe.value,
            packet.side.value,
            packet.raw_points,
            packet.unified_score,
            packet.entry_price,
            f"{packet.atr:.4f}" if packet.atr else "-",
            components,
        )

        return packet

    # ── Bileşen Skorlama Fonksiyonları ───────────────────────────

    def _score_momentum(
        self, current: IndicatorResult, previous: IndicatorResult
    ) -> int:
        """Momentum — EMA9/EMA21 kesişim (MASTER_BOT 3.3: 2 puan).

        Yukarı kesişim (golden cross): EMA9 önceden EMA21 altında,
        şimdi üstüne geçti → Long momentum.
        Aşağı kesişim: tersi → Short momentum.
        """
        if (
            current.ema_9 is None or current.ema_21 is None
            or previous.ema_9 is None or previous.ema_21 is None
        ):
            return 0

        # Yukarı kesişim: önceki EMA9 <= EMA21, şimdiki EMA9 > EMA21
        bullish_cross = previous.ema_9 <= previous.ema_21 and current.ema_9 > current.ema_21
        # Aşağı kesişim: önceki EMA9 >= EMA21, şimdiki EMA9 < EMA21
        bearish_cross = previous.ema_9 >= previous.ema_21 and current.ema_9 < current.ema_21

        if bullish_cross or bearish_cross:
            return 2

        # Kesişim olmadan ama güçlü ayrışma (devam sinyali olarak 1 puan)
        spread_pct = abs(current.ema_9 - current.ema_21) / current.ema_21 * 100
        if spread_pct > 0.1:
            return 1

        return 0

    def _score_vwap(self, candle: OHLCV, current: IndicatorResult) -> int:
        """VWAP konumu (MASTER_BOT 3.3: 2 puan).

        Fiyat VWAP üstünde ve yaklaşım + fırlama → Long.
        Fiyat VWAP altında ve yaklaşım + kırılma → Short.
        """
        if current.vwap is None or current.vwap == 0:
            return 0

        vwap_dist_pct = (candle.close - current.vwap) / current.vwap * 100

        # ±%0.1 bant içi yaklaşım + yön
        if abs(vwap_dist_pct) < 0.1:
            # VWAP bandı içinde — yakın temas, yöne göre 2 puan
            return 2
        elif 0.1 <= vwap_dist_pct <= 0.5:
            # Fiyat VWAP üstünde, makul mesafe → Long desteği
            return 1
        elif -0.5 <= vwap_dist_pct <= -0.1:
            # Fiyat VWAP altında → Short desteği
            return 1

        return 0

    def _score_rsi(
        self, current: IndicatorResult, previous: IndicatorResult
    ) -> int:
        """RSI momentum (MASTER_BOT 3.3: 1 puan).

        RSI > 50 + yukarı ivme → Long.
        RSI < 50 + aşağı ivme → Short.
        """
        if current.rsi is None or previous.rsi is None:
            return 0

        # Yukarı ivme: RSI yükseliyor ve 50 üstünde
        if current.rsi > 50 and current.rsi > previous.rsi:
            return 1
        # Aşağı ivme: RSI düşüyor ve 50 altında
        if current.rsi < 50 and current.rsi < previous.rsi:
            return 1

        return 0

    def _score_macd(
        self, current: IndicatorResult, previous: IndicatorResult
    ) -> int:
        """MACD histogram yön değişimi (2 puan).

        Histogram negatiften pozitife → bullish momentum.
        Histogram pozitiften negatife → bearish momentum.
        """
        if current.macd_histogram is None or previous.macd_histogram is None:
            return 0

        # Negatif → pozitif geçiş
        if previous.macd_histogram < 0 and current.macd_histogram > 0:
            return 2
        # Pozitif → negatif geçiş
        if previous.macd_histogram > 0 and current.macd_histogram < 0:
            return 2
        # Aynı yönde güçlenme (1 puan)
        if (
            current.macd_histogram > 0
            and current.macd_histogram > previous.macd_histogram
        ):
            return 1
        if (
            current.macd_histogram < 0
            and current.macd_histogram < previous.macd_histogram
        ):
            return 1

        return 0

    def _score_adx(self, current: IndicatorResult) -> int:
        """ADX trend gücü (MASTER_BOT 3.2/3.3: 2 puan).

        ADX > 25 → güçlü trend (2 puan).
        ADX 20-25 → orta trend (1 puan).
        ADX < 20 → trend yok, pencere kapanır (0 puan).
        """
        if current.adx is None:
            return 0

        if current.adx >= 25:
            return 2
        if current.adx >= 20:
            return 1
        return 0

    def _score_di_cross(
        self, current: IndicatorResult, previous: IndicatorResult
    ) -> int:
        """DI+/DI- kesişim (1 puan).

        DI+ üstte → bullish trend.
        DI- üstte → bearish trend.
        Kesişim anı ekstra sinyal.
        """
        if (
            current.plus_di is None or current.minus_di is None
            or previous.plus_di is None or previous.minus_di is None
        ):
            return 0

        # DI+ aşağıdan yukarı DI- yi geçti
        bullish = (
            previous.plus_di <= previous.minus_di
            and current.plus_di > current.minus_di
        )
        # DI- aşağıdan yukarı DI+ yı geçti
        bearish = (
            previous.minus_di <= previous.plus_di
            and current.minus_di > current.plus_di
        )

        if bullish or bearish:
            return 1
        return 0

    # ── Yön Belirleme ────────────────────────────────────────────

    def _determine_side(
        self,
        current: IndicatorResult,
        previous: IndicatorResult,
        candle: OHLCV,
        components: dict[str, int],
    ) -> Optional[Side]:
        """Bileşen skorlarından net yön belirle.

        Strateji: bullish ve bearish bileşenlerin ağırlıklı oylaması.
        Net fark yoksa → None (sinyal üretme).
        """
        bullish_votes: int = 0
        bearish_votes: int = 0

        # Momentum yönü
        if (
            current.ema_9 is not None and current.ema_21 is not None
            and previous.ema_9 is not None and previous.ema_21 is not None
        ):
            if current.ema_9 > current.ema_21:
                bullish_votes += components.get("momentum", 0)
            elif current.ema_9 < current.ema_21:
                bearish_votes += components.get("momentum", 0)

        # VWAP yönü
        if current.vwap is not None and current.vwap > 0:
            if candle.close > current.vwap:
                bullish_votes += components.get("vwap", 0)
            elif candle.close < current.vwap:
                bearish_votes += components.get("vwap", 0)

        # RSI yönü
        if current.rsi is not None:
            if current.rsi > 50:
                bullish_votes += components.get("rsi", 0)
            elif current.rsi < 50:
                bearish_votes += components.get("rsi", 0)

        # MACD yönü
        if current.macd_histogram is not None:
            if current.macd_histogram > 0:
                bullish_votes += components.get("macd", 0)
            elif current.macd_histogram < 0:
                bearish_votes += components.get("macd", 0)

        # DI yönü
        if current.plus_di is not None and current.minus_di is not None:
            if current.plus_di > current.minus_di:
                bullish_votes += components.get("di_cross", 0)
            elif current.minus_di > current.plus_di:
                bearish_votes += components.get("di_cross", 0)

        # ADX yönsüzdür ama trend gücünü destekler
        # → kazanan tarafa eklenir (aşağıda)

        # Net yön: en az 2 puan fark olmalı (gürültü filtresi)
        diff = bullish_votes - bearish_votes
        if abs(diff) < 2:
            return None

        side = Side.BUY if diff > 0 else Side.SELL

        # ADX bonus: trend güçlüyse kazanana ekle
        adx_pts = components.get("adx", 0)
        if adx_pts > 0:
            if side == Side.BUY:
                bullish_votes += adx_pts
            else:
                bearish_votes += adx_pts

        return side

    @property
    def signal_count(self) -> int:
        return self._signal_count
