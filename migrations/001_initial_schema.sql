-- TimescaleDB uzantisini aktif et
CREATE EXTENSION IF NOT EXISTS timescaledb;

-- Tick verileri (ham fiyat akisi)
CREATE TABLE IF NOT EXISTS ticks (
    time        TIMESTAMPTZ NOT NULL,
    symbol      TEXT        NOT NULL,
    exchange    TEXT        NOT NULL,
    price       DOUBLE PRECISION NOT NULL,
    volume      DOUBLE PRECISION NOT NULL,
    bid         DOUBLE PRECISION,
    ask         DOUBLE PRECISION
);

SELECT create_hypertable('ticks', 'time', if_not_exists => TRUE);

CREATE INDEX IF NOT EXISTS idx_ticks_symbol_time ON ticks (symbol, time DESC);

-- OHLCV mumlari (5m, 15m vb.)
CREATE TABLE IF NOT EXISTS candles (
    time        TIMESTAMPTZ      NOT NULL,
    symbol      TEXT             NOT NULL,
    exchange    TEXT             NOT NULL,
    timeframe   TEXT             NOT NULL,  -- '5m', '15m', '1h', '4h', '1d', '1w'
    open        DOUBLE PRECISION NOT NULL,
    high        DOUBLE PRECISION NOT NULL,
    low         DOUBLE PRECISION NOT NULL,
    close       DOUBLE PRECISION NOT NULL,
    volume      DOUBLE PRECISION NOT NULL
);

SELECT create_hypertable('candles', 'time', if_not_exists => TRUE);

CREATE INDEX IF NOT EXISTS idx_candles_symbol_tf_time ON candles (symbol, timeframe, time DESC);

-- Islem gecmisi (trade log) — Execution Engine db_logger semasi
CREATE TABLE IF NOT EXISTS trades (
    id                  BIGSERIAL,
    order_id            TEXT NOT NULL,
    symbol              TEXT NOT NULL,
    exchange            TEXT NOT NULL,
    strategy            TEXT NOT NULL,
    side                TEXT NOT NULL,
    entry_price         DOUBLE PRECISION NOT NULL,
    exit_price          DOUBLE PRECISION NOT NULL,
    quantity            DOUBLE PRECISION NOT NULL,
    pnl                 DOUBLE PRECISION NOT NULL,
    pnl_pct             DOUBLE PRECISION NOT NULL,
    close_reason        TEXT NOT NULL,
    entry_time          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    exit_time           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    atr_value           DOUBLE PRECISION,
    stop_loss           DOUBLE PRECISION,
    tp1_price           DOUBLE PRECISION,
    tp2_price           DOUBLE PRECISION,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

SELECT create_hypertable('trades', 'exit_time', if_not_exists => TRUE, migrate_data => TRUE);

CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades (symbol);
CREATE INDEX IF NOT EXISTS idx_trades_strategy ON trades (strategy);
CREATE INDEX IF NOT EXISTS idx_trades_order_id ON trades (order_id);
CREATE INDEX IF NOT EXISTS idx_trades_exit_time ON trades (exit_time DESC);

-- Veri saklama politikasi: tick verileri 30 gun, mumlar suresiz
SELECT add_retention_policy('ticks', INTERVAL '30 days', if_not_exists => TRUE);
