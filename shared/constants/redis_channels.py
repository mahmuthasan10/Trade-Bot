"""
Master Trading Bot v3.0 - Redis Pub/Sub Kanal Tanımları
Tüm servisler bu merkezi kanal isimlerini kullanır.
Hardcode kanal ismi YASAKTIR.
"""

# ── Data Feed Service → Strategy Engine ──────────────────────────
TICK_STREAM = "stream:ticks:{symbol}"          # Her sembol için tick akışı
ORDERBOOK_STREAM = "stream:orderbook:{symbol}" # Derinlik verisi
SPREAD_STREAM = "stream:spread:{symbol}"       # Anlık spread

# ── Strategy Engine → Risk Gatekeeper ────────────────────────────
SIGNAL_CHANNEL = "channel:signals"             # Sinyal skorları (0-100)

# ── Risk Gatekeeper → Execution Engine ───────────────────────────
APPROVED_ORDERS = "channel:approved_orders"    # Onaylanan emirler
REJECTED_ORDERS = "channel:rejected_orders"    # Reddedilen emirler

# ── Execution Engine → Tüm Sistem ───────────────────────────────
FILL_CHANNEL = "channel:fills"                 # Gerçekleşen işlemler
POSITION_UPDATE = "channel:positions"          # Pozisyon güncellemeleri

# ── Sistem Geneli ────────────────────────────────────────────────
SYSTEM_ALERTS = "channel:alerts"               # Kill switch, recovery vb.
HEARTBEAT = "channel:heartbeat"                # Servis sağlık kontrolü

# ── Redis Hash Keys (State) ──────────────────────────────────────
PORTFOLIO_STATE = "state:portfolio"            # Kümülatif Net PnL, mod vb.
ASSET_STATE = "state:asset:{symbol}"           # Varlık bazlı durum
RECOVERY_STATE = "state:recovery"              # Recovery mode bilgisi
