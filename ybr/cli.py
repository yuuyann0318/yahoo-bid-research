# -*- coding: utf-8 -*-
"""ybr.cli: `bin/ybr run|selftest` の本体。

パイプライン:
  検索語ごとにヤフオク検索（1語1ページ・100件）
   → 一次フィルタ（総額レンジ / 残り時間 / NG語 / 7日重複）
   → メルカリ売切相場（上限内・間隔2.5秒・24hキャッシュ・クエリ段階縮退）
   → 利益計算（入札提案価格の逆算）
   → 採用判定 → 並べ替え（提案額での利益 降順 → 終了が近い順）
   → result.md / result.csv / candidates.json / run.log

終了コード: 0=成功 / 2=候補0件 / 3=取得失敗（403・429・通信断・日次上限・相場全滅）
AI の目利きはここでは行わない（このCLIを呼ぶ Claude 側が result.md を読んで行う）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

from . import filter as ybr_filter
from . import mercari as ybr_mercari
from . import profiles as ybr_profiles
from . import profit as ybr_profit
from . import report as ybr_report
from . import yahoo as ybr_yahoo
from .cache import CompsCache, DailyBudget, History, NullCompsCache, NullHistory

JST = timezone(timedelta(hours=9))

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_NO_CANDIDATES = 2
EXIT_FETCH_FAILED = 3

PROJECT_ROOT = ybr_profiles.PROJECT_ROOT
MAX_CONSECUTIVE_YAHOO_FAILURES = 3


# --- 引数 ---------------------------------------------------------------------
def _margin(value):
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError("--margin は数値で指定してください: {}".format(value))
    if v < ybr_profit.MIN_TARGET_MARGIN_PCT or v > ybr_profit.MAX_TARGET_MARGIN_PCT:
        raise argparse.ArgumentTypeError(
            "--margin は {:.0f}〜{:.0f} の範囲で指定してください: {}".format(
                ybr_profit.MIN_TARGET_MARGIN_PCT, ybr_profit.MAX_TARGET_MARGIN_PCT, value))
    return v


def _positive(value):
    try:
        v = int(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError("整数で指定してください: {}".format(value))
    if v <= 0:
        raise argparse.ArgumentTypeError("1以上で指定してください: {}".format(value))
    return v


def _csv_list(value):
    return [p.strip() for p in str(value or "").split(",") if p.strip()]


def build_run_parser():
    p = argparse.ArgumentParser(
        prog="ybr run",
        description="ヤフオクの入札候補をリストアップする（入札はしない）",
    )
    p.add_argument("--category", choices=("accessory", "apparel", "both"), default="both")
    p.add_argument("--keywords", type=_csv_list, default=None,
                   help="検索語をカンマ区切りで（指定時は keywords/*.txt を使わない）")
    p.add_argument("--brands", type=_csv_list, default=None,
                   help="keywords/*.txt の該当行だけに絞る（例: バーバリー,モンクレール）")
    p.add_argument("--margin", type=_margin, default=30.0, help="目標利益率%%（対売上・5〜90）")
    p.add_argument("--top", type=_positive, default=20)
    p.add_argument("--min-total", type=int, default=3000)
    p.add_argument("--max-total", type=int, default=150000)
    p.add_argument("--min-minutes", type=int, default=60)
    p.add_argument("--max-hours", type=float, default=72.0)
    p.add_argument("--pages", type=_positive, default=1)
    p.add_argument("--max-yahoo-requests", type=_positive, default=30)
    p.add_argument("--max-mercari-requests", type=_positive, default=60)
    p.add_argument("--max-yahoo-per-day", type=_positive, default=120)
    p.add_argument("--max-mercari-per-day", type=_positive, default=360)
    p.add_argument("--max-candidates", type=_positive, default=None,
                   help="メルカリ照会にかける候補の上限（既定はプロファイル値）")
    p.add_argument("--no-cache", action="store_true", help="24hキャッシュを使わない")
    p.add_argument("--no-history", action="store_true", help="7日重複除外を使わない")
    p.add_argument("--out", default=None, help="出力先（既定 reports/<YYYYmmdd-HHMMSS>/）")
    p.add_argument("--state-dir", default=None, help="キャッシュ・予算・履歴の保存先")
    return p


# --- 予算 ---------------------------------------------------------------------
class _Budget:
    """1実行の上限 + 日次上限の両方を見る会計。"""

    def __init__(self, kind, run_limit, daily):
        self.kind = kind
        self.run_limit = int(run_limit)
        self.daily = daily
        self.used = 0
        self.blocked_by = None

    def remaining(self):
        run_left = max(0, self.run_limit - self.used)
        day_left = self.daily.remaining(self.kind) if self.daily else run_left
        return min(run_left, day_left)

    def block_reason(self):
        """なぜ使えないのかを1行で返す（日次上限と1実行上限を区別する）。"""
        if self.daily is not None and self.daily.remaining(self.kind) <= 0:
            return "日次上限({}回)に到達".format(self.daily.limits.get(self.kind))
        if self.used >= self.run_limit:
            return "1実行の上限({}回)に到達".format(self.run_limit)
        return self.blocked_by or "上限に到達"

    def spend(self, n=1):
        n = int(n)
        if n <= 0:
            return True
        if self.used + n > self.run_limit:
            self.blocked_by = "1実行の上限({}回)".format(self.run_limit)
            return False
        if self.daily is not None and not self.daily.spend(self.kind, n):
            self.blocked_by = "日次上限({}回)".format(self.daily.limits.get(self.kind))
            return False
        self.used += n
        return True


class _Logger:
    """run.log（1行1JSON）。書けなくても処理は止めない。"""

    def __init__(self, path=None):
        self.path = path
        self.records = []

    def log(self, event, **fields):
        rec = {"ts": datetime.now(JST).isoformat(), "event": event}
        rec.update(fields)
        self.records.append(rec)
        if not self.path:
            return
        try:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError:
            pass


# --- 検索語の割り当て ---------------------------------------------------------
def _interleave(groups):
    """[(cat, [kw...]), ...] をカテゴリ交互に並べる（上限打ち切りで片方が消えないように）。"""
    out = []
    index = 0
    while True:
        added = False
        for cat, kws in groups:
            if index < len(kws):
                out.append((cat, kws[index]))
                added = True
        if not added:
            return out
        index += 1


def resolve_jobs(args, base_dir=None):
    """(category, keyword) のリストを決める。"""
    cats = ("accessory", "apparel") if args.category == "both" else (args.category,)
    if args.keywords:
        if args.category == "both":
            # カテゴリ未指定の明示検索語は語ごとに推定する（不明は apparel=保守的な送料）
            return [(ybr_profiles.resolve_category(k, base_dir), k) for k in args.keywords]
        return [(cats[0], k) for k in args.keywords]
    groups = []
    for cat in cats:
        groups.append((cat, ybr_profiles.load_keywords(
            cat, base_dir=base_dir, brands=args.brands)))
    return _interleave(groups)


def _profile_for(category, args, base_dir=None):
    overrides = {
        "targetMarginPct": float(args.margin),
        "minTotalYen": int(args.min_total),
        "maxTotalYen": int(args.max_total),
        "minMinutesRemaining": int(args.min_minutes),
        "maxHoursRemaining": float(args.max_hours),
    }
    if args.max_candidates:
        overrides["maxCandidates"] = int(args.max_candidates)
    return ybr_profiles.load_profile(category, base_dir=base_dir, overrides=overrides)


# --- 本体 ---------------------------------------------------------------------
def run(argv=None, deps=None, base_dir=None):
    """run サブコマンドの本体。dict（exit_code / payload / paths）を返す。"""
    args = build_run_parser().parse_args(argv)
    deps = deps or {}
    base_dir = base_dir or PROJECT_ROOT
    started = time.time()
    now = datetime.now(JST)

    out_dir = args.out or os.path.join(base_dir, "reports", now.strftime("%Y%m%d-%H%M%S"))
    state_dir = args.state_dir or os.path.join(base_dir, "state")
    os.makedirs(out_dir, exist_ok=True)
    os.makedirs(state_dir, exist_ok=True)
    logger = _Logger(os.path.join(out_dir, ybr_report.LOG_NAME))

    daily = DailyBudget(
        os.path.join(state_dir, "daily-budget.json"),
        limits={"yahoo": int(args.max_yahoo_per_day), "mercari": int(args.max_mercari_per_day)},
    )
    yahoo_budget = _Budget("yahoo", args.max_yahoo_requests, daily)
    mercari_budget = _Budget("mercari", args.max_mercari_requests, daily)

    cache = NullCompsCache() if args.no_cache else CompsCache(
        os.path.join(state_dir, "comps-cache.sqlite3"))
    history = NullHistory() if args.no_history else History(
        os.path.join(state_dir, "history.json"), dedup_window_days=7)

    jobs = resolve_jobs(args, base_dir=base_dir)
    profiles_by_cat = {}
    for cat, _kw in jobs:
        if cat not in profiles_by_cat:
            profiles_by_cat[cat] = _profile_for(cat, args, base_dir=base_dir)

    notes = []
    degraded = None
    stop_reason = None
    fetcher = deps.get("fetcher")
    throttle_fn = deps.get("throttle_fn")

    logger.log("run_start", category=args.category, keywords=len(jobs),
               margin=args.margin, top=args.top, out=out_dir)

    # ---- 1) ヤフオク検索 ----
    raw_by_cat = {}
    yahoo_items = 0
    attempted = 0
    failures = 0
    consecutive = 0
    for cat, keyword in jobs:
        if yahoo_budget.remaining() <= 0:
            stop_reason = yahoo_budget.block_reason()
            notes.append("ヤフオク検索を打ち切り: {}（残りの検索語{}語は未実行）".format(
                stop_reason, len(jobs) - attempted))
            break
        if not yahoo_budget.spend(1):
            stop_reason = yahoo_budget.block_reason()
            notes.append("ヤフオク検索を打ち切り: {}".format(stop_reason))
            break
        attempted += 1
        try:
            items, warning = ybr_yahoo.search_keyword(
                keyword, pages=args.pages, fetcher=fetcher,
                spend=yahoo_budget.spend, now=now)
        except ybr_yahoo.BlockedError as e:
            degraded = str(e)
            logger.log("yahoo_blocked", keyword=keyword, status=e.status)
            break
        except Exception as e:  # noqa: BLE001 - 通信断等
            failures += 1
            consecutive += 1
            logger.log("yahoo_failed", keyword=keyword,
                       error="{}: {}".format(e.__class__.__name__, str(e)[:160]))
            if consecutive >= MAX_CONSECUTIVE_YAHOO_FAILURES:
                degraded = "ヤフオク取得が{}回連続で失敗しました（{}）".format(
                    consecutive, "{}: {}".format(e.__class__.__name__, str(e)[:120]))
                break
            continue
        consecutive = 0
        if warning:
            notes.append(warning)
            logger.log("yahoo_warning", keyword=keyword, warning=warning)
        for it in items:
            it["category"] = cat
        raw_by_cat.setdefault(cat, []).extend(items)
        yahoo_items += len(items)
        logger.log("yahoo_ok", keyword=keyword, items=len(items))

    if degraded is None and attempted == 0:
        degraded = "ヤフオク検索を1回も実行できませんでした（{}）".format(
            stop_reason or ("検索語が0件" if not jobs else yahoo_budget.block_reason()))
    if degraded is None and failures and yahoo_items == 0:
        degraded = "ヤフオク取得が全て失敗しました（{}件の検索語で0件）".format(failures)

    # ---- 2) 一次フィルタ ----
    kept = []
    excluded = []
    for cat, items in raw_by_cat.items():
        k, e = ybr_filter.prefilter(items, profiles_by_cat[cat], history=history, now=now)
        kept.extend(k)
        excluded.extend(e)
    kept.sort(key=lambda c: int(c.get("minutes_remaining") or 10 ** 9))
    logger.log("prefilter", kept=len(kept), excluded=len(excluded))

    # ---- 3) メルカリ相場 + 4) 利益計算 ----
    comps_clients = {}
    adopted = []
    for cand in kept:
        cat = cand.get("category")
        profile = profiles_by_cat.get(cat) or ybr_profiles.build_profile("apparel", {})
        client = comps_clients.get(cat)
        if client is None:
            client = ybr_mercari.MercariComps(
                profile, cache,
                adapter=deps.get("mercari_adapter"),
                budget=mercari_budget.spend,
                throttle_fn=throttle_fn,
            )
            comps_clients[cat] = client
        if mercari_budget.remaining() <= 0:
            cand["excluded_reason"] = "メルカリ照会の上限に到達（未照会）"
            excluded.append(cand)
            continue
        client.fetch(cand)
        result = ybr_profit.evaluate(cand, profile)
        cand["profit"] = result
        if cand.get("comps_error") and not (cand.get("comps") or {}).get("count"):
            cand["excluded_reason"] = ybr_mercari.FETCH_ERROR_REASON
            excluded.append(cand)
            continue
        if not result["ok"]:
            cand["excluded_reason"] = result["reason"]
            excluded.append(cand)
            continue
        adopted.append(cand)

    mercari_requests = sum(c.requests_made for c in comps_clients.values())
    mercari_successes = sum(c.successes for c in comps_clients.values())
    mercari_aborted = any(c.aborted for c in comps_clients.values())
    mercari_failures = sum(c.fetch_failures for c in comps_clients.values())
    logger.log("mercari", requests=mercari_requests, successes=mercari_successes,
               failures=mercari_failures, aborted=mercari_aborted)

    if degraded is None and kept and mercari_successes == 0 and mercari_failures > 0:
        last = next((c.last_error for c in comps_clients.values() if c.last_error), "")
        degraded = "メルカリ相場の取得が全て失敗しました（{}）".format(last or "原因不明")

    # ---- 5) 並べ替え・上位抽出 ----
    adopted.sort(key=lambda c: (
        -int((c.get("profit") or {}).get("profit_at_bid") or 0),
        int(c.get("minutes_remaining") or 10 ** 9),
    ))
    if len(adopted) > args.top:
        for c in adopted[args.top:]:
            c["excluded_reason"] = "上位{}件の枠外".format(args.top)
            excluded.append(c)
        adopted = adopted[:args.top]

    history.mark(adopted)

    reasons = {}
    for c in excluded:
        key = c.get("excluded_reason") or "理由不明"
        reasons[key] = reasons.get(key, 0) + 1

    if degraded is not None:
        exit_code = EXIT_FETCH_FAILED
    elif not adopted:
        exit_code = EXIT_NO_CANDIDATES
    else:
        exit_code = EXIT_OK

    payload = {
        "meta": {
            "generated_at": now.isoformat(),
            "categories": sorted({c for c, _ in jobs}) or [args.category],
            "keywords": [k for _c, k in jobs],
            "margin_pct": float(args.margin),
            "top": int(args.top),
            "yahoo_requests": yahoo_budget.used,
            "yahoo_items": yahoo_items,
            "prefilter_kept": len(kept),
            "mercari_requests": mercari_requests,
            "mercari_successes": mercari_successes,
            "mercari_failures": mercari_failures,
            "adopted_count": len(adopted),
            "excluded_count": len(excluded),
            "excluded_reasons": reasons,
            "elapsed_sec": round(time.time() - started, 2),
            "exit_code": exit_code,
            "degraded": degraded,
            "notes": notes,
            "out_dir": out_dir,
            "state_dir": state_dir,
            "version": "0.1.0",
        },
        "adopted": adopted,
        "excluded": excluded,
    }

    paths = ybr_report.write_outputs(payload, out_dir)
    logger.log("run_end", exit_code=exit_code, adopted=len(adopted),
               elapsed_sec=payload["meta"]["elapsed_sec"])
    return {"exit_code": exit_code, "payload": payload, "paths": paths}


# --- selftest ----------------------------------------------------------------
def selftest(argv=None, base_dir=None):
    """ネットワークを使わずに環境と設定を点検する。"""
    p = argparse.ArgumentParser(prog="ybr selftest")
    p.add_argument("--state-dir", default=None)
    args = p.parse_args(argv or [])
    base_dir = base_dir or PROJECT_ROOT
    ok = True
    lines = []

    lines.append("python: {}".format(sys.version.split()[0]))
    if sys.version_info < (3, 8):
        ok = False
        lines.append("NG: Python 3.8 以上が必要")

    for cat in ybr_profiles.CATEGORIES:
        try:
            prof = ybr_profiles.load_profile(cat, base_dir=base_dir)
            kws = ybr_profiles.load_keywords(cat, base_dir=base_dir)
            lines.append("profile {}: OK (出品送料{}円 / NG語{}個 / 検索語{}語)".format(
                cat, prof["sellShippingYen"], len(prof["ngWords"]), len(kws)))
            if not kws:
                ok = False
                lines.append("NG: keywords/{}.txt が空".format(cat))
        except Exception as e:  # noqa: BLE001
            ok = False
            lines.append("NG: profile {} 読み込み失敗: {}".format(cat, e))

    try:
        import mercapi  # noqa: F401
        lines.append("mercapi: OK")
    except Exception as e:  # noqa: BLE001
        lines.append("mercapi: 未インストール（bin/ybr setup を実行）: {}".format(
            e.__class__.__name__))

    state_dir = args.state_dir or os.path.join(base_dir, "state")
    try:
        os.makedirs(state_dir, exist_ok=True)
        probe = os.path.join(state_dir, ".write-probe")
        with open(probe, "w", encoding="utf-8") as f:
            f.write("ok")
        os.remove(probe)
        lines.append("state: 書き込みOK ({})".format(state_dir))
    except OSError as e:
        ok = False
        lines.append("NG: state ディレクトリに書けない: {}".format(e))

    # パースと利益計算のドライラン（ネットワーク不使用）
    try:
        fixture = os.path.join(base_dir, "tests", "fixtures", "yahoo_search.fixture.html")
        if os.path.exists(fixture):
            with open(fixture, encoding="utf-8") as f:
                items = ybr_yahoo.parse_items(f.read(), "selftest")
            lines.append("parser: OK (フィクスチャから{}件)".format(len(items)))
        cand = {"price": 5000, "postage": 0,
                "comps": {"count": 10, "median": 10000, "fallback_level": 0}}
        res = ybr_profit.evaluate(cand, ybr_profiles.build_profile("accessory", {}))
        assert res["suggested_bid"] == 5700, res
        lines.append("profit: OK (売値1万円→入札提案5,700円)")
    except Exception as e:  # noqa: BLE001
        ok = False
        lines.append("NG: ドライラン失敗: {}".format(e))

    print("\n".join(lines))
    print("selftest: {}".format("OK" if ok else "NG"))
    return EXIT_OK if ok else EXIT_USAGE


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print("使い方: ybr run [options] | ybr selftest", file=sys.stderr)
        return EXIT_USAGE
    command, rest = argv[0], argv[1:]
    if command == "selftest":
        return selftest(rest)
    if command == "run":
        result = run(rest)
        meta = result["payload"]["meta"]
        print("採用 {}件 / ヤフオク取得 {}件 / メルカリ照会 {}回 / 終了コード {}".format(
            meta["adopted_count"], meta["yahoo_items"], meta["mercari_requests"],
            result["exit_code"]))
        if meta.get("degraded"):
            print("⚠️ {}".format(meta["degraded"]), file=sys.stderr)
        print(result["paths"]["md"])
        return result["exit_code"]
    print("未知のコマンド: {}".format(command), file=sys.stderr)
    return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
