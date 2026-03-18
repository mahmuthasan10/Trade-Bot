"""
Performance Analyzer - Trade Sonuç Analizi

PostgreSQL trades tablosundaki tüm işlemleri çekerek detaylı
performans metriklerini hesaplar ve terminale renkli tablo olarak basar.

Kullanım:
    python scripts/performance_analyzer.py
    python scripts/performance_analyzer.py --strategy universal
    python scripts/performance_analyzer.py --symbol BTC/USDT --days 7

Hesaplanan Metrikler:
    1. Toplam İşlem Sayısı / Kazanan / Kaybeden
    2. Win Rate (%)
    3. Net PnL (toplam kâr/zarar)
    4. Profit Factor (Brüt Kâr / Brüt Zarar)
    5. Maximum Drawdown ($ ve %)
    6. Ortalama Kâr / Ortalama Kayıp
    7. Strateji ve sembol bazlı kırılımlar
    8. Kapanış nedeni dağılımı (TP1, TP2, STOP_LOSS vb.)
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import asyncpg

from config.settings import settings
from shared.utils.logger import get_logger

logger = get_logger("scripts.analyzer")


# ── Metrik Veri Yapıları ─────────────────────────────────────────

@dataclass
class PerformanceMetrics:
    """Hesaplanan tüm performans metrikleri."""

    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    win_rate: float = 0.0

    gross_profit: float = 0.0
    gross_loss: float = 0.0
    net_pnl: float = 0.0
    profit_factor: float = 0.0

    avg_win: float = 0.0
    avg_loss: float = 0.0
    avg_pnl: float = 0.0
    largest_win: float = 0.0
    largest_loss: float = 0.0

    max_drawdown: float = 0.0
    max_drawdown_pct: float = 0.0

    avg_trade_duration_min: float = 0.0

    by_strategy: dict = field(default_factory=dict)
    by_symbol: dict = field(default_factory=dict)
    by_close_reason: dict = field(default_factory=dict)


# ── Ana Sınıf ────────────────────────────────────────────────────

class PerformanceAnalyzer:
    """
    asyncpg ile trades tablosunu sorgular, metrikleri hesaplar
    ve terminale renkli formatta basar.
    """

    def __init__(
        self,
        strategy: Optional[str] = None,
        symbol: Optional[str] = None,
        days: Optional[int] = None,
    ) -> None:
        self.strategy = strategy
        self.symbol = symbol
        self.days = days
        self._pool: Optional[asyncpg.Pool] = None

    async def _connect(self) -> None:
        self._pool = await asyncpg.create_pool(
            dsn=settings.postgres.dsn,
            min_size=1,
            max_size=3,
        )

    async def _fetch_trades(self) -> list[asyncpg.Record]:
        """Filtrelere göre trades tablosundan satırları çek."""
        assert self._pool is not None

        conditions: list[str] = []
        params: list = []
        idx = 1

        if self.strategy:
            conditions.append(f"strategy = ${idx}")
            params.append(self.strategy)
            idx += 1

        if self.symbol:
            conditions.append(f"symbol = ${idx}")
            params.append(self.symbol)
            idx += 1

        if self.days:
            conditions.append(f"exit_time >= NOW() - INTERVAL '{self.days} days'")

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

        query = f"""
            SELECT
                order_id, symbol, exchange, strategy, side,
                entry_price, exit_price, quantity,
                pnl, pnl_pct, close_reason,
                entry_time, exit_time
            FROM trades
            {where}
            ORDER BY exit_time ASC
        """

        async with self._pool.acquire() as conn:
            rows = await conn.fetch(query, *params)

        return rows

    def _compute_metrics(self, trades: list[asyncpg.Record]) -> PerformanceMetrics:
        """Tüm metrikleri hesapla."""
        m = PerformanceMetrics()

        if not trades:
            return m

        m.total_trades = len(trades)

        pnl_list: list[float] = []
        win_pnls: list[float] = []
        loss_pnls: list[float] = []
        durations: list[float] = []

        for t in trades:
            pnl = float(t["pnl"])
            pnl_list.append(pnl)

            if pnl > 0:
                m.winning_trades += 1
                m.gross_profit += pnl
                win_pnls.append(pnl)
            else:
                m.losing_trades += 1
                m.gross_loss += abs(pnl)
                loss_pnls.append(pnl)

            # Süre
            entry_t = t["entry_time"]
            exit_t = t["exit_time"]
            if entry_t and exit_t:
                delta = (exit_t - entry_t).total_seconds() / 60.0
                durations.append(delta)

            # Strateji kırılımı
            strat = t["strategy"]
            if strat not in m.by_strategy:
                m.by_strategy[strat] = {"wins": 0, "losses": 0, "pnl": 0.0}
            m.by_strategy[strat]["pnl"] += pnl
            if pnl > 0:
                m.by_strategy[strat]["wins"] += 1
            else:
                m.by_strategy[strat]["losses"] += 1

            # Sembol kırılımı
            sym = t["symbol"]
            if sym not in m.by_symbol:
                m.by_symbol[sym] = {"wins": 0, "losses": 0, "pnl": 0.0}
            m.by_symbol[sym]["pnl"] += pnl
            if pnl > 0:
                m.by_symbol[sym]["wins"] += 1
            else:
                m.by_symbol[sym]["losses"] += 1

            # Kapanış nedeni
            reason = t["close_reason"]
            m.by_close_reason[reason] = m.by_close_reason.get(reason, 0) + 1

        # ── Temel Metrikler ──
        m.net_pnl = m.gross_profit - m.gross_loss
        m.win_rate = (m.winning_trades / m.total_trades * 100) if m.total_trades > 0 else 0.0
        m.profit_factor = (m.gross_profit / m.gross_loss) if m.gross_loss > 0 else float("inf")

        m.avg_win = (sum(win_pnls) / len(win_pnls)) if win_pnls else 0.0
        m.avg_loss = (sum(loss_pnls) / len(loss_pnls)) if loss_pnls else 0.0
        m.avg_pnl = (sum(pnl_list) / len(pnl_list)) if pnl_list else 0.0

        m.largest_win = max(win_pnls) if win_pnls else 0.0
        m.largest_loss = min(loss_pnls) if loss_pnls else 0.0

        if durations:
            m.avg_trade_duration_min = sum(durations) / len(durations)

        # ── Maximum Drawdown ──
        m.max_drawdown, m.max_drawdown_pct = self._calc_max_drawdown(pnl_list)

        return m

    @staticmethod
    def _calc_max_drawdown(pnl_list: list[float]) -> tuple[float, float]:
        """
        Kümülatif PnL eğrisi üzerinden Maximum Drawdown hesapla.

        Returns:
            (max_drawdown_abs, max_drawdown_pct)
        """
        if not pnl_list:
            return 0.0, 0.0

        cumulative = 0.0
        peak = 0.0
        max_dd = 0.0

        for pnl in pnl_list:
            cumulative += pnl
            if cumulative > peak:
                peak = cumulative
            dd = peak - cumulative
            if dd > max_dd:
                max_dd = dd

        max_dd_pct = (max_dd / peak * 100) if peak > 0 else 0.0
        return max_dd, max_dd_pct

    # ── Terminal Çıktısı ──────────────────────────────────────────

    def _print_report(self, m: PerformanceMetrics) -> None:
        """Metrikleri renkli ve formatlı olarak terminale bas."""

        # ANSI renk kodları
        BOLD = "\033[1m"
        GREEN = "\033[92m"
        RED = "\033[91m"
        YELLOW = "\033[93m"
        CYAN = "\033[96m"
        DIM = "\033[2m"
        RESET = "\033[0m"

        def colored_pnl(val: float) -> str:
            color = GREEN if val >= 0 else RED
            sign = "+" if val >= 0 else ""
            return f"{color}{sign}{val:,.4f}{RESET}"

        def colored_pct(val: float) -> str:
            color = GREEN if val >= 0 else RED
            sign = "+" if val >= 0 else ""
            return f"{color}{sign}{val:.2f}%{RESET}"

        line = f"{DIM}{'─' * 60}{RESET}"
        dline = f"{BOLD}{'═' * 60}{RESET}"

        print()
        print(dline)
        print(f"{BOLD}{CYAN}  MASTER BOT v3.0 — PERFORMANS RAPORU{RESET}")
        print(dline)

        # Filtreler
        filters = []
        if self.strategy:
            filters.append(f"Strateji: {self.strategy}")
        if self.symbol:
            filters.append(f"Sembol: {self.symbol}")
        if self.days:
            filters.append(f"Son {self.days} gün")
        if filters:
            print(f"  {DIM}Filtre: {' | '.join(filters)}{RESET}")
            print(line)

        if m.total_trades == 0:
            print(f"\n  {YELLOW}Hiç işlem bulunamadı.{RESET}\n")
            print(dline)
            return

        # ── Genel Özet ──
        print(f"\n  {BOLD}GENEL ÖZET{RESET}")
        print(line)
        print(f"  {'Toplam İşlem':<28} {BOLD}{m.total_trades}{RESET}")
        print(f"  {'Kazanan':<28} {GREEN}{m.winning_trades}{RESET}")
        print(f"  {'Kaybeden':<28} {RED}{m.losing_trades}{RESET}")
        print(f"  {'Win Rate':<28} {colored_pct(m.win_rate)}")
        print(f"  {'Ort. İşlem Süresi':<28} {m.avg_trade_duration_min:.1f} dk")

        # ── Kâr / Zarar ──
        print(f"\n  {BOLD}KÂR / ZARAR{RESET}")
        print(line)
        print(f"  {'Net PnL':<28} {colored_pnl(m.net_pnl)}")
        print(f"  {'Brüt Kâr':<28} {GREEN}+{m.gross_profit:,.4f}{RESET}")
        print(f"  {'Brüt Zarar':<28} {RED}-{m.gross_loss:,.4f}{RESET}")

        pf_str = f"{m.profit_factor:.2f}" if m.profit_factor != float("inf") else "∞"
        pf_color = GREEN if m.profit_factor >= 1.0 else RED
        print(f"  {'Profit Factor':<28} {pf_color}{pf_str}{RESET}")

        # ── Risk Metrikleri ──
        print(f"\n  {BOLD}RİSK METRİKLERİ{RESET}")
        print(line)
        print(f"  {'Ortalama Kâr (kazanan)':<28} {GREEN}+{m.avg_win:,.4f}{RESET}")
        print(f"  {'Ortalama Kayıp (kaybeden)':<28} {RED}{m.avg_loss:,.4f}{RESET}")
        print(f"  {'Ortalama PnL (tüm)':<28} {colored_pnl(m.avg_pnl)}")
        print(f"  {'En Büyük Kazanç':<28} {GREEN}+{m.largest_win:,.4f}{RESET}")
        print(f"  {'En Büyük Kayıp':<28} {RED}{m.largest_loss:,.4f}{RESET}")
        print(f"  {'Max Drawdown':<28} {RED}-{m.max_drawdown:,.4f}{RESET}  ({RED}-{m.max_drawdown_pct:.2f}%{RESET})")

        # ── Strateji Kırılımı ──
        if m.by_strategy:
            print(f"\n  {BOLD}STRATEJİ KIRILIMI{RESET}")
            print(line)
            print(f"  {'Strateji':<20} {'İşlem':>6} {'Win%':>8} {'Net PnL':>14}")
            print(f"  {DIM}{'─' * 50}{RESET}")
            for strat, data in sorted(m.by_strategy.items()):
                total = data["wins"] + data["losses"]
                wr = (data["wins"] / total * 100) if total > 0 else 0.0
                pnl_c = colored_pnl(data["pnl"])
                wr_c = colored_pct(wr)
                print(f"  {strat:<20} {total:>6} {wr_c:>18} {pnl_c:>24}")

        # ── Sembol Kırılımı ──
        if m.by_symbol:
            print(f"\n  {BOLD}SEMBOL KIRILIMI{RESET}")
            print(line)
            print(f"  {'Sembol':<20} {'İşlem':>6} {'Win%':>8} {'Net PnL':>14}")
            print(f"  {DIM}{'─' * 50}{RESET}")
            for sym, data in sorted(m.by_symbol.items(), key=lambda x: x[1]["pnl"], reverse=True):
                total = data["wins"] + data["losses"]
                wr = (data["wins"] / total * 100) if total > 0 else 0.0
                pnl_c = colored_pnl(data["pnl"])
                wr_c = colored_pct(wr)
                print(f"  {sym:<20} {total:>6} {wr_c:>18} {pnl_c:>24}")

        # ── Kapanış Nedeni Dağılımı ──
        if m.by_close_reason:
            print(f"\n  {BOLD}KAPANIŞ NEDENİ DAĞILIMI{RESET}")
            print(line)
            for reason, count in sorted(m.by_close_reason.items(), key=lambda x: x[1], reverse=True):
                pct = count / m.total_trades * 100
                bar_len = int(pct / 2)
                bar = f"{CYAN}{'█' * bar_len}{RESET}"
                print(f"  {reason:<20} {count:>4} ({pct:5.1f}%)  {bar}")

        print()
        print(dline)
        print(f"  {DIM}Rapor zamanı: {datetime.now(tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}{RESET}")
        print(dline)
        print()

    # ── Ana Akış ─────────────────────────────────────────────────

    async def run(self) -> PerformanceMetrics:
        """Bağlan → sorgula → hesapla → yazdır."""
        try:
            await self._connect()
            trades = await self._fetch_trades()
            metrics = self._compute_metrics(trades)
            self._print_report(metrics)
            return metrics
        finally:
            if self._pool:
                await self._pool.close()


# ── CLI Giriş Noktası ────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Master Bot v3.0 — Trade Performans Analizi",
    )
    parser.add_argument("--strategy", type=str, default=None, help="Strateji filtresi (ör: universal, scalping)")
    parser.add_argument("--symbol", type=str, default=None, help="Sembol filtresi (ör: BTC/USDT)")
    parser.add_argument("--days", type=int, default=None, help="Son N gün filtresi")

    args = parser.parse_args()

    analyzer = PerformanceAnalyzer(
        strategy=args.strategy,
        symbol=args.symbol,
        days=args.days,
    )
    asyncio.run(analyzer.run())


if __name__ == "__main__":
    main()
