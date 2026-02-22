# Flow Hunter v2 — Pure Order Flow Trading Strategy

## Overview

Flow Hunter v2 is a complete rewrite of the trading strategy, focusing exclusively on order flow analysis and footprint patterns. Unlike the ICT-based strategy_v7.py, this strategy follows the Flow Hunter trading playbook methodology.

**Core Philosophy:** "Who is going to come in after me, and why?"

Every entry requires:
1. A key level (prior session H/L/POC, VAH/VAL, demand/supply zones)
2. Order flow confirmation (absorption, delta divergence, or AIP)

**Exit is the most important part** - uses tiered exits with POC-based targets.

---

## Key Differences from Strategy v7

| Feature | Strategy v7 (ICT) | Flow Hunter v2 (Order Flow) |
|---------|-------------------|------------------------------|
| **Entry Logic** | ICT methodology (OBs, FVGs, displacement) | Pure order flow (absorption, delta divergence) |
| **Exit Logic** | ROE targets + footprint exits | Tiered exits (50% POC, 25% swing, 25% delta) |
| **Position Sizing** | Phase-based (20% → 5% risk) | Confirmation-based (2% → 0.5% risk) |
| **Session Rules** | Session weights (Asia 0.8x, London 1.0x) | Hard limits (max 3 trades, stop after 2 losses) |
| **Risk Management** | Circuit breaker, daily loss limit | Session-based stops, no trading after 2 losses |
| **Complexity** | High (2000+ lines, 17 changes) | Medium (1250 lines, clean implementation) |

---

## Trading Setups

### Setup A: Absorption-Initiation Pattern (AIP)
**Frequency:** High (bread and butter setup)

**What it detects:** Trapped traders at key levels

**Entry criteria:**
1. **Absorption candle:** Heavy selling (negative delta) BUT closes in upper 70% of range
2. **Initiation candle:** Positive delta, closes above midpoint
3. **CVD divergence:** Price making lower lows, CVD making higher lows (optional)

**Example (LONG):**
- Price approaches prior session low (support)
- Candle shows heavy selling (delta -500) but closes near high
- Next candle shows buying (delta +300) and closes strong
- Enter LONG - sellers are trapped

### Setup B: Absorption Reversal
**Frequency:** Low (highest conviction)

**What it detects:** Massive volume at key levels with minimal price movement

**Entry criteria:**
1. **Key level:** Must be at prior POC, VAH/VAL, or session high/low
2. **Massive volume:** 2x average volume
3. **Delta/volume ratio:** ≥25% (significant order flow)
4. **Delta flip:** Previous candle negative delta, current positive (or vice versa)
5. **Minimal price movement:** <0.2% range despite high volume

**Example (LONG):**
- Price at prior session POC
- Volume 3x average, but price only moved $20
- Delta ratio 30% (300 delta on 1000 volume)
- Previous candle -200 delta, current +300 delta
- Enter LONG - absorption complete, buyers taking over

### Setup C: Delta Divergence
**Frequency:** Medium (trend exhaustion)

**What it detects:** Price making new highs/lows but CVD showing opposite behavior

**Entry criteria:**
1. **Price action:** Making higher highs (bearish) or lower lows (bullish)
2. **Delta divergence:** CVD declining while price rising (or vice versa)
3. **Key level proximity:** Must be near resistance (bearish) or support (bullish)
4. **Minimum candles:** 3+ candles showing divergence

**Example (SHORT):**
- Price making higher highs on 5m
- CVD showing lower highs (delta declining)
- Price near VAH (resistance)
- Enter SHORT - buyers exhausted

---

## Big Picture Analysis (1H)

Before every session, answer four questions:

### 1. What's the trend?
- **Bullish:** Higher highs + higher lows
- **Bearish:** Lower highs + lower lows
- **Range:** Neither pattern

### 2. Where are the key levels?
- Prior session high/low
- Prior session POC
- VAH/VAL (Value Area High/Low)
- Demand/supply zones
- Order blocks, FVGs

### 3. Where is price relative to Value Area?
- **Inside VA:** Expect range day, POC acts as magnet
- **Outside VA:** Expect directional movement

### 4. Where are the liquidity pools?
- Equal highs/lows on 1H (within 0.5%)
- These are targets - price drawn to stops

---

## Exit System (Tiered)

| Tier | Portion | Target | Action |
|------|---------|--------|--------|
| 1 | 50% | Nearest POC or heavy volume node | Move stop to breakeven |
| 2 | 25% | Next swing level or 2x stop distance | Trail stop behind last POC |
| 3 | 25% | Delta exit signal | Hold until flow reverses |

### Delta Exit Signal
**In a LONG:** Strong negative delta (>20% ratio) AND closes below prior candle's POC
**In a SHORT:** Strong positive delta (>20% ratio) AND closes above prior candle's POC

---

## Position Sizing

| Confirmations | Risk Per Trade | Example |
|---------------|----------------|---------|
| 3+ signals | 2% | AIP + CVD divergence + key level |
| 2 signals | 1% | Absorption + delta flip |
| 1 signal | 0.5% or skip | Key level only |

**Leverage:** 10x (fixed)

---

## Session Rules

### Trading Windows (ET → UTC)
- **London:** 3:00-5:00 ET (8:00-10:00 UTC) ✅
- **NY AM:** 9:30-11:30 ET (14:30-16:30 UTC) ✅
- **Lunch:** 12:00-14:00 ET (17:00-19:00 UTC) ❌ AVOID

### Hard Limits
- **Max 3 trades per session**
- **Stop after 2 losses** (session stopped, no more trades)
- **Max spread:** 0.1% (reject if wider)

---

## Configuration Parameters

```python
# Session rules
max_trades_per_session = 3
max_losses_before_stop = 2

# Position sizing
risk_3_confirmations = 0.02  # 2%
risk_2_confirmations = 0.01  # 1%
risk_1_confirmation = 0.005  # 0.5%

# Absorption detection
absorption_volume_mult = 2.0      # Volume must be 2x average
absorption_delta_ratio = 0.25     # Delta/volume ratio ≥ 25%
absorption_price_range = 0.002    # Price movement < 0.2%

# Delta divergence
divergence_lookback = 10          # Candles to check
divergence_min_candles = 3        # Minimum candles showing divergence

# Leverage
leverage = 10
```

---

## Trade Journal

Every trade logs:
- Setup type (A/B/C)
- Key level type
- Number of confirmations
- Entry/stop/exit prices
- P&L
- Exit reason

Example log:
```
[FH2] 🎯 ENTRY LONG @ 68000 | size=0.001470 BTC | stop=67660 | setup=A_AIP | confirmations=3 (key_level_prior_session_low, aip_absorption, aip_initiation)
[FH2] 📤 PARTIAL EXIT Tier1_POC (50%) @ 68200 | P&L=$0.1470 | remaining=0.000735 BTC
[FH2] 📤 PARTIAL EXIT Tier2_Swing (25%) @ 68400 | P&L=$0.1470 | remaining=0.000368 BTC
[FH2] ✅ EXIT Tier3_Delta LONG @ 68350 | P&L=$0.2573 | total_pnl=$0.5513 (1W/0L)
```

---

## Deployment

See `DEPLOYMENT_INSTRUCTIONS.md` for step-by-step deployment guide.

