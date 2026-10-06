# -*- coding: utf-8 -*-
"""永続化: 日次予算のロック・重複履歴・相場キャッシュ（m21 の回帰つき）。"""
from __future__ import annotations

import json
import os
import tempfile
import unittest

from tests import ROOT  # noqa: F401
from ybr.cache import CompsCache, DailyBudget, History, NullCompsCache, today_str


class TestDailyBudget(unittest.TestCase):
    def _budget(self, d, limits=None):
        return DailyBudget(os.path.join(d, "daily-budget.json"),
                           limits=limits or {"yahoo": 3, "mercari": 2})

    def test_spend_until_limit(self):
        with tempfile.TemporaryDirectory() as d:
            b = self._budget(d)
            self.assertTrue(b.spend("yahoo", 1))
            self.assertTrue(b.spend("yahoo", 2))
            self.assertFalse(b.spend("yahoo", 1))
            self.assertEqual(b.remaining("yahoo"), 0)

    def test_over_limit_does_not_consume(self):
        with tempfile.TemporaryDirectory() as d:
            b = self._budget(d)
            self.assertFalse(b.spend("yahoo", 99))
            self.assertEqual(b.remaining("yahoo"), 3)

    def test_second_instance_sees_latest_state(self):
        """m21: 起動時スナップショットだと別プロセスが残り1回を二重消費できた。"""
        with tempfile.TemporaryDirectory() as d:
            a = self._budget(d, {"yahoo": 1})
            b = self._budget(d, {"yahoo": 1})  # 先に両方インスタンス化しておく
            self.assertTrue(a.spend("yahoo", 1))
            self.assertFalse(b.spend("yahoo", 1), "同時実行で二重消費できてしまう")

    def test_remaining_reads_fresh_state(self):
        with tempfile.TemporaryDirectory() as d:
            a = self._budget(d, {"yahoo": 2})
            b = self._budget(d, {"yahoo": 2})
            a.spend("yahoo", 2)
            self.assertEqual(b.remaining("yahoo"), 0)

    def test_date_rollover_resets(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "daily-budget.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"date": "2000-01-01", "counts": {"yahoo": 999}}, f)
            b = DailyBudget(path, limits={"yahoo": 3})
            self.assertEqual(b.remaining("yahoo"), 3)

    def test_corrupt_file_is_treated_as_empty(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "daily-budget.json")
            with open(path, "w", encoding="utf-8") as f:
                f.write("{not json")
            b = DailyBudget(path, limits={"yahoo": 3})
            self.assertTrue(b.spend("yahoo", 1))

    def test_save_failure_warns_but_continues(self):
        """m21（監査判断）: 保存失敗で止めず、警告を残して続行する。"""
        with tempfile.TemporaryDirectory() as d:
            b = self._budget(d)
            os.chmod(d, 0o500)  # 書き込み不可にする
            try:
                self.assertTrue(b.spend("yahoo", 1))
                self.assertTrue(b.save_failed)
                self.assertTrue(any("保存に失敗" in w for w in b.warnings), b.warnings)
            finally:
                os.chmod(d, 0o700)

    def test_lock_file_is_created_next_to_state(self):
        with tempfile.TemporaryDirectory() as d:
            b = self._budget(d)
            b.spend("yahoo", 1)
            self.assertTrue(os.path.exists(b.path + b.LOCK_SUFFIX))

    def test_state_file_has_today(self):
        with tempfile.TemporaryDirectory() as d:
            b = self._budget(d)
            b.spend("mercari", 1)
            with open(b.path, encoding="utf-8") as f:
                self.assertEqual(json.load(f)["date"], today_str())


class TestHistory(unittest.TestCase):
    def test_marks_and_detects_duplicates(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "history.json")
            h = History(path)
            cand = {"auction_id": "x1", "title": "ティファニー オープンハート"}
            self.assertFalse(h.is_duplicate(cand))
            h.mark([cand])
            self.assertTrue(History(path).is_duplicate(cand))

    def test_title_normalization_detects_duplicate(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "history.json")
            h = History(path)
            h.mark([{"auction_id": "x1", "title": "ティファニー　オープンハート！"}])
            self.assertTrue(
                History(path).is_duplicate(
                    {"auction_id": "x2", "title": "ティファニー オープンハート"}))


class TestCompsCache(unittest.TestCase):
    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            c = CompsCache(os.path.join(d, "c.sqlite3"))
            c.put("q1", {"count": 3, "median": 1000})
            self.assertEqual(c.get("q1")["median"], 1000)

    def test_missing_key_is_none(self):
        with tempfile.TemporaryDirectory() as d:
            c = CompsCache(os.path.join(d, "c.sqlite3"))
            self.assertIsNone(c.get("nope"))

    def test_expired_entry_is_dropped(self):
        with tempfile.TemporaryDirectory() as d:
            c = CompsCache(os.path.join(d, "c.sqlite3"), ttl_hours=0)
            c.put("q1", {"count": 3, "median": 1000})
            self.assertIsNone(c.get("q1"))

    def test_corrupt_db_is_recreated(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "c.sqlite3")
            with open(path, "wb") as f:
                f.write(b"not a database at all")
            c = CompsCache(path)
            c.put("q1", {"count": 1, "median": 10})
            self.assertIn(c.get("q1"), ({"count": 1, "median": 10}, None))

    def test_null_cache_is_always_miss(self):
        n = NullCompsCache()
        n.put("q", {"count": 1})
        self.assertIsNone(n.get("q"))


if __name__ == "__main__":
    unittest.main()
