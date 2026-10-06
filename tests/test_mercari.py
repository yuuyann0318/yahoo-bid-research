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


class _DictCache:
    def __init__(self):
        self.store = {}

    def get(self, q):
        return self.store.get(q)

    def put(self, q, stats):
        self.store[q] = dict(stats)


class TestCacheDoesNotCarryFallbackLevel(unittest.TestCase):
    """C1: キャッシュは統計だけを持つ。縮退段数と係数は取得時に毎回決める。"""

    def setUp(self):
        self.p = build_profile("accessory", {})

    def test_cached_entry_has_no_fallback_level(self):
        cache = _DictCache()
        ad = _Adapter({"ティファニー オープンハート": [50000, 50000, 50000]})
        cand = {"title": "ティファニー オープンハート", "keyword": "ティファニー"}
        _mc, comps = _comps(self.p, cand, ad, cache=cache)
        self.assertEqual(comps["fallback_level"], 0)
        stored = cache.store["ティファニー オープンハート"]
        self.assertNotIn("fallback_level", stored)
        self.assertNotIn("cache_hit", stored)
        self.assertNotIn("query", stored)

    def test_level0_cache_reused_at_level2_keeps_level2(self):
        """0段で入れたキャッシュを別商品の2段縮退で引いても level2 のまま。"""
        cache = _DictCache()
        ad = _Adapter({"ティファニー オープンハート": [50000, 50000, 50000, 50000]})
        first = {"title": "ティファニー オープンハート", "keyword": "ティファニー"}
        _m1, c1 = _comps(self.p, first, ad, cache=cache)
        self.assertEqual(c1["fallback_level"], 0)

        # 6語→3語→2語 と縮退して同じ2語クエリに到達する候補
        long_title = "ティファニー オープンハート ネックレス シルバー スモール ペンダント"
        second = {"title": long_title, "keyword": "ティファニー"}
        mc2, c2 = _comps(self.p, second, ad, cache=cache)
        self.assertEqual(c2["query"], "ティファニー オープンハート")
        self.assertEqual(c2["fallback_level"], 2, "縮退段数がキャッシュで汚染されている")
        self.assertTrue(c2["cache_hit"])
        self.assertEqual(mc2.requests_made, 2)  # 6語・3語はHTTP、2語はキャッシュ

    def test_coefficient_applies_after_cache_hit(self):
        """C1 の実害確認: 係数0.90が外れて予想売値が+11%になっていた。"""
        from ybr import profit as ybr_profit
        cache = _DictCache()
        ad = _Adapter({"ティファニー オープンハート": [50000] * 10})
        _m1, _c1 = _comps(self.p, {"title": "ティファニー オープンハート",
                                   "keyword": "ティファニー"}, ad, cache=cache)
        long_title = "ティファニー オープンハート ネックレス シルバー スモール ペンダント"
        cand = {"title": long_title, "keyword": "ティファニー", "price": 1000, "postage": 0}
        _m2, _c2 = _comps(self.p, cand, ad, cache=cache)
        res = ybr_profit.evaluate(cand, self.p)
        self.assertEqual(res["expected_sale"], 45000)  # 50,000 × 0.90

    def test_trimmed_from_survives_cache(self):
        """m18: 外れ値除外の元件数もキャッシュ経由で残る。"""
        cache = _DictCache()
        prices = [5000, 5100, 5200, 5300, 5400, 5500, 5600, 999999]
        ad = _Adapter({"テスト 商品": prices})
        _m1, c1 = _comps(self.p, {"title": "テスト 商品", "keyword": "テスト"},
                         ad, cache=cache)
        self.assertEqual(c1["trimmed_from"], 8)
        _m2, c2 = _comps(self.p, {"title": "テスト 商品", "keyword": "テスト"},
                         ad, cache=cache)
        self.assertEqual(c2["trimmed_from"], 8)
        self.assertEqual(c2["count"], 7)


class TestCompsStatus(unittest.TestCase):
    """C3: 「売切0件」と「未照会（予算切れ・失敗・中断）」を混ぜない。"""

    def setUp(self):
        self.p = build_profile("accessory", {})

    def test_zero_hits_status(self):
        cand = {"title": "存在しない 商品", "keyword": "存在しない"}
        _mc, comps = _comps(self.p, cand, _Adapter({}))
        self.assertIsNone(comps)
        self.assertEqual(cand["comps_status"], "zero_hits")
        self.assertTrue(cand["mercari_url"].startswith("https://jp.mercari.com/search?"))

    def test_budget_exhausted_status(self):
        cand = {"title": "テスト 商品", "keyword": "テスト"}
        mc, comps = _comps(self.p, cand, _Adapter({}), budget=lambda _n: False)
        self.assertIsNone(comps)
        self.assertEqual(cand["comps_status"], "budget_exhausted")
        self.assertTrue(mc.budget_exhausted)

    def test_fetch_error_status(self):
        cand = {"title": "テスト 商品", "keyword": "テスト"}
        _mc, comps = _comps(self.p, cand, _Adapter(raise_always=True))
        self.assertIsNone(comps)
        self.assertEqual(cand["comps_status"], "fetch_error")

    def test_aborted_status_after_three_failures(self):
        ad = _Adapter(raise_always=True)
        mc = mercari.MercariComps(self.p, NullCompsCache(), adapter=ad,
                                  throttle_fn=lambda _s: None)
        last = None
        for i in range(4):
            last = {"title": "テスト 商品 %d" % i, "keyword": "テスト"}
            mc.fetch(last)
        self.assertTrue(mc.aborted)
        self.assertEqual(last["comps_status"], "aborted")

    def test_no_query_status(self):
        cand = {"title": "", "keyword": ""}
        _mc, comps = _comps(self.p, cand, _Adapter({}))
        self.assertIsNone(comps)
        self.assertEqual(cand["comps_status"], "no_query")

    def test_status_kind_labels_never_say_zero_for_unqueried(self):
        for status in ("budget_exhausted", "fetch_error", "aborted", "blocked", "no_query"):
            self.assertIn("未照会", mercari.STATUS_KIND[status], status)
            self.assertNotIn("売切0件", mercari.STATUS_KIND[status], status)
        self.assertIn("売切0件", mercari.STATUS_KIND["zero_hits"])


class _HttpError(Exception):
    def __init__(self, status):
        class _R:
            status_code = status
        self.response = _R()
        super().__init__("HTTP {}".format(status))


class TestBlockedAndCounters(unittest.TestCase):
    """C4 / M7: ブロック検出と「有効相場 vs HTTP成功」の分離。"""

    def setUp(self):
        self.p = build_profile("accessory", {})

    def test_http_403_marks_blocked_and_aborts(self):
        class AD:
            calls = []

            def search(self, q):
                AD.calls.append(q)
                raise _HttpError(403)

        ad = AD()
        mc = mercari.MercariComps(self.p, NullCompsCache(), adapter=ad,
                                  throttle_fn=lambda _s: None)
        cand = {"title": "テスト 商品", "keyword": "テスト"}
        mc.fetch(cand)
        self.assertTrue(mc.blocked)
        self.assertEqual(mc.blocked_status, 403)
        self.assertTrue(mc.aborted)
        self.assertEqual(cand["comps_status"], "blocked")
        # 2件目は照会しない（1回で止まる）
        mc.fetch({"title": "別の 商品", "keyword": "別"})
        self.assertEqual(len(AD.calls), 1)

    def test_http_429_marks_blocked(self):
        class AD:
            def search(self, q):
                raise _HttpError(429)

        mc = mercari.MercariComps(self.p, NullCompsCache(), adapter=AD(),
                                  throttle_fn=lambda _s: None)
        mc.fetch({"title": "テスト 商品", "keyword": "テスト"})
        self.assertEqual(mc.blocked_status, 429)

    def test_cache_hit_counts_as_valid_comps_not_http(self):
        cache = _DictCache()
        ad = _Adapter({"テスト 商品": [1000, 2000, 3000]})
        mc1 = mercari.MercariComps(self.p, cache, adapter=ad,
                                   throttle_fn=lambda _s: None)
        mc1.fetch({"title": "テスト 商品", "keyword": "テスト"})
        self.assertEqual((mc1.valid_comps, mc1.http_successes, mc1.cache_hits), (1, 1, 0))

        mc2 = mercari.MercariComps(self.p, cache, adapter=ad,
                                   throttle_fn=lambda _s: None)
        mc2.fetch({"title": "テスト 商品", "keyword": "テスト"})
        self.assertEqual((mc2.valid_comps, mc2.http_successes, mc2.cache_hits), (1, 0, 1))
        self.assertEqual(mc2.successes, 1)  # 後方互換の別名


class TestTokenizeFixes(unittest.TestCase):
    """M14 / M15: 括弧付き型番の保持と全角トークンの正規化。"""

    def setUp(self):
        self.acc = build_profile("accessory", {})
        self.app = build_profile("apparel", {})

    def test_parenthesized_model_numbers_are_kept(self):
        q = mercari.build_queries(
            {"title": "リーバイス (501) デニム", "keyword": "リーバイス"}, self.app)
        self.assertIn("501", q[0][1])
        qa = mercari.build_queries(
            {"title": "ブルガリ (B-zero1) リング", "keyword": "ブルガリ"}, self.acc)
        self.assertIn("B-zero1", qa[0][1])

    def test_management_codes_in_parens_are_removed(self):
        for code in ("(12678_0252)", "(1234567)", "(abc12345xyz)", "(1234-5678)"):
            q = mercari.build_queries(
                {"title": "ティファニー %s オープンハート" % code,
                 "keyword": "ティファニー"}, self.acc)
            self.assertNotIn(code.strip("()"), q[0][1], code)

    def test_fullwidth_size_tokens_are_dropped(self):
        q = mercari.build_queries(
            {"title": "モンクレール ダウン Ｍ Ｗ３２ ＮＡＶＹ", "keyword": "モンクレール"},
            self.app)
        first = q[0][1]
        self.assertNotIn("Ｍ", first)
        self.assertNotIn("Ｗ３２", first)

    def test_decorative_symbols_are_stripped(self):
        """実走で発見: 「▽」のような装飾記号がクエリのトークンとして残っていた。"""
        q = mercari.build_queries(
            {"title": "▽ Tiffany&Co. ティファニー ◎ リターン トゥ ※",
             "keyword": "ティファニー"}, self.acc)
        first = q[0][1]
        for sym in ("▽", "◎", "※"):
            self.assertNotIn(sym, first)
        self.assertIn("Tiffany&Co.", first)  # ブランド表記の & は残す

    def test_fullwidth_noise_words_are_dropped(self):
        q = mercari.build_queries(
            {"title": "バーバリー トレンチコート 美品 ＵＳＥＤ", "keyword": "バーバリー"},
            self.app)
        self.assertNotIn("ＵＳＥＤ", q[0][1])


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
