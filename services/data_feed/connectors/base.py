"""
Data Feed Service - Abstract Base Connector
Her borsa connector'ı bu sınıfı miras alır.
Exponential backoff ile auto-reconnect mantığı burada merkezi olarak tanımlıdır.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from typing import AsyncIterator

from shared.constants.enums import Exchange
from shared.utils.logger import get_logger
from services.data_feed.models.tick import MarketDataPacket

logger = get_logger("data_feed.base")

# ── Exponential Backoff Sabitleri ────────────────────────────────
INITIAL_BACKOFF_SEC: float = 1.0
MAX_BACKOFF_SEC: float = 60.0
BACKOFF_MULTIPLIER: float = 2.0


class BaseExchangeConnector(ABC):
    """
    Borsa WebSocket bağlantısı için soyut temel sınıf.
    Alt sınıflar sadece _connect() ve _listen() metodlarını implemente eder.
    Reconnect mantığı bu sınıfta çözülür.
    """

    def __init__(self, exchange: Exchange, symbols: list[str]) -> None:
        self.exchange = exchange
        self.symbols = symbols
        self._running: bool = False
        self._backoff: float = INITIAL_BACKOFF_SEC

    def _reset_backoff(self) -> None:
        self._backoff = INITIAL_BACKOFF_SEC

    def _next_backoff(self) -> float:
        current = self._backoff
        self._backoff = min(self._backoff * BACKOFF_MULTIPLIER, MAX_BACKOFF_SEC)
        return current

    @abstractmethod
    async def _connect(self) -> None:
        """Borsaya WebSocket bağlantısı kur (ccxt.pro exchange instance)."""
        ...

    @abstractmethod
    async def _listen(self) -> AsyncIterator[MarketDataPacket]:
        """Bağlantı üzerinden tick verilerini yield et."""
        ...
        yield  # type: ignore[misc]

    @abstractmethod
    async def _close(self) -> None:
        """Bağlantıyı temiz kapat."""
        ...

    async def stream(self) -> AsyncIterator[MarketDataPacket]:
        """
        Ana akış döngüsü — auto-reconnect ile sonsuz çalışır.
        Bağlantı koparsa exponential backoff ile yeniden bağlanır.
        """
        self._running = True

        while self._running:
            try:
                await self._connect()
                self._reset_backoff()
                logger.info(
                    "%s bağlantısı kuruldu | semboller: %s",
                    self.exchange.value,
                    self.symbols,
                )

                async for packet in self._listen():
                    if not self._running:
                        break
                    yield packet

            except asyncio.CancelledError:
                logger.info("%s stream iptal edildi", self.exchange.value)
                self._running = False
                break

            except Exception as exc:
                wait = self._next_backoff()
                logger.warning(
                    "%s bağlantı hatası: %s | %ss sonra yeniden denenecek",
                    self.exchange.value,
                    exc,
                    wait,
                )
                await asyncio.sleep(wait)

            finally:
                try:
                    await self._close()
                except Exception as close_err:
                    logger.debug("Kapatma hatası (görmezden geliniyor): %s", close_err)

    async def stop(self) -> None:
        """Dışarıdan durdurma sinyali."""
        self._running = False
        logger.info("%s connector durduruluyor", self.exchange.value)
