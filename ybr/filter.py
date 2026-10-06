# -*- coding: utf-8 -*-
"""ybr.filter: 一次フィルタ（NG語・総額レンジ・残り時間・重複）。

NG語照合は必ず normalize_for_match() を両辺に通す（NFKC + 小文字化）。
半角カナ「ﾚﾌﾟﾘｶ」・全角英字「ＧＰ」での迂回を塞ぐため。

さらに、
- 2〜4文字の ASCII 語（gp / gf 等）は**単語境界付き**で照合する（"GPS" を誤爆させない）。
- allowPhrases（「GPではありません」等）と**文字位置が重なる**NG出現は除外しない
  （否定表現の救済。出現ごとに判定するので「GPではありません。GP刻印あり」は除外される）。
"""
from __future__ import annotations

import re
import unicodedata

_ASCII_SHORT = re.compile(r"^[a-z0-9]{1,4}$")


def normalize_for_match(text):
    """照合用の正規化: NFKC（全角→半角・半角カナ→全角カナ）→ 小文字化。"""
    if not text:
        return ""
    return unicodedata.normalize("NFKC", str(text)).lower()


def _spans_of_word(haystack, word):
    """word の出現位置 (start, end) を列挙する。短いASCII語は単語境界付き。"""
    w = normalize_for_match(word)
    if not w:
        return []
    if _ASCII_SHORT.match(w):
        pattern = r"(?<![0-9a-z])" + re.escape(w) + r"(?![0-9a-z])"
        return [(m.start(), m.end()) for m in re.finditer(pattern, haystack)]
    spans = []
    pos = haystack.find(w)
    while pos != -1:
        spans.append((pos, pos + len(w)))
        pos = haystack.find(w, pos + 1)
    return spans


def _spans_of_pattern(haystack, pattern):
    try:
        rx = re.compile(pattern)
    except re.error:
        return []
    return [(m.start(), m.end()) for m in rx.finditer(haystack)]


def _allow_spans(haystack, allow_phrases):
    spans = []
    for phrase in allow_phrases or ():
        spans.extend(_spans_of_word(haystack, phrase))
    return spans


def _covered(span, allow_spans):
    s, e = span
    for a_s, a_e in allow_spans:
        if s < a_e and a_s < e:  # 文字位置が重なる
            return True
    return False


def ng_hit(title, ng_words, allow_phrases=(), ng_patterns=()):
    """NG語に該当すれば「該当した語」を返す。該当しなければ None。"""
    hay = normalize_for_match(title)
    if not hay:
        return None
    allowed = _allow_spans(hay, allow_phrases)
    for word in ng_words or ():
        for span in _spans_of_word(hay, word):
            if not _covered(span, allowed):
                return word
    for pattern in ng_patterns or ():
        for span in _spans_of_pattern(hay, pattern):
            if not _covered(span, allowed):
                return hay[span[0]:span[1]]
    return None


def caution_hits(title, caution_words):
    """注意語（除外しない）のうち該当したものを列挙する。"""
    hay = normalize_for_match(title)
    if not hay:
        return []
    out = []
    for word in caution_words or ():
        if _spans_of_word(hay, word):
            out.append(word)
    return out


def total_cost_estimate(candidate, profile):
    """総額（現在価格 + 仕入送料）。送料未定はペナルティ額で見積る。"""
    price = int(candidate.get("price") or 0)
    postage = candidate.get("postage")
    if postage is None:
        postage = int(profile.get("unknownShippingPenaltyYen", 1000))
    return price + int(postage)


def prefilter(items, profile, history=None, now=None):
    """一次フィルタ。(kept, excluded) を返す。

    - kept は「終了が近い順」。profile["maxCandidates"] 件で打ち切る
      （メルカリ照会コストを抑えるため。打ち切り分は excluded に理由付きで残す）。
    - excluded の各要素には excluded_reason を必ず入れる（0件の理由を説明できるように）。
    """
    del now  # 残り時間は minutes_remaining（取得時刻基準）で判定するため未使用
    min_total = int(profile.get("minTotalYen", 0))
    max_total = int(profile.get("maxTotalYen", 10 ** 9))
    min_minutes = int(profile.get("minMinutesRemaining", 0))
    max_minutes = int(float(profile.get("maxHoursRemaining", 72)) * 60)
    ng_words = profile.get("ngWords") or []
    ng_patterns = profile.get("ngPatterns") or []
    allow_phrases = profile.get("allowPhrases") or []
    caution_words = profile.get("cautionWords") or []

    kept = []
    excluded = []
    seen_ids = set()

    for item in items or []:
        c = dict(item)
        c.setdefault("category", profile.get("category"))
        aid = c.get("auction_id")

        # 同一実行内はオークションIDだけで重複判定する。
        # タイトル一致では弾かない（別出品者の同名出品は「別の仕入れ機会」なので落とすと機会損失）。
        if aid and aid in seen_ids:
            c["excluded_reason"] = "同一実行内の重複(オークションID)"
            c["excluded_kind"] = "重複(同一実行内)"
            excluded.append(c)
            continue

        minutes = c.get("minutes_remaining")
        if minutes is None:
            c["excluded_reason"] = "残り時間不明(終了時刻が取れない)"
            c["excluded_kind"] = "残り時間不明"
            excluded.append(c)
            continue
        if int(minutes) < min_minutes:
            c["excluded_reason"] = "締切が近すぎる(残り{}分 < {}分)".format(
                int(minutes), min_minutes)
            c["excluded_kind"] = "締切が近すぎる(残り{}分未満)".format(min_minutes)
            excluded.append(c)
            continue
        if int(minutes) > max_minutes:
            c["excluded_reason"] = "終了まで遠い(残り{}時間 > {}時間)".format(
                int(minutes) // 60, max_minutes // 60)
            c["excluded_kind"] = "終了まで遠い({}時間超)".format(max_minutes // 60)
            excluded.append(c)
            continue

        hit = ng_hit(c.get("title"), ng_words, allow_phrases, ng_patterns)
        if hit:
            c["excluded_reason"] = "NG語に該当({})".format(hit)
            c["excluded_kind"] = "NG語に該当"
            excluded.append(c)
            continue

        total = total_cost_estimate(c, profile)
        c["total_cost"] = total
        if total < min_total or total > max_total:
            c["excluded_reason"] = "総額レンジ外({:,}円 / {:,}〜{:,}円)".format(
                total, min_total, max_total)
            c["excluded_kind"] = "総額レンジ外({:,}〜{:,}円)".format(min_total, max_total)
            excluded.append(c)
            continue

        if history is not None and history.is_duplicate(c):
            c["excluded_reason"] = "直近{}日で既に報告済み(重複)".format(
                int(profile.get("dedupWindowDays", 7)))
            c["excluded_kind"] = "過去に報告済み(重複)"
            excluded.append(c)
            continue

        c["cautions"] = caution_hits(c.get("title"), caution_words)
        if aid:
            seen_ids.add(aid)
        kept.append(c)

    kept.sort(key=lambda c: int(c.get("minutes_remaining") or 10 ** 9))

    limit = int(profile.get("maxCandidates", 40))
    if len(kept) > limit:
        for c in kept[limit:]:
            c["excluded_reason"] = "候補数上限({}件)で打ち切り".format(limit)
            c["excluded_kind"] = "候補数上限で打ち切り"
            excluded.append(c)
        kept = kept[:limit]

    return kept, excluded
