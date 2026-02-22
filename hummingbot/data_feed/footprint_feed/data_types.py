"""
Configuration and data types for the Footprint data feed.
"""
from pydantic import BaseModel
from typing import List, Optional


class FootprintConfig(BaseModel):
    """
    Configuration for a FootprintFeed instance.

    Attributes:
        connector: Exchange connector name (e.g., "hyperliquid_perpetual")
        trading_pair: Trading pair (e.g., "BTC-USD")
        timeframes: List of timeframes to build candles for (e.g., ["1m", "5m"])
        tick_size: Price bucketing granularity in dollars (default 1.0)
        imbalance_threshold: Min ratio for imbalance detection (default 3.0)
        max_candles: Max completed candles to retain per timeframe (default 50)
        domain: Exchange domain (e.g., "hyperliquid_perpetual_testnet")
    """
    connector: str
    trading_pair: str
    timeframes: List[str] = ["1m", "5m"]
    tick_size: float = 1.0
    imbalance_threshold: float = 3.0
    max_candles: int = 50
    domain: Optional[str] = None
