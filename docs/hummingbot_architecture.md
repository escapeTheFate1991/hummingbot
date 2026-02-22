# Hummingbot Fork Architecture — Hyperliquid Integration

## Overview

Our fork is based on Hummingbot v2.12.0. This document maps the data flow from
market data → strategy → order → execution for the Hyperliquid perpetuals connector.

---

## 1. Connector Architecture

### Connector Class Hierarchy

```
PerpetualDerivativePyBase (hummingbot/connector/perpetual_derivative_py_base.py)
  └── HyperliquidPerpetualDerivative (connector/derivative/hyperliquid_perpetual/)
        ├── hyperliquid_perpetual_derivative.py  (1143 lines — main connector)
        ├── hyperliquid_perpetual_auth.py        (203 lines — signing/auth)
        ├── hyperliquid_perpetual_api_order_book_data_source.py (407 lines — WS data)
        ├── hyperliquid_perpetual_user_stream_data_source.py    (136 lines — user events)
        ├── hyperliquid_perpetual_constants.py    (123 lines — URLs, limits)
        ├── hyperliquid_perpetual_web_utils.py    (163 lines — HTTP helpers)
        └── hyperliquid_perpetual_utils.py        (208 lines — config/trading rules)
```

### Key URLs (from constants.py)

| Environment | REST | WebSocket |
|------------|------|-----------|
| Mainnet | `https://api.hyperliquid.xyz` | `wss://api.hyperliquid.xyz/ws` |
| Testnet | `https://api.hyperliquid-testnet.xyz` | `wss://api.hyperliquid-testnet.xyz/ws` |

All REST endpoints go to `/info` (POST) or `/exchange` (POST). The `type` field in the JSON body determines the action.

### Authentication

- Uses EIP-712 signing (Ethereum-style)
- Credentials: `hyperliquid_perpetual_secret_key` (private key) + `hyperliquid_perpetual_address` (wallet)
- Modes: `arb_wallet` or `api_wallet`
- Optional vault mode (`use_vault`)

---

## 2. WebSocket Data Flow

### Subscriptions (from `_subscribe_channels`)

The order book data source subscribes to three channels per trading pair:

```json
{"method": "subscribe", "subscription": {"type": "trades", "coin": "BTC"}}
{"method": "subscribe", "subscription": {"type": "l2Book", "coin": "BTC"}}
{"method": "subscribe", "subscription": {"type": "activeAssetCtx", "coin": "BTC"}}
```

### Trade Message Format

```json
{
  "channel": "trades",
  "data": [
    {
      "coin": "BTC",
      "side": "A",       // "A" = taker sell (aggressor), "B" = taker buy
      "px": "97500.0",
      "sz": "0.01",
      "hash": "0x...",
      "time": 1740000000000  // milliseconds
    }
  ]
}
```

Parsed in `_parse_trade_message()`:
- `side "A"` → `TradeType.SELL`
- `side "B"` → `TradeType.BUY`
- Price, amount, hash, timestamp extracted

### Order Book Message Format

```json
{
  "channel": "l2Book",
  "data": {
    "coin": "BTC",
    "time": 1740000000000,
    "levels": [
      [{"px": "97500.0", "sz": "1.5"}, ...],   // bids
      [{"px": "97501.0", "sz": "0.8"}, ...]    // asks
    ]
  }
}
```

### Message Routing

`_channel_originating_message()` routes by channel name:
- `"l2Book"` → order_book_snapshot queue
- `"trades"` → trade messages queue  
- `"activeAssetCtx"` → funding info queue

---

## 3. Strategy Architecture

### Strategy Base Class

```
StrategyPyBase
  └── ScriptStrategyBase (hummingbot/strategy/script_strategy_base.py)
        └── ICTBTCPerpsV5Footprint (scripts/ict_btc_perps_v5_footprint.py)
```

### Key Methods

| Method | Purpose |
|--------|---------|
| `on_tick()` | Main loop — called every tick (~1s) |
| `buy()` / `sell()` | Place orders via connector |
| `get_balance()` | Query account balance |
| `get_active_orders()` | List open orders |
| `did_fill_order()` | Callback when order fills |
| `format_status()` | CLI status display |
| `on_stop()` | Cleanup on shutdown |

### Candles Feed

Candles are independent data feeds, NOT from the connector's WS:

```python
CandlesFactory.get_candle(CandlesConfig(
    connector="hyperliquid_perpetual",
    trading_pair="BTC-USD",
    interval="5m",
    max_records=200
))
```

Uses `hyperliquid_perpetual_candles/` which:
- REST: POST `/info` with `{"type": "candleSnapshot", "req": {"coin": "BTC", "interval": "5m", ...}}`
- WS: Subscribe to `{"type": "candle", "coin": "BTC", "interval": "5m"}`
- Fields: `t` (time), `o`, `h`, `l`, `c` (OHLC), `v` (volume), `n` (trade count)

---

## 4. Footprint Data — Custom Integration Point

### The Gap

Hummingbot's native candle data does NOT include:
- Bid/ask volume split
- Aggressor side classification per level
- Delta or cumulative delta
- Volume profile / POC

### Our Solution: `footprint_aggregator.py`

Located in `scripts/footprint_aggregator.py`, called from the strategy's `on_tick()`:

```
Hyperliquid REST API ──→ FootprintAggregator ──→ Strategy scoring
  (recentTrades)           (builds candles)        (entry/exit signals)
```

**Data source:** REST polling (not WS), using:
```json
POST /info
{"type": "recentTrades", "coin": "BTC"}
```

Returns tick-level trades with aggressor side (`"A"`/`"B"`), which is the critical data
that standard candles don't provide.

**Why REST instead of WS:**
- Strategy runs in synchronous `on_tick()` context
- Hummingbot's WS is managed by the connector (can't easily tap into it)
- REST polling at 2s intervals is sufficient for 1m/5m candle building
- Simpler, no async complexity in strategy code

### Native Integration (IMPLEMENTED)

The footprint data feed is now a first-class Hummingbot module:

```
hummingbot/data_feed/footprint_feed/
├── __init__.py              # Public exports
├── data_types.py            # FootprintConfig (Pydantic)
├── footprint_candle.py      # FootprintCandle + PriceLevel data classes
└── footprint_feed.py        # FootprintFeed (NetworkBase) — WS consumer + pattern detection
```

`FootprintFeed` extends `NetworkBase` (same as `CandlesBase`) and:
- Opens its own WebSocket to Hyperliquid (trades channel)
- Processes trades with aggressor side classification
- Builds footprint candles per timeframe (1m, 5m)
- Runs pattern detection: absorption, stacked imbalances, finished auctions
- Exposes public API matching the strategy's needs

**Backward compatibility:** `scripts/footprint_aggregator.py` is a sync REST-polling
wrapper using the same data structures. Strategies can use either approach.

---

## 5. Order Lifecycle

```
Strategy.buy/sell()
  → Connector.create_order()
    → REST POST /exchange (signed)
      → Exchange processes
        → WS user stream: order update
          → Connector.did_fill_order() callback
            → Strategy.did_fill_order() callback
```

### Order Types

- `MARKET` — immediate execution
- `LIMIT` — resting order
- Hyperliquid also supports: `limit_tif` (GTC/IOC/ALO), `reduce_only`

### Position Management

- Connector tracks positions via `account_positions` property
- Position key: trading pair (e.g., `"BTC-USD"`) in ONEWAY mode
- Fields: `amount` (+/- for long/short), `entry_price`, `unrealized_pnl`, `leverage`
- Our strategy reconciles internal tracking with exchange state every 10 ticks

---

## 6. Docker

### Existing Dockerfile (multi-stage)

1. **Builder stage:** conda env from `setup/environment.yml`, pip packages from `setup/pip_packages.txt`, Cython build
2. **Release stage:** copies artifacts, sets up mount points

### Mount Points

```
/home/hummingbot/conf/         — configuration files
/home/hummingbot/conf/connectors/ — API credentials
/home/hummingbot/logs/         — log output
/home/hummingbot/data/         — persistent data
/home/hummingbot/scripts/      — strategy scripts
/home/hummingbot/controllers/  — v2 controllers
```

### Entrypoint

```bash
conda activate hummingbot && ./bin/hummingbot_quickstart.py
```

---

## 7. Config & Credentials

### Connector Config

Located in `conf/connectors/hyperliquid_perpetual_testnet.yml`:
```yaml
hyperliquid_perpetual_secret_key: "0x..."
hyperliquid_perpetual_address: "0x..."
```

### Strategy Config

Strategies configured in `conf/conf_client.yml` or via CLI:
```
config strategy_file_path scripts/ict_btc_perps_v5_footprint.py
```

---

## 8. Data Flow Diagram

```
                    ┌─────────────────────────────────────────┐
                    │          Hyperliquid Exchange            │
                    └──────┬──────────────┬───────────────────┘
                           │              │
                    WebSocket          REST API
                    (trades,           (recentTrades,
                     l2Book,            candleSnapshot,
                     candle)            orders, account)
                           │              │
              ┌────────────┴──┐    ┌──────┴──────────────┐
              │  Connector    │    │ FootprintAggregator  │
              │  (order book, │    │ (footprint candles,  │
              │   positions,  │    │  delta, absorption,  │
              │   orders)     │    │  stacked imbalances) │
              └───────┬───────┘    └──────┬──────────────┘
                      │                    │
              ┌───────┴────────────────────┴──────┐
              │         Strategy v5/v7             │
              │  (ICT + Footprint scoring engine)  │
              │                                    │
              │  1. Market State (regime, session)  │
              │  2. Momentum (SMI, CMF)            │
              │  3. ICT Structure (OB, FVG, BOS)   │
              │  4. Footprint Scoring              │
              │  5. Risk Management                │
              │  6. Execution + Position Mgmt      │
              └────────────────────────────────────┘
```
