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


class _MercariHttpError(Exception):
    """mercapi が投げる HTTPStatusError 相当（response.status_code を持つ）。"""

    def __init__(self, status):
        class _R:
            status_code = status
        self.response = _R()
        super().__init__("HTTP {}".format(status))


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

    def test_excluded_breakdown_is_grouped_by_kind(self):
        """実走で発見: 金額入りの理由をそのまま集計するとキーが散って0件の理由が読めない。"""
        with tempfile.TemporaryDirectory() as tmp:
            argv = ["--category", "accessory", "--keywords", "ティファニー",
                    "--out", os.path.join(tmp, "o"), "--state-dir", os.path.join(tmp, "s"),
                    "--no-cache", "--min-total", "140000", "--max-total", "150000"]
            res = cli.run(argv, deps=_deps())
            reasons = res["payload"]["meta"]["excluded_reasons"]
            self.assertIn("総額レンジ外(140,000〜150,000円)", reasons)
            self.assertGreaterEqual(reasons["総額レンジ外(140,000〜150,000円)"], 2)
            # 1件ごとの詳細（金額入り）は candidates.json に残る
            with open(res["paths"]["json"], encoding="utf-8") as f:
                data = json.load(f)
            detailed = [e["excluded_reason"] for e in data["excluded"]
                        if (e.get("excluded_kind") or "").startswith("総額レンジ外")]
            self.assertTrue(any("円 / 140,000" in r for r in detailed), detailed[:3])

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

    def test_unusable_margin_is_surfaced_not_silent(self):
        """実走で発見: --margin 90 は手数料10%と合計100%で使えず30%に戻る。黙って戻さない。"""
        with tempfile.TemporaryDirectory() as tmp:
            res = cli.run(_argv(tmp, "--margin", "90"), deps=_deps())
            meta = res["payload"]["meta"]
            self.assertEqual(meta["margin_pct"], 30.0)
            self.assertEqual(meta["margin_pct_requested"], 90.0)
            self.assertTrue(any("利益率" in n for n in meta["notes"]), meta["notes"])
            with open(res["paths"]["md"], encoding="utf-8") as f:
                self.assertIn("指定した利益率", f.read())

    def test_usable_margin_has_no_note(self):
        with tempfile.TemporaryDirectory() as tmp:
            res = cli.run(_argv(tmp, "--margin", "40"), deps=_deps())
            meta = res["payload"]["meta"]
            self.assertEqual(meta["margin_pct"], 40.0)
            self.assertFalse(any("指定した利益率" in n for n in meta["notes"]))

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


def _md(res):
    with open(res["paths"]["md"], encoding="utf-8") as f:
        return f.read()


class TestBlockedPropagation(unittest.TestCase):
    """C2 / C4: ブロック・中断は全カテゴリを止めて exit3＋⚠️バナー（ネット不使用）。"""

    def test_yahoo_403_on_second_page_stops_everything(self):
        calls = []

        def fetcher(url):
            calls.append(url)
            if len(calls) == 1:
                return _fixture_html()
            raise urllib.error.HTTPError(url, 403, "Forbidden", None, None)

        with tempfile.TemporaryDirectory() as tmp:
            argv = ["--category", "both", "--pages", "3",
                    "--out", os.path.join(tmp, "o"), "--state-dir", os.path.join(tmp, "s"),
                    "--no-cache", "--no-history", "--max-yahoo-requests", "20"]
            res = cli.run(argv, deps=_deps(fetcher=fetcher))
            self.assertEqual(res["exit_code"], 3)
            self.assertIn("403", res["payload"]["meta"]["degraded"])
            self.assertEqual(len(calls), 2, "403のあとも取得を続けている")
            md = _md(res)
            self.assertIn("⚠️", md)
            self.assertIn("403", md)

    def test_mercari_403_gives_exit3_even_with_successes(self):
        """C4: 成功した相場があっても、ブロックで中断したら exit3。"""
        class AD:
            def __init__(self):
                self.calls = 0

            def search(self, query):
                self.calls += 1
                if self.calls == 1:
                    return [_Item(60000 + i * 100) for i in range(12)]
                raise _MercariHttpError(403)

        with tempfile.TemporaryDirectory() as tmp:
            res = cli.run(_argv(tmp, "--no-history"), deps=_deps(adapter=AD()))
            self.assertEqual(res["exit_code"], 3)
            self.assertIn("403", res["payload"]["meta"]["degraded"])
            self.assertEqual(res["payload"]["meta"]["mercari_blocked_status"], 403)
            md = _md(res)
            self.assertIn("⚠️", md)

    def test_mercari_abort_gives_exit3_even_with_successes(self):
        class AD:
            def __init__(self):
                self.calls = 0

            def search(self, query):
                self.calls += 1
                if self.calls == 1:
                    return [_Item(60000 + i * 100) for i in range(12)]
                raise RuntimeError("mercapi down")

        with tempfile.TemporaryDirectory() as tmp:
            res = cli.run(_argv(tmp, "--no-history"), deps=_deps(adapter=AD()))
            self.assertEqual(res["exit_code"], 3)
            self.assertTrue(res["payload"]["meta"]["mercari_aborted"])
            self.assertIn("連続", res["payload"]["meta"]["degraded"])
            self.assertIn("⚠️", _md(res))

    def test_yahoo_three_consecutive_http_failures_stop(self):
        """m22: 検索語単位で9回続かせない。"""
        calls = []

        def fetcher(url):
            calls.append(url)
            raise OSError("connection reset")

        with tempfile.TemporaryDirectory() as tmp:
            argv = ["--category", "apparel",
                    "--out", os.path.join(tmp, "o"), "--state-dir", os.path.join(tmp, "s"),
                    "--no-cache", "--no-history", "--max-yahoo-requests", "20"]
            res = cli.run(argv, deps=_deps(fetcher=fetcher))
            self.assertEqual(res["exit_code"], 3)
            self.assertEqual(len(calls), 3, "3回連続失敗で止まっていない")


class TestUnqueriedVsZeroHits(unittest.TestCase):
    """C3: 「売切0件」と「未照会」を混ぜない。"""

    def test_zero_hits_kind(self):
        with tempfile.TemporaryDirectory() as tmp:
            res = cli.run(_argv(tmp, "--no-history"),
                          deps=_deps(adapter=_MercariAdapter(empty=True)))
            kinds = res["payload"]["meta"]["excluded_reasons"]
            self.assertIn("相場不明(メルカリ売切0件)", kinds)

    def test_budget_exhausted_kind_is_not_zero_hits(self):
        with tempfile.TemporaryDirectory() as tmp:
            res = cli.run(_argv(tmp, "--no-history", "--max-mercari-requests", "1"),
                          deps=_deps(adapter=_MercariAdapter(empty=True)))
            kinds = res["payload"]["meta"]["excluded_reasons"]
            unqueried = [k for k in kinds if "未照会" in k]
            self.assertTrue(unqueried, kinds)
            for k in unqueried:
                self.assertNotIn("売切0件", k)

    def test_fetch_error_kind_is_not_zero_hits(self):
        class AD:
            def search(self, query):
                raise RuntimeError("mercapi unavailable")

        with tempfile.TemporaryDirectory() as tmp:
            res = cli.run(_argv(tmp, "--no-history"), deps=_deps(adapter=AD()))
            kinds = res["payload"]["meta"]["excluded_reasons"]
            self.assertFalse([k for k in kinds if "売切0件" in k], kinds)
            self.assertTrue([k for k in kinds if "未照会" in k], kinds)


class TestHistoryTiming(unittest.TestCase):
    """M9: exit3 の部分結果を「報告済み」にしない。"""

    def test_history_not_marked_on_exit3(self):
        class AD:
            def __init__(self):
                self.calls = 0

            def search(self, query):
                self.calls += 1
                if self.calls == 1:
                    return [_Item(60000 + i * 100) for i in range(12)]
                raise RuntimeError("mercapi down")

        with tempfile.TemporaryDirectory() as tmp:
            state = os.path.join(tmp, "s")
            argv = ["--category", "accessory", "--keywords", "ティファニー オープンハート",
                    "--out", os.path.join(tmp, "o1"), "--state-dir", state, "--no-cache"]
            res = cli.run(argv, deps=_deps(adapter=AD()))
            self.assertEqual(res["exit_code"], 3)
            self.assertFalse(os.path.exists(os.path.join(state, "history.json")))

    def test_history_marked_on_exit0(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = os.path.join(tmp, "s")
            argv = ["--category", "accessory", "--keywords", "ティファニー オープンハート",
                    "--out", os.path.join(tmp, "o1"), "--state-dir", state, "--no-cache"]
            res = cli.run(argv, deps=_deps())
            self.assertEqual(res["exit_code"], 0)
            self.assertTrue(os.path.exists(os.path.join(state, "history.json")))

    def test_history_marked_on_exit2(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = os.path.join(tmp, "s")
            argv = ["--category", "accessory", "--keywords", "ティファニー オープンハート",
                    "--out", os.path.join(tmp, "o1"), "--state-dir", state, "--no-cache"]
            res = cli.run(argv, deps=_deps(adapter=_MercariAdapter(empty=True)))
            self.assertEqual(res["exit_code"], 2)
            self.assertTrue(os.path.exists(os.path.join(state, "history.json")))


class TestUsageExitCode(unittest.TestCase):
    """M8: 引数エラーは exit1（「採用0件」の2と区別する）。"""

    def test_bad_margin_exits_1(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SystemExit) as cm:
                cli.run(_argv(tmp, "--margin", "0"), deps=_deps())
            self.assertEqual(cm.exception.code, cli.EXIT_USAGE)
            self.assertEqual(cm.exception.code, 1)

    def test_nan_margin_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SystemExit) as cm:
                cli.run(_argv(tmp, "--margin", "nan"), deps=_deps())
            self.assertEqual(cm.exception.code, 1)

    def test_inf_margin_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SystemExit) as cm:
                cli.run(_argv(tmp, "--margin", "inf"), deps=_deps())
            self.assertEqual(cm.exception.code, 1)

    def test_unknown_category_exits_1(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SystemExit) as cm:
                cli.run(_argv(tmp, "--category", "shoes"), deps=_deps())
            self.assertEqual(cm.exception.code, 1)

    def test_unknown_command_returns_1(self):
        self.assertEqual(cli.main(["nope"]), 1)
        self.assertEqual(cli.main([]), 1)


class TestDailyLimitIsDegraded(unittest.TestCase):
    """Codex#3: 日次上限での途中打ち切りは部分結果なので exit3。"""

    def test_daily_limit_mid_run_sets_degraded(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = os.path.join(tmp, "s")
            base = ["--category", "accessory", "--state-dir", state,
                    "--no-cache", "--no-history", "--max-yahoo-per-day", "1",
                    "--max-yahoo-requests", "5"]
            cli.run(base + ["--out", os.path.join(tmp, "o1")], deps=_deps())
            res = cli.run(base + ["--out", os.path.join(tmp, "o2")], deps=_deps())
            self.assertEqual(res["exit_code"], 3)
            self.assertIn("日次上限", res["payload"]["meta"]["degraded"])
            self.assertIn("⚠️", _md(res))


class TestMetaDisclosure(unittest.TestCase):
    """m19 / m17 / M16: 見出しの数値を実態に合わせる。"""

    def test_planned_vs_executed_keywords(self):
        with tempfile.TemporaryDirectory() as tmp:
            argv = ["--category", "apparel",
                    "--out", os.path.join(tmp, "o"), "--state-dir", os.path.join(tmp, "s"),
                    "--no-cache", "--no-history", "--max-yahoo-requests", "2"]
            res = cli.run(argv, deps=_deps())
            meta = res["payload"]["meta"]
            self.assertGreater(meta["keywords_planned"], meta["keywords_executed"])
            self.assertEqual(meta["keywords_executed"], 2)
            self.assertIn("予定{}語／実行{}語".format(
                meta["keywords_planned"], meta["keywords_executed"]), _md(res))

    def test_effective_margin_shown_near_heading(self):
        with tempfile.TemporaryDirectory() as tmp:
            res = cli.run(_argv(tmp, "--no-history", "--margin", "90"), deps=_deps())
            md = _md(res)
            heading_block = md.split("## ")[0]
            self.assertIn("指定の90%は使用不可", heading_block)
            self.assertIn("**30%**", heading_block)

    def test_non_consecutive_yahoo_failures_are_disclosed(self):
        calls = []

        def flaky(url):
            calls.append(url)
            if len(calls) % 2 == 1:
                raise OSError("flaky")
            return _fixture_html()

        with tempfile.TemporaryDirectory() as tmp:
            argv = ["--category", "apparel",
                    "--out", os.path.join(tmp, "o"), "--state-dir", os.path.join(tmp, "s"),
                    "--no-cache", "--no-history", "--max-yahoo-requests", "6"]
            res = cli.run(argv, deps=_deps(fetcher=flaky))
            self.assertGreater(res["payload"]["meta"]["yahoo_failures"], 0)
            self.assertIn("ヤフオク取得に失敗した検索語", _md(res))

    def test_budget_warning_surfaces_in_md(self):
        with tempfile.TemporaryDirectory() as tmp:
            res = cli.run(_argv(tmp, "--no-history"), deps=_deps())
            res["payload"]["meta"]["warnings"] = ["日次予算の保存に失敗（テスト）"]
            from ybr import report
            self.assertIn("日次予算の保存に失敗", report.render_markdown(res["payload"]))


class TestBrandRoundRobin(unittest.TestCase):
    """m25: 上限が小さいとき先頭ブランドに偏らせない。"""

    def test_jobs_rotate_across_brands(self):
        args = cli.build_run_parser().parse_args(["--category", "accessory"])
        jobs = cli.resolve_jobs(args)
        brands = [cli._brand_of(k) for _c, k in jobs[:4]]
        self.assertEqual(len(set(brands)), 4, brands)

    def test_both_rotates_across_categories_and_brands(self):
        args = cli.build_run_parser().parse_args(["--category", "both"])
        jobs = cli.resolve_jobs(args)
        self.assertEqual(jobs[0][0], "accessory")
        self.assertEqual(jobs[1][0], "apparel")
        self.assertNotEqual(cli._brand_of(jobs[0][1]), cli._brand_of(jobs[2][1]))

    def test_executed_keywords_cover_multiple_brands(self):
        with tempfile.TemporaryDirectory() as tmp:
            argv = ["--category", "accessory",
                    "--out", os.path.join(tmp, "o"), "--state-dir", os.path.join(tmp, "s"),
                    "--no-cache", "--no-history", "--max-yahoo-requests", "4"]
            res = cli.run(argv, deps=_deps())
            used = res["payload"]["meta"]["keywords"][:4]
            self.assertGreater(len({cli._brand_of(k) for k in used}), 1, used)


class TestUnknownCategoryKeyword(unittest.TestCase):
    """M11: カテゴリ不明の語は送料もNG語も保守側。"""

    def test_unknown_keyword_uses_union_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            argv = ["--category", "both", "--keywords", "謎ブランド 謎アイテム",
                    "--out", os.path.join(tmp, "o"), "--state-dir", os.path.join(tmp, "s"),
                    "--no-cache", "--no-history"]
            res = cli.run(argv, deps=_deps())
            self.assertIn("unknown", res["payload"]["meta"]["categories"])
            adopted = res["payload"]["adopted"]
            if adopted:
                self.assertEqual(adopted[0]["profit"]["ship_out"], 850)


class TestExitCodeConstants(unittest.TestCase):
    def test_constants(self):
        self.assertEqual(cli.EXIT_OK, 0)
        self.assertEqual(cli.EXIT_NO_CANDIDATES, 2)
        self.assertEqual(cli.EXIT_FETCH_FAILED, 3)

    def test_blocked_error_is_exported(self):
        self.assertTrue(issubclass(yahoo.BlockedError, Exception))


if __name__ == "__main__":
    unittest.main()
