# -*- coding: utf-8 -*-
"""一次フィルタ: NG語の正規化迂回・否定表現の救済・レンジ・残り時間・重複。"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from tests import ROOT  # noqa: F401
from ybr import filter as ybr_filter
from ybr.profiles import build_profile

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
        self.acc = build_profile("accessory", {})
        self.app = build_profile("apparel", {})

    def _acc(self, title):
        return ybr_filter.ng_hit(title, self.acc["ngWords"], self.acc["allowPhrases"])

    def _app(self, title):
        return ybr_filter.ng_hit(title, self.app["ngWords"], self.app["allowPhrases"])

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
