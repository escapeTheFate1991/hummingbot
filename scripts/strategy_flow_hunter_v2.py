"""
Flow Hunter v2 — Pure Order Flow Trading Strategy
═══════════════════════════════════════════════════════════════

Based on the Flow Hunter Trading Playbook.
Focus: Absorption patterns, delta divergence, POC-based exits.

CORE PHILOSOPHY:
  "Who is going to come in after me, and why?"
  
  Every entry requires a key level + order flow confirmation.
  Exit is the most important part of any strategy.

SETUPS:
  A. Absorption-Initiation Pattern (AIP) — Trapped traders
  B. Absorption Reversal — Massive volume at key levels
  C. Delta Divergence — Trend exhaustion signals

BIG PICTURE (1H):
  1. What's the trend? (HH/HL = bull, LH/LL = bear, neither = range)
  2. Where are the key levels? (prior session H/L/POC, VAH/VAL, demand/supply zones)
  3. Where is price relative to Value Area? (inside = range, outside = directional)
  4. Where are the liquidity pools? (equal highs/lows = targets)

EXITS (Tiered):
  50% at nearest POC or heavy volume node
  25% at next swing level or 2x ATR
  25% hold until delta exit signal

POSITION SIZING:
  3+ confirmations = 2% risk
  2 confirmations = 1% risk
  1 confirmation = 0.5% risk or skip

SESSION RULES:
  Max 3 trades per session
  Stop after 2 losses
  Best windows: NY AM (9:30-11:30 ET), London (3:00-5:00 ET)
  Avoid: Lunch (12:00-14:00 ET)

EXCHANGE: Hyperliquid Perpetuals (mainnet)
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

# Native Footprint Feed
from hummingbot.data_feed.footprint_feed import FootprintFeed, FootprintConfig


# ═══════════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════════

# Trading sessions (ET hours converted to UTC)
SESSIONS = {
    "london":    {"start": 8,  "end": 10, "weight": 1.0},  # 3:00-5:00 ET = 8:00-10:00 UTC
    "ny_am":     {"start": 14, "end": 16, "weight": 1.0},  # 9:30-11:30 ET = 14:30-16:30 UTC
    "lunch":     {"start": 17, "end": 19, "weight": 0.0},  # 12:00-14:00 ET = 17:00-19:00 UTC (AVOID)
}

# Setup types
SETUP_A_AIP = "A_AIP"              # Absorption-Initiation Pattern
SETUP_B_ABSORPTION = "B_ABSORPTION"  # Absorption Reversal
SETUP_C_DIVERGENCE = "C_DIVERGENCE"  # Delta Divergence

# Position states
STATE_FLAT = "FLAT"
STATE_LONG = "LONG"
STATE_SHORT = "SHORT"
STATE_CLOSING = "CLOSING"


class FlowHunterV2(ScriptStrategyBase):
    """
    Pure order flow trading strategy based on Flow Hunter playbook.
    Focus on absorption patterns, delta divergence, and POC-based exits.
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
    btc_5m_candles = CandlesFactory.get_candle(CandlesConfig(
        connector="hyperliquid_perpetual", trading_pair="BTC-USD",
        interval="5m", max_records=200
    ))
    btc_1h_candles = CandlesFactory.get_candle(CandlesConfig(
        connector="hyperliquid_perpetual", trading_pair="BTC-USD",
        interval="1h", max_records=100
    ))
    btc_30m_candles = CandlesFactory.get_candle(CandlesConfig(
        connector="hyperliquid_perpetual", trading_pair="BTC-USD",
        interval="30m", max_records=100
    ))

    # ═══════════════════════════════════════════════════════════════
    # CONFIGURABLE PARAMETERS
    # ═══════════════════════════════════════════════════════════════

    # Session rules
    max_trades_per_session = 3
    max_losses_before_stop = 2
    
    # Position sizing (risk per trade based on confirmations)
    risk_3_confirmations = 0.02  # 2%
    risk_2_confirmations = 0.01  # 1%
    risk_1_confirmation = 0.005  # 0.5%
    
    # Leverage
    leverage = 10
    
    # Absorption detection
    absorption_volume_mult = 2.0      # Volume must be 2x average
    absorption_delta_ratio = 0.25     # Delta/volume ratio ≥ 25%
    absorption_price_range = 0.002    # Price movement < 0.2% for absorption
    
    # Delta divergence
    divergence_lookback = 10          # Candles to check for divergence
    divergence_min_candles = 3        # Minimum candles showing divergence
    
    # POC rules
    poc_first_touch_only = True       # Only trade first POC touch
    poc_heavy_volume_mult = 1.5       # Heavy volume = 1.5x average
    
    # Spread check
    max_spread_pct = 0.001            # 0.1% max spread

    # Footprint parameters
    fp_imbalance_threshold = 3.0      # 3:1 ratio for imbalance detection

    # ═══════════════════════════════════════════════════════════════
    # STATE INITIALIZATION
    # ═══════════════════════════════════════════════════════════════

    def __init__(self, connectors: Dict[str, ConnectorBase]):
        super().__init__(connectors)

        # Start data feeds
        self.btc_5m_candles.start()
        self.btc_1h_candles.start()
        self.btc_30m_candles.start()

        # Start footprint feed
        self.footprint = FootprintFeed(FootprintConfig(
            connector="hyperliquid_perpetual",
            trading_pair=self.PAIR,
            timeframes=["1m", "5m"],
            tick_size=1.0,
            imbalance_threshold=self.fp_imbalance_threshold,
            domain=self.EXCHANGE,
        ))
        self.footprint.start()

        # ── Big Picture (1H) ──
        self.trend = 0  # 1=bull, -1=bear, 0=range
        self.key_levels: List[dict] = []  # {price, type, timestamp}
        self.prior_session_high = 0.0
        self.prior_session_low = 0.0
        self.prior_session_poc = 0.0
        self.vah = 0.0  # Value Area High
        self.val = 0.0  # Value Area Low
        self.liquidity_pools: List[dict] = []  # {price, type, strength}

        # ── Position State ──
        self.position_state = STATE_FLAT
        self.position_side = None  # 1=LONG, -1=SHORT
        self.entry_price = Decimal("0")
        self.entry_time = 0
        self.position_size = Decimal("0")
        self.stop_price = Decimal("0")
        self.setup_type = None  # SETUP_A_AIP, SETUP_B_ABSORPTION, SETUP_C_DIVERGENCE

        # ── Tiered Exit Tracking ──
        self.tier1_filled = False  # 50% at POC
        self.tier2_filled = False  # 25% at swing level
        self.tier3_active = True   # 25% with delta exit

        # ── Session Tracking ──
        self.session_trades = 0
        self.session_losses = 0
        self.session_stopped = False
        self.current_session = "unknown"

        # ── Performance ──
        self.trade_history: List[dict] = []
        self.total_pnl = Decimal("0")
        self.win_count = 0
        self.loss_count = 0
        self._leverage_set = False

        # ── POC Touch Tracking ──
        self.poc_touches: Dict[float, int] = {}  # {poc_price: touch_count}

        # ── Confirmation Signals ──
        self.confirmations: List[str] = []  # Track what signals confirmed entry

        # ── Logging ──
        self.startup_logged = False
        self.last_log_time = 0

    @property
    def candles_ready(self):
        return (self.btc_5m_candles.ready and
                self.btc_1h_candles.ready and
                self.btc_30m_candles.ready and
                len(self.btc_5m_candles.candles_df) >= 50 and
                len(self.btc_1h_candles.candles_df) >= 25)

    async def on_stop(self):
        self.btc_5m_candles.stop()
        self.btc_1h_candles.stop()
        self.btc_30m_candles.stop()
        self.footprint.stop()

    # ═══════════════════════════════════════════════════════════════
    # MAIN TICK LOOP
    # ═══════════════════════════════════════════════════════════════

    def on_tick(self):
        if not self.candles_ready:
            return

        # Log first tick
        if not self.startup_logged:
            self.startup_logged = True
            self.logger().info(f"[FH2] ✅ Candles ready — Flow Hunter v2 active!")

        # Set leverage once
        if not self._leverage_set:
            self._set_leverage()

        # Check session
        self._update_session()

        # Exit-first: manage existing position
        if self.position_side is not None:
            self._manage_position()
            return

        # Periodic logging
        now = time.time()
        if now - self.last_log_time > 60:
            self._log_status()

        # Check if we can trade
        if not self._can_trade():
            return

        # Run big picture analysis (1H)
        self._analyze_big_picture()

        # Run setup detection (5m footprint)
        signal = self._detect_setups()
        if signal == 0:
            return

        # Execute entry
        self._execute_entry(signal)

    # ═══════════════════════════════════════════════════════════════
    # LEVERAGE MANAGEMENT
    # ═══════════════════════════════════════════════════════════════

    def _set_leverage(self):
        try:
            connector = self.connectors.get(self.EXCHANGE)
            if connector and hasattr(connector, 'set_leverage'):
                connector.set_leverage(self.PAIR, self.leverage)
                self.logger().info(f"[FH2] 🔧 Leverage set to {self.leverage}x")
                self._leverage_set = True
        except Exception as e:
            self.logger().warning(f"[FH2] Failed to set leverage: {e}")

    # ═══════════════════════════════════════════════════════════════
    # SESSION MANAGEMENT
    # ═══════════════════════════════════════════════════════════════

    def _update_session(self):
        """Update current session and check if we should stop trading."""
        hour = datetime.now(timezone.utc).hour

        for session_name, session_data in SESSIONS.items():
            if session_data["start"] <= hour < session_data["end"]:
                if self.current_session != session_name:
                    # New session started - reset counters
                    self.current_session = session_name
                    self.session_trades = 0
                    self.session_losses = 0
                    self.session_stopped = False
                    self.logger().info(f"[FH2] 📅 New session: {session_name}")
                return

        self.current_session = "unknown"

    def _can_trade(self) -> bool:
        """Check if we can open a new position."""
        # Session stopped after 2 losses
        if self.session_stopped:
            return False

        # Max trades per session
        if self.session_trades >= self.max_trades_per_session:
            return False

        # Avoid lunch hour
        if self.current_session == "lunch":
            return False

        # Only trade during best windows
        if self.current_session not in ["london", "ny_am"]:
            return False

        return True

    # ═══════════════════════════════════════════════════════════════
    # BIG PICTURE ANALYSIS (1H)
    # ═══════════════════════════════════════════════════════════════

    def _analyze_big_picture(self):
        """
        Answer the four questions before trading:
        1. What's the trend?
        2. Where are the key levels?
        3. Where is price relative to Value Area?
        4. Where are the liquidity pools?
        """
        df_1h = self.btc_1h_candles.candles_df.copy()
        df_30m = self.btc_30m_candles.candles_df.copy()

        if df_1h.empty or df_30m.empty:
            return

        # 1. Trend detection (higher highs/higher lows vs lower highs/lower lows)
        self.trend = self._detect_trend(df_1h)

        # 2. Key levels (prior session H/L/POC, VAH/VAL, demand/supply zones)
        self._identify_key_levels(df_1h, df_30m)

        # 3. Value Area (VAH/VAL from 30m volume profile)
        self._calculate_value_area(df_30m)

        # 4. Liquidity pools (equal highs/lows on 1H)
        self._detect_liquidity_pools(df_1h)

    def _detect_trend(self, df: pd.DataFrame) -> int:
        """
        Detect trend on 1H chart.
        Returns: 1=bull, -1=bear, 0=range
        """
        if len(df) < 20:
            return 0

        # Find swing highs and lows
        highs = []
        lows = []

        for i in range(5, len(df) - 5):
            # Swing high: higher than 5 candles on each side
            if df.iloc[i]['high'] == df.iloc[i-5:i+6]['high'].max():
                highs.append((i, df.iloc[i]['high']))

            # Swing low: lower than 5 candles on each side
            if df.iloc[i]['low'] == df.iloc[i-5:i+6]['low'].min():
                lows.append((i, df.iloc[i]['low']))

        if len(highs) < 2 or len(lows) < 2:
            return 0

        # Check for higher highs and higher lows (bullish)
        recent_highs = [h[1] for h in highs[-3:]]
        recent_lows = [l[1] for l in lows[-3:]]

        if len(recent_highs) >= 2 and recent_highs[-1] > recent_highs[-2]:
            if len(recent_lows) >= 2 and recent_lows[-1] > recent_lows[-2]:
                return 1  # Bullish

        # Check for lower highs and lower lows (bearish)
        if len(recent_highs) >= 2 and recent_highs[-1] < recent_highs[-2]:
            if len(recent_lows) >= 2 and recent_lows[-1] < recent_lows[-2]:
                return -1  # Bearish

        return 0  # Range

    def _identify_key_levels(self, df_1h: pd.DataFrame, df_30m: pd.DataFrame):
        """
        Identify key levels:
        - Prior session high/low
        - Prior session POC
        - Demand/supply zones (origins of strong moves)
        - Order blocks
        - Fair value gaps
        """
        self.key_levels = []

        if len(df_1h) < 24:
            return

        # Prior session (last 24 hours)
        prior_session = df_1h.iloc[-24:]
        self.prior_session_high = float(prior_session['high'].max())
        self.prior_session_low = float(prior_session['low'].min())

        # Add to key levels
        self.key_levels.append({
            "price": self.prior_session_high,
            "type": "prior_session_high",
            "timestamp": time.time()
        })
        self.key_levels.append({
            "price": self.prior_session_low,
            "type": "prior_session_low",
            "timestamp": time.time()
        })

        # Prior session POC (price with highest volume)
        if 'volume' in prior_session.columns:
            # Group by price levels and sum volume
            # Simplified: use close price as proxy for POC
            volume_by_price = {}
            for _, row in prior_session.iterrows():
                price_bucket = round(float(row['close']) / 10) * 10  # Bucket by $10
                volume_by_price[price_bucket] = volume_by_price.get(price_bucket, 0) + float(row['volume'])

            if volume_by_price:
                self.prior_session_poc = max(volume_by_price.items(), key=lambda x: x[1])[0]
                self.key_levels.append({
                    "price": self.prior_session_poc,
                    "type": "prior_session_poc",
                    "timestamp": time.time()
                })

    def _calculate_value_area(self, df: pd.DataFrame):
        """
        Calculate Value Area High (VAH) and Value Area Low (VAL).
        Value Area contains 70% of volume.
        """
        if len(df) < 10 or 'volume' not in df.columns:
            return

        # Use last 48 candles (24 hours of 30m data)
        recent = df.iloc[-48:]

        # Create volume profile
        volume_by_price = {}
        for _, row in recent.iterrows():
            price_bucket = round(float(row['close']) / 10) * 10
            volume_by_price[price_bucket] = volume_by_price.get(price_bucket, 0) + float(row['volume'])

        if not volume_by_price:
            return

        # Sort by price
        sorted_prices = sorted(volume_by_price.items())
        total_volume = sum(v for _, v in sorted_prices)
        target_volume = total_volume * 0.70

        # Find POC (highest volume price)
        poc_price, poc_volume = max(sorted_prices, key=lambda x: x[1])

        # Expand from POC until we have 70% of volume
        accumulated_volume = poc_volume
        low_idx = high_idx = next(i for i, (p, _) in enumerate(sorted_prices) if p == poc_price)

        while accumulated_volume < target_volume and (low_idx > 0 or high_idx < len(sorted_prices) - 1):
            # Check which direction has more volume
            low_vol = sorted_prices[low_idx - 1][1] if low_idx > 0 else 0
            high_vol = sorted_prices[high_idx + 1][1] if high_idx < len(sorted_prices) - 1 else 0

            if low_vol > high_vol and low_idx > 0:
                low_idx -= 1
                accumulated_volume += low_vol
            elif high_idx < len(sorted_prices) - 1:
                high_idx += 1
                accumulated_volume += high_vol
            else:
                break

        self.val = sorted_prices[low_idx][0]
        self.vah = sorted_prices[high_idx][0]

        # Add to key levels
        self.key_levels.append({"price": self.vah, "type": "vah", "timestamp": time.time()})
        self.key_levels.append({"price": self.val, "type": "val", "timestamp": time.time()})

    def _detect_liquidity_pools(self, df: pd.DataFrame):
        """
        Detect liquidity pools (equal highs/lows on 1H).
        These are targets - price is drawn to them because stops are clustered there.
        """
        self.liquidity_pools = []

        if len(df) < 20:
            return

        # Find equal highs (within 0.5%)
        for i in range(len(df) - 10, len(df) - 1):
            for j in range(i + 1, len(df)):
                high_i = float(df.iloc[i]['high'])
                high_j = float(df.iloc[j]['high'])

                if abs(high_i - high_j) / high_i < 0.005:  # Within 0.5%
                    self.liquidity_pools.append({
                        "price": (high_i + high_j) / 2,
                        "type": "equal_highs",
                        "strength": 2
                    })

        # Find equal lows (within 0.5%)
        for i in range(len(df) - 10, len(df) - 1):
            for j in range(i + 1, len(df)):
                low_i = float(df.iloc[i]['low'])
                low_j = float(df.iloc[j]['low'])

                if abs(low_i - low_j) / low_i < 0.005:  # Within 0.5%
                    self.liquidity_pools.append({
                        "price": (low_i + low_j) / 2,
                        "type": "equal_lows",
                        "strength": 2
                    })

    # ═══════════════════════════════════════════════════════════════
    # SETUP DETECTION (5M FOOTPRINT)
    # ═══════════════════════════════════════════════════════════════

    def _detect_setups(self) -> int:
        """
        Detect trading setups on 5m footprint chart.
        Returns: 1=LONG, -1=SHORT, 0=no setup
        """
        df_5m = self.btc_5m_candles.candles_df.copy()
        if df_5m.empty or len(df_5m) < 10:
            return 0

        price = float(df_5m.iloc[-1]['close'])

        # Reset confirmations
        self.confirmations = []

        # Check if we're at a key level
        key_level = self._find_nearest_key_level(price)
        if not key_level:
            return 0  # No key level nearby - skip

        self.confirmations.append(f"key_level_{key_level['type']}")

        # Try Setup A: Absorption-Initiation Pattern (AIP)
        signal = self._detect_setup_a_aip(df_5m, price, key_level)
        if signal != 0:
            self.setup_type = SETUP_A_AIP
            return signal

        # Try Setup B: Absorption Reversal
        signal = self._detect_setup_b_absorption(df_5m, price, key_level)
        if signal != 0:
            self.setup_type = SETUP_B_ABSORPTION
            return signal

        # Try Setup C: Delta Divergence
        signal = self._detect_setup_c_divergence(df_5m, price, key_level)
        if signal != 0:
            self.setup_type = SETUP_C_DIVERGENCE
            return signal

        return 0

    def _find_nearest_key_level(self, price: float) -> Optional[dict]:
        """
        Find nearest key level within 0.3% of current price.
        Returns None if no key level nearby.
        """
        if not self.key_levels:
            return None

        nearest = None
        min_distance = float('inf')

        for level in self.key_levels:
            distance = abs(level['price'] - price) / price
            if distance < 0.003 and distance < min_distance:  # Within 0.3%
                min_distance = distance
                nearest = level

        return nearest

    def _detect_setup_a_aip(self, df: pd.DataFrame, price: float, key_level: dict) -> int:
        """
        Setup A: Absorption-Initiation Pattern (AIP)

        Step 1 - Absorption Candle:
          Heavy selling (negative delta, seller imbalances) BUT closes in upper portion.
          Sellers attacked and failed. They got absorbed.

        Step 2 - Initiation Candle:
          Next candle closes above buyer imbalances with positive delta.
          Buyers have taken control.

        Step 3 - CVD Check:
          While price made lower lows, was CVD making higher lows? (divergence)

        Returns: 1=LONG, -1=SHORT, 0=no setup
        """
        if len(df) < 3:
            return 0

        # Get last 2 candles
        absorption_candle = df.iloc[-2]
        initiation_candle = df.iloc[-1]

        # Get footprint data for these candles
        fp_absorption = self.footprint.get_completed_candles("5m", count=2)
        if len(fp_absorption) < 2:
            return 0

        fp_abs = fp_absorption[-2]  # Absorption candle
        fp_init = fp_absorption[-1]  # Initiation candle

        # LONG setup: Absorption at support
        if self._is_support_level(key_level):
            # Step 1: Absorption candle - heavy selling but closes high
            candle_range = absorption_candle['high'] - absorption_candle['low']
            if candle_range == 0:
                return 0

            close_position = (absorption_candle['close'] - absorption_candle['low']) / candle_range

            # Must have negative delta (selling pressure)
            if fp_abs.total_delta >= 0:
                return 0

            # Must close in upper 70% of range
            if close_position < 0.7:
                return 0

            # Step 2: Initiation candle - positive delta, closes above midpoint
            if fp_init.total_delta <= 0:
                return 0

            init_range = initiation_candle['high'] - initiation_candle['low']
            if init_range > 0:
                init_close_pos = (initiation_candle['close'] - initiation_candle['low']) / init_range
                if init_close_pos < 0.5:
                    return 0

            # Step 3: CVD divergence check
            if self._check_cvd_divergence(df, direction=1):
                self.confirmations.append("cvd_divergence")

            self.confirmations.append("aip_absorption")
            self.confirmations.append("aip_initiation")
            return 1

        # SHORT setup: Absorption at resistance
        elif self._is_resistance_level(key_level):
            # Step 1: Absorption candle - heavy buying but closes low
            candle_range = absorption_candle['high'] - absorption_candle['low']
            if candle_range == 0:
                return 0

            close_position = (absorption_candle['close'] - absorption_candle['low']) / candle_range

            # Must have positive delta (buying pressure)
            if fp_abs.total_delta <= 0:
                return 0

            # Must close in lower 30% of range
            if close_position > 0.3:
                return 0

            # Step 2: Initiation candle - negative delta, closes below midpoint
            if fp_init.total_delta >= 0:
                return 0

            init_range = initiation_candle['high'] - initiation_candle['low']
            if init_range > 0:
                init_close_pos = (initiation_candle['close'] - initiation_candle['low']) / init_range
                if init_close_pos > 0.5:
                    return 0

            # Step 3: CVD divergence check
            if self._check_cvd_divergence(df, direction=-1):
                self.confirmations.append("cvd_divergence")

            self.confirmations.append("aip_absorption")
            self.confirmations.append("aip_initiation")
            return -1

        return 0

    def _is_support_level(self, level: dict) -> bool:
        """Check if level is a support level."""
        return level['type'] in ['prior_session_low', 'val', 'demand_zone', 'equal_lows']

    def _is_resistance_level(self, level: dict) -> bool:
        """Check if level is a resistance level."""
        return level['type'] in ['prior_session_high', 'vah', 'supply_zone', 'equal_highs']

    def _check_cvd_divergence(self, df: pd.DataFrame, direction: int) -> bool:
        """
        Check for CVD divergence.
        direction: 1=bullish (price lower lows, CVD higher lows), -1=bearish (price higher highs, CVD lower highs)
        """
        if len(df) < self.divergence_lookback:
            return False

        recent = df.iloc[-self.divergence_lookback:]

        # Get CVD for each candle (simplified - would need actual CVD tracking)
        # For now, use close price as proxy
        # TODO: Implement proper CVD tracking across candles

        return False  # Placeholder - implement proper CVD divergence detection

    def _detect_setup_b_absorption(self, df: pd.DataFrame, price: float, key_level: dict) -> int:
        """
        Setup B: Absorption Reversal

        Filter 1: Must be at a key level
        Filter 2: Must have opposing aggression (delta flip)
        Filter 3: Delta/volume ratio ≥ 25%

        Massive volume at key level with minimal price movement.
        Someone is absorbing all market orders.

        Returns: 1=LONG, -1=SHORT, 0=no setup
        """
        # Get current footprint candle
        current_fp = self.footprint.get_latest_candle("5m")
        if not current_fp or current_fp.volume == 0:
            return 0

        # Get completed candles for comparison
        completed = self.footprint.get_completed_candles("5m", count=10)
        if len(completed) < 5:
            return 0

        # Check for massive volume (2x average)
        avg_volume = sum(c.volume for c in completed[-5:]) / 5
        if current_fp.volume < avg_volume * self.absorption_volume_mult:
            return 0

        # Check delta/volume ratio
        delta_ratio = abs(current_fp.total_delta) / current_fp.volume
        if delta_ratio < self.absorption_delta_ratio:
            return 0

        # Check for minimal price movement
        candle_range = current_fp.high_price - current_fp.low_price
        price_movement_pct = candle_range / current_fp.close_price if current_fp.close_price > 0 else 0
        if price_movement_pct > self.absorption_price_range:
            return 0

        # Check for delta flip (opposing aggression)
        prev_delta = completed[-1].total_delta if completed else 0
        current_delta = current_fp.total_delta

        # LONG setup: Previous negative delta (selling), now positive (buying)
        if prev_delta < 0 and current_delta > 0 and self._is_support_level(key_level):
            self.confirmations.append("absorption_volume")
            self.confirmations.append("delta_flip")
            self.confirmations.append("delta_ratio_significant")
            return 1

        # SHORT setup: Previous positive delta (buying), now negative (selling)
        if prev_delta > 0 and current_delta < 0 and self._is_resistance_level(key_level):
            self.confirmations.append("absorption_volume")
            self.confirmations.append("delta_flip")
            self.confirmations.append("delta_ratio_significant")
            return -1

        return 0

    def _detect_setup_c_divergence(self, df: pd.DataFrame, price: float, key_level: dict) -> int:
        """
        Setup C: Delta Divergence

        Price making new highs but CVD showing opposite behavior (exhaustion).

        Bearish: Price higher highs, CVD lower highs or flat
        Bullish: Price lower lows, CVD higher lows or flat

        Returns: 1=LONG, -1=SHORT, 0=no setup
        """
        if len(df) < self.divergence_lookback:
            return 0

        recent = df.iloc[-self.divergence_lookback:]

        # Get footprint candles for delta analysis
        fp_candles = self.footprint.get_completed_candles("5m", count=self.divergence_lookback)
        if len(fp_candles) < self.divergence_lookback:
            return 0

        # Find swing high/low in price
        price_high_idx = recent['high'].idxmax()
        price_low_idx = recent['low'].idxmin()

        # Get corresponding delta values
        # Simplified: compare recent delta trend vs price trend
        recent_deltas = [c.total_delta for c in fp_candles[-5:]]

        # Bearish divergence: Price making higher highs, delta declining
        if price_high_idx == len(recent) - 1:  # Recent high
            if len(recent_deltas) >= 3:
                # Check if delta is declining
                if recent_deltas[-1] < recent_deltas[-2] < recent_deltas[-3]:
                    if self._is_resistance_level(key_level):
                        self.confirmations.append("delta_divergence_bearish")
                        return -1

        # Bullish divergence: Price making lower lows, delta improving
        if price_low_idx == len(recent) - 1:  # Recent low
            if len(recent_deltas) >= 3:
                # Check if delta is improving (less negative or more positive)
                if recent_deltas[-1] > recent_deltas[-2] > recent_deltas[-3]:
                    if self._is_support_level(key_level):
                        self.confirmations.append("delta_divergence_bullish")
                        return 1

        return 0

    # ═══════════════════════════════════════════════════════════════
    # POSITION SIZING
    # ═══════════════════════════════════════════════════════════════

    def _calculate_position_size(self, price: float, stop_price: float) -> Decimal:
        """
        AGGRESSIVE POSITION SIZING FOR $10-$100 GROWTH PHASE

        Strategy: Use 100% of available margin on every trade
        Goal: Maximize learning and growth from $10 to $100

        With 10x leverage:
        - $10 balance = $100 buying power
        - Position size = $100 / price

        This is high risk but optimal for:
        1. Rapid capital growth in small account phase
        2. Maximum trade journal data collection
        3. Learning from real P&L impact
        """
        balance = self._get_balance()
        if balance is None or balance <= 0:
            self.logger().warning(f"[FH2] No balance available")
            return Decimal("0")

        # Use 100% of balance with leverage
        # Hyperliquid uses cross margin, so balance * leverage = buying power
        buying_power = balance * Decimal(str(self.leverage))

        # Position size in BTC = buying_power / price
        position_size = buying_power / Decimal(str(price))

        # Log the aggressive sizing
        self.logger().info(
            f"[FH2] 💰 AGGRESSIVE SIZING: balance=${float(balance):.2f} | "
            f"leverage={self.leverage}x | buying_power=${float(buying_power):.2f} | "
            f"size={float(position_size):.6f} BTC (${float(position_size * Decimal(str(price))):.2f})"
        )

        return position_size

    def _get_balance(self) -> Optional[Decimal]:
        """Get account balance."""
        try:
            connector = self.connectors.get(self.EXCHANGE)
            if connector:
                balance = connector.get_balance("USD")
                return Decimal(str(balance)) if balance else None
        except Exception as e:
            self.logger().warning(f"[FH2] Failed to get balance: {e}")
        return None

    # ═══════════════════════════════════════════════════════════════
    # EXECUTION
    # ═══════════════════════════════════════════════════════════════

    def _execute_entry(self, signal: int):
        """Execute entry order."""
        try:
            df_5m = self.btc_5m_candles.candles_df.copy()
            price = float(df_5m.iloc[-1]['close'])

            # Calculate stop price
            stop_price = self._calculate_stop_price(price, signal)

            # Calculate position size
            position_size = self._calculate_position_size(price, stop_price)

            if position_size <= 0:
                self.logger().warning(f"[FH2] Position size too small: {position_size}")
                return

            # Place order
            connector = self.connectors.get(self.EXCHANGE)
            if not connector:
                return

            direction = "LONG" if signal == 1 else "SHORT"

            # Market order - LONG uses buy(), SHORT uses sell()
            if signal == 1:  # LONG
                self.buy(
                    connector_name=self.EXCHANGE,
                    trading_pair=self.PAIR,
                    amount=position_size,
                    order_type=OrderType.MARKET,
                    position_action=PositionAction.OPEN
                )
            else:  # SHORT
                self.sell(
                    connector_name=self.EXCHANGE,
                    trading_pair=self.PAIR,
                    amount=position_size,
                    order_type=OrderType.MARKET,
                    position_action=PositionAction.OPEN
                )

            # Update state
            self.position_side = signal
            self.entry_price = Decimal(str(price))
            self.entry_time = time.time()
            self.position_size = position_size
            self.stop_price = Decimal(str(stop_price))
            self.position_state = STATE_LONG if signal == 1 else STATE_SHORT

            # Reset tier tracking
            self.tier1_filled = False
            self.tier2_filled = False
            self.tier3_active = True

            # Update session counters
            self.session_trades += 1

            # Log entry
            confirmations_str = ", ".join(self.confirmations)
            self.logger().info(
                f"[FH2] 🎯 ENTRY {direction} @ {price:.0f} | "
                f"size={float(position_size):.6f} BTC | "
                f"stop={float(stop_price):.0f} | "
                f"setup={self.setup_type} | "
                f"confirmations={len(self.confirmations)} ({confirmations_str})"
            )

        except Exception as e:
            self.logger().error(f"[FH2] Entry execution error: {e}")

    def _calculate_stop_price(self, entry: float, side: int) -> float:
        """
        Calculate stop loss price.
        Place stop beyond key level or absorption zone.
        """
        # Find nearest key level
        key_level = self._find_nearest_key_level(entry)

        if key_level:
            # Place stop beyond key level
            if side == 1:  # LONG
                return key_level['price'] * 0.995  # 0.5% below support
            else:  # SHORT
                return key_level['price'] * 1.005  # 0.5% above resistance

        # Fallback: 1% stop
        if side == 1:
            return entry * 0.99
        else:
            return entry * 1.01

    # ═══════════════════════════════════════════════════════════════
    # POSITION MANAGEMENT (TIERED EXITS)
    # ═══════════════════════════════════════════════════════════════

    def _manage_position(self):
        """
        Manage open position with tiered exits:
        - 50% at nearest POC or heavy volume node
        - 25% at next swing level or 2x ATR
        - 25% hold until delta exit signal
        """
        try:
            df_5m = self.btc_5m_candles.candles_df.copy()
            if df_5m.empty:
                return

            current_price = Decimal(str(df_5m.iloc[-1]['close']))

            # Check stop loss first
            if self._check_stop_loss(current_price):
                return

            # Tier 1: 50% at nearest POC
            if not self.tier1_filled:
                if self._check_tier1_exit(current_price):
                    self._execute_partial_exit(0.5, "Tier1_POC", current_price)
                    self.tier1_filled = True
                    return

            # Tier 2: 25% at swing level
            if self.tier1_filled and not self.tier2_filled:
                if self._check_tier2_exit(current_price):
                    self._execute_partial_exit(0.25, "Tier2_Swing", current_price)
                    self.tier2_filled = True
                    return

            # Tier 3: 25% with delta exit signal
            if self.tier1_filled and self.tier2_filled and self.tier3_active:
                if self._check_delta_exit_signal():
                    self._execute_partial_exit(0.25, "Tier3_Delta", current_price)
                    self.tier3_active = False
                    self._finalize_close()
                    return

        except Exception as e:
            self.logger().error(f"[FH2] Position management error: {e}")

    def _check_stop_loss(self, current_price: Decimal) -> bool:
        """Check if stop loss hit."""
        if self.position_side == 1:  # LONG
            if current_price <= self.stop_price:
                self._close_full_position("SL", current_price)
                return True
        else:  # SHORT
            if current_price >= self.stop_price:
                self._close_full_position("SL", current_price)
                return True
        return False

    def _check_tier1_exit(self, current_price: Decimal) -> bool:
        """Check if we should take Tier 1 exit (50% at POC)."""
        # Get current POC
        poc = self.footprint.get_poc("5m")
        if not poc:
            return False

        # Check if price reached POC
        distance = abs(float(current_price) - poc) / float(current_price)

        if self.position_side == 1:  # LONG
            # POC should be above entry
            if poc > float(self.entry_price) and distance < 0.002:  # Within 0.2%
                return True
        else:  # SHORT
            # POC should be below entry
            if poc < float(self.entry_price) and distance < 0.002:
                return True

        return False

    def _check_tier2_exit(self, current_price: Decimal) -> bool:
        """Check if we should take Tier 2 exit (25% at swing level)."""
        # Simplified: check if we're up 2x the stop distance
        entry_to_stop = abs(float(self.entry_price) - float(self.stop_price))
        entry_to_current = abs(float(current_price) - float(self.entry_price))

        if entry_to_current >= entry_to_stop * 2:
            return True

        return False

    def _check_delta_exit_signal(self) -> bool:
        """
        Check for delta exit signal.

        In a long: Strong negative delta AND closes below prior candle's POC
        In a short: Strong positive delta AND closes above prior candle's POC
        """
        current_fp = self.footprint.get_latest_candle("5m")
        completed = self.footprint.get_completed_candles("5m", count=2)

        if not current_fp or len(completed) < 1:
            return False

        prior_fp = completed[-1]

        if self.position_side == 1:  # LONG
            # Strong negative delta
            if current_fp.total_delta < 0:
                delta_ratio = abs(current_fp.total_delta) / current_fp.volume if current_fp.volume > 0 else 0
                if delta_ratio > 0.2:  # 20% delta ratio
                    # Closes below prior POC
                    if prior_fp.poc and current_fp.close_price < prior_fp.poc:
                        return True

        else:  # SHORT
            # Strong positive delta
            if current_fp.total_delta > 0:
                delta_ratio = current_fp.total_delta / current_fp.volume if current_fp.volume > 0 else 0
                if delta_ratio > 0.2:
                    # Closes above prior POC
                    if prior_fp.poc and current_fp.close_price > prior_fp.poc:
                        return True

        return False

    def _execute_partial_exit(self, portion: float, reason: str, exit_price: Decimal):
        """Execute partial exit."""
        try:
            exit_size = self.position_size * Decimal(str(portion))

            connector = self.connectors.get(self.EXCHANGE)
            if not connector:
                return

            # Close portion - LONG uses sell(), SHORT uses buy()
            if self.position_side == 1:  # LONG
                self.sell(
                    connector_name=self.EXCHANGE,
                    trading_pair=self.PAIR,
                    amount=exit_size,
                    order_type=OrderType.MARKET,
                    position_action=PositionAction.CLOSE
                )
            else:  # SHORT
                self.buy(
                    connector_name=self.EXCHANGE,
                    trading_pair=self.PAIR,
                    amount=exit_size,
                    order_type=OrderType.MARKET,
                    position_action=PositionAction.CLOSE
                )

            # Calculate P&L for this portion
            if self.position_side == 1:
                pnl = (exit_price - self.entry_price) * exit_size
            else:
                pnl = (self.entry_price - exit_price) * exit_size

            self.total_pnl += pnl

            # Update position size
            self.position_size -= exit_size

            self.logger().info(
                f"[FH2] 📤 PARTIAL EXIT {reason} ({portion*100:.0f}%) @ {float(exit_price):.0f} | "
                f"P&L=${float(pnl):.4f} | remaining={float(self.position_size):.6f} BTC"
            )

        except Exception as e:
            self.logger().error(f"[FH2] Partial exit error: {e}")

    def _close_full_position(self, reason: str, exit_price: Decimal):
        """Close full position."""
        try:
            connector = self.connectors.get(self.EXCHANGE)
            if not connector:
                return

            # Close position - LONG uses sell(), SHORT uses buy()
            if self.position_side == 1:  # LONG
                self.sell(
                    connector_name=self.EXCHANGE,
                    trading_pair=self.PAIR,
                    amount=self.position_size,
                    order_type=OrderType.MARKET,
                    position_action=PositionAction.CLOSE
                )
            else:  # SHORT
                self.buy(
                    connector_name=self.EXCHANGE,
                    trading_pair=self.PAIR,
                    amount=self.position_size,
                    order_type=OrderType.MARKET,
                    position_action=PositionAction.CLOSE
                )

            # Calculate P&L
            if self.position_side == 1:
                pnl = (exit_price - self.entry_price) * self.position_size
            else:
                pnl = (self.entry_price - exit_price) * self.position_size

            self.total_pnl += pnl

            # Update counters
            if pnl > 0:
                self.win_count += 1
            else:
                self.loss_count += 1
                self.session_losses += 1

            # Check if we should stop trading this session
            if self.session_losses >= self.max_losses_before_stop:
                self.session_stopped = True
                self.logger().warning(f"[FH2] ⛔ Session stopped after {self.session_losses} losses")

            # Log exit
            direction = "LONG" if self.position_side == 1 else "SHORT"
            emoji = "✅" if pnl > 0 else "❌"

            self.logger().info(
                f"[FH2] {emoji} EXIT {reason} {direction} @ {float(exit_price):.0f} | "
                f"P&L=${float(pnl):.4f} | "
                f"total_pnl=${float(self.total_pnl):.4f} ({self.win_count}W/{self.loss_count}L)"
            )

            # Add to trade history
            self.trade_history.append({
                "entry_time": self.entry_time,
                "exit_time": time.time(),
                "setup_type": self.setup_type,
                "direction": direction,
                "entry_price": float(self.entry_price),
                "exit_price": float(exit_price),
                "size": float(self.position_size),
                "pnl": float(pnl),
                "confirmations": len(self.confirmations),
                "exit_reason": reason
            })

            self._finalize_close()

        except Exception as e:
            self.logger().error(f"[FH2] Full exit error: {e}")

    def _finalize_close(self):
        """Reset position state."""
        self.position_state = STATE_FLAT
        self.position_side = None
        self.entry_price = Decimal("0")
        self.entry_time = 0
        self.position_size = Decimal("0")
        self.stop_price = Decimal("0")
        self.setup_type = None
        self.tier1_filled = False
        self.tier2_filled = False
        self.tier3_active = True
        self.confirmations = []

    # ═══════════════════════════════════════════════════════════════
    # LOGGING
    # ═══════════════════════════════════════════════════════════════

    def _log_status(self):
        """Log current status."""
        self.last_log_time = time.time()

        df_5m = self.btc_5m_candles.candles_df.copy()
        if df_5m.empty:
            return

        price = float(df_5m.iloc[-1]['close'])

        trend_str = "BULL" if self.trend == 1 else "BEAR" if self.trend == -1 else "RANGE"
        wr = int(self.win_count / (self.win_count + self.loss_count) * 100) if (self.win_count + self.loss_count) > 0 else 0

        self.logger().info(
            f"[FH2] {self.current_session} | {trend_str} | "
            f"price=${price:.0f} | "
            f"pnl=${float(self.total_pnl):.2f} wr={wr}% | "
            f"session={self.session_trades}/{self.max_trades_per_session} losses={self.session_losses} | "
            f"key_levels={len(self.key_levels)} liq_pools={len(self.liquidity_pools)}"
        )


# ═══════════════════════════════════════════════════════════════
# STRATEGY REGISTRATION
# ═══════════════════════════════════════════════════════════════

# This allows the strategy to be loaded by Hummingbot
def create_strategy(connectors: Dict[str, ConnectorBase]) -> FlowHunterV2:
    return FlowHunterV2(connectors)


