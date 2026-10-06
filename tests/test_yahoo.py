# -*- coding: utf-8 -*-
"""ヤフオク検索HTMLの解析（フィクスチャ使用・ネットワーク不使用）。"""
from __future__ import annotations

import os
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone

from tests import FIXTURES, ROOT  # noqa: F401
from ybr import yahoo

JST = timezone(timedelta(hours=9))
NOW = datetime(2026, 10, 6, 12, 0, 0, tzinfo=JST)


def _fixture_html():
    with open(os.path.join(FIXTURES, "yahoo_search.fixture.html"), encoding="utf-8") as f:
        return f.read()


class TestParseItems(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.items = yahoo.parse_items(_fixture_html(), "テスト", now=NOW)
        cls.by_id = {i["auction_id"]: i for i in cls.items}

    def test_skips_items_without_price(self):
        self.assertNotIn("g7778889990", self.by_id)

    def test_rejects_invalid_auction_id(self):
        for aid in self.by_id:
            self.assertRegex(aid, r"^[A-Za-z0-9]+$")

    def test_count(self):
        # 12ブロックのうち「価格なし」「不正ID」の2件を除いた10件
        self.assertEqual(len(self.items), 10)

    def test_current_price_preferred_over_buynow(self):
        it = self.by_id["c3334445556"]
        self.assertEqual(it["price"], 30000)
        self.assertEqual(it["buynow"], 45000)

    def test_buynow_used_when_no_current(self):
        it = self.by_id["b2223334445"]
        self.assertEqual(it["price"], 25800)

    def test_free_shipping_is_zero(self):
        self.assertEqual(self.by_id["x1234567890"]["postage"], 0)

    def test_numeric_postage(self):
        self.assertEqual(self.by_id["b2223334445"]["postage"], 700)
        self.assertEqual(self.by_id["h8889990001"]["postage"], 1200)

    def test_unknown_postage_is_none(self):
        self.assertIsNone(self.by_id["c3334445556"]["postage"])

    def test_minutes_remaining(self):
        self.assertEqual(self.by_id["x1234567890"]["minutes_remaining"], 1440)
        self.assertEqual(self.by_id["b2223334445"]["minutes_remaining"], 720)
        self.assertEqual(self.by_id["c3334445556"]["minutes_remaining"], 3 * 1440 + 720)
        self.assertEqual(self.by_id["e5556667778"]["minutes_remaining"], 30)
        self.assertEqual(self.by_id["h8889990001"]["minutes_remaining"], 1440 + 360)

    def test_end_at_is_jst_iso(self):
        it = self.by_id["x1234567890"]
        self.assertEqual(it["end_at"], (NOW + timedelta(minutes=1440)).isoformat())

    def test_store_detection_by_numeric_id(self):
        self.assertTrue(self.by_id["1234567890"]["is_store"])
        self.assertFalse(self.by_id["x1234567890"]["is_store"])

    def test_bids(self):
        self.assertEqual(self.by_id["x1234567890"]["bids"], 3)
        self.assertEqual(self.by_id["b2223334445"]["bids"], 0)

    def test_url_built_from_id(self):
        self.assertEqual(
            self.by_id["x1234567890"]["url"],
            "https://page.auctions.yahoo.co.jp/jp/auction/x1234567890",
        )

    def test_control_characters_removed_from_title(self):
        t = self.by_id["j0001112223"]["title"]
        self.assertNotIn("\n", t)
        self.assertNotIn("\r", t)
        self.assertIn("カナダグース", t)

    def test_image_url_extracted_or_none(self):
        self.assertEqual(
            self.by_id["x1234567890"]["image"], "https://example.invalid/img/a.jpg"
        )
        self.assertIsNone(self.by_id["b2223334445"]["image"])


class TestSanitizeTitle(unittest.TestCase):
    def test_strips_control_chars_and_collapses_spaces(self):
        self.assertEqual(yahoo.sanitize_title("a\nb\t c d"), "a b c d")

    def test_none_is_empty(self):
        self.assertEqual(yahoo.sanitize_title(None), "")


class TestBuildSearchUrl(unittest.TestCase):
    def test_contains_keyword_and_paging(self):
        url = yahoo.build_search_url("バーバリー トレンチ", start=1, per=100)
        self.assertTrue(url.startswith("https://auctions.yahoo.co.jp/search/search?"))
        self.assertIn("n=100", url)
        self.assertIn("b=1", url)
        self.assertIn("exflg=1", url)


class TestSearchKeyword(unittest.TestCase):
    def test_dedupes_repeated_auction_ids(self):
        html = _fixture_html()
        doubled = html + html
        items, warning = yahoo.search_keyword("テスト", fetcher=lambda _u: doubled)
        self.assertEqual(len(items), 10)
        self.assertIsNone(warning)

    def test_structure_change_warning_when_zero_parsed(self):
        items, warning = yahoo.search_keyword(
            "テスト", fetcher=lambda _u: "<html>no products</html>")
        self.assertEqual(items, [])
        self.assertIsNotNone(warning)
        self.assertIn("構造変化", warning)

    def test_http_403_raises_blocked_error(self):
        def boom(_url):
            raise urllib.error.HTTPError(_url, 403, "Forbidden", None, None)

        with self.assertRaises(yahoo.BlockedError):
            yahoo.search_keyword("テスト", fetcher=boom)

    def test_http_429_raises_blocked_error(self):
        def boom(_url):
            raise urllib.error.HTTPError(_url, 429, "Too Many Requests", None, None)

        with self.assertRaises(yahoo.BlockedError):
            yahoo.search_keyword("テスト", fetcher=boom)

    def test_other_error_propagates(self):
        def boom(_url):
            raise OSError("connection reset")

        with self.assertRaises(OSError):
            yahoo.search_keyword("テスト", fetcher=boom)


if __name__ == "__main__":
    unittest.main()
