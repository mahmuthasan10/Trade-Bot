"""
Master Trading Bot v3.0 - Merkezi Loglama
Her servis bu modülden kendi logger'ını alır.
"""

from __future__ import annotations

import logging
import sys
from typing import Optional


def get_logger(name: str, level: Optional[str] = None) -> logging.Logger:
    """
    Servis bazlı logger oluşturur.

    Kullanım:
        from shared.utils.logger import get_logger
        logger = get_logger("data_feed")
    """
    logger = logging.getLogger(f"trading.{name}")

    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        formatter = logging.Formatter(
            fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        handler.setFormatter(formatter)
        logger.addHandler(handler)

    if level:
        logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    elif logger.level == logging.NOTSET:
        logger.setLevel(logging.INFO)

    return logger
