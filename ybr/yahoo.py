# -*- coding: utf-8 -*-
"""ybr.yahoo: ヤフオク検索の取得とHTML解析（標準ライブラリのみ）。

accessory-profit-scout の scout/yahoo.py を移植・簡素化した。
- 検索URL: https://auctions.yahoo.co.jp/search/search?p=<kw>&va=<kw>&n=100&b=<start>&exflg=1
- 既定 fetcher は urllib + Chrome偽装UA + gzip展開 + throttle（Python-urllib 既定UAは 403 になる）
- ストア判定: auction_id の先頭が数字（個人出品IDは英字始まり）
- 403 / 429 は BlockedError にして**即時停止**（結果を創作しない）
"""
from __future__ import annotations

import gzip
import html
import random
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from .throttle import throttle

JST = timezone(timedelta(hours=9))

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

SEARCH_BASE = "https://auctions.yahoo.co.jp/search/search"
AUCTION_URL = "https://page.auctions.yahoo.co.jp/jp/auction/"

POLITE_MIN = 2.5
POLITE_MAX = 4.0
BLOCKED_STATUS = (403, 429)

_VALID_ID = re.compile(r"^[A-Za-z0-9]+$")
_CTRL_RE = re.compile("[\\x00-\\x1f\\x7f\\u2028\\u2029]")


MAX_CONSECUTIVE_FAILURES = 3


class BlockedError(RuntimeError):
    """403 / 429 でブロックされた（即時停止してよい）。"""

    def __init__(self, status, url=None):
        self.status = int(status)
        self.url = url
        super().__init__("ヤフオク取得がブロックされました(HTTP {})".format(self.status))


class BudgetExhausted(RuntimeError):
    """リクエスト予算が尽きて1回も送信できなかった（取得失敗とは区別する）。"""


class ConsecutiveFailureError(RuntimeError):
    """HTTP試行が連続で失敗した（結果を創作せず止めるためのシグナル）。"""

    def __init__(self, consecutive, last_error=None):
        self.consecutive = int(consecutive)
        self.last_error = last_error
        super().__init__(
            "ヤフオク取得が{}回連続で失敗しました（{}）".format(
                self.consecutive, last_error or "原因不明"))


class FailureTracker:
    """HTTP**試行**単位で連続失敗を数える（検索語単位で数えると実質9回続くため・m22）。

    default_fetcher と CLI がこの1つのインスタンスを共有する。
    """

    def __init__(self, limit=MAX_CONSECUTIVE_FAILURES):
        self.limit = int(limit)
        self.consecutive = 0
        self.total = 0
        self.last_error = None

    def record_failure(self, error=None):
        self.consecutive += 1
        self.total += 1
        if error is not None:
            self.last_error = "{}: {}".format(error.__class__.__name__, str(error)[:120])
        return self.consecutive

    def record_success(self):
        self.consecutive = 0

    def exceeded(self):
        return self.consecutive >= self.limit


def sanitize_title(text):
    """制御文字・改行を空白に置換して1行に正規化する（レポートへの行注入対策）。"""
    if not text:
        return ""
    cleaned = _CTRL_RE.sub(" ", str(text))
    return re.sub(r"\s+", " ", cleaned).strip()


def now_jst():
    return datetime.now(JST)


def build_search_url(keyword, start=1, per=100):
    params = {"p": keyword, "va": keyword, "n": per, "b": start, "exflg": 1}
    return SEARCH_BASE + "?" + urllib.parse.urlencode(params)


def default_fetcher(url, retries=3, spend=None, throttle_fn=None, tracker=None):
    """urllib + Chrome偽装UA + gzip展開。各試行前に throttle（2.5〜4.0秒ジッタ）。

    spend: callable(n)->bool。リトライ（2回目以降の実HTTP試行）を予算計上する
           （1回目の分はページ単位で search_keyword が計上する・M6）。
    tracker: FailureTracker。HTTP試行単位で連続失敗を数え、上限で
             ConsecutiveFailureError を投げる（m22）。
    403 / 429 はリトライせず BlockedError を送出する。
    """
    th = throttle_fn or throttle
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept-Language": "ja,en;q=0.9",
            "Accept-Encoding": "gzip",
        },
    )
    last_err = None
    for attempt in range(max(1, int(retries))):
        if attempt > 0 and spend is not None and not spend(1):
            break  # リトライ予算切れ → 実リクエストを増やさない
        th(random.uniform(POLITE_MIN, POLITE_MAX))
        try:
            with urllib.request.urlopen(req, timeout=25) as r:
                data = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    data = gzip.decompress(data)
        except urllib.error.HTTPError as e:
            if int(getattr(e, "code", 0)) in BLOCKED_STATUS:
                raise BlockedError(e.code, url)
            last_err = e
        except Exception as e:  # noqa: BLE001 - 通信系を広く拾ってリトライ
            last_err = e
        else:
            if tracker is not None:
                tracker.record_success()
            return data.decode("utf-8", "ignore")
        if tracker is not None:
            tracker.record_failure(last_err)
            if tracker.exceeded():
                raise ConsecutiveFailureError(tracker.consecutive, tracker.last_error)
    raise last_err if last_err is not None else RuntimeError("取得に失敗しました")


def _wrap_text(block, cls):
    m = re.search(r'class="' + cls + r'">(.*?)</dd>', block, re.S)
    if not m:
        m = re.search(r'class="' + cls + r'">(.*?)</div>', block, re.S)
    if not m:
        return ""
    txt = re.sub(r"<[^>]+>", " ", m.group(1))
    return re.sub(r"\s+", " ", txt).strip()


def time_to_minutes(text):
    """「1日」「12時間」「5分」「3日12時間」などを概算の分に変換。取れなければ None。"""
    if not text:
        return None
    total = 0
    matched = False
    for value, unit in re.findall(r"(\d+)\s*(日|時間|分|秒)", text):
        matched = True
        v = int(value)
        if unit == "日":
            total += v * 1440
        elif unit == "時間":
            total += v * 60
        elif unit == "分":
            total += v
        else:
            total += 1
    return total if matched else None


def _parse_postage(block):
    """送料テキストから金額(int)。無料=0、未定/不明=None。"""
    m = re.search(r'Product__postage[^>]*>(.*?)</', block, re.S)
    if not m:
        return None
    txt = re.sub(r"<[^>]+>", "", m.group(1))
    if "無料" in txt:
        return 0
    mm = re.search(r"送料\s*([0-9,]+)\s*円", txt)
    if mm:
        return int(mm.group(1).replace(",", ""))
    if "0円" in txt:
        return 0
    return None


def parse_items(page_html, keyword, now=None):
    """検索結果HTMLから候補レコード(dict)のリストを返す。

    価格が現在/即決とも取れない商品、ID が英数字でない商品はスキップする。
    """
    now = now or now_jst()
    items = []
    for block in re.split(r'(?=<li class="Product">)', page_html or ""):
        if not block.startswith('<li class="Product">'):
            continue
        end = block.find("</li>")
        if end != -1:
            block = block[: end + 5]

        m_id = re.search(r'data-auction-id="([^"]*)"', block)
        if not m_id:
            continue
        aid = m_id.group(1)
        if not _VALID_ID.match(aid):
            continue  # 不正なID（属性インジェクション）は弾く

        m_title = re.search(r'data-auction-title="([^"]*)"', block)
        title = sanitize_title(html.unescape(m_title.group(1))) if m_title else ""

        current_price = None
        buynow_price = None
        for pm in re.finditer(
            r'Product__price">\s*<span class="Product__label">(現在|即決)</span>'
            r'\s*<span class="Product__priceValue[^"]*">([\d,]+)円',
            block,
        ):
            val = int(pm.group(2).replace(",", ""))
            if pm.group(1) == "現在":
                current_price = val
            else:
                buynow_price = val
        price = current_price if current_price is not None else buynow_price
        if price is None:
            continue  # 価格不明の商品は候補にしない

        bid_text = _wrap_text(block, "Product__bidWrap")
        m_bid = re.search(r"\d+", bid_text)
        bids = int(m_bid.group(0)) if m_bid else 0

        minutes = time_to_minutes(_wrap_text(block, "Product__timeWrap"))
        end_at = (now + timedelta(minutes=minutes)).isoformat() if minutes is not None else None

        fm = re.search(r'data-auction-isfreeshipping="([^"]*)"', block)
        free_shipping = bool(fm and fm.group(1) not in ("", "0"))
        postage = 0 if free_shipping else _parse_postage(block)

        m_img = re.search(r'data-auction-img="([^"]*)"', block)
        image = html.unescape(m_img.group(1)) if m_img and m_img.group(1) else None

        items.append({
            "auction_id": aid,
            "title": title,
            "url": AUCTION_URL + aid,
            "image": image,
            "price": price,
            "buynow": buynow_price,
            "postage": postage,
            "bids": bids,
            "is_store": aid[0].isdigit(),
            "end_at": end_at,
            "minutes_remaining": minutes,
            "keyword": keyword,
            "category": None,
            "total_cost": None,
            "comps": None,
            "mercari_url": None,
            "profit": None,
            "cautions": [],
            "excluded_reason": None,
        })
    return items


def search_keyword(keyword, pages=1, per=100, fetcher=None, spend=None, now=None,
                   tracker=None):
    """1キーワードを検索し (候補リスト, 警告 or None) を返す。

    - fetcher(url)->str(HTML)。省略時は default_fetcher。
    - **全ページの初回HTTPを spend(1) で予算計上する**（M6。リトライ分は fetcher 側）。
      1ページ目の分も計上するので、呼び出し側は事前計上しないこと。
    - **BlockedError はページ位置に関係なく再送出する**（C2。2ページ目以降で握り潰すと
      403/429 のあとも次の検索語を取得し続けてしまう）。
    - ConsecutiveFailureError も同様に再送出する（m22）。
    - ページは取れたのにパース0件なら warning="構造変化の疑い: <keyword>"。
    """
    if fetcher is not None:
        fetch = fetcher
    else:
        def fetch(url):
            return default_fetcher(url, spend=spend, tracker=tracker)

    items = []
    seen_ids = set()
    pages_fetched = 0
    for page in range(max(1, int(pages))):
        if spend is not None and not spend(1):
            if pages_fetched == 0:
                raise BudgetExhausted(
                    "リクエスト予算が尽きたため「{}」を取得できませんでした".format(keyword))
            break  # 2ページ目以降は取得済み分を活かす
        url = build_search_url(keyword, start=page * per + 1, per=per)
        try:
            page_html = fetch(url)
        except (BlockedError, ConsecutiveFailureError):
            raise  # ページ位置に関係なく止める（C2 / m22）
        except urllib.error.HTTPError as e:
            # 注入された fetcher が素の HTTPError を投げても BlockedError に正規化する
            if int(getattr(e, "code", 0)) in BLOCKED_STATUS:
                raise BlockedError(e.code, url)
            if tracker is not None:
                tracker.record_failure(e)
                if tracker.exceeded():
                    raise ConsecutiveFailureError(tracker.consecutive, tracker.last_error)
            if pages_fetched == 0:
                raise
            break
        except Exception as e:  # noqa: BLE001 - 通信断等
            if tracker is not None:
                tracker.record_failure(e)
                if tracker.exceeded():
                    raise ConsecutiveFailureError(tracker.consecutive, tracker.last_error)
            if pages_fetched == 0:
                raise
            break  # 取得済み分は活かす
        if tracker is not None:
            tracker.record_success()
        pages_fetched += 1
        page_items = parse_items(page_html, keyword, now=now)
        if not page_items:
            break
        for it in page_items:
            if it["auction_id"] in seen_ids:
                continue
            seen_ids.add(it["auction_id"])
            items.append(it)

    warning = None
    if pages_fetched > 0 and not items:
        warning = "構造変化の疑い: {}".format(keyword)
    return items, warning
