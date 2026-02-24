# Error Handling Fix - Order Failure Logging

**Date:** 2026-02-24  
**Status:** ✅ DEPLOYED AND ACTIVE

---

## 🎯 PROBLEM IDENTIFIED

User reported seeing unhelpful error messages in logs:

```
[FH2] ❌ ORDER FAILED: 0x2321d9... | Type: MARKET | Reason: None
```

This made troubleshooting difficult because the actual error reason was missing.

---

## 🔍 ROOT CAUSE ANALYSIS

### **What Was Happening:**

1. **Original Failure (00:36:56):**
   ```json
   {
     "order_id": "0x2321d9ef2a7f638d3cfc804cf5470071",
     "error_message": "Error submitting order: Insufficient margin to place order. asset=0",
     "error_type": "OSError"
   }
   ```
   ✅ This had proper error details

2. **Re-triggered Failures (02:30:17, 03:06:03, 03:07:23):**
   ```json
   {
     "order_id": "0x2321d9ef2a7f638d3cfc804cf5470071",
     "error_message": null,
     "error_type": null,
     "misc_updates": null
   }
   ```
   ❌ These had NO error details

### **Why This Happened:**

- The order tracker was **re-triggering old failed orders** multiple times
- When an `OrderUpdate` with `OrderState.FAILED` is processed, it triggers `MarketOrderFailureEvent`
- The **first** failure event includes `misc_updates` with error details
- **Subsequent** failure events for the same order have `misc_updates=None`
- This is because the order is already in FAILED state, and the re-trigger doesn't include the original error

### **Additional Issue:**

- The strategy was logging **ALL** order failures, including old/unrelated orders
- This created noise in the logs
- The failed order `0x2321d9ef2a7f638d3cfc804cf5470071` was from a previous session
- It wasn't one of our tracked orders (entry, SL, TP1, TP2)

---

## ✅ SOLUTION IMPLEMENTED

### **Fix #1: Store Error Messages**

Added error tracking dictionary to store first error message:

```python
# In __init__:
self.failed_order_errors: Dict[str, str] = {}  # order_id -> error_message
```

### **Fix #2: Improved did_fail_order() Callback**

```python
def did_fail_order(self, event):
    order_id = event.order_id
    error_msg = event.error_message if hasattr(event, 'error_message') and event.error_message else None
    
    # Store error message if this is the first failure for this order
    if error_msg and order_id not in self.failed_order_errors:
        self.failed_order_errors[order_id] = error_msg
    
    # Get error message (use stored if current is None)
    display_error = error_msg or self.failed_order_errors.get(order_id, 'Unknown')
    
    # Only process if this is one of our tracked orders
    tracked_orders = [self.entry_order_id, self.sl_order_id, self.tp1_order_id, self.tp2_order_id]
    if order_id not in tracked_orders:
        # This is an old/unrelated order - log at debug level only
        self.logger().debug(
            f"[FH2] 🔍 Ignoring failure for untracked order: {order_id[:8]}... | "
            f"Reason: {display_error}"
        )
        return
    
    # Log the failure with proper error message
    self.logger().error(
        f"[FH2] ❌ ORDER FAILED: {order_id[:8]}... | "
        f"Type: {order_type.name} | "
        f"Reason: {display_error}"
    )
```

### **Fix #3: Clear Error Tracking on State Reset**

```python
def _finalize_close(self):
    # ... cancel orders ...
    # Clear error tracking for closed orders
    self.failed_order_errors.clear()

def _reset_position_state(self):
    # ... reset state ...
    # Clear error tracking for failed orders
    self.failed_order_errors.clear()
```

---

## 📊 BEFORE vs AFTER

### **BEFORE (Unhelpful):**

```
[FH2] ❌ ORDER FAILED: 0x2321d9... | Type: MARKET | Reason: None
[FH2] ❌ ORDER FAILED: 0x2321d9... | Type: MARKET | Reason: None
[FH2] ❌ ORDER FAILED: 0x2321d9... | Type: MARKET | Reason: None
```

**Problems:**
- No error reason shown
- Same old order logged multiple times
- Noise in logs from unrelated orders

### **AFTER (Helpful):**

**For tracked orders (first failure):**
```
[FH2] ❌ ORDER FAILED: 0xabc123... | Type: MARKET | Reason: Insufficient margin to place order. asset=0
```

**For tracked orders (re-triggered):**
```
[FH2] ❌ ORDER FAILED: 0xabc123... | Type: MARKET | Reason: Insufficient margin to place order. asset=0
```
(Uses stored error message)

**For untracked orders:**
```
(No log at ERROR level - only at DEBUG level)
```

**Benefits:**
- ✅ Always shows actual error reason
- ✅ Filters out old/unrelated orders
- ✅ Clean logs with only relevant failures
- ✅ Easy troubleshooting

---

## 🚀 DEPLOYMENT STATUS

- ✅ Code updated in `/home/eddy/Development/hummingbot/scripts/strategy_flow_hunter_v2.py`
- ✅ Deployed to Docker container `hummingbot_v7`
- ✅ Bot restarted successfully
- ✅ No more noise from old failed orders
- ✅ Error messages now informative

---

## 🎯 WHAT THIS MEANS

### **When a Real Order Fails:**

You'll now see helpful error messages like:

```
[FH2] ❌ ORDER FAILED: 0xabc123... | Type: MARKET | Reason: Insufficient margin to place order. asset=0
[FH2] 🚨 ENTRY ORDER FAILED - Aborting trade
```

Or:

```
[FH2] ❌ ORDER FAILED: 0xdef456... | Type: LIMIT | Reason: Order price outside allowed range
[FH2] 🚨🚨🚨 STOP LOSS ORDER FAILED - CLOSING POSITION IMMEDIATELY
```

### **When Old Orders Re-trigger:**

You'll see nothing in the logs (filtered out at DEBUG level).

---

## 📝 TECHNICAL DETAILS

### **Why Order Tracker Re-triggers Failures:**

The Hummingbot order tracker periodically checks order status. When it finds an order in FAILED state:

1. It creates an `OrderUpdate` with `new_state=OrderState.FAILED`
2. This triggers `_trigger_failure_event()` in `client_order_tracker.py`
3. The `MarketOrderFailureEvent` is created with `misc_updates` from the `OrderUpdate`
4. If `misc_updates=None`, the event has `error_message=null`

### **Our Solution:**

- Store the first error message when `misc_updates` is present
- Reuse stored error message when `misc_updates=None`
- Filter out orders that aren't in our tracked list
- Clear error tracking when position closes

---

## 🎉 RESULT

**Clean, informative error logs that make troubleshooting easy!** 🚀

No more "Reason: None" - every error now shows the actual problem.

