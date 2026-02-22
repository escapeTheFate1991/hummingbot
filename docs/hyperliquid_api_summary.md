# Hyperliquid API Summary

*For Hummingbot FootprintAggregator Integration*

## Overview

Hyperliquid is a high-performance L1 blockchain with a fully onchain order book supporting 200k orders per second. This document provides comprehensive API documentation for building a FootprintAggregator that processes tick-level trade data with aggressor side classification.

## Base URLs

| Environment | REST API | WebSocket |
|-------------|----------|-----------|
| **Mainnet** | `https://api.hyperliquid.xyz` | `wss://api.hyperliquid.xyz/ws` |
| **Testnet** | `https://api.hyperliquid-testnet.xyz` | `wss://api.hyperliquid-testnet.xyz/ws` |
| **Local** | `http://localhost:3001` | `ws://localhost:3001/ws` |

## Authentication

### Public Endpoints
- No authentication required for market data, orderbook, trades, candles
- All `/info` endpoints are public

### Private Endpoints (Trading)
- Uses Ethereum-style signing with private key
- All `/exchange` endpoints require signatures
- Signature process:
  1. Create action payload
  2. Sign with wallet private key using EIP-712 standard
  3. Include `nonce` (timestamp), `signature`, `vaultAddress` (optional)

### API Key Generation (Optional)
- Generate API wallet on https://app.hyperliquid.xyz/API
- Use API wallet's private key while setting main wallet as `account_address`
- Recommended for security isolation

## REST API Endpoints

### Market Data

#### Get All Mid Prices
```
POST /info
{
    "type": "allMids",
    "dex": ""  // optional, "" for default perp dex
}
```
**Response:**
```json
{
    "BTC": "43250.5",
    "ETH": "2650.25",
    "ATOM": "12.45"
}
```

#### Get L2 Order Book
```
POST /info
{
    "type": "l2Book",
    "coin": "ETH"
}
```
**Response:**
```json
{
    "coin": "ETH",
    "levels": [
        [
            {"px": "2650.1", "sz": "1.5", "n": 3},
            {"px": "2649.9", "sz": "2.1", "n": 5}
        ],
        [
            {"px": "2650.3", "sz": "0.8", "n": 2},
            {"px": "2650.5", "sz": "1.2", "n": 4}
        ]
    ],
    "time": 1703275200000
}
```
- `levels[0]` = bids (descending price)
- `levels[1]` = asks (ascending price)
- `px` = price, `sz` = size, `n` = number of orders

#### Get Candle Data
```
POST /info
{
    "type": "candleSnapshot",
    "req": {
        "coin": "ETH",
        "interval": "1m",  // 1m, 5m, 15m, 1h, 4h, 1d
        "startTime": 1703271600000,
        "endTime": 1703275200000
    }
}
```
**Response:**
```json
[
    {
        "T": 1703271600000,  // close time
        "c": "2650.25",      // close
        "h": "2651.00",      // high
        "i": "1m",           // interval
        "l": "2649.50",      // low
        "n": 156,            // number of trades
        "o": "2650.00",      // open
        "s": "ETH",          // symbol
        "t": 1703271540000,  // open time
        "v": "125.75"        // volume
    }
]
```

#### Get User Fills (Tick-Level Trade Data)
```
POST /info
{
    "type": "userFills",
    "user": "0x..."
}
```

**For all trades (public):**
```
POST /info
{
    "type": "userFillsByTime",
    "user": "0x...",
    "startTime": 1703271600000,
    "endTime": 1703275200000,
    "aggregateByTime": false
}
```

**Response:**
```json
[
    {
        "coin": "ETH",
        "px": "2650.25",     // execution price
        "sz": "1.5",         // trade size
        "side": "B",         // B=Buy/Bid, A=Ask/Sell (AGGRESSOR SIDE)
        "time": 1703275200000,  // timestamp (milliseconds)
        "startPosition": "0.5",
        "dir": "Open Long",
        "closedPnl": "0",
        "hash": "0x...",
        "oid": 123456,
        "crossed": true      // true if aggressor (market order)
    }
]
```

### Account Information

#### Get User State
```
POST /info
{
    "type": "clearinghouseState",
    "user": "0x...",
    "dex": ""
}
```

#### Get Open Orders
```
POST /info
{
    "type": "openOrders", 
    "user": "0x...",
    "dex": ""
}
```
**Response:**
```json
[
    {
        "coin": "ETH",
        "limitPx": "2650.0",
        "oid": 123456,
        "side": "B",         // B=Buy, A=Sell
        "sz": "1.5",
        "timestamp": 1703275200000
    }
]
```

### Trading Operations

#### Place Order
```
POST /exchange
{
    "action": {
        "type": "order",
        "orders": [
            {
                "a": 0,              // asset (0=BTC, 1=ETH, etc.)
                "b": true,           // is_buy
                "p": "2650.0",       // price
                "s": "1.5",          // size
                "r": false,          // reduce_only
                "t": {"limit": {"tif": "Gtc"}},  // order type
                "c": "client123"     // client order id (optional)
            }
        ]
    },
    "nonce": 1703275200000,
    "signature": "0x...",
    "vaultAddress": null
}
```

**Order Types:**
- `{"limit": {"tif": "Gtc"}}` - Good Till Canceled
- `{"limit": {"tif": "Ioc"}}` - Immediate or Cancel (market order)
- `{"limit": {"tif": "Alo"}}` - Add Liquidity Only (post-only)

#### Cancel Order
```
POST /exchange
{
    "action": {
        "type": "cancel",
        "cancels": [
            {
                "a": 0,      // asset
                "o": 123456  // order id
            }
        ]
    },
    "nonce": 1703275200000,
    "signature": "0x..."
}
```

## WebSocket API

### Connection
```
WebSocket URL: wss://api.hyperliquid.xyz/ws
```

### Message Format
All WebSocket messages follow this structure:
```json
{
    "method": "subscribe|unsubscribe|ping",
    "subscription": {...}
}
```

### Subscription Types

#### Trade Stream (Tick-Level Data)
```json
{
    "method": "subscribe",
    "subscription": {
        "type": "trades",
        "coin": "ETH"
    }
}
```

**Stream Response:**
```json
{
    "channel": "trades",
    "data": [
        {
            "coin": "ETH",
            "side": "B",              // AGGRESSOR SIDE: B=Buy, A=Sell
            "px": "2650.25",          // execution price
            "sz": "1.5",              // trade size  
            "time": 1703275200000,    // timestamp (milliseconds precision)
            "hash": "0x...",          // transaction hash
            "crossed": true           // aggressor flag
        }
    ]
}
```
**Key for FootprintAggregator:**
- `side` = aggressor side (buyer vs seller initiated)
- `px` = exact execution price  
- `sz` = exact trade size
- `time` = millisecond precision timestamp
- `crossed` = true indicates market order (aggressor)

#### Order Book Delta Stream
```json
{
    "method": "subscribe", 
    "subscription": {
        "type": "l2Book",
        "coin": "ETH"
    }
}
```

**Stream Response:**
```json
{
    "channel": "l2Book",
    "data": {
        "coin": "ETH",
        "levels": [
            [
                {"px": "2650.1", "sz": "0.0", "n": 0},  // size=0 means level removed
                {"px": "2649.9", "sz": "2.5", "n": 3}   // updated level
            ],
            [
                {"px": "2650.3", "sz": "1.8", "n": 4}
            ]
        ],
        "time": 1703275200000
    }
}
```

#### Candle/OHLCV Stream  
```json
{
    "method": "subscribe",
    "subscription": {
        "type": "candle",
        "coin": "ETH", 
        "interval": "1m"
    }
}
```

**Stream Response:**
```json
{
    "channel": "candle",
    "data": {
        "T": 1703275200000,   // close time
        "c": "2650.25",       // close
        "h": "2651.00",       // high
        "i": "1m",            // interval
        "l": "2649.50",       // low
        "n": 156,             // number of trades
        "o": "2650.00",       // open
        "s": "ETH",           // symbol
        "t": 1703275140000,   // open time
        "v": "125.75"         // volume
    }
}
```

#### Best Bid/Offer Stream
```json
{
    "method": "subscribe",
    "subscription": {
        "type": "bbo",
        "coin": "ETH"
    }
}
```

### User-Specific Streams

#### User Fills Stream
```json
{
    "method": "subscribe",
    "subscription": {
        "type": "userFills", 
        "user": "0x..."
    }
}
```

#### Order Updates Stream
```json
{
    "method": "subscribe",
    "subscription": {
        "type": "orderUpdates"
    }
}
```

## Key Data Structures for FootprintAggregator

### Trade Payload Schema
```typescript
interface TradeData {
    coin: string;           // trading pair
    side: "B" | "A";       // aggressor side (B=buyer, A=seller)  
    px: string;            // price (decimal string)
    sz: string;            // size (decimal string)
    time: number;          // timestamp (milliseconds)
    hash: string;          // transaction hash  
    crossed: boolean;      // true = market order, false = limit fill
}
```

### Order Book Payload Schema
```typescript
interface OrderBookLevel {
    px: string;    // price level
    sz: string;    // total size (0 = level removed)
    n: number;     // number of orders
}

interface OrderBookData {
    coin: string;
    levels: [OrderBookLevel[], OrderBookLevel[]]; // [bids, asks]
    time: number;  // timestamp
}
```

### Candle Payload Schema  
```typescript
interface CandleData {
    T: number;     // close time (ms)
    c: string;     // close price
    h: string;     // high price  
    i: string;     // interval
    l: string;     // low price
    n: number;     // trade count
    o: string;     // open price
    s: string;     // symbol
    t: number;     // open time (ms)
    v: string;     // volume
}
```

## Asset Mapping

### Perpetuals (Perp Dex)
- Assets start at index 0
- BTC = 0, ETH = 1, etc.
- Get mapping via `/info` with `type: "meta"`

### Spot Assets  
- Assets start at index 10000
- BTC/USD = 10000, ETH/USD = 10001, etc.
- Get mapping via `/info` with `type: "spotMeta"`

### Asset Resolution
```
POST /info
{
    "type": "meta",
    "dex": ""
}
```
**Response:**
```json
{
    "universe": [
        {"name": "BTC", "szDecimals": 5},
        {"name": "ETH", "szDecimals": 4},
        {"name": "ATOM", "szDecimals": 3}
    ]
}
```

## Data Precision & Timestamps

### Price Precision
- Perpetuals: 5 significant figures, 6 decimal places max
- Spot: 5 significant figures, 8 decimal places max  
- Prices adjusted by `szDecimals` from asset metadata

### Size Precision
- Based on `szDecimals` from asset metadata
- BTC: 5 decimals (0.00001)
- ETH: 4 decimals (0.0001)

### Timestamp Precision
- All timestamps in **milliseconds** (Unix epoch)
- WebSocket trade data has millisecond precision
- Critical for tick-level analysis and aggressor classification

## Aggressor Side Classification

The `side` field in trade data indicates the **aggressor side**:
- `"B"` (Bid/Buy) = Buyer-initiated trade (market buy order hit ask)
- `"A"` (Ask/Sell) = Seller-initiated trade (market sell order hit bid)

Additional aggressor indicators:
- `crossed: true` = Market order (aggressor)  
- `crossed: false` = Limit order fill (passive)

This is essential for FootprintAggregator to:
1. Classify buying vs selling pressure
2. Identify market vs limit order flow
3. Analyze order book absorption patterns

## Rate Limits

- **REST API**: 1200 requests per minute per IP
- **WebSocket**: No explicit limit, but connection management required
- **Orders**: 100 orders per second per user

## Error Handling

### Common Error Responses
```json
{
    "status": "err",
    "response": "Insufficient margin"
}
```

### WebSocket Connection Management
- Send ping every 50 seconds: `{"method": "ping"}`
- Handle reconnection on disconnect
- Resubscribe to channels after reconnection

## SDK Integration Notes

### Python SDK Usage
```python
from hyperliquid.info import Info
from hyperliquid.utils import constants

# Initialize
info = Info(constants.MAINNET_API_URL)

# Get trades (tick data)
fills = info.user_fills_by_time(
    "0x...", 
    start_time=1703271600000,
    end_time=1703275200000,
    aggregate_by_time=False
)

# WebSocket trade stream
def on_trades(ws_msg):
    for trade in ws_msg["data"]:
        aggressor_side = trade["side"]  # B or A
        price = float(trade["px"])
        size = float(trade["sz"])
        timestamp = trade["time"]
        
info.subscribe({"type": "trades", "coin": "ETH"}, on_trades)
```

## Summary for FootprintAggregator

**Critical Data Points:**
1. **Tick-level trades**: `/info` with `type: "userFillsByTime"` or WebSocket `trades` channel
2. **Aggressor classification**: `side` field ("B"=buyer, "A"=seller)
3. **Order book deltas**: WebSocket `l2Book` channel  
4. **Timestamp precision**: Milliseconds for accurate sequencing
5. **Price/size precision**: Asset-specific via metadata

**Recommended WebSocket Subscriptions:**
- `trades` for tick data with aggressor side
- `l2Book` for order book updates
- `candle` for OHLCV aggregation

The API provides all necessary components for building a comprehensive FootprintAggregator with precise tick-level analysis and reliable aggressor side classification.