# -*- coding: utf-8 -*-
"""レポート出力: URL単独行・制御文字無害化・CSV・candidates.json。"""
from __future__ import annotations

import csv
import io
import json
import os
import re
import tempfile
import unittest

from tests import ROOT  # noqa: F401
from ybr import report

URL_RE = re.compile(r"https?://[^\s)\]」』、。]+")
CTRL_RE = re.compile("[\\x00-\\x08\\x0b\\x0c\\x0e-\\x1f\\x7f\\u2028\\u2029]")


def _adopted(aid="x1", title="ティファニー オープンハート ネックレス"):
    return {
        "auction_id": aid,
        "category": "accessory",
        "title": title,
        "url": "https://page.auctions.yahoo.co.jp/jp/auction/" + aid,
        "price": 5000,
        "postage": 0,
        "total_cost": 5000,
        "bids": 2,
        "is_store": False,
        "end_at": "2026-10-07T12:00:00+09:00",
        "minutes_remaining": 1440,
        "keyword": "ティファニー オープンハート",
        "comps": {"query": "ティファニー オープンハート ネックレス", "count": 12,
                  "median": 10000, "p25": 9000, "p75": 11000, "min": 8000,
                  "max": 13000, "mean": 10100, "fallback_level": 0, "cache_hit": False},
        "mercari_url": "https://jp.mercari.com/search?keyword=x&status=sold_out",
        "profit": {"ok": True, "reason": None, "expected_sale": 10000, "confidence": "高",
                   "comps_count": 12, "fee": 1000, "ship_out": 300, "ship_in": 0,
                   "net": 8700, "target_profit": 3000, "suggested_bid": 5700,
                   "break_even_bid": 8700, "profit_at_bid": 3000, "margin_pct": 30.0},
        "excluded_reason": None,
    }


def _payload(adopted=None, excluded=None, notes=None):
    return {
        "meta": {
            "generated_at": "2026-10-06T12:00:00+09:00",
            "categories": ["accessory"],
            "keywords": ["ティファニー オープンハート"],
            "margin_pct": 30.0,
            "top": 20,
            "yahoo_requests": 1,
            "yahoo_items": 10,
            "prefilter_kept": 1,
            "mercari_requests": 1,
            "adopted_count": len(adopted or []),
            "elapsed_sec": 12.3,
            "exit_code": 0,
            "degraded": None,
            "notes": notes or [],
            "excluded_reasons": {},
        },
        "adopted": adopted if adopted is not None else [_adopted()],
        "excluded": excluded or [],
    }


class TestSanitize(unittest.TestCase):
    def test_removes_control_chars(self):
        self.assertEqual(report.sanitize_text("a\nb c"), "a b c")

    def test_none_is_empty(self):
        self.assertEqual(report.sanitize_text(None), "")

    def test_truncate_adds_ellipsis(self):
        self.assertTrue(report.truncate_title("あ" * 100, limit=10).endswith("…"))


class TestMarkdown(unittest.TestCase):
    def setUp(self):
        self.md = report.render_markdown(_payload())

    def test_every_url_is_on_its_own_line(self):
        for line in self.md.split("\n"):
            urls = URL_RE.findall(line)
            if not urls:
                continue
            self.assertEqual(len(urls), 1, "1行に複数URL: %r" % line)
            self.assertEqual(line.strip(), urls[0], "URLが単独行でない: %r" % line)

    def test_no_control_characters(self):
        self.assertEqual(CTRL_RE.findall(self.md), [])

    def test_has_yahoo_and_mercari_urls(self):
        self.assertIn("https://page.auctions.yahoo.co.jp/jp/auction/x1", self.md)
        self.assertIn("https://jp.mercari.com/search?", self.md)

    def test_numbers_are_labeled_as_estimates(self):
        self.assertIn("目安", self.md)

    def test_table_columns_present(self):
        for col in ("ヤフオクURL", "現在価格", "メルカリ予想売値", "入札提案価格", "注意"):
            self.assertIn(col, self.md)

    def test_zero_adopted_is_reported_honestly(self):
        md = report.render_markdown(_payload(adopted=[]))
        self.assertIn("採用0件", md)
        self.assertNotIn("https://page.auctions.yahoo.co.jp/jp/auction/x1", md)

    def test_title_with_fake_url_is_stripped(self):
        """タイトルに混入した偽URLは除去する（偽リンク行の注入を防ぐ）。"""
        bad = _adopted(title="カナダグース\nhttps://evil.invalid/fake")
        md = report.render_markdown(_payload(adopted=[bad]))
        self.assertNotIn("evil.invalid", md)
        for line in md.split("\n"):
            urls = URL_RE.findall(line)
            if urls:
                self.assertEqual(line.strip(), urls[0], "URLが単独行でない: %r" % line)

    def test_loose_match_warning_for_large_n(self):
        """実走で発見: メルカリは関連度検索なので n が大きいと別物が混ざる。"""
        c = _adopted()
        c["comps"]["count"] = 111
        c["profit"]["comps_count"] = 111
        md = report.render_markdown(_payload(adopted=[c]))
        self.assertIn("関連度検索で別物混入の可能性", md)

    def test_no_loose_match_warning_for_small_n(self):
        md = report.render_markdown(_payload())
        self.assertNotIn("関連度検索で別物混入の可能性", md)

    def test_degraded_banner_is_shown(self):
        p = _payload()
        p["meta"]["degraded"] = "ヤフオク取得がブロックされました(HTTP 403)"
        md = report.render_markdown(p)
        self.assertIn("403", md)


class TestCsv(unittest.TestCase):
    def test_header_and_rows(self):
        text = report.render_csv(_payload())
        rows = list(csv.reader(io.StringIO(text)))
        self.assertEqual(rows[0][0], "順位")
        self.assertIn("入札提案価格", rows[0])
        self.assertEqual(len(rows), 2)

    def test_csv_fields_have_no_newlines(self):
        bad = _adopted(title="a\nb")
        text = report.render_csv(_payload(adopted=[bad]))
        rows = list(csv.reader(io.StringIO(text)))
        for cell in rows[1]:
            self.assertNotIn("\n", cell)


class TestUrlStripping(unittest.TestCase):
    """m20 / Codex#14: 大文字スキームの偽URLも除去する。"""

    def test_uppercase_scheme_is_stripped(self):
        bad = _adopted(title="カナダグース HTTPS://EVIL.INVALID/fake")
        md = report.render_markdown(_payload(adopted=[bad]))
        self.assertNotIn("EVIL.INVALID", md)
        self.assertNotIn("evil.invalid", md.lower())

    def test_mixed_case_scheme_is_stripped(self):
        self.assertNotIn("evil", report.safe_title("商品名 HtTpS://evil.test/x").lower())

    def test_ftp_scheme_is_stripped(self):
        self.assertNotIn("evil", report.safe_title("商品名 ftp://evil.test/x").lower())

    def test_normal_title_is_kept(self):
        self.assertEqual(report.safe_title("ティファニー ネックレス"), "ティファニー ネックレス")


class TestCsvInjection(unittest.TestCase):
    """m20 / Codex#13: CSVの数式インジェクションを無害化する。"""

    def test_formula_title_is_neutralized(self):
        bad = _adopted(title="=1+1")
        text = report.render_csv(_payload(adopted=[bad]))
        rows = list(csv.reader(io.StringIO(text)))
        title_idx = rows[0].index("商品名")
        self.assertTrue(rows[1][title_idx].startswith("'="), rows[1][title_idx])

    def test_all_lead_chars_are_neutralized(self):
        for lead in ("=", "+", "-", "@"):
            bad = _adopted(title=lead + "cmd")
            rows = list(csv.reader(io.StringIO(report.render_csv(_payload(adopted=[bad])))))
            idx = rows[0].index("商品名")
            self.assertTrue(rows[1][idx].startswith("'" + lead), rows[1][idx])

    def test_numbers_are_not_quoted(self):
        rows = list(csv.reader(io.StringIO(report.render_csv(_payload()))))
        idx = rows[0].index("入札提案価格")
        self.assertEqual(rows[1][idx], "5700")

    def test_normal_title_is_untouched(self):
        rows = list(csv.reader(io.StringIO(report.render_csv(_payload()))))
        idx = rows[0].index("商品名")
        self.assertFalse(rows[1][idx].startswith("'"))


class TestTrimmedFromDisclosure(unittest.TestCase):
    """m18: 表示nがIQRトリム後であることを隠さない。"""

    def test_label_shows_original_count(self):
        c = _adopted()
        c["comps"]["count"] = 110
        c["comps"]["trimmed_from"] = 115
        self.assertEqual(report.comps_count_label(c), "110（元115・外れ値除外）")

    def test_label_without_trim(self):
        self.assertEqual(report.comps_count_label(_adopted()), "12")

    def test_md_shows_trim_info(self):
        c = _adopted()
        c["comps"]["count"] = 110
        c["comps"]["trimmed_from"] = 115
        c["profit"]["comps_count"] = 110
        md = report.render_markdown(_payload(adopted=[c]))
        self.assertIn("元115・外れ値除外", md)

    def test_csv_has_original_count_column(self):
        c = _adopted()
        c["comps"]["count"] = 110
        c["comps"]["trimmed_from"] = 115
        rows = list(csv.reader(io.StringIO(report.render_csv(_payload(adopted=[c])))))
        self.assertIn("相場元件数", rows[0])
        self.assertEqual(rows[1][rows[0].index("相場元件数")], "115")


class TestUnqueriedWording(unittest.TestCase):
    """C3: result.md でも「未照会」と「売切0件」を言い分ける。"""

    def test_zero_hits_wording(self):
        c = _adopted()
        c["comps"] = None
        c["comps_status"] = "zero_hits"
        c["profit"]["expected_sale"] = None
        md = report.render_markdown(_payload(adopted=[c]))
        self.assertIn("相場不明(売切0件)", md)

    def test_unqueried_wording(self):
        c = _adopted()
        c["comps"] = None
        c["comps_status"] = "budget_exhausted"
        c["comps_error"] = "メルカリ照会の予算切れで未照会"
        c["profit"]["expected_sale"] = None
        md = report.render_markdown(_payload(adopted=[c]))
        self.assertIn("未照会", md)
        self.assertNotIn("売切0件", md)


class TestWriteOutputs(unittest.TestCase):
    def test_writes_four_files(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "run1")
            paths = report.write_outputs(_payload(), out)
            for key in ("md", "csv", "json"):
                self.assertTrue(os.path.exists(paths[key]), key)
            with open(paths["json"], encoding="utf-8") as f:
                data = json.load(f)
            self.assertIn("adopted", data)
            self.assertIn("excluded", data)
            self.assertIn("meta", data)
            self.assertEqual(data["adopted"][0]["profit"]["suggested_bid"], 5700)

    def test_excluded_reasons_kept_in_json(self):
        ex = [{"auction_id": "z9", "title": "除外品", "price": 100,
               "url": "https://page.auctions.yahoo.co.jp/jp/auction/z9",
               "excluded_reason": "NG語に該当(コピー)"}]
        with tempfile.TemporaryDirectory() as d:
            paths = report.write_outputs(_payload(excluded=ex), os.path.join(d, "r"))
            with open(paths["json"], encoding="utf-8") as f:
                data = json.load(f)
            self.assertEqual(data["excluded"][0]["excluded_reason"], "NG語に該当(コピー)")


if __name__ == "__main__":
    unittest.main()
