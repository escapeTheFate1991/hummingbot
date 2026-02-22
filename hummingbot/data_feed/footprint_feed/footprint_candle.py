"""
FootprintCandle — Volume profile candle with bid/ask per price level.

This is a core Hummingbot data structure for order flow analysis.
Used by the FootprintFeed and consumed by strategies.
"""
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple


@dataclass
class PriceLevel:
    """Volume data at a single price level within a footprint candle."""
    price: float
    bid_volume: float = 0.0   # Taker sell volume (aggressor hitting bids)
    ask_volume: float = 0.0   # Taker buy volume (aggressor lifting asks)

    @property
    def delta(self) -> float:
        """Net order flow: positive = buying pressure, negative = selling pressure."""
        return self.ask_volume - self.bid_volume

    @property
    def total_volume(self) -> float:
        return self.bid_volume + self.ask_volume

    @property
    def imbalance_ratio(self) -> float:
        """Ratio of dominant side to weak side. >3.0 = significant imbalance."""
        if self.bid_volume == 0 and self.ask_volume == 0:
            return 0.0
        if self.bid_volume == 0 or self.ask_volume == 0:
            return float('inf')
        return max(self.ask_volume, self.bid_volume) / min(self.ask_volume, self.bid_volume)

    @property
    def imbalance_direction(self) -> int:
        """1 = bullish (ask dominant), -1 = bearish (bid dominant), 0 = neutral."""
        if self.ask_volume > self.bid_volume:
            return 1
        elif self.bid_volume > self.ask_volume:
            return -1
        return 0


@dataclass
class FootprintCandle:
    """
    A single footprint candle containing volume profile data at each price level.

    This extends a standard OHLCV candle with:
      - Bid/ask volume split per price level
      - Delta (net order flow) per level and aggregate
      - Pattern detection: absorption, stacked imbalances, finished auctions
      - Point of Control (POC) — highest volume price level

    Created and managed by FootprintFeed.
    """
    timestamp: float             # Candle start time (epoch seconds)
    timeframe: str               # "1m", "5m", etc.
    open_price: float = 0.0
    high_price: float = 0.0
    low_price: float = 0.0
    close_price: float = 0.0
    levels: Dict[float, PriceLevel] = field(default_factory=dict)

    # Detected patterns (populated by FootprintFeed._analyze_candle)
    absorption: List[float] = field(default_factory=list)
    stacked_imbalances: List[Tuple[float, int]] = field(default_factory=list)
    finished_auction_high: Optional[float] = None
    finished_auction_low: Optional[float] = None

    @property
    def volume(self) -> float:
        return sum(lvl.total_volume for lvl in self.levels.values())

    @property
    def total_delta(self) -> float:
        return sum(lvl.delta for lvl in self.levels.values())

    @property
    def bid_volume(self) -> float:
        return sum(lvl.bid_volume for lvl in self.levels.values())

    @property
    def ask_volume(self) -> float:
        return sum(lvl.ask_volume for lvl in self.levels.values())

    @property
    def poc(self) -> Optional[float]:
        """Point of Control — price level with highest total volume."""
        if not self.levels:
            return None
        return max(self.levels.values(), key=lambda l: l.total_volume).price

    def add_trade(self, price: float, size: float, is_buyer: bool, tick_size: float = 1.0):
        """
        Add a trade to the candle.

        Args:
            price: Trade price
            size: Trade size (base asset)
            is_buyer: True if taker is buyer (aggressor lifting asks)
            tick_size: Price bucketing granularity
        """
        bucketed = round(price / tick_size) * tick_size

        if bucketed not in self.levels:
            self.levels[bucketed] = PriceLevel(price=bucketed)

        level = self.levels[bucketed]
        if is_buyer:
            level.ask_volume += size
        else:
            level.bid_volume += size

        # Update OHLC
        if self.open_price == 0.0:
            self.open_price = price
        self.close_price = price
        if price > self.high_price or self.high_price == 0.0:
            self.high_price = price
        if price < self.low_price or self.low_price == 0.0:
            self.low_price = price
