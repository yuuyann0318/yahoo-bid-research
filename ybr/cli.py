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
import math
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
class UsageErrorParser(argparse.ArgumentParser):
    """引数エラーを終了コード1にする（M8。既定の2は「採用0件」と衝突する）。"""

    def error(self, message):
        self.print_usage(sys.stderr)
        sys.stderr.write("{}: error: {}\n".format(self.prog, message))
        raise SystemExit(EXIT_USAGE)


def _margin(value):
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError("--margin は数値で指定してください: {}".format(value))
    if not math.isfinite(v):  # nan / inf を弾く（m17 / Codex#18）
        raise argparse.ArgumentTypeError(
            "--margin に非有限値は使えません: {}".format(value))
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
    p = UsageErrorParser(
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
        self.hit_run_limit = False
        self.hit_daily_limit = False

    def remaining(self):
        run_left = max(0, self.run_limit - self.used)
        day_left = self.daily.remaining(self.kind) if self.daily else run_left
        if day_left <= 0 and self.daily is not None:
            self.hit_daily_limit = True
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
            self.hit_run_limit = True
            return False
        if self.daily is not None and not self.daily.spend(self.kind, n):
            self.blocked_by = "日次上限({}回)".format(self.daily.limits.get(self.kind))
            # 日次上限は「異常」扱い（部分結果で成功にしない・Codex#3）
            self.hit_daily_limit = True
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
def _round_robin(groups):
    """[(key, [item...]), ...] を順番に1つずつ取り出して並べる。"""
    out = []
    index = 0
    while True:
        added = False
        for _key, items in groups:
            if index < len(items):
                out.append(items[index])
                added = True
        if not added:
            return out
        index += 1


def _brand_of(keyword):
    """検索語の先頭トークンをブランドとみなす（m25 のラウンドロビンのキー）。"""
    parts = str(keyword or "").split()
    return parts[0] if parts else str(keyword or "")


def _brand_round_robin(category, keywords):
    """同一カテゴリ内をブランド間ラウンドロビンにする（m25）。

    --max-yahoo-requests が小さいとき、先頭ブランド（ティファニー4語）に偏って
    他ブランドが1語も実行されない問題を防ぐ。
    """
    groups = []
    order = []
    for kw in keywords:
        brand = _brand_of(kw)
        if brand not in order:
            order.append(brand)
            groups.append((brand, []))
        for key, items in groups:
            if key == brand:
                items.append((category, kw))
                break
    return _round_robin(groups)


def resolve_jobs(args, base_dir=None):
    """(category, keyword) のリストを決める。category は accessory/apparel/unknown。"""
    cats = ("accessory", "apparel") if args.category == "both" else (args.category,)
    if args.keywords:
        if args.category == "both":
            # カテゴリ未指定の明示検索語は語ごとに推定する。
            # 判定できない語は "unknown"（送料はアパレル=高い側、NG語は両カテゴリの和集合・M11）
            return [(ybr_profiles.resolve_category(k, base_dir), k) for k in args.keywords]
        return [(cats[0], k) for k in args.keywords]
    groups = []
    for cat in cats:
        kws = ybr_profiles.load_keywords(cat, base_dir=base_dir, brands=args.brands)
        groups.append((cat, _brand_round_robin(cat, kws)))
    return _round_robin(groups)


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
    return ybr_profiles.profile_for_resolved(
        category, base_dir=base_dir, overrides=overrides)



def _profit_reason_kind(result):
    """利益判定の不採用理由を集計用の種類に畳む（金額を含めない）。"""
    reason = (result or {}).get("reason") or "理由不明"
    for kind in ("相場不明", "入札上限超過", "提案額が下限未満"):
        if reason.startswith(kind):
            return kind
    return reason


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

    # 指定した利益率がそのまま使えない場合（手数料率との合計が100%以上など）は
    # 黙って既定へ戻さず、実際に使った値をメモと meta に出す（silent fallback 禁止）。
    effective_margin = float(args.margin)
    for _cat, _prof in profiles_by_cat.items():
        effective_margin = ybr_profit.target_margin_pct(_prof)
        break
    if abs(effective_margin - float(args.margin)) > 1e-9:
        notes.append(
            "指定した利益率{:.0f}%は使えないため{:.0f}%で計算した"
            "（手数料率との合計が100%以上、または妥当範囲[{:.0f}〜{:.0f}%]外）".format(
                float(args.margin), effective_margin,
                ybr_profit.MIN_TARGET_MARGIN_PCT, ybr_profit.MAX_TARGET_MARGIN_PCT))
    fetcher = deps.get("fetcher")
    throttle_fn = deps.get("throttle_fn")

    logger.log("run_start", category=args.category, keywords=len(jobs),
               margin=args.margin, top=args.top, out=out_dir)

    # ---- 1) ヤフオク検索 ----
    # 予算計上は search_keyword 側に任せる（全ページの初回HTTPを計上する・M6）。
    # 連続失敗は HTTP試行単位で数える（検索語単位だと実質9回続く・m22）。
    raw_by_cat = {}
    yahoo_items = 0
    attempted = 0
    executed = 0
    failures = 0
    yahoo_last_error = None
    tracker = ybr_yahoo.FailureTracker(MAX_CONSECUTIVE_YAHOO_FAILURES)
    for cat, keyword in jobs:
        if yahoo_budget.remaining() <= 0:
            stop_reason = yahoo_budget.block_reason()
            notes.append("ヤフオク検索を打ち切り: {}（残りの検索語{}語は未実行）".format(
                stop_reason, len(jobs) - attempted))
            break
        attempted += 1
        try:
            items, warning = ybr_yahoo.search_keyword(
                keyword, pages=args.pages, fetcher=fetcher,
                spend=yahoo_budget.spend, now=now, tracker=tracker)
        except ybr_yahoo.BlockedError as e:
            # C2: 403/429 は全カテゴリの取得を止めて exit3（次の検索語へ進まない）
            degraded = str(e)
            logger.log("yahoo_blocked", keyword=keyword, status=e.status)
            break
        except ybr_yahoo.BudgetExhausted as e:
            stop_reason = yahoo_budget.block_reason()
            notes.append("ヤフオク検索を打ち切り: {}（{}）".format(stop_reason, str(e)[:80]))
            break
        except ybr_yahoo.ConsecutiveFailureError as e:
            failures += 1
            yahoo_last_error = e.last_error
            degraded = str(e)
            logger.log("yahoo_consecutive_failure", keyword=keyword,
                       consecutive=e.consecutive, error=e.last_error)
            break
        except Exception as e:  # noqa: BLE001 - 通信断等
            failures += 1
            yahoo_last_error = "{}: {}".format(e.__class__.__name__, str(e)[:120])
            logger.log("yahoo_failed", keyword=keyword, error=yahoo_last_error)
            continue
        executed += 1
        if warning:
            notes.append(warning)
            logger.log("yahoo_warning", keyword=keyword, warning=warning)
        for it in items:
            it["category"] = cat
        raw_by_cat.setdefault(cat, []).extend(items)
        yahoo_items += len(items)
        logger.log("yahoo_ok", keyword=keyword, items=len(items))

    if degraded is None and executed == 0:
        degraded = "ヤフオク検索を1回も実行できませんでした（{}）".format(
            yahoo_last_error or stop_reason
            or ("検索語が0件" if not jobs else yahoo_budget.block_reason()))
    if degraded is None and failures and yahoo_items == 0:
        degraded = "ヤフオク取得が全て失敗しました（{}語で0件 / {}）".format(
            failures, yahoo_last_error or "原因不明")
    # Codex#3: 日次上限での途中打ち切りは部分結果なので成功扱いにしない
    if degraded is None and yahoo_budget.hit_daily_limit:
        degraded = "ヤフオクの日次上限({}回)に到達し途中で打ち切りました（部分結果）".format(
            daily.limits.get("yahoo"))
    # M16: 連続していないヤフオク失敗も result.md / meta に出す（run.log だけに埋めない）
    if failures and degraded is None:
        notes.append("ヤフオク取得に失敗した検索語が{}語あります（{}）".format(
            failures, yahoo_last_error or "原因不明"))

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
        profile = profiles_by_cat.get(cat) or ybr_profiles.load_union_profile(base_dir)
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
            cand["comps_status"] = "budget_exhausted"
            cand["excluded_reason"] = "メルカリ照会の上限に到達（未照会・{}）".format(
                mercari_budget.block_reason())
            cand["excluded_kind"] = ybr_mercari.STATUS_KIND["budget_exhausted"]
            excluded.append(cand)
            continue
        client.fetch(cand)
        result = ybr_profit.evaluate(cand, profile)
        cand["profit"] = result
        status = cand.get("comps_status")
        # C3: 「売切0件（相場不明）」と「未照会（予算切れ・取得失敗・中断）」を混ぜない
        if status not in (None, "ok", "zero_hits"):
            cand["excluded_reason"] = "{}: {}".format(
                ybr_mercari.STATUS_KIND.get(status, "相場未照会"),
                cand.get("comps_error") or "詳細不明")
            cand["excluded_kind"] = ybr_mercari.STATUS_KIND.get(status, "相場未照会")
            excluded.append(cand)
            continue
        if not result["ok"]:
            cand["excluded_reason"] = result["reason"]
            cand["excluded_kind"] = (
                ybr_mercari.STATUS_KIND["zero_hits"] if status == "zero_hits"
                else _profit_reason_kind(result))
            excluded.append(cand)
            continue
        adopted.append(cand)

    mercari_requests = sum(c.requests_made for c in comps_clients.values())
    mercari_valid = sum(c.valid_comps for c in comps_clients.values())
    mercari_http_ok = sum(c.http_successes for c in comps_clients.values())
    mercari_cache_hits = sum(c.cache_hits for c in comps_clients.values())
    mercari_aborted = any(c.aborted for c in comps_clients.values())
    mercari_blocked = next(
        (c.blocked_status for c in comps_clients.values() if c.blocked), None)
    mercari_failures = sum(c.fetch_failures for c in comps_clients.values())
    logger.log("mercari", requests=mercari_requests, valid_comps=mercari_valid,
               http_successes=mercari_http_ok, cache_hits=mercari_cache_hits,
               failures=mercari_failures, aborted=mercari_aborted,
               blocked_status=mercari_blocked)

    # C4: ブロック・連続失敗の中断は「成功件数に関係なく」exit3 + ⚠️バナー
    mercari_degraded = None
    if mercari_blocked is not None:
        mercari_degraded = "メルカリ相場の取得がブロックされ中断しました(HTTP {})".format(
            mercari_blocked)
    elif mercari_aborted:
        last = next((c.last_error for c in comps_clients.values() if c.last_error), "")
        mercari_degraded = "メルカリ相場の取得が{}回連続で失敗し中断しました（{}）".format(
            ybr_mercari.MAX_CONSECUTIVE_FAILURES, last or "原因不明")
    if mercari_degraded is not None:
        if degraded is None:
            degraded = mercari_degraded
        else:
            # ヤフオク側の理由を上書きしない（どちらも exit3。両方を残す）
            notes.append(mercari_degraded)
    elif degraded is None and kept and mercari_valid == 0 and mercari_failures > 0:
        # M7: キャッシュ命中は「有効相場」に数えるので、ここは本当に1件も無いときだけ
        last = next((c.last_error for c in comps_clients.values() if c.last_error), "")
        degraded = "メルカリ相場の取得が全て失敗しました（{}）".format(last or "原因不明")
    elif degraded is None and mercari_budget.hit_daily_limit:
        degraded = "メルカリの日次上限({}回)に到達し途中で打ち切りました（部分結果）".format(
            daily.limits.get("mercari"))

    # ---- 5) 並べ替え・上位抽出 ----
    adopted.sort(key=lambda c: (
        -int((c.get("profit") or {}).get("profit_at_bid") or 0),
        int(c.get("minutes_remaining") or 10 ** 9),
    ))
    if len(adopted) > args.top:
        for c in adopted[args.top:]:
            c["excluded_reason"] = "上位{}件の枠外".format(args.top)
            c["excluded_kind"] = "上位{}件の枠外".format(args.top)
            excluded.append(c)
        adopted = adopted[:args.top]

    # 内訳は「種類」で集計する（金額入りの文言でキーが散ると0件の理由が読めない）。
    reasons = {}
    for c in excluded:
        key = c.get("excluded_kind") or c.get("excluded_reason") or "理由不明"
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
            "keywords_planned": len(jobs),
            "keywords_executed": executed,
            "margin_pct": effective_margin,
            "margin_pct_requested": float(args.margin),
            "top": int(args.top),
            "yahoo_requests": yahoo_budget.used,
            "yahoo_items": yahoo_items,
            "yahoo_failures": failures,
            "yahoo_last_error": yahoo_last_error,
            "prefilter_kept": len(kept),
            "mercari_requests": mercari_requests,
            "mercari_valid_comps": mercari_valid,
            "mercari_http_successes": mercari_http_ok,
            "mercari_cache_hits": mercari_cache_hits,
            "mercari_successes": mercari_valid,  # 後方互換（意味=有効相場の件数）
            "mercari_failures": mercari_failures,
            "mercari_blocked_status": mercari_blocked,
            "mercari_aborted": mercari_aborted,
            "adopted_count": len(adopted),
            "excluded_count": len(excluded),
            "excluded_reasons": reasons,
            "elapsed_sec": round(time.time() - started, 2),
            "exit_code": exit_code,
            "degraded": degraded,
            "notes": notes,
            "warnings": list(daily.warnings),
            "budget_save_failed": daily.save_failed,
            "out_dir": out_dir,
            "state_dir": state_dir,
            "version": "0.1.0",
        },
        "adopted": adopted,
        "excluded": excluded,
    }

    paths = ybr_report.write_outputs(payload, out_dir)
    # M9: レポート出力が成功し、かつ終了コードが 0/2 のときだけ履歴に残す
    # （exit3 の部分結果を「報告済み」にすると7日間再掲されない）
    if exit_code in (EXIT_OK, EXIT_NO_CANDIDATES):
        history.mark(adopted)
        logger.log("history_marked", count=len(adopted))
    else:
        logger.log("history_skipped", reason="exit_code={}".format(exit_code))
    logger.log("run_end", exit_code=exit_code, adopted=len(adopted),
               elapsed_sec=payload["meta"]["elapsed_sec"])
    return {"exit_code": exit_code, "payload": payload, "paths": paths}


# --- selftest ----------------------------------------------------------------
def selftest(argv=None, base_dir=None):
    """ネットワークを使わずに環境と設定を点検する。"""
    p = UsageErrorParser(prog="ybr selftest")
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
