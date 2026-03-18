"""
Risk Gatekeeper - Portföy Risk Kapısı

MASTER_BOT_v3.md Bölüm 5.1 ve 7.2:

Net PnL Kaybı →  Lot Çarpanı  | Ek Skor Eşiği  | Maks Pozisyon
─────────────────────────────────────────────────────────────────
0 – %2.5       →  ×1.00       | +0              | 8
%2.5 – %5      →  ×0.80       | +0              | 5
%5 – %8        →  ×0.60       | +1              | 3
%8 – %10       →  ×0.30       | +2              | 2
>%10           →  ×0.00       | Kill            | 0

Bu modül:
    1. Net PnL'ye göre lot çarpanını belirler
    2. Ek skor eşiği uygular (sinyal daha yüksek olmalı)
    3. Açık pozisyon limitini kontrol eder
    4. HARD_KILL durumunda tüm sistemi durdurur

ASLA borsaya emir göndermez — sadece hesaplar ve karar verir.
"""

from __future__ import annotations

from dataclasses import dataclass

from shared.constants.enums import RiskLevel
from shared.utils.logger import get_logger

logger = get_logger("risk_gatekeeper.portfolio_risk_gate")


@dataclass(frozen=True, slots=True)
class RiskGateResult:
    """Portfolio risk kapısının sonucu."""

    risk_level: RiskLevel
    lot_multiplier: float        # Uygulanacak lot çarpanı (0.0 → 1.0)
    extra_score_threshold: int   # Sinyalin aşması gereken ek puan
    max_open_positions: int      # İzin verilen maksimum açık pozisyon
    passed: bool                 # Bu filtreden geçti mi?
    detail: str                  # İnsan tarafından okunabilir açıklama


# ── Risk parametreleri (MASTER_BOT_v3.md Bölüm 7.2) ─────────────

_RISK_TABLE: dict[RiskLevel, dict] = {
    RiskLevel.NORMAL: {
        "lot_mult": 1.0,
        "extra_score": 0,
        "max_pos": 8,
    },
    RiskLevel.CAUTION: {
        "lot_mult": 0.80,
        "extra_score": 0,
        "max_pos": 5,
    },
    RiskLevel.WARNING: {
        "lot_mult": 0.60,
        "extra_score": 1,
        "max_pos": 3,
    },
    RiskLevel.EMERGENCY: {
        "lot_mult": 0.30,
        "extra_score": 2,
        "max_pos": 2,
    },
    RiskLevel.HARD_KILL: {
        "lot_mult": 0.0,
        "extra_score": 99,
        "max_pos": 0,
    },
}


class PortfolioRiskGate:
    """
    Net PnL bazlı risk kapısı.

    Kullanım:
        gate = PortfolioRiskGate()
        result = gate.evaluate(risk_level, current_open_positions)
        if not result.passed:
            # Emir reddedildi
    """

    def __init__(self) -> None:
        self._kill_triggered: bool = False

    def evaluate(
        self,
        risk_level: RiskLevel,
        current_open_positions: int,
    ) -> RiskGateResult:
        """
        Portföy risk seviyesine göre emir geçişini değerlendir.

        Args:
            risk_level: Güncel portföy risk seviyesi
            current_open_positions: Şu anda açık pozisyon sayısı

        Dönüş: RiskGateResult (passed=True ise emir devam edebilir)
        """
        params = _RISK_TABLE[risk_level]
        lot_mult: float = params["lot_mult"]
        extra_score: int = params["extra_score"]
        max_pos: int = params["max_pos"]

        # ── HARD KILL ──
        if risk_level == RiskLevel.HARD_KILL:
            self._kill_triggered = True
            detail = (
                "🔴 HARD KILL: Net PnL >-%10 eşiğini aştı | "
                "Tüm yeni emirler DURDURULDU | Recovery modu tetiklenecek"
            )
            logger.critical(detail)
            return RiskGateResult(
                risk_level=risk_level,
                lot_multiplier=0.0,
                extra_score_threshold=99,
                max_open_positions=0,
                passed=False,
                detail=detail,
            )

        # ── EMERGENCY: Sistem donduruldu ──
        if risk_level == RiskLevel.EMERGENCY:
            detail = (
                f"🟠 EMERGENCY: Net PnL -%8-10 bandında | "
                f"lot×{lot_mult} | max_poz={max_pos} | "
                f"Yeni pozisyon çok kısıtlı"
            )
            logger.warning(detail)
            # Emergency'de yeni pozisyon açmaya izin ver ama çok kısıtlı
            if current_open_positions >= max_pos:
                return RiskGateResult(
                    risk_level=risk_level,
                    lot_multiplier=lot_mult,
                    extra_score_threshold=extra_score,
                    max_open_positions=max_pos,
                    passed=False,
                    detail=f"{detail} | Pozisyon limiti doldu "
                    f"({current_open_positions}/{max_pos})",
                )

        # ── Pozisyon limiti kontrolü ──
        if current_open_positions >= max_pos:
            detail = (
                f"Pozisyon limiti aşıldı | seviye={risk_level.name} | "
                f"açık={current_open_positions} ≥ max={max_pos}"
            )
            logger.info(detail)
            return RiskGateResult(
                risk_level=risk_level,
                lot_multiplier=lot_mult,
                extra_score_threshold=extra_score,
                max_open_positions=max_pos,
                passed=False,
                detail=detail,
            )

        # ── Geçiş ──
        detail = (
            f"Portfolio Risk OK | seviye={risk_level.name} | "
            f"lot×{lot_mult} | ek_skor=+{extra_score} | "
            f"poz={current_open_positions}/{max_pos}"
        )
        logger.debug(detail)
        return RiskGateResult(
            risk_level=risk_level,
            lot_multiplier=lot_mult,
            extra_score_threshold=extra_score,
            max_open_positions=max_pos,
            passed=True,
            detail=detail,
        )

    @property
    def is_kill_triggered(self) -> bool:
        return self._kill_triggered
