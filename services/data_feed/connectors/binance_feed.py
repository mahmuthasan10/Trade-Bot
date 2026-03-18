"""
Data Feed Service - Binance WebSocket Connector
ccxt.pro async WebSocket üzerinden ticker verisi dinler.
SADECE veri alır ve normalize eder — indikatör/mum/DB işlemi YAPMAZ.
"""

from __future__ import annotations

import asyncio
import time
from typing import AsyncIterator, Optional

import ccxt.pro as ccxtpro

from config.settings import settings
from shared.constants.enums import Exchange
from shared.utils.logger import get_logger
from services.data_feed.connectors.base import BaseExchangeConnector
from services.data_feed.models.tick import MarketDataPacket, NormalizedTick, OrderBookData

logger = get_logger("data_feed.binance")


class BinanceFeedConnector(BaseExchangeConnector):
    """
    Binance ccxt.pro WebSocket connector.
    watch_tickers() ile birden fazla sembolü eşzamanlı dinler.
    """

    def __init__(self, symbols: list[str], testnet: bool = True) -> None:
        super().__init__(exchange=Exchange.BINANCE, symbols=symbols)
        self._exchange: Optional[ccxtpro.binance] = None
        self._testnet = testnet
        self._orderbook_depth: int = 10

    async def _connect(self) -> None:
        config: dict = {
            "enableRateLimit": True,
            "options": {
                "defaultType": "spot",
                "watchTickers": {"maxSubscriptions": 50},
            },
        }

        api_key = settings.binance.api_key
        api_secret = settings.binance.api_secret
        if api_key and api_secret:
            config["apiKey"] = api_key
            config["secret"] = api_secret

        self._exchange = ccxtpro.binance(config)

        if self._testnet:
            self._exchange.set_sandbox_mode(True)
            logger.info("Binance TESTNET modu aktif")

    async def _listen(self) -> AsyncIterator[MarketDataPacket]:
        if self._exchange is None:
            raise RuntimeError("Exchange bağlantısı kurulmadı")

        while self._running:
            try:
                # watch_tickers: birden fazla sembolü tek WebSocket'te dinler
                tickers: dict = await self._exchange.watch_tickers(self.symbols)

                for symbol, data in tickers.items():
                    if symbol not in self.symbols:
                        continue

                    order_book = await self._exchange.watch_order_book(
                        symbol,
                        limit=self._orderbook_depth,
                    )

                    packet = self._normalize_packet(
                        symbol=symbol,
                        ticker_raw=data,
                        orderbook_raw=order_book,
                    )
                    if packet is not None:
                        yield packet

            except asyncio.CancelledError:
                raise

            except Exception as exc:
                logger.warning("Binance listen hatası: %s", exc)
                raise  # base.stream() yakalayıp reconnect edecek

    def _normalize_packet(
        self,
        symbol: str,
        ticker_raw: dict,
        orderbook_raw: dict,
    ) -> Optional[MarketDataPacket]:
        """Ticker + orderbook verisini normalize tek pakete dönüştürür."""
        try:
            bids_raw = orderbook_raw.get("bids", [])
            asks_raw = orderbook_raw.get("asks", [])
            bids = [[float(level[0]), float(level[1])] for level in bids_raw[: self._orderbook_depth]]
            asks = [[float(level[0]), float(level[1])] for level in asks_raw[: self._orderbook_depth]]

            best_bid = bids[0][0] if bids else float(ticker_raw.get("bid", 0) or 0)
            best_ask = asks[0][0] if asks else float(ticker_raw.get("ask", 0) or 0)
            last = float(ticker_raw.get("last", 0) or 0)

            if last <= 0:
                return None

            timestamp = (
                float(ticker_raw.get("timestamp", 0) or 0) / 1000
                if ticker_raw.get("timestamp")
                else time.time()
            )

            tick = NormalizedTick(
                symbol=symbol,
                exchange=Exchange.BINANCE,
                price=last,
                bid=best_bid,
                ask=best_ask,
                volume_24h=float(ticker_raw.get("quoteVolume", 0) or 0),
                timestamp=timestamp,
                bid_volume=float(ticker_raw.get("bidVolume", 0) or 0) or None,
                ask_volume=float(ticker_raw.get("askVolume", 0) or 0) or None,
            )

            orderbook = OrderBookData(
                symbol=symbol,
                exchange=Exchange.BINANCE,
                bids=bids,
                asks=asks,
                bid=best_bid,
                ask=best_ask,
                timestamp=timestamp,
            )

            return MarketDataPacket(tick=tick, orderbook=orderbook)
        except (ValueError, TypeError, KeyError) as exc:
            logger.debug("Normalize hatası [%s]: %s", symbol, exc)
            return None

    async def _close(self) -> None:
        if self._exchange is not None:
            try:
                await self._exchange.close()
            except Exception as exc:
                logger.debug("Binance kapatma hatası: %s", exc)
            finally:
                self._exchange = None
