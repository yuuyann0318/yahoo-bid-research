# -*- coding: utf-8 -*-
"""プロファイル・検索語ファイルの読み込みと上書き。"""
from __future__ import annotations

import json
import os
import tempfile
import unittest

from tests import ROOT  # noqa: F401
from ybr import profiles


class TestBuildProfile(unittest.TestCase):
    def test_accessory_defaults(self):
        p = profiles.build_profile("accessory", {})
        self.assertEqual(p["sellShippingYen"], 300)
        self.assertEqual(p["mercariFeeRate"], 0.10)
        self.assertEqual(p["minProfitYen"], 2000)
        self.assertEqual(p["minBidYen"], 1000)
        self.assertEqual(p["unknownShippingPenaltyYen"], 1000)

    def test_apparel_defaults(self):
        p = profiles.build_profile("apparel", {})
        self.assertEqual(p["sellShippingYen"], 850)

    def test_unknown_category_raises(self):
        with self.assertRaises(ValueError):
            profiles.build_profile("shoes", {})

    def test_overrides_applied(self):
        p = profiles.build_profile("accessory", {"sellShippingYen": 210})
        self.assertEqual(p["sellShippingYen"], 210)

    def test_ng_words_are_lists(self):
        for name in ("accessory", "apparel"):
            p = profiles.build_profile(name, {})
            self.assertIsInstance(p["ngWords"], list)
            self.assertTrue(p["ngWords"])
            self.assertIsInstance(p["allowPhrases"], list)
            self.assertIsInstance(p["noiseWords"], list)
            self.assertIsInstance(p["dropTokenPatterns"], list)


class TestLoadProfileFile(unittest.TestCase):
    def test_reads_json_and_merges(self):
        with tempfile.TemporaryDirectory() as d:
            pdir = os.path.join(d, "profiles")
            os.makedirs(pdir)
            with open(os.path.join(pdir, "accessory.json"), "w", encoding="utf-8") as f:
                json.dump({"sellShippingYen": 160, "minProfitYen": 1500}, f)
            p = profiles.load_profile("accessory", base_dir=d)
            self.assertEqual(p["sellShippingYen"], 160)
            self.assertEqual(p["minProfitYen"], 1500)
            self.assertEqual(p["mercariFeeRate"], 0.10)  # 既定が残る

    def test_missing_file_uses_builtin_defaults(self):
        with tempfile.TemporaryDirectory() as d:
            p = profiles.load_profile("apparel", base_dir=d)
            self.assertEqual(p["sellShippingYen"], 850)

    def test_broken_json_raises_value_error(self):
        with tempfile.TemporaryDirectory() as d:
            pdir = os.path.join(d, "profiles")
            os.makedirs(pdir)
            with open(os.path.join(pdir, "accessory.json"), "w", encoding="utf-8") as f:
                f.write("{not json")
            with self.assertRaises(ValueError):
                profiles.load_profile("accessory", base_dir=d)

    def test_repo_profiles_load(self):
        for name in ("accessory", "apparel"):
            p = profiles.load_profile(name)
            self.assertIn("ngWords", p)
            self.assertTrue(p["ngWords"])


class TestKeywords(unittest.TestCase):
    def test_comments_and_blanks_ignored(self):
        with tempfile.TemporaryDirectory() as d:
            kdir = os.path.join(d, "keywords")
            os.makedirs(kdir)
            with open(os.path.join(kdir, "accessory.txt"), "w", encoding="utf-8") as f:
                f.write("# コメント\n\nティファニー オープンハート\n  グッチ シルバー  \n")
            kws = profiles.load_keywords("accessory", base_dir=d)
            self.assertEqual(kws, ["ティファニー オープンハート", "グッチ シルバー"])

    def test_dedupes(self):
        with tempfile.TemporaryDirectory() as d:
            kdir = os.path.join(d, "keywords")
            os.makedirs(kdir)
            with open(os.path.join(kdir, "accessory.txt"), "w", encoding="utf-8") as f:
                f.write("A\nA\nB\n")
            self.assertEqual(profiles.load_keywords("accessory", base_dir=d), ["A", "B"])

    def test_brand_filter(self):
        with tempfile.TemporaryDirectory() as d:
            kdir = os.path.join(d, "keywords")
            os.makedirs(kdir)
            with open(os.path.join(kdir, "apparel.txt"), "w", encoding="utf-8") as f:
                f.write("バーバリー トレンチコート\nモンクレール ダウン\nパタゴニア フリース\n")
            kws = profiles.load_keywords("apparel", base_dir=d, brands=["モンクレール"])
            self.assertEqual(kws, ["モンクレール ダウン"])

    def test_brand_filter_no_match_uses_brand_as_keyword(self):
        with tempfile.TemporaryDirectory() as d:
            kdir = os.path.join(d, "keywords")
            os.makedirs(kdir)
            with open(os.path.join(kdir, "apparel.txt"), "w", encoding="utf-8") as f:
                f.write("バーバリー トレンチコート\n")
            kws = profiles.load_keywords("apparel", base_dir=d, brands=["ジルサンダー"])
            self.assertEqual(kws, ["ジルサンダー"])

    def test_repo_keyword_files_are_non_empty(self):
        for name in ("accessory", "apparel"):
            kws = profiles.load_keywords(name)
            self.assertGreaterEqual(len(kws), 5, name)

    def test_repo_apparel_has_expected_brands(self):
        kws = " / ".join(profiles.load_keywords("apparel"))
        for brand in ("バーバリー", "モンクレール", "カナダグース", "ノースフェイス",
                      "パタゴニア", "アークテリクス", "シュプリーム", "ステューシー",
                      "ラルフローレン", "トミーヒルフィガー", "コムデギャルソン",
                      "ヨウジヤマモト", "イッセイミヤケ", "マルジェラ", "ストーンアイランド",
                      "バブアー", "リーバイス", "チャンピオン", "カーハート"):
            self.assertIn(brand, kws, brand)

    def test_repo_accessory_has_expected_brands(self):
        kws = " / ".join(profiles.load_keywords("accessory"))
        for brand in ("ティファニー", "カルティエ", "ブルガリ", "グッチ", "エルメス",
                      "ジョージジェンセン", "ミキモト", "4℃"):
            self.assertIn(brand, kws, brand)


class TestResolveCategory(unittest.TestCase):
    def test_known_accessory_keyword(self):
        self.assertEqual(profiles.resolve_category("ティファニー オープンハート"), "accessory")

    def test_known_apparel_keyword(self):
        self.assertEqual(profiles.resolve_category("モンクレール ダウン"), "apparel")

    def test_undecidable_keyword_is_unknown(self):
        """M11: 判定できない語は apparel と断定せず unknown にする。"""
        self.assertEqual(profiles.resolve_category("謎のブランド 謎の商品"), "unknown")


class TestUnionProfile(unittest.TestCase):
    """M11: カテゴリ不明の語は送料だけでなくNG語も保守側（両カテゴリの和集合）にする。"""

    def setUp(self):
        self.union = profiles.load_union_profile()

    def test_shipping_is_apparel_side(self):
        self.assertEqual(self.union["sellShippingYen"], 850)

    def test_category_is_unknown(self):
        self.assertEqual(self.union["category"], "unknown")

    def test_ng_words_include_both_categories(self):
        for word in ("めっき", "GP", "石取れ"):          # accessory 側
            self.assertIn(word, self.union["ngWords"], word)
        for word in ("リメイク", "虫食い", "破れ"):        # apparel 側
            self.assertIn(word, self.union["ngWords"], word)

    def test_jewelry_ng_applies_to_unknown_keyword(self):
        from ybr import filter as ybr_filter
        hit = ybr_filter.ng_hit("シャネル ピアス K18GP", self.union["ngWords"],
                                self.union["allowPhrases"], self.union["ngPatterns"])
        self.assertIsNotNone(hit)

    def test_profile_for_resolved_dispatches(self):
        self.assertEqual(
            profiles.profile_for_resolved("unknown")["category"], "unknown")
        self.assertEqual(
            profiles.profile_for_resolved("accessory")["sellShippingYen"], 300)


class TestBrandTerms(unittest.TestCase):
    def test_brand_terms_come_from_keywords_file(self):
        terms = profiles.brand_terms("apparel")
        self.assertIn("モンクレール", terms)
        self.assertIn("バーバリー", terms)

    def test_load_profile_injects_brand_terms(self):
        p = profiles.load_profile("accessory")
        self.assertTrue(p["brandTerms"])
        self.assertIn("ティファニー", p["brandTerms"])

    def test_brand_suffix_words_present(self):
        for cat in ("accessory", "apparel"):
            self.assertIn("風", profiles.load_profile(cat)["brandSuffixNgWords"])


if __name__ == "__main__":
    unittest.main()
