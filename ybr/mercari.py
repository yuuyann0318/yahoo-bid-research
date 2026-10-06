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


# 括弧の中身が「管理番号」とみなせる形（M14 / Codex r2）。
# **数字を含むものだけ**を管理番号候補にする。純アルファベット（BURBERRY・ARCTERYX）は残す。
_MGMT_CODE_RE = re.compile(
    r"^(?:"
    r"[0-9a-z]*_[0-9a-z_\-]*"           # アンダースコア入り: 12678_0252
    r"|\d{5,}"                          # 純数字5桁以上: 1234567
    r"|\d{3,}[-]\d{3,}"                 # 1234-5678
    r"|(?=.*\d)(?=.*[a-z])[0-9a-z]{8,}"  # 英数混在8文字以上: abc12345xyz
    r")$",
    re.IGNORECASE,
)
_HAS_DIGIT_RE = re.compile(r"\d")


def _is_management_code(inner):
    """管理番号と確認できる表記か。数字を含まないものは必ず False（型番・ブランド名を守る）。"""
    if not _HAS_DIGIT_RE.search(inner or ""):
        return False
    return bool(_MGMT_CODE_RE.match(inner))


def _strip_brackets(match):
    """括弧の中身が管理番号なら捨て、そうでなければ中身を残す（M14）。"""
    inner = match.group(1)
    if _is_management_code(inner):
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


class MercariBlockedError(RuntimeError):
    """メルカリ側に 403 / 429 でブロックされた（status を必ず保持する）。

    mercapi 0.4.2 の `Mercapi._search_impl` は `self._client.send()` の戻りの
    status を見ずに `res.json()` → マッピングするため、403/429 は本文が HTML なら
    JSONDecodeError、JSON なら ParseAPIResponseError になり **status を持たない**。
    それでは「ブロック」を検知できないので、httpx の response イベントフックで
    status を見てこの例外に変換する（H1）。
    """

    def __init__(self, status, url=None):
        self.status = int(status)
        self.status_code = self.status  # _http_status_of からも拾えるように
        self.url = url
        super().__init__("メルカリ側にブロックされました(HTTP {})".format(self.status))


MERCAPI_CLIENT_ATTR = "_client"


def install_block_hook(mercapi_instance):
    """mercapi 内部の httpx.AsyncClient に response フックを足す（H1）。

    - 403 / 429 → MercariBlockedError（即時中断させる）
    - 5xx はここでは何もしない（従来どおり解析エラー＝fetch_error として扱う）
    戻り値: 登録できたら True。内部属性が変わっていたら False（selftest で警告する）。
    """
    client = getattr(mercapi_instance, MERCAPI_CLIENT_ATTR, None)
    hooks = getattr(client, "event_hooks", None)
    if client is None or not isinstance(hooks, dict):
        return False

    async def _on_response(response):
        status = int(getattr(response, "status_code", 0) or 0)
        if status in BLOCKED_STATUSES:
            raise MercariBlockedError(status, str(getattr(response, "url", "") or ""))

    try:
        merged = dict(hooks)
        merged["request"] = list(merged.get("request") or [])
        merged["response"] = list(merged.get("response") or []) + [_on_response]
        client.event_hooks = merged
    except Exception:  # noqa: BLE001 - httpx の内部仕様変更に耐える
        return False
    return bool((getattr(client, "event_hooks", {}) or {}).get("response"))


def hook_support_status():
    """selftest 用: mercapi 内部クライアントへフックを張れるかを実機確認する（KR4）。

    戻り値: (ok: bool, detail: str)。ネットワークは使わない。
    """
    try:
        from mercapi import Mercapi
    except Exception as e:  # noqa: BLE001
        return False, "mercapi を import できない: {}".format(e.__class__.__name__)
    try:
        instance = Mercapi()
    except Exception as e:  # noqa: BLE001
        return False, "Mercapi() を生成できない: {}".format(e.__class__.__name__)
    client = getattr(instance, MERCAPI_CLIENT_ATTR, None)
    if client is None:
        return False, "内部クライアント属性 {} が無い（mercapi の仕様変更）".format(
            MERCAPI_CLIENT_ATTR)
    if not install_block_hook(instance):
        return False, "response フックを登録できない（httpx の仕様変更）"
    return True, "{}.event_hooks['response'] に登録成功".format(MERCAPI_CLIENT_ATTR)


class MercapiAdapter:
    """実 mercapi を遅延importして売切検索する本番アダプタ。

    検索ごとに Mercapi を作り直す（asyncio.run が毎回新しいループを作るため）。
    作った直後に install_block_hook() で 403/429 検出フックを張る。
    """

    def __init__(self):
        self.hook_installed = None  # 直近の検索でフックを張れたか（None=未実施）

    def search(self, query):
        from mercapi import Mercapi  # 遅延import（未インストールは例外→取得失敗として扱う）
        from mercapi.requests.search import SearchRequestData

        adapter = self

        async def _run():
            mc = Mercapi()
            adapter.hook_installed = install_block_hook(mc)
            res = await mc.search(query, status=[SearchRequestData.Status.STATUS_SOLD_OUT])
            return list(res.items)

        return asyncio.run(_run())


class MercariState:
    """メルカリ照会の中断状態と連続失敗数を**実行全体で**共有する（H2）。

    カテゴリ別に持つと、403 を受けた後に別カテゴリがまた照会してしまう。
    """

    def __init__(self, max_consecutive=MAX_CONSECUTIVE_FAILURES):
        self.max_consecutive = int(max_consecutive)
        self.blocked = False
        self.blocked_status = None
        self.aborted = False
        self.last_error = None
        self.consecutive_failures = 0
        self.fetch_failures = 0

    def record_success(self):
        self.consecutive_failures = 0

    def record_failure(self, error_text):
        self.fetch_failures += 1
        self.consecutive_failures += 1
        self.last_error = error_text
        if self.consecutive_failures >= self.max_consecutive:
            self.aborted = True
        return self.consecutive_failures

    def record_blocked(self, status):
        self.blocked = True
        self.blocked_status = int(status)
        self.aborted = True

    def stopped(self):
        return self.blocked or self.aborted


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

    def __init__(self, profile, cache, adapter=None, budget=None, throttle_fn=None,
                 state=None):
        self.profile = profile or {}
        self.cache = cache
        self.adapter = adapter if adapter is not None else MercapiAdapter()
        self.budget = budget
        self.throttle_fn = throttle_fn or throttle_module.throttle
        # 中断状態・連続失敗数は実行全体で共有する（H2）。省略時は自前で持つ。
        self.state = state if state is not None else MercariState()
        try:
            self.min_interval = float(self.profile.get("mercariMinIntervalSec", 2.5))
        except (TypeError, ValueError):
            self.min_interval = 2.5
        self.requests_made = 0
        self.http_successes = 0
        self.valid_comps = 0
        self.cache_hits = 0
        self.budget_exhausted = False

    # 後方互換（旧名）。意味は「有効相場の件数」。
    @property
    def successes(self):
        return self.valid_comps

    # 中断状態は共有 state を参照する（カテゴリをまたいで効く）
    @property
    def aborted(self):
        return self.state.aborted

    @property
    def blocked(self):
        return self.state.blocked

    @property
    def blocked_status(self):
        return self.state.blocked_status

    @property
    def last_error(self):
        return self.state.last_error

    @property
    def fetch_failures(self):
        return self.state.fetch_failures

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

        # 中断状態は共有（H2）。403のあとは別カテゴリの候補も照会しない。
        if self.state.blocked:
            return self._fail(
                candidate, "blocked",
                "メルカリ側にブロックされ照会中断(HTTP {})".format(self.state.blocked_status),
                first_query)
        if self.state.aborted:
            return self._fail(
                candidate, "aborted",
                self.state.last_error or "連続失敗により照会中断", first_query)

        for level, query in attempts:
            # M: キャッシュ確認を**予算判定より先に**行う（キャッシュ命中は予算を消費せず、
            # budget_exhausted にもしない）。
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
                error_text = "{}: {}".format(e.__class__.__name__, str(e)[:120])
                status = e.status if isinstance(e, MercariBlockedError) else _http_status_of(e)
                if status in BLOCKED_STATUSES:
                    self.state.fetch_failures += 1
                    self.state.last_error = error_text
                    self.state.record_blocked(status)
                    return self._fail(
                        candidate, "blocked",
                        "メルカリ側にブロックされました(HTTP {})".format(status), first_query)
                consecutive = self.state.record_failure(error_text)
                if self.state.aborted:
                    return self._fail(
                        candidate, "aborted",
                        "{}回連続で失敗し照会中断({})".format(consecutive, error_text),
                        first_query)
                return self._fail(candidate, "fetch_error", error_text, first_query)
            self.state.record_success()
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
