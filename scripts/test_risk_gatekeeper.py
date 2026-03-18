"""
Risk Gatekeeper - Kapsamlı Asenkron Entegrasyon Testi

4 senaryo ile Risk Gatekeeper'ın tüm filtrelerini test eder:
    1. Normal Onay       → PnL %0, normal spread → ONAY (lot×1.0)
    2. Spread Reddi      → anlık/ort > 2.0× → REDDEDİLDİ (SPREAD_GATE)
    3. Recovery Modu     → lot×0.50 aktif → ONAY (lot×0.50)
    4. Hard Kill         → PnL -%11 → REDDEDİLDİ (HARD_KILL)

Kullanım:
    cd <proje_kök>
    py -3 scripts/test_risk_gatekeeper.py

Gereksinimler:
    - Redis çalışıyor olmalı (docker-compose up redis)
    - Risk Gatekeeper servisi çalışmıyor olmalı (test kendi başlatır)
"""

from __future__ import annotations

import asyncio
import json
import sys
import time

sys.path.insert(0, ".")

import redis.asyncio as aioredis

from config.settings import settings
from shared.constants import redis_channels as ch
from shared.utils.logger import get_logger

logger = get_logger("test.risk_gatekeeper")

TEST_SYMBOL = "BTC/USDT"
TEST_EXCHANGE = "binance"

# ── Renkli terminal çıktısı ─────────────────────────────────────

GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
BOLD = "\033[1m"
RESET = "\033[0m"


def log_pass(scenario: str, detail: str) -> None:
    logger.info(f"{GREEN}{BOLD}PASSED{RESET} | {scenario} | {detail}")


def log_fail(scenario: str, detail: str) -> None:
    logger.error(f"{RED}{BOLD}FAILED{RESET} | {scenario} | {detail}")


def log_scenario(num: int, title: str) -> None:
    logger.info("")
    logger.info(f"{CYAN}{BOLD}{'-' * 60}{RESET}")
    logger.info(f"{CYAN}{BOLD}  SENARYO {num}: {title}{RESET}")
    logger.info(f"{CYAN}{BOLD}{'-' * 60}{RESET}")


# ── Yardımcı fonksiyonlar ───────────────────────────────────────

async def create_redis() -> aioredis.Redis:
    """Test için Redis bağlantısı oluştur."""
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


async def clean_redis_state(client: aioredis.Redis) -> None:
    """Test öncesi tüm risk state'lerini temizle."""
    await client.delete(ch.PORTFOLIO_STATE, ch.RECOVERY_STATE)
    # Asset state'lerini de temizle
    asset_key = ch.ASSET_STATE.format(symbol=TEST_SYMBOL)
    await client.delete(asset_key)


def make_signal_packet(
    score: float = 75.0,
    entry_price: float = 67_500.0,
    atr: float = 150.0,
    side: str = "BUY",
    raw_points: int = 7,
) -> dict:
    """Test amaçlı SignalPacket JSON'u oluştur."""
    return {
        "symbol": TEST_SYMBOL,
        "exchange": TEST_EXCHANGE,
        "strategy": "DAY_TRADING",
        "side": side,
        "timeframe": "5m",
        "raw_points": raw_points,
        "unified_score": score,
        "min_threshold": 4,
        "entry_price": entry_price,
        "atr": atr,
        "components": {"momentum": 2, "vwap": 2, "rsi": 1, "macd": 2},
        "timestamp": time.time(),
    }


def make_spread_data(spread_pct: float) -> dict:
    """Test amaçlı spread verisi oluştur."""
    return {
        "symbol": TEST_SYMBOL,
        "exchange": TEST_EXCHANGE,
        "bid": 67_500.0,
        "ask": 67_500.0 * (1 + spread_pct / 100),
        "spread_abs": 67_500.0 * spread_pct / 100,
        "spread_pct": spread_pct,
        "timestamp": time.time(),
    }


async def wait_for_message(
    pubsub: aioredis.client.PubSub,
    target_channel: str,
    timeout_sec: float = 5.0,
) -> dict | None:
    """Belirtilen kanaldan bir mesaj bekle (timeout ile)."""
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        msg = await pubsub.get_message(
            ignore_subscribe_messages=True,
            timeout=0.3,
        )
        if msg and msg["type"] == "message" and msg["channel"] == target_channel:
            return json.loads(msg["data"])
    return None


# ── SENARYO 1: Normal Onay ──────────────────────────────────────

async def test_normal_approval(
    pub: aioredis.Redis,
    state: aioredis.Redis,
    result_pubsub: aioredis.client.PubSub,
) -> bool:
    """
    PnL %0, normal spread, 7 puanlık sinyal → ONAY bekleniyor.
    lot_multiplier = 1.0 olmalı.
    """
    log_scenario(1, "Normal Onay (PnL=%0, Normal Spread)")

    # 1) State: Net PnL = 0
    await state.hset(ch.PORTFOLIO_STATE, mapping={
        "net_pnl": "0.0",
        "open_positions": "0",
        "last_update": str(time.time()),
    })
    # Recovery pasif
    await state.hset(ch.RECOVERY_STATE, mapping={
        "active": "0",
        "consecutive_wins": "0",
    })

    # 2) Normal spread verisi gönder (60dk ortalama oluşsun)
    spread_channel = ch.SPREAD_STREAM.format(symbol=TEST_SYMBOL)
    normal_spread = 0.015  # %0.015 — normal spread
    for i in range(15):
        await pub.publish(
            spread_channel,
            json.dumps(make_spread_data(normal_spread)),
        )
        await asyncio.sleep(0.02)

    # Spread tracker'ın verileri işlemesi için kısa bekleme
    await asyncio.sleep(0.5)

    # 3) Sinyal gönder
    signal = make_signal_packet(score=75.0, raw_points=7)
    await pub.publish(ch.SIGNAL_CHANNEL, json.dumps(signal))

    # 4) Sonucu oku
    result = await wait_for_message(result_pubsub, ch.APPROVED_ORDERS, timeout_sec=5.0)

    if result is None:
        # Belki rejected'a düştü?
        log_fail("Senaryo 1", "channel:approved_orders'dan mesaj alınamadı!")
        return False

    # 5) Doğrulama
    ok = True

    if result["symbol"] != TEST_SYMBOL:
        log_fail("Senaryo 1", f"Symbol hatalı: {result['symbol']}")
        ok = False

    if result["lot_multiplier"] != 1.0:
        log_fail(
            "Senaryo 1",
            f"lot_multiplier=={result['lot_multiplier']}, beklenen==1.0",
        )
        ok = False

    if result["risk_level"] != "NORMAL":
        log_fail(
            "Senaryo 1",
            f"risk_level=={result['risk_level']}, beklenen==NORMAL",
        )
        ok = False

    if result["lot_size"] <= 0:
        log_fail("Senaryo 1", f"lot_size <= 0: {result['lot_size']}")
        ok = False

    if result["stop_loss"] <= 0:
        log_fail("Senaryo 1", f"stop_loss <= 0: {result['stop_loss']}")
        ok = False

    if ok:
        log_pass(
            "Senaryo 1",
            f"ONAY | lot_mult={result['lot_multiplier']} | "
            f"lot={result['lot_size']:.6f} | stop={result['stop_loss']:.2f} | "
            f"tp1={result['tp1_price']:.2f} | tp2={result['tp2_price']:.2f}",
        )

    return ok


# ── SENARYO 2: Spread Reddi ─────────────────────────────────────

async def test_spread_rejection(
    pub: aioredis.Redis,
    state: aioredis.Redis,
    result_pubsub: aioredis.client.PubSub,
) -> bool:
    """
    Normal spread ortalaması oluştuktan sonra anlık spread'i 2.5× yükselt.
    Sinyal geldiğinde SPREAD_GATE sebebiyle reddedilmeli.
    """
    log_scenario(2, "Spread Reddi (anlık > 2.0x ortalama)")

    # 1) State temiz
    await state.hset(ch.PORTFOLIO_STATE, mapping={
        "net_pnl": "0.0",
        "open_positions": "0",
    })
    await state.hset(ch.RECOVERY_STATE, "active", "0")

    # 2) Önce normal spread'ler gönder (ortalamayı oluştur)
    spread_channel = ch.SPREAD_STREAM.format(symbol=TEST_SYMBOL)
    normal_spread = 0.015  # %0.015

    for i in range(20):
        await pub.publish(
            spread_channel,
            json.dumps(make_spread_data(normal_spread)),
        )
        await asyncio.sleep(0.02)

    # 3) Şimdi çok yüksek spread gönder (2.5× ortalama)
    high_spread = normal_spread * 2.5  # %0.0375
    for i in range(3):
        await pub.publish(
            spread_channel,
            json.dumps(make_spread_data(high_spread)),
        )
        await asyncio.sleep(0.02)

    await asyncio.sleep(0.5)

    # 4) Sinyal gönder
    signal = make_signal_packet(score=85.0, raw_points=9)
    await pub.publish(ch.SIGNAL_CHANNEL, json.dumps(signal))

    # 5) Sonucu oku — rejected_orders kanalından
    result = await wait_for_message(result_pubsub, ch.REJECTED_ORDERS, timeout_sec=5.0)

    if result is None:
        # Belki approved oldu?
        log_fail(
            "Senaryo 2",
            "channel:rejected_orders'dan mesaj alınamadı! "
            "(Sinyal onaylanmış olabilir — spread gate çalışmadı)",
        )
        return False

    ok = True

    if result["rejection_reason"] != "SPREAD_GATE":
        log_fail(
            "Senaryo 2",
            f"reason=={result['rejection_reason']}, beklenen==SPREAD_GATE",
        )
        ok = False

    if ok:
        log_pass(
            "Senaryo 2",
            f"REDDEDİLDİ | sebep={result['rejection_reason']} | "
            f"detay={result.get('rejection_detail', '')[:80]}",
        )

    return ok


# ── SENARYO 3: Recovery Modu ────────────────────────────────────

async def test_recovery_mode(
    pub: aioredis.Redis,
    state: aioredis.Redis,
    result_pubsub: aioredis.client.PubSub,
) -> bool:
    """
    Recovery modu aktifken sinyal gönder.
    Onaylanmalı ama lot_multiplier = 0.50 olmalı.
    """
    log_scenario(3, "Recovery Modu (lot x0.50)")

    # 1) State: PnL normal, Recovery AKTİF
    await state.hset(ch.PORTFOLIO_STATE, mapping={
        "net_pnl": "0.0",
        "open_positions": "0",
    })
    await state.hset(ch.RECOVERY_STATE, mapping={
        "active": "1",
        "consecutive_wins": "0",
        "triggered_at": str(time.time()),
        "cleared_at": "",
    })

    # 2) Normal spread gönder (gate'den geçsin)
    spread_channel = ch.SPREAD_STREAM.format(symbol=TEST_SYMBOL)
    normal_spread = 0.015

    for i in range(15):
        await pub.publish(
            spread_channel,
            json.dumps(make_spread_data(normal_spread)),
        )
        await asyncio.sleep(0.02)

    await asyncio.sleep(0.5)

    # 3) Sinyal gönder
    signal = make_signal_packet(score=70.0, raw_points=7)
    await pub.publish(ch.SIGNAL_CHANNEL, json.dumps(signal))

    # 4) Sonucu oku
    result = await wait_for_message(result_pubsub, ch.APPROVED_ORDERS, timeout_sec=5.0)

    if result is None:
        log_fail("Senaryo 3", "channel:approved_orders'dan mesaj alınamadı!")
        return False

    ok = True

    # PnL NORMAL (lot×1.0) + Recovery (lot×0.50) = final 0.50
    expected_mult = 0.50
    actual_mult = result["lot_multiplier"]

    if abs(actual_mult - expected_mult) > 0.01:
        log_fail(
            "Senaryo 3",
            f"lot_multiplier=={actual_mult}, beklenen=={expected_mult}",
        )
        ok = False

    if ok:
        log_pass(
            "Senaryo 3",
            f"ONAY | lot_mult={actual_mult} (Recovery aktif) | "
            f"lot={result['lot_size']:.6f} | stop={result['stop_loss']:.2f}",
        )

    return ok


# ── SENARYO 4: Hard Kill ────────────────────────────────────────

async def test_hard_kill(
    pub: aioredis.Redis,
    state: aioredis.Redis,
    result_pubsub: aioredis.client.PubSub,
) -> bool:
    """
    Net PnL = -%11 iken mükemmel sinyal gönder.
    HARD_KILL sebebiyle acımasızca reddedilmeli.
    """
    log_scenario(4, "Hard Kill (PnL = -%11)")

    # 1) State: Net PnL = -0.11 (-%11 kayıp)
    await state.hset(ch.PORTFOLIO_STATE, mapping={
        "net_pnl": "-0.11",
        "open_positions": "0",
    })
    # Recovery'yi kapalı bırak (Hard Kill zaten aktive edecek)
    await state.hset(ch.RECOVERY_STATE, "active", "0")

    # 2) Normal spread (spread gate'e takılmamalı)
    spread_channel = ch.SPREAD_STREAM.format(symbol=TEST_SYMBOL)
    for i in range(15):
        await pub.publish(
            spread_channel,
            json.dumps(make_spread_data(0.015)),
        )
        await asyncio.sleep(0.02)

    await asyncio.sleep(0.5)

    # 3) Mükemmel sinyal gönder (10 puan, 95 unified)
    signal = make_signal_packet(score=95.0, raw_points=10)
    await pub.publish(ch.SIGNAL_CHANNEL, json.dumps(signal))

    # 4) Sonucu oku — rejected_orders kanalından
    result = await wait_for_message(result_pubsub, ch.REJECTED_ORDERS, timeout_sec=5.0)

    if result is None:
        log_fail(
            "Senaryo 4",
            "channel:rejected_orders'dan mesaj alınamadı! "
            "(Sinyal Hard Kill'e rağmen onaylanmış olabilir!)",
        )
        return False

    ok = True

    if result["rejection_reason"] != "PORTFOLIO_RISK":
        log_fail(
            "Senaryo 4",
            f"reason=={result['rejection_reason']}, beklenen==PORTFOLIO_RISK",
        )
        ok = False

    # HARD_KILL detayda görünmeli
    detail = result.get("rejection_detail", "")
    if "HARD KILL" not in detail.upper() and "HARD_KILL" not in detail.upper():
        log_fail(
            "Senaryo 4",
            f"Detayda HARD KILL ifadesi bulunamadı: {detail[:80]}",
        )
        ok = False

    if ok:
        # Recovery modunun da aktive edildiğini kontrol et
        rec_active = await state.hget(ch.RECOVERY_STATE, "active")
        recovery_ok = rec_active == "1"

        log_pass(
            "Senaryo 4",
            f"REDDEDİLDİ | sebep={result['rejection_reason']} | "
            f"detay={detail[:60]} | "
            f"recovery_tetiklendi={'EVET' if recovery_ok else 'HAYIR'}",
        )
        if not recovery_ok:
            log_fail(
                "Senaryo 4",
                "Hard Kill sonrası Recovery modu aktive EDİLMEDİ!",
            )
            ok = False

    return ok


# ── ANA TEST ORKESTRATÖRÜ ───────────────────────────────────────

async def main() -> None:
    logger.info("")
    logger.info(f"{BOLD}{'=' * 60}{RESET}")
    logger.info(f"{BOLD}  Risk Gatekeeper - Entegrasyon Testi (4 Senaryo){RESET}")
    logger.info(f"{BOLD}  Redis: {settings.redis.host}:{settings.redis.port}{RESET}")
    logger.info(f"{BOLD}{'=' * 60}{RESET}")

    # ── Redis bağlantıları ──
    pub = await create_redis()       # Publish (sinyal + spread gönderme)
    state = await create_redis()     # State yönetimi (PnL, Recovery ayarlama)

    # Sonuç dinleyici (approved + rejected)
    result_sub = await create_redis()
    result_pubsub = result_sub.pubsub()
    await result_pubsub.subscribe(
        ch.APPROVED_ORDERS,
        ch.REJECTED_ORDERS,
        ch.SYSTEM_ALERTS,
    )

    # ── Risk Gatekeeper'ı başlat ──
    from services.risk_gatekeeper.gatekeeper import RiskGatekeeper

    gatekeeper = RiskGatekeeper(symbols=[TEST_SYMBOL])
    gk_task = asyncio.create_task(gatekeeper.start())

    # Gatekeeper'ın bağlantıları kurması için bekle
    await asyncio.sleep(1.5)

    logger.info(f"{YELLOW}Risk Gatekeeper başlatıldı, testler başlıyor...{RESET}")

    # ── Testleri çalıştır ──
    results: dict[str, bool] = {}

    try:
        # Her senaryo arasında state temizliği
        await clean_redis_state(state)
        results["Senaryo 1: Normal Onay"] = await test_normal_approval(
            pub, state, result_pubsub
        )

        await asyncio.sleep(0.5)

        results["Senaryo 2: Spread Reddi"] = await test_spread_rejection(
            pub, state, result_pubsub
        )

        await asyncio.sleep(0.5)
        await clean_redis_state(state)

        results["Senaryo 3: Recovery Modu"] = await test_recovery_mode(
            pub, state, result_pubsub
        )

        await asyncio.sleep(0.5)
        await clean_redis_state(state)

        results["Senaryo 4: Hard Kill"] = await test_hard_kill(
            pub, state, result_pubsub
        )

    except Exception as exc:
        logger.exception("Test sırasında beklenmeyen hata: %s", exc)
    finally:
        # ── Temizlik ──
        await gatekeeper.stop()
        gk_task.cancel()
        try:
            await gk_task
        except asyncio.CancelledError:
            pass

        await result_pubsub.unsubscribe()
        await result_pubsub.aclose()

        # Redis state temizle
        await clean_redis_state(state)

        for client in (pub, state, result_sub):
            await client.aclose()

    # ── Sonuç özeti ──
    logger.info("")
    logger.info(f"{BOLD}{'=' * 60}{RESET}")
    logger.info(f"{BOLD}  TEST SONUÇLARI{RESET}")
    logger.info(f"{BOLD}{'=' * 60}{RESET}")

    passed = 0
    failed = 0
    for name, ok in results.items():
        status = f"{GREEN}PASSED{RESET}" if ok else f"{RED}FAILED{RESET}"
        logger.info(f"  {status}  {name}")
        if ok:
            passed += 1
        else:
            failed += 1

    logger.info(f"{BOLD}{'-' * 60}{RESET}")
    total = passed + failed
    if failed == 0:
        logger.info(
            f"  {GREEN}{BOLD}TÜM TESTLER BAŞARILI! "
            f"({passed}/{total}){RESET}"
        )
    else:
        logger.info(
            f"  {RED}{BOLD}{failed} TEST BAŞARISIZ! "
            f"({passed}/{total} geçti){RESET}"
        )

    logger.info(f"{BOLD}{'=' * 60}{RESET}")

    if failed > 0:
        sys.exit(1)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
