"""
Risk Gatekeeper - Portfolio State Manager

Redis Hash üzerinde Kümülatif Net PnL ve Recovery modunu takip eder.

Redis Key'leri (shared/constants/redis_channels.py):
    state:portfolio  → {net_pnl, risk_level, open_positions, last_update}
    state:recovery   → {active, consecutive_wins, triggered_at, cleared_at}

Mimari Kuralı:
    - Bu modül ASLA borsaya emir göndermez
    - Sadece Redis Hash okur/yazar
    - Execution Engine'den gelen fill bilgileriyle güncellenir
"""

from __future__ import annotations

import time
from typing import Optional

import redis.asyncio as aioredis
from redis.exceptions import RedisError

from config.settings import settings
from shared.constants import redis_channels as ch
from shared.constants.enums import RiskLevel
from shared.utils.logger import get_logger

logger = get_logger("risk_gatekeeper.portfolio_state")

# Recovery Modu: Kill Switch sonrası 3 ardışık kâr gerektirir
RECOVERY_CONSECUTIVE_WINS_REQUIRED: int = 3

# Lot çarpanı: Recovery modunda baz lot'un yarısı kullanılır
RECOVERY_LOT_MULTIPLIER: float = 0.50


class PortfolioStateManager:
    """
    Kümülatif Net PnL durumunu ve Recovery modunu Redis üzerinde yönetir.

    PnL Güncellemesi:
        Execution Engine gerçekleşen işlemleri (fills) channel:fills üzerinden
        yayınlar → Bu modül dinler ve state:portfolio'yu günceller.

    Risk Seviyesi Hesaplama:
        Net PnL kaybına göre RiskLevel (NORMAL → HARD_KILL) belirlenir.
    """

    def __init__(self) -> None:
        self._client: Optional[aioredis.Redis] = None

    async def connect(self) -> None:
        """Redis bağlantısını kur."""
        self._client = aioredis.Redis(
            host=settings.redis.host,
            port=settings.redis.port,
            password=settings.redis.password or None,
            db=settings.redis.db,
            decode_responses=True,
            socket_connect_timeout=5,
        )
        await self._client.ping()
        logger.info("PortfolioStateManager Redis'e bağlandı")

    async def _ensure_client(self) -> aioredis.Redis:
        if self._client is None:
            await self.connect()
        assert self._client is not None
        return self._client

    # ─── Kümülatif Net PnL ──────────────────────────────────────────

    async def get_net_pnl(self) -> float:
        """
        Kümülatif Net PnL değerini oku.
        Dönüş: Oran olarak kayıp (ör: -0.04 = -%4).
        Henüz set edilmemişse 0.0 döner.
        """
        client = await self._ensure_client()
        try:
            value = await client.hget(ch.PORTFOLIO_STATE, "net_pnl")
            return float(value) if value is not None else 0.0
        except (RedisError, ValueError) as exc:
            logger.error("Net PnL okunamadı | %s", exc)
            return 0.0

    async def update_net_pnl(self, pnl_change: float) -> float:
        """
        Net PnL'yi güncelle (artı veya eksi yönde).
        Dönüş: Güncel kümülatif Net PnL.
        """
        client = await self._ensure_client()
        try:
            new_pnl = await client.hincrbyfloat(
                ch.PORTFOLIO_STATE, "net_pnl", pnl_change
            )
            await client.hset(
                ch.PORTFOLIO_STATE, "last_update", str(time.time())
            )
            logger.info(
                "Net PnL güncellendi | değişim=%.4f | yeni=%.4f",
                pnl_change,
                new_pnl,
            )
            return float(new_pnl)
        except RedisError as exc:
            logger.error("Net PnL güncellenemedi | %s", exc)
            raise

    async def set_net_pnl(self, value: float) -> None:
        """Net PnL'yi doğrudan ayarla (reset veya başlangıç için)."""
        client = await self._ensure_client()
        await client.hset(ch.PORTFOLIO_STATE, mapping={
            "net_pnl": str(value),
            "last_update": str(time.time()),
        })

    async def get_open_position_count(self) -> int:
        """Açık pozisyon sayısını oku."""
        client = await self._ensure_client()
        try:
            value = await client.hget(ch.PORTFOLIO_STATE, "open_positions")
            return int(value) if value is not None else 0
        except (RedisError, ValueError):
            return 0

    async def set_open_position_count(self, count: int) -> None:
        """Açık pozisyon sayısını güncelle."""
        client = await self._ensure_client()
        await client.hset(ch.PORTFOLIO_STATE, "open_positions", str(count))

    # ─── Risk Seviyesi ──────────────────────────────────────────────

    def calculate_risk_level(self, net_pnl: float) -> RiskLevel:
        """
        Kümülatif Net PnL kaybına göre risk seviyesi belirle.

        MASTER_BOT_v3.md Bölüm 5.1:
            0 – %2.5 kayıp  → NORMAL
            %2.5 – %5       → CAUTION  (lot -%20, +1 skor eşiği)
            %5 – %8         → WARNING  (lot -%40, max 3 pozisyon)
            %8 – %10        → EMERGENCY (donduruldu)
            >%10            → HARD_KILL (sistem kapanır)

        Args:
            net_pnl: Oran olarak PnL (negatif = kayıp, ör: -0.04 = -%4)
        """
        loss = abs(min(net_pnl, 0.0))

        if loss >= 0.10:
            return RiskLevel.HARD_KILL
        if loss >= 0.08:
            return RiskLevel.EMERGENCY
        if loss >= 0.05:
            return RiskLevel.WARNING
        if loss >= 0.025:
            return RiskLevel.CAUTION
        return RiskLevel.NORMAL

    async def get_risk_level(self) -> RiskLevel:
        """Güncel PnL'ye göre risk seviyesini döndür."""
        net_pnl = await self.get_net_pnl()
        return self.calculate_risk_level(net_pnl)

    # ─── Recovery Modu ──────────────────────────────────────────────

    async def is_recovery_active(self) -> bool:
        """Recovery modu aktif mi?"""
        client = await self._ensure_client()
        try:
            value = await client.hget(ch.RECOVERY_STATE, "active")
            return value == "1"
        except RedisError:
            return False

    async def activate_recovery(self) -> None:
        """
        Kill Switch tetiklendi — Recovery modunu aktive et.

        MASTER_BOT_v3.md Bölüm 5.4:
            → Lot büyüklüğü %50 küçülür
            → 3 ardışık kârlı işlem olana kadar devam eder
        """
        client = await self._ensure_client()
        await client.hset(ch.RECOVERY_STATE, mapping={
            "active": "1",
            "consecutive_wins": "0",
            "triggered_at": str(time.time()),
            "cleared_at": "",
        })
        logger.warning(
            "⚠ RECOVERY MODU AKTİF | Lot çarpanı=%.2f | "
            "Çıkış koşulu: %d ardışık kâr",
            RECOVERY_LOT_MULTIPLIER,
            RECOVERY_CONSECUTIVE_WINS_REQUIRED,
        )

    async def record_trade_result(self, is_win: bool) -> bool:
        """
        İşlem sonucunu kaydet ve Recovery temizleme koşulunu kontrol et.

        Dönüş: True ise Recovery modu temizlendi (normal moda dönüldü).
        """
        if not await self.is_recovery_active():
            return False

        client = await self._ensure_client()

        if is_win:
            new_wins = await client.hincrby(
                ch.RECOVERY_STATE, "consecutive_wins", 1
            )
            logger.info(
                "Recovery: Kârlı işlem | ardışık=%d/%d",
                new_wins,
                RECOVERY_CONSECUTIVE_WINS_REQUIRED,
            )

            if new_wins >= RECOVERY_CONSECUTIVE_WINS_REQUIRED:
                await self._clear_recovery(client)
                return True
        else:
            # Zararlı işlem: ardışık kâr sayacını sıfırla
            await client.hset(ch.RECOVERY_STATE, "consecutive_wins", "0")
            logger.info("Recovery: Zararlı işlem | ardışık kâr sıfırlandı")

        return False

    async def _clear_recovery(self, client: aioredis.Redis) -> None:
        """Recovery modundan çık — normal operasyona dön."""
        await client.hset(ch.RECOVERY_STATE, mapping={
            "active": "0",
            "cleared_at": str(time.time()),
        })
        logger.info(
            "✓ Recovery modu TEMİZLENDİ | %d ardışık kâr sağlandı | "
            "Normal moda dönüldü",
            RECOVERY_CONSECUTIVE_WINS_REQUIRED,
        )

    async def get_recovery_lot_multiplier(self) -> float:
        """
        Recovery moduna göre lot çarpanını döndür.

        Recovery aktif → 0.50 (yarı lot)
        Recovery pasif → 1.00 (tam lot)
        """
        if await self.is_recovery_active():
            return RECOVERY_LOT_MULTIPLIER
        return 1.0

    # ─── Bağlantı Yönetimi ─────────────────────────────────────────

    async def close(self) -> None:
        """Redis bağlantısını kapat."""
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:
                pass
            self._client = None
            logger.info("PortfolioStateManager kapatıldı")
