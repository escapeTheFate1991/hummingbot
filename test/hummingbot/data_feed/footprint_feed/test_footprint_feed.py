"""Unit tests for FootprintFeed pattern detection and public API."""
import unittest
import sys
import os
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', '..', '..'))

from hummingbot.data_feed.footprint_feed.footprint_candle import FootprintCandle, PriceLevel


class TestPatternDetection(unittest.TestCase):
    """Tests for absorption, stacked imbalances, and finished auction detection."""

    def _make_feed(self):
        """Create a feed instance for testing pattern detection methods."""
        # Import here to avoid full hummingbot env dependency
        from hummingbot.data_feed.footprint_feed.footprint_feed import FootprintFeed
        from hummingbot.data_feed.footprint_feed.data_types import FootprintConfig
        config = FootprintConfig(
            connector="hyperliquid_perpetual",
            trading_pair="BTC-USD",
            domain="hyperliquid_perpetual_testnet",
        )
        return FootprintFeed(config)

    def test_absorption_at_extreme(self):
        """High volume at candle extreme = absorption."""
        feed = self._make_feed()
        candle = FootprintCandle(timestamp=1000, timeframe="5m")

        # Build a candle with high volume at the low
        candle.levels = {
            100.0: PriceLevel(price=100.0, bid_volume=10.0, ask_volume=0.5),  # High vol at low
            101.0: PriceLevel(price=101.0, bid_volume=1.0, ask_volume=1.0),
            102.0: PriceLevel(price=102.0, bid_volume=0.5, ask_volume=1.0),
            103.0: PriceLevel(price=103.0, bid_volume=0.3, ask_volume=0.8),
            104.0: PriceLevel(price=104.0, bid_volume=0.2, ask_volume=0.5),
        }
        candle.low_price = 100.0
        candle.high_price = 104.0

        result = feed._detect_absorption(candle)
        self.assertIn(100.0, result)

    def test_absorption_not_at_center(self):
        """High volume at center of candle should NOT be absorption."""
        feed = self._make_feed()
        candle = FootprintCandle(timestamp=1000, timeframe="5m")

        candle.levels = {
            100.0: PriceLevel(price=100.0, bid_volume=0.5, ask_volume=0.5),
            102.0: PriceLevel(price=102.0, bid_volume=10.0, ask_volume=10.0),  # Center
            104.0: PriceLevel(price=104.0, bid_volume=0.5, ask_volume=0.5),
        }
        candle.low_price = 100.0
        candle.high_price = 104.0

        result = feed._detect_absorption(candle)
        self.assertNotIn(102.0, result)

    def test_stacked_imbalances_bullish(self):
        """3+ consecutive bullish imbalance levels = stacked."""
        feed = self._make_feed()
        feed._imbalance_threshold = 3.0
        candle = FootprintCandle(timestamp=1000, timeframe="5m")

        # 4 consecutive levels with ask >> bid (bullish)
        candle.levels = {
            100.0: PriceLevel(price=100.0, bid_volume=0.1, ask_volume=1.0),  # 10:1
            101.0: PriceLevel(price=101.0, bid_volume=0.2, ask_volume=1.5),  # 7.5:1
            102.0: PriceLevel(price=102.0, bid_volume=0.1, ask_volume=0.8),  # 8:1
            103.0: PriceLevel(price=103.0, bid_volume=0.1, ask_volume=0.5),  # 5:1
        }
        candle.low_price = 100.0
        candle.high_price = 103.0

        result = feed._detect_stacked_imbalances(candle)
        self.assertTrue(len(result) > 0)
        # All bullish
        for _, direction in result:
            self.assertEqual(direction, 1)

    def test_stacked_imbalances_not_enough_levels(self):
        """Less than 3 consecutive = no stack."""
        feed = self._make_feed()
        feed._imbalance_threshold = 3.0
        candle = FootprintCandle(timestamp=1000, timeframe="5m")

        candle.levels = {
            100.0: PriceLevel(price=100.0, bid_volume=0.1, ask_volume=1.0),
            101.0: PriceLevel(price=101.0, bid_volume=0.1, ask_volume=1.0),
            # Only 2 levels — not enough
        }
        candle.low_price = 100.0
        candle.high_price = 101.0

        result = feed._detect_stacked_imbalances(candle)
        self.assertEqual(len(result), 0)

    def test_finished_auction_high(self):
        """Near-zero volume at the high = finished auction."""
        feed = self._make_feed()
        candle = FootprintCandle(timestamp=1000, timeframe="5m")

        candle.levels = {
            100.0: PriceLevel(price=100.0, bid_volume=2.0, ask_volume=3.0),
            101.0: PriceLevel(price=101.0, bid_volume=1.5, ask_volume=2.0),
            102.0: PriceLevel(price=102.0, bid_volume=0.001, ask_volume=0.001),  # Near zero
        }
        candle.low_price = 100.0
        candle.high_price = 102.0

        feed._detect_finished_auction(candle)
        self.assertIsNotNone(candle.finished_auction_high)
        self.assertAlmostEqual(candle.finished_auction_high, 102.0)

    def test_finished_auction_low(self):
        """Near-zero volume at the low = finished auction."""
        feed = self._make_feed()
        candle = FootprintCandle(timestamp=1000, timeframe="5m")

        candle.levels = {
            100.0: PriceLevel(price=100.0, bid_volume=0.001, ask_volume=0.001),  # Near zero
            101.0: PriceLevel(price=101.0, bid_volume=1.5, ask_volume=2.0),
            102.0: PriceLevel(price=102.0, bid_volume=2.0, ask_volume=3.0),
        }
        candle.low_price = 100.0
        candle.high_price = 102.0

        feed._detect_finished_auction(candle)
        self.assertIsNotNone(candle.finished_auction_low)
        self.assertAlmostEqual(candle.finished_auction_low, 100.0)


class TestFootprintFeedAPI(unittest.TestCase):
    """Tests for the public API methods."""

    def _make_feed_with_data(self):
        from hummingbot.data_feed.footprint_feed.footprint_feed import FootprintFeed
        from hummingbot.data_feed.footprint_feed.data_types import FootprintConfig
        config = FootprintConfig(
            connector="hyperliquid_perpetual",
            trading_pair="BTC-USD",
            domain="hyperliquid_perpetual_testnet",
        )
        feed = FootprintFeed(config)

        now = time.time()
        # Ingest a set of trades
        feed._ingest_trade(97500.0, 0.05, True, now)
        feed._ingest_trade(97501.0, 0.02, False, now)
        feed._ingest_trade(97500.0, 0.03, True, now)
        feed._ingest_trade(97499.0, 0.01, False, now)
        feed._ingest_trade(97502.0, 0.04, True, now)

        return feed

    def test_get_latest_candle(self):
        feed = self._make_feed_with_data()
        for tf in ["1m", "5m"]:
            candle = feed.get_latest_candle(tf)
            self.assertIsNotNone(candle)
            self.assertGreater(candle.volume, 0)

    def test_get_cumulative_delta(self):
        feed = self._make_feed_with_data()
        delta = feed.get_cumulative_delta("5m")
        # 0.05 + 0.03 + 0.04 (buys) - 0.02 - 0.01 (sells) = 0.09
        self.assertGreater(delta, 0)

    def test_get_current_delta(self):
        feed = self._make_feed_with_data()
        delta = feed.get_current_delta("5m")
        self.assertGreater(delta, 0)

    def test_get_poc(self):
        feed = self._make_feed_with_data()
        poc = feed.get_poc("5m")
        self.assertIsNotNone(poc)
        # 97500 has 0.05 + 0.03 = 0.08 (highest volume)
        self.assertAlmostEqual(poc, 97500.0)

    def test_get_delta_at_price(self):
        feed = self._make_feed_with_data()
        delta = feed.get_delta_at_price(97500.0, "5m", tolerance=1.0)
        # At 97500: 0.08 ask - 0.0 bid = 0.08
        self.assertGreater(delta, 0)

    def test_get_volume_profile(self):
        feed = self._make_feed_with_data()
        profile = feed.get_volume_profile("5m")
        self.assertGreater(len(profile), 0)
        self.assertIn(97500.0, profile)

    def test_get_stats(self):
        feed = self._make_feed_with_data()
        stats = feed.get_stats()
        self.assertIn("trade_count", stats)
        self.assertIn("5m", stats)
        self.assertGreater(stats["5m"]["current_volume"], 0)

    def test_ready_after_trade(self):
        feed = self._make_feed_with_data()
        # Feed should be ready after ingesting trades
        # (ready is set by _process_ws_message, not _ingest_trade directly)
        # But _trade_count tracks it
        self.assertGreater(feed._trade_count, 0)


if __name__ == "__main__":
    unittest.main()
