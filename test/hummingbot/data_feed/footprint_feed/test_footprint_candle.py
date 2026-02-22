"""Unit tests for FootprintCandle and PriceLevel."""
import unittest
import sys
import os
import importlib.util

# Direct-load footprint_candle to bypass hummingbot.__init__ (needs pandas/conda)
_MODULE_PATH = os.path.join(
    os.path.dirname(__file__), '..', '..', '..', '..',
    'hummingbot', 'data_feed', 'footprint_feed', 'footprint_candle.py'
)
_spec = importlib.util.spec_from_file_location("footprint_candle", os.path.abspath(_MODULE_PATH))
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
FootprintCandle = _mod.FootprintCandle
PriceLevel = _mod.PriceLevel


class TestPriceLevel(unittest.TestCase):
    """Tests for the PriceLevel data class."""

    def _make_level(self, price=100.0, bid=0.0, ask=0.0):
        return PriceLevel(price=price, bid_volume=bid, ask_volume=ask)

    def test_delta_positive(self):
        lvl = self._make_level(bid=1.0, ask=3.0)
        self.assertAlmostEqual(lvl.delta, 2.0)

    def test_delta_negative(self):
        lvl = self._make_level(bid=5.0, ask=1.0)
        self.assertAlmostEqual(lvl.delta, -4.0)

    def test_total_volume(self):
        lvl = self._make_level(bid=2.0, ask=3.0)
        self.assertAlmostEqual(lvl.total_volume, 5.0)

    def test_imbalance_ratio_bullish(self):
        lvl = self._make_level(bid=1.0, ask=4.0)
        self.assertAlmostEqual(lvl.imbalance_ratio, 4.0)

    def test_imbalance_ratio_bearish(self):
        lvl = self._make_level(bid=6.0, ask=2.0)
        self.assertAlmostEqual(lvl.imbalance_ratio, 3.0)

    def test_imbalance_ratio_zero_volume(self):
        lvl = self._make_level(bid=0.0, ask=0.0)
        self.assertEqual(lvl.imbalance_ratio, 0.0)

    def test_imbalance_ratio_one_side_zero(self):
        lvl = self._make_level(bid=0.0, ask=5.0)
        self.assertEqual(lvl.imbalance_ratio, float('inf'))

    def test_imbalance_direction(self):
        self.assertEqual(self._make_level(bid=1, ask=3).imbalance_direction, 1)
        self.assertEqual(self._make_level(bid=3, ask=1).imbalance_direction, -1)
        self.assertEqual(self._make_level(bid=2, ask=2).imbalance_direction, 0)


class TestFootprintCandle(unittest.TestCase):
    """Tests for FootprintCandle."""

    def _make_candle(self):
        return FootprintCandle(timestamp=1000.0, timeframe="5m")

    def test_add_trade_updates_ohlc(self):
        c = self._make_candle()
        c.add_trade(100.0, 1.0, True)
        c.add_trade(102.0, 1.0, False)
        c.add_trade(99.0, 1.0, True)
        c.add_trade(101.0, 1.0, False)

        self.assertAlmostEqual(c.open_price, 100.0)
        self.assertAlmostEqual(c.close_price, 101.0)
        self.assertAlmostEqual(c.high_price, 102.0)
        self.assertAlmostEqual(c.low_price, 99.0)

    def test_add_trade_volume_split(self):
        c = self._make_candle()
        c.add_trade(100.0, 1.0, True)   # buyer → ask_volume
        c.add_trade(100.0, 2.0, False)  # seller → bid_volume

        self.assertAlmostEqual(c.ask_volume, 1.0)
        self.assertAlmostEqual(c.bid_volume, 2.0)
        self.assertAlmostEqual(c.volume, 3.0)

    def test_delta_calculation(self):
        c = self._make_candle()
        c.add_trade(100.0, 3.0, True)   # +3 ask
        c.add_trade(100.0, 1.0, False)  # +1 bid
        # delta = ask - bid = 3 - 1 = 2
        self.assertAlmostEqual(c.total_delta, 2.0)

    def test_tick_bucketing(self):
        c = self._make_candle()
        c.add_trade(100.3, 1.0, True, tick_size=1.0)
        c.add_trade(100.7, 1.0, True, tick_size=1.0)
        # Both should bucket to 100.0 with tick_size=1.0
        # 100.3 rounds to 100.0, 100.7 rounds to 101.0
        self.assertIn(100.0, c.levels)
        self.assertIn(101.0, c.levels)

    def test_poc(self):
        c = self._make_candle()
        c.add_trade(100.0, 5.0, True)
        c.add_trade(101.0, 1.0, True)
        c.add_trade(102.0, 2.0, False)
        # 100.0 has most volume (5.0)
        self.assertAlmostEqual(c.poc, 100.0)

    def test_poc_empty_candle(self):
        c = self._make_candle()
        self.assertIsNone(c.poc)

    def test_volume_empty_candle(self):
        c = self._make_candle()
        self.assertAlmostEqual(c.volume, 0.0)
        self.assertAlmostEqual(c.total_delta, 0.0)


if __name__ == "__main__":
    unittest.main()
