"""
Strategy Engine - İndikatör Hesaplayıcı
Mum kapanışlarında teknik göstergeleri hesaplar.

Desteklenen indikatörler:
  - EMA (9, 21, 55), SMA (50, 200)
  - MACD (12, 26, 9)
  - ADX (14) + DI+/DI-
  - ATR (14)
  - RSI (14)
  - VWAP (mum bazlı)

pandas-ta kullanılır. CPU-bound hesaplamalar asyncio.to_thread ile
event loop'u bloklamadan çalıştırılır.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import Optional

import numpy as np
import pandas as pd
import pandas_ta as ta

from shared.constants.enums import Exchange, Timeframe
from shared.utils.logger import get_logger
from services.strategy_engine.models.candle import OHLCV, IndicatorResult

logger = get_logger("strategy_engine.indicators")

# İndikatör hesaplaması için minimum mum sayısı
# SMA(200) en uzun pencere → en az 200 mum gerekli ama
# başlangıçta daha az mumla da çalışabilmeli (kısmi sonuçlar)
MIN_CANDLES_FOR_FULL: int = 200
MIN_CANDLES_FOR_PARTIAL: int = 26  # MACD(26) minimum


class CandleHistory:
    """Bir (symbol, exchange, timeframe) için mum geçmişi tutar.

    Sabit boyutlu deque kullanarak bellek tüketimini sınırlar.
    """

    __slots__ = ("_candles", "_max_size")

    def __init__(self, max_size: int = 250) -> None:
        self._candles: deque[OHLCV] = deque(maxlen=max_size)
        self._max_size = max_size

    def append(self, candle: OHLCV) -> None:
        self._candles.append(candle)

    def to_dataframe(self) -> pd.DataFrame:
        """Mum listesini pandas DataFrame'e dönüştür."""
        if not self._candles:
            return pd.DataFrame()

        data = {
            "open": [c.open for c in self._candles],
            "high": [c.high for c in self._candles],
            "low": [c.low for c in self._candles],
            "close": [c.close for c in self._candles],
            "volume": [c.volume for c in self._candles],
            "vwap_candle": [c.vwap for c in self._candles],
        }
        return pd.DataFrame(data)

    @property
    def count(self) -> int:
        return len(self._candles)

    @property
    def last(self) -> Optional[OHLCV]:
        return self._candles[-1] if self._candles else None


def _compute_indicators(df: pd.DataFrame) -> dict:
    """CPU-bound indikatör hesaplaması (to_thread içinde çalışır).

    pandas-ta ile tüm indikatörleri bir seferde hesaplar.
    Yeterli veri yoksa None döner.
    """
    result: dict = {}
    n = len(df)

    # ── Moving Averages ─────────────────────────────────────────
    if n >= 9:
        ema9 = ta.ema(df["close"], length=9)
        result["ema_9"] = float(ema9.iloc[-1]) if ema9 is not None and not ema9.empty else None
    if n >= 21:
        ema21 = ta.ema(df["close"], length=21)
        result["ema_21"] = float(ema21.iloc[-1]) if ema21 is not None and not ema21.empty else None
    if n >= 55:
        ema55 = ta.ema(df["close"], length=55)
        result["ema_55"] = float(ema55.iloc[-1]) if ema55 is not None and not ema55.empty else None
    if n >= 50:
        sma50 = ta.sma(df["close"], length=50)
        result["sma_50"] = float(sma50.iloc[-1]) if sma50 is not None and not sma50.empty else None
    if n >= 200:
        sma200 = ta.sma(df["close"], length=200)
        result["sma_200"] = float(sma200.iloc[-1]) if sma200 is not None and not sma200.empty else None

    # ── MACD (12, 26, 9) ────────────────────────────────────────
    if n >= MIN_CANDLES_FOR_PARTIAL:
        macd_df = ta.macd(df["close"], fast=12, slow=26, signal=9)
        if macd_df is not None and not macd_df.empty:
            last = macd_df.iloc[-1]
            result["macd_line"] = _safe_float(last.get("MACD_12_26_9"))
            result["macd_signal"] = _safe_float(last.get("MACDs_12_26_9"))
            result["macd_histogram"] = _safe_float(last.get("MACDh_12_26_9"))

    # ── ADX (14) + DI+/DI- ─────────────────────────────────────
    if n >= 14:
        adx_df = ta.adx(df["high"], df["low"], df["close"], length=14)
        if adx_df is not None and not adx_df.empty:
            last = adx_df.iloc[-1]
            result["adx"] = _safe_float(last.get("ADX_14"))
            result["plus_di"] = _safe_float(last.get("DMP_14"))
            result["minus_di"] = _safe_float(last.get("DMN_14"))

    # ── ATR (14) ────────────────────────────────────────────────
    if n >= 14:
        atr = ta.atr(df["high"], df["low"], df["close"], length=14)
        if atr is not None and not atr.empty:
            result["atr"] = _safe_float(atr.iloc[-1])

    # ── RSI (14) ────────────────────────────────────────────────
    if n >= 14:
        rsi = ta.rsi(df["close"], length=14)
        if rsi is not None and not rsi.empty:
            result["rsi"] = _safe_float(rsi.iloc[-1])

    # ── VWAP (mum bazlı yaklaşım) ──────────────────────────────
    # Gerçek VWAP kümülatiftir; burada son mum'un candle-level VWAP'ı kullanılır
    result["vwap"] = float(df["vwap_candle"].iloc[-1])

    return result


def _safe_float(val) -> Optional[float]:
    """NaN/None güvenli float dönüşümü."""
    if val is None:
        return None
    try:
        f = float(val)
        return None if np.isnan(f) else round(f, 8)
    except (ValueError, TypeError):
        return None


class IndicatorCalculator:
    """
    Mum kapanışlarında indikatör hesaplayan modül.

    Her (symbol, exchange, timeframe) için mum geçmişi tutar.
    Yeni mum kapandığında:
      1. Geçmişe ekle
      2. pandas-ta ile hesapla (asyncio.to_thread — non-blocking)
      3. IndicatorResult döndür
    """

    def __init__(self) -> None:
        # Anahtar: (symbol, exchange_value, timeframe) → CandleHistory
        self._histories: dict[tuple[str, str, Timeframe], CandleHistory] = {}
        self._calc_count: int = 0

        logger.info("IndicatorCalculator başlatıldı")

    def _get_history(
        self, symbol: str, exchange: str, timeframe: Timeframe
    ) -> CandleHistory:
        """Mum geçmişini al veya oluştur."""
        key = (symbol, exchange, timeframe)
        if key not in self._histories:
            self._histories[key] = CandleHistory()
        return self._histories[key]

    async def on_candle_closed(self, candle: OHLCV) -> Optional[IndicatorResult]:
        """
        Mum kapandığında çağrılır.
        İndikatörleri hesaplar ve IndicatorResult döndürür.
        Yeterli veri yoksa kısmi sonuç döner.
        """
        exchange_str = candle.exchange.value
        history = self._get_history(candle.symbol, exchange_str, candle.timeframe)
        history.append(candle)

        if history.count < 2:
            logger.debug(
                "Yetersiz mum verisi | %s %s %s | count=%d",
                candle.symbol,
                exchange_str,
                candle.timeframe.value,
                history.count,
            )
            return None

        # DataFrame oluştur (hafif, ana thread)
        df = history.to_dataframe()

        # CPU-bound hesaplamayı thread pool'a gönder (event loop bloklanmaz)
        indicators = await asyncio.to_thread(_compute_indicators, df)

        self._calc_count += 1

        result = IndicatorResult(
            symbol=candle.symbol,
            exchange=candle.exchange,
            timeframe=candle.timeframe,
            timestamp=time.time(),
            ema_9=indicators.get("ema_9"),
            ema_21=indicators.get("ema_21"),
            ema_55=indicators.get("ema_55"),
            sma_50=indicators.get("sma_50"),
            sma_200=indicators.get("sma_200"),
            macd_line=indicators.get("macd_line"),
            macd_signal=indicators.get("macd_signal"),
            macd_histogram=indicators.get("macd_histogram"),
            adx=indicators.get("adx"),
            plus_di=indicators.get("plus_di"),
            minus_di=indicators.get("minus_di"),
            atr=indicators.get("atr"),
            rsi=indicators.get("rsi"),
            vwap=indicators.get("vwap"),
        )

        logger.info(
            "İndikatörler hesaplandı | %s %s %s | RSI=%.2f ADX=%.2f ATR=%.4f | mumlar=%d",
            candle.symbol,
            exchange_str,
            candle.timeframe.value,
            result.rsi or 0.0,
            result.adx or 0.0,
            result.atr or 0.0,
            history.count,
        )

        return result

    @property
    def calculation_count(self) -> int:
        return self._calc_count

    @property
    def tracked_pairs(self) -> int:
        return len(self._histories)
