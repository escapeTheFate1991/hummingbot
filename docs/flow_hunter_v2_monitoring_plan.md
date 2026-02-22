# Flow Hunter v2 — 24-Hour Monitoring Plan

## Overview

This document outlines the systematic monitoring approach for Flow Hunter v2 during the first 24 hours of deployment. It defines what to look for, how to correct issues, and what constitutes red flags vs green flags.

---

## Phase 1: Initial Deployment (0-2 Hours)

### What to Look For

1. **Strategy Initialization**
   - ✅ Candles loading (5m, 30m, 1H)
   - ✅ Footprint feed connecting
   - ✅ Leverage set to 10x
   - ✅ No Python errors in logs

2. **Big Picture Analysis**
   - ✅ Trend detection working (BULL/BEAR/RANGE)
   - ✅ Key levels identified (prior session H/L/POC, VAH/VAL)
   - ✅ Liquidity pools detected
   - ✅ Status logs every 5 minutes

3. **Session Detection**
   - ✅ Correct session identified (london/ny_am/lunch)
   - ✅ Session stopped = False during trading hours
   - ✅ Session stopped = True during lunch

### Green Flags ✅

- Logs show: `[FH2] london | BULL | price=$68000 | pnl=$0.00 wr=0% | session=0/3 losses=0 | key_levels=5 liq_pools=2`
- No errors or exceptions
- Candles updating every 5 minutes
- Footprint data flowing

### Red Flags 🚩

- **Python errors** (AttributeError, KeyError, TypeError)
  - **Fix:** Check logs, identify line number, fix code bug
- **No candles loading** ("Candles not ready")
  - **Fix:** Check CandlesFactory configuration, verify exchange connection
- **No footprint data** (footprint.get_latest_candle returns None)
  - **Fix:** Check FootprintFeed configuration, verify WebSocket connection
- **Leverage not set** (still showing 1x)
  - **Fix:** Check `_set_leverage()` method, verify connector.set_leverage() call

### Corrective Actions

| Issue | Root Cause | Fix |
|-------|------------|-----|
| Strategy crashes on start | Missing import or syntax error | Review traceback, fix code |
| Candles not loading | Exchange API issue | Restart container, check network |
| Footprint feed disconnected | WebSocket timeout | Restart container, check Hyperliquid status |

---

## Phase 2: First Trades (2-6 Hours)

### What to Look For

1. **Entry Logic**
   - ✅ Entries only at key levels
   - ✅ Setup type logged (A_AIP, B_ABSORPTION, C_DIVERGENCE)
   - ✅ Confirmations logged (2-3 signals)
   - ✅ Both LONG and SHORT entries occur
   - ✅ Position sizing correct (2%/1%/0.5% based on confirmations)

2. **Entry Frequency**
   - ✅ Max 3 trades per session
   - ✅ No entries during lunch (17:00-19:00 UTC)
   - ✅ Reasonable spacing between entries (not every candle)

3. **Stop Loss Placement**
   - ✅ Stop loss set correctly (below support for LONG, above resistance for SHORT)
   - ✅ Stop distance reasonable (0.5-1% from entry)

### Green Flags ✅

- **Balanced direction:** 40-60% LONG vs SHORT (not 100% one direction)
- **Quality setups:** Entries show 2-3 confirmations
- **Proper logging:**
  ```
  [FH2] 🎯 ENTRY LONG @ 68000 | size=0.001470 BTC | stop=67660 | setup=A_AIP | confirmations=3 (key_level_prior_session_low, aip_absorption, aip_initiation)
  ```
- **Session limits respected:** Stops after 3 trades or 2 losses

### Red Flags 🚩

- **100% LONG or 100% SHORT** (directional bias bug)
  - **Root Cause:** Setup detection favoring one direction
  - **Fix:** Review setup logic, check key level detection, verify delta calculations
  
- **Entries every candle** (too aggressive)
  - **Root Cause:** Setup thresholds too loose
  - **Fix:** Tighten absorption_volume_mult (2.0 → 2.5), increase absorption_delta_ratio (0.25 → 0.30)
  
- **No entries at all** (too conservative)
  - **Root Cause:** Setup thresholds too strict, or no key levels detected
  - **Fix:** Check key level detection, lower thresholds slightly, verify big picture analysis
  
- **Wrong position sizing** (all trades same size)
  - **Root Cause:** Confirmation counting bug
  - **Fix:** Review `_calculate_position_size()` method, verify confirmations list

### Corrective Actions

| Issue | Diagnosis | Fix |
|-------|-----------|-----|
| 100% LONG bias | Check SHORT setup detection | Review `_detect_setups()`, verify premium/discount logic |
| Too many entries | Setups too loose | Raise `absorption_volume_mult` to 2.5, `absorption_delta_ratio` to 0.30 |
| No entries | Setups too strict or no key levels | Lower thresholds, check `_identify_key_levels()` |
| Wrong sizing | Confirmations not counted | Debug `confirmations` list, verify append logic |

---

## Phase 3: Exit Performance (6-12 Hours)

### What to Look For

1. **Tiered Exits**
   - ✅ Tier 1 (50%) exits at POC
   - ✅ Tier 2 (25%) exits at swing level
   - ✅ Tier 3 (25%) exits with delta signal
   - ✅ Partial exit logs show correct percentages

2. **Stop Loss Hits**
   - ✅ Stop losses trigger when price hits stop_price
   - ✅ Full position closed on SL
   - ✅ P&L calculated correctly

3. **Exit Logging**
   - ✅ All exits logged with P&L
   - ✅ No "reconcile" exits (ghost trades)
   - ✅ Win/loss count accurate

### Green Flags ✅

- **Tiered exits working:**
  ```
  [FH2] 📤 PARTIAL EXIT Tier1_POC (50%) @ 68200 | P&L=$0.1470 | remaining=0.000735 BTC
  [FH2] 📤 PARTIAL EXIT Tier2_Swing (25%) @ 68400 | P&L=$0.1470 | remaining=0.000368 BTC
  [FH2] ✅ EXIT Tier3_Delta LONG @ 68350 | P&L=$0.2573 | total_pnl=$0.5513 (1W/0L)
  ```
- **Win rate 40-60%** (realistic for new strategy)
- **Average win > average loss** (good risk/reward)

### Red Flags 🚩

- **All exits are SL** (stop loss only, no profit exits)
  - **Root Cause:** Tier exit logic not triggering
  - **Fix:** Review `_check_tier1_exit()`, `_check_tier2_exit()`, verify POC calculation
  
- **Reconcile exits** (positions closed on exchange without exit logs)
  - **Root Cause:** Stop loss hit on exchange before strategy detects it
  - **Fix:** Add reconcile exit logging (similar to strategy_v7 Change #14)
  
- **Wrong P&L calculation** (P&L doesn't match exchange)
  - **Root Cause:** Entry/exit price mismatch, or leverage not factored
  - **Fix:** Verify P&L formula, check if using notional or BTC amount
  
- **Tier percentages wrong** (not 50%/25%/25%)
  - **Root Cause:** `portion` parameter incorrect
  - **Fix:** Review `_execute_partial_exit()` calls, verify portion values

### Corrective Actions

| Issue | Diagnosis | Fix |
|-------|-----------|-----|
| All SL exits | Tier logic not working | Debug `_check_tier1_exit()`, verify POC calculation |
| Reconcile exits | SL hit before detection | Add `_reconcile_position()` with fill query |
| Wrong P&L | Calculation error | Verify formula: `(exit - entry) * size` for LONG |
| Wrong percentages | Portion parameter bug | Check `_execute_partial_exit(0.5, ...)` calls |

---

## Phase 4: Performance Analysis (12-24 Hours)

### What to Look For

1. **Win Rate**
   - Target: 45-55% (realistic for order flow)
   - Red flag: <30% or >70%

2. **Risk/Reward**
   - Target: Average win ≥ 1.5x average loss
   - Red flag: Average win < average loss

3. **Entry Quality**
   - Target: 60%+ entries with 3 confirmations
   - Red flag: Most entries with 1 confirmation only

4. **Session Performance**
   - Target: Positive P&L in 2+ sessions
   - Red flag: Negative P&L in all sessions

### Green Flags ✅

- **Win rate 45-55%**
- **Average win $0.50, average loss $0.30** (1.67 R:R)
- **70% of entries have 3 confirmations**
- **Positive P&L in London and NY AM sessions**
- **No trades during lunch** (session rules working)

### Red Flags 🚩

- **Win rate <30%** (strategy not working)
  - **Root Cause:** Poor setup detection or wrong market regime
  - **Fix:** Review trade journal, identify losing patterns, adjust setup logic
  
- **Average win < average loss** (bad R:R)
  - **Root Cause:** Exits too early, or stops too wide
  - **Fix:** Adjust tier exit targets, tighten stop loss placement
  
- **Most entries 1 confirmation** (low quality)
  - **Root Cause:** Setup detection too loose
  - **Fix:** Require minimum 2 confirmations, skip 1-confirmation setups
  
- **Negative P&L all sessions** (fundamental issue)
  - **Root Cause:** Strategy doesn't fit current market regime
  - **Fix:** Pause trading, review big picture analysis, wait for better conditions

### Corrective Actions

| Metric | Target | Red Flag | Fix |
|--------|--------|----------|-----|
| Win Rate | 45-55% | <30% | Review losing trades, adjust setup logic |
| R:R Ratio | ≥1.5 | <1.0 | Adjust tier exits, tighten stops |
| Confirmations | 60%+ with 3 | Most with 1 | Require min 2 confirmations |
| Session P&L | 2+ positive | All negative | Pause, review market regime |

---

## Continuous Monitoring Checklist

### Every Hour
- [ ] Check logs for errors
- [ ] Verify entries are at key levels
- [ ] Confirm exits are logging properly
- [ ] Check win/loss count

### Every 4 Hours
- [ ] Calculate win rate
- [ ] Calculate average win vs average loss
- [ ] Review setup distribution (A/B/C)
- [ ] Check LONG vs SHORT balance

### Every 12 Hours
- [ ] Full trade journal review
- [ ] Identify patterns in losing trades
- [ ] Verify session rules working
- [ ] Check P&L vs exchange balance

---

## Decision Tree

```
Is strategy running without errors?
├─ NO → Fix errors, restart
└─ YES → Continue

Are entries happening?
├─ NO → Check setup thresholds, key level detection
└─ YES → Continue

Are entries balanced (LONG/SHORT)?
├─ NO → Review setup logic, check directional bias
└─ YES → Continue

Are exits logging properly?
├─ NO → Add reconcile logic, check exit methods
└─ YES → Continue

Is win rate 30-70%?
├─ NO → Review trade quality, adjust thresholds
└─ YES → Continue

Is R:R ratio ≥1.0?
├─ NO → Adjust tier exits, tighten stops
└─ YES → Strategy performing well, continue monitoring
```

---

## Summary

**First 2 hours:** Focus on initialization and error-free operation
**Hours 2-6:** Focus on entry quality and frequency
**Hours 6-12:** Focus on exit performance and P&L tracking
**Hours 12-24:** Focus on overall performance metrics and adjustments

**Key Success Metrics:**
- Win rate: 45-55%
- R:R ratio: ≥1.5
- Entry quality: 60%+ with 3 confirmations
- Direction balance: 40-60% LONG/SHORT
- Session rules: Respected (max 3 trades, stop after 2 losses)

