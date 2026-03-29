"""
Master Trading Bot v3.0 - Merkezi Konfigürasyon
Tüm servisler bu modül üzerinden ortam değişkenlerini okur.
API anahtarları ASLA hardcode edilmez.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

# .env dosyasını yükle (proje kök dizininden)
_ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(dotenv_path=_ENV_PATH)


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def _env_int(key: str, default: int = 0) -> int:
    return int(os.environ.get(key, default))


def _env_float(key: str, default: float = 0.0) -> float:
    return float(os.environ.get(key, default))


def _env_bool(key: str, default: bool = False) -> bool:
    return os.environ.get(key, str(default)).lower() in ("true", "1", "yes")


# ── Redis ────────────────────────────────────────────────────────
@dataclass(frozen=True)
class RedisConfig:
    host: str = field(default_factory=lambda: _env("REDIS_HOST", "localhost"))
    port: int = field(default_factory=lambda: _env_int("REDIS_PORT", 6381))
    password: str = field(default_factory=lambda: _env("REDIS_PASSWORD", ""))
    db: int = field(default_factory=lambda: _env_int("REDIS_DB", 0))

    @property
    def url(self) -> str:
        auth = f":{self.password}@" if self.password else ""
        return f"redis://{auth}{self.host}:{self.port}/{self.db}"


# ── PostgreSQL / TimescaleDB ─────────────────────────────────────
@dataclass(frozen=True)
class PostgresConfig:
    host: str = field(default_factory=lambda: _env("PG_HOST", "localhost"))
    port: int = field(default_factory=lambda: _env_int("PG_PORT", 5433))
    user: str = field(default_factory=lambda: _env("PG_USER"))
    password: str = field(default_factory=lambda: _env("PG_PASSWORD"))
    database: str = field(default_factory=lambda: _env("PG_DB", "trading_bot"))

    @property
    def dsn(self) -> str:
        return (
            f"postgresql://{self.user}:{self.password}"
            f"@{self.host}:{self.port}/{self.database}"
        )


# ── Borsa API'leri ───────────────────────────────────────────────
@dataclass(frozen=True)
class BinanceConfig:
    api_key: str = field(default_factory=lambda: _env("BINANCE_API_KEY"))
    api_secret: str = field(default_factory=lambda: _env("BINANCE_API_SECRET"))
    testnet: bool = field(default_factory=lambda: _env_bool("BINANCE_TESTNET", True))


@dataclass(frozen=True)
class BybitConfig:
    api_key: str = field(default_factory=lambda: _env("BYBIT_API_KEY"))
    api_secret: str = field(default_factory=lambda: _env("BYBIT_API_SECRET"))
    testnet: bool = field(default_factory=lambda: _env_bool("BYBIT_TESTNET", True))


@dataclass(frozen=True)
class AlpacaConfig:
    api_key: str = field(default_factory=lambda: _env("ALPACA_API_KEY"))
    api_secret: str = field(default_factory=lambda: _env("ALPACA_API_SECRET"))
    base_url: str = field(
        default_factory=lambda: _env(
            "ALPACA_BASE_URL", "https://paper-api.alpaca.markets"
        )
    )


# ── Ana Konfigürasyon ───────────────────────────────────────────
@dataclass(frozen=True)
class Settings:
    redis: RedisConfig = field(default_factory=RedisConfig)
    postgres: PostgresConfig = field(default_factory=PostgresConfig)
    binance: BinanceConfig = field(default_factory=BinanceConfig)
    bybit: BybitConfig = field(default_factory=BybitConfig)
    alpaca: AlpacaConfig = field(default_factory=AlpacaConfig)

    # Genel ayarlar
    log_level: str = field(default_factory=lambda: _env("LOG_LEVEL", "INFO"))
    environment: str = field(default_factory=lambda: _env("ENVIRONMENT", "development"))


def _validate_settings(s: Settings) -> None:
    """Başlangıçta kritik ayarları doğrula."""
    import sys

    if s.environment == "production":
        errors: list[str] = []
        if not s.binance.api_key or not s.binance.api_secret:
            errors.append("BINANCE_API_KEY ve BINANCE_API_SECRET production ortamında zorunludur")
        if s.binance.testnet:
            errors.append("BINANCE_TESTNET=true iken ENVIRONMENT=production olamaz")
        if errors:
            for e in errors:
                print(f"[HATA] {e}", file=sys.stderr)
            sys.exit(1)

    if not s.binance.testnet and (not s.binance.api_key or not s.binance.api_secret):
        print(
            "[UYARI] BINANCE_TESTNET=false ama API key boş! "
            "Gerçek işlem yapılamaz.",
            file=sys.stderr,
        )


# Singleton erişim
settings = Settings()
_validate_settings(settings)
