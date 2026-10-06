# -*- coding: utf-8 -*-
"""メルカリ相場: クエリ構築・段階縮退・売切URL・取得失敗の扱い（ネットワーク不使用）。"""
from __future__ import annotations

import unittest
import urllib.parse

from tests import ROOT  # noqa: F401
from ybr import mercari
from ybr.cache import NullCompsCache
from ybr.profiles import build_profile


class _Adapter:
    """query -> 価格リスト を返すスタブ。未登録クエリは0件。"""

    def __init__(self, table=None, raise_always=False):
        self.table = table or {}
        self.calls = []
        self.raise_always = raise_always

    def search(self, query):
        self.calls.append(query)
        if self.raise_always:
            raise RuntimeError("stub failure")
        prices = self.table.get(query)
        if prices is None:
            return []
        return [
            _Item(p, item_type="ITEM_TYPE_MERCARI", status="ITEM_STATUS_SOLD_OUT")
            for p in prices
        ]


class _Item:
    def __init__(self, price, item_type="ITEM_TYPE_MERCARI",
                 status="ITEM_STATUS_SOLD_OUT", is_no_price=False, real_price=None):
        self.price = price
        self.real_price = real_price if real_price is not None else price
        self.item_type = item_type
        self.status = status
        self.is_no_price = is_no_price


def _comps(profile, candidate, adapter, budget=None, cache=None):
    mc = mercari.MercariComps(
        profile,
        cache or NullCompsCache(),
        adapter=adapter,
        budget=budget,
        throttle_fn=lambda _s: None,
    )
    return mc, mc.fetch(candidate)


class TestBuildQueries(unittest.TestCase):
    def test_accessory_noise_words_dropped(self):
        p = build_profile("accessory", {})
        q = mercari.build_queries(
            {"title": "【美品】ティファニー オープンハート ネックレス 送料無料 正規品 箱付き",
             "keyword": "ティファニー オープンハート"}, p)
        first = q[0][1]
        for bad in ("美品", "送料無料", "正規品", "箱付き"):
            self.assertNotIn(bad, first)
        self.assertIn("ティファニー", first)
        self.assertIn("オープンハート", first)

    def test_apparel_size_color_gender_condition_dropped(self):
        p = build_profile("apparel", {})
        q = mercari.build_queries(
            {"title": "モンクレール ダウン ジャケット メンズ M ネイビー 美品 正規品 即決",
             "keyword": "モンクレール ダウン"}, p)
        first = q[0][1]
        for bad in ("メンズ", "ネイビー", "美品", "正規品", "即決"):
            self.assertNotIn(bad, first)
        self.assertNotIn(" M", " " + first)
        self.assertIn("モンクレール", first)

    def test_apparel_numeric_size_dropped(self):
        p = build_profile("apparel", {})
        q = mercari.build_queries(
            {"title": "バーバリー トレンチコート 40 号 ベージュ", "keyword": "バーバリー"}, p)
        first = q[0][1]
        self.assertNotIn("40", first)
        self.assertNotIn("号", first)

    def test_steps_are_6_3_2_tokens(self):
        p = build_profile("apparel", {})
        title = "アークテリクス ベータ ジャケット ゴアテックス プロ シェル アルファ ハードシェル"
        q = mercari.build_queries({"title": title, "keyword": "アークテリクス"}, p)
        self.assertEqual([lv for lv, _ in q], [0, 1, 2])
        self.assertEqual(len(q[0][1].split()), 6)
        self.assertEqual(len(q[1][1].split()), 3)
        self.assertEqual(len(q[2][1].split()), 2)

    def test_bid_start_noise_tokens_dropped(self):
        """実走で発見: 「1円スタート」が残ると関連度検索で無関係な商品を拾い相場がぶれる。"""
        for cat in ("accessory", "apparel"):
            p = build_profile(cat, {})
            q = mercari.build_queries(
                {"title": "1円スタート【TIFFANY&Co.】ティファニー オープンハート ブレスレット",
                 "keyword": "ティファニー オープンハート"}, p)
            self.assertNotIn("1円スタート", q[0][1], cat)
            self.assertIn("ティファニー", q[0][1], cat)

    def test_price_like_tokens_dropped(self):
        p = build_profile("apparel", {})
        q = mercari.build_queries(
            {"title": "モンクレール ダウン 2980円即決 1スタ", "keyword": "モンクレール"}, p)
        self.assertEqual(q[0][1], "モンクレール ダウン")

    def test_model_numbers_are_kept(self):
        """501 や B-zero1 のような型番・ライン名は落とさない。"""
        p = build_profile("apparel", {})
        q = mercari.build_queries(
            {"title": "リーバイス 501 デニム", "keyword": "リーバイス"}, p)
        self.assertIn("501", q[0][1])
        pa = build_profile("accessory", {})
        qa = mercari.build_queries(
            {"title": "ブルガリ B-zero1 リング", "keyword": "ブルガリ"}, pa)
        self.assertIn("B-zero1", qa[0][1])

    def test_price_numeric_tokens_dropped_but_model_numbers_kept(self):
        p = build_profile("accessory", {})
        q = mercari.build_queries(
            {"title": "ティファニー 1837 リング 12,000円 3 2.5", "keyword": "ティファニー"}, p)
        first = q[0][1]
        self.assertIn("1837", first)
        self.assertNotIn("12,000円", first)
        self.assertNotIn("2.5", first)

    def test_duplicate_queries_collapse(self):
        p = build_profile("accessory", {})
        q = mercari.build_queries({"title": "ティファニー リング", "keyword": "ティファニー"}, p)
        self.assertEqual(len(q), 1)
        self.assertEqual(q[0][0], 0)

    def test_falls_back_to_keyword_when_title_empty(self):
        p = build_profile("accessory", {})
        q = mercari.build_queries({"title": "", "keyword": "ブルガリ ビーゼロワン"}, p)
        self.assertTrue(q)
        self.assertIn("ブルガリ", q[0][1])

    def test_no_tokens_returns_empty(self):
        p = build_profile("accessory", {})
        self.assertEqual(mercari.build_queries({"title": "", "keyword": ""}, p), [])


class TestSoldSearchUrl(unittest.TestCase):
    def test_url_has_required_params(self):
        url = mercari.sold_search_url("ティファニー オープンハート")
        self.assertTrue(url.startswith("https://jp.mercari.com/search?"))
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        self.assertEqual(qs["status"], ["sold_out"])
        self.assertEqual(qs["item_types"], ["mercari"])
        self.assertEqual(qs["order"], ["desc"])
        self.assertEqual(qs["sort"], ["created_time"])
        self.assertEqual(qs["keyword"], ["ティファニー オープンハート"])

    def test_url_is_single_line(self):
        url = mercari.sold_search_url("ティファニー\nオープンハート")
        self.assertNotIn("\n", url)


class TestFetch(unittest.TestCase):
    def setUp(self):
        self.p = build_profile("accessory", {})

    def test_success_records_stats(self):
        ad = _Adapter({"ティファニー オープンハート ネックレス": [10000, 11000, 12000]})
        cand = {"title": "ティファニー オープンハート ネックレス", "keyword": "ティファニー"}
        mc, comps = _comps(self.p, cand, ad)
        self.assertIsNotNone(comps)
        self.assertEqual(comps["count"], 3)
        self.assertEqual(comps["median"], 11000)
        self.assertEqual(comps["fallback_level"], 0)
        self.assertEqual(mc.requests_made, 1)

    def test_degrades_to_shorter_query(self):
        ad = _Adapter({"ティファニー オープンハート": [9000, 9500, 10000]})
        title = "ティファニー オープンハート ネックレス シルバー スモール サイズ"
        cand = {"title": title, "keyword": "ティファニー"}
        mc, comps = _comps(self.p, cand, ad)
        self.assertIsNotNone(comps)
        self.assertGreaterEqual(comps["fallback_level"], 1)
        self.assertEqual(comps["query"], "ティファニー オープンハート")
        self.assertGreaterEqual(mc.requests_made, 2)

    def test_zero_hits_returns_none_without_error(self):
        ad = _Adapter({})
        cand = {"title": "存在しない 商品 名前", "keyword": "存在しない"}
        mc, comps = _comps(self.p, cand, ad)
        self.assertIsNone(comps)
        self.assertNotIn("comps_error", cand)
        self.assertEqual(mc.fetch_failures, 0)

    def test_non_mercari_and_trading_items_excluded(self):
        class AD:
            calls = []

            def search(self, query):
                AD.calls.append(query)
                return [
                    _Item(100000, item_type="ITEM_TYPE_BEYOND"),
                    _Item(200000, status="ITEM_STATUS_ON_SALE"),
                    _Item(300000, is_no_price=True),
                    _Item(5000),
                    _Item(7000),
                    _Item(6000),
                ]

        cand = {"title": "テスト 商品", "keyword": "テスト"}
        mc, comps = _comps(self.p, cand, AD())
        self.assertEqual(comps["count"], 3)
        self.assertEqual(comps["median"], 6000)

    def test_real_price_preferred(self):
        class AD:
            def search(self, query):
                return [_Item(1, real_price=5000), _Item(1, real_price=6000),
                        _Item(1, real_price=7000)]

        cand = {"title": "テスト 商品", "keyword": "テスト"}
        _mc, comps = _comps(self.p, cand, AD())
        self.assertEqual(comps["median"], 6000)

    def test_adapter_error_sets_comps_error(self):
        ad = _Adapter(raise_always=True)
        cand = {"title": "テスト 商品", "keyword": "テスト"}
        mc, comps = _comps(self.p, cand, ad)
        self.assertIsNone(comps)
        self.assertIn("comps_error", cand)
        self.assertEqual(mc.fetch_failures, 1)

    def test_three_consecutive_failures_abort(self):
        ad = _Adapter(raise_always=True)
        mc = mercari.MercariComps(self.p, NullCompsCache(), adapter=ad,
                                  throttle_fn=lambda _s: None)
        for i in range(4):
            mc.fetch({"title": "テスト 商品 %d" % i, "keyword": "テスト"})
        self.assertTrue(mc.aborted)
        self.assertEqual(len(ad.calls), 3)

    def test_budget_exhausted_stops(self):
        ad = _Adapter({"テスト 商品": [1000, 2000, 3000]})
        cand = {"title": "テスト 商品", "keyword": "テスト"}
        mc, comps = _comps(self.p, cand, ad, budget=lambda _n: False)
        self.assertIsNone(comps)
        self.assertTrue(mc.budget_exhausted)
        self.assertEqual(ad.calls, [])

    def test_cache_hit_skips_adapter(self):
        class Cache:
            def __init__(self):
                self.store = {}

            def get(self, q):
                return self.store.get(q)

            def put(self, q, comps):
                self.store[q] = comps

        cache = Cache()
        ad = _Adapter({"テスト 商品": [1000, 2000, 3000]})
        cand1 = {"title": "テスト 商品", "keyword": "テスト"}
        _mc1, comps1 = _comps(self.p, cand1, ad, cache=cache)
        self.assertFalse(comps1["cache_hit"])
        cand2 = {"title": "テスト 商品", "keyword": "テスト"}
        mc2, comps2 = _comps(self.p, cand2, ad, cache=cache)
        self.assertTrue(comps2["cache_hit"])
        self.assertEqual(mc2.requests_made, 0)
        self.assertEqual(len(ad.calls), 1)


class TestStats(unittest.TestCase):
    def test_iqr_trim_applies_at_8_or_more(self):
        prices = [5000, 5100, 5200, 5300, 5400, 5500, 5600, 999999]
        st = mercari.compute_stats(prices)
        self.assertEqual(st["count"], 7)
        self.assertEqual(st["trimmed_from"], 8)

    def test_no_trim_under_8(self):
        st = mercari.compute_stats([5000, 5100, 999999])
        self.assertEqual(st["count"], 3)
        self.assertNotIn("trimmed_from", st)

    def test_zero_prices_returns_count_zero_only(self):
        self.assertEqual(mercari.compute_stats([]), {"count": 0})


if __name__ == "__main__":
    unittest.main()
