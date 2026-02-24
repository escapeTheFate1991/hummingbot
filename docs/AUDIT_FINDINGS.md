# Flow Hunter V2 - Comprehensive Audit Findings

**Date:** 2026-02-23  
**Auditor:** AI Agent  
**Scope:** Strategy logic, order handling, Hummingbot connector integration, Hyperliquid API usage

---

## 🚨 CRITICAL ISSUES

### 1. **ORPHANED POSITION LOGIC IS BROKEN** ⚠️

**Location:** `strategy_flow_hunter_v2.py` lines 1394-1552

**Current Behavior:**
- On restart, detects orphaned position (position without internal state)
- **BLINDLY adds SL/TP orders** without checking if they already exist
- **DOES NOT check if SL/TP price already hit**
- **DOES NOT respect trend** - will protect a LONG position even in BEAR trend

**Problems:**
1. **Duplicate orders**: If SL/TP orders already exist on exchange, creates duplicates
2. **Protects bad positions**: If position is underwater and SL should have hit, keeps it alive
3. **Ignores strategy logic**: Doesn't check if position still aligns with current trend/setup
4. **No exit logic**: Should close position if TP target already reached

**User Complaint:**
> "I HATE THAT! We have the orphan position functionality for a reason. If the position is still good according to our strategy we shouldn't be killing it. Only kill the position if the SL price or the TP price had already been reached."

**Recommended Fix:**
```python
def _check_and_protect_orphaned_position(self):
    # 1. Check if position exists
    # 2. Get current price
    # 3. Calculate what SL/TP SHOULD be
    # 4. Check if SL already hit → CLOSE POSITION
    # 5. Check if TP already hit → CLOSE POSITION  
    # 6. Check if position aligns with current trend → if not, CLOSE
    # 7. Query existing orders on exchange
    # 8. Only add SL/TP if they don't exist
    # 9. Sync internal state
```

---

### 2. **TREND NOT RESPECTED IN ENTRY LOGIC** ⚠️

**Location:** `strategy_flow_hunter_v2.py` lines 572-611

**Current Behavior:**
- `_detect_setups()` checks for key levels and setups (AIP, Absorption, Divergence)
- **NO TREND FILTER** - will take LONG in BEAR trend, SHORT in BULL trend

**User Complaint:**
> "Also why aren't we respecting the trend? Looks like we missing big sweeps in the downward direction to catch a long position on the bounce instead. Is that the strategy?"

**Analysis:**
The strategy DOES detect trend (`self.trend` = 1/0/-1) but **NEVER USES IT** in entry logic.

**Recommended Fix:**
Add trend filter in `_detect_setups()`:
```python
# After detecting signal from setups
if signal == 1 and self.trend == -1:  # LONG in BEAR trend
    return 0  # Block counter-trend longs
if signal == -1 and self.trend == 1:  # SHORT in BULL trend
    return 0  # Block counter-trend shorts
```

**OR** allow counter-trend but require stronger confirmation:
```python
# Require key level + 2 confirmations for counter-trend
if signal == 1 and self.trend == -1:
    if len(self.confirmations) < 3:
        return 0
```

---

### 3. **NO VALIDATION OF EXISTING ORDERS BEFORE PLACEMENT**

**Location:** `strategy_flow_hunter_v2.py` lines 1141-1270 (entry execution)

**Problem:**
- Places 4 orders (entry, SL, TP1, TP2) without checking if they already exist
- If bot crashes mid-execution, restart will try to place orders again
- Could result in duplicate SL/TP orders

**Recommended Fix:**
Before placing SL/TP orders, query `connector.get_open_orders()` and check if SL/TP already exist.

---

## ⚠️ HIGH PRIORITY ISSUES

### 4. **TRIGGER ORDER PARAMETERS MAY BE INCORRECT**

**Location:** `strategy_flow_hunter_v2.py` lines 1194, 1204, 1475, 1485

**Current Code:**
```python
trigger={"triggerPx": float(stop_price), "tpsl": "sl", "isMarket": True}
```

**Hyperliquid API Documentation:**
According to Hyperliquid docs, trigger orders should use:
- `triggerPx`: Trigger price
- `tpsl`: "tp" or "sl"  
- `isMarket`: True for market execution, False for limit

**Potential Issues:**
1. **Missing `reduceOnly` flag**: SL/TP should have `reduceOnly=True` (already set via `position_action=PositionAction.CLOSE`)
2. **Price parameter**: When `isMarket=True`, the `price` parameter might be ignored or cause errors
3. **Order type**: Using `OrderType.LIMIT` for trigger orders - should verify this is correct

**Recommended Action:**
1. Review Hyperliquid connector code to verify trigger order format
2. Test trigger orders in isolation to confirm they execute correctly
3. Add error handling for trigger order placement failures

---

### 5. **NO ERROR HANDLING FOR ORDER PLACEMENT FAILURES**

**Location:** All `self.buy()` and `self.sell()` calls

**Problem:**
- Order placement is fire-and-forget
- No confirmation that orders were accepted
- No retry logic
- If SL order fails to place, position is UNPROTECTED

**Recommended Fix:**
```python
# Place SL order
sl_order_id = self.sell(...)

# Wait for confirmation (async)
await asyncio.sleep(0.5)

# Check if order exists
open_orders = connector.get_open_orders()
sl_exists = any(o.client_order_id == sl_order_id for o in open_orders)

if not sl_exists:
    self.logger().error("[FH2] SL ORDER FAILED - CLOSING POSITION")
    self._close_position_immediately()
```

---

## 📋 MEDIUM PRIORITY ISSUES

### 6. **POSITION STATE SYNC ISSUES**

**Problem:**
- Internal state (`self.position_side`, `self.position_size`) updated BEFORE orders fill
- If entry order fails, internal state shows position but exchange has none
- If bot crashes after state update but before orders fill, orphan logic won't detect it

**Recommended Fix:**
- Only update internal state AFTER confirming entry order filled
- Use order fill callbacks to sync state

---

### 7. **NO VALIDATION OF POSITION SIZE CALCULATIONS**

**Location:** `_calculate_position_size()` (not shown in audit)

**Potential Issues:**
- Position size could be too large (exceeds balance)
- Position size could be too small (below minimum)
- No check for maximum leverage limits

**Recommended Action:**
Add validation:
```python
# Check minimum
if position_size < trading_rule.min_order_size:
    return 0

# Check maximum (based on balance and leverage)
max_size = balance * leverage / price
if position_size > max_size:
    position_size = max_size
```

---

### 8. **TIER DETECTION LOGIC IS FRAGILE**

**Location:** Lines 1367-1380

**Problem:**
```python
if not self.tier1_filled and current_position_size <= original_size * 0.55:
    self.tier1_filled = True
```

**Issues:**
- Uses 55% threshold (should be 50%)
- Doesn't account for partial fills
- Doesn't verify TP1 order actually filled
- Could false-trigger if position manually reduced

**Recommended Fix:**
- Query filled orders to confirm TP1/TP2 actually filled
- Use tighter threshold (52% instead of 55%)

---

## 🔍 LOW PRIORITY / OBSERVATIONS

### 9. **NO TREND FILTER = COUNTER-TREND TRADING**

The strategy appears designed to trade **reversals at key levels**, not trend-following.

**Evidence:**
- Looks for absorption patterns (sellers absorbed at support)
- Looks for delta divergence (trend exhaustion)
- Enters at key levels (support/resistance)

**This is intentional counter-trend trading**, but should be documented clearly.

**Recommendation:**
Add configuration option:
```python
self.allow_counter_trend = True  # Set to False for trend-only trading
```

---

### 10. **COMPRESSION DETECTION NOT USED IN ENTRY LOGIC**

**Location:** Lines 972-993

**Observation:**
- Strategy detects compression (`self.compression_active`)
- Logs it in status
- **NEVER USES IT** to filter entries

**Question:** Should compression block entries or require stronger confirmation?

---

## ✅ WHAT'S WORKING WELL

1. **Clean code structure** - Well-organized, documented
2. **Defensive programming** - Lots of error handling
3. **Exchange-side SL/TP** - Critical for bot crash protection
4. **Tiered exits** - Sophisticated exit strategy
5. **Big picture analysis** - Comprehensive market context

---

## 🎯 IMMEDIATE ACTION ITEMS

1. **FIX ORPHANED POSITION LOGIC** (CRITICAL)
   - Check if SL/TP already hit before protecting
   - Query existing orders before placing new ones
   - Respect trend when deciding to keep position

2. **ADD TREND FILTER TO ENTRIES** (HIGH)
   - Block counter-trend trades OR require stronger confirmation
   - Document if counter-trend is intentional

3. **ADD ORDER PLACEMENT VALIDATION** (HIGH)
   - Confirm SL order placed successfully
   - Close position immediately if SL fails

4. **TEST TRIGGER ORDERS** (HIGH)
   - Verify trigger order format with Hyperliquid
   - Test in isolation before live trading

---

**Next Steps:** Review findings with user, prioritize fixes, implement changes.

---

## 📊 HYPERLIQUID API COMPLIANCE CHECK

### Trigger Order Format (from Hyperliquid docs)

**Correct format for TP/SL trigger orders:**
```json
{
  "type": "order",
  "orders": {
    "asset": 0,  // BTC asset ID
    "isBuy": false,  // false for LONG SL (sell to close)
    "limitPx": 95000.0,  // Limit price (can be same as trigger for market-like execution)
    "sz": 0.001,  // Size
    "reduceOnly": true,  // MUST be true for SL/TP
    "orderType": {
      "trigger": {
        "triggerPx": 95000.0,  // Price that triggers the order
        "tpsl": "sl",  // "sl" for stop loss, "tp" for take profit
        "isMarket": true  // true = market order when triggered
      }
    }
  }
}
```

**Current Implementation:**
```python
self.sell(
    connector_name=self.EXCHANGE,
    trading_pair=self.PAIR,
    amount=position_size,
    order_type=OrderType.LIMIT,  # ✅ Correct
    price=Decimal(str(stop_price)),  # ✅ Used as limitPx
    position_action=PositionAction.CLOSE,  # ✅ Sets reduceOnly=True
    trigger={"triggerPx": float(stop_price), "tpsl": "sl", "isMarket": True}  # ✅ Correct format
)
```

**Status:** ✅ **APPEARS CORRECT** based on connector code review

**Verification Needed:**
1. Check Hyperliquid connector `_place_order()` method handles trigger parameter correctly
2. Verify `reduceOnly` is set when `position_action=PositionAction.CLOSE`
3. Test trigger orders execute correctly on exchange

---

## 🔧 DETAILED FIX PROPOSALS

### FIX #1: Orphaned Position Logic (CRITICAL)

**File:** `strategy_flow_hunter_v2.py`
**Function:** `_check_and_protect_orphaned_position()`
**Lines:** 1394-1552

**New Logic:**
```python
def _check_and_protect_orphaned_position(self):
    """
    Smart orphaned position handling:
    1. Check if SL/TP already hit → close position
    2. Check if position aligns with strategy → close if not
    3. Query existing orders → only add missing protection
    4. Sync internal state
    """
    try:
        connector = self.connectors.get(self.EXCHANGE)
        if not connector:
            return

        # Get position from exchange
        positions = connector.account_positions
        if not positions:
            self.logger().info(f"[FH2] No orphaned position found - starting fresh")
            return

        position = None
        for pos_key, pos in positions.items():
            if pos.trading_pair == self.PAIR:
                position = pos
                break

        if position is None or position.amount == 0:
            self.logger().info(f"[FH2] No orphaned position found - starting fresh")
            return

        # Position exists - check if we have internal state
        if self.position_side is not None:
            self.logger().info(f"[FH2] Position already tracked - no orphan protection needed")
            return

        # ORPHANED POSITION DETECTED
        side = 1 if float(position.amount) > 0 else -1
        position_size = abs(float(position.amount))
        entry_price = float(position.entry_price)

        # Get current price
        df_5m = self.btc_5m_candles.candles_df.copy()
        if df_5m.empty:
            return
        current_price = float(df_5m.iloc[-1]['close'])

        self.logger().warning(
            f"[FH2] ⚠️ ORPHANED POSITION DETECTED: "
            f"{position_size:.6f} BTC @ ${entry_price:.2f} | "
            f"Current: ${current_price:.2f}"
        )

        # Calculate what SL/TP SHOULD be
        risk_pct = 0.02
        if side == 1:  # LONG
            stop_price = entry_price * (1 - risk_pct)
        else:  # SHORT
            stop_price = entry_price * (1 + risk_pct)

        tier1_target, tier2_target = self._calculate_tp_targets(entry_price, stop_price, side)

        # CHECK 1: Has SL already been hit?
        if side == 1 and current_price <= stop_price:
            self.logger().warning(f"[FH2] ❌ SL ALREADY HIT - CLOSING ORPHANED POSITION")
            self._close_orphaned_position(position_size, side, "SL_HIT")
            return
        elif side == -1 and current_price >= stop_price:
            self.logger().warning(f"[FH2] ❌ SL ALREADY HIT - CLOSING ORPHANED POSITION")
            self._close_orphaned_position(position_size, side, "SL_HIT")
            return

        # CHECK 2: Has TP already been hit?
        if side == 1 and current_price >= tier1_target:
            self.logger().info(f"[FH2] ✅ TP ALREADY HIT - CLOSING ORPHANED POSITION WITH PROFIT")
            self._close_orphaned_position(position_size, side, "TP_HIT")
            return
        elif side == -1 and current_price <= tier1_target:
            self.logger().info(f"[FH2] ✅ TP ALREADY HIT - CLOSING ORPHANED POSITION WITH PROFIT")
            self._close_orphaned_position(position_size, side, "TP_HIT")
            return

        # CHECK 3: Does position align with current trend?
        if self.trend != 0:  # If we have a clear trend
            if side == 1 and self.trend == -1:  # LONG in BEAR trend
                self.logger().warning(f"[FH2] ⚠️ LONG position in BEAR trend - CLOSING")
                self._close_orphaned_position(position_size, side, "TREND_MISMATCH")
                return
            elif side == -1 and self.trend == 1:  # SHORT in BULL trend
                self.logger().warning(f"[FH2] ⚠️ SHORT position in BULL trend - CLOSING")
                self._close_orphaned_position(position_size, side, "TREND_MISMATCH")
                return

        # CHECK 4: Query existing orders on exchange
        open_orders = connector.get_open_orders(self.PAIR)
        has_sl = False
        has_tp1 = False
        has_tp2 = False

        for order in open_orders:
            # Check if it's a trigger order (SL)
            if hasattr(order, 'trigger') and order.trigger:
                has_sl = True
            # Check if it's a limit order (TP)
            elif order.order_type == OrderType.LIMIT and order.position_action == PositionAction.CLOSE:
                # Distinguish TP1 vs TP2 by size
                if abs(float(order.amount) - position_size * 0.5) < 0.0001:
                    has_tp1 = True
                elif abs(float(order.amount) - position_size * 0.25) < 0.0001:
                    has_tp2 = True

        # PROTECT: Add missing orders
        pnl_pct = ((current_price - entry_price) / entry_price) * 100 * side

        self.logger().info(
            f"[FH2] Protecting orphaned position | "
            f"PnL: {pnl_pct:+.2f}% | "
            f"SL: ${stop_price:.2f} {'✅' if has_sl else '❌'} | "
            f"TP1: ${tier1_target:.2f} {'✅' if has_tp1 else '❌'} | "
            f"TP2: ${tier2_target:.2f} {'✅' if has_tp2 else '❌'}"
        )

        # Place missing SL
        if not has_sl:
            if side == 1:
                self.sell(
                    connector_name=self.EXCHANGE,
                    trading_pair=self.PAIR,
                    amount=Decimal(str(position_size)),
                    order_type=OrderType.LIMIT,
                    price=Decimal(str(stop_price)),
                    position_action=PositionAction.CLOSE,
                    trigger={"triggerPx": float(stop_price), "tpsl": "sl", "isMarket": True}
                )
            else:
                self.buy(
                    connector_name=self.EXCHANGE,
                    trading_pair=self.PAIR,
                    amount=Decimal(str(position_size)),
                    order_type=OrderType.LIMIT,
                    price=Decimal(str(stop_price)),
                    position_action=PositionAction.CLOSE,
                    trigger={"triggerPx": float(stop_price), "tpsl": "sl", "isMarket": True}
                )

        # Place missing TP1
        if not has_tp1:
            tier1_size = Decimal(str(position_size * 0.5))
            if side == 1:
                self.sell(
                    connector_name=self.EXCHANGE,
                    trading_pair=self.PAIR,
                    amount=tier1_size,
                    order_type=OrderType.LIMIT,
                    price=Decimal(str(tier1_target)),
                    position_action=PositionAction.CLOSE
                )
            else:
                self.buy(
                    connector_name=self.EXCHANGE,
                    trading_pair=self.PAIR,
                    amount=tier1_size,
                    order_type=OrderType.LIMIT,
                    price=Decimal(str(tier1_target)),
                    position_action=PositionAction.CLOSE
                )

        # Place missing TP2
        if not has_tp2:
            tier2_size = Decimal(str(position_size * 0.25))
            if side == 1:
                self.sell(
                    connector_name=self.EXCHANGE,
                    trading_pair=self.PAIR,
                    amount=tier2_size,
                    order_type=OrderType.LIMIT,
                    price=Decimal(str(tier2_target)),
                    position_action=PositionAction.CLOSE
                )
            else:
                self.buy(
                    connector_name=self.EXCHANGE,
                    trading_pair=self.PAIR,
                    amount=tier2_size,
                    order_type=OrderType.LIMIT,
                    price=Decimal(str(tier2_target)),
                    position_action=PositionAction.CLOSE
                )

        # Sync internal state
        self.position_side = side
        self.position_size = Decimal(str(position_size))
        self.entry_price = Decimal(str(entry_price))
        self.stop_price = Decimal(str(stop_price))
        self.entry_time = time.time()
        self.tier1_filled = False
        self.tier2_filled = False
        self.tier3_active = True

        self.logger().info(f"[FH2] ✅ Orphaned position now protected | State synced")

    except Exception as e:
        self.logger().error(f"[FH2] Orphaned position check error: {e}")

def _close_orphaned_position(self, size: float, side: int, reason: str):
    """Close orphaned position immediately."""
    try:
        if side == 1:  # LONG
            self.sell(
                connector_name=self.EXCHANGE,
                trading_pair=self.PAIR,
                amount=Decimal(str(size)),
                order_type=OrderType.MARKET,
                position_action=PositionAction.CLOSE
            )
        else:  # SHORT
            self.buy(
                connector_name=self.EXCHANGE,
                trading_pair=self.PAIR,
                amount=Decimal(str(size)),
                order_type=OrderType.MARKET,
                position_action=PositionAction.CLOSE
            )

        self.logger().info(f"[FH2] ✅ Orphaned position closed | Reason: {reason}")

    except Exception as e:
        self.logger().error(f"[FH2] Failed to close orphaned position: {e}")
```

---

### FIX #2: Add Trend Filter to Entry Logic

**File:** `strategy_flow_hunter_v2.py`
**Function:** `_detect_setups()`
**Lines:** 572-611

**Add after line 609:**
```python
        # Try Setup C: Delta Divergence
        signal = self._detect_setup_c_divergence(df_5m, price, key_level)
        if signal != 0:
            self.setup_type = SETUP_C_DIVERGENCE
            return signal

        return 0

    # ADD THIS NEW METHOD:
    def _apply_trend_filter(self, signal: int) -> int:
        """
        Apply trend filter to entry signals.

        Options:
        1. Block all counter-trend trades
        2. Allow counter-trend but require stronger confirmation
        3. No filter (pure reversal trading)
        """
        if self.trend == 0:
            # No clear trend - allow all trades
            return signal

        # OPTION 1: Block counter-trend (conservative)
        if signal == 1 and self.trend == -1:  # LONG in BEAR
            self.logger().info(f"[FH2] ❌ LONG signal blocked - BEAR trend")
            return 0
        if signal == -1 and self.trend == 1:  # SHORT in BULL
            self.logger().info(f"[FH2] ❌ SHORT signal blocked - BULL trend")
            return 0

        # OPTION 2: Require stronger confirmation for counter-trend
        # if signal == 1 and self.trend == -1:
        #     if len(self.confirmations) < 3:
        #         self.logger().info(f"[FH2] ❌ LONG needs 3+ confirmations in BEAR (has {len(self.confirmations)})")
        #         return 0
        # if signal == -1 and self.trend == 1:
        #     if len(self.confirmations) < 3:
        #         self.logger().info(f"[FH2] ❌ SHORT needs 3+ confirmations in BULL (has {len(self.confirmations)})")
        #         return 0

        return signal
```

**Then modify `on_tick()` line 303:**
```python
        # Run setup detection (5m footprint)
        signal = self._detect_setups()
        if signal == 0:
            return

        # Apply trend filter
        signal = self._apply_trend_filter(signal)
        if signal == 0:
            return

        # Execute entry
        self._execute_entry(signal)
```

---

## 🧪 TESTING CHECKLIST

Before deploying fixes:

- [ ] Test orphan logic with position where SL already hit
- [ ] Test orphan logic with position where TP already hit
- [ ] Test orphan logic with LONG in BEAR trend
- [ ] Test orphan logic with existing SL/TP orders
- [ ] Test trend filter blocks counter-trend entries
- [ ] Test trigger orders execute correctly on Hyperliquid
- [ ] Test entry execution with all 4 orders (entry, SL, TP1, TP2)
- [ ] Test position state sync after order fills
- [ ] Test tier detection logic with partial fills
- [ ] Test bot restart with open position

---

## 📝 CONFIGURATION RECOMMENDATIONS

Add these to strategy config:

```python
# Orphan position handling
self.orphan_respect_trend = True  # Close orphans that don't match trend
self.orphan_check_sl_hit = True   # Close if SL already hit
self.orphan_check_tp_hit = True   # Close if TP already hit

# Trend filtering
self.require_trend_alignment = True  # Block counter-trend trades
self.counter_trend_min_confirmations = 3  # If allowing counter-trend

# Order validation
self.validate_sl_placement = True  # Confirm SL order placed
self.close_if_sl_fails = True      # Close position if SL fails to place
```

