"""
Execution Engine - Position Tracker / Manager

Açık pozisyonları bellekte takip eder. Her 5 saniyede bir arka plan döngüsü
(background task) ile şu kuralları işletir:

1. Kademeli Çıkış:
   - TP1 (+1R): %50 kapat, stop → breakeven
   - TP2 (+2R): %35 kapat, trailing stop başlat (ATR × 0.8)
   - TP3: Kalan %15'i trailing stop ile sür

2. Time-in-Trade:
   - Girişten 30 dakika (6 adet 5m mum) geçtiyse
   - VE fiyat ±0.3 ATR bandından çıkamamışsa
   - → Piyasa fiyatından (Market) kapat

Mimari Kuralı:
    - Bu sınıf kendi başına emir GÖNDERMEZ
    - OrderExecutor.close_position() metodunu kullanır
    - Kapanan işlemleri DbLogger'a bildirir
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Callable, Awaitable, Optional

import redis.asyncio as aioredis

from config.settings import settings
from shared.constants import redis_channels as ch
from shared.utils.logger import get_logger
from services.execution_engine.models.trade import (
    CloseReason,
    Position,
    PositionStatus,
    TradeRecord,
)

logger = get_logger("execution.position_manager")

# ── Sabitler ─────────────────────────────────────────────────────
TICK_INTERVAL_SEC: float = 5.0           # Pozisyon kontrol sıklığı
TIME_IN_TRADE_BARS: int = 6              # 6 adet 5m mum = 30 dakika
TIME_IN_TRADE_SEC: float = 30 * 60       # 30 dakika (saniye)
ATR_DRIFT_FACTOR: float = 0.3            # ±0.3 ATR bandı
TRAILING_ATR_MULTIPLIER: float = 0.8     # TP2 sonrası trailing mesafe

# TP kısmi kapanış oranları
TP1_CLOSE_RATIO: float = 0.50
TP2_CLOSE_RATIO: float = 0.35
TP3_CLOSE_RATIO: float = 0.15

# Tip tanımları
CloseCallback = Callable[[str, "Side", float, str], Awaitable[Optional[dict]]]
TradeCallback = Callable[[TradeRecord], Awaitable[None]]


class PositionManager:
    """
    Aktif pozisyonları bellekte tutar ve periyodik olarak TP/SL/Time-Exit kontrolü yapar.
    """

    def __init__(
        self,
        close_callback: Optional[CloseCallback] = None,
        trade_callback: Optional[TradeCallback] = None,
    ) -> None:
        """
        Args:
            close_callback: Pozisyon kapatma emri göndermek için
                           OrderExecutor.close_position referansı.
            trade_callback: Kapanış kaydı yazmak için
                           DbLogger.log_trade referansı.
        """
        self._positions: dict[str, Position] = {}   # order_id → Position
        self._close_callback = close_callback
        self._trade_callback = trade_callback
        self._running: bool = False
        self._redis: Optional[aioredis.Redis] = None

    @property
    def open_positions(self) -> dict[str, Position]:
        """Aktif pozisyon sözlüğünü döndür (salt okunur amaçlı)."""
        return self._positions

    @property
    def position_count(self) -> int:
        return len(self._positions)

    # ── Pozisyon Ekleme ──────────────────────────────────────────

    async def add_position(self, position: Position) -> None:
        """Yeni açılan pozisyonu takibe al."""
        self._positions[position.order_id] = position
        logger.info(
            "POZİSYON EKLENDİ | %s | %s %s | giriş=%.4f | SL=%.4f | TP1=%.4f | TP2=%.4f",
            position.order_id, position.side.value, position.symbol,
            position.entry_price, position.stop_loss,
            position.tp1_price, position.tp2_price,
        )
        await self._publish_position_update(position, "OPENED")

    # ── Arka Plan Döngüsü ───────────────────────────────────────

    async def start(self) -> None:
        """Periyodik pozisyon kontrol döngüsünü başlat."""
        self._running = True
        self._redis = aioredis.Redis(
            host=settings.redis.host,
            port=settings.redis.port,
            password=settings.redis.password or None,
            db=settings.redis.db,
            decode_responses=True,
        )
        logger.info("Position Manager başlatıldı | kontrol aralığı=%.1fs", TICK_INTERVAL_SEC)

        while self._running:
            try:
                await self._check_all_positions()
            except Exception as exc:
                logger.error("Pozisyon kontrol hatası: %s", exc)

            await asyncio.sleep(TICK_INTERVAL_SEC)

    async def stop(self) -> None:
        """Döngüyü durdur."""
        self._running = False
        if self._redis:
            await self._redis.aclose()
            self._redis = None
        logger.info("Position Manager durduruldu | açık pozisyon=%d", len(self._positions))

    # ── Fiyat Güncelleme ─────────────────────────────────────────

    async def update_price(self, symbol: str, current_price: float) -> None:
        """
        Dışarıdan gelen fiyat güncellemesi.
        Tick stream veya WebSocket'ten çağrılır.
        """
        for pos in self._positions.values():
            if pos.symbol == symbol and pos.status != PositionStatus.CLOSED:
                pos.last_price_update = time.time()

                # Trailing high/low güncelle
                if pos.status in (PositionStatus.TP2_HIT, PositionStatus.TRAILING):
                    if pos.is_long:
                        if pos.trailing_high is None or current_price > pos.trailing_high:
                            pos.trailing_high = current_price
                            pos.trailing_stop_price = current_price - pos.tp3_trailing_atr
                    else:
                        if pos.trailing_high is None or current_price < pos.trailing_high:
                            pos.trailing_high = current_price
                            pos.trailing_stop_price = current_price + pos.tp3_trailing_atr

    # ── Pozisyon Kontrol Mantığı ─────────────────────────────────

    async def _check_all_positions(self) -> None:
        """Tüm açık pozisyonları kontrol et."""
        closed_ids: list[str] = []

        for order_id, pos in list(self._positions.items()):
            if pos.status == PositionStatus.CLOSED:
                closed_ids.append(order_id)
                continue

            # Mevcut fiyatı al (Redis state veya son güncelleme)
            current_price = await self._get_current_price(pos.symbol)
            if current_price is None:
                continue

            # 1. Stop Loss kontrolü
            if self._check_stop_loss(pos, current_price):
                await self._close_partial(
                    pos, pos.remaining_quantity, current_price, CloseReason.STOP_LOSS,
                )
                closed_ids.append(order_id)
                continue

            # 2. Trailing Stop kontrolü (TP2 sonrası)
            if pos.status in (PositionStatus.TP2_HIT, PositionStatus.TRAILING):
                if self._check_trailing_stop(pos, current_price):
                    await self._close_partial(
                        pos, pos.remaining_quantity, current_price, CloseReason.TP3_TRAILING,
                    )
                    closed_ids.append(order_id)
                    continue

            # 3. TP1 kontrolü (%50 kapat)
            if pos.status == PositionStatus.OPEN:
                if self._check_tp1(pos, current_price):
                    qty = pos.tp1_quantity
                    await self._close_partial(pos, qty, current_price, CloseReason.TP1)
                    pos.remaining_quantity -= qty
                    pos.status = PositionStatus.TP1_HIT
                    # Stop → Breakeven (giriş fiyatı)
                    pos.stop_loss = pos.entry_price
                    logger.info("TP1 tetiklendi | %s | stop → breakeven", order_id)

            # 4. TP2 kontrolü (%35 kapat + trailing başlat)
            if pos.status == PositionStatus.TP1_HIT:
                if self._check_tp2(pos, current_price):
                    qty = pos.tp2_quantity
                    await self._close_partial(pos, qty, current_price, CloseReason.TP2)
                    pos.remaining_quantity -= qty
                    pos.status = PositionStatus.TP2_HIT
                    # Trailing stop başlat
                    pos.trailing_high = current_price
                    if pos.is_long:
                        pos.trailing_stop_price = current_price - pos.tp3_trailing_atr
                    else:
                        pos.trailing_stop_price = current_price + pos.tp3_trailing_atr
                    logger.info(
                        "TP2 tetiklendi | %s | trailing başladı | mesafe=%.4f",
                        order_id, pos.tp3_trailing_atr,
                    )

            # 5. Time-in-Trade kontrolü (30dk hareketsizlik)
            if pos.status in (PositionStatus.OPEN, PositionStatus.TP1_HIT):
                if self._check_time_exit(pos, current_price):
                    await self._close_partial(
                        pos, pos.remaining_quantity, current_price, CloseReason.TIME_EXIT,
                    )
                    closed_ids.append(order_id)

        # Kapanan pozisyonları temizle
        for oid in closed_ids:
            if oid in self._positions:
                self._positions[oid].status = PositionStatus.CLOSED
                del self._positions[oid]

    # ── TP / SL / Time Kontrolleri ───────────────────────────────

    def _check_stop_loss(self, pos: Position, price: float) -> bool:
        """Stop-loss tetiklendi mi?"""
        if pos.is_long:
            return price <= pos.stop_loss
        return price >= pos.stop_loss

    def _check_tp1(self, pos: Position, price: float) -> bool:
        """TP1 seviyesine ulaşıldı mı?"""
        if pos.is_long:
            return price >= pos.tp1_price
        return price <= pos.tp1_price

    def _check_tp2(self, pos: Position, price: float) -> bool:
        """TP2 seviyesine ulaşıldı mı?"""
        if pos.is_long:
            return price >= pos.tp2_price
        return price <= pos.tp2_price

    def _check_trailing_stop(self, pos: Position, price: float) -> bool:
        """Trailing stop tetiklendi mi?"""
        if pos.trailing_stop_price is None:
            return False
        if pos.is_long:
            return price <= pos.trailing_stop_price
        return price >= pos.trailing_stop_price

    def _check_time_exit(self, pos: Position, price: float) -> bool:
        """
        Time-in-Trade: 30dk geçti ve fiyat ±0.3 ATR bandından çıkamadıysa kapat.
        """
        elapsed = time.time() - pos.entry_time
        if elapsed < TIME_IN_TRADE_SEC:
            return False

        drift = abs(price - pos.entry_price)
        band = pos.atr_value * ATR_DRIFT_FACTOR

        if drift < band:
            logger.info(
                "TIME EXIT | %s | süre=%.0fs | drift=%.4f < band=%.4f",
                pos.order_id, elapsed, drift, band,
            )
            return True
        return False

    # ── Kısmi Kapanış İşlemi ────────────────────────────────────

    async def _close_partial(
        self,
        pos: Position,
        quantity: float,
        exit_price: float,
        reason: CloseReason,
    ) -> None:
        """
        Kısmi veya tam kapanış yap.
        1. OrderExecutor üzerinden kapanış emri gönder
        2. TradeRecord oluştur ve DbLogger'a bildir
        3. Redis'e pozisyon güncellemesi yayınla
        """
        # PnL hesapla
        if pos.is_long:
            pnl = (exit_price - pos.entry_price) * quantity
        else:
            pnl = (pos.entry_price - exit_price) * quantity

        pnl_pct = ((exit_price - pos.entry_price) / pos.entry_price) * 100
        if not pos.is_long:
            pnl_pct = -pnl_pct

        logger.info(
            "KAPANIŞ | %s | %s | miktar=%.6f | giriş=%.4f → çıkış=%.4f | PnL=%.4f (%.2f%%) | neden=%s",
            pos.order_id, pos.symbol, quantity,
            pos.entry_price, exit_price, pnl, pnl_pct, reason.value,
        )

        # 1. Borsaya kapanış emri gönder
        if self._close_callback:
            await self._close_callback(pos.symbol, pos.side, quantity, reason.value)

        # 2. Realized PnL güncelle
        pos.realized_pnl += pnl

        # 3. TradeRecord oluştur ve veritabanına bildir
        record = TradeRecord(
            order_id=pos.order_id,
            symbol=pos.symbol,
            exchange=pos.exchange.value,
            strategy=pos.strategy if isinstance(pos.strategy, str) else pos.strategy,
            side=pos.side.value,
            entry_price=pos.entry_price,
            exit_price=exit_price,
            quantity=quantity,
            pnl=pnl,
            pnl_pct=pnl_pct,
            close_reason=reason.value,
            entry_time=pos.entry_time,
            exit_time=time.time(),
            atr_value=pos.atr_value,
            stop_loss=pos.stop_loss,
            tp1_price=pos.tp1_price,
            tp2_price=pos.tp2_price,
        )

        if self._trade_callback:
            await self._trade_callback(record)

        # 4. Redis'e PnL güncellemesi
        await self._publish_fill(pos, pnl, reason)
        await self._publish_position_update(pos, reason.value)

    # ── Redis Yayınları ──────────────────────────────────────────

    async def _publish_fill(self, pos: Position, pnl: float, reason: CloseReason) -> None:
        """channel:fills kanalına PnL bilgisi gönder (Risk Gatekeeper dinler)."""
        if not self._redis:
            return
        try:
            payload = json.dumps({
                "order_id": pos.order_id,
                "symbol": pos.symbol,
                "pnl": pnl,
                "pnl_pct": (pnl / (pos.entry_price * pos.total_quantity)) * 100
                if pos.entry_price > 0 else 0.0,
                "close_reason": reason.value,
                "timestamp": time.time(),
            })
            await self._redis.publish(ch.FILL_CHANNEL, payload)
        except Exception as exc:
            logger.error("Fill publish hatası: %s", exc)

    async def _publish_position_update(self, pos: Position, event: str) -> None:
        """channel:positions kanalına pozisyon durumu gönder."""
        if not self._redis:
            return
        try:
            payload = json.dumps({
                "order_id": pos.order_id,
                "symbol": pos.symbol,
                "status": pos.status.value,
                "event": event,
                "remaining_quantity": pos.remaining_quantity,
                "realized_pnl": pos.realized_pnl,
                "timestamp": time.time(),
            })
            await self._redis.publish(ch.POSITION_UPDATE, payload)
        except Exception as exc:
            logger.error("Position update publish hatası: %s", exc)

    # ── Fiyat Sorgulama ──────────────────────────────────────────

    async def _get_current_price(self, symbol: str) -> Optional[float]:
        """
        Redis'teki son tick fiyatını oku.
        TODO: Daha sonra WebSocket stream'den direkt beslenecek.
        """
        if not self._redis:
            return None
        try:
            channel = ch.TICK_STREAM.format(symbol=symbol)
            # Son bilinen fiyatı state'ten oku
            price_str = await self._redis.hget(f"price:{symbol}", "last_price")
            if price_str:
                return float(price_str)
        except Exception:
            pass
        return None
