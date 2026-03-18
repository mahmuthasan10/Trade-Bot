"""
Master Trading Bot v3.0 - Sistem Geneli Enum Tanımları
"""

from enum import Enum, IntEnum


class Exchange(str, Enum):
    BINANCE = "binance"
    BYBIT = "bybit"
    ALPACA = "alpaca"


class Strategy(str, Enum):
    UNIVERSAL = "UNIVERSAL"       # Swing Trading
    DAY_TRADING = "DAY_TRADING"   # Scalping
    FIRSAT = "FIRSAT"             # Fırsat (Opportunity)


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"


class Timeframe(str, Enum):
    M5 = "5m"
    M15 = "15m"
    H1 = "1h"
    H4 = "4h"
    D1 = "1d"
    W1 = "1w"


class RiskLevel(IntEnum):
    """Portfolio Risk Katmanı - Kümülatif Net PnL bazlı"""
    NORMAL = 0          # 0 - %2.5 kayıp
    CAUTION = 1         # %2.5 - %5 kayıp  → lot -20%
    WARNING = 2         # %5 - %8 kayıp    → kısıtlı işlem
    EMERGENCY = 3       # %8 - %10 kayıp   → lot -40%
    HARD_KILL = 4       # >%10 kayıp       → SİSTEM KAPANIR


class FirsatMode(str, Enum):
    """Fırsat Bot - Kümülatif günlük PnL bazlı mod"""
    NORMAL = "NORMAL"           # %0-1 kazanç
    FREE = "FREE"               # %1-2 kazanç
    AGGRESSIVE = "AGGRESSIVE"   # %2-3 kazanç
    MOMENTUM = "MOMENTUM"       # %3+ kazanç
