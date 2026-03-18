"""
Entegrasyon Testi: Data Feed → Redis Pub/Sub pipeline kontrolü.
Sahte (mock) tick verisi üretir, Redis'e publish eder, subscriber ile doğrular.

Kullanım:
    py -3 scripts/test_feed_redis.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import time

import redis.asyncio as aioredis

# Proje kökünü path'e ekle
sys.path.insert(0, ".")

from config.settings import settings
from shared.constants import redis_channels as ch
from shared.constants.enums import Exchange
from shared.utils.logger import get_logger
from services.data_feed.models.tick import NormalizedTick, OrderBookData, SpreadData
from services.data_feed.publisher import RedisPublisher

logger = get_logger("test.feed_redis")

TEST_SYMBOL = "BTC/USDT"


async def test_publish_subscribe() -> bool:
    """Tick ve Spread publish/subscribe döngüsünü test eder."""
    received: dict[str, list] = {"tick": [], "spread": [], "orderbook": []}

    # ── Subscriber ───────────────────────────────────────────────
    sub_client = aioredis.Redis(
        host=settings.redis.host,
        port=settings.redis.port,
        decode_responses=True,
    )
    pubsub = sub_client.pubsub()

    tick_channel = ch.TICK_STREAM.format(symbol=TEST_SYMBOL)
    spread_channel = ch.SPREAD_STREAM.format(symbol=TEST_SYMBOL)
    orderbook_channel = ch.ORDERBOOK_STREAM.format(symbol=TEST_SYMBOL)
    await pubsub.subscribe(tick_channel, spread_channel, orderbook_channel)

    # ── Publisher ────────────────────────────────────────────────
    publisher = RedisPublisher()
    await publisher.connect()

    # Sahte tick üret
    fake_tick = NormalizedTick(
        symbol=TEST_SYMBOL,
        exchange=Exchange.BINANCE,
        price=67_500.25,
        bid=67_500.00,
        ask=67_500.50,
        volume_24h=1_200_000_000.0,
        timestamp=time.time(),
    )

    fake_spread = SpreadData.from_tick(fake_tick)
    fake_orderbook = OrderBookData(
        symbol=TEST_SYMBOL,
        exchange=Exchange.BINANCE,
        bids=[[67_500.00, 1.5], [67_499.95, 2.0]],
        asks=[[67_500.50, 1.2], [67_500.55, 3.1]],
        bid=67_500.00,
        ask=67_500.50,
        timestamp=time.time(),
    )

    # Publish
    await publisher.publish_tick(fake_tick)
    await publisher.publish_spread(fake_spread)
    await publisher.publish_orderbook(fake_orderbook)
    await publisher.send_heartbeat()

    logger.info("Mesajlar publish edildi, subscriber kontrol ediliyor...")

    # Mesajları oku (timeout ile)
    deadline = time.time() + 3.0
    while time.time() < deadline and (
        not received["tick"] or not received["spread"] or not received["orderbook"]
    ):
        msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.5)
        if msg and msg["type"] == "message":
            data = json.loads(msg["data"])
            if msg["channel"] == tick_channel:
                received["tick"].append(data)
            elif msg["channel"] == spread_channel:
                received["spread"].append(data)
            elif msg["channel"] == orderbook_channel:
                received["orderbook"].append(data)

    # Temizlik
    await pubsub.unsubscribe()
    await pubsub.aclose()
    await sub_client.aclose()
    await publisher.close()

    # ── Doğrulama ────────────────────────────────────────────────
    success = True

    if received["tick"]:
        t = received["tick"][0]
        assert t["symbol"] == TEST_SYMBOL, f"Symbol hatalı: {t['symbol']}"
        assert t["price"] == 67_500.25, f"Price hatalı: {t['price']}"
        assert t["exchange"] == "binance", f"Exchange hatalı: {t['exchange']}"
        logger.info("TICK OK | price=%.2f bid=%.2f ask=%.2f", t["price"], t["bid"], t["ask"])
    else:
        logger.error("TICK HATA | Mesaj alınamadı!")
        success = False

    if received["spread"]:
        s = received["spread"][0]
        assert s["spread_abs"] > 0, f"Spread negatif: {s['spread_abs']}"
        logger.info(
            "SPREAD OK | abs=%.8f pct=%.6f%%",
            s["spread_abs"],
            s["spread_pct"],
        )
    else:
        logger.error("SPREAD HATA | Mesaj alınamadı!")
        success = False

    if received["orderbook"]:
        ob = received["orderbook"][0]
        assert ob["bid"] == 67_500.00, f"Orderbook bid hatalı: {ob['bid']}"
        assert ob["ask"] == 67_500.50, f"Orderbook ask hatalı: {ob['ask']}"
        logger.info(
            "ORDERBOOK OK | bid=%.2f ask=%.2f levels=(%d/%d)",
            ob["bid"],
            ob["ask"],
            len(ob["bids"]),
            len(ob["asks"]),
        )
    else:
        logger.error("ORDERBOOK HATA | Mesaj alınamadı!")
        success = False

    return success


async def main() -> None:
    logger.info("=" * 50)
    logger.info("Data Feed → Redis Pub/Sub Entegrasyon Testi")
    logger.info("=" * 50)

    try:
        ok = await test_publish_subscribe()
    except Exception as exc:
        logger.error("Test hatası: %s", exc)
        ok = False

    logger.info("=" * 50)
    if ok:
        logger.info("TÜM TESTLER BAŞARILI!")
    else:
        logger.error("TESTLERDE HATA VAR!")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
