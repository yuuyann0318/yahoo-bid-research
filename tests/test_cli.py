# -*- coding: utf-8 -*-
"""CLI パイプラインのE2E（依存注入・ネットワーク不使用）と終了コード。"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
import urllib.error

from tests import FIXTURES, ROOT  # noqa: F401
from ybr import cli, yahoo


def _fixture_html():
    with open(os.path.join(FIXTURES, "yahoo_search.fixture.html"), encoding="utf-8") as f:
        return f.read()


class _Item:
    def __init__(self, price):
        self.price = price
        self.real_price = price
        self.item_type = "ITEM_TYPE_MERCARI"
        self.status = "ITEM_STATUS_SOLD_OUT"
        self.is_no_price = False


class _MercariAdapter:
    """どのクエリでも 12件・中央値 60,000円 を返す（必ず利益が出る相場）。"""

    def __init__(self, median=60000, fail=False, empty=False):
        self.median = median
        self.fail = fail
        self.empty = empty
        self.calls = []

    def search(self, query):
        self.calls.append(query)
        if self.fail:
            raise RuntimeError("mercapi unavailable (stub)")
        if self.empty:
            return []
        base = self.median
        return [_Item(base + (i - 6) * 100) for i in range(12)]


def _argv(tmp, *extra):
    base = [
        "--category", "accessory",
        "--keywords", "ティファニー オープンハート",
        "--out", os.path.join(tmp, "out"),
        "--state-dir", os.path.join(tmp, "state"),
        "--no-cache",
    ]
    return base + list(extra)


def _deps(fetcher=None, adapter=None):
    return {
        "fetcher": fetcher if fetcher is not None else (lambda _u: _fixture_html()),
        "mercari_adapter": adapter if adapter is not None else _MercariAdapter(),
        "throttle_fn": lambda _s: None,
    }


class TestPipeline(unittest.TestCase):
    def test_success_exit_0_and_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            res = cli.run(_argv(tmp), deps=_deps())
            self.assertEqual(res["exit_code"], 0, res["payload"]["meta"])
            md = res["paths"]["md"]
            self.assertTrue(os.path.exists(md))
            with open(md, encoding="utf-8") as f:
                text = f.read()
            self.assertIn("https://page.auctions.yahoo.co.jp/jp/auction/", text)
            self.assertIn("https://jp.mercari.com/search?", text)
            self.assertGreaterEqual(len(res["payload"]["adopted"]), 1)

    def test_candidates_json_has_reasons_and_query(self):
        with tempfile.TemporaryDirectory() as tmp:
            res = cli.run(_argv(tmp), deps=_deps())
            with open(res["paths"]["json"], encoding="utf-8") as f:
                data = json.load(f)
            self.assertTrue(data["excluded"])
            for e in data["excluded"]:
                self.assertTrue(e.get("excluded_reason"))
            top = data["adopted"][0]
            self.assertTrue(top["comps"]["query"])
            self.assertTrue(top["mercari_url"].startswith("https://jp.mercari.com/search?"))

    def test_zero_adopted_exit_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            res = cli.run(_argv(tmp), deps=_deps(adapter=_MercariAdapter(empty=True)))
            self.assertEqual(res["exit_code"], 2)
            self.assertEqual(res["payload"]["adopted"], [])
            self.assertTrue(os.path.exists(res["paths"]["md"]))

    def test_blocked_403_exit_3(self):
        def boom(_url):
            raise urllib.error.HTTPError(_url, 403, "Forbidden", None, None)

        with tempfile.TemporaryDirectory() as tmp:
            res = cli.run(_argv(tmp), deps=_deps(fetcher=boom))
            self.assertEqual(res["exit_code"], 3)
            self.assertIn("403", res["payload"]["meta"]["degraded"])

    def test_network_error_exit_3(self):
        def boom(_url):
            raise OSError("connection reset by peer")

        with tempfile.TemporaryDirectory() as tmp:
            res = cli.run(_argv(tmp), deps=_deps(fetcher=boom))
            self.assertEqual(res["exit_code"], 3)

    def test_mercari_total_failure_exit_3(self):
        with tempfile.TemporaryDirectory() as tmp:
            res = cli.run(_argv(tmp), deps=_deps(adapter=_MercariAdapter(fail=True)))
            self.assertEqual(res["exit_code"], 3)
            self.assertIn("相場", res["payload"]["meta"]["degraded"])

    def test_yahoo_request_budget_limits_calls(self):
        calls = []

        def counting(url):
            calls.append(url)
            return _fixture_html()

        with tempfile.TemporaryDirectory() as tmp:
            argv = [
                "--category", "accessory",
                "--keywords", "語1,語2,語3,語4",
                "--out", os.path.join(tmp, "out"),
                "--state-dir", os.path.join(tmp, "state"),
                "--no-cache",
                "--max-yahoo-requests", "2",
            ]
            cli.run(argv, deps=_deps(fetcher=counting))
            self.assertEqual(len(calls), 2)

    def test_mercari_request_budget_limits_calls(self):
        ad = _MercariAdapter(empty=True)  # 必ず縮退して3回/候補 呼ぶ
        with tempfile.TemporaryDirectory() as tmp:
            cli.run(_argv(tmp, "--max-mercari-requests", "2"), deps=_deps(adapter=ad))
            self.assertLessEqual(len(ad.calls), 2)

    def test_top_limits_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            res = cli.run(_argv(tmp, "--top", "1"), deps=_deps())
            self.assertLessEqual(len(res["payload"]["adopted"]), 1)

    def test_history_prevents_repeat_next_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = os.path.join(tmp, "state")
            a1 = ["--category", "accessory", "--keywords", "ティファニー オープンハート",
                  "--out", os.path.join(tmp, "o1"), "--state-dir", state, "--no-cache"]
            r1 = cli.run(a1, deps=_deps())
            self.assertGreaterEqual(len(r1["payload"]["adopted"]), 1)
            a2 = ["--category", "accessory", "--keywords", "ティファニー オープンハート",
                  "--out", os.path.join(tmp, "o2"), "--state-dir", state, "--no-cache"]
            r2 = cli.run(a2, deps=_deps())
            self.assertEqual(r2["payload"]["adopted"], [])
            self.assertEqual(r2["exit_code"], 2)

    def test_daily_budget_blocks_second_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = os.path.join(tmp, "state")
            argv = ["--category", "accessory", "--keywords", "ティファニー オープンハート",
                    "--out", os.path.join(tmp, "o1"), "--state-dir", state, "--no-cache",
                    "--max-yahoo-per-day", "1"]
            cli.run(argv, deps=_deps())
            argv2 = ["--category", "accessory", "--keywords", "ティファニー オープンハート",
                     "--out", os.path.join(tmp, "o2"), "--state-dir", state, "--no-cache",
                     "--max-yahoo-per-day", "1"]
            res = cli.run(argv2, deps=_deps())
            self.assertEqual(res["exit_code"], 3)
            self.assertIn("日次上限", res["payload"]["meta"]["degraded"])

    def test_apparel_profile_used_for_apparel_category(self):
        with tempfile.TemporaryDirectory() as tmp:
            argv = ["--category", "apparel", "--keywords", "モンクレール ダウン",
                    "--out", os.path.join(tmp, "o"), "--state-dir", os.path.join(tmp, "s"),
                    "--no-cache"]
            res = cli.run(argv, deps=_deps())
            adopted = res["payload"]["adopted"]
            self.assertTrue(adopted)
            self.assertEqual(adopted[0]["profit"]["ship_out"], 850)
            self.assertEqual(adopted[0]["category"], "apparel")

    def test_margin_argument_validated(self):
        with tempfile.TemporaryDirectory() as tmp:
            res = cli.run(_argv(tmp, "--margin", "50"), deps=_deps())
            self.assertEqual(res["payload"]["meta"]["margin_pct"], 50.0)

    def test_invalid_margin_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SystemExit):
                cli.run(_argv(tmp, "--margin", "0"), deps=_deps())

    def test_brands_filter_narrows_keywords(self):
        with tempfile.TemporaryDirectory() as tmp:
            argv = ["--category", "apparel", "--brands", "モンクレール",
                    "--out", os.path.join(tmp, "o"), "--state-dir", os.path.join(tmp, "s"),
                    "--no-cache", "--max-yahoo-requests", "3"]
            res = cli.run(argv, deps=_deps())
            kws = res["payload"]["meta"]["keywords"]
            self.assertTrue(kws)
            for k in kws:
                self.assertIn("モンクレール", k)

    def test_sorted_by_profit_desc(self):
        with tempfile.TemporaryDirectory() as tmp:
            res = cli.run(_argv(tmp), deps=_deps())
            profits = [c["profit"]["profit_at_bid"] for c in res["payload"]["adopted"]]
            self.assertEqual(profits, sorted(profits, reverse=True))

    def test_main_returns_exit_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            code = cli.main(["selftest", "--state-dir", os.path.join(tmp, "s")])
            self.assertEqual(code, 0)


class TestExitCodeConstants(unittest.TestCase):
    def test_constants(self):
        self.assertEqual(cli.EXIT_OK, 0)
        self.assertEqual(cli.EXIT_NO_CANDIDATES, 2)
        self.assertEqual(cli.EXIT_FETCH_FAILED, 3)

    def test_blocked_error_is_exported(self):
        self.assertTrue(issubclass(yahoo.BlockedError, Exception))


if __name__ == "__main__":
    unittest.main()
