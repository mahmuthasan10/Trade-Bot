"""
Risk Gatekeeper - Ana Orkestratör

channel:signals kanalından gelen SignalPacket'leri dinler ve
sırasıyla şu filtrelerden geçirir:

    1. Portfolio Risk Gate  → Net PnL bazlı lot/pozisyon limiti
    2. Spread Gate          → Anlık/60dk ortalama spread kontrolü
    3. Adaptif ATR Stop     → Volatilite rejimine göre stop/TP hesaplama
    4. Recovery Çarpanı     → Kill Switch sonrası lot yarıya düşer

Tüm filtrelerden geçen sinyal → ApprovedOrder → channel:approved_orders
Herhangi bir filtreye takılan  → RejectedOrder → channel:rejected_orders

Mimari Kuralı:
    - Bu servis ASLA borsaya emir göndermez
    - Sadece karar verir ve Redis'e onay/red yayınlar
    - Execution Engine channel:approved_orders'ı dinleyerek borsaya iletir
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Optional

import redis.asyncio as aioredis
from redis.exceptions import RedisError

from config.settings import settings
from shared.constants import redis_channels as ch
from shared.constants.enums import (
    Exchange,
    OrderType,
    RiskLevel,
    Side,
    Strategy,
    Timeframe,
)
from shared.utils.logger import get_logger

from services.risk_gatekeeper.models.order import ApprovedOrder, RejectedOrder
from services.risk_gatekeeper.portfolio_state import PortfolioStateManager
from services.risk_gatekeeper.rules.atr_stop import AdaptiveATRStop
from services.risk_gatekeeper.rules.portfolio_risk_gate import PortfolioRiskGate
from services.risk_gatekeeper.rules.spread_gate import SpreadGatekeeper

logger = get_logger("risk_gatekeeper.gatekeeper")

# Day Trading varsayılan risk parametreleri
BASE_RISK_PER_TRADE_PCT: float = 0.0030  # %0.30 — MASTER_BOT_v3 Bölüm 3.1

# Redis publish retry
INITIAL_BACKOFF_SEC: float = 1.0
MAX_BACKOFF_SEC: float = 15.0
BACKOFF_MULTIPLIER: float = 2.0
MAX_RETRIES: int = 5


class RiskGatekeeper:
    """
    Ana risk orkestratörü.

    Veri Akışı:
        channel:signals → [SignalPacket JSON]
                              ↓
                    ┌─ Portfolio Risk Gate ─┐
                    │  Net PnL + pozisyon   │
                    └──────────┬────────────┘
                              ↓
                    ┌─── Spread Gate ───────┐
                    │  anlık/60dk oran      │
                    └──────────┬────────────┘
                              ↓
                    ┌── Adaptif ATR Stop ───┐
                    │  çarpan + stop/TP     │
                    └──────────┬────────────┘
                              ↓
                    ┌── Recovery Çarpanı ───┐
                    │  lot × 0.50 (aktifse) │
                    └──────────┬────────────┘
                              ↓
                    channel:approved_orders
    """

    def __init__(self, symbols: list[str]) -> None:
        self._symbols = symbols

        # Alt modüller
        self._state = PortfolioStateManager()
        self._spread_gate = SpreadGatekeeper()
        self._portfolio_gate = PortfolioRiskGate()
        self._atr_stop = AdaptiveATRStop()

        # Redis bağlantıları
        self._sub_client: Optional[aioredis.Redis] = None
        self._pub_client: Optional[aioredis.Redis] = None
        self._pubsub: Optional[aioredis.client.PubSub] = None

        # İstatistikler
        self._signals_received: int = 0
        self._approved_count: int = 0
        self._rejected_count: int = 0
        self._running: bool = False

    async def start(self) -> None:
        """Tüm bağlantıları kur ve dinlemeye başla."""
        self._running = True

        # Redis bağlantıları
        self._sub_client = await self._create_redis_client()
        self._pub_client = await self._create_redis_client()

        # Alt modül bağlantıları
        await self._state.connect()
        await self._spread_gate.connect(self._symbols)

        # channel:signals'a abone ol
        self._pubsub = self._sub_client.pubsub()
        await self._pubsub.subscribe(ch.SIGNAL_CHANNEL)

        logger.info(
            "RiskGatekeeper başlatıldı | semboller=%s | "
            "risk_per_trade=%.2f%%",
            self._symbols,
            BASE_RISK_PER_TRADE_PCT * 100,
        )

        # Spread dinleme ve sinyal işleme paralel çalışır
        spread_task = asyncio.create_task(
            self._spread_gate.listen_spread_updates()
        )
        signal_task = asyncio.create_task(self._signal_listen_loop())

        try:
            await asyncio.gather(spread_task, signal_task)
        except asyncio.CancelledError:
            logger.info("RiskGatekeeper durduruldu (CancelledError)")

    async def _create_redis_client(self) -> aioredis.Redis:
        client = aioredis.Redis(
            host=settings.redis.host,
            port=settings.redis.port,
            password=settings.redis.password or None,
            db=settings.redis.db,
            decode_responses=True,
            socket_connect_timeout=5,
        )
        await client.ping()
        return client

    async def _signal_listen_loop(self) -> None:
        """channel:signals kanalını sürekli dinle ve her sinyali işle."""
        logger.info("Sinyal dinleme döngüsü başladı | kanal=%s", ch.SIGNAL_CHANNEL)

        while self._running:
            try:
                if self._pubsub is None:
                    break

                message = await self._pubsub.get_message(
                    ignore_subscribe_messages=True,
                    timeout=1.0,
                )
                if message is None or message["type"] != "message":
                    continue

                try:
                    signal_data: dict = json.loads(message["data"])
                except (json.JSONDecodeError, TypeError) as exc:
                    logger.warning("Geçersiz sinyal JSON | %s", exc)
                    continue

                self._signals_received += 1
                await self._process_signal(signal_data)

            except (RedisError, OSError) as exc:
                logger.error("Sinyal dinleme hatası | %s", exc)
                if self._running:
                    await asyncio.sleep(INITIAL_BACKOFF_SEC)

    async def _process_signal(self, signal: dict) -> None:
        """
        Tek bir sinyali tüm risk filtrelerinden geçir.

        Filtre Sırası:
            1. Portfolio Risk Gate (Net PnL + pozisyon limiti)
            2. Spread Gate (anlık/ortalama oranı)
            3. Adaptif ATR Stop (stop/TP hesaplama)
            4. Recovery lot çarpanı
        """
        symbol = signal.get("symbol", "UNKNOWN")
        exchange_str = signal.get("exchange", "binance")
        strategy_str = signal.get("strategy", "DAY_TRADING")
        side_str = signal.get("side", "BUY")
        unified_score = float(signal.get("unified_score", 0))
        entry_price = float(signal.get("entry_price", 0))
        atr_value = signal.get("atr")
        atr_value = float(atr_value) if atr_value is not None else 0.0

        # Enum dönüşümleri
        exchange = Exchange(exchange_str)
        strategy = Strategy(strategy_str)
        side = Side(side_str)
        timeframe = Timeframe(signal.get("timeframe", "5m"))

        logger.info(
            "Sinyal alındı [#%d] | %s %s %s | skor=%.1f | fiyat=%.2f",
            self._signals_received,
            symbol,
            side.value,
            strategy.value,
            unified_score,
            entry_price,
        )

        # ── FILTRE 1: Portfolio Risk Gate ──────────────────────────
        net_pnl = await self._state.get_net_pnl()
        risk_level = self._state.calculate_risk_level(net_pnl)
        open_positions = await self._state.get_open_position_count()

        risk_result = self._portfolio_gate.evaluate(risk_level, open_positions)

        if not risk_result.passed:
            await self._publish_rejection(
                symbol=symbol,
                exchange=exchange,
                strategy=strategy,
                side=side,
                unified_score=unified_score,
                entry_price=entry_price,
                reason="PORTFOLIO_RISK",
                detail=risk_result.detail,
                risk_level=risk_level,
            )

            # HARD KILL ise Recovery modunu tetikle
            if risk_level == RiskLevel.HARD_KILL:
                await self._state.activate_recovery()
                await self._publish_system_alert(
                    "HARD_KILL", f"Net PnL={net_pnl:.4f} | Sistem durduruldu"
                )
            return

        # Ek skor eşiği kontrolü
        min_threshold = int(signal.get("min_threshold", 0))
        effective_threshold = min_threshold + risk_result.extra_score_threshold
        if unified_score < effective_threshold:
            await self._publish_rejection(
                symbol=symbol,
                exchange=exchange,
                strategy=strategy,
                side=side,
                unified_score=unified_score,
                entry_price=entry_price,
                reason="SCORE_BELOW_THRESHOLD",
                detail=(
                    f"Skor {unified_score:.1f} < eşik {effective_threshold} "
                    f"(baz={min_threshold} + ek={risk_result.extra_score_threshold})"
                ),
                risk_level=risk_level,
            )
            return

        # ── FILTRE 2: Spread Gate ─────────────────────────────────
        spread_passed, spread_ratio, spread_detail = self._spread_gate.check(symbol)

        if not spread_passed:
            await self._publish_rejection(
                symbol=symbol,
                exchange=exchange,
                strategy=strategy,
                side=side,
                unified_score=unified_score,
                entry_price=entry_price,
                reason="SPREAD_GATE",
                detail=spread_detail,
                risk_level=risk_level,
                spread_ratio=spread_ratio,
            )
            return

        # ── FILTRE 3: Adaptif ATR Stop ────────────────────────────
        stop_levels = self._atr_stop.calculate_levels(
            symbol=symbol,
            side=side,
            entry_price=entry_price,
            atr_value=atr_value,
        )

        if stop_levels is None:
            await self._publish_rejection(
                symbol=symbol,
                exchange=exchange,
                strategy=strategy,
                side=side,
                unified_score=unified_score,
                entry_price=entry_price,
                reason="ATR_INVALID",
                detail=f"ATR hesaplanamadı (atr={atr_value})",
                risk_level=risk_level,
            )
            return

        # ── FILTRE 4: Recovery + Final Lot Hesaplama ──────────────
        recovery_mult = await self._state.get_recovery_lot_multiplier()
        final_lot_multiplier = (
            risk_result.lot_multiplier * recovery_mult
        )

        # Lot = 0 ise emir anlamsız
        if final_lot_multiplier <= 0:
            await self._publish_rejection(
                symbol=symbol,
                exchange=exchange,
                strategy=strategy,
                side=side,
                unified_score=unified_score,
                entry_price=entry_price,
                reason="LOT_ZERO",
                detail=(
                    f"Final lot çarpanı=0 | risk_mult={risk_result.lot_multiplier} "
                    f"× recovery_mult={recovery_mult}"
                ),
                risk_level=risk_level,
            )
            return

        # ── TÜM FİLTRELER GEÇTİ — ApprovedOrder oluştur ────────
        risk_pct = BASE_RISK_PER_TRADE_PCT * final_lot_multiplier
        lot_size = self._calculate_lot_size(
            entry_price=entry_price,
            stop_distance=stop_levels.risk_distance,
            risk_pct=risk_pct,
        )

        order = ApprovedOrder(
            symbol=symbol,
            exchange=exchange,
            strategy=strategy,
            side=side,
            timeframe=timeframe,
            entry_price=entry_price,
            unified_score=unified_score,
            lot_size=lot_size,
            lot_multiplier=final_lot_multiplier,
            stop_loss=stop_levels.stop_loss,
            atr_multiplier=stop_levels.atr_multiplier,
            atr_value=stop_levels.atr_value,
            tp1_price=stop_levels.tp1_price,
            tp2_price=stop_levels.tp2_price,
            tp3_trailing_atr=stop_levels.tp3_trailing_atr,
            risk_level=risk_level,
            risk_per_trade_pct=risk_pct,
        )

        await self._publish_approved(order)

    def _calculate_lot_size(
        self,
        entry_price: float,
        stop_distance: float,
        risk_pct: float,
    ) -> float:
        """
        Risk bazlı pozisyon büyüklüğü hesapla.

        Formül:
            lot = (portföy_değeri × risk_pct) / stop_mesafesi

        Şimdilik portföy değeri Redis'ten okunacak (placeholder=10000).
        Faz 4'te Execution Engine gerçek bakiye sağlar.
        """
        # TODO: Faz 4'te gerçek portföy bakiyesi Redis'ten alınacak
        portfolio_value = 10_000.0

        if stop_distance <= 0 or entry_price <= 0:
            return 0.0

        risk_amount = portfolio_value * risk_pct
        lot_size = risk_amount / stop_distance

        return round(lot_size, 8)

    # ─── Redis Publish ──────────────────────────────────────────────

    async def _safe_publish(self, channel: str, payload: str) -> None:
        """Retry mantıklı Redis publish."""
        backoff = INITIAL_BACKOFF_SEC

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                if self._pub_client is None:
                    self._pub_client = await self._create_redis_client()
                await self._pub_client.publish(channel, payload)
                return
            except (RedisError, OSError) as exc:
                if attempt == MAX_RETRIES:
                    logger.error(
                        "Redis publish başarısız (tüm denemeler) | "
                        "kanal=%s | %s",
                        channel,
                        exc,
                    )
                    raise
                logger.warning(
                    "Redis publish hatası (%d/%d) | %.1fs sonra tekrar | %s",
                    attempt,
                    MAX_RETRIES,
                    backoff,
                    exc,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * BACKOFF_MULTIPLIER, MAX_BACKOFF_SEC)

    async def _publish_approved(self, order: ApprovedOrder) -> None:
        """Onaylanan emri channel:approved_orders'a yayınla."""
        payload = json.dumps(order.to_dict())
        await self._safe_publish(ch.APPROVED_ORDERS, payload)
        self._approved_count += 1

        logger.info(
            "[ONAY] #%d | %s %s | lot=%.6f (x%.2f) | "
            "stop=%.2f | tp1=%.2f | tp2=%.2f | risk=%.3f%%",
            self._approved_count,
            order.symbol,
            order.side.value,
            order.lot_size,
            order.lot_multiplier,
            order.stop_loss,
            order.tp1_price,
            order.tp2_price,
            order.risk_per_trade_pct * 100,
        )

    async def _publish_rejection(
        self,
        symbol: str,
        exchange: Exchange,
        strategy: Strategy,
        side: Side,
        unified_score: float,
        entry_price: float,
        reason: str,
        detail: str,
        risk_level: Optional[RiskLevel] = None,
        spread_ratio: Optional[float] = None,
    ) -> None:
        """Reddedilen emri channel:rejected_orders'a yayınla."""
        rejection = RejectedOrder(
            symbol=symbol,
            exchange=exchange,
            strategy=strategy,
            side=side,
            unified_score=unified_score,
            entry_price=entry_price,
            rejection_reason=reason,
            rejection_detail=detail,
            risk_level=risk_level,
            spread_ratio=spread_ratio,
        )

        payload = json.dumps(rejection.to_dict())
        await self._safe_publish(ch.REJECTED_ORDERS, payload)
        self._rejected_count += 1

        logger.warning(
            "[RED] #%d | %s %s | sebep=%s | %s",
            self._rejected_count,
            symbol,
            side.value,
            reason,
            detail,
        )

    async def _publish_system_alert(self, alert_type: str, detail: str) -> None:
        """Sistem uyarısı yayınla (kill switch, recovery vb.)."""
        payload = json.dumps({
            "service": "risk_gatekeeper",
            "alert_type": alert_type,
            "detail": detail,
            "timestamp": time.time(),
        })
        await self._safe_publish(ch.SYSTEM_ALERTS, payload)
        logger.critical("SYSTEM ALERT: %s | %s", alert_type, detail)

    # ─── Kapatma ────────────────────────────────────────────────────

    async def stop(self) -> None:
        """Tüm bağlantıları temiz kapat."""
        self._running = False

        if self._pubsub is not None:
            try:
                await self._pubsub.unsubscribe()
                await self._pubsub.aclose()
            except Exception:
                pass
            self._pubsub = None

        await self._spread_gate.close()
        await self._state.close()

        for client in (self._sub_client, self._pub_client):
            if client is not None:
                try:
                    await client.aclose()
                except Exception:
                    pass
        self._sub_client = None
        self._pub_client = None

        logger.info(
            "RiskGatekeeper kapatıldı | sinyal=%d | onay=%d | red=%d",
            self._signals_received,
            self._approved_count,
            self._rejected_count,
        )

    # ─── Monitoring ─────────────────────────────────────────────────

    @property
    def signals_received(self) -> int:
        return self._signals_received

    @property
    def approved_count(self) -> int:
        return self._approved_count

    @property
    def rejected_count(self) -> int:
        return self._rejected_count
