# -*- coding: utf-8 -*-
"""ybr.report: result.md / result.csv / candidates.json の出力。

ルール（accessory-profit-scout の report.py と同じ方針）:
- **URL は必ず単独行**に置く（文中・表セル内に埋めない。Terminal/LINE でのリンク化と
  CLAUDE.md §6-4 のため）。一覧表の「ヤフオクURL」列はオークションIDを出し、
  実URLは各件の詳細ブロックで独立行に置く。
- 文字列はすべて制御文字・改行を除去して1行化する（MD構造破壊・偽リンク行の注入対策）。
- タイトルに混入した `http(s)://...` は除去する（出品者が仕込んだ偽リンクを掲載しない）。
- 金額には必ず「目安」を付ける。相場0件は「相場不明」と書き、0埋めしない。
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
from datetime import datetime

_CTRL_RE = re.compile("[\\x00-\\x1f\\x7f\\u2028\\u2029]")
# 大文字スキーム（HTTPS://）でも除去する（m20 / Codex#14）
_URL_IN_TEXT_RE = re.compile(r"(?i)\b(?:https?|ftp)://\S+")
# CSV の数式インジェクション対策（m20 / Codex#13）。先頭がこれらの文字なら無害化する。
_CSV_FORMULA_LEAD = ("=", "+", "-", "@", "\t", "\r")

# メルカリ検索は完全一致ではなく関連度順のため、件数が多いときは
# 「別物まで混ざった相場」になりうる。その場合は注意として明示する（実走で確認）。
LOOSE_MATCH_COUNT = 60

MD_NAME = "result.md"
CSV_NAME = "result.csv"
JSON_NAME = "candidates.json"
LOG_NAME = "run.log"

CSV_HEADER = [
    "順位", "カテゴリ", "商品名", "オークションID", "ヤフオクURL",
    "現在価格", "仕入送料", "総額", "入札件数", "終了日時", "残り分",
    "メルカリ予想売値", "相場件数", "相場元件数", "確度", "相場クエリ",
    "入札提案価格", "提案額での見込み利益", "損益分岐入札額",
    "メルカリ売切相場URL", "検索語", "注意", "備考",
]
CSV_DISCLAIMER = "金額はすべて目安(メルカリ売切相場の中央値からの逆算)"

TABLE_HEADER = (
    "| # | 商品名 | ヤフオクURL | 現在価格(総額) | 終了 | "
    "メルカリ予想売値(目安・n件) | 入札提案価格 | 提案額での見込み利益 | 注意 |"
)
TABLE_SEP = "|---|---|---|---|---|---|---|---|---|"


def sanitize_text(text):
    """制御文字・改行を空白に置換して1行化する。"""
    if text is None:
        return ""
    cleaned = _CTRL_RE.sub(" ", str(text))
    return re.sub(r"\s+", " ", cleaned).strip()


def safe_title(title):
    """タイトルを1行化し、混入した URL を除去する（偽リンク行の注入防止）。"""
    text = _URL_IN_TEXT_RE.sub(" ", sanitize_text(title))
    text = re.sub(r"\s+", " ", text).strip()
    return text or "(タイトル不明)"


def truncate_title(title, limit=44):
    text = safe_title(title)
    if len(text) <= limit:
        return text
    return text[:limit] + "…"


def _cell(text):
    """表セル用: 1行化＋`|` のエスケープ。"""
    return sanitize_text(text).replace("|", "／")


def csv_safe(value):
    """CSVセルの数式インジェクション対策（m20）。数値はそのまま、文字列だけ無害化する。"""
    if value is None:
        return ""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    text = sanitize_text(value)
    if text[:1] in _CSV_FORMULA_LEAD:
        return "'" + text
    return text


def comps_count_label(candidate):
    """「110（元115・外れ値除外）」の形。IQRトリムを隠さない（m18）。"""
    comps = (candidate or {}).get("comps") or {}
    count = comps.get("count")
    if count is None:
        return "不明"
    trimmed_from = comps.get("trimmed_from")
    if trimmed_from and int(trimmed_from) > int(count):
        return "{}（元{}・外れ値除外）".format(int(count), int(trimmed_from))
    return str(int(count))


def _yen(value):
    if value is None:
        return "不明"
    return "{:,}円".format(int(value))


def _postage_label(candidate):
    postage = candidate.get("postage")
    if postage is None:
        return "未定"
    if int(postage) == 0:
        return "無料"
    return _yen(postage)


def _end_label(candidate, short=False):
    raw = candidate.get("end_at")
    if not raw:
        return "不明"
    try:
        dt = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return sanitize_text(raw)
    if short:
        return dt.strftime("%m/%d %H:%M")
    return dt.strftime("%Y-%m-%d %H:%M JST")


def _remaining_label(candidate):
    minutes = candidate.get("minutes_remaining")
    if minutes is None:
        return "不明"
    minutes = int(minutes)
    return "残り{}時間{}分".format(minutes // 60, minutes % 60)


def _notes(candidate):
    """機械的に出せる注意書き（Claude が目利きで追記する前の土台）。"""
    notes = []
    profit = candidate.get("profit") or {}
    comps = candidate.get("comps") or {}
    if candidate.get("postage") is None:
        notes.append("送料未定(+{}円で見積り)".format(profit.get("ship_in", 0)))
    if profit.get("confidence") == "低":
        notes.append("相場n{}件で確度低".format(profit.get("comps_count", 0)))
    level = int(comps.get("fallback_level", 0) or 0)
    if level >= 1:
        notes.append("相場クエリを{}段縮退".format(level))
    p25, p75 = comps.get("p25"), comps.get("p75")
    if p25 and p75 and p25 > 0 and (p75 / p25) > 2.5:
        notes.append("相場のばらつき大(p25-p75が2.5倍超)")
    if int(comps.get("count") or 0) >= LOOSE_MATCH_COUNT:
        notes.append("相場n{}件と多い(関連度検索で別物混入の可能性→相場URLを目視確認)".format(
            comps.get("count")))
    if candidate.get("is_store"):
        notes.append("ストア出品")
    for w in candidate.get("cautions") or []:
        notes.append("注意語:{}".format(w))
    if candidate.get("comps_error"):
        notes.append("相場取得失敗あり")
    return notes


def _notes_cell(candidate):
    notes = _notes(candidate)
    return _cell(" / ".join(notes)) if notes else "-"


def _comps_cell(candidate):
    profit = candidate.get("profit") or {}
    if not profit.get("expected_sale"):
        return "相場不明"
    return "{}(n={}・確度{})".format(
        _yen(profit["expected_sale"]), comps_count_label(candidate),
        profit.get("confidence") or "?")


def _table_rows(adopted):
    rows = []
    for i, c in enumerate(adopted, start=1):
        profit = c.get("profit") or {}
        rows.append("| {} | {} | {} | {} | {} | {} | {} | {} | {} |".format(
            i,
            _cell(truncate_title(c.get("title"))),
            _cell("ID: " + str(c.get("auction_id") or "?")),
            "{}({})".format(_yen(c.get("price")), _yen(c.get("total_cost"))),
            _cell(_end_label(c, short=True)),
            _comps_cell(c),
            _yen(profit.get("suggested_bid")),
            _yen(profit.get("profit_at_bid")),
            _notes_cell(c),
        ))
    return rows


def _detail_block(rank, c):
    profit = c.get("profit") or {}
    comps = c.get("comps") or {}
    lines = ["### {}位: {}".format(rank, safe_title(c.get("title")))]
    lines.append("- カテゴリ: {} / 検索語: {}".format(
        sanitize_text(c.get("category") or "-"), sanitize_text(c.get("keyword") or "-")))
    lines.append("- 現在価格 {}（入札{}件・送料 {} → 総額 {}）".format(
        _yen(c.get("price")), c.get("bids", 0), _postage_label(c), _yen(c.get("total_cost"))))
    lines.append("- 終了: {}（{}）".format(_end_label(c), _remaining_label(c)))
    if comps.get("count"):
        lines.append(
            "- メルカリ売切相場（目安）: 中央値 {} / n={} / p25 {} 〜 p75 {} / クエリ「{}」".format(
                _yen(comps.get("median")), comps_count_label(c),
                _yen(comps.get("p25")), _yen(comps.get("p75")),
                sanitize_text(comps.get("query"))))
    else:
        status = sanitize_text(c.get("comps_status") or "")
        detail = sanitize_text(c.get("comps_error") or "")
        label = "相場不明(売切0件)" if status == "zero_hits" else "未照会"
        lines.append("- メルカリ売切相場: 取得できず（{}{}）".format(
            label, "・" + detail if detail else ""))
    lines.append("- 予想売値（目安）: {}（確度 {}）".format(
        _yen(profit.get("expected_sale")), profit.get("confidence") or "?"))
    lines.append(
        "- 内訳（目安）: 手取り {} = 予想売値 {} − 手数料 {} − 出品送料 {} ／ 目標利益 {}".format(
            _yen(profit.get("net")), _yen(profit.get("expected_sale")),
            _yen(profit.get("fee")), _yen(profit.get("ship_out")),
            _yen(profit.get("target_profit"))))
    lines.append("- **入札提案価格（目安）: {}** → 提案額で落札できた場合の見込み利益 {}".format(
        _yen(profit.get("suggested_bid")), _yen(profit.get("profit_at_bid"))))
    lines.append("- 損益分岐入札額（目安）: {}（これ以上出すと赤字）".format(
        _yen(profit.get("break_even_bid"))))
    notes = _notes(c)
    lines.append("- 注意: {}".format(_cell(" / ".join(notes)) if notes else "特記なし"))
    lines.append("- ヤフオク商品ページ:")
    lines.append(sanitize_text(c.get("url")))
    if c.get("mercari_url"):
        lines.append("- メルカリ売切相場（この数字の出典）:")
        lines.append(sanitize_text(c.get("mercari_url")))
    lines.append("")
    return lines


def _excluded_table(meta):
    reasons = meta.get("excluded_reasons") or {}
    if not reasons:
        return []
    lines = ["## 不採用の内訳（なぜ候補から外れたか）", "", "| 理由 | 件数 |", "|---|---|"]
    for reason, count in sorted(reasons.items(), key=lambda kv: -kv[1]):
        lines.append("| {} | {} |".format(_cell(reason), int(count)))
    lines.append("")
    return lines


def render_markdown(payload):
    """result.md の本文を作る。"""
    meta = payload.get("meta") or {}
    adopted = payload.get("adopted") or []

    lines = ["# ヤフオク入札リサーチ（すべて目安）", ""]
    if meta.get("degraded"):
        lines.append("> ⚠️ 取得が不完全です: {}".format(sanitize_text(meta["degraded"])))
        lines.append("> この結果は部分的です。数値をそのまま使わないこと。")
        lines.append("")
    lines.append("- 生成: {}".format(sanitize_text(meta.get("generated_at"))))
    lines.append("- 対象カテゴリ: {}".format(
        sanitize_text(" / ".join(meta.get("categories") or []))))
    # 実効利益率を見出しのすぐ下に出す（m17。指定値と違うときは併記して黙って変えない）
    effective = float(meta.get("margin_pct") or 0)
    requested = meta.get("margin_pct_requested")
    if requested is not None and abs(float(requested) - effective) > 1e-9:
        lines.append(
            "- 目標利益率: **{:.0f}%**（対売上・目安）← 指定の{:.0f}%は使用不可のため変更".format(
                effective, float(requested)))
    else:
        lines.append("- 目標利益率: {:.0f}%（対売上・目安）".format(effective))
    # 計画語数と実行語数を分けて出す（m19。実走4語でも23語と見せてはいけない）
    lines.append("- 検索語 予定{}語／実行{}語 / ヤフオク取得 {}件 / 一次フィルタ通過 {}件 / メルカリ照会 {}回".format(
        meta.get("keywords_planned", len(meta.get("keywords") or [])),
        meta.get("keywords_executed", 0), meta.get("yahoo_items", 0),
        meta.get("prefilter_kept", 0), meta.get("mercari_requests", 0)))
    lines.append("- 採用 {}件 / 所要 {:.1f}秒 / 終了コード {}".format(
        len(adopted), float(meta.get("elapsed_sec") or 0.0), meta.get("exit_code", 0)))
    if meta.get("yahoo_failures"):
        lines.append("- ⚠️ ヤフオク取得に失敗した検索語: {}語（{}）".format(
            meta["yahoo_failures"], sanitize_text(meta.get("yahoo_last_error") or "原因不明")))
    lines.append("- 金額はすべて**目安**（メルカリ売切相場の中央値からの逆算）。実際の落札・販売を保証しない。")
    lines.append("- このツールは入札しない（リストアップまで）。入札判断と実行は人が行う。")
    lines.append("")

    for warning in meta.get("warnings") or []:
        lines.append("- ⚠️ {}".format(sanitize_text(warning)))
    if meta.get("warnings"):
        lines.append("")

    for note in meta.get("notes") or []:
        lines.append("- メモ: {}".format(sanitize_text(note)))
    if meta.get("notes"):
        lines.append("")

    if not adopted:
        lines.append("## 結果: 採用0件")
        lines.append("")
        lines.append("条件を満たす商品は見つからなかった（0件を0件として報告している）。")
        lines.append("")
        lines.extend(_excluded_table(meta))
        return "\n".join(lines).rstrip() + "\n"

    lines.append("## 一覧（提案額での見込み利益 降順）")
    lines.append("")
    lines.append(TABLE_HEADER)
    lines.append(TABLE_SEP)
    lines.extend(_table_rows(adopted))
    lines.append("")
    lines.append("## 各件の詳細（URLは独立行）")
    lines.append("")
    for i, c in enumerate(adopted, start=1):
        lines.extend(_detail_block(i, c))
    lines.extend(_excluded_table(meta))
    return "\n".join(lines).rstrip() + "\n"


def render_csv(payload):
    """result.csv の本文を作る。"""
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(CSV_HEADER)
    for i, c in enumerate(payload.get("adopted") or [], start=1):
        profit = c.get("profit") or {}
        comps = c.get("comps") or {}
        writer.writerow([csv_safe(v) for v in (
            i,
            c.get("category"),
            safe_title(c.get("title")),
            c.get("auction_id"),
            c.get("url"),
            c.get("price"),
            "未定" if c.get("postage") is None else c.get("postage"),
            c.get("total_cost"),
            c.get("bids"),
            _end_label(c),
            c.get("minutes_remaining"),
            profit.get("expected_sale"),
            comps_count_label(c),
            comps.get("trimmed_from") or comps.get("count"),
            profit.get("confidence"),
            comps.get("query"),
            profit.get("suggested_bid"),
            profit.get("profit_at_bid"),
            profit.get("break_even_bid"),
            c.get("mercari_url"),
            c.get("keyword"),
            " / ".join(_notes(c)),
            CSV_DISCLAIMER,
        )])
    return buf.getvalue()


def _write_text(path, text):
    tmp = "{}.tmp-{}".format(path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def write_outputs(payload, out_dir):
    """result.md / result.csv / candidates.json を書き出してパスを返す。"""
    os.makedirs(out_dir, exist_ok=True)
    md_path = os.path.join(out_dir, MD_NAME)
    csv_path = os.path.join(out_dir, CSV_NAME)
    json_path = os.path.join(out_dir, JSON_NAME)
    _write_text(md_path, render_markdown(payload))
    _write_text(csv_path, render_csv(payload))
    _write_text(json_path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    return {"dir": out_dir, "md": md_path, "csv": csv_path, "json": json_path}
