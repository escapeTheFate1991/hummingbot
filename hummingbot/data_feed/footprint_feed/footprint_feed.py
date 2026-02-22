"""
FootprintFeed — Native Hummingbot data feed for order flow / footprint candles.

Architecture:
  - Subscribes to the exchange's WebSocket trade stream (same as the connector)
  - Aggregates trades into FootprintCandle objects per timeframe
  - Runs pattern detection (absorption, stacked imbalances, finished auctions)
  - Exposes a public API for strategies to query footprint metrics

This is a proper Hummingbot NetworkBase feed, started/stopped like CandlesBase.
It connects to the exchange WS independently (its own connection) so it doesn't
interfere with the connector's order book data source.

Supported exchanges: Hyperliquid (perpetual + spot)
Extensible to any exchange that provides trade-level data with aggressor side.
"""
import asyncio
import logging
import time
from collections import defaultdict, deque
from typing import Any, Dict, List, Optional, Tuple

from hummingbot.core.api_throttler.async_throttler import AsyncThrottler
from hummingbot.core.api_throttler.data_types import RateLimit
from hummingbot.core.network_base import NetworkBase
from hummingbot.core.network_iterator import NetworkStatus
from hummingbot.core.utils.async_utils import safe_ensure_future
from hummingbot.core.web_assistant.connections.data_types import WSJSONRequest
from hummingbot.core.web_assistant.web_assistants_factory import WebAssistantsFactory
from hummingbot.core.web_assistant.ws_assistant import WSAssistant

from hummingbot.data_feed.footprint_feed.footprint_candle import FootprintCandle, PriceLevel
from hummingbot.data_feed.footprint_feed.data_types import FootprintConfig

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════
# EXCHANGE-SPECIFIC CONSTANTS
# ═══════════════════════════════════════════════════════════════════

HYPERLIQUID_WS_URL = "wss://api.hyperliquid.xyz/ws"
HYPERLIQUID_TESTNET_WS_URL = "wss://api.hyperliquid-testnet.xyz/ws"
HYPERLIQUID_REST_URL = "https://api.hyperliquid.xyz/info"
HYPERLIQUID_TESTNET_REST_URL = "https://api.hyperliquid-testnet.xyz/info"

HEARTBEAT_INTERVAL = 30.0

RATE_LIMITS = [
    RateLimit(limit_id="footprint_ws", limit=100, time_interval=60),
]

TF_SECONDS = {
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "1h": 3600,
}


class FootprintFeed(NetworkBase):
    """
    Native Hummingbot data feed that builds footprint candles from the exchange
    WebSocket trade stream.

    Usage in strategy:
        from hummingbot.data_feed.footprint_feed import FootprintFeed, FootprintConfig

        fp_feed = FootprintFeed(FootprintConfig(
            connector="hyperliquid_perpetual",
            trading_pair="BTC-USD",
            timeframes=["1m", "5m"],
            domain="hyperliquid_perpetual_testnet",
        ))

        # In __init__:
        fp_feed.start()

        # In on_tick:
        candle = fp_feed.get_latest_candle("5m")
        delta = fp_feed.get_cumulative_delta("5m")

        # In on_stop:
        fp_feed.stop()
    """

    def __init__(self, config: FootprintConfig):
        super().__init__()
        self._config = config
        self._trading_pair = config.trading_pair
        self._timeframes = config.timeframes
        self._tick_size = config.tick_size
        self._imbalance_threshold = config.imbalance_threshold
        self._max_candles = config.max_candles
        self._domain = config.domain or config.connector

        # Resolve exchange-specific URLs
        self._ws_url = self._resolve_ws_url()
        self._rest_url = self._resolve_rest_url()

        # Resolve coin symbol from trading pair
        self._coin = self._trading_pair.split("-")[0]

        # Candle storage
        self._candles: Dict[str, deque] = {
            tf: deque(maxlen=config.max_candles) for tf in self._timeframes
        }
        self._current_candle: Dict[str, Optional[FootprintCandle]] = {
            tf: None for tf in self._timeframes
        }
        self._cumulative_delta: Dict[str, float] = {
            tf: 0.0 for tf in self._timeframes
        }

        # Trade dedup
        self._seen_trade_ids: deque = deque(maxlen=5000)

        # Network
        async_throttler = AsyncThrottler(rate_limits=RATE_LIMITS)
        self._api_factory = WebAssistantsFactory(throttler=async_throttler)
        self._ws_assistant: Optional[WSAssistant] = None
        self._listen_task: Optional[asyncio.Task] = None
        self._ping_task: Optional[asyncio.Task] = None

        # State
        self._ready = False
        self._trade_count = 0
        self._last_trade_time = 0.0

    # ═══════════════════════════════════════════════════════════════
    # URL RESOLUTION
    # ═══════════════════════════════════════════════════════════════

    def _resolve_ws_url(self) -> str:
        if "testnet" in self._domain:
            return HYPERLIQUID_TESTNET_WS_URL
        return HYPERLIQUID_WS_URL

    def _resolve_rest_url(self) -> str:
        if "testnet" in self._domain:
            return HYPERLIQUID_TESTNET_REST_URL
        return HYPERLIQUID_REST_URL

    # ═══════════════════════════════════════════════════════════════
    # NETWORK LIFECYCLE (Hummingbot standard)
    # ═══════════════════════════════════════════════════════════════

    @property
    def ready(self) -> bool:
        """True once we've received at least one trade."""
        return self._ready

    async def check_network(self) -> NetworkStatus:
        """Health check — can we reach the exchange?"""
        try:
            ws: WSAssistant = await self._api_factory.get_ws_assistant()
            await ws.connect(ws_url=self._ws_url, ping_timeout=HEARTBEAT_INTERVAL)
            await ws.disconnect()
            return NetworkStatus.CONNECTED
        except Exception:
            return NetworkStatus.NOT_CONNECTED

    async def start_network(self):
        """Start WebSocket connection and begin consuming trades."""
        await self.stop_network()
        self._listen_task = safe_ensure_future(self._listen_for_trades())
        logger.info(f"[FootprintFeed] Started for {self._coin} on {self._domain} "
                    f"(timeframes: {self._timeframes})")

    async def stop_network(self):
        """Stop WebSocket connection and clean up."""
        if self._ping_task is not None:
            self._ping_task.cancel()
            self._ping_task = None
        if self._listen_task is not None:
            self._listen_task.cancel()
            self._listen_task = None
        if self._ws_assistant is not None:
            await self._ws_assistant.disconnect()
            self._ws_assistant = None

    # ═══════════════════════════════════════════════════════════════
    # WEBSOCKET TRADE STREAM
    # ═══════════════════════════════════════════════════════════════

    async def _listen_for_trades(self):
        """
        Main WebSocket loop — connects, subscribes to trade stream,
        and processes messages indefinitely with auto-reconnect.
        """
        while True:
            try:
                self._ws_assistant = await self._api_factory.get_ws_assistant()
                await self._ws_assistant.connect(
                    ws_url=self._ws_url,
                    ping_timeout=HEARTBEAT_INTERVAL,
                )

                # Subscribe to trades
                subscribe_payload = {
                    "method": "subscribe",
                    "subscription": {
                        "type": "trades",
                        "coin": self._coin,
                    }
                }
                subscribe_request = WSJSONRequest(payload=subscribe_payload)
                await self._ws_assistant.send(subscribe_request)

                # Start ping loop
                self._ping_task = safe_ensure_future(self._ping_loop())

                logger.info(f"[FootprintFeed] Subscribed to {self._coin} trades via WebSocket")

                # Process messages
                async for msg in self._ws_assistant.iter_messages():
                    self._process_ws_message(msg.data)

            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"[FootprintFeed] WebSocket error: {e}. Reconnecting in 5s...")
                if self._ping_task:
                    self._ping_task.cancel()
                    self._ping_task = None
                await asyncio.sleep(5.0)

    async def _ping_loop(self):
        """Keep WebSocket alive with periodic pings."""
        try:
            while True:
                await asyncio.sleep(HEARTBEAT_INTERVAL)
                if self._ws_assistant:
                    ping_request = WSJSONRequest(payload={"method": "ping"})
                    await self._ws_assistant.send(ping_request)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.debug(f"[FootprintFeed] Ping error: {e}")

    def _process_ws_message(self, data: Dict[str, Any]):
        """
        Process a WebSocket message from the trade stream.

        Hyperliquid trade message format:
        {
            "channel": "trades",
            "data": [
                {
                    "coin": "BTC",
                    "side": "A"|"B",     # A=taker sell, B=taker buy
                    "px": "97500.0",
                    "sz": "0.01",
                    "time": 1740000000000,
                    "hash": "0x...",
                    "tid": 1234567890
                }
            ]
        }
        """
        if not isinstance(data, dict):
            return

        channel = data.get("channel", "")
        if "trades" not in channel:
            return

        trades = data.get("data", [])
        if not isinstance(trades, list):
            return

        for trade in trades:
            try:
                # Dedup by hash or tid
                trade_id = trade.get("hash", trade.get("tid", ""))
                if trade_id in self._seen_trade_ids:
                    continue
                self._seen_trade_ids.append(trade_id)

                price = float(trade["px"])
                size = float(trade["sz"])
                side = trade["side"]
                trade_time = trade["time"] / 1000.0  # ms → seconds

                is_buyer = (side == "B")  # B = taker buy (lifting asks)

                self._ingest_trade(price, size, is_buyer, trade_time)

                self._trade_count += 1
                self._last_trade_time = trade_time
                if not self._ready and self._trade_count >= 1:
                    self._ready = True

            except (KeyError, ValueError, TypeError) as e:
                logger.debug(f"[FootprintFeed] Bad trade message: {e}")

    # ═══════════════════════════════════════════════════════════════
    # CANDLE BUILDING
    # ═══════════════════════════════════════════════════════════════

    def _ingest_trade(self, price: float, size: float, is_buyer: bool, trade_time: float):
        """Route a trade into the appropriate candle for each timeframe."""
        for tf in self._timeframes:
            tf_seconds = TF_SECONDS.get(tf, 60)
            candle_start = (trade_time // tf_seconds) * tf_seconds

            current = self._current_candle.get(tf)

            # New candle period?
            if current is None or current.timestamp != candle_start:
                # Finalize previous candle
                if current is not None and current.volume > 0:
                    self._analyze_candle(current)
                    self._cumulative_delta[tf] += current.total_delta
                    self._candles[tf].append(current)

                # Start new candle
                self._current_candle[tf] = FootprintCandle(
                    timestamp=candle_start,
                    timeframe=tf,
                )

            self._current_candle[tf].add_trade(price, size, is_buyer, self._tick_size)

    # ═══════════════════════════════════════════════════════════════
    # PATTERN DETECTION
    # ═══════════════════════════════════════════════════════════════

    def _analyze_candle(self, candle: FootprintCandle):
        """Run all pattern detections on a completed or current candle."""
        if not candle.levels:
            return

        candle.absorption = self._detect_absorption(candle)
        candle.stacked_imbalances = self._detect_stacked_imbalances(candle)
        self._detect_finished_auction(candle)

    def _detect_absorption(self, candle: FootprintCandle) -> List[float]:
        """
        Absorption: High volume at a price level near candle extremes.
        Indicates large resting orders absorbing aggressor flow.
        """
        if len(candle.levels) < 3:
            return []

        volumes = [lvl.total_volume for lvl in candle.levels.values()]
        avg_vol = sum(volumes) / len(volumes)
        if avg_vol == 0:
            return []

        threshold = avg_vol * 2.0
        absorption_levels = []
        price_range = candle.high_price - candle.low_price
        if price_range == 0:
            return []

        for lvl in candle.levels.values():
            if lvl.total_volume < threshold:
                continue

            dist_from_high = candle.high_price - lvl.price
            dist_from_low = lvl.price - candle.low_price

            at_extreme = (dist_from_high < price_range * 0.2 or
                         dist_from_low < price_range * 0.2)

            if at_extreme:
                absorption_levels.append(lvl.price)

        return absorption_levels

    def _detect_stacked_imbalances(self, candle: FootprintCandle) -> List[Tuple[float, int]]:
        """
        Stacked imbalances: 3+ consecutive price levels with unidirectional
        flow dominance (imbalance_ratio > threshold). Indicates institutional
        order flow.
        """
        if len(candle.levels) < 3:
            return []

        sorted_levels = sorted(candle.levels.values(), key=lambda l: l.price)
        stacks = []
        current_run = []
        current_direction = 0

        for lvl in sorted_levels:
            ratio = lvl.imbalance_ratio
            direction = lvl.imbalance_direction

            if ratio >= self._imbalance_threshold and direction != 0:
                if direction == current_direction:
                    current_run.append(lvl.price)
                else:
                    if len(current_run) >= 3:
                        stacks.append((current_run[0], current_direction))
                    current_run = [lvl.price]
                    current_direction = direction
            else:
                if len(current_run) >= 3:
                    stacks.append((current_run[0], current_direction))
                current_run = []
                current_direction = 0

        if len(current_run) >= 3:
            stacks.append((current_run[0], current_direction))

        return stacks

    def _detect_finished_auction(self, candle: FootprintCandle):
        """
        Finished auction: zero/near-zero volume at candle extreme.
        The auction process is complete — no more interest to push further.
        """
        if not candle.levels:
            return

        sorted_levels = sorted(candle.levels.values(), key=lambda l: l.price)
        if len(sorted_levels) < 2:
            return

        avg_vol = candle.volume / len(sorted_levels)
        if avg_vol == 0:
            return

        highest = sorted_levels[-1]
        if highest.total_volume < avg_vol * 0.1:
            candle.finished_auction_high = highest.price

        lowest = sorted_levels[0]
        if lowest.total_volume < avg_vol * 0.1:
            candle.finished_auction_low = lowest.price

    # ═══════════════════════════════════════════════════════════════
    # PUBLIC API — Called by Strategy
    # ═══════════════════════════════════════════════════════════════

    def set_imbalance_threshold(self, threshold: float):
        """Update imbalance detection threshold (e.g., higher for Asia session)."""
        self._imbalance_threshold = threshold

    def get_latest_candle(self, timeframe: str) -> Optional[FootprintCandle]:
        """Get the current (in-progress) candle for a timeframe."""
        candle = self._current_candle.get(timeframe)
        if candle and candle.volume > 0:
            # Re-analyze on access for fresh pattern data
            self._analyze_candle(candle)
        return candle

    def get_completed_candles(self, timeframe: str, count: int = 10) -> List[FootprintCandle]:
        """Get the last N completed candles."""
        candles = self._candles.get(timeframe, deque())
        return list(candles)[-count:]

    def get_cumulative_delta(self, timeframe: str) -> float:
        """
        Running total of all candle deltas (completed + current).
        Positive = net buying, negative = net selling.
        """
        base = self._cumulative_delta.get(timeframe, 0.0)
        current = self._current_candle.get(timeframe)
        if current:
            base += current.total_delta
        return base

    def get_current_delta(self, timeframe: str) -> float:
        """Delta of the current (in-progress) candle only."""
        current = self._current_candle.get(timeframe)
        return current.total_delta if current else 0.0

    def get_poc(self, timeframe: str) -> Optional[float]:
        """Point of Control of the current candle."""
        current = self._current_candle.get(timeframe)
        return current.poc if current else None

    def has_absorption_at_price(self, price: float, timeframe: str, tolerance: float = 2.0) -> bool:
        """Check if absorption was detected near a given price (current + last completed)."""
        for candle in self._get_recent_candles(timeframe, 2):
            for abs_price in candle.absorption:
                if abs(abs_price - price) <= tolerance:
                    return True
        return False

    def has_stacked_imbalances(self, direction: int, timeframe: str) -> int:
        """Count stacked imbalance zones matching direction in recent candles."""
        count = 0
        for candle in self._get_recent_candles(timeframe, 2):
            count += sum(1 for _, d in candle.stacked_imbalances if d == direction)
        return count

    def has_finished_auction(self, timeframe: str) -> Tuple[bool, Optional[float]]:
        """Check if a finished auction was detected in the current candle."""
        current = self._current_candle.get(timeframe)
        if not current:
            return False, None

        # Re-analyze for freshness
        self._analyze_candle(current)

        if current.finished_auction_high is not None:
            return True, current.finished_auction_high
        if current.finished_auction_low is not None:
            return True, current.finished_auction_low
        return False, None

    def get_delta_at_price(self, price: float, timeframe: str, tolerance: float = 3.0) -> float:
        """Get delta at a specific price level (summed over nearby levels)."""
        total_delta = 0.0
        current = self._current_candle.get(timeframe)
        if not current:
            return 0.0

        for lvl_price, lvl in current.levels.items():
            if abs(lvl_price - price) <= tolerance:
                total_delta += lvl.delta
        return total_delta

    def get_volume_profile(self, timeframe: str) -> Dict[float, float]:
        """Volume profile (price → total volume) for the current candle."""
        current = self._current_candle.get(timeframe)
        if not current:
            return {}
        return {price: lvl.total_volume for price, lvl in current.levels.items()}

    def get_session_volume_profile(self, timeframe: str, num_candles: int = 12) -> Dict[float, float]:
        """Aggregated volume profile across recent candles."""
        profile: Dict[float, float] = defaultdict(float)

        for candle in self._get_recent_candles(timeframe, num_candles):
            for price, lvl in candle.levels.items():
                profile[price] += lvl.total_volume

        return dict(profile)

    def get_session_poc(self, timeframe: str, num_candles: int = 12) -> Optional[float]:
        """Session-level Point of Control from aggregated volume profile."""
        profile = self.get_session_volume_profile(timeframe, num_candles)
        if not profile:
            return None
        return max(profile, key=profile.get)

    def get_stats(self) -> Dict:
        """Diagnostic stats for logging."""
        stats = {"trade_count": self._trade_count, "ready": self._ready}
        for tf in self._timeframes:
            current = self._current_candle.get(tf)
            stats[tf] = {
                "completed": len(self._candles.get(tf, deque())),
                "current_volume": current.volume if current else 0,
                "current_delta": current.total_delta if current else 0,
                "current_levels": len(current.levels) if current else 0,
                "cum_delta": self._cumulative_delta.get(tf, 0),
            }
        return stats

    # ═══════════════════════════════════════════════════════════════
    # INTERNAL HELPERS
    # ═══════════════════════════════════════════════════════════════

    def _get_recent_candles(self, timeframe: str, count: int) -> List[FootprintCandle]:
        """Get current candle + last N-1 completed candles."""
        result = []
        current = self._current_candle.get(timeframe)
        if current:
            result.append(current)

        completed = self._candles.get(timeframe, deque())
        result.extend(list(completed)[-(count - 1):])

        return result
