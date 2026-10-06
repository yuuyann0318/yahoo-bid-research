# -*- coding: utf-8 -*-
"""一次フィルタ: NG語の正規化迂回・否定表現の救済・レンジ・残り時間・重複。"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from tests import ROOT  # noqa: F401
from ybr import filter as ybr_filter
from ybr.profiles import build_profile, load_profile

JST = timezone(timedelta(hours=9))
NOW = datetime(2026, 10, 6, 12, 0, 0, tzinfo=JST)


class TestNormalize(unittest.TestCase):
    def test_halfwidth_kana_normalizes(self):
        self.assertEqual(
            ybr_filter.normalize_for_match("ﾚﾌﾟﾘｶ"),
            ybr_filter.normalize_for_match("レプリカ"),
        )

    def test_fullwidth_ascii_normalizes_and_lowercases(self):
        self.assertEqual(ybr_filter.normalize_for_match("ＧＰ"), "gp")

    def test_none_is_empty(self):
        self.assertEqual(ybr_filter.normalize_for_match(None), "")


class TestNgHit(unittest.TestCase):
    def setUp(self):
        # brandTerms（<ブランド名>風 判定用）は load_profile が検索語ファイルから注入する
        self.acc = load_profile("accessory")
        self.app = load_profile("apparel")

    def _hit(self, profile, title):
        return ybr_filter.ng_hit(
            title, profile["ngWords"], profile["allowPhrases"], profile["ngPatterns"],
            brand_terms=profile.get("brandTerms"),
            brand_suffix_words=profile.get("brandSuffixNgWords"),
            allow_patterns=profile.get("allowPatterns"))

    def _acc(self, title):
        return self._hit(self.acc, title)

    def _app(self, title):
        return self._hit(self.app, title)

    def test_plain_copy_word_hits(self):
        self.assertIsNotNone(self._acc("ティファニー ネックレス コピー品"))

    def test_halfwidth_kana_replica_hits(self):
        self.assertIsNotNone(self._acc("ティファニー ネックレス ﾚﾌﾟﾘｶ"))

    def test_fullwidth_gp_hits(self):
        self.assertIsNotNone(self._acc("ネックレス １８Ｋ ＧＰ"))

    def test_gps_does_not_hit_gp(self):
        self.assertIsNone(self._acc("ガーミン GPS ウォッチ"))

    def test_gp_negation_is_rescued(self):
        self.assertIsNone(self._acc("ティファニー シルバー GPではありません"))

    def test_mekki_negation_is_rescued(self):
        self.assertIsNone(self._acc("シルバー925 メッキではありません"))

    def test_rhodium_coating_is_not_ng(self):
        self.assertIsNone(self._acc("ティファニー ロジウムコーティング 仕上げ"))

    def test_mekki_hits(self):
        self.assertIsNotNone(self._acc("金メッキ ネックレス"))

    def test_apparel_damage_kakou_is_rescued(self):
        self.assertIsNone(self._app("リーバイス 501 ダメージ加工 W32"))

    def test_apparel_vintage_kakou_is_rescued(self):
        self.assertIsNone(self._app("リーバイス ヴィンテージ加工 デニム"))

    def test_apparel_damage_ari_hits(self):
        self.assertIsNotNone(self._app("リーバイス 501 ダメージあり"))

    def test_apparel_yogore_hits(self):
        self.assertIsNotNone(self._app("シュプリーム パーカー 汚れあり"))

    def test_apparel_supercopy_hits(self):
        self.assertIsNotNone(self._app("バーバリー スーパーコピー トレンチ"))

    def test_empty_title_is_none(self):
        self.assertIsNone(self._acc(""))

    # --- M5: 品位+めっき表記（最頻表記）を検出する ---
    def test_plating_marks_are_detected(self):
        for title in ("K18GP ネックレス", "18KGP リング", "K14GF ブレス",
                      "K18GF チェーン", "SV925GP ペンダント", "925GP リング",
                      "18K GP ネックレス", "K18-GP リング", "18金GP",
                      "gold plated ネックレス", "GOLD FILLED チェーン",
                      "GP刻印 あり"):
            self.assertIsNotNone(self._acc(title), title)

    def test_plating_pattern_does_not_hit_gps_suffix(self):
        self.assertIsNone(self._acc("K18 GPSケース"))

    def test_k18gp_negation_is_rescued(self):
        self.assertIsNone(self._acc("K18GPではありません 本物のK18"))

    # --- M13: 救済は完全包含のときだけ ---
    def test_supercopy_inside_copyright_is_not_rescued(self):
        self.assertIsNotNone(self._app("バーバリー スーパーコピーライト 表記"))

    def test_plain_copyright_is_rescued(self):
        self.assertIsNone(self._app("バーバリー トレンチコート コピーライト 2026"))

    # --- Codex#5: 区切り挿入による迂回（カタカナNG語） ---
    def test_separator_inserted_katakana_ng_is_detected(self):
        self.assertIsNotNone(self._acc("ティファニー レ プ リ カ ネックレス"))
        self.assertIsNotNone(self._app("バーバリー コ・ピー 品"))

    def test_separator_strip_does_not_break_long_vowel_words(self):
        self.assertIsNotNone(self._app("シュプリーム コピー パーカー"))

    # --- M23: 「風」「タイプ」はブランド名の直後だけNG ---
    def test_brand_fu_is_ng(self):
        self.assertIsNotNone(self._acc("ティファニー風 ネックレス シルバー"))

    def test_brand_type_is_ng(self):
        self.assertIsNotNone(self._app("バーバリータイプ トレンチコート"))

    def test_brand_fu_with_separator_is_ng(self):
        self.assertIsNotNone(self._app("モンクレール 風 ダウン"))

    def test_harukaze_is_not_ng(self):
        self.assertIsNone(self._app("春風コレクション シャツ"))

    def test_a_type_is_not_ng(self):
        self.assertIsNone(self._app("Aタイプ ジャケット 未使用"))

    def test_standalone_fu_word_is_not_ng(self):
        self.assertIsNone(self._app("北欧 インテリア 扇風機 カバー"))

    # --- M10/M12/M23: アパレル語彙の修正 ---
    def test_remake_is_ng(self):
        self.assertIsNotNone(self._app("リーバイス リメイク デニムスカート"))

    def test_mushikui_is_ng(self):
        self.assertIsNotNone(self._app("カシミヤ ニット 虫食い あり"))

    def test_shimi_ari_is_ng(self):
        for title in ("シャツ シミあり", "コート シミ有", "ニット シミ多", "シャツ 染みあり"):
            self.assertIsNotNone(self._app(title), title)

    def test_cashmere_is_not_ng(self):
        """「シミ」単独をNG/注意にすると カシミヤ・カシミア に誤爆する（M23）。"""
        for title in ("カシミヤ 100% コート", "カシミア ニット 極美品"):
            self.assertIsNone(self._app(title), title)
            self.assertEqual(
                ybr_filter.caution_hits(title, self.app["cautionWords"]), [], title)

    def test_damage_sukoshi_is_not_rescued_and_is_caution(self):
        """M12: 「ダメージ少」は救済語ではなく注意語。"""
        self.assertNotIn("ダメージ少", self.app["allowPhrases"])
        self.assertIn("ダメージ少", self.app["cautionWords"])
        self.assertIsNotNone(self._app("リーバイス ダメージ少なめ"))


def _item(aid="x1", title="ティファニー オープンハート", price=12000, postage=0, minutes=1440):
    return {
        "auction_id": aid,
        "title": title,
        "url": "https://page.auctions.yahoo.co.jp/jp/auction/" + aid,
        "price": price,
        "postage": postage,
        "bids": 1,
        "is_store": False,
        "minutes_remaining": minutes,
        "end_at": (NOW + timedelta(minutes=minutes)).isoformat(),
        "keyword": "ティファニー オープンハート",
        "excluded_reason": None,
    }


class _History:
    def __init__(self, ids=()):
        self.ids = set(ids)

    def is_duplicate(self, candidate):
        return (candidate or {}).get("auction_id") in self.ids


class TestPrefilter(unittest.TestCase):
    def setUp(self):
        self.p = build_profile("accessory", {})

    def _run(self, items, history=None):
        return ybr_filter.prefilter(items, self.p, history=history, now=NOW)

    def test_normal_item_kept(self):
        kept, excluded = self._run([_item()])
        self.assertEqual(len(kept), 1)
        self.assertEqual(excluded, [])

    def test_total_below_min_excluded(self):
        kept, excluded = self._run([_item(price=500)])
        self.assertEqual(kept, [])
        self.assertIn("総額レンジ外", excluded[0]["excluded_reason"])

    def test_total_above_max_excluded(self):
        kept, excluded = self._run([_item(price=999999)])
        self.assertEqual(kept, [])
        self.assertIn("総額レンジ外", excluded[0]["excluded_reason"])

    def test_total_uses_postage(self):
        # minTotal 3000。価格2900 + 送料200 = 3100 なら通る
        kept, _ = self._run([_item(price=2900, postage=200)])
        self.assertEqual(len(kept), 1)

    def test_unknown_postage_uses_penalty_for_total(self):
        # 価格2500 + 送料未定ペナルティ1000 = 3500 → 通る
        kept, _ = self._run([_item(price=2500, postage=None)])
        self.assertEqual(len(kept), 1)

    def test_ending_too_soon_excluded(self):
        kept, excluded = self._run([_item(minutes=30)])
        self.assertEqual(kept, [])
        self.assertIn("締切", excluded[0]["excluded_reason"])

    def test_ending_too_far_excluded(self):
        kept, excluded = self._run([_item(minutes=73 * 60)])
        self.assertEqual(kept, [])
        self.assertIn("終了まで", excluded[0]["excluded_reason"])

    def test_unknown_remaining_excluded(self):
        it = _item()
        it["minutes_remaining"] = None
        kept, excluded = self._run([it])
        self.assertEqual(kept, [])
        self.assertIn("残り時間不明", excluded[0]["excluded_reason"])

    def test_ng_word_excluded(self):
        kept, excluded = self._run([_item(title="ティファニー ﾚﾌﾟﾘｶ ネックレス")])
        self.assertEqual(kept, [])
        self.assertIn("NG語", excluded[0]["excluded_reason"])

    def test_history_duplicate_excluded(self):
        kept, excluded = self._run([_item(aid="dup1")], history=_History(["dup1"]))
        self.assertEqual(kept, [])
        self.assertIn("重複", excluded[0]["excluded_reason"])

    def test_same_auction_id_deduped_within_run(self):
        kept, excluded = self._run([_item(aid="same"), _item(aid="same")])
        self.assertEqual(len(kept), 1)
        self.assertEqual(len(excluded), 1)
        self.assertIn("重複", excluded[0]["excluded_reason"])

    def test_kept_sorted_by_ending_soonest(self):
        items = [_item(aid="a", minutes=1440), _item(aid="b", minutes=120)]
        kept, _ = self._run(items)
        self.assertEqual([c["auction_id"] for c in kept], ["b", "a"])

    def test_max_candidates_limit(self):
        p = build_profile("accessory", {"maxCandidates": 2})
        items = [_item(aid="a%d" % i, minutes=100 + i) for i in range(5)]
        kept, excluded = ybr_filter.prefilter(items, p, now=NOW)
        self.assertEqual(len(kept), 2)
        self.assertTrue(any("候補数上限" in e["excluded_reason"] for e in excluded))


if __name__ == "__main__":
    unittest.main()
