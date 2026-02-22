

Fair point. Here it is:

---

# Hummingbot v7 ICT + Footprint Trading Bot — User Manual

## What Is This?

A fully automated BTC perpetual futures trading bot that runs on **Hyperliquid** (a decentralized perpetuals exchange). It uses **ICT (Inner Circle Trader) methodology** combined with **footprint order flow analysis** to find high-probability trade entries and exits.

The bot runs inside a Docker container using the **Hummingbot** open-source trading framework, with a custom Python strategy script (`strategy_v7.py`) that plugs into Hummingbot's connector and data feed system.

**In plain terms:** The bot watches Bitcoin's price action on multiple timeframes, identifies institutional trading patterns (order blocks, liquidity sweeps, fair value gaps), confirms them with real order flow data (who's actually buying and selling at each price level), scores everything, and executes trades when confidence is high enough.

---

## Architecture Overview

```
┌──────────────────────────────────────────────────────┐
│                  Docker Container                     │
│  ┌────────────────────────────────────────────────┐  │
│  │             Hummingbot v2.12.0                  │  │
│  │                                                 │  │
│  │  ┌──────────────┐  ┌────────────────────────┐  │  │
│  │  │ Hyperliquid  │  │    strategy_v7.py       │  │  │
│  │  │  Connector   │◄─┤                         │  │  │
│  │  │  (mainnet)   │  │  ICT Analysis Engine    │  │  │
│  │  └──────┬───────┘  │  Footprint Scoring      │  │  │
│  │         │          │  State Machine          │  │  │
│  │         │          │  Risk Management        │  │  │
│  │  ┌──────▼───────┐  └─────────┬──────────────┘  │  │
│  │  │  Hyperliquid │            │                  │  │
│  │  │   Exchange   │  ┌─────────▼──────────────┐  │  │
│  │  │  (mainnet)   │  │    Data Feeds           │  │  │
│  │  │              │  │  • 5m candles (OHLCV)   │  │  │
│  │  │  - Orders    │  │  • 1h candles (OHLCV)   │  │  │
│  │  │  - Positions │  │  • 1m footprint (WS)    │  │  │
│  │  │  - Balances  │  │  • 5m footprint (WS)    │  │  │
│  │  └──────────────┘  └────────────────────────┘  │  │
│  └────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────┘
```

**Data sources feeding the strategy:**
1. **5-minute candles** (200 candles = ~17 hours of history) — primary analysis timeframe
2. **1-hour candles** (100 candles = ~4 days of history) — higher timeframe bias
3. **Footprint feed** (1-minute and 5-minute) — real-time order flow via WebSocket, showing bid/ask volume at every price level

---

## How It Thinks: The Analysis Pipeline

Every tick (~1 second), the bot runs through this exact sequence:

### Step 1: Regime Detection (The Gate)

The bot first determines the market's current "regime" by comparing recent ATR (Average True Range) to its longer-term average:

| Regime | Condition | Trading? |
|--------|-----------|----------|
| **EXPANSION** | ATR > 1.2× average AND trending structure | ✅ Yes — this is where money is made |
| **PULLBACK** | ATR elevated but no clear trend | ✅ Yes — retracement entries |
| **COMPRESSION** | ATR < 0.6× average AND tight range (<0.8%) | ❌ No — stay flat, wait |

**If COMPRESSION → the bot does nothing.** This is the most important filter. Most losses come from trading in choppy, compressed markets.

### Step 2: Swing Structure & Break of Structure (BOS)

The bot identifies swing highs and swing lows on the 5-minute chart (using a 5-candle lookback), then watches for **Break of Structure**:

- **Bullish BOS:** Price closes above the most recent swing high → direction lock = LONG
- **Bearish BOS:** Price closes below the most recent swing low → direction lock = SHORT

The direction lock lasts for **40 candles** (about 3.3 hours on 5m) and decays naturally. While locked:
- A LONG lock **blocks all short entries**
- A SHORT lock **blocks all long entries**

This prevents the bot from fighting the trend.

### Step 3: Premium/Discount Zones

Using the last 40 candles of price action, the bot divides the range into zones:

- **Discount zone** (bottom 40%) → Only longs allowed
- **Premium zone** (top 40%) → Only shorts allowed
- **Equilibrium** (middle 20%) → Both allowed

This is core ICT: buy in discount, sell in premium. The bot hard-blocks entries that violate this rule.

### Step 4: Liquidity Sweep Detection

The bot watches the last 3 candles for **stop hunts** — price spikes through a swing high/low then reverses back:

- Wick below a swing low that closes back above it → **bullish sweep** (+1.5 points to long score)
- Wick above a swing high that closes back below it → **bearish sweep** (+1.5 points to short score)

Liquidity sweeps are one of the strongest ICT signals — they show institutional players running stops before reversing.

### Step 5: ICT Components

**Order Blocks (OBs):**
The bot scans the last 50 candles for order blocks — the last opposing candle before a strong displacement move (1.5× average body size). Order blocks represent zones where institutions placed large orders.

- Fresh OBs (≤12 candles old) score higher than stale ones
- Price must be near the OB zone (within 0.2-0.4%) to score
- Strong displacement (>2× average body) gets a bonus

**Fair Value Gaps (FVGs):**
Three-candle patterns where a gap exists between candle 1's high and candle 3's low. These gaps tend to get filled — price returns to them. The bot scores when price is inside an FVG.

**Displacement-Retrace:**
The bot looks for a strong displacement candle followed by a retrace back into the order block + FVG zone. When both an OB and FVG overlap, it's the highest-conviction ICT setup (scores 2.0 points).

### Step 6: Momentum Confirmation

**SMI (Stochastic Momentum Index):**
- SMI above signal line + positive slope → bullish momentum (+1.5)
- SMI below signal line + negative slope → bearish momentum (+1.5)
- Strong slope (>3) → extra +0.5

**CMF (Chaikin Money Flow):**
- CMF > 0.01 → buying pressure (+1.0)
- CMF < -0.01 → selling pressure (+1.0)

### Step 7: Footprint Analysis (The Edge)

This is what separates v7 from basic ICT strategies. The footprint feed shows the **actual order flow** — every trade hitting the bid or lifting the ask at each price level.

Seven footprint signals are calculated:

| Signal | What It Means | Score |
|--------|---------------|-------|
| **Absorption at OB** | Heavy volume absorbed at an order block without price moving through it → institutions defending the level | +1.5 |
| **Stacked Imbalances** | 3+ consecutive price levels where buy:sell ratio exceeds 3:1 (or 4:1 in Asia session) → one-sided aggression | +1.0 |
| **Delta Divergence** | Price moving up but net delta is negative (or vice versa) → the move is fake, about to reverse | **-2.0 (BLOCKS entry)** |
| **Finished Auction** | Zero bid or zero ask volume at the candle's extreme → the auction is complete, no more participants in that direction | +0.5 |
| **Cumulative Delta** | Running total of (ask volume - bid volume) over recent candles → sustained buying or selling pressure | +0.5 |
| **POC Proximity** | Price is near the Point of Control (highest-volume price level) → fair value, tends to attract price | +0.5 |
| **Trapped Traders** | Heavy volume at one extreme but close at the opposite (e.g., sellers dump at lows but candle closes near high → sellers are trapped) | +1.5 |

**Delta Divergence is the most important.** It's the only signal that can completely block an entry with its -2.0 penalty. If order flow disagrees with price action, the bot steps aside.

### Step 8: Scoring & Decision

All component scores are summed separately for long and short:

```
Long Score  = (bias + OB + FVG + displacement + sweep + SMI + CMF) × session_weight + footprint_long
Short Score = (bias + OB + FVG + displacement + sweep + SMI + CMF) × session_weight + footprint_short
```

**Session weights** adjust scores based on time of day:

| Session | Hours (UTC) | Weight | Why |
|---------|-------------|--------|-----|
| Asia | 00-08 | ×0.8 | Lower volume, more chop |
| London | 07-16 | ×1.0 | Full weight |
| NY AM | 13-17 | ×1.0 | Full weight, London/NY overlap gets +0.5 bonus |
| NY PM | 17-21 | ×0.7 | Reduced — often reversal/chop |
| Dead Zone | 21-24 | ×0.3 | Minimal — low liquidity |

**Entry rules:**
1. Score must be ≥ 3.0 (minimum threshold)
2. Winning side must lead by > 0.5 points
3. Direction lock must not block the signal
4. Premium/discount must not block the signal
5. If scores are tied but hourly bias exists → bias breaks the tie

---

## Position Sizing & Risk Management

### Capital Phases

The bot automatically adjusts risk based on account balance:

| Phase | Balance | Risk per Trade | Leverage | Stop Buffer | Purpose |
|-------|---------|---------------|----------|-------------|---------|
| **Phase 1: Micro** | ≤$100 | 20% | 10x | 2.0% | Survive with small account |
| **Phase 2: Growth** | ≤$500 | 15% | 15x | 2.0% | Build capital |
| **Phase 3: Build** | ≤$2,000 | 10% | 20x | 1.8% | Accelerate |
| **Phase 4: Scale** | ≤$10,000 | 8% | 15x | 1.5% | Reduce risk as stakes grow |
| **Phase 5: Protect** | >$10,000 | 5% | 10x | 1.2% | Wealth preservation |

**Position sizing formula:**
```
margin = balance × risk% × score_factor
position_value = margin × leverage
position_size = position_value / price

score_factor = 0.6 + (score - 3.0) × 0.13    (ranges from 0.6 to 1.0)
```

Higher-conviction trades (higher scores) get larger positions. A score of 3.0 uses 60% of max size; a perfect score uses 100%.

**Hard safety limits:**
- Maximum position size: **0.1 BTC** (regardless of balance/leverage)
- Maximum slippage: **0.5%** — if bid-ask spread exceeds this, entry is rejected
- Taker fee accounting: **0.045%** per side (baked into P&L calculations)

### Risk Controls

| Control | Setting | What It Does |
|---------|---------|-------------|
| **Daily loss limit** | 15% of starting balance | Triggers circuit breaker |
| **Circuit breaker** | 1 hour pause | After hitting daily loss limit, no trading for 1 hour |
| **Daily trade limit** | 20 trades max | Prevents overtrading |
| **Trade cooldown** | 120 seconds | Minimum time between trades |
| **Close cooldown** | 30 seconds | Minimum time after closing before re-entering |
| **Startup grace** | 30 seconds | No trading for first 30s (lets connector sync positions) |

---

## Position Management (Exits)

Once in a position, the bot manages it using a priority-ordered exit system:

### Exit Priority (checked every tick):

**1. Take Profit (ROE ≥ 6%)**
If return on equity reaches 6%, close immediately. No questions asked.

**2. Footprint Exits (the smart exits):**

| Exit Type | Condition | Why |
|-----------|-----------|-----|
| **Delta Exhaustion** | ROE ≥ 4% AND delta ratio < 0.1 | Momentum dried up near TP — take profit early |
| **Cumulative Delta Flip** | Cum delta crosses ±200 against position | Entire flow has reversed — get out |
| **Opposing Absorption** | ROE ≥ 2% AND absorption detected against position | Institutions absorbing your direction — ceiling/floor hit |
| **Finished Auction** | Price at a level with zero volume on one side | The auction is done — no more fuel in your direction |

**3. Breakeven Trailing Stop**
Once best ROE reaches 3%, a trailing stop activates. If ROE drops back to 0.5%, the position closes at roughly breakeven — protecting gains.

**4. Stop Loss**
Two methods (priority order):
- **Footprint absorption stop:** If absorption was detected near entry, stop goes N ticks beyond that zone
- **Phase-based percentage stop:** Fallback — uses the stop buffer from the capital phase table

### Exit Pricing
- **Longs exit at the bid** (what someone will actually pay you)
- **Shorts exit at the ask** (what you'd actually pay to buy back)
- Never uses mid-price for exit decisions — that's a fantasy price you can't actually get

---

## State Machine

The bot tracks position state deterministically:

```
FLAT ──[entry signal + all checks pass]──► LONG or SHORT
                                                │
                                    [exit trigger fires]
                                                │
                                                ▼
                                            CLOSING
                                                │
                                    [fills arrive from exchange]
                                                │
                                                ▼
                                            COOLDOWN
                                                │
                                    [cooldown expires]
                                                │
                                                ▼
                                              FLAT
```

Special states:
- **EXCHANGE_MISMATCH**: Exchange shows a position but internal state is flat (orphan detected). Bot auto-closes the orphan.
- **CLOSING**: Waiting for fill confirmations from exchange. No other actions taken during this state.

---

## Honest P&L Tracking

v7 was built with a "no lies" philosophy:

1. **Entry price** = VWAP of all actual fill prices from the exchange (via `did_fill_order` callback)
2. **Exit price** = VWAP of all close fill prices
3. **P&L** = calculated from real fills only, after subtracting taker fees on both sides
4. **Trade count** increments on actual fill, not on order placement attempt
5. **Win/loss** determined by net P&L after fees

The bot maintains a full `trade_history` list with entry/exit VWAP, fill details, reasons, and scores for every trade.

---

## Hyperliquid-Specific Details

### Unified Margin System
Hyperliquid keeps USDC in the **spot clearinghouse**, not the perps clearinghouse. When you open a perps position, it pulls margin from spot automatically. The Hummingbot connector was modified to check **both** clearinghouse endpoints and use `max()` to determine available balance.

### Position Mode
Hyperliquid uses **one-way mode only** (no hedge mode). This means:
- `PositionAction.CLOSE` must be used for all closing orders — never plain `buy()`/`sell()`
- Plain `sell()` on a long would create a new short position (orphan), not close the long

### Fees
- Taker: 0.045% (market orders)
- Maker: 0.02% (limit orders — not currently used by v7)
- Fees are paid from your USDC balance

---

## Infrastructure

### Docker Container
- **Image:** `hummingbot-v7:latest` (custom built from `~/Development/hummingbot/Dockerfile.v7`)
- **Container name:** `hummingbot_v7`
- **Network:** host mode (required for WebSocket feeds)
- **Config password:** stored as ENV in Dockerfile
- **Volumes mounted:**
  - `conf/` — connector configs (API keys, exchange settings)
  - `logs/` — trading logs
  - `data/` — persistent data (candle cache, etc.)
  - `scripts/` — the strategy Python files

### Key Files

| File | Purpose |
|------|---------|
| `~/Development/hummingbot/scripts/strategy_v7.py` | The strategy — all trading logic |
| `~/Development/hummingbot/hummingbot/data_feed/footprint_feed.py` | Native footprint data feed |
| `~/Development/hummingbot/hummingbot/connector/derivative/hyperliquid_perpetual/` | Exchange connector (modified for unified margin) |
| `~/hummingbot/logs/` | Runtime logs |
| `~/hummingbot/conf/connectors/` | API key configs |

### Monitoring
An hourly cron job (`hummingbot-v7-hourly-report`) runs via OpenClaw, reads container logs, and generates a status report including: trade count, P&L, system health, footprint stats, and any errors.

---

## Configuration Reference

All parameters are class-level variables in `strategy_v7.py`. No external config files needed — everything is in the code.

### Key Parameters to Tune

| Parameter | Default | What It Controls |
|-----------|---------|-----------------|
| `min_score_to_trade` | 3.0 | Minimum score for entry — higher = fewer but better trades |
| `roe_target_pct` | 6% | Take profit target |
| `roe_breakeven_pct` | 3% | When trailing stop activates |
| `max_trades_per_day` | 20 | Daily trade cap |
| `trade_cooldown` | 120s | Minimum seconds between trades |
| `fp_imbalance_threshold` | 3.0 | Footprint imbalance ratio (London/NY) |
| `fp_asia_imbalance_threshold` | 4.0 | Footprint imbalance ratio (Asia — stricter) |
| `fp_delta_div_penalty` | -2.0 | How hard delta divergence blocks entries |
| `direction_lock_decay` | 40 candles | How long BOS direction lock lasts |
| `max_daily_loss_pct` | 15% | Circuit breaker threshold |

### Starting/Stopping

```bash
# Start
cd ~/hummingbot
docker run -d --name hummingbot_v7 --network host \
  -v $(pwd)/conf:/home/hummingbot/conf \
  -v $(pwd)/conf/connectors:/home/hummingbot/conf/connectors \
  -v $(pwd)/conf/strategies:/home/hummingbot/conf/strategies \
  -v $(pwd)/conf/scripts:/home/hummingbot/conf/scripts \
  -v $(pwd)/logs:/home/hummingbot/logs \
  -v $(pwd)/data:/home/hummingbot/data \
  -v $(pwd)/scripts:/home/hummingbot/scripts \
  hummingbot-v7:latest

# Stop
docker stop hummingbot_v7

# Logs
docker logs -f hummingbot_v7 --tail 100

# Rebuild after code changes
cd ~/Development/hummingbot
docker build -f Dockerfile.v7 -t hummingbot-v7:latest .
docker rm -f hummingbot_v7
# Then run again
```

---

## Known Issues & Future Work

1. **Finished auction exits fire too fast** (16-22 seconds) — may need minimum hold time or ROE threshold before footprint exits activate
2. **`KeyError: 'USD'`** in funding payment fetch — non-fatal log spam every 2 minutes
3. **Insufficient margin** on micro balance — $5 isn't enough for some position sizes at current BTC prices
4. **Duplicate position detection** — partially fixed with `_can_open_position()` checking exchange positions

---

## The Philosophy

> "Never write fallback code. If something fails, HALT. No guessing defaults."

This bot was built with the principle that **honesty beats optimism**. Every P&L number is from real exchange fills. Every balance check hits the actual API. Every position close uses `PositionAction.CLOSE`. There are no `except: use_default` blocks anywhere in the codebase.

The footprint data provides an edge that pure price-action strategies don't have — you can see *who* is actually trading, not just *what* price did. When institutions absorb selling at an order block, the footprint shows it. When a move runs out of steam (finished auction), the footprint shows it. When trapped traders are about to get squeezed, the footprint shows it.

ICT provides the framework (where to look). Footprint provides the confirmation (whether it's real).