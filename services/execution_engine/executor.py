"""
Execution Engine - Order Executor

ccxt.pro kullanarak onaylanmış emirleri borsaya asenkron olarak iletir.
Şimdilik Binance Testnet ayarlarıyla çalışır.

Mimari Kuralı:
    - SADECE channel:approved_orders'dan gelen emirleri borsaya gönderir
    - Kendi başına karar ALMAZ — risk kararları Risk Gatekeeper'a aittir
    - Tüm borsa çağrıları exponential backoff ile sarılır
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import Optional

import ccxt.pro as ccxtpro
import redis.asyncio as aioredis
from redis.exceptions import RedisError

from config.settings import settings
from shared.constants import redis_channels as ch
from shared.constants.enums import Exchange, OrderType, Side
from shared.utils.logger import get_logger
from services.execution_engine.models.trade import Position, PositionStatus

logger = get_logger("execution.executor")

# ── Backoff sabitleri ────────────────────────────────────────────
INITIAL_BACKOFF_SEC: float = 1.0
MAX_BACKOFF_SEC: float = 30.0
BACKOFF_MULTIPLIER: float = 2.0
MAX_ORDER_RETRIES: int = 3


class OrderExecutor:
    """
    Borsa ile doğrudan iletişim kuran sınıf.

    Sorumlulukları:
        1. channel:approved_orders kanalını dinlemek
        2. ccxt.pro ile Market/Limit emir göndermek
        3. Fill bilgisini channel:fills kanalına yayınlamak
        4. Açılan pozisyonu PositionManager'a bildirmek
    """

    def __init__(self, position_callback=None) -> None:
        """
        Args:
            position_callback: Yeni pozisyon açıldığında çağrılacak async fonksiyon.
                               Signature: async def callback(position: Position) -> None
        """
        self._redis: Optional[aioredis.Redis] = None
        self._pubsub: Optional[aioredis.client.PubSub] = None
        self._exchange: Optional[ccxtpro.Exchange] = None
        self._position_callback = position_callback
        self._running: bool = False

    # ── Bağlantı Yönetimi ────────────────────────────────────────

    async def _connect_redis(self) -> None:
        """Redis bağlantısı kur."""
        self._redis = aioredis.Redis(
            host=settings.redis.host,
            port=settings.redis.port,
            password=settings.redis.password or None,
            db=settings.redis.db,
            decode_responses=True,
            socket_connect_timeout=5,
        )
        await self._redis.ping()
        logger.info("Redis bağlantısı kuruldu")

    async def _connect_exchange(self) -> None:
        """
        ccxt.pro Binance Testnet bağlantısı kur.
        Canlıya geçişte sadece sandbox=False yapılacak.
        """
        self._exchange = ccxtpro.binance({
            "apiKey": settings.binance.api_key,
            "secret": settings.binance.api_secret,
            "sandbox": settings.binance.testnet,
            "enableRateLimit": True,
            "options": {
                "defaultType": "spot",
            },
        })
        # Piyasa bilgilerini yükle
        await self._exchange.load_markets()
        mode = "TESTNET" if settings.binance.testnet else "CANLI"
        logger.info("Binance %s bağlantısı kuruldu | %d piyasa yüklendi",
                     mode, len(self._exchange.markets))

    async def connect(self) -> None:
        """Tüm bağlantıları başlat."""
        await self._connect_redis()
        await self._connect_exchange()

    # ── Ana Dinleme Döngüsü ──────────────────────────────────────

    async def start(self) -> None:
        """
        channel:approved_orders kanalını dinle ve gelen emirleri işle.
        """
        await self.connect()
        self._running = True

        self._pubsub = self._redis.pubsub()
        await self._pubsub.subscribe(ch.APPROVED_ORDERS)
        logger.info("Dinleniyor: %s", ch.APPROVED_ORDERS)

        backoff = INITIAL_BACKOFF_SEC

        while self._running:
            try:
                message = await self._pubsub.get_message(
                    ignore_subscribe_messages=True,
                    timeout=1.0,
                )
                if message and message["type"] == "message":
                    data = json.loads(message["data"])
                    asyncio.create_task(self._process_order(data))

                backoff = INITIAL_BACKOFF_SEC  # Başarılı okumada sıfırla

            except (RedisError, OSError) as exc:
                logger.error("Redis dinleme hatası: %s | %ss sonra yeniden deneniyor", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * BACKOFF_MULTIPLIER, MAX_BACKOFF_SEC)

                # Yeniden bağlan
                try:
                    await self._connect_redis()
                    self._pubsub = self._redis.pubsub()
                    await self._pubsub.subscribe(ch.APPROVED_ORDERS)
                except Exception:
                    pass

    async def stop(self) -> None:
        """Graceful shutdown."""
        self._running = False
        logger.info("Executor durduruluyor...")

        if self._pubsub:
            await self._pubsub.unsubscribe(ch.APPROVED_ORDERS)
            await self._pubsub.aclose()
            self._pubsub = None

        if self._exchange:
            await self._exchange.close()
            self._exchange = None

        if self._redis:
            await self._redis.aclose()
            self._redis = None

        logger.info("Executor durduruldu")

    # ── Emir İşleme ─────────────────────────────────────────────

    async def _process_order(self, order_data: dict) -> None:
        """
        Onaylı emri borsaya ilet.

        1. ccxt ile Market/Limit emir gönder
        2. Fill bilgisini Redis'e yayınla
        3. Position nesnesini oluştur ve callback ile bildir
        """
        symbol = order_data["symbol"]
        side_str = order_data["side"].lower()   # ccxt "buy"/"sell" bekler
        order_type = order_data.get("order_type", "MARKET")
        lot_size = order_data["lot_size"]
        entry_price = order_data["entry_price"]

        order_id = f"exec_{uuid.uuid4().hex[:12]}"

        logger.info(
            "EMİR İŞLENİYOR | %s | %s %s | miktar=%.6f | fiyat=%.4f | tip=%s",
            order_id, side_str.upper(), symbol, lot_size, entry_price, order_type,
        )

        try:
            result = await self._execute_with_retry(
                symbol=symbol,
                side=side_str,
                order_type=order_type,
                amount=lot_size,
                price=entry_price if order_type == "LIMIT" else None,
            )

            fill_price = result.get("average", result.get("price", entry_price))
            filled_amount = result.get("filled", lot_size)
            exchange_order_id = result.get("id", "")

            logger.info(
                "EMİR GERÇEKLEŞTİ | %s | borsa_id=%s | fill=%.4f | miktar=%.6f",
                order_id, exchange_order_id, fill_price, filled_amount,
            )

            # Fill bilgisini Redis'e yayınla
            fill_payload = {
                "order_id": order_id,
                "exchange_order_id": exchange_order_id,
                "symbol": symbol,
                "side": order_data["side"],
                "fill_price": fill_price,
                "filled_amount": filled_amount,
                "timestamp": time.time(),
            }
            await self._publish(ch.FILL_CHANNEL, fill_payload)

            # Position nesnesi oluştur ve PositionManager'a bildir
            position = Position(
                order_id=order_id,
                symbol=symbol,
                exchange=Exchange(order_data["exchange"]),
                strategy=order_data["strategy"],
                side=Side(order_data["side"]),
                timeframe=order_data["timeframe"],
                entry_price=fill_price,
                total_quantity=filled_amount,
                remaining_quantity=filled_amount,
                stop_loss=order_data["stop_loss"],
                tp1_price=order_data["tp1_price"],
                tp2_price=order_data["tp2_price"],
                tp3_trailing_atr=order_data["tp3_trailing_atr"],
                atr_value=order_data["atr_value"],
                status=PositionStatus.OPEN,
                entry_time=time.time(),
            )

            if self._position_callback:
                await self._position_callback(position)

        except Exception as exc:
            logger.error(
                "EMİR BAŞARISIZ | %s | %s %s | hata=%s",
                order_id, side_str.upper(), symbol, exc,
            )

    async def _execute_with_retry(
        self,
        symbol: str,
        side: str,
        order_type: str,
        amount: float,
        price: Optional[float] = None,
    ) -> dict:
        """
        Borsa emri gönder — exponential backoff ile yeniden deneme.
        """
        backoff = INITIAL_BACKOFF_SEC

        for attempt in range(1, MAX_ORDER_RETRIES + 1):
            try:
                if self._exchange is None:
                    raise RuntimeError("Borsa bağlantısı yok")

                if order_type == "LIMIT" and price is not None:
                    result = await self._exchange.create_limit_order(
                        symbol=symbol,
                        side=side,
                        amount=amount,
                        price=price,
                    )
                else:
                    result = await self._exchange.create_market_order(
                        symbol=symbol,
                        side=side,
                        amount=amount,
                    )
                return result

            except Exception as exc:
                if attempt == MAX_ORDER_RETRIES:
                    raise

                logger.warning(
                    "Emir gönderim hatası (deneme %d/%d) | %ss sonra tekrar | hata=%s",
                    attempt, MAX_ORDER_RETRIES, backoff, exc,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * BACKOFF_MULTIPLIER, MAX_BACKOFF_SEC)

        raise RuntimeError("Maksimum deneme aşıldı")

    # ── Yardımcı: Redis Publish ──────────────────────────────────

    async def _publish(self, channel: str, data: dict) -> None:
        """Redis kanalına JSON yayınla."""
        try:
            if self._redis:
                await self._redis.publish(channel, json.dumps(data))
        except RedisError as exc:
            logger.error("Redis publish hatası | channel=%s | %s", channel, exc)

    # ── Kısmi kapanış emri (PositionManager tarafından çağrılır) ──

    async def close_position(
        self,
        symbol: str,
        side: Side,
        quantity: float,
        reason: str,
    ) -> Optional[dict]:
        """
        Pozisyonun bir kısmını veya tamamını kapat.

        Args:
            symbol: İşlem çifti (örn: "BTC/USDT")
            side: Orijinal pozisyonun yönü (kapanış ters yöndedir)
            quantity: Kapatılacak miktar
            reason: Kapanış nedeni (TP1, TP2, STOP_LOSS vb.)
        """
        # Kapanış yönü: orijinalin tersi
        close_side = "sell" if side == Side.BUY else "buy"

        logger.info(
            "KAPANIŞ EMRİ | %s %s | miktar=%.6f | neden=%s",
            close_side.upper(), symbol, quantity, reason,
        )

        try:
            result = await self._execute_with_retry(
                symbol=symbol,
                side=close_side,
                order_type="MARKET",
                amount=quantity,
            )
            return result
        except Exception as exc:
            logger.error("Kapanış emri başarısız | %s | %s", symbol, exc)
            return None
