# -*- coding: utf-8 -*-
"""ybr.mercari: メルカリ売切相場の取得（mercapi 0.4.2・ログイン不要の検索のみ）。

accessory-profit-scout の scout/mercari.py + scout/stats.py を移植・簡素化した。

必須の後処理（捏造・誤相場を防ぐため省略禁止）:
  item_type == "ITEM_TYPE_MERCARI"（メルカリShops/BEYOND を除外）
  status    == "ITEM_STATUS_SOLD_OUT"（出品中の混入を除外）
  not is_no_price
  価格は real_price を優先

件数は必ず**自分で数えた件数**を使う（API の num_found は信用しない）。
0件は 0件として返す（相場不明。0埋め・推測で埋めない）。
"""
from __future__ import annotations

import asyncio
import re
import statistics
import unicodedata
import urllib.parse

from . import throttle as throttle_module

ITEM_TYPE_MERCARI = "ITEM_TYPE_MERCARI"
STATUS_SOLD_OUT = "ITEM_STATUS_SOLD_OUT"

# adapter例外がこの回数連続したら「全滅障害」とみなし以降の照会を中断する
MAX_CONSECUTIVE_FAILURES = 3

# メルカリ側にブロックされたと判断する HTTP ステータス（即時中断・C4）
BLOCKED_STATUSES = (403, 429)

FETCH_ERROR_REASON = "相場取得失敗(通信/依存エラー)"

# comps_status -> 不採用の区分ラベル（C3: 「売切0件」と「未照会」を絶対に混ぜない）
STATUS_KIND = {
    "zero_hits": "相場不明(メルカリ売切0件)",
    "no_query": "相場未照会(クエリを作れず)",
    "budget_exhausted": "相場未照会(メルカリ予算切れ)",
    "fetch_error": "相場未照会(取得失敗)",
    "aborted": "相場未照会(連続失敗で照会中断)",
    "blocked": "相場未照会(メルカリにブロック)",
}

# クエリ語数の段階（fallback_level: 0=6語, 1=3語, 2=2語）
_TOKEN_STEPS = ((0, 6), (1, 3), (2, 2))

_NUMERIC_TOKEN = re.compile(r"^[0-9,.]+円?$")
_SOLD_SEARCH_BASE = "https://jp.mercari.com/search"


def _is_noise_number(word):
    """数字だけのトークンのうち「価格・サイズ・個数」を落とし、型番は残す。

    落とす: 12,000円 / 2.5 / 40 / 3（1〜2桁、カンマ・小数点・円を含むもの）
    残す  : 501（リーバイス）/ 925（シルバー）/ 1837（ティファニー）など3桁以上の型番
    """
    if not _NUMERIC_TOKEN.match(word):
        return False
    if "," in word or "." in word or "円" in word:
        return True
    return len(re.sub(r"\D", "", word)) <= 2


def compute_stats(prices):
    """価格リストから統計を出す（n>=8 で 1.5*IQR トリム）。0件は {"count": 0}。"""
    vals = sorted(int(p) for p in prices if p is not None)
    n = len(vals)
    if n == 0:
        return {"count": 0}

    trimmed_from = None
    if n >= 8:
        q = statistics.quantiles(vals, n=4, method="inclusive")
        q1, q3 = q[0], q[2]
        iqr = q3 - q1
        lo, hi = q1 - 1.5 * iqr, q3 + 1.5 * iqr
        kept = [p for p in vals if lo <= p <= hi]
        if 4 <= len(kept) < n:
            trimmed_from = n
            vals = kept
            n = len(vals)

    if n >= 2:
        q = statistics.quantiles(vals, n=4, method="inclusive")
        p25, p75 = int(round(q[0])), int(round(q[2]))
    else:
        p25 = p75 = vals[0]

    stats = {
        "count": n,
        "min": vals[0],
        "p25": p25,
        "median": int(round(statistics.median(vals))),
        "p75": p75,
        "max": vals[-1],
        "mean": int(round(statistics.mean(vals))),
    }
    if trimmed_from is not None:
        stats["trimmed_from"] = trimmed_from
    return stats


def sold_search_url(query):
    """メルカリの売切(sold_out)検索URL。相場に実際に使ったクエリで組み立てる。"""
    q = re.sub(r"\s+", " ", str(query or "")).strip()
    params = [
        ("keyword", q),
        ("status", "sold_out"),
        ("item_types", "mercari"),
        ("order", "desc"),
        ("sort", "created_time"),
    ]
    return _SOLD_SEARCH_BASE + "?" + urllib.parse.urlencode(params)


# 括弧の中身が「管理番号」とみなせる形（M14）。
# これ以外は中身を残して括弧だけ外す（(501) や (B-zero1) のような実在型番を守る）。
_MGMT_CODE_RE = re.compile(
    r"^(?:"
    r"[0-9a-z]*_[0-9a-z_\-]*"       # アンダースコア入り: 12678_0252
    r"|\d{5,}"                      # 5桁以上の連番
    r"|[0-9a-z]{8,}"                # 8文字以上の英数字の羅列
    r"|\d{3,}[-]\d{3,}"             # 1234-5678
    r")$",
    re.IGNORECASE,
)


def _strip_brackets(match):
    """括弧の中身が管理番号なら捨て、そうでなければ中身を残す（M14）。"""
    inner = match.group(1)
    if _MGMT_CODE_RE.match(inner):
        return " "
    return " " + inner + " "


def _clean_title(title):
    """タイトルを検索語向けに整える（管理番号の除去・括弧記号の除去→空白区切り）。"""
    if not title:
        return ""
    s = re.sub(r"[（(]\s*([0-9A-Za-z_\-]+)\s*[)）]", _strip_brackets, title)
    s = re.sub(
        r"[【】\[\]（）()「」『』〈〉《》〔〕｛｝{}/／・|,、。!！?？☆★■□◆◇●○◎"
        r"▽△▼▲※→←↑↓♪♡♥〓–—―~〜～×＜＞<>#＃*＊+＋:：;；\"'`^_]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _compiled_drops(profile):
    out = []
    for pattern in profile.get("dropTokenPatterns") or ():
        try:
            out.append(re.compile(pattern, re.IGNORECASE))
        except re.error:
            continue
    return out


def _norm_token(word):
    """判定用のトークン正規化: NFKC + 小文字化（全角「Ｍ」「Ｗ３２」対策・M15）。"""
    return unicodedata.normalize("NFKC", str(word or "")).strip().lower()


def tokenize(candidate, profile):
    """相場クエリ用のトークン列（ノイズ語・サイズ/色等・数値のみの語を除去）。

    判定は NFKC 正規化後の文字列で行い、クエリには元の表記を入れる（M15）。
    """
    noise = {_norm_token(w) for w in (profile.get("noiseWords") or ())}
    drops = _compiled_drops(profile)
    tokens = []
    seen = set()

    def _add(word):
        w = (word or "").strip()
        if not w:
            return
        key = _norm_token(w)
        if not key or key in seen:
            return
        if key in noise or _is_noise_number(key):
            return
        for rx in drops:
            if rx.match(key):
                return
        seen.add(key)
        tokens.append(w)

    for w in _clean_title(candidate.get("title")).split():
        _add(w)
    if not tokens:  # タイトルから語が取れなければ収集キーワードで代用
        for w in _clean_title(candidate.get("keyword")).split():
            _add(w)
    return tokens


def build_queries(candidate, profile):
    """(fallback_level, query) のリスト。同一クエリは低い level を優先して1つに畳む。"""
    tokens = tokenize(candidate, profile)
    if not tokens:
        return []
    attempts = []
    seen = set()
    for level, n in _TOKEN_STEPS:
        q = " ".join(tokens[:n])
        if q and q not in seen:
            seen.add(q)
            attempts.append((level, q))
    return attempts


def extract_prices(raw_items):
    """必須後処理3条件を通った商品の価格リスト（real_price優先）。"""
    prices = []
    for it in raw_items or ():
        if getattr(it, "item_type", None) != ITEM_TYPE_MERCARI:
            continue
        if getattr(it, "status", None) != STATUS_SOLD_OUT:
            continue
        if getattr(it, "is_no_price", False):
            continue
        price = getattr(it, "real_price", None)
        if price is None:
            price = getattr(it, "price", None)
        if price is not None:
            prices.append(int(price))
    return prices


class MercapiAdapter:
    """実 mercapi を遅延importして売切検索する本番アダプタ。"""

    def search(self, query):
        from mercapi import Mercapi  # 遅延import（未インストールは例外→取得失敗として扱う）
        from mercapi.requests.search import SearchRequestData

        async def _run():
            mc = Mercapi()
            res = await mc.search(query, status=[SearchRequestData.Status.STATUS_SOLD_OUT])
            return list(res.items)

        return asyncio.run(_run())


def _http_status_of(error):
    """例外から HTTP ステータスを取り出す（httpx / urllib / 文字列のいずれでも）。"""
    response = getattr(error, "response", None)
    status = getattr(response, "status_code", None)
    if status is None:
        status = getattr(error, "status_code", None)
    if status is None:
        status = getattr(error, "code", None)
    try:
        return int(status) if status is not None else None
    except (TypeError, ValueError):
        return None


# 統計としてキャッシュに保存するキー。fallback_level / query / cache_hit は**保存しない**
# （C1: クエリ文字列が同じでも縮退段数は候補ごとに違うため、取得時に毎回決める）。
_CACHE_STAT_KEYS = ("count", "median", "p25", "p75", "min", "max", "mean", "trimmed_from")


class MercariComps:
    """候補1件のメルカリ売切相場(comps)を取得する。

    - profile: mercariMinIntervalSec を throttle に使う
    - cache: get(query)/put(query, stats)
    - adapter: search(query)->list[item-like]（省略時は実 mercapi）
    - budget: callable(n)->bool（False=予算切れ。fetch は None を返す）
    - throttle_fn: テスト注入用（省略時は最小2.0秒のハードfloorが効く）

    集計（M7）:
      valid_comps     … 有効な相場を得た件数（キャッシュ命中も含む）
      http_successes  … 実際に adapter.search が成功した回数
    状態（C3/C4）: candidate["comps_status"] に ok / zero_hits / fetch_error /
      aborted / blocked / budget_exhausted / no_query を必ず入れる。
    """

    def __init__(self, profile, cache, adapter=None, budget=None, throttle_fn=None):
        self.profile = profile or {}
        self.cache = cache
        self.adapter = adapter if adapter is not None else MercapiAdapter()
        self.budget = budget
        self.throttle_fn = throttle_fn or throttle_module.throttle
        try:
            self.min_interval = float(self.profile.get("mercariMinIntervalSec", 2.5))
        except (TypeError, ValueError):
            self.min_interval = 2.5
        self.requests_made = 0
        self.http_successes = 0
        self.valid_comps = 0
        self.cache_hits = 0
        self.budget_exhausted = False
        self.fetch_failures = 0
        self.aborted = False
        self.blocked = False
        self.blocked_status = None
        self.last_error = None
        self._consecutive_failures = 0

    # 後方互換（旧名）。意味は「有効相場の件数」。
    @property
    def successes(self):
        return self.valid_comps

    def _comps_from_stats(self, stats, query, level, cache_hit):
        """統計 + 「今回の」縮退段数から comps を組む（C1）。"""
        comps = {"query": query, "fallback_level": int(level), "cache_hit": bool(cache_hit)}
        for key in _CACHE_STAT_KEYS:
            if stats.get(key) is not None:
                comps[key] = stats[key]
        return comps

    def _fail(self, candidate, status, reason, url_query=None):
        candidate["comps_status"] = status
        candidate["comps_error"] = reason
        if url_query:
            candidate["mercari_url"] = sold_search_url(url_query)
        return None

    def fetch(self, candidate):
        """candidate の comps を取得して返す（candidate["comps"] にも入れる）。

        取得不能は None。そのとき candidate["comps_status"] で
        「売切0件（相場不明）」と「未照会／取得失敗」を必ず区別する（C3）。
        例外は外に漏らさない。
        """
        attempts = build_queries(candidate, self.profile)
        if not attempts:
            return self._fail(candidate, "no_query", "相場クエリを作れなかった(タイトルから語が取れない)")
        first_query = attempts[0][1]

        if self.blocked:
            return self._fail(
                candidate, "blocked",
                "メルカリ側にブロックされ照会中断(HTTP {})".format(self.blocked_status),
                first_query)
        if self.aborted:
            return self._fail(
                candidate, "aborted",
                self.last_error or "連続失敗により照会中断", first_query)

        for level, query in attempts:
            cached = self.cache.get(query)
            if cached is not None:
                comps = self._comps_from_stats(cached, query, level, True)
                if int(comps.get("count", 0) or 0) <= 0:
                    continue  # 0件はキャッシュしないが、念のため縮退を続ける
                self.valid_comps += 1
                self.cache_hits += 1
                candidate["comps"] = comps
                candidate["comps_status"] = "ok"
                candidate["mercari_url"] = sold_search_url(query)
                return comps

            if self.budget is not None and not self.budget(1):
                self.budget_exhausted = True
                return self._fail(
                    candidate, "budget_exhausted",
                    "メルカリ照会の予算切れで未照会", first_query)

            self.throttle_fn(self.min_interval)
            self.requests_made += 1
            try:
                raw_items = self.adapter.search(query)
            except Exception as e:  # noqa: BLE001 - mercapi不在/通信障害=相場取得不能
                self.fetch_failures += 1
                self._consecutive_failures += 1
                self.last_error = "{}: {}".format(e.__class__.__name__, str(e)[:120])
                status = _http_status_of(e)
                if status in BLOCKED_STATUSES:
                    self.blocked = True
                    self.blocked_status = status
                    self.aborted = True
                    return self._fail(
                        candidate, "blocked",
                        "メルカリ側にブロックされました(HTTP {})".format(status), first_query)
                if self._consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    self.aborted = True
                    return self._fail(
                        candidate, "aborted",
                        "{}回連続で失敗し照会中断({})".format(
                            self._consecutive_failures, self.last_error), first_query)
                return self._fail(candidate, "fetch_error", self.last_error, first_query)
            self._consecutive_failures = 0
            self.http_successes += 1

            prices = extract_prices(raw_items)
            if not prices:
                continue  # 次の（短い）クエリへ縮退

            stats = compute_stats(prices)
            # キャッシュには統計だけを保存する（縮退段数・係数は毎回決める・C1）
            self.cache.put(query, {k: stats[k] for k in _CACHE_STAT_KEYS if k in stats})
            comps = self._comps_from_stats(stats, query, level, False)
            self.valid_comps += 1
            candidate["comps"] = comps
            candidate["comps_status"] = "ok"
            candidate["mercari_url"] = sold_search_url(query)
            return comps

        # 全クエリで有効価格0件（照会はできた）。参照用URLは残す。捏造はしない。
        candidate["comps_status"] = "zero_hits"
        candidate["mercari_url"] = sold_search_url(first_query)
        return None
