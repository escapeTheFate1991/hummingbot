# Flow Hunter V2 - Hummingbot Integration Audit

**Date:** 2026-02-24  
**Scope:** Strategy-to-Hummingbot integration, order lifecycle, state synchronization, error handling

---

## 🎯 EXECUTIVE SUMMARY

This audit examines the integration between Flow Hunter V2 strategy and the Hummingbot framework, focusing on:
- Order flow from strategy → base → connector → exchange
- Position state synchronization
- Error handling and propagation
- Communication gaps and anti-patterns

**Critical Findings:** 7 issues identified (2 critical, 3 high, 2 medium)

---

## 📊 ORDER LIFECYCLE ANALYSIS

### **Flow Diagram:**
```
Strategy (FH2)
    ↓ self.buy()/sell() with **kwargs
ScriptStrategyBase
    ↓ connector.buy()/sell() [if kwargs] OR buy_with_specific_market() [if no kwargs]
Hyperliquid Connector
    ↓ safe_ensure_future(_create_order())
    ↓ _place_order() [async]
    ↓ _api_post() to Hyperliquid
Exchange
    ↓ WebSocket order updates
Connector (order tracker)
    ↓ process_order_update()
    ↓ Trigger events (OrderFilledEvent, etc.)
Strategy (did_fill_order callback)
```

---

## 🚨 CRITICAL ISSUES

### **ISSUE #1: Order Placement is Fire-and-Forget (CRITICAL)**

**Location:** `hyperliquid_perpetual_derivative.py:454-462`, `hyperliquid_perpetual_derivative.py:491-499`

**Problem:**
```python
def buy(self, trading_pair, amount, order_type, price, **kwargs) -> str:
    order_id = get_new_client_order_id(...)
    hex_order_id = f"0x{md5.hexdigest()}"
    
    safe_ensure_future(self._create_order(...))  # ❌ Fire and forget!
    return hex_order_id  # Returns BEFORE order is placed
```

**Impact:**
- Strategy receives order ID immediately
- Order placement happens asynchronously
- **No way to know if order actually succeeded**
- Errors in `_place_order()` are caught but **NOT propagated back to strategy**
- Strategy logs "✅ Order placed" even if it failed

**Evidence from your case:**
- Entry order succeeded (position opened)
- SL/TP orders failed silently (ZERO orders on exchange)
- Strategy had no idea orders failed

**Root Cause:**
- `safe_ensure_future()` schedules async task but doesn't wait
- Errors are logged in connector but strategy never sees them
- No callback mechanism to notify strategy of failures

**Fix Required:**
1. Add order validation after placement
2. Query open orders to confirm SL/TP exist
3. Retry failed orders
4. Close position if SL fails after retries

---

### **ISSUE #2: Position State Sync is Eventually Consistent (CRITICAL)**

**Location:** `strategy_flow_hunter_v2.py:1249-1260`

**Problem:**
```python
# Update state IMMEDIATELY after calling buy/sell
self.position_side = signal
self.entry_price = Decimal(str(price))
self.position_size = position_size
# ... but order hasn't filled yet!
```

**Impact:**
- Strategy state updated before order fills
- If entry order fails, strategy thinks it has a position but doesn't
- If bot crashes between order placement and fill, state is wrong
- Orphan logic will try to protect non-existent position

**Correct Pattern (from strategy_v7.py):**
```python
# DON'T update state in _execute_entry()
# ONLY update in did_fill_order() callback
def did_fill_order(self, event: OrderFilledEvent):
    if event.trade_type == TradeType.BUY:
        self.position_side = 1
        self.entry_price = event.price
```

**Fix Required:**
- Remove state updates from `_execute_entry()`
- Add `did_fill_order()` callback
- Update state only when fill event received
- Handle partial fills correctly

---

## ⚠️ HIGH PRIORITY ISSUES

### **ISSUE #3: No Error Propagation from Connector to Strategy**

**Location:** Integration gap between connector and strategy

**Problem:**
- Connector catches errors in `_place_order()` and raises `IOError`
- Error is caught by `_create_order()` wrapper
- Error triggers `OrderUpdate` with `OrderState.FAILED`
- **Strategy has NO callback for failed orders**

**Missing Callback:**
```python
# Strategy needs this but doesn't have it:
def did_fail_order(self, event: MarketOrderFailureEvent):
    self.logger().error(f"Order {event.order_id} failed: {event.error_message}")
    # Handle failure (retry, close position, etc.)
```

**Fix Required:**
- Add `did_fail_order()` callback to strategy
- Listen for `MarketEvent.OrderFailure` events
- Implement retry logic or emergency position close

---

### **ISSUE #4: Orphan Logic Doesn't Query Existing Orders**

**Location:** `strategy_flow_hunter_v2.py:1454-1552`

**Problem:**
```python
def _check_and_protect_orphaned_position(self):
    # Detects orphaned position ✅
    # Calculates SL/TP prices ✅
    # Places SL/TP orders ❌ WITHOUT checking if they already exist!
```

**Impact:**
- Could create duplicate SL/TP orders
- Could protect position that already has protection
- Wastes API calls and creates confusion

**Fix Required:**
- Query `connector.get_open_orders(self.PAIR)` first
- Check which orders already exist
- Only place missing orders
- Implemented in audit document (lines 278-672)

---

### **ISSUE #5: No Validation That Orders Exist on Exchange**

**Location:** Strategy has no post-placement validation

**Problem:**
- Strategy places 4 orders (entry, SL, TP1, TP2)
- Assumes all succeeded
- **Never checks if they actually exist on exchange**

**Fix Required:**
```python
# After entry execution, validate orders exist:
await asyncio.sleep(2)  # Wait for orders to propagate
open_orders = connector.get_open_orders(self.PAIR)

has_sl = any(hasattr(o, 'trigger') and o.trigger for o in open_orders)
if not has_sl:
    self.logger().error("[FH2] ❌ SL order missing - CLOSING POSITION")
    self._close_position_immediately()
```

---

## 📋 MEDIUM PRIORITY ISSUES

### **ISSUE #6: Position Query Uses Property Instead of Method**

**Location:** `strategy_flow_hunter_v2.py:1410`

**Current:**
```python
positions = connector.account_positions  # Property
```

**Analysis:**
- This is actually CORRECT for Hummingbot perpetual connectors
- `account_positions` is a property that returns cached position dict
- Updated by `_update_positions()` polling loop every 5-12 seconds

**Potential Issue:**
- Positions might be stale (up to 12 seconds old)
- For critical decisions, might want to force refresh

**Recommendation:**
- Current approach is fine for most cases
- For critical checks (orphan logic), consider forcing position update

---

### **ISSUE #7: No Handling of Partial Fills for SL/TP Orders**

**Location:** Entry execution doesn't track SL/TP order IDs

**Problem:**
```python
# Strategy places SL/TP but doesn't store order IDs
sl_order_id = self.sell(...)  # ✅ Gets ID
# ... but never stores it!
```

**Impact:**
- Can't track if SL/TP orders fill
- Can't cancel SL/TP when position closes
- Orphaned orders might remain on exchange

**Fix Required:**
```python
# Store order IDs
self.sl_order_id = sl_order_id
self.tp1_order_id = tp1_order_id
self.tp2_order_id = tp2_order_id

# Cancel remaining orders when position closes
def _finalize_close(self):
    if self.sl_order_id:
        self.cancel(self.EXCHANGE, self.PAIR, self.sl_order_id)
    if self.tp1_order_id:
        self.cancel(self.EXCHANGE, self.PAIR, self.tp1_order_id)
    # ...
```

---

## 🔍 COMMUNICATION GAPS

### **Gap #1: Strategy → Connector (Order Placement)**
- **Current:** Fire-and-forget with no confirmation
- **Should Be:** Async/await with error handling OR callback on failure

### **Gap #2: Connector → Strategy (Order Failures)**
- **Current:** Errors logged but not propagated
- **Should Be:** `did_fail_order()` callback

### **Gap #3: Connector → Strategy (Position Updates)**
- **Current:** Strategy polls `account_positions` property
- **Should Be:** Consider adding position update callback (optional)

### **Gap #4: Strategy → Exchange (Order Validation)**
- **Current:** No validation that orders exist
- **Should Be:** Query open orders after placement

---

## 🎨 ANTI-PATTERNS DETECTED

### **Anti-Pattern #1: Optimistic State Updates**
```python
# ❌ BAD: Update state before confirmation
self.buy(...)
self.position_side = 1  # Assumes order will fill

# ✅ GOOD: Update state on fill event
def did_fill_order(self, event):
    self.position_side = 1
```

### **Anti-Pattern #2: Silent Failures**
```python
# ❌ BAD: No error handling
order_id = self.sell(trigger={...})
self.logger().info("✅ SL placed")  # Might have failed!

# ✅ GOOD: Validate placement
order_id = self.sell(trigger={...})
await asyncio.sleep(1)
if not self._verify_order_exists(order_id):
    self.logger().error("❌ SL failed")
```

### **Anti-Pattern #3: Fire-and-Forget Async**
```python
# ❌ BAD: Schedule task and forget
safe_ensure_future(self._create_order(...))
return order_id  # Returns immediately

# ✅ GOOD: Wait for result
result = await self._create_order(...)
return result
```

---

## 📝 RECOMMENDATIONS

### **Immediate (Critical):**
1. ✅ **Add order placement logging** (DONE)
2. ✅ **Add trend filter** (DONE)
3. ⏳ **Implement order validation**
4. ⏳ **Add did_fail_order() callback**
5. ⏳ **Move state updates to did_fill_order()**

### **High Priority:**
6. ⏳ **Implement smart orphan logic** (code ready in AUDIT_FINDINGS.md)
7. ⏳ **Add order ID tracking for SL/TP**
8. ⏳ **Add order cancellation on position close**

### **Medium Priority:**
9. Consider position update callbacks
10. Add retry logic for failed orders
11. Add health check for open orders

---

## 🧪 TESTING CHECKLIST

- [ ] Test order placement with network failure
- [ ] Test order placement with API error
- [ ] Test bot restart with open position
- [ ] Test bot restart with pending orders
- [ ] Test SL trigger execution
- [ ] Test TP limit order fills
- [ ] Test partial fills
- [ ] Test position close with orphaned orders
- [ ] Test trend filter blocking entries
- [ ] Test orphan logic with existing orders

---

**Next Steps:** Implement fixes in priority order, test thoroughly before live deployment.

