"""
FootprintAggregator — Backward-compatible wrapper.

The real implementation now lives in the Hummingbot codebase at:
    hummingbot/data_feed/footprint_feed/

This file provides a sync-friendly wrapper that strategies can use
without needing to manage async lifecycle directly. It wraps the native
FootprintFeed for strategies that call update_sync() from on_tick().

For new strategies, use FootprintFeed directly:
    from hummingbot.data_feed.footprint_feed import FootprintFeed, FootprintConfig
"""
import logging
import time
from typing import Dict, List, Optional, Tuple

import requests

logger = logging.getLogger(__name__)


# Re-export core types from codebase
try:
    from hummingbot.data_feed.footprint_feed.footprint_candle import FootprintCandle, PriceLevel
    from hummingbot.data_feed.footprint_feed.data_types import FootprintConfig
except ImportError:
    # Fallback for running outside Hummingbot env (testing)
    from dataclasses import dataclass, field

    @dataclass
    class PriceLevel:
        price: float
        bid_volume: float = 0.0
        ask_volume: float = 0.0
        @property
        def delta(self): return self.ask_volume - self.bid_volume
        @property
        def total_volume(self): return self.bid_volume + self.ask_volume
        @property
        def imbalance_ratio(self):
            if self.bid_volume == 0 and self.ask_volume == 0: return 0.0
            if self.bid_volume == 0 or self.ask_volume == 0: return float('inf')
            return max(self.ask_volume, self.bid_volume) / min(self.ask_volume, self.bid_volume)
        @property
        def imbalance_direction(self):
            if self.ask_volume > self.bid_volume: return 1
            elif self.bid_volume > self.ask_volume: return -1
            return 0

    @dataclass
    class FootprintCandle:
        timestamp: float
        timeframe: str
        open_price: float = 0.0
        high_price: float = 0.0
        low_price: float = 0.0
        close_price: float = 0.0
        levels: Dict[float, PriceLevel] = field(default_factory=dict)
        absorption: list = field(default_factory=list)
        stacked_imbalances: list = field(default_factory=list)
        finished_auction_high: Optional[float] = None
        finished_auction_low: Optional[float] = None

        @property
        def volume(self): return sum(l.total_volume for l in self.levels.values())
        @property
        def total_delta(self): return sum(l.delta for l in self.levels.values())
        @property
        def bid_volume(self): return sum(l.bid_volume for l in self.levels.values())
        @property
        def ask_volume(self): return sum(l.ask_volume for l in self.levels.values())
        @property
        def poc(self):
            if not self.levels: return None
            return max(self.levels.values(), key=lambda l: l.total_volume).price

        def add_trade(self, price, size, is_buyer, tick_size=1.0):
            bucketed = round(price / tick_size) * tick_size
            if bucketed not in self.levels:
                self.levels[bucketed] = PriceLevel(price=bucketed)
            lvl = self.levels[bucketed]
            if is_buyer: lvl.ask_volume += size
            else: lvl.bid_volume += size
            if self.open_price == 0.0: self.open_price = price
            self.close_price = price
            if price > self.high_price or self.high_price == 0.0: self.high_price = price
            if price < self.low_price or self.low_price == 0.0: self.low_price = price


TF_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600}


class FootprintAggregator:
    """
    Sync-friendly footprint aggregator for use in strategy on_tick().

    Polls Hyperliquid REST API for recent trades and builds footprint candles.
    This is a convenience wrapper — the native async FootprintFeed in
    hummingbot/data_feed/footprint_feed/ is preferred for new strategies.
    """

    MAINNET_URL = "https://api.hyperliquid.xyz/info"
    TESTNET_URL = "https://api.hyperliquid-testnet.xyz/info"

    def __init__(
        self,
        coin: str = "BTC",
        timeframes: List[str] = None,
        imbalance_threshold: float = 3.0,
        tick_size: float = 1.0,
        max_candles: int = 50,
        use_testnet: bool = True,
    ):
        self.coin = coin
        self.timeframes = timeframes or ["1m", "5m"]
        self.imbalance_threshold = imbalance_threshold
        self.tick_size = tick_size
        self.max_candles = max_candles
        self.api_url = self.TESTNET_URL if use_testnet else self.MAINNET_URL

        self.candles: Dict[str, List[FootprintCandle]] = {tf: [] for tf in self.timeframes}
        self.current_candle: Dict[str, Optional[FootprintCandle]] = {tf: None for tf in self.timeframes}
        self.cumulative_delta: Dict[str, float] = {tf: 0.0 for tf in self.timeframes}

        self._last_trade_hashes: set = set()
        self._max_hash_cache = 5000
        self._last_poll_time = 0
        self._poll_interval = 2.0
        self._session = requests.Session()
        self._session.headers.update({"Content-Type": "application/json"})

    def set_imbalance_threshold(self, threshold: float):
        self.imbalance_threshold = threshold

    # ── Data Ingestion ──

    def update_sync(self):
        now = time.time()
        if now - self._last_poll_time < self._poll_interval:
            return
        self._last_poll_time = now

        try:
            trades = self._fetch_recent_trades()
            if not trades:
                return

            for trade in trades:
                trade_hash = trade.get("hash", "")
                if trade_hash in self._last_trade_hashes:
                    continue
                self._last_trade_hashes.add(trade_hash)

                price = float(trade["px"])
                size = float(trade["sz"])
                side = trade["side"]
                trade_time = trade["time"] / 1000.0
                is_buyer = (side == "B")

                self._ingest_trade(price, size, is_buyer, trade_time)

            if len(self._last_trade_hashes) > self._max_hash_cache:
                self._last_trade_hashes = set(list(self._last_trade_hashes)[-self._max_hash_cache // 2:])

            for tf in self.timeframes:
                candle = self.current_candle.get(tf)
                if candle and candle.volume > 0:
                    self._analyze_candle(candle)

        except Exception as e:
            logger.warning(f"[FootprintAggregator] Update error: {e}")

    def _fetch_recent_trades(self) -> List[dict]:
        try:
            resp = self._session.post(
                self.api_url,
                json={"type": "recentTrades", "coin": self.coin},
                timeout=5,
            )
            if resp.status_code == 200:
                return resp.json()
            return []
        except requests.exceptions.RequestException as e:
            logger.warning(f"[FootprintAggregator] API error: {e}")
            return []

    def _ingest_trade(self, price: float, size: float, is_buyer: bool, trade_time: float):
        for tf in self.timeframes:
            tf_seconds = TF_SECONDS.get(tf, 60)
            candle_start = (trade_time // tf_seconds) * tf_seconds

            current = self.current_candle.get(tf)

            if current is None or current.timestamp != candle_start:
                if current is not None and current.volume > 0:
                    self._analyze_candle(current)
                    self.cumulative_delta[tf] += current.total_delta
                    self.candles[tf].append(current)
                    if len(self.candles[tf]) > self.max_candles:
                        self.candles[tf] = self.candles[tf][-self.max_candles:]

                self.current_candle[tf] = FootprintCandle(timestamp=candle_start, timeframe=tf)

            self.current_candle[tf].add_trade(price, size, is_buyer, self.tick_size)

    # ── Pattern Detection ──

    def _analyze_candle(self, candle: FootprintCandle):
        if not candle.levels:
            return
        candle.absorption = self._detect_absorption(candle)
        candle.stacked_imbalances = self._detect_stacked_imbalances(candle)
        self._detect_finished_auction(candle)

    def _detect_absorption(self, candle: FootprintCandle) -> List[float]:
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
            if dist_from_high < price_range * 0.2 or dist_from_low < price_range * 0.2:
                absorption_levels.append(lvl.price)
        return absorption_levels

    def _detect_stacked_imbalances(self, candle: FootprintCandle) -> List[tuple]:
        if len(candle.levels) < 3:
            return []
        sorted_levels = sorted(candle.levels.values(), key=lambda l: l.price)
        stacks = []
        current_run = []
        current_direction = 0
        for lvl in sorted_levels:
            ratio = lvl.imbalance_ratio
            direction = lvl.imbalance_direction
            if ratio >= self.imbalance_threshold and direction != 0:
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

    # ── Public API (identical to FootprintFeed) ──

    def get_latest_candle(self, timeframe: str) -> Optional[FootprintCandle]:
        return self.current_candle.get(timeframe)

    def get_completed_candles(self, timeframe: str, count: int = 10) -> List[FootprintCandle]:
        return self.candles.get(timeframe, [])[-count:]

    def get_cumulative_delta(self, timeframe: str) -> float:
        base = self.cumulative_delta.get(timeframe, 0.0)
        current = self.current_candle.get(timeframe)
        if current:
            base += current.total_delta
        return base

    def get_current_delta(self, timeframe: str) -> float:
        current = self.current_candle.get(timeframe)
        return current.total_delta if current else 0.0

    def get_poc(self, timeframe: str) -> Optional[float]:
        current = self.current_candle.get(timeframe)
        return current.poc if current else None

    def has_absorption_at_price(self, price: float, timeframe: str, tolerance: float = 2.0) -> bool:
        for candle in self._get_recent(timeframe, 2):
            for abs_price in candle.absorption:
                if abs(abs_price - price) <= tolerance:
                    return True
        return False

    def has_stacked_imbalances(self, direction: int, timeframe: str) -> int:
        count = 0
        for candle in self._get_recent(timeframe, 2):
            count += sum(1 for _, d in candle.stacked_imbalances if d == direction)
        return count

    def has_finished_auction(self, timeframe: str) -> Tuple[bool, Optional[float]]:
        current = self.current_candle.get(timeframe)
        if not current:
            return False, None
        if current.finished_auction_high is not None:
            return True, current.finished_auction_high
        if current.finished_auction_low is not None:
            return True, current.finished_auction_low
        return False, None

    def get_delta_at_price(self, price: float, timeframe: str, tolerance: float = 3.0) -> float:
        total = 0.0
        current = self.current_candle.get(timeframe)
        if not current:
            return 0.0
        for lvl_price, lvl in current.levels.items():
            if abs(lvl_price - price) <= tolerance:
                total += lvl.delta
        return total

    def get_volume_profile(self, timeframe: str) -> Dict[float, float]:
        current = self.current_candle.get(timeframe)
        if not current:
            return {}
        return {p: l.total_volume for p, l in current.levels.items()}

    def get_stats(self) -> Dict:
        stats = {}
        for tf in self.timeframes:
            current = self.current_candle.get(tf)
            stats[tf] = {
                "completed_candles": len(self.candles.get(tf, [])),
                "current_volume": current.volume if current else 0,
                "current_delta": current.total_delta if current else 0,
                "current_levels": len(current.levels) if current else 0,
                "cum_delta": self.cumulative_delta.get(tf, 0),
            }
        return stats

    def _get_recent(self, timeframe: str, count: int) -> List[FootprintCandle]:
        result = []
        current = self.current_candle.get(timeframe)
        if current:
            result.append(current)
        completed = self.candles.get(timeframe, [])
        result.extend(completed[-(count - 1):])
        return result
