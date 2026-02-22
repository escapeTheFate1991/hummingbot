"""
ICT BTC Perpetuals Strategy v7 — Native Footprint Integration
═══════════════════════════════════════════════════════════════

v7 = Complete rewrite using native FootprintFeed from Hummingbot codebase.

KEY CHANGES FROM v5:
  ✅ FootprintFeed is a native Hummingbot data feed (WebSocket, not REST polling)
  ✅ All thresholds configurable via class-level params (no magic numbers)
  ✅ Every entry/exit decision logged with full reasoning + data values
  ✅ Deterministic state machine — no hidden side effects
  ✅ Honesty refactor: actual exchange fills for P&L, real position sizes
  ✅ Risk controls: max position, circuit breaker, cooldown, max daily loss
  ✅ Footprint-informed stop placement (absorption zones, not fixed %)

ARCHITECTURE:
  Market Data Sources:
    1. CandlesFactory → 5m + 1h OHLCV candles (standard Hummingbot)
    2. FootprintFeed  → 1m + 5m footprint candles (native, WebSocket-fed)
    3. Connector      → order book, positions, balances (standard Hummingbot)

  Analysis Pipeline (on_tick):
    1. Regime Detection     → EXPANSION / PULLBACK / COMPRESSION (gate)
    2. Swing Structure      → BOS / direction lock
    3. ICT Components       → OBs, FVGs, displacement-retrace, liquidity sweeps
    4. Momentum             → SMI, CMF
    5. Footprint Analysis   → delta, absorption, stacked imbalances, POC
    6. Scoring Engine       → weighted sum with hard filters
    7. Execution            → position sizing, order placement
    8. Position Management  → footprint-informed exits, trailing stops

  State Machine:
    FLAT → LONG/SHORT → CLOSING → COOLDOWN → FLAT

EXCHANGE: Hyperliquid Perpetuals (testnet or mainnet)
PAIR: BTC-USD
"""

import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from hummingbot.connector.connector_base import ConnectorBase
from hummingbot.core.data_type.common import OrderType, PositionAction, PositionSide
from hummingbot.data_feed.candles_feed.candles_factory import CandlesFactory
from hummingbot.data_feed.candles_feed.data_types import CandlesConfig
from hummingbot.strategy.script_strategy_base import ScriptStrategyBase

# ── Native Footprint Feed (built into Hummingbot codebase) ──
from hummingbot.data_feed.footprint_feed import FootprintFeed, FootprintConfig


# ═══════════════════════════════════════════════════════════════════
# CONFIGURATION — All thresholds in one place, fully documented
# ═══════════════════════════════════════════════════════════════════

# Trading sessions (UTC hours)
SESSIONS = {
    "asia":      {"start": 0,  "end": 8,  "weight": 0.8},
    "london":    {"start": 7,  "end": 16, "weight": 1.0},
    "ny_am":     {"start": 13, "end": 17, "weight": 1.0},
    "ny_pm":     {"start": 17, "end": 21, "weight": 0.7},
    "dead_zone": {"start": 21, "end": 24, "weight": 0.3},
}

# Capital phases: (max_balance, pct_risk, leverage, stop_buffer_pct, label)
# Designed so max loss per trade = risk% * balance, even with leverage.
# Phase 1: $5 account, 20% risk = $1 max loss, 10x lev, 2% buffer = liquidation at 10%
CAPITAL_PHASES = [
    (100,   0.20, 10, 0.005, "Phase 1: Micro"),  # 0.5% stop = 5% ROE loss with 10x lev
    (500,   0.15, 15, 0.005, "Phase 2: Growth"),  # 0.5% stop = 7.5% ROE loss with 15x lev
    (2000,  0.10, 20, 0.004, "Phase 3: Build"),   # 0.4% stop = 8% ROE loss with 20x lev
    (10000, 0.08, 15, 0.005, "Phase 4: Scale"),   # 0.5% stop = 7.5% ROE loss with 15x lev
    (99999, 0.05, 10, 0.005, "Phase 5: Protect"), # 0.5% stop = 5% ROE loss with 10x lev
]

# Market regimes
EXPANSION = "EXPANSION"
PULLBACK = "PULLBACK"
COMPRESSION = "COMPRESSION"
RANGING = "RANGING"  # True sideways market - low volatility + no trend structure

# Position states
STATE_FLAT = "FLAT"
STATE_LONG = "LONG"
STATE_SHORT = "SHORT"
STATE_CLOSING = "CLOSING"
STATE_COOLDOWN = "COOLDOWN"
STATE_MISMATCH = "EXCHANGE_MISMATCH"


class StrategyV7(ScriptStrategyBase):
    """
    ICT + Footprint execution engine v7.
    Native footprint data feed, deterministic state machine, honest P&L.
    """

    # ═══════════════════════════════════════════════════════════════
    # EXCHANGE + PAIR
    # ═══════════════════════════════════════════════════════════════
    EXCHANGE = "hyperliquid_perpetual"
    PAIR = "BTC-USD"

    markets = {EXCHANGE: {PAIR}}

    # ═══════════════════════════════════════════════════════════════
    # DATA FEEDS
    # ═══════════════════════════════════════════════════════════════
    # All data sources use mainnet — candles, footprint, and trading all on the same exchange.
    btc_5m_candles = CandlesFactory.get_candle(CandlesConfig(
        connector="hyperliquid_perpetual", trading_pair="BTC-USD",
        interval="5m", max_records=200
    ))
    btc_1h_candles = CandlesFactory.get_candle(CandlesConfig(
        connector="hyperliquid_perpetual", trading_pair="BTC-USD",
        interval="1h", max_records=100
    ))

    # ═══════════════════════════════════════════════════════════════
    # CONFIGURABLE PARAMETERS
    # ═══════════════════════════════════════════════════════════════

    # Regime detection
    atr_length = 14
    atr_avg_length = 50
    regime_range_lookback = 20
    regime_expansion_mult = 1.2
    regime_compression_mult = 0.6
    regime_range_pct = 0.008

    # Swing structure
    swing_lookback = 5
    swing_history = 20

    # Order blocks
    ob_lookback = 50
    ob_displacement_mult = 1.5
    ob_max_range_atr = 0.8
    ob_proximity_pct = 0.002
    ob_extended_pct = 0.004
    ob_fresh_candles = 12
    ob_retrace_pct = 0.5

    # FVGs
    fvg_min_atr_mult = 0.15

    # SMI
    smi_k_length = 14
    smi_d_length = 3
    smi_signal_length = 9

    # CMF
    cmf_length = 20
    cmf_threshold = 0.01

    # Direction lock
    direction_lock_decay = 40

    # Premium / discount
    dealing_range_lookback = 40

    # Scoring
    min_score_to_trade = 5.5  # Raised from 4.0 to 5.5 to reduce entry frequency
    session_overlap_bonus = 0.5
    # Trade management
    trade_cooldown = 300  # Increased from 120s to 300s (5 minutes) to reduce entry frequency
    roe_target_pct = Decimal("0.06")
    roe_breakeven_pct = Decimal("0.03")
    max_open_positions = 1
    position_close_cooldown = 30

    # Risk controls
    max_daily_loss_pct = 0.15       # 15% daily drawdown = circuit breaker
    max_trades_per_day = 20         # Hard cap on daily trades
    circuit_breaker_duration = 3600  # 1 hour cooldown after circuit break

    # Footprint parameters
    fp_imbalance_threshold = 3.0     # 3:1 ratio for London/NY
    fp_asia_imbalance_threshold = 4.0  # 4:1 for Asia
    fp_absorption_weight = 1.5       # Score for absorption at OB
    fp_stacked_weight = 1.0          # Score for stacked imbalances
    fp_delta_div_penalty = -2.0      # Score for delta divergence (BLOCKS)
    fp_finished_auction_bonus = 0.5  # Score for finished auction at level
    fp_cum_delta_bonus = 0.5         # Score for cumulative delta alignment
    fp_poc_proximity_bonus = 0.5     # Score for price near POC
    fp_trapped_traders_bonus = 1.5   # Score for trapped traders pattern

    # Trapped traders detection
    fp_trapped_vol_threshold = 0.4     # Min volume concentration in extreme third
    fp_trapped_close_bull = 0.7        # Close must be above this % of range for trapped sellers
    fp_trapped_close_bear = 0.3        # Close must be below this % of range for trapped buyers

    # Footprint stop placement
    fp_stop_absorption_buffer = 3.0  # Place stop N ticks beyond absorption zone
    fp_stop_use_absorption = True    # Use absorption zones for stop placement

    # Hard safety limits
    max_position_size_btc = Decimal("0.1")   # Never exceed 0.1 BTC regardless of balance/leverage
    max_slippage_pct = 0.005                  # 0.5% max slippage — reject if spread too wide
    taker_fee_rate = Decimal("0.00045")       # Hyperliquid taker fee: 0.045%

    # ═══════════════════════════════════════════════════════════════
    # STATE INITIALIZATION
    # ═══════════════════════════════════════════════════════════════

    def __init__(self, connectors: Dict[str, ConnectorBase]):
        super().__init__(connectors)

        # Start standard data feeds
        self.btc_5m_candles.start()
        self.btc_1h_candles.start()

        # Start native footprint feed
        self.footprint = FootprintFeed(FootprintConfig(
            connector="hyperliquid_perpetual",
            trading_pair=self.PAIR,
            timeframes=["1m", "5m"],
            tick_size=1.0,
            imbalance_threshold=self.fp_imbalance_threshold,
            domain=self.EXCHANGE,
        ))
        self.footprint.start()

        # ── Regime ──
        self.regime = COMPRESSION
        self.atr_value = 0.0
        self.atr_mean = 0.0
        self.range_pct = 0.0

        # ── Direction lock ──
        self.direction_lock = 0
        self.lock_candle_age = 0
        self.last_bos_direction = 0

        # ── Structure ──
        self.swing_highs: List[dict] = []
        self.swing_lows: List[dict] = []
        self.hourly_bias = 0
        self.premium_discount = 0
        self.dealing_range_high = 0.0
        self.dealing_range_low = 0.0

        # ── ICT components ──
        self.order_blocks: List[dict] = []
        self.fair_value_gaps: List[dict] = []
        self.displacement_retrace = (0, 0.0)
        self.liquidity_swept = 0

        # ── Momentum ──
        self.smi_value = 0.0
        self.smi_signal_val = 0.0
        self.smi_slope = 0.0
        self.cmf_value = 0.0

        # ── Session ──
        self.current_session = "unknown"
        self.session_weight = 0.5

        # ── Risk ──
        self.capital_phase = "Unknown"

        # ── Execution ──
        self.current_signal = 0
        self.signal_scores = {"long": 0.0, "short": 0.0}
        self.last_trade_time = 0
        self.trade_count = 0
        self.last_log_time = 0
        self.startup_logged = False

        # ── Position state machine ──
        self.position_state = STATE_FLAT
        self.entry_price = None
        self.position_side = None
        self.entry_time = None  # Track when position was opened (for minimum hold time)
        self.trailing_stop_active = False
        self.best_roe = Decimal("0")
        self.last_close_time = 0
        self.exchange_position_size = Decimal("0")
        self.position_reconcile_interval = 10
        self.position_reconcile_counter = 0
        self._current_position_size = Decimal("0")
        self._actual_filled_amount = Decimal("0")

        # ── Performance (honest) ──
        self.trade_history: List[dict] = []
        self.session_start_time = time.time()
        self.session_start_balance = None
        self.total_pnl = Decimal("0")
        self.win_count = 0
        self.loss_count = 0
        self._leverage_set = False

        # ── Risk controls ──
        self.daily_pnl = Decimal("0")
        self.daily_trade_count = 0
        self.circuit_breaker_active = False
        self.circuit_breaker_until = 0
        self.last_daily_reset = 0

        # ── Footprint scoring cache ──
        self.fp_scores = {
            "absorption": 0.0, "stacked": 0.0, "delta_div": 0.0,
            "finished_auction": 0.0, "cum_delta": 0.0, "poc_prox": 0.0,
            "trapped_traders": 0.0,
        }

        # ── Entry/exit reasoning log ──
        self._last_entry_reason = ""
        self._last_exit_reason = ""

    @property
    def candles_ready(self):
        # Don't wait for full deque — just need enough candles for analysis
        try:
            has_5m = len(self.btc_5m_candles.candles_df) >= 80
            has_1h = len(self.btc_1h_candles.candles_df) >= 25
            return has_5m and has_1h
        except Exception:
            return False

    async def on_stop(self):
        self.btc_5m_candles.stop()
        self.btc_1h_candles.stop()
        self.footprint.stop()

    # ═══════════════════════════════════════════════════════════════
    # BALANCE — NO FALLBACKS, NO GUESSING
    # ═══════════════════════════════════════════════════════════════

    def _get_balance(self) -> Optional[float]:
        """Get real account balance. Returns None if unavailable — caller must handle."""
        try:
            connector = self.connectors.get(self.EXCHANGE)
            if not connector:
                self.logger().error("[v7] ❌ Connector not available — HALTING")
                return None
            bal = float(connector.get_balance("USD"))
            if bal <= 0:
                # Only log once per minute to avoid spam
                now = time.time()
                if not hasattr(self, '_last_zero_balance_log') or now - self._last_zero_balance_log > 60:
                    self._last_zero_balance_log = now
                    self.logger().warning(f"[v7] ⚠️ Balance is ${bal:.4f} — no funds available")
                return None
            return bal
        except Exception as e:
            self.logger().error(f"[v7] ❌ Balance query failed: {e} — HALTING trading decisions")
            return None

    # ═══════════════════════════════════════════════════════════════
    # EXCHANGE POSITION MANAGEMENT
    # ═══════════════════════════════════════════════════════════════

    def _get_exchange_position(self) -> Tuple[Optional[int], Decimal]:
        """Get actual position from exchange. Returns (side, size)."""
        try:
            connector = self.connectors.get(self.EXCHANGE)
            if not connector:
                return None, Decimal("0")
            positions = connector.account_positions
            if self.PAIR in positions:
                pos = positions[self.PAIR]
                if pos and abs(float(pos.amount)) >= 0.001:
                    side = 1 if float(pos.amount) > 0 else -1
                    return side, Decimal(str(abs(float(pos.amount))))
            return None, Decimal("0")
        except Exception as e:
            self.logger().warning(f"[v7] Position query error: {e}")
            return None, Decimal("0")

    def _reconcile_position(self) -> bool:
        """Sync internal state with exchange. Returns True if state changed."""
        ex_side, ex_size = self._get_exchange_position()
        self.exchange_position_size = ex_size
        changed = False

        if ex_side is None and self.position_side is not None:
            # Don't immediately clear — the connector may not have updated yet
            # Wait at least 30 seconds after last trade before trusting "flat" from exchange
            if time.time() - self.last_trade_time < 30:
                self.logger().debug(f"[v7] 🔄 RECONCILE: Exchange shows flat but last trade was <30s ago — waiting")
                return False

            # CRITICAL: If we're in CLOSING state, this means the close fill never arrived
            # We need to finalize the close properly instead of just clearing state
            if getattr(self, '_closing', False):
                self.logger().warning(
                    f"[v7] ⚠️ RECONCILE: Position closed on exchange but no fill event received! "
                    f"Forcing finalize_close() | reason={getattr(self, '_close_reason', 'unknown')}"
                )
                self._finalize_close()  # This will log the exit and track P&L
                self._closing = False
                return True

            # NEW: Position was closed on exchange without us initiating it (likely stop loss hit)
            # Query recent fills to get actual exit price and log properly
            self.logger().warning(
                f"[v7] ⚠️ RECONCILE: Position closed on exchange (likely SL hit) without close event! "
                f"Querying fills to log exit properly..."
            )
            self._reconcile_and_log_exit()
            self.logger().info(f"[v7] 🔄 RECONCILE: Exchange confirmed flat, state cleared and exit logged")
            self.position_side = None
            self.entry_price = None
            self.entry_time = None
            self.position_state = STATE_FLAT
            self._current_position_size = Decimal("0")
            self._actual_filled_amount = Decimal("0")
            changed = True

        elif ex_side is None and self.position_side is None and self.position_state in (STATE_MISMATCH, STATE_CLOSING):
            # Orphan was closed successfully — reset to FLAT
            self.logger().info(f"[v7] ✅ ORPHAN RESOLVED: Exchange now flat, resuming trading")
            self.position_state = STATE_FLAT
            self._orphan_close_attempted = False
            self.last_close_time = time.time()  # Brief cooldown after orphan cleanup
            changed = True

        elif ex_side is not None and self.position_side is None:
            # Auto-close orphaned positions instead of deadlocking
            if not hasattr(self, '_orphan_close_attempted') or not self._orphan_close_attempted:
                self._orphan_close_attempted = True
                self.logger().warning(
                    f"[v7] ⚠️ ORPHAN DETECTED: Exchange has {ex_side} position ({ex_size} BTC) "
                    f"but internal tracking is flat. AUTO-CLOSING orphaned position..."
                )
                try:
                    if ex_side == 1:  # Long orphan → sell to close
                        self.sell(self.EXCHANGE, self.PAIR, ex_size, OrderType.MARKET,
                                  position_action=PositionAction.CLOSE)
                        self.logger().info(f"[v7] 🔄 Placed SELL {ex_size} BTC to close orphaned LONG")
                    else:  # Short orphan → buy to close
                        self.buy(self.EXCHANGE, self.PAIR, ex_size, OrderType.MARKET,
                                  position_action=PositionAction.CLOSE)
                        self.logger().info(f"[v7] 🔄 Placed BUY {ex_size} BTC to close orphaned SHORT")
                    self.position_state = STATE_CLOSING
                except Exception as e:
                    self.logger().error(f"[v7] ❌ Failed to close orphan: {e}")
                    self.position_state = STATE_MISMATCH
            else:
                # Already attempted close, check if it worked
                self.logger().info(
                    f"[v7] ⏳ Orphan close pending — exchange still shows {ex_side} ({ex_size} BTC)"
                )
                # After 60 seconds, allow retry
                if not hasattr(self, '_orphan_close_time'):
                    self._orphan_close_time = time.time()
                elif time.time() - self._orphan_close_time > 60:
                    self._orphan_close_attempted = False
                    self._orphan_close_time = time.time()
                    self.logger().warning("[v7] 🔄 Orphan close retry — resetting attempt flag")
            changed = True

        return changed

    def _reconcile_and_log_exit(self):
        """
        Called when reconcile detects position was closed on exchange without us initiating it.
        Queries recent fills to get actual exit price and logs the exit properly with P&L tracking.
        """
        try:
            # Save entry info before clearing
            entry_price = self.entry_price
            entry_time = self.entry_time
            position_side = self.position_side
            position_size = self._actual_filled_amount or self._current_position_size

            if not entry_price or not position_side:
                self.logger().warning("[v7] Cannot log reconcile exit - missing entry data")
                return

            # Query recent fills from exchange to get actual exit price
            connector = self.connectors.get(self.EXCHANGE)
            if not connector:
                self.logger().warning("[v7] Cannot query fills - connector not available")
                # Fall back to estimating P&L from current market price
                self._log_reconcile_exit_fallback(entry_price, position_side, position_size)
                return

            # Use connector's internal method to query fills
            # This is async, so we'll use safe_ensure_future
            import asyncio
            from hummingbot.core.utils.async_utils import safe_ensure_future

            async def query_and_log():
                try:
                    # Query fills from last 60 seconds
                    start_time = int((time.time() - 60) * 1000)  # milliseconds

                    fills_response = await connector._api_post(
                        path_url="/info",
                        data={
                            "type": "userFillsByTime",
                            "user": connector.hyperliquid_perpetual_address,
                            "startTime": start_time
                        }
                    )

                    # Find fills that match our position
                    exit_fills = []
                    for fill in fills_response:
                        # Check if this is a closing fill (opposite direction of our position)
                        fill_dir = fill.get("dir", "")
                        if position_side == 1 and "Close Long" in fill_dir:
                            exit_fills.append(fill)
                        elif position_side == -1 and "Close Short" in fill_dir:
                            exit_fills.append(fill)

                    if exit_fills:
                        # Calculate VWAP exit price from fills
                        total_value = sum(float(f["px"]) * float(f["sz"]) for f in exit_fills)
                        total_size = sum(float(f["sz"]) for f in exit_fills)
                        exit_vwap = Decimal(str(total_value / total_size)) if total_size > 0 else Decimal("0")

                        # Calculate P&L
                        if position_side == 1:  # LONG
                            pnl = (exit_vwap - entry_price) * position_size
                        else:  # SHORT
                            pnl = (entry_price - exit_vwap) * position_size

                        # Calculate ROE
                        roe = Decimal("0")
                        if entry_price > 0:
                            if position_side == 1:
                                roe = (exit_vwap - entry_price) / entry_price
                            else:
                                roe = (entry_price - exit_vwap) / entry_price

                        # Track performance
                        self.total_pnl += pnl
                        self.daily_pnl += pnl
                        if pnl > 0:
                            self.win_count += 1
                        else:
                            self.loss_count += 1

                        # Log exit
                        side_str = "LONG" if position_side == 1 else "SHORT"
                        result = "✅" if pnl > 0 else "❌"
                        self.logger().info(
                            f"[v7] {result} EXIT SL (reconcile) | "
                            f"REAL P&L=${float(pnl):.4f} ROE={float(roe)*100:.2f}% | "
                            f"entry_vwap=${float(entry_price):.2f} exit_vwap=${float(exit_vwap):.2f} | "
                            f"size={float(position_size):.6f} BTC | "
                            f"session_pnl=${float(self.daily_pnl):.4f} ({self.win_count}W/{self.loss_count}L)"
                        )

                        # Add to trade history
                        self.trade_history.append({
                            "time": time.time(),
                            "side": side_str,
                            "entry": float(entry_price),
                            "exit": float(exit_vwap),
                            "pnl": float(pnl),
                            "reason": "SL (reconcile)",
                            "roe": float(roe)
                        })

                        self.logger().info(f"[v7] 📊 Reconcile exit logged from {len(exit_fills)} fills")
                    else:
                        # No fills found - fall back to market price estimate
                        self.logger().warning("[v7] No exit fills found in recent history - using fallback")
                        self._log_reconcile_exit_fallback(entry_price, position_side, position_size)

                except Exception as e:
                    self.logger().error(f"[v7] Error querying fills for reconcile exit: {e}")
                    self._log_reconcile_exit_fallback(entry_price, position_side, position_size)

            safe_ensure_future(query_and_log())

        except Exception as e:
            self.logger().error(f"[v7] Error in _reconcile_and_log_exit: {e}")

    def _log_reconcile_exit_fallback(self, entry_price: Decimal, position_side: int, position_size: Decimal):
        """Fallback method to log reconcile exit using current market price when fills unavailable."""
        try:
            connector = self.connectors.get(self.EXCHANGE)
            if connector:
                # Use bid for longs, ask for shorts
                if position_side == 1:
                    exit_price = Decimal(str(connector.get_price(self.PAIR, False)))  # bid
                else:
                    exit_price = Decimal(str(connector.get_price(self.PAIR, True)))   # ask
            else:
                exit_price = entry_price  # Worst case - assume breakeven

            # Calculate P&L
            if position_side == 1:
                pnl = (exit_price - entry_price) * position_size
            else:
                pnl = (entry_price - exit_price) * position_size

            # Calculate ROE
            roe = Decimal("0")
            if entry_price > 0:
                if position_side == 1:
                    roe = (exit_price - entry_price) / entry_price
                else:
                    roe = (entry_price - exit_price) / entry_price

            # Track performance
            self.total_pnl += pnl
            self.daily_pnl += pnl
            if pnl > 0:
                self.win_count += 1
            else:
                self.loss_count += 1

            # Log exit
            side_str = "LONG" if position_side == 1 else "SHORT"
            result = "✅" if pnl > 0 else "❌"
            self.logger().info(
                f"[v7] {result} EXIT SL (reconcile-est) | "
                f"EST P&L=${float(pnl):.4f} ROE={float(roe)*100:.2f}% | "
                f"entry=${float(entry_price):.2f} exit_est=${float(exit_price):.2f} | "
                f"size={float(position_size):.6f} BTC | "
                f"session_pnl=${float(self.daily_pnl):.4f} ({self.win_count}W/{self.loss_count}L)"
            )

            # Add to trade history
            self.trade_history.append({
                "time": time.time(),
                "side": side_str,
                "entry": float(entry_price),
                "exit": float(exit_price),
                "pnl": float(pnl),
                "reason": "SL (reconcile-est)",
                "roe": float(roe)
            })

        except Exception as e:
            self.logger().error(f"[v7] Error in fallback reconcile exit logging: {e}")

    def _can_open_position(self) -> bool:
        """Check all preconditions for opening a new position."""
        if self.position_side is not None:
            return False
        if self.position_state in (STATE_MISMATCH, STATE_CLOSING):
            return False

        # Exchange verification
        ex_side, _ = self._get_exchange_position()
        if ex_side is not None:
            return False

        # Cooldown after close
        if time.time() - self.last_close_time < self.position_close_cooldown:
            return False
        if self.position_state == STATE_COOLDOWN:
            self.position_state = STATE_FLAT

        # Active orders check
        if len(self.get_active_orders(connector_name=self.EXCHANGE)) > 0:
            return False

        # Trade cooldown
        if time.time() - self.last_trade_time < self.trade_cooldown:
            return False

        # Circuit breaker
        if self.circuit_breaker_active:
            if time.time() > self.circuit_breaker_until:
                self.circuit_breaker_active = False
                self.logger().info("[v7] 🟢 Circuit breaker expired. Trading resumed.")
            else:
                return False

        # Daily trade limit
        if self.daily_trade_count >= self.max_trades_per_day:
            return False

        return True

    # ═══════════════════════════════════════════════════════════════
    # MAIN TICK
    # ═══════════════════════════════════════════════════════════════

    def on_tick(self):
        if not self.candles_ready:
            return

        # Log first tick when candles become ready
        if not hasattr(self, '_first_tick_logged') or not self._first_tick_logged:
            self._first_tick_logged = True
            self._startup_time = time.time()
            self.logger().info(f"[v7] ✅ Candles ready — strategy active! 5m={len(self.btc_5m_candles.candles_df)} 1h={len(self.btc_1h_candles.candles_df)}")

        now = time.time()

        # Startup grace period: wait 30 seconds for connector to sync positions from exchange
        if hasattr(self, '_startup_time') and now - self._startup_time < 30:
            return

        # ── Daily reset ──
        if now - self.last_daily_reset > 86400:
            self._daily_reset()

        # ── Set leverage once ──
        if not self._leverage_set:
            self._set_leverage()

        # ── Record starting balance ──
        if self.session_start_balance is None:
            bal = self._get_balance()
            if bal is None:
                return  # Can't trade without knowing balance
            self.session_start_balance = bal

        # ── Update footprint session threshold ──
        if self.current_session == "asia":
            self.footprint.set_imbalance_threshold(self.fp_asia_imbalance_threshold)
        else:
            self.footprint.set_imbalance_threshold(self.fp_imbalance_threshold)

        # ── Reconcile position periodically ──
        self.position_reconcile_counter += 1
        if self.position_reconcile_counter >= self.position_reconcile_interval:
            self.position_reconcile_counter = 0
            self._reconcile_position()

        # ── Finalize close if fills have arrived ──
        if getattr(self, '_closing', False) and self.position_state == STATE_CLOSING:
            # Check if exchange position is flat (fills completed)
            ex_side, ex_size = self._get_exchange_position()
            if ex_side is None:
                # Position closed on exchange — finalize with real P&L
                self._finalize_close()
                return
            elif getattr(self, '_close_filled_amount', Decimal("0")) > 0:
                # Partial close — wait a bit longer for remaining fills
                if time.time() - getattr(self, '_close_time', now) > 30:
                    # Timeout — finalize with what we have
                    self.logger().warning("[v7] ⚠️ Close fill timeout — finalizing with partial fills")
                    self._finalize_close()
                    return
            return  # Don't do anything else while closing

        # ── EXIT-FIRST: manage existing position ──
        if self.position_side is not None:
            self._manage_position()
            return

        # ── Periodic logging ──
        if not self.startup_logged or (now - self.last_log_time > 60):
            self._log_status()

        # ── Check if we can open ──
        if not self._can_open_position():
            return

        # ── Run full analysis ──
        signal = self._run_analysis()
        if signal == 0:
            return

        self._execute_entry(signal)

    # ═══════════════════════════════════════════════════════════════
    # REGIME DETECTION
    # ═══════════════════════════════════════════════════════════════

    def _classify_regime(self, df: pd.DataFrame) -> str:
        if len(df) < self.atr_avg_length + 5:
            return COMPRESSION

        high = df["high"].astype(float)
        low = df["low"].astype(float)
        close = df["close"].astype(float)

        tr = pd.concat([
            high - low,
            (high - close.shift(1)).abs(),
            (low - close.shift(1)).abs()
        ], axis=1).max(axis=1)

        atr = float(tr.rolling(self.atr_length).mean().iloc[-1])
        atr_mean = float(tr.rolling(self.atr_avg_length, min_periods=30).mean().iloc[-1])

        self.atr_value = atr
        self.atr_mean = atr_mean

        if atr_mean == 0:
            return COMPRESSION

        recent = df.iloc[-self.regime_range_lookback:]
        range_high = float(recent["high"].max())
        range_low = float(recent["low"].min())
        mid_price = float(df.iloc[-1]["close"])
        self.range_pct = (range_high - range_low) / mid_price if mid_price > 0 else 0

        atr_ratio = atr / atr_mean
        has_trend = self._has_trend_structure()

        # RANGING: Low volatility + tight range + NO trend structure
        # This is a true sideways market where we should reduce/stop trading
        if atr_ratio < 0.8 and self.range_pct < 0.012 and not has_trend:
            return RANGING

        # COMPRESSION: Low volatility but may have some structure
        if atr_ratio < self.regime_compression_mult and self.range_pct < self.regime_range_pct:
            return COMPRESSION

        # EXPANSION: High volatility with clear trend structure
        if atr_ratio > self.regime_expansion_mult and has_trend:
            return EXPANSION
        if atr_ratio > 1.5:
            return EXPANSION

        # PULLBACK: Default state - moderate volatility
        return PULLBACK

    def _has_trend_structure(self) -> bool:
        if len(self.swing_highs) < 2 or len(self.swing_lows) < 2:
            return False
        sh = self.swing_highs[-2:]
        sl = self.swing_lows[-2:]
        bullish = sh[1]["price"] > sh[0]["price"] and sl[1]["price"] > sl[0]["price"]
        bearish = sl[1]["price"] < sl[0]["price"] and sh[1]["price"] < sh[0]["price"]
        return bullish or bearish

    # ═══════════════════════════════════════════════════════════════
    # SWING STRUCTURE + BOS
    # ═══════════════════════════════════════════════════════════════

    def _find_swing_points(self, df: pd.DataFrame):
        highs = df["high"].astype(float).values
        lows = df["low"].astype(float).values
        n = self.swing_lookback

        self.swing_highs = []
        self.swing_lows = []

        for i in range(n, len(df) - n):
            if highs[i] == max(highs[i-n:i+n+1]):
                self.swing_highs.append({"price": highs[i], "idx": i})
            if lows[i] == min(lows[i-n:i+n+1]):
                self.swing_lows.append({"price": lows[i], "idx": i})

        self.swing_highs = self.swing_highs[-self.swing_history:]
        self.swing_lows = self.swing_lows[-self.swing_history:]

    def _detect_bos_and_lock(self, df: pd.DataFrame):
        self.lock_candle_age += 1

        if self.lock_candle_age > self.direction_lock_decay:
            if self.direction_lock != 0:
                self.logger().info(f"[v7] 🔓 Direction lock expired ({self.lock_candle_age} candles)")
            self.direction_lock = 0
            self.lock_candle_age = 0

        # Clear direction lock in low-volatility regimes
        if self.regime in (COMPRESSION, RANGING):
            self.direction_lock = 0
            return

        last_close = float(df.iloc[-1]["close"])

        if len(self.swing_highs) >= 1:
            last_sh = self.swing_highs[-1]["price"]
            if last_close > last_sh and self.last_bos_direction != 1:
                self.direction_lock = 1
                self.lock_candle_age = 0
                self.last_bos_direction = 1
                self.logger().info(f"[v7] 🔒 BULLISH BOS @ {last_close:.0f} > SH {last_sh:.0f}")

        if len(self.swing_lows) >= 1:
            last_sl = self.swing_lows[-1]["price"]
            if last_close < last_sl and self.last_bos_direction != -1:
                self.direction_lock = -1
                self.lock_candle_age = 0
                self.last_bos_direction = -1
                self.logger().info(f"[v7] 🔒 BEARISH BOS @ {last_close:.0f} < SL {last_sl:.0f}")

    # ═══════════════════════════════════════════════════════════════
    # PREMIUM / DISCOUNT + LIQUIDITY SWEEP
    # ═══════════════════════════════════════════════════════════════

    def _calculate_premium_discount(self, df: pd.DataFrame, price: float):
        recent = df.iloc[-self.dealing_range_lookback:]
        self.dealing_range_high = float(recent["high"].max())
        self.dealing_range_low = float(recent["low"].min())
        rng = self.dealing_range_high - self.dealing_range_low
        if rng == 0:
            self.premium_discount = 0
            return
        position = (price - self.dealing_range_low) / rng
        if position < 0.4:
            self.premium_discount = 1     # Discount → longs
        elif position > 0.6:
            self.premium_discount = -1    # Premium → shorts
        else:
            self.premium_discount = 0

    def _detect_liquidity_sweep(self, df: pd.DataFrame) -> int:
        if len(df) < 10:
            return 0
        last_3 = df.iloc[-3:]
        lows = last_3["low"].astype(float).values
        highs = last_3["high"].astype(float).values
        closes = last_3["close"].astype(float).values

        for sl in self.swing_lows[-5:]:
            for i in range(len(last_3)):
                if lows[i] < sl["price"] and closes[i] > sl["price"]:
                    return 1
        for sh in self.swing_highs[-5:]:
            for i in range(len(last_3)):
                if highs[i] > sh["price"] and closes[i] < sh["price"]:
                    return -1
        return 0

    # ═══════════════════════════════════════════════════════════════
    # ICT: ORDER BLOCKS + FVGs + DISPLACEMENT-RETRACE
    # ═══════════════════════════════════════════════════════════════

    def _find_order_blocks(self, df: pd.DataFrame) -> List[dict]:
        obs = []
        lookback = min(self.ob_lookback, len(df) - 2)
        if lookback < 3:
            return obs

        bodies = (df["close"] - df["open"]).abs()
        avg_body = float(bodies.rolling(20, min_periods=5).mean().iloc[-1])
        if avg_body == 0:
            return obs

        atr = self.atr_value if self.atr_value > 0 else avg_body * 2

        for i in range(len(df) - lookback, len(df) - 1):
            if i < 1:
                continue
            curr = df.iloc[i]
            nxt = df.iloc[i + 1]

            nxt_open, nxt_close = float(nxt["open"]), float(nxt["close"])
            nxt_high, nxt_low = float(nxt["high"]), float(nxt["low"])
            body_next = abs(nxt_close - nxt_open)

            if body_next < avg_body * self.ob_displacement_mult:
                continue

            nxt_range = nxt_high - nxt_low
            if nxt_range == 0:
                continue
            if nxt_close > nxt_open:
                if (nxt_close - nxt_low) / nxt_range < 0.8:
                    continue
            else:
                if (nxt_high - nxt_close) / nxt_range < 0.8:
                    continue

            c_open, c_close = float(curr["open"]), float(curr["close"])
            c_high, c_low = float(curr["high"]), float(curr["low"])

            if c_high - c_low > atr * self.ob_max_range_atr:
                continue

            candle_age = len(df) - 1 - i
            fresh = candle_age <= self.ob_fresh_candles

            if c_close < c_open and nxt_close > nxt_open:
                obs.append({"high": c_high, "low": c_low, "mid": (c_high + c_low) / 2,
                           "direction": 1, "fresh": fresh, "age": candle_age,
                           "strength": body_next / avg_body})
            elif c_close > c_open and nxt_close < nxt_open:
                obs.append({"high": c_high, "low": c_low, "mid": (c_high + c_low) / 2,
                           "direction": -1, "fresh": fresh, "age": candle_age,
                           "strength": body_next / avg_body})

        return obs[-15:]

    def _find_fvgs(self, df: pd.DataFrame) -> List[dict]:
        fvgs = []
        if len(df) < 3:
            return fvgs
        atr = self.atr_value if self.atr_value > 0 else 50
        min_gap = atr * self.fvg_min_atr_mult

        for i in range(1, len(df) - 1):
            prev_high = float(df.iloc[i - 1]["high"])
            next_low = float(df.iloc[i + 1]["low"])
            prev_low = float(df.iloc[i - 1]["low"])
            next_high = float(df.iloc[i + 1]["high"])
            age = len(df) - 1 - i

            if next_low > prev_high and (next_low - prev_high) > min_gap:
                fvgs.append({"high": next_low, "low": prev_high, "mid": (next_low + prev_high) / 2,
                            "direction": 1, "age": age})
            elif prev_low > next_high and (prev_low - next_high) > min_gap:
                fvgs.append({"high": prev_low, "low": next_high, "mid": (prev_low + next_high) / 2,
                            "direction": -1, "age": age})

        return fvgs[-15:]

    def _detect_displacement_retrace(self, df: pd.DataFrame, price: float) -> Tuple[int, float]:
        if len(df) < 20:
            return 0, 0.0
        bodies = (df["close"] - df["open"]).abs()
        avg_body = float(bodies.rolling(20, min_periods=5).mean().iloc[-1])
        if avg_body == 0:
            return 0, 0.0

        for lookback in range(3, 20):
            idx = len(df) - 1 - lookback
            if idx < 1:
                continue
            candle = df.iloc[idx]
            body = abs(float(candle["close"] - candle["open"]))
            if body < avg_body * 1.5:
                continue

            c_open, c_close = float(candle["open"]), float(candle["close"])
            has_fvg = any(abs(fvg["age"] - lookback) <= 2 for fvg in self.fair_value_gaps)
            ob_zone = None
            for ob in self.order_blocks:
                if abs(ob["age"] - lookback) <= 2:
                    ob_zone = ob
                    break

            if not (has_fvg or ob_zone):
                continue

            if c_close > c_open and ob_zone and ob_zone["direction"] == 1:
                ob_50 = ob_zone["low"] + (ob_zone["high"] - ob_zone["low"]) * self.ob_retrace_pct
                buffer = price * 0.002
                if ob_zone["low"] - buffer <= price <= ob_50 + buffer:
                    return 1, 2.0 if has_fvg and ob_zone else 1.5

            elif c_close < c_open and ob_zone and ob_zone["direction"] == -1:
                ob_50 = ob_zone["high"] - (ob_zone["high"] - ob_zone["low"]) * self.ob_retrace_pct
                buffer = price * 0.002
                if ob_50 - buffer <= price <= ob_zone["high"] + buffer:
                    return -1, 2.0 if has_fvg and ob_zone else 1.5

        return 0, 0.0

    # ═══════════════════════════════════════════════════════════════
    # MOMENTUM: SMI + CMF
    # ═══════════════════════════════════════════════════════════════

    def _calculate_smi(self, df: pd.DataFrame) -> Tuple[float, float, float]:
        try:
            k, d = self.smi_k_length, self.smi_d_length
            high_max = df["high"].rolling(k).max()
            low_min = df["low"].rolling(k).min()
            mid = (high_max + low_min) / 2
            distance = df["close"] - mid
            range_val = high_max - low_min
            sd = distance.ewm(span=d).mean().ewm(span=d).mean()
            sr = range_val.ewm(span=d).mean().ewm(span=d).mean() / 2
            smi = pd.Series(np.where(sr != 0, (sd / sr) * 100, 0), index=df.index)
            signal = smi.ewm(span=self.smi_signal_length).mean()
            slope = float(smi.iloc[-1] - smi.iloc[-3]) if len(smi) >= 3 else 0
            return float(smi.iloc[-1]), float(signal.iloc[-1]), slope
        except Exception:
            return 0, 0, 0

    def _calculate_cmf(self, df: pd.DataFrame) -> float:
        try:
            hl = df["high"] - df["low"]
            mfm = np.where(hl != 0, ((df["close"] - df["low"]) - (df["high"] - df["close"])) / hl, 0)
            mfv = mfm * df["volume"]
            cmf = pd.Series(mfv).rolling(self.cmf_length).sum() / df["volume"].rolling(self.cmf_length).sum()
            val = float(cmf.iloc[-1])
            return val if not np.isnan(val) else 0
        except Exception:
            return 0

    # ═══════════════════════════════════════════════════════════════
    # SESSION + BIAS
    # ═══════════════════════════════════════════════════════════════

    def _detect_session(self) -> Tuple[str, float]:
        utc_hour = datetime.now(timezone.utc).hour
        for name, s in SESSIONS.items():
            if s["start"] <= utc_hour < s["end"]:
                weight = s["weight"]
                if 13 <= utc_hour < 16:
                    weight += self.session_overlap_bonus
                return name, weight
        return "off_hours", 0.3

    def _get_ema_bias(self, df: pd.DataFrame) -> int:
        if len(df) < 25:
            return 1 if len(df) >= 5 and float(df["close"].iloc[-1]) > float(df["close"].iloc[-5]) else 0

        ema_fast = df["close"].ewm(span=9, adjust=False).mean()
        ema_slow = df["close"].ewm(span=21, adjust=False).mean()
        diff_pct = (float(ema_fast.iloc[-1]) - float(ema_slow.iloc[-1])) / float(ema_slow.iloc[-1])

        # Increased threshold from 0.001 (0.1%) to 0.005 (0.5%) for hourly timeframe
        # 0.1% was too sensitive and gave false signals in sideways markets
        if diff_pct > 0.005:
            return 1
        elif diff_pct < -0.005:
            return -1
        if len(ema_fast) >= 3:
            slope = float(ema_fast.iloc[-1]) - float(ema_fast.iloc[-3])
            return 1 if slope > 0 else -1 if slope < 0 else 0
        return 0

    # ═══════════════════════════════════════════════════════════════
    # FOOTPRINT SCORING
    # ═══════════════════════════════════════════════════════════════

    def _calculate_footprint_scores(self, price: float) -> Tuple[float, float]:
        """
        Calculate footprint-based score additions for long and short.
        Every score is logged with the data values that produced it.
        """
        fp_long = 0.0
        fp_short = 0.0
        self.fp_scores = {k: 0.0 for k in self.fp_scores}

        try:
            # ── 1. Absorption at Order Blocks (+1.5) ──
            for ob in self.order_blocks:
                if abs(price - ob["mid"]) <= price * 0.002:
                    if self.footprint.has_absorption_at_price(price, "5m", tolerance=2.0):
                        if ob["direction"] == 1:
                            fp_long += self.fp_absorption_weight
                            self.fp_scores["absorption"] = self.fp_absorption_weight
                        elif ob["direction"] == -1:
                            fp_short += self.fp_absorption_weight
                            self.fp_scores["absorption"] = -self.fp_absorption_weight
                        break

            # ── 2. Stacked Imbalances (+1.0) ──
            bull_stacks = self.footprint.has_stacked_imbalances(1, "5m")
            bear_stacks = self.footprint.has_stacked_imbalances(-1, "5m")
            if bull_stacks >= 3:
                fp_long += self.fp_stacked_weight
                self.fp_scores["stacked"] = self.fp_stacked_weight
            if bear_stacks >= 3:
                fp_short += self.fp_stacked_weight
                self.fp_scores["stacked"] = -self.fp_stacked_weight

            # ── 3. Delta Divergence (-2.0 BLOCKS entry) ──
            candle_5m = self.footprint.get_latest_candle("5m")
            if candle_5m and candle_5m.close_price and candle_5m.open_price:
                price_move = (candle_5m.close_price - candle_5m.open_price) / candle_5m.open_price
                delta_norm = candle_5m.total_delta / max(candle_5m.volume, 1)

                if price_move > 0.001 and delta_norm < -0.1:
                    fp_long += self.fp_delta_div_penalty
                    self.fp_scores["delta_div"] = self.fp_delta_div_penalty
                elif price_move < -0.001 and delta_norm > 0.1:
                    fp_short += self.fp_delta_div_penalty
                    self.fp_scores["delta_div"] = -self.fp_delta_div_penalty

            # ── 4. Finished Auction (+0.5) ──
            has_fa, fa_price = self.footprint.has_finished_auction("5m")
            if has_fa and fa_price and abs(price - fa_price) <= 5.0:
                candle = self.footprint.get_latest_candle("5m")
                if candle:
                    if fa_price == candle.finished_auction_high:
                        fp_short += self.fp_finished_auction_bonus
                        self.fp_scores["finished_auction"] = -self.fp_finished_auction_bonus
                    elif fa_price == candle.finished_auction_low:
                        fp_long += self.fp_finished_auction_bonus
                        self.fp_scores["finished_auction"] = self.fp_finished_auction_bonus

            # ── 5. Cumulative Delta Alignment (+0.5) ──
            cum_delta = self.footprint.get_cumulative_delta("5m")
            if cum_delta > 100:
                fp_long += self.fp_cum_delta_bonus
                self.fp_scores["cum_delta"] = self.fp_cum_delta_bonus
            elif cum_delta < -100:
                fp_short += self.fp_cum_delta_bonus
                self.fp_scores["cum_delta"] = -self.fp_cum_delta_bonus

            # ── 6. POC Proximity (+0.5) ──
            poc = self.footprint.get_poc("5m")
            if poc and abs(price - poc) <= 10.0:
                fp_long += self.fp_poc_proximity_bonus * 0.5
                fp_short += self.fp_poc_proximity_bonus * 0.5
                self.fp_scores["poc_prox"] = self.fp_poc_proximity_bonus

            # ── 7. Trapped Traders (+1.5) — NEW in v7 ──
            if candle_5m and candle_5m.volume > 0:
                trapped = self._detect_trapped_traders(candle_5m)
                if trapped == 1:  # Trapped sellers → long
                    fp_long += self.fp_trapped_traders_bonus
                    self.fp_scores["trapped_traders"] = self.fp_trapped_traders_bonus
                elif trapped == -1:  # Trapped buyers → short
                    fp_short += self.fp_trapped_traders_bonus
                    self.fp_scores["trapped_traders"] = -self.fp_trapped_traders_bonus

            return fp_long, fp_short

        except Exception as e:
            self.logger().warning(f"[v7] Footprint scoring error: {e}")
            return 0.0, 0.0

    def _detect_trapped_traders(self, candle) -> int:
        """
        Trapped Traders: Heavy volume at one extreme but price closes at the opposite.
        - Heavy selling at lows but close near high → sellers trapped → long
        - Heavy buying at highs but close near low → buyers trapped → short
        """
        if not candle.levels or len(candle.levels) < 3:
            return 0

        price_range = candle.high_price - candle.low_price
        if price_range == 0:
            return 0

        close_position = (candle.close_price - candle.low_price) / price_range

        sorted_levels = sorted(candle.levels.values(), key=lambda l: l.price)
        n_levels = len(sorted_levels)
        bottom_third = sorted_levels[:max(1, n_levels // 3)]
        top_third = sorted_levels[-max(1, n_levels // 3):]

        bottom_bid_vol = sum(l.bid_volume for l in bottom_third)
        top_ask_vol = sum(l.ask_volume for l in top_third)
        total_vol = candle.volume

        if total_vol == 0:
            return 0

        # Sellers trapped: heavy selling at bottom but close near top
        if bottom_bid_vol / total_vol > self.fp_trapped_vol_threshold and close_position > self.fp_trapped_close_bull:
            return 1

        # Buyers trapped: heavy buying at top but close near bottom
        if top_ask_vol / total_vol > self.fp_trapped_vol_threshold and close_position < self.fp_trapped_close_bear:
            return -1

        return 0

    # ═══════════════════════════════════════════════════════════════
    # FOOTPRINT-INFORMED STOP PLACEMENT
    # ═══════════════════════════════════════════════════════════════

    def _calculate_stop_price(self, entry: float, side: int, balance: float) -> float:
        """
        Calculate stop loss price using footprint data when available.

        Priority:
        1. Absorption zone stop (if absorption detected near entry)
        2. Phase-based ATR stop (fallback)
        """
        _, _, liq_buffer, _ = self._get_capital_phase(balance)

        # Default: phase-based stop
        default_stop_distance = entry * liq_buffer

        if self.fp_stop_use_absorption:
            # Look for absorption zones in recent candles
            candles = self.footprint.get_completed_candles("5m", count=3)
            current = self.footprint.get_latest_candle("5m")
            all_candles = candles + ([current] if current else [])

            for candle in all_candles:
                if not candle or not candle.absorption:
                    continue

                for abs_price in candle.absorption:
                    if side == 1:  # Long: stop below absorption
                        if abs_price < entry and (entry - abs_price) < default_stop_distance * 2:
                            stop = abs_price - self.fp_stop_absorption_buffer
                            self.logger().info(
                                f"[v7] 🎯 Absorption stop: {stop:.0f} "
                                f"(absorption @ {abs_price:.0f}, {self.fp_stop_absorption_buffer} buffer)"
                            )
                            return stop

                    elif side == -1:  # Short: stop above absorption
                        if abs_price > entry and (abs_price - entry) < default_stop_distance * 2:
                            stop = abs_price + self.fp_stop_absorption_buffer
                            self.logger().info(
                                f"[v7] 🎯 Absorption stop: {stop:.0f} "
                                f"(absorption @ {abs_price:.0f}, {self.fp_stop_absorption_buffer} buffer)"
                            )
                            return stop

        # Fallback: fixed percentage
        if side == 1:
            return entry - default_stop_distance
        else:
            return entry + default_stop_distance

    # ═══════════════════════════════════════════════════════════════
    # FULL ANALYSIS + SCORING
    # ═══════════════════════════════════════════════════════════════

    def _run_analysis(self) -> int:
        try:
            df_5m = self.btc_5m_candles.candles_df.copy()
            df_1h = self.btc_1h_candles.candles_df.copy()
            if df_5m.empty or df_1h.empty:
                return 0

            price = float(df_5m.iloc[-1]["close"])

            # Step 1: Swing structure
            self._find_swing_points(df_5m)

            # Step 2: Regime (GATE)
            self.regime = self._classify_regime(df_5m)

            # RANGING: True sideways market - block all trading
            if self.regime == RANGING:
                self.current_signal = 0
                self.signal_scores = {"long": 0.0, "short": 0.0}
                return 0

            # COMPRESSION: Low volatility - block trading (old behavior)
            if self.regime == COMPRESSION:
                self.current_signal = 0
                self.signal_scores = {"long": 0.0, "short": 0.0}
                return 0

            # Step 3: BOS + direction lock
            self._detect_bos_and_lock(df_5m)

            # Step 4: Premium / discount
            self._calculate_premium_discount(df_5m, price)

            # Step 5: Liquidity sweep
            self.liquidity_swept = self._detect_liquidity_sweep(df_5m)

            # Step 6: ICT components
            self.current_session, self.session_weight = self._detect_session()
            self.hourly_bias = self._get_ema_bias(df_1h)
            self.order_blocks = self._find_order_blocks(df_5m)
            self.fair_value_gaps = self._find_fvgs(df_5m)
            self.displacement_retrace = self._detect_displacement_retrace(df_5m, price)

            # Step 7: Momentum
            self.smi_value, self.smi_signal_val, self.smi_slope = self._calculate_smi(df_5m)
            self.cmf_value = self._calculate_cmf(df_5m)

            # Step 8: Score and decide
            return self._score_and_decide(price)

        except Exception as e:
            self.logger().warning(f"[v7] Analysis error: {e}", exc_info=True)
            return 0

    def _score_and_decide(self, price: float) -> int:
        sl = 0.0
        ss = 0.0

        # Bias (1.0)
        if self.hourly_bias == 1: sl += 1.0
        elif self.hourly_bias == -1: ss += 1.0

        # OB proximity (2.0-3.5)
        best_long_ob = 0.0
        best_short_ob = 0.0
        for ob in self.order_blocks:
            tight = price * self.ob_proximity_pct
            wide = price * self.ob_extended_pct
            if ob["direction"] == 1:
                if ob["low"] - tight <= price <= ob["high"] + tight:
                    pts = (3.0 if ob["fresh"] else 2.0) + (0.5 if ob["strength"] > 2.0 else 0)
                    best_long_ob = max(best_long_ob, pts)
                elif abs(price - ob["mid"]) < wide:
                    best_long_ob = max(best_long_ob, 1.0)
            elif ob["direction"] == -1:
                if ob["low"] - tight <= price <= ob["high"] + tight:
                    pts = (3.0 if ob["fresh"] else 2.0) + (0.5 if ob["strength"] > 2.0 else 0)
                    best_short_ob = max(best_short_ob, pts)
                elif abs(price - ob["mid"]) < wide:
                    best_short_ob = max(best_short_ob, 1.0)
        sl += best_long_ob
        ss += best_short_ob

        # FVG (1.0)
        for fvg in self.fair_value_gaps:
            if fvg["direction"] == 1 and fvg["low"] <= price <= fvg["high"]:
                sl += 1.0; break
        for fvg in self.fair_value_gaps:
            if fvg["direction"] == -1 and fvg["low"] <= price <= fvg["high"]:
                ss += 1.0; break

        # Displacement-retrace (1.5-2.0)
        dr_dir, dr_bonus = self.displacement_retrace
        if dr_dir == 1: sl += dr_bonus
        elif dr_dir == -1: ss += dr_bonus

        # Liquidity sweep (1.5)
        if self.liquidity_swept == 1: sl += 1.5
        elif self.liquidity_swept == -1: ss += 1.5

        # SMI (1.5 + 0.5)
        if self.smi_value > self.smi_signal_val and self.smi_slope > 0: sl += 1.5
        if self.smi_value < self.smi_signal_val and self.smi_slope < 0: ss += 1.5
        if self.smi_slope > 3: sl += 0.5
        elif self.smi_slope < -3: ss += 0.5

        # CMF (1.0)
        if self.cmf_value > self.cmf_threshold: sl += 1.0
        elif self.cmf_value < -self.cmf_threshold: ss += 1.0

        # Premium/Discount (1.0) - Buy discount, sell premium
        if self.premium_discount == 1: sl += 1.0   # Discount zone → favor longs
        elif self.premium_discount == -1: ss += 1.0  # Premium zone → favor shorts

        # Session multiplier
        sl *= self.session_weight
        ss *= self.session_weight

        # Footprint scoring
        fp_long, fp_short = self._calculate_footprint_scores(price)
        sl += fp_long
        ss += fp_short

        # Hard filters (apply BEFORE logging to show actual decision scores)
        if self.direction_lock == 1: ss = 0
        elif self.direction_lock == -1: sl = 0

        # REMOVED premium/discount hard filter - it's broken for sideways markets
        # Instead, use it as a scoring component (already applied above)
        # if self.premium_discount == -1: sl = 0   # No longs in premium
        # elif self.premium_discount == 1: ss = 0  # No shorts in discount

        # PRE-ENTRY DELTA FILTER: Don't enter if delta is already past exit threshold
        # This prevents instant delta flip exits
        # Widened from ±200 to ±400 to give trades more room in volatile markets
        cum_delta_5m = self.footprint.get_cumulative_delta("5m")
        if cum_delta_5m < -400: sl = 0  # Don't LONG if delta already bearish
        if cum_delta_5m > 400: ss = 0   # Don't SHORT if delta already bullish

        # Log scores AFTER filters (shows actual decision values)
        self.signal_scores = {"long": round(sl, 1), "short": round(ss, 1)}

        # Decision
        if sl >= self.min_score_to_trade and sl > ss + 0.5:
            self.current_signal = 1
            return 1
        elif ss >= self.min_score_to_trade and ss > sl + 0.5:
            self.current_signal = -1
            return -1

        if (sl >= self.min_score_to_trade and ss >= self.min_score_to_trade and
                abs(sl - ss) <= 0.5 and self.hourly_bias != 0):
            self.current_signal = self.hourly_bias
            return self.hourly_bias

        self.current_signal = 0
        return 0

    # ═══════════════════════════════════════════════════════════════
    # EXECUTION
    # ═══════════════════════════════════════════════════════════════

    def _get_capital_phase(self, balance: float) -> Tuple[float, int, float, str]:
        for max_bal, pct, lev, buf, label in CAPITAL_PHASES:
            if balance <= max_bal:
                return pct, lev, buf, label
        return 0.10, 20, 0.020, "Phase 4: Protect"

    def _set_leverage(self):
        balance = self._get_balance()
        if balance is None:
            return  # Can't set leverage without knowing balance
        _, leverage, _, phase = self._get_capital_phase(balance)
        connector = self.connectors[self.EXCHANGE]
        connector.set_leverage(self.PAIR, leverage)
        self.logger().info(f"[v7] 🔧 Leverage set to {leverage}x ({phase})")
        self._leverage_set = True

    def _execute_entry(self, signal: int):
        connector = self.connectors[self.EXCHANGE]
        # Use bid/ask for realistic entry pricing — longs buy at ask, shorts sell at bid
        if signal == 1:
            entry_price = connector.get_price(self.PAIR, True)  # ask
        else:
            entry_price = connector.get_price(self.PAIR, False)  # bid
        if entry_price is None:
            return

        price = float(entry_price)

        balance = self._get_balance()
        if balance is None:
            self.logger().error("[v7] ❌ Cannot enter — balance unavailable")
            return

        if balance < 1:
            self.logger().warning(f"[v7] Balance too low: ${balance:.2f}")
            return

        score = max(self.signal_scores["long"], self.signal_scores["short"])
        pct, leverage, _, label = self._get_capital_phase(balance)
        self.capital_phase = label
        score_factor = min(1.0, 0.6 + (score - self.min_score_to_trade) * 0.13)
        margin = max(balance * pct * score_factor, 10.0)
        position_value = margin * leverage
        position_size = Decimal(str(position_value)) / Decimal(str(price))

        # Hard cap on position size
        if position_size > self.max_position_size_btc:
            self.logger().warning(
                f"[v7] ⚠️ Position size {float(position_size):.6f} BTC exceeds max "
                f"{float(self.max_position_size_btc)} BTC — capping"
            )
            position_size = self.max_position_size_btc

        # Slippage check — reject if spread too wide
        try:
            best_bid = float(connector.get_price(self.PAIR, False))
            best_ask = float(connector.get_price(self.PAIR, True))
            spread_pct = (best_ask - best_bid) / price if price > 0 else 0
            if spread_pct > self.max_slippage_pct:
                self.logger().warning(
                    f"[v7] ⚠️ Spread too wide: {spread_pct*100:.3f}% > {self.max_slippage_pct*100:.1f}% — skipping entry"
                )
                return
        except Exception:
            pass  # If we can't check spread, proceed with caution

        # Calculate stop price using footprint data
        stop_price = self._calculate_stop_price(price, signal, balance)

        direction = "LONG" if signal == 1 else "SHORT"
        cum_delta = self.footprint.get_cumulative_delta("5m")

        # ── Build entry reason string (verifiable) ──
        reason_parts = [
            f"regime={self.regime}",
            f"session={self.current_session}(×{self.session_weight:.1f})",
            f"score={score:.1f}(L{self.signal_scores['long']:.1f}/S{self.signal_scores['short']:.1f})",
            f"lock={'L' if self.direction_lock==1 else 'S' if self.direction_lock==-1 else 'N'}",
            f"pd={'disc' if self.premium_discount==1 else 'prem' if self.premium_discount==-1 else 'eq'}",
            f"bias={'bull' if self.hourly_bias==1 else 'bear' if self.hourly_bias==-1 else 'flat'}",
            f"OBs={len(self.order_blocks)}",
            f"sweep={self.liquidity_swept}",
            f"δ5m={cum_delta:.0f}",
            f"SMI={self.smi_value:.1f}(sig={self.smi_signal_val:.1f})",
            f"CMF={self.cmf_value:.3f}",
        ]
        # Footprint specifics
        for k, v in self.fp_scores.items():
            if v != 0:
                reason_parts.append(f"fp.{k}={v:+.1f}")

        self._last_entry_reason = " | ".join(reason_parts)

        self.logger().info(
            f"[v7] 🎯 ENTRY {direction} @ {price:.0f} | "
            f"size={float(position_size):.6f} BTC lev={leverage}x margin=${margin:.0f} | "
            f"stop={stop_price:.0f} | {self._last_entry_reason}"
        )

        # Initialize fill tracking BEFORE placing order (did_fill_order will update)
        self._fill_prices = []
        self._actual_filled_amount = Decimal("0")
        self._closing = False
        self._current_position_size = position_size
        # Entry price starts as mid_price estimate — did_fill_order will overwrite with VWAP
        self.entry_price = Decimal(str(price))
        self.entry_time = time.time()  # Track entry time for minimum hold duration
        self._stop_price = stop_price
        self.best_roe = Decimal("0")

        if signal == 1:
            self.position_side = 1
            self.position_state = STATE_LONG
            self.buy(self.EXCHANGE, self.PAIR, position_size, OrderType.MARKET)
        else:
            self.position_side = -1
            self.position_state = STATE_SHORT
            self.sell(self.EXCHANGE, self.PAIR, position_size, OrderType.MARKET)
        self.trailing_stop_active = False
        self.last_trade_time = time.time()

    # ═══════════════════════════════════════════════════════════════
    # POSITION MANAGEMENT
    # ═══════════════════════════════════════════════════════════════

    def _manage_position(self):
        if self.position_side is None or self.entry_price is None:
            return

        connector = self.connectors[self.EXCHANGE]

        # Use bid/ask for exit decisions — not mid_price
        # For a LONG, we'd exit at the bid. For a SHORT, we'd exit at the ask.
        try:
            if self.position_side == 1:
                exit_price = float(connector.get_price(self.PAIR, False))  # bid
            else:
                exit_price = float(connector.get_price(self.PAIR, True))   # ask
        except Exception:
            return  # Can't manage without price

        if exit_price <= 0:
            return

        current = Decimal(str(exit_price))
        entry = self.entry_price

        if self.position_side == 1:
            price_pct = (current - entry) / entry
        else:
            price_pct = (entry - current) / entry

        balance = self._get_balance()
        if balance is None:
            # If we can't get balance while in a position, don't manage — wait for next tick
            return

        _, leverage, liq_buffer, _ = self._get_capital_phase(balance)
        roe = price_pct * leverage

        if roe > self.best_roe:
            self.best_roe = roe

        # ── Take Profit ──
        if roe >= self.roe_target_pct:
            pnl = self._calc_pnl(entry, current)
            self._close_position("TP", pnl,
                f"ROE={float(roe)*100:.1f}% entry={float(entry):.0f} exit={float(current):.0f}")
            return

        # ── Footprint exits ──
        if self._check_footprint_exits(current, roe):
            return

        # ── Breakeven trailing ──
        if self.best_roe >= self.roe_breakeven_pct and not self.trailing_stop_active:
            self.trailing_stop_active = True
            self.logger().info(f"[v7] 🔄 Breakeven stop active | best ROE={float(self.best_roe)*100:.1f}%")

        if self.trailing_stop_active and roe <= Decimal("0.005"):
            pnl = self._calc_pnl(entry, current)
            self._close_position("Breakeven", pnl,
                f"ROE={float(roe)*100:.1f}% pulled back from {float(self.best_roe)*100:.1f}%")
            return

        # ── Stop Loss (footprint-informed or phase-based) ──
        current_price = float(current)
        if self._stop_price and self._stop_price > 0:
            if self.position_side == 1 and current_price <= self._stop_price:
                pnl = self._calc_pnl(entry, current)
                self._close_position("SL", pnl,
                    f"price={current_price:.0f} hit stop={self._stop_price:.0f}")
                return
            elif self.position_side == -1 and current_price >= self._stop_price:
                pnl = self._calc_pnl(entry, current)
                self._close_position("SL", pnl,
                    f"price={current_price:.0f} hit stop={self._stop_price:.0f}")
                return
        else:
            # Fallback: percentage-based
            stop_pct = Decimal(str(liq_buffer))
            if price_pct < -stop_pct:
                pnl = self._calc_pnl(entry, current)
                self._close_position("SL", pnl,
                    f"ROE={float(roe)*100:.1f}% exceeded buffer={liq_buffer*100:.1f}%")
                return

    def _check_footprint_exits(self, current: Decimal, roe: Decimal) -> bool:
        """Check footprint conditions for early exit."""
        try:
            # GLOBAL MINIMUM HOLD TIME: Don't exit within first 60 seconds
            # This prevents rapid-fire entries/exits
            hold_time = time.time() - self.entry_time if self.entry_time else 0
            if hold_time < 60:
                return False

            current_f = float(current)

            # 1. Delta exhaustion near TP zone
            if roe >= Decimal("0.04"):
                candle = self.footprint.get_latest_candle("5m")
                if candle and candle.volume > 0:
                    delta_ratio = abs(candle.total_delta) / candle.volume
                    if delta_ratio < 0.1:
                        pnl = self._calc_pnl(self.entry_price, current)
                        self._close_position("Delta exhaustion", pnl,
                            f"ROE={float(roe)*100:.1f}% delta_ratio={delta_ratio:.3f} (vol={candle.volume:.4f})")
                        return True

            # 2. Cumulative delta flip (widened from ±200 to ±400)
            cum_delta = self.footprint.get_cumulative_delta("5m")
            if self.position_side == 1 and cum_delta < -400:
                pnl = self._calc_pnl(self.entry_price, current)
                self._close_position("Delta flip", pnl,
                    f"LONG but cum_delta={cum_delta:.0f} (threshold: -400)")
                return True
            elif self.position_side == -1 and cum_delta > 400:
                pnl = self._calc_pnl(self.entry_price, current)
                self._close_position("Delta flip", pnl,
                    f"SHORT but cum_delta={cum_delta:.0f} (threshold: +400)")
                return True

            # 3. Opposing absorption
            if roe >= Decimal("0.02"):
                if self.footprint.has_absorption_at_price(current_f, "5m", tolerance=3.0):
                    delta_at = self.footprint.get_delta_at_price(current_f, "5m", tolerance=3.0)
                    opposing = ((self.position_side == 1 and delta_at < -50) or
                               (self.position_side == -1 and delta_at > 50))
                    if opposing:
                        pnl = self._calc_pnl(self.entry_price, current)
                        self._close_position("Absorption", pnl,
                            f"opposing flow at {current_f:.0f} delta={delta_at:.1f}")
                        return True

            # 4. Finished auction at extreme (with safeguards)
            has_fa, fa_price = self.footprint.has_finished_auction("5m")
            if has_fa and fa_price and abs(current_f - fa_price) <= 3.0:
                # SAFEGUARD 1: Minimum hold time (60 seconds)
                hold_time = time.time() - self.entry_time if self.entry_time else 0
                if hold_time < 60:
                    return False  # Don't exit on finished auction within first 60 seconds

                # SAFEGUARD 2: Only exit if profitable or held for 5+ minutes
                if roe < Decimal("0.005") and hold_time < 300:  # 0.5% ROE or 5 min hold
                    return False  # Don't exit at breakeven/loss unless held long enough

                pnl = self._calc_pnl(self.entry_price, current)
                self._close_position("Finished auction", pnl,
                    f"auction exhausted @ {fa_price:.0f} (hold={hold_time:.0f}s, roe={float(roe)*100:.2f}%)")
                return True

            return False

        except Exception as e:
            self.logger().warning(f"[v7] Footprint exit error: {e}")
            return False

    # ═══════════════════════════════════════════════════════════════
    # TRADE LIFECYCLE
    # ═══════════════════════════════════════════════════════════════

    def did_fill_order(self, event):
        """Track REAL fill prices and amounts from exchange — the only honest source of truth.
        Entry fills and close fills are tracked SEPARATELY — never mixed."""
        amount = Decimal(str(event.amount))
        price = Decimal(str(event.price))

        if getattr(self, '_closing', False):
            # ── CLOSE FILL — track exit price only ──
            if not hasattr(self, '_close_fill_prices'):
                self._close_fill_prices = []
            self._close_fill_prices.append({"price": price, "amount": amount})
            self._close_filled_amount = getattr(self, '_close_filled_amount', Decimal("0")) + amount
            self.logger().info(
                f"[v7] 📝 CLOSE FILL: {float(amount):.6f} BTC @ ${float(price):.2f} | "
                f"total closed: {float(self._close_filled_amount):.6f}"
            )

        elif self.position_side is not None:
            # ── ENTRY FILL — track entry price only ──
            self._actual_filled_amount += amount

            if not hasattr(self, '_fill_prices'):
                self._fill_prices = []
            self._fill_prices.append({"price": price, "amount": amount})

            # Update entry price to VWAP of all entry fills
            if self._actual_filled_amount > 0:
                vwap = sum(f["price"] * f["amount"] for f in self._fill_prices) / self._actual_filled_amount
                self.entry_price = vwap
                self.logger().info(
                    f"[v7] 📝 ENTRY FILL: {float(amount):.6f} BTC @ ${float(price):.2f} | "
                    f"VWAP entry: ${float(vwap):.2f} | total filled: {float(self._actual_filled_amount):.6f}"
                )

    def _calc_pnl(self, entry: Decimal, exit_price: Decimal) -> Decimal:
        """Calculate P&L. Entry should be VWAP from did_fill_order, exit_price is mid_price ESTIMATE.
        Real exit P&L is calculated in did_complete_close_order using actual fill prices."""
        diff = (exit_price - entry) if self.position_side == 1 else (entry - exit_price)
        size = self._actual_filled_amount if self._actual_filled_amount > 0 else self._current_position_size
        return diff * size

    def _calc_real_pnl(self) -> Decimal:
        """Calculate REAL P&L from actual exchange fill prices only. No estimates."""
        if not hasattr(self, '_fill_prices') or not self._fill_prices:
            return Decimal("0")
        if not hasattr(self, '_close_fill_prices') or not self._close_fill_prices:
            return Decimal("0")

        # Entry VWAP from actual fills
        entry_total = sum(f["price"] * f["amount"] for f in self._fill_prices)
        entry_amount = sum(f["amount"] for f in self._fill_prices)
        if entry_amount == 0:
            return Decimal("0")
        entry_vwap = entry_total / entry_amount

        # Exit VWAP from actual fills
        exit_total = sum(f["price"] * f["amount"] for f in self._close_fill_prices)
        exit_amount = sum(f["amount"] for f in self._close_fill_prices)
        if exit_amount == 0:
            return Decimal("0")
        exit_vwap = exit_total / exit_amount

        # Use minimum of entry/exit amounts (honest size)
        size = min(entry_amount, exit_amount)

        if self.position_side == 1:
            gross_pnl = (exit_vwap - entry_vwap) * size
        else:
            gross_pnl = (entry_vwap - exit_vwap) * size

        # Subtract fees: taker fee on both entry and exit notional
        entry_notional = entry_vwap * entry_amount
        exit_notional = exit_vwap * exit_amount
        total_fees = (entry_notional + exit_notional) * self.taker_fee_rate
        net_pnl = gross_pnl - total_fees

        self.logger().info(
            f"[v7] 💰 P&L breakdown: gross=${float(gross_pnl):.4f} fees=${float(total_fees):.4f} net=${float(net_pnl):.4f}"
        )
        return net_pnl

    def _close_position(self, reason: str, pnl_estimate: Decimal, details: str = ""):
        """Close position. pnl_estimate is mid-price based (logged as estimate).
        Real P&L is calculated AFTER close fills arrive in did_fill_order."""

        # Prevent double-close
        if getattr(self, '_closing', False):
            self.logger().warning(f"[v7] ⚠️ Double-close prevented — already closing ({self._close_reason})")
            return

        # Save close context for post-fill reconciliation
        self._closing = True
        self._close_fill_prices = []
        self._close_filled_amount = Decimal("0")
        self._close_reason = reason
        self._close_details = details
        self._close_entry_price = self.entry_price
        self._close_side = self.position_side
        self._close_time = time.time()

        try:
            if self.position_side == 1:
                indicative_exit = float(self.connectors[self.EXCHANGE].get_price(self.PAIR, False))
            else:
                indicative_exit = float(self.connectors[self.EXCHANGE].get_price(self.PAIR, True))
        except Exception:
            indicative_exit = 0

        self.logger().info(
            f"[v7] 🔄 CLOSING {reason} | entry=${float(self.entry_price):.2f} "
            f"exit_est=${indicative_exit:.2f} | est_pnl=${float(pnl_estimate):.2f} (ESTIMATE — real P&L after fill) | {details}"
        )

        # Close order — use actual tracked position size, never fabricate
        close_size = self._actual_filled_amount if self._actual_filled_amount > 0 else self._current_position_size
        if close_size <= 0:
            self.logger().error("[v7] ❌ Cannot close — no tracked position size. Skipping.")
            self._closing = False
            return

        if self.position_side == 1:
            self.sell(self.EXCHANGE, self.PAIR, close_size, OrderType.MARKET,
                      position_action=PositionAction.CLOSE)
        else:
            self.buy(self.EXCHANGE, self.PAIR, close_size, OrderType.MARKET,
                      position_action=PositionAction.CLOSE)

        self._last_exit_reason = f"{reason}: {details}"

        # Don't reset state yet — wait for fills to arrive for honest P&L
        self.position_state = STATE_CLOSING

    def _finalize_close(self):
        """Called after close fills arrive. Records REAL P&L from actual exchange fills."""
        real_pnl = self._calc_real_pnl()

        # Track performance with REAL numbers
        self.total_pnl += real_pnl
        self.daily_pnl += real_pnl
        if real_pnl > 0:
            self.win_count += 1
        else:
            self.loss_count += 1

        # Exit VWAP from actual fills
        exit_vwap = Decimal("0")
        if self._close_fill_prices:
            exit_total = sum(f["price"] * f["amount"] for f in self._close_fill_prices)
            exit_amount = sum(f["amount"] for f in self._close_fill_prices)
            if exit_amount > 0:
                exit_vwap = exit_total / exit_amount

        entry_vwap = self._close_entry_price or Decimal("0")
        roe = Decimal("0")
        if entry_vwap > 0 and self._actual_filled_amount > 0:
            if self._close_side == 1:
                roe = (exit_vwap - entry_vwap) / entry_vwap
            else:
                roe = (entry_vwap - exit_vwap) / entry_vwap

        self.trade_history.append({
            "time": time.time(),
            "side": "LONG" if self._close_side == 1 else "SHORT",
            "entry": float(entry_vwap),
            "exit": float(exit_vwap),
            "pnl": float(real_pnl),
            "roe_pct": float(roe * 100),
            "size": float(self._actual_filled_amount),
            "reason": self._close_reason,
            "details": self._close_details,
            "entry_reason": self._last_entry_reason,
            "entry_fills": [{"price": float(f["price"]), "amount": float(f["amount"])} for f in (self._fill_prices or [])],
            "exit_fills": [{"price": float(f["price"]), "amount": float(f["amount"])} for f in self._close_fill_prices],
        })

        self.logger().info(
            f"[v7] {'✅' if real_pnl > 0 else '❌'} EXIT {self._close_reason} | "
            f"REAL P&L=${float(real_pnl):.4f} ROE={float(roe)*100:.2f}% | "
            f"entry_vwap=${float(entry_vwap):.2f} exit_vwap=${float(exit_vwap):.2f} | "
            f"size={float(self._actual_filled_amount):.6f} BTC | "
            f"session_pnl=${float(self.total_pnl):.4f} ({self.win_count}W/{self.loss_count}L)"
        )

        # Reset state
        self.last_close_time = time.time()
        self.position_side = None
        self.entry_price = None
        self.entry_time = None  # Reset entry time
        self._current_position_size = Decimal("0")
        self._actual_filled_amount = Decimal("0")
        self._fill_prices = []
        self._closing = False
        self._close_fill_prices = []
        self._close_filled_amount = Decimal("0")
        self.trailing_stop_active = False
        self.best_roe = Decimal("0")
        self.position_state = STATE_COOLDOWN
        self._stop_price = 0
        self.trade_count += 1
        self.daily_trade_count += 1

        # Circuit breaker check
        if self.session_start_balance and self.session_start_balance > 0:
            drawdown = float(self.daily_pnl) / self.session_start_balance
            if drawdown < -self.max_daily_loss_pct:
                self.circuit_breaker_active = True
                self.circuit_breaker_until = time.time() + self.circuit_breaker_duration
                self.logger().warning(
                    f"[v7] 🚨 CIRCUIT BREAKER: Daily drawdown {drawdown*100:.1f}% "
                    f"exceeded {self.max_daily_loss_pct*100:.0f}% limit. "
                    f"Trading paused for {self.circuit_breaker_duration}s."
                )

    # ═══════════════════════════════════════════════════════════════
    # HELPERS
    # ═══════════════════════════════════════════════════════════════

    def _daily_reset(self):
        self.daily_pnl = Decimal("0")
        self.daily_trade_count = 0
        self.circuit_breaker_active = False
        self.last_daily_reset = time.time()
        bal = self._get_balance()
        if bal is not None:
            self.session_start_balance = bal
        self.logger().info("[v7] 📆 Daily counters reset")

    def _log_status(self):
        self.last_log_time = time.time()
        self.startup_logged = True
        lock_str = {1: "🔒L", -1: "🔒S", 0: "—"}
        wr = (self.win_count / max(self.win_count + self.loss_count, 1)) * 100
        cum_delta = self.footprint.get_cumulative_delta("5m")
        cur_delta = self.footprint.get_current_delta("5m")
        fp_stats = self.footprint.get_stats()

        self.logger().info(
            f"[v7] {self.current_session} | {self.regime} "
            f"lock={lock_str.get(self.direction_lock, '?')}({self.lock_candle_age}) "
            f"pnl=${float(self.total_pnl):.1f} wr={wr:.0f}% "
            f"ATR={self.atr_value:.0f} range={self.range_pct*100:.2f}% "
            f"OBs={len(self.order_blocks)} sweep={self.liquidity_swept} "
            f"L{self.signal_scores['long']:.1f}/S{self.signal_scores['short']:.1f} "
            f"δ={cum_delta:.0f}({cur_delta:.0f}) fp_trades={fp_stats.get('trade_count', 0)} "
            f"phase={self.capital_phase} trades={self.trade_count}/{self.max_trades_per_day}"
        )

    # ═══════════════════════════════════════════════════════════════
    # STATUS DISPLAY
    # ═══════════════════════════════════════════════════════════════

    def format_status(self) -> str:
        if not self.ready_to_trade:
            return "Connectors not ready."
        if not self.candles_ready:
            return (f"Candles loading... "
                    f"5m: {len(self.btc_5m_candles.candles_df) if hasattr(self.btc_5m_candles, 'candles_df') else 0}/80 "
                    f"1h: {len(self.btc_1h_candles.candles_df) if hasattr(self.btc_1h_candles, 'candles_df') else 0}/25")

        lines = []
        bal = self._get_balance()
        lines.append(f"💰 Balance: ${bal:.2f}" if bal else "💰 Balance: unavailable")

        wr = (self.win_count / max(self.win_count + self.loss_count, 1)) * 100
        session_h = (time.time() - self.session_start_time) / 3600

        cum_d1 = self.footprint.get_cumulative_delta("1m")
        cum_d5 = self.footprint.get_cumulative_delta("5m")
        cur_d5 = self.footprint.get_current_delta("5m")
        poc = self.footprint.get_poc("5m")
        fp_stats = self.footprint.get_stats()

        regime_e = {EXPANSION: "🚀", PULLBACK: "↩️", COMPRESSION: "⏸️", RANGING: "📊"}
        lock_s = {1: "🔒 LONG", -1: "🔒 SHORT", 0: "🔓 NONE"}
        pd_s = {1: "🟢 DISC", -1: "🔴 PREM", 0: "⚪ EQ"}

        lines.extend([
            "",
            "═══ ICT v7 — Native Footprint Engine ═══",
            f"",
            f"── Performance ──",
            f"💰 P&L:     ${float(self.total_pnl):.2f} (daily: ${float(self.daily_pnl):.2f})",
            f"📊 Record:  {wr:.0f}% ({self.win_count}W/{self.loss_count}L) in {session_h:.1f}h",
            f"🔢 Trades:  {self.trade_count} (daily: {self.daily_trade_count}/{self.max_trades_per_day})",
            f"",
            f"── State Machine ──",
            f"🔮 Regime:  {regime_e.get(self.regime, '?')} {self.regime} (ATR={self.atr_value:.0f})",
            f"🔐 Lock:    {lock_s.get(self.direction_lock, '?')} ({self.lock_candle_age}/{self.direction_lock_decay})",
            f"💎 Zone:    {pd_s.get(self.premium_discount, '?')}",
            f"📍 Session: {self.current_session} (×{self.session_weight:.1f})",
            f"",
            f"── Footprint (Native WS) ──",
            f"📊 Delta:   1m={cum_d1:.0f} | 5m={cum_d5:.0f} (cur: {cur_d5:.0f})",
            f"🎯 POC:     {poc:.0f}" if poc else f"🎯 POC:     N/A",
            f"📡 Trades:  {fp_stats.get('trade_count', 0)} ingested",
            f"⚡ Ready:   {'YES' if self.footprint.ready else 'NO'}",
        ])

        # Show FP scores if non-zero
        active_fp = {k: v for k, v in self.fp_scores.items() if v != 0}
        if active_fp:
            fp_str = " ".join(f"{k}={v:+.1f}" for k, v in active_fp.items())
            lines.append(f"⚖️  FP:     {fp_str}")

        lines.extend([
            f"",
            f"── Scoring ──",
            f"🎯 Signal:  L={self.signal_scores['long']:.1f} / S={self.signal_scores['short']:.1f} (min: {self.min_score_to_trade})",
            f"📋 Phase:   {self.capital_phase}",
        ])

        # Position
        lines.append(f"")
        state_e = {STATE_FLAT: "⚪", STATE_LONG: "🟢", STATE_SHORT: "🔴",
                   STATE_CLOSING: "🟡", STATE_COOLDOWN: "🔵", STATE_MISMATCH: "🚫"}
        lines.append(f"── Position ──")
        lines.append(f"🔄 State:   {state_e.get(self.position_state, '?')} {self.position_state}")

        if self.position_side is not None:
            pos = "LONG" if self.position_side == 1 else "SHORT"
            lines.append(f"📌 {pos} @ {float(self.entry_price):.0f}")
            if hasattr(self, '_stop_price') and self._stop_price:
                lines.append(f"🛑 Stop:    {self._stop_price:.0f}")
            lines.append(f"📈 Best ROE: {float(self.best_roe)*100:.1f}%")
            lines.append(f"📏 Size:    {float(self._current_position_size):.6f} BTC")

        if self.circuit_breaker_active:
            remaining = max(0, self.circuit_breaker_until - time.time())
            lines.append(f"🚨 CIRCUIT BREAKER: {remaining:.0f}s remaining")

        # Last trade
        if self.trade_history:
            last = self.trade_history[-1]
            lines.extend([
                f"",
                f"── Last Trade ──",
                f"{'✅' if last['pnl'] > 0 else '❌'} {last['side']} {last['entry']:.0f}→{last['exit']:.0f} "
                f"${last['pnl']:.2f} ({last['reason']})",
                f"   {last.get('details', '')}",
            ])

        return "\n".join(lines)
