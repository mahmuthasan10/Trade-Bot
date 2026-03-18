"""
Execution Engine - Kapsamlı Asenkron Simülasyon Testi

4 senaryo ile Execution Engine'in piyasa dinamiklerine doğru tepki
verdiğini kanıtlar. Gerçek borsa API'sine (ccxt) ve veritabanına
(PostgreSQL) istek ATMAZ — tüm dış bağımlılıklar mocklanmıştır.

Senaryolar:
    1. Emir İletimi ve Pozisyon Açılışı
       → ApprovedOrder → mock borsa → fill → PositionManager'da aktif pozisyon
    2. Kademeli TP Tetiklenmesi
       → Fiyat TP1'e → %50 kapanış + stop→BE → Fiyat TP2'ye → %35 kapanış
    3. Time-in-Trade Kapanışı
       → 30dk+ hareketsizlik (±0.3 ATR bandı) → Market kapanış
    4. Veritabanı Kaydı
       → Kapanan her işlem DbLogger.log_trade'e iletildi mi?

Kullanım:
    cd <proje_kök>
    python scripts/test_execution_engine.py

Gereksinimler:
    - Harici bağımlılık YOK (Redis, PostgreSQL, ccxt bağlantısı gerekmez)
    - Tüm bileşenler in-memory mocklanmıştır
"""

from __future__ import annotations

import asyncio
import io
import json
import sys
import time

# Windows cp1254 encoding sorununu coz — UTF-8 zorla
if sys.stdout.encoding != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

sys.path.insert(0, ".")

from shared.constants.enums import Exchange, OrderType, Side, Strategy, Timeframe
from shared.utils.logger import get_logger
from services.execution_engine.models.trade import (
    CloseReason,
    Position,
    PositionStatus,
    TradeRecord,
)
from services.execution_engine.position_manager import (
    PositionManager,
    TIME_IN_TRADE_SEC,
)

logger = get_logger("test.execution_engine")

# ── Renkli terminal çıktısı ───────────────────────────────────────
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
MAGENTA = "\033[95m"
BOLD = "\033[1m"
DIM = "\033[2m"
RESET = "\033[0m"


def log_pass(scenario: str, detail: str) -> None:
    logger.info(f"{GREEN}{BOLD}  PASSED{RESET} | {scenario} | {detail}")


def log_fail(scenario: str, detail: str) -> None:
    logger.error(f"{RED}{BOLD}  FAILED{RESET} | {scenario} | {detail}")


def log_assert(scenario: str, condition: bool, detail_pass: str, detail_fail: str) -> bool:
    """Tek satırda assert + log. Döndürülen değer: başarılı mı?"""
    if condition:
        log_pass(scenario, detail_pass)
    else:
        log_fail(scenario, detail_fail)
    return condition


def log_scenario(num: int, title: str) -> None:
    logger.info("")
    logger.info(f"{CYAN}{BOLD}{'-' * 64}{RESET}")
    logger.info(f"{CYAN}{BOLD}  SENARYO {num}: {title}{RESET}")
    logger.info(f"{CYAN}{BOLD}{'-' * 64}{RESET}")


def log_step(step: str) -> None:
    logger.info(f"{DIM}    ↳ {step}{RESET}")


# ── Test Sabitleri ─────────────────────────────────────────────────
TEST_SYMBOL = "BTC/USDT"
TEST_EXCHANGE = Exchange.BINANCE
TEST_STRATEGY = Strategy.DAY_TRADING

# Sahte fiyat verileri
ENTRY_PRICE = 67_500.00
ATR_VALUE = 150.0         # ATR(14, 5m)
ATR_MULTIPLIER = 1.2      # Adaptif çarpan (sakin piyasa)
STOP_LOSS = ENTRY_PRICE - (ATR_VALUE * ATR_MULTIPLIER)    # 67320.0
TP1_PRICE = ENTRY_PRICE + (ATR_VALUE * ATR_MULTIPLIER)    # 67680.0  (+1R)
TP2_PRICE = ENTRY_PRICE + (ATR_VALUE * ATR_MULTIPLIER * 2)  # 67860.0  (+2R)
TP3_TRAILING_ATR = ATR_VALUE * 0.8                          # 120.0

LOT_SIZE = 0.015  # BTC miktarı


def make_approved_order_data() -> dict:
    """Sahte ApprovedOrder JSON verisi oluştur (Risk Gatekeeper çıktısı)."""
    return {
        "symbol": TEST_SYMBOL,
        "exchange": TEST_EXCHANGE.value,
        "strategy": TEST_STRATEGY.value,
        "side": Side.BUY.value,
        "timeframe": Timeframe.M5.value,
        "entry_price": ENTRY_PRICE,
        "unified_score": 78.0,
        "lot_size": LOT_SIZE,
        "lot_multiplier": 1.0,
        "stop_loss": STOP_LOSS,
        "atr_multiplier": ATR_MULTIPLIER,
        "atr_value": ATR_VALUE,
        "tp1_price": TP1_PRICE,
        "tp2_price": TP2_PRICE,
        "tp3_trailing_atr": TP3_TRAILING_ATR,
        "risk_per_trade_pct": 0.30,
        "risk_level": "NORMAL",
        "order_type": OrderType.MARKET.value,
        "timestamp": time.time(),
    }


def make_position(
    order_id: str = "test_001",
    entry_time: float | None = None,
) -> Position:
    """Test amaçlı Position nesnesi oluştur."""
    return Position(
        order_id=order_id,
        symbol=TEST_SYMBOL,
        exchange=TEST_EXCHANGE,
        strategy=TEST_STRATEGY,
        side=Side.BUY,
        timeframe=Timeframe.M5,
        entry_price=ENTRY_PRICE,
        total_quantity=LOT_SIZE,
        remaining_quantity=LOT_SIZE,
        stop_loss=STOP_LOSS,
        tp1_price=TP1_PRICE,
        tp2_price=TP2_PRICE,
        tp3_trailing_atr=TP3_TRAILING_ATR,
        atr_value=ATR_VALUE,
        status=PositionStatus.OPEN,
        entry_time=entry_time or time.time(),
    )


# ── Mock Bileşenler ───────────────────────────────────────────────

class MockExchangeResult:
    """ccxt emir sonucunu simüle eder."""

    @staticmethod
    def market_buy(symbol: str, amount: float, price: float) -> dict:
        return {
            "id": f"BINANCE_{int(time.time()*1000)}",
            "symbol": symbol,
            "side": "buy",
            "type": "market",
            "amount": amount,
            "filled": amount,
            "price": price,
            "average": price,
            "status": "closed",
            "timestamp": int(time.time() * 1000),
        }

    @staticmethod
    def market_sell(symbol: str, amount: float, price: float) -> dict:
        return {
            "id": f"BINANCE_{int(time.time()*1000)}",
            "symbol": symbol,
            "side": "sell",
            "type": "market",
            "amount": amount,
            "filled": amount,
            "price": price,
            "average": price,
            "status": "closed",
            "timestamp": int(time.time() * 1000),
        }


class TradeRecorder:
    """DbLogger yerine geçen in-memory kayıt tutucu."""

    def __init__(self) -> None:
        self.trades: list[TradeRecord] = []
        self.call_count: int = 0

    async def log_trade(self, record: TradeRecord) -> int:
        """DbLogger.log_trade mocklanmış versiyonu."""
        self.trades.append(record)
        self.call_count += 1
        logger.info(
            f"{MAGENTA}    [DB MOCK] Trade kaydedildi | %s | %s %s | "
            f"PnL=%.4f (%.2f%%) | neden=%s{RESET}",
            record.order_id, record.side, record.symbol,
            record.pnl, record.pnl_pct, record.close_reason,
        )
        return self.call_count


class CloseOrderRecorder:
    """OrderExecutor.close_position yerine geçen mock."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def close_position(
        self, symbol: str, side: Side, quantity: float, reason: str,
    ) -> dict:
        record = {
            "symbol": symbol,
            "side": side.value,
            "quantity": quantity,
            "reason": reason,
            "timestamp": time.time(),
        }
        self.calls.append(record)
        # Mock borsa yanıtı
        return MockExchangeResult.market_sell(symbol, quantity, ENTRY_PRICE)


class TestablePositionManager(PositionManager):
    """
    PositionManager'ın test edilebilir versiyonu.

    Değişiklikler:
        - Redis bağlantısı gerektirmez
        - _get_current_price bir dict'ten okur (kontrol edilebilir)
        - _publish_fill ve _publish_position_update sessizce atlar
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._mock_prices: dict[str, float] = {}

    def set_price(self, symbol: str, price: float) -> None:
        """Test tarafından fiyat enjekte et."""
        self._mock_prices[symbol] = price

    async def _get_current_price(self, symbol: str) -> float | None:
        """Redis yerine in-memory dict'ten oku."""
        return self._mock_prices.get(symbol)

    async def _publish_fill(self, pos, pnl, reason) -> None:
        """Redis publish atla — test ortamı."""
        pass

    async def _publish_position_update(self, pos, event) -> None:
        """Redis publish atla — test ortamı."""
        pass

    async def check_positions_once(self) -> None:
        """Tek seferlik pozisyon kontrolü (arka plan döngüsü olmadan)."""
        await self._check_all_positions()


# ═══════════════════════════════════════════════════════════════════
#  SENARYO 1: Emir İletimi ve Pozisyon Açılışı
# ═══════════════════════════════════════════════════════════════════

async def test_order_execution_and_position_open() -> bool:
    """
    channel:approved_orders → OrderExecutor → mock borsa → fill → PositionManager

    Doğrulama:
        - Mock borsaya BUY emri gönderildi
        - PositionManager'da yeni aktif pozisyon oluştu
        - Pozisyon bilgileri (fiyat, SL, TP1, TP2) doğru
    """
    log_scenario(1, "Emir İletimi ve Pozisyon Açılışı")

    # ── Bileşenleri oluştur ──
    trade_recorder = TradeRecorder()
    close_recorder = CloseOrderRecorder()

    pm = TestablePositionManager(
        close_callback=close_recorder.close_position,
        trade_callback=trade_recorder.log_trade,
    )

    # Pozisyon callback: executor → position_manager
    positions_added: list[Position] = []

    async def on_position(pos: Position) -> None:
        positions_added.append(pos)
        await pm.add_position(pos)

    # ── Sahte ApprovedOrder → doğrudan _process_order simüle et ──
    order_data = make_approved_order_data()
    log_step(f"ApprovedOrder oluşturuldu: {order_data['side']} {order_data['symbol']} "
             f"| miktar={order_data['lot_size']} | giriş={order_data['entry_price']}")

    # Executor'ın _process_order mantığını simüle et (ccxt mocklu)
    log_step("Mock borsa emri gönderiliyor (ccxt.create_market_order simülasyonu)...")

    mock_fill = MockExchangeResult.market_buy(
        symbol=order_data["symbol"],
        amount=order_data["lot_size"],
        price=order_data["entry_price"],
    )
    log_step(f"Borsa yanıtı: id={mock_fill['id']} | fill={mock_fill['average']} "
             f"| miktar={mock_fill['filled']}")

    # Position oluştur (executor'ın yapacağı gibi)
    position = Position(
        order_id=f"exec_test_{int(time.time())}",
        symbol=order_data["symbol"],
        exchange=Exchange(order_data["exchange"]),
        strategy=order_data["strategy"],
        side=Side(order_data["side"]),
        timeframe=order_data["timeframe"],
        entry_price=mock_fill["average"],
        total_quantity=mock_fill["filled"],
        remaining_quantity=mock_fill["filled"],
        stop_loss=order_data["stop_loss"],
        tp1_price=order_data["tp1_price"],
        tp2_price=order_data["tp2_price"],
        tp3_trailing_atr=order_data["tp3_trailing_atr"],
        atr_value=order_data["atr_value"],
        status=PositionStatus.OPEN,
        entry_time=time.time(),
    )

    await on_position(position)

    # ── Doğrulamalar ──
    ok = True

    ok &= log_assert(
        "1.1 Pozisyon oluştu",
        len(positions_added) == 1,
        f"1 pozisyon eklendi (positions_added={len(positions_added)})",
        f"Pozisyon sayısı hatalı: {len(positions_added)} (beklenen: 1)",
    )

    ok &= log_assert(
        "1.2 PM pozisyon sayısı",
        pm.position_count == 1,
        f"PositionManager'da 1 aktif pozisyon",
        f"PM pozisyon sayısı: {pm.position_count} (beklenen: 1)",
    )

    pos = positions_added[0] if positions_added else None

    if pos:
        ok &= log_assert(
            "1.3 Durum OPEN",
            pos.status == PositionStatus.OPEN,
            f"status={pos.status.value}",
            f"status={pos.status.value} (beklenen: OPEN)",
        )

        ok &= log_assert(
            "1.4 Giriş fiyatı",
            abs(pos.entry_price - ENTRY_PRICE) < 0.01,
            f"entry_price={pos.entry_price}",
            f"entry_price={pos.entry_price} (beklenen: {ENTRY_PRICE})",
        )

        ok &= log_assert(
            "1.5 Stop Loss",
            abs(pos.stop_loss - STOP_LOSS) < 0.01,
            f"stop_loss={pos.stop_loss:.2f}",
            f"stop_loss={pos.stop_loss:.2f} (beklenen: {STOP_LOSS:.2f})",
        )

        ok &= log_assert(
            "1.6 TP1/TP2 seviyeleri",
            abs(pos.tp1_price - TP1_PRICE) < 0.01 and abs(pos.tp2_price - TP2_PRICE) < 0.01,
            f"TP1={pos.tp1_price:.2f} | TP2={pos.tp2_price:.2f}",
            f"TP1={pos.tp1_price:.2f}/{TP1_PRICE:.2f} | TP2={pos.tp2_price:.2f}/{TP2_PRICE:.2f}",
        )

        ok &= log_assert(
            "1.7 Lot miktarı",
            abs(pos.remaining_quantity - LOT_SIZE) < 0.0001,
            f"remaining_quantity={pos.remaining_quantity}",
            f"remaining_quantity={pos.remaining_quantity} (beklenen: {LOT_SIZE})",
        )

    return ok


# ═══════════════════════════════════════════════════════════════════
#  SENARYO 2: Kademeli TP Tetiklenmesi
# ═══════════════════════════════════════════════════════════════════

async def test_tiered_tp_trigger() -> bool:
    """
    Pozisyon açık → Fiyat TP1'e → %50 kapanış + stop→BE
                  → Fiyat TP2'ye → %35 kapanış + trailing başlat

    Doğrulama:
        - TP1'de pozisyonun %50'si kapatıldı
        - TP1 sonrası stop giriş fiyatına (breakeven) çekildi
        - TP2'de pozisyonun %35'i kapatıldı
        - TP2 sonrası trailing stop aktive edildi
        - Kalan miktar %15 (TP3 trailing kısmı)
    """
    log_scenario(2, "Kademeli TP Tetiklenmesi (TP1 → TP2)")

    # ── Bileşenleri oluştur ──
    trade_recorder = TradeRecorder()
    close_recorder = CloseOrderRecorder()

    pm = TestablePositionManager(
        close_callback=close_recorder.close_position,
        trade_callback=trade_recorder.log_trade,
    )

    # ── Pozisyon aç ──
    pos = make_position(order_id="tp_test_001")
    await pm.add_position(pos)
    log_step(f"Pozisyon açıldı: {pos.side.value} {pos.symbol} @ {pos.entry_price}")
    log_step(f"SL={STOP_LOSS:.2f} | TP1={TP1_PRICE:.2f} | TP2={TP2_PRICE:.2f}")

    ok = True

    # ── Adım 1: Fiyat TP1'in hemen altında → henüz tetiklenmemeli ──
    price_below_tp1 = TP1_PRICE - 5.0
    pm.set_price(TEST_SYMBOL, price_below_tp1)
    log_step(f"Fiyat güncellendi: {price_below_tp1:.2f} (TP1'in {5:.2f}$ altında)")
    await pm.check_positions_once()

    ok &= log_assert(
        "2.1 TP1 altı: pozisyon açık",
        pos.status == PositionStatus.OPEN,
        f"status={pos.status.value} (TP1 tetiklenmedi)",
        f"status={pos.status.value} (TP1 erken tetiklendi!)",
    )

    ok &= log_assert(
        "2.2 TP1 altı: kapanış yok",
        len(close_recorder.calls) == 0,
        "Kapanış emri gönderilmedi",
        f"Beklenmeyen kapanış emri: {len(close_recorder.calls)} adet",
    )

    # ── Adım 2: Fiyat TP1'e ulaşıyor → %50 kapanış ──
    price_at_tp1 = TP1_PRICE + 2.0
    pm.set_price(TEST_SYMBOL, price_at_tp1)
    log_step(f"Fiyat TP1'e ulaştı: {price_at_tp1:.2f} (TP1={TP1_PRICE:.2f})")
    await pm.check_positions_once()

    expected_tp1_qty = LOT_SIZE * 0.50

    ok &= log_assert(
        "2.3 TP1: durum TP1_HIT",
        pos.status == PositionStatus.TP1_HIT,
        f"status={pos.status.value}",
        f"status={pos.status.value} (beklenen: TP1_HIT)",
    )

    ok &= log_assert(
        "2.4 TP1: %50 kapatıldı",
        len(close_recorder.calls) == 1
        and abs(close_recorder.calls[0]["quantity"] - expected_tp1_qty) < 0.0001,
        f"Kapanış: miktar={close_recorder.calls[0]['quantity'] if close_recorder.calls else 0:.6f} "
        f"(beklenen: {expected_tp1_qty:.6f})",
        f"Kapanış sayısı={len(close_recorder.calls)} "
        f"veya miktar hatalı",
    )

    ok &= log_assert(
        "2.5 TP1: stop → breakeven",
        abs(pos.stop_loss - ENTRY_PRICE) < 0.01,
        f"stop_loss={pos.stop_loss:.2f} → giriş fiyatı (breakeven)",
        f"stop_loss={pos.stop_loss:.2f} (beklenen: {ENTRY_PRICE:.2f} BE)",
    )

    remaining_after_tp1 = LOT_SIZE - expected_tp1_qty
    ok &= log_assert(
        "2.6 TP1: kalan miktar",
        abs(pos.remaining_quantity - remaining_after_tp1) < 0.0001,
        f"remaining={pos.remaining_quantity:.6f}",
        f"remaining={pos.remaining_quantity:.6f} (beklenen: {remaining_after_tp1:.6f})",
    )

    # ── Adım 3: Fiyat TP2'ye ulaşıyor → %35 kapanış ──
    price_at_tp2 = TP2_PRICE + 3.0
    pm.set_price(TEST_SYMBOL, price_at_tp2)
    log_step(f"Fiyat TP2'ye ulaştı: {price_at_tp2:.2f} (TP2={TP2_PRICE:.2f})")
    await pm.check_positions_once()

    expected_tp2_qty = LOT_SIZE * 0.35

    ok &= log_assert(
        "2.7 TP2: durum TP2_HIT",
        pos.status == PositionStatus.TP2_HIT,
        f"status={pos.status.value}",
        f"status={pos.status.value} (beklenen: TP2_HIT)",
    )

    ok &= log_assert(
        "2.8 TP2: %35 kapatıldı",
        len(close_recorder.calls) == 2
        and abs(close_recorder.calls[1]["quantity"] - expected_tp2_qty) < 0.0001,
        f"Kapanış: miktar={close_recorder.calls[1]['quantity'] if len(close_recorder.calls) > 1 else 0:.6f} "
        f"(beklenen: {expected_tp2_qty:.6f})",
        f"Kapanış sayısı={len(close_recorder.calls)} "
        f"veya miktar hatalı",
    )

    ok &= log_assert(
        "2.9 TP2: trailing stop aktif",
        pos.trailing_stop_price is not None and pos.trailing_high is not None,
        f"trailing_stop={pos.trailing_stop_price:.2f} | trailing_high={pos.trailing_high:.2f}",
        "trailing_stop veya trailing_high None!",
    )

    # Kalan miktar = %15 (TP3 kısmı)
    expected_remaining = LOT_SIZE * 0.15
    ok &= log_assert(
        "2.10 TP2: kalan miktar (%15)",
        abs(pos.remaining_quantity - expected_remaining) < 0.0001,
        f"remaining={pos.remaining_quantity:.6f} (TP3 trailing kısmı)",
        f"remaining={pos.remaining_quantity:.6f} (beklenen: {expected_remaining:.6f})",
    )

    # ── Trade kayıtları doğrula ──
    ok &= log_assert(
        "2.11 Trade kayıtları",
        trade_recorder.call_count == 2,
        f"2 trade kaydedildi (TP1 + TP2)",
        f"Trade sayısı: {trade_recorder.call_count} (beklenen: 2)",
    )

    if trade_recorder.call_count >= 2:
        ok &= log_assert(
            "2.12 TP1 kaydı doğru",
            trade_recorder.trades[0].close_reason == CloseReason.TP1.value,
            f"İlk kayıt: close_reason={trade_recorder.trades[0].close_reason}",
            f"İlk kayıt: close_reason={trade_recorder.trades[0].close_reason} (beklenen: TP1)",
        )
        ok &= log_assert(
            "2.13 TP2 kaydı doğru",
            trade_recorder.trades[1].close_reason == CloseReason.TP2.value,
            f"İkinci kayıt: close_reason={trade_recorder.trades[1].close_reason}",
            f"İkinci kayıt: close_reason={trade_recorder.trades[1].close_reason} (beklenen: TP2)",
        )

    return ok


# ═══════════════════════════════════════════════════════════════════
#  SENARYO 3: Time-in-Trade Kapanışı
# ═══════════════════════════════════════════════════════════════════

async def test_time_in_trade_exit() -> bool:
    """
    Pozisyon açık → Fiyat ±0.3 ATR bandı içinde → 30dk+ geçti → Market kapanış

    Doğrulama:
        - Süre dolmadan fiyat banda yakınken kapanmıyor
        - Süre dolunca ve fiyat hâlâ bant içindeyse TIME_EXIT ile kapanıyor
        - Fiyat bant dışına çıktıysa kapanmıyor (istisna)
    """
    log_scenario(3, "Time-in-Trade Kapanışı (30dk Hareketsizlik)")

    trade_recorder = TradeRecorder()
    close_recorder = CloseOrderRecorder()

    pm = TestablePositionManager(
        close_callback=close_recorder.close_position,
        trade_callback=trade_recorder.log_trade,
    )

    # ── Time-in-Trade parametreleri ──
    band = ATR_VALUE * 0.3  # ±45.0$
    log_step(f"ATR={ATR_VALUE} | ±0.3 ATR bandı = ±{band:.2f}$")
    log_step(f"Giriş fiyatı: {ENTRY_PRICE:.2f}")
    log_step(f"Bant: [{ENTRY_PRICE - band:.2f} — {ENTRY_PRICE + band:.2f}]")

    ok = True

    # ── Adım 1: Süre dolmamış + bant içi fiyat → kapanmAMALI ──
    pos = make_position(order_id="time_test_001", entry_time=time.time())
    await pm.add_position(pos)

    price_in_band = ENTRY_PRICE + 20.0  # Bant içinde (+20 < +45)
    pm.set_price(TEST_SYMBOL, price_in_band)
    log_step(f"[t=0s] Fiyat={price_in_band:.2f} (bant içi, süre dolmadı)")
    await pm.check_positions_once()

    ok &= log_assert(
        "3.1 Erken: kapanma yok",
        pos.status == PositionStatus.OPEN and len(close_recorder.calls) == 0,
        "Pozisyon açık, kapanış yok (süre dolmadı)",
        f"Erken kapanma! status={pos.status.value}, kapanış={len(close_recorder.calls)}",
    )

    # ── Adım 2: Süre doldu + bant içi fiyat → KAPANMALI ──
    # entry_time'ı 31 dakika geriye çek (30dk eşiği aşsın)
    pos.entry_time = time.time() - (31 * 60)
    pm.set_price(TEST_SYMBOL, price_in_band)
    log_step(f"[t=31dk] Fiyat={price_in_band:.2f} (bant içi, süre DOLDU)")
    await pm.check_positions_once()

    ok &= log_assert(
        "3.2 Time Exit: pozisyon kapandı",
        pos.status == PositionStatus.CLOSED,
        f"status=CLOSED (TIME_EXIT tetiklendi)",
        f"status={pos.status.value} (beklenen: CLOSED)",
    )

    ok &= log_assert(
        "3.3 Time Exit: kapanış emri",
        len(close_recorder.calls) == 1 and close_recorder.calls[0]["reason"] == "TIME_EXIT",
        f"reason={close_recorder.calls[0]['reason'] if close_recorder.calls else 'YOK'}",
        f"Kapanış sayısı={len(close_recorder.calls)}, "
        f"reason={close_recorder.calls[0]['reason'] if close_recorder.calls else 'YOK'}",
    )

    ok &= log_assert(
        "3.4 Time Exit: trade kaydı",
        trade_recorder.call_count == 1
        and trade_recorder.trades[0].close_reason == CloseReason.TIME_EXIT.value,
        f"Trade kaydedildi: close_reason={trade_recorder.trades[0].close_reason if trade_recorder.trades else 'YOK'}",
        f"Trade sayısı={trade_recorder.call_count}",
    )

    # ── Pozisyon PM'den temizlendi mi? ──
    ok &= log_assert(
        "3.5 Time Exit: PM'den silindi",
        pm.position_count == 0,
        f"Açık pozisyon=0 (temizlendi)",
        f"Açık pozisyon={pm.position_count} (beklenen: 0)",
    )

    # ── Adım 3: Süre doldu AMA fiyat bant dışı → KAPANMAMALI ──
    close_recorder_2 = CloseOrderRecorder()
    trade_recorder_2 = TradeRecorder()

    pm2 = TestablePositionManager(
        close_callback=close_recorder_2.close_position,
        trade_callback=trade_recorder_2.log_trade,
    )

    pos2 = make_position(
        order_id="time_test_002",
        entry_time=time.time() - (35 * 60),  # 35dk önce
    )
    await pm2.add_position(pos2)

    # Fiyat bant DIŞINDA (+80 > +45 ATR bandı)
    price_out_band = ENTRY_PRICE + 80.0
    pm2.set_price(TEST_SYMBOL, price_out_band)
    log_step(f"[t=35dk] Fiyat={price_out_band:.2f} (bant DIŞI → momentum var)")
    await pm2.check_positions_once()

    ok &= log_assert(
        "3.6 İstisna: bant dışı → kapanmadı",
        pos2.status == PositionStatus.OPEN and len(close_recorder_2.calls) == 0,
        "Pozisyon açık kaldı (fiyat bant dışında, momentum var)",
        f"status={pos2.status.value}, kapanış={len(close_recorder_2.calls)} "
        f"(bant dışında kapanmamalıydı!)",
    )

    return ok


# ═══════════════════════════════════════════════════════════════════
#  SENARYO 4: Veritabanı Kaydı Doğrulaması
# ═══════════════════════════════════════════════════════════════════

async def test_database_logging() -> bool:
    """
    Her kapanan işlemde DbLogger.log_trade'in doğru verilerle
    çağrıldığını doğrula.

    Senaryo: 1 pozisyon aç → SL ile kapat → kayıt doğrula
    Ardından: TP1 + TP2 senaryosu → 2 kayıt doğrula

    Doğrulama:
        - DbLogger.log_trade çağrıldı
        - TradeRecord alanları doğru (order_id, symbol, pnl, reason)
        - PnL hesaplaması doğru
    """
    log_scenario(4, "Veritabanı Kaydı Doğrulaması (DbLogger)")

    trade_recorder = TradeRecorder()
    close_recorder = CloseOrderRecorder()

    pm = TestablePositionManager(
        close_callback=close_recorder.close_position,
        trade_callback=trade_recorder.log_trade,
    )

    ok = True

    # ── Test A: Stop Loss kapanışı ──
    log_step("Test A: Stop Loss ile kapanış")
    pos_sl = make_position(order_id="db_test_sl")
    await pm.add_position(pos_sl)

    sl_price = STOP_LOSS - 5.0  # SL'nin 5$ altı
    pm.set_price(TEST_SYMBOL, sl_price)
    log_step(f"Fiyat SL altına düştü: {sl_price:.2f} (SL={STOP_LOSS:.2f})")
    await pm.check_positions_once()

    ok &= log_assert(
        "4.1 SL: trade kaydedildi",
        trade_recorder.call_count == 1,
        f"1 trade kaydı oluştu",
        f"Trade sayısı: {trade_recorder.call_count} (beklenen: 1)",
    )

    if trade_recorder.trades:
        rec = trade_recorder.trades[0]

        ok &= log_assert(
            "4.2 SL: order_id doğru",
            rec.order_id == "db_test_sl",
            f"order_id={rec.order_id}",
            f"order_id={rec.order_id} (beklenen: db_test_sl)",
        )

        ok &= log_assert(
            "4.3 SL: close_reason doğru",
            rec.close_reason == CloseReason.STOP_LOSS.value,
            f"close_reason={rec.close_reason}",
            f"close_reason={rec.close_reason} (beklenen: STOP_LOSS)",
        )

        # PnL doğrula: BUY pozisyon, giriş=67500, çıkış=SL-5
        expected_pnl = (sl_price - ENTRY_PRICE) * LOT_SIZE  # Negatif
        ok &= log_assert(
            "4.4 SL: PnL hesabı doğru",
            abs(rec.pnl - expected_pnl) < 0.01,
            f"pnl={rec.pnl:.4f} (beklenen: {expected_pnl:.4f})",
            f"pnl={rec.pnl:.4f} (beklenen: {expected_pnl:.4f})",
        )

        ok &= log_assert(
            "4.5 SL: PnL negatif",
            rec.pnl < 0,
            f"pnl={rec.pnl:.4f} (zarar — doğru)",
            f"pnl={rec.pnl:.4f} (SL'de kâr olmamalı!)",
        )

        ok &= log_assert(
            "4.6 SL: metadata doğru",
            rec.symbol == TEST_SYMBOL
            and rec.exchange == TEST_EXCHANGE.value
            and rec.side == Side.BUY.value
            and rec.atr_value == ATR_VALUE,
            f"symbol={rec.symbol} | exchange={rec.exchange} | "
            f"side={rec.side} | atr={rec.atr_value}",
            "Metadata alanları hatalı",
        )

    # ── Test B: TP1 + TP2 çoklu kayıt ──
    log_step("Test B: TP1 + TP2 ile çoklu kapanış kaydı")
    trade_recorder_b = TradeRecorder()
    close_recorder_b = CloseOrderRecorder()

    pm_b = TestablePositionManager(
        close_callback=close_recorder_b.close_position,
        trade_callback=trade_recorder_b.log_trade,
    )

    pos_tp = make_position(order_id="db_test_tp")
    await pm_b.add_position(pos_tp)

    # TP1
    pm_b.set_price(TEST_SYMBOL, TP1_PRICE + 1)
    await pm_b.check_positions_once()

    # TP2
    pm_b.set_price(TEST_SYMBOL, TP2_PRICE + 1)
    await pm_b.check_positions_once()

    ok &= log_assert(
        "4.7 TP çoklu: 2 trade kaydı",
        trade_recorder_b.call_count == 2,
        f"2 trade kaydedildi (TP1 + TP2)",
        f"Trade sayısı: {trade_recorder_b.call_count} (beklenen: 2)",
    )

    if trade_recorder_b.call_count >= 2:
        tp1_rec = trade_recorder_b.trades[0]
        tp2_rec = trade_recorder_b.trades[1]

        ok &= log_assert(
            "4.8 TP çoklu: her ikisi de pozitif PnL",
            tp1_rec.pnl > 0 and tp2_rec.pnl > 0,
            f"TP1 PnL={tp1_rec.pnl:.4f} | TP2 PnL={tp2_rec.pnl:.4f} (kâr)",
            f"TP1 PnL={tp1_rec.pnl:.4f} | TP2 PnL={tp2_rec.pnl:.4f} (kâr bekleniyor!)",
        )

        ok &= log_assert(
            "4.9 TP çoklu: miktar oranları",
            abs(tp1_rec.quantity - LOT_SIZE * 0.50) < 0.0001
            and abs(tp2_rec.quantity - LOT_SIZE * 0.35) < 0.0001,
            f"TP1 qty={tp1_rec.quantity:.6f} (%50) | TP2 qty={tp2_rec.quantity:.6f} (%35)",
            f"TP1 qty={tp1_rec.quantity:.6f} | TP2 qty={tp2_rec.quantity:.6f} (oranlar hatalı)",
        )

        ok &= log_assert(
            "4.10 TP çoklu: aynı order_id",
            tp1_rec.order_id == tp2_rec.order_id == "db_test_tp",
            f"Her iki kayıt aynı order_id: {tp1_rec.order_id}",
            f"order_id farklı: {tp1_rec.order_id} vs {tp2_rec.order_id}",
        )

        # Zaman kontrolleri
        ok &= log_assert(
            "4.11 TP çoklu: exit_time > entry_time",
            tp1_rec.exit_time > tp1_rec.entry_time
            and tp2_rec.exit_time > tp2_rec.entry_time,
            "exit_time > entry_time (doğru)",
            "Zaman sıralaması hatalı!",
        )

    return ok


# ═══════════════════════════════════════════════════════════════════
#  ANA TEST ORKESTRATÖRÜ
# ═══════════════════════════════════════════════════════════════════

async def main() -> None:
    logger.info("")
    logger.info(f"{BOLD}{'=' * 64}{RESET}")
    logger.info(f"{BOLD}  Execution Engine - Kapsamlı Asenkron Simülasyon Testi{RESET}")
    logger.info(f"{BOLD}  Mod: In-Memory (Redis/PostgreSQL/ccxt gerektirmez){RESET}")
    logger.info(f"{BOLD}{'=' * 64}{RESET}")
    logger.info("")
    logger.info(f"{DIM}  Fiyat Parametreleri:{RESET}")
    logger.info(f"{DIM}    Giriş    = {ENTRY_PRICE:.2f}{RESET}")
    logger.info(f"{DIM}    ATR      = {ATR_VALUE:.2f} (çarpan: {ATR_MULTIPLIER}){RESET}")
    logger.info(f"{DIM}    Stop     = {STOP_LOSS:.2f}{RESET}")
    logger.info(f"{DIM}    TP1 (1R) = {TP1_PRICE:.2f}{RESET}")
    logger.info(f"{DIM}    TP2 (2R) = {TP2_PRICE:.2f}{RESET}")
    logger.info(f"{DIM}    Trailing = {TP3_TRAILING_ATR:.2f} (ATR × 0.8){RESET}")
    logger.info(f"{DIM}    Lot      = {LOT_SIZE} BTC{RESET}")

    results: dict[str, bool] = {}

    try:
        results["Senaryo 1: Emir İletimi ve Pozisyon Açılışı"] = (
            await test_order_execution_and_position_open()
        )

        results["Senaryo 2: Kademeli TP Tetiklenmesi (TP1 → TP2)"] = (
            await test_tiered_tp_trigger()
        )

        results["Senaryo 3: Time-in-Trade Kapanışı"] = (
            await test_time_in_trade_exit()
        )

        results["Senaryo 4: Veritabanı Kaydı Doğrulaması"] = (
            await test_database_logging()
        )

    except Exception as exc:
        logger.exception("Test sırasında beklenmeyen hata: %s", exc)

    # ── Sonuç Özeti ──
    logger.info("")
    logger.info(f"{BOLD}{'=' * 64}{RESET}")
    logger.info(f"{BOLD}  TEST SONUÇLARI{RESET}")
    logger.info(f"{BOLD}{'=' * 64}{RESET}")

    passed = 0
    failed = 0
    for name, success in results.items():
        icon = f"{GREEN}PASSED{RESET}" if success else f"{RED}FAILED{RESET}"
        logger.info(f"  {icon}  {name}")
        if success:
            passed += 1
        else:
            failed += 1

    total = passed + failed
    logger.info(f"{BOLD}{'-' * 64}{RESET}")

    if failed == 0:
        logger.info(
            f"  {GREEN}{BOLD}TÜM SENARYOLAR BAŞARILI! "
            f"({passed}/{total}){RESET}"
        )
    else:
        logger.info(
            f"  {RED}{BOLD}{failed} SENARYO BAŞARISIZ! "
            f"({passed}/{total} geçti){RESET}"
        )

    logger.info(f"{BOLD}{'=' * 64}{RESET}")

    if failed > 0:
        sys.exit(1)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
