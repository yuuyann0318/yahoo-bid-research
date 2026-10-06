# -*- coding: utf-8 -*-
"""利益式・入札提案価格の境界テスト（ネットワーク不使用）。"""
from __future__ import annotations

import unittest

from tests import ROOT  # noqa: F401  (sys.path を通すため)
from ybr import profit
from ybr.profiles import build_profile


def _profile(**over):
    p = build_profile("accessory", {})
    p.update(over)
    return p


def _cand(price, count=10, median=10000, postage=0, fallback_level=0):
    return {
        "auction_id": "x1",
        "title": "テスト商品",
        "price": price,
        "postage": postage,
        "comps": {
            "query": "テスト 商品",
            "count": count,
            "median": median,
            "p25": int(median * 0.9),
            "p75": int(median * 1.1),
            "min": int(median * 0.8),
            "max": int(median * 1.2),
            "mean": median,
            "fallback_level": fallback_level,
            "cache_hit": False,
        },
    }


class TestFloor100(unittest.TestCase):
    def test_floor100_rounds_down(self):
        self.assertEqual(profit.floor100(5699), 5600)
        self.assertEqual(profit.floor100(5700), 5700)
        self.assertEqual(profit.floor100(5701), 5700)

    def test_floor100_negative_goes_down(self):
        self.assertEqual(profit.floor100(-1), -100)


class TestExpectedSale(unittest.TestCase):
    def test_high_confidence_uses_median_as_is(self):
        r = profit.evaluate(_cand(1000, count=8, median=10000), _profile())
        self.assertEqual(r["expected_sale"], 10000)
        self.assertEqual(r["confidence"], "高")

    def test_low_confidence_applies_0_9(self):
        r = profit.evaluate(_cand(1000, count=5, median=10000), _profile())
        self.assertEqual(r["expected_sale"], 9000)
        self.assertEqual(r["confidence"], "低")

    def test_count_under_3_is_unknown_and_not_adopted(self):
        r = profit.evaluate(_cand(1000, count=2, median=10000), _profile())
        self.assertFalse(r["ok"])
        self.assertIsNone(r["expected_sale"])
        self.assertIn("相場不明", r["reason"])

    def test_no_comps_is_unknown(self):
        c = {"auction_id": "x", "title": "t", "price": 1000, "postage": 0, "comps": None}
        r = profit.evaluate(c, _profile())
        self.assertFalse(r["ok"])
        self.assertIn("相場不明", r["reason"])

    def test_fallback_level_1_applies_0_95(self):
        r = profit.evaluate(_cand(1000, count=10, median=10000, fallback_level=1), _profile())
        self.assertEqual(r["expected_sale"], 9500)

    def test_fallback_level_2_applies_0_90(self):
        r = profit.evaluate(_cand(1000, count=10, median=10000, fallback_level=2), _profile())
        self.assertEqual(r["expected_sale"], 9000)

    def test_low_confidence_and_fallback_compound(self):
        r = profit.evaluate(_cand(1000, count=4, median=10000, fallback_level=2), _profile())
        # 10000 * 0.9（確度低） * 0.90（縮退2段） = 8100
        self.assertEqual(r["expected_sale"], 8100)


class TestSuggestedBid(unittest.TestCase):
    def test_textbook_numbers(self):
        """売値10,000円・手数料10%・出品送料300円・仕入送料0円・目標30%。

        手取り = 10000 - 1000 - 300 = 8700
        目標利益 = max(10000*0.30, 2000) = 3000
        提案 = floor100(8700 - 3000 - 0) = 5700
        """
        r = profit.evaluate(_cand(5000, count=10, median=10000), _profile())
        self.assertEqual(r["fee"], 1000)
        self.assertEqual(r["net"], 8700)
        self.assertEqual(r["target_profit"], 3000)
        self.assertEqual(r["suggested_bid"], 5700)
        self.assertEqual(r["profit_at_bid"], 3000)
        self.assertTrue(r["ok"])

    def test_break_even_is_above_suggested(self):
        r = profit.evaluate(_cand(5000, count=10, median=10000), _profile())
        self.assertEqual(r["break_even_bid"], 8700)
        self.assertGreater(r["break_even_bid"], r["suggested_bid"])

    def test_min_profit_floor_applies_for_cheap_items(self):
        """売値5,000円で目標30%=1,500円 < minProfitYen 2,000円 → 2,000円を使う。"""
        r = profit.evaluate(_cand(1000, count=10, median=5000), _profile())
        self.assertEqual(r["target_profit"], 2000)
        # 手取り = 5000 - 500 - 300 = 4200 → floor100(4200-2000) = 2200
        self.assertEqual(r["suggested_bid"], 2200)

    def test_unknown_postage_penalty_1000(self):
        free = profit.evaluate(_cand(1000, median=10000, postage=0), _profile())
        unknown = profit.evaluate(_cand(1000, median=10000, postage=None), _profile())
        self.assertEqual(unknown["ship_in"], 1000)
        self.assertEqual(free["suggested_bid"] - unknown["suggested_bid"], 1000)

    def test_actual_postage_is_subtracted(self):
        r = profit.evaluate(_cand(1000, median=10000, postage=700), _profile())
        self.assertEqual(r["ship_in"], 700)
        self.assertEqual(r["suggested_bid"], 5000)  # floor100(8700-3000-700)

    def test_apparel_shipping_850(self):
        p = build_profile("apparel", {})
        r = profit.evaluate(_cand(1000, median=10000, postage=0), p)
        self.assertEqual(r["ship_out"], 850)
        self.assertEqual(r["net"], 10000 - 1000 - 850)
        self.assertEqual(r["suggested_bid"], 5100)  # floor100(8150-3000)

    def test_suggested_bid_is_multiple_of_100(self):
        r = profit.evaluate(_cand(1000, median=13333), _profile())
        self.assertEqual(r["suggested_bid"] % 100, 0)


class TestAdoption(unittest.TestCase):
    def test_price_equal_to_suggested_is_adopted(self):
        r = profit.evaluate(_cand(5700, median=10000), _profile())
        self.assertTrue(r["ok"], r["reason"])

    def test_price_one_yen_over_is_rejected(self):
        r = profit.evaluate(_cand(5701, median=10000), _profile())
        self.assertFalse(r["ok"])
        self.assertIn("入札上限超過", r["reason"])

    def test_suggested_below_min_bid_is_rejected(self):
        """提案額が minBidYen（既定1000円）未満なら採用しない。"""
        r = profit.evaluate(_cand(100, median=3400), _profile())
        self.assertLess(r["suggested_bid"], 1000)
        self.assertFalse(r["ok"])
        self.assertIn("提案額が下限", r["reason"])

    def test_margin_override_changes_bid(self):
        low = profit.evaluate(_cand(1000, median=100000), _profile(targetMarginPct=10))
        high = profit.evaluate(_cand(1000, median=100000), _profile(targetMarginPct=50))
        self.assertGreater(low["suggested_bid"], high["suggested_bid"])

    def test_negative_suggested_bid_is_rejected_not_crash(self):
        r = profit.evaluate(_cand(100, median=1000), _profile())
        self.assertFalse(r["ok"])
        self.assertIsInstance(r["suggested_bid"], int)


class TestMarginClamp(unittest.TestCase):
    def test_out_of_range_margin_falls_back_to_default(self):
        self.assertEqual(profit.target_margin_pct(_profile(targetMarginPct=1)), 30.0)
        self.assertEqual(profit.target_margin_pct(_profile(targetMarginPct=95)), 30.0)
        self.assertEqual(profit.target_margin_pct(_profile(targetMarginPct="abc")), 30.0)

    def test_in_range_margin_is_used(self):
        self.assertEqual(profit.target_margin_pct(_profile(targetMarginPct=45)), 45.0)


if __name__ == "__main__":
    unittest.main()
