"""
Data Feed Service - Feed Manager
Birden fazla borsa connector'ını orkestre eder.
Her connector'dan gelen tick'leri Redis'e publish eder.
Heartbeat döngüsünü yönetir.
"""

from __future__ import annotations

import asyncio

from shared.utils.logger import get_logger
from services.data_feed.connectors.base import BaseExchangeConnector
from services.data_feed.models.tick import SpreadData
from services.data_feed.publisher import RedisPublisher, HEARTBEAT_INTERVAL_SEC

logger = get_logger("data_feed.manager")


class FeedManager:
    """
    Tüm exchange feed'lerini başlatır, tick→Redis pipeline'ını çalıştırır.
    Graceful shutdown desteği vardır.
    """

    def __init__(self, connectors: list[BaseExchangeConnector]) -> None:
        self._connectors = connectors
        self._publisher = RedisPublisher()
        self._tasks: list[asyncio.Task] = []

    async def start(self) -> None:
        """Tüm feed'leri ve heartbeat'i başlatır."""
        await self._publisher.connect()

        for connector in self._connectors:
            task = asyncio.create_task(
                self._run_feed(connector),
                name=f"feed-{connector.exchange.value}",
            )
            self._tasks.append(task)

        # Heartbeat döngüsü
        hb_task = asyncio.create_task(
            self._heartbeat_loop(),
            name="heartbeat",
        )
        self._tasks.append(hb_task)

        logger.info(
            "FeedManager başlatıldı | %d connector aktif",
            len(self._connectors),
        )

        # Tüm task'ler bitene kadar bekle
        try:
            await asyncio.gather(*self._tasks)
        except asyncio.CancelledError:
            logger.info("FeedManager iptal edildi")

    async def _run_feed(self, connector: BaseExchangeConnector) -> None:
        """Tek bir connector'ın stream'ini dinler ve Redis'e publish eder."""
        async for packet in connector.stream():
            try:
                # Tick publish
                tick = packet.tick
                await self._publisher.publish_tick(tick)
                await self._publisher.publish_orderbook(packet.orderbook)

                # Spread hesapla ve publish
                spread = SpreadData.from_tick(tick)
                await self._publisher.publish_spread(spread)

            except Exception as exc:
                logger.error(
                    "Publish hatası [%s]: %s",
                    tick.symbol,
                    exc,
                )
                # Publish hatası feed'i durdurmaz, devam et

    async def _heartbeat_loop(self) -> None:
        """Periyodik heartbeat gönderir."""
        while True:
            try:
                await self._publisher.send_heartbeat()
            except Exception as exc:
                logger.debug("Heartbeat hatası: %s", exc)
            await asyncio.sleep(HEARTBEAT_INTERVAL_SEC)

    async def stop(self) -> None:
        """Tüm feed'leri ve publisher'ı temiz kapatır."""
        logger.info("FeedManager durduruluyor...")

        for connector in self._connectors:
            await connector.stop()

        for task in self._tasks:
            task.cancel()

        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self._publisher.close()
        self._tasks.clear()
        logger.info("FeedManager durduruldu")
