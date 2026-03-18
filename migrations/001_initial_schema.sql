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

-- Islem gecmisi (trade log)
CREATE TABLE IF NOT EXISTS trades (
    id              BIGSERIAL PRIMARY KEY,
    time            TIMESTAMPTZ      NOT NULL DEFAULT NOW(),
    symbol          TEXT             NOT NULL,
    exchange        TEXT             NOT NULL,
    strategy        TEXT             NOT NULL,  -- 'UNIVERSAL', 'DAY_TRADING', 'FIRSAT'
    side            TEXT             NOT NULL,  -- 'BUY', 'SELL'
    order_type      TEXT             NOT NULL,  -- 'MARKET', 'LIMIT'
    price           DOUBLE PRECISION NOT NULL,
    quantity        DOUBLE PRECISION NOT NULL,
    commission      DOUBLE PRECISION DEFAULT 0,
    pnl             DOUBLE PRECISION,
    signal_score    INTEGER,
    metadata        JSONB
);

CREATE INDEX IF NOT EXISTS idx_trades_strategy_time ON trades (strategy, time DESC);
CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades (symbol, time DESC);

-- Veri saklama politikasi: tick verileri 30 gun, mumlar suresiz
SELECT add_retention_policy('ticks', INTERVAL '30 days', if_not_exists => TRUE);
