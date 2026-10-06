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
import urllib.parse

from . import throttle as throttle_module

ITEM_TYPE_MERCARI = "ITEM_TYPE_MERCARI"
STATUS_SOLD_OUT = "ITEM_STATUS_SOLD_OUT"

# adapter例外がこの回数連続したら「全滅障害」とみなし以降の照会を中断する
MAX_CONSECUTIVE_FAILURES = 3

FETCH_ERROR_REASON = "相場取得失敗(通信/依存エラー)"

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


def _clean_title(title):
    """タイトルを検索語向けに整える（管理番号・括弧記号の除去→空白区切り）。"""
    if not title:
        return ""
    s = re.sub(r"[（(][0-9A-Za-z_\-]+[)）]", " ", title)
    s = re.sub(
        r"[【】\[\]（）()「」『』〈〉《》/／・|,、。!！?？☆★■□◆◇●○～〜×"
        r"＜＞<>#＃*＊+＋:：;；\"'`^~_]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _compiled_drops(profile):
    out = []
    for pattern in profile.get("dropTokenPatterns") or ():
        try:
            out.append(re.compile(pattern, re.IGNORECASE))
        except re.error:
            continue
    return out


def tokenize(candidate, profile):
    """相場クエリ用のトークン列（ノイズ語・サイズ/色等・数値のみの語を除去）。"""
    noise = {str(w).lower() for w in (profile.get("noiseWords") or ())}
    drops = _compiled_drops(profile)
    tokens = []
    seen = set()

    def _add(word):
        w = (word or "").strip()
        if not w:
            return
        key = w.lower()
        if key in seen:
            return
        if key in noise or _is_noise_number(w):
            return
        for rx in drops:
            if rx.match(w):
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


class MercariComps:
    """候補1件のメルカリ売切相場(comps)を取得する。

    - profile: mercariMinIntervalSec を throttle に使う
    - cache: get(query)/put(query, comps)
    - adapter: search(query)->list[item-like]（省略時は実 mercapi）
    - budget: callable(n)->bool（False=予算切れ。fetch は None を返す）
    - throttle_fn: テスト注入用（省略時は最小2.0秒のハードfloorが効く）
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
        self.successes = 0
        self.budget_exhausted = False
        self.fetch_failures = 0
        self.aborted = False
        self.last_error = None
        self._consecutive_failures = 0

    def fetch(self, candidate):
        """candidate の comps を取得して返す（candidate["comps"] にも入れる）。

        取得不能は None。adapter例外は candidate["comps_error"] に残す
        （「売切0件（相場不明）」と「取得失敗」を混同しないため）。例外は外に漏らさない。
        """
        if self.aborted:
            candidate["comps_error"] = self.last_error or "連続失敗により照会中断"
            return None
        for level, query in build_queries(candidate, self.profile):
            cached = self.cache.get(query)
            if cached is not None:
                comps = dict(cached)
                comps["cache_hit"] = True
                candidate["comps"] = comps
                candidate["mercari_url"] = sold_search_url(comps.get("query") or query)
                return comps

            if self.budget is not None and not self.budget(1):
                self.budget_exhausted = True
                return None

            self.throttle_fn(self.min_interval)
            self.requests_made += 1
            try:
                raw_items = self.adapter.search(query)
            except Exception as e:  # noqa: BLE001 - mercapi不在/通信障害=相場取得不能
                self.fetch_failures += 1
                self._consecutive_failures += 1
                self.last_error = "{}: {}".format(e.__class__.__name__, str(e)[:120])
                candidate["comps_error"] = self.last_error
                if self._consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    self.aborted = True
                return None
            self._consecutive_failures = 0

            prices = extract_prices(raw_items)
            if not prices:
                continue  # 次の（短い）クエリへ縮退

            st = compute_stats(prices)
            comps = {
                "query": query,
                "count": st["count"],
                "median": st["median"],
                "p25": st["p25"],
                "p75": st["p75"],
                "min": st["min"],
                "max": st["max"],
                "mean": st["mean"],
                "fallback_level": level,
                "cache_hit": False,
            }
            if "trimmed_from" in st:
                comps["trimmed_from"] = st["trimmed_from"]
            self.cache.put(query, comps)
            self.successes += 1
            candidate["comps"] = comps
            candidate["mercari_url"] = sold_search_url(query)
            return comps

        # 全クエリで有効価格0件。参照用URLだけは最初のクエリで残す（捏造はしない）。
        attempts = build_queries(candidate, self.profile)
        if attempts:
            candidate["mercari_url"] = sold_search_url(attempts[0][1])
        return None
