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


class BlockedError(RuntimeError):
    """403 / 429 でブロックされた（即時停止してよい）。"""

    def __init__(self, status, url=None):
        self.status = int(status)
        self.url = url
        super().__init__("ヤフオク取得がブロックされました(HTTP {})".format(self.status))


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


def default_fetcher(url, retries=3, spend=None, throttle_fn=None):
    """urllib + Chrome偽装UA + gzip展開。各試行前に throttle（2.5〜4.0秒ジッタ）。

    spend: callable(n)->bool。リトライ（2回目以降の実HTTP試行）を予算計上する。
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
            break
        th(random.uniform(POLITE_MIN, POLITE_MAX))
        try:
            with urllib.request.urlopen(req, timeout=25) as r:
                data = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    data = gzip.decompress(data)
            return data.decode("utf-8", "ignore")
        except urllib.error.HTTPError as e:
            if int(getattr(e, "code", 0)) in BLOCKED_STATUS:
                raise BlockedError(e.code, url)
            last_err = e
        except Exception as e:  # noqa: BLE001 - 通信系を広く拾ってリトライ
            last_err = e
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


def search_keyword(keyword, pages=1, per=100, fetcher=None, spend=None, now=None):
    """1キーワードを検索し (候補リスト, 警告 or None) を返す。

    - fetcher(url)->str(HTML)。省略時は default_fetcher。
    - 1ページ目の失敗は例外をそのまま送出（403/429 は BlockedError）。
    - ページは取れたのにパース0件なら warning="構造変化の疑い: <keyword>"。
    """
    if fetcher is not None:
        fetch = fetcher
    else:
        def fetch(url):
            return default_fetcher(url, spend=spend)

    items = []
    seen_ids = set()
    pages_fetched = 0
    for page in range(max(1, int(pages))):
        url = build_search_url(keyword, start=page * per + 1, per=per)
        try:
            page_html = fetch(url)
        except urllib.error.HTTPError as e:
            # 注入された fetcher が素の HTTPError を投げても BlockedError に正規化する
            # （403/429 を「ただの失敗」として連続リトライしないため）
            if int(getattr(e, "code", 0)) in BLOCKED_STATUS:
                raise BlockedError(e.code, url)
            if pages_fetched == 0:
                raise
            break
        except Exception:
            if pages_fetched == 0:
                raise
            break  # 取得済み分は活かす
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
