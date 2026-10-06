# -*- coding: utf-8 -*-
"""ybr.filter: 一次フィルタ（NG語・総額レンジ・残り時間・重複）。

NG語照合は必ず normalize_for_match() を両辺に通す（NFKC + 小文字化）。
半角カナ「ﾚﾌﾟﾘｶ」・全角英字「ＧＰ」での迂回を塞ぐため。

さらに、
- 2〜4文字の ASCII 語（gp / gf 等）は**単語境界付き**で照合する（"GPS" を誤爆させない）。
- K18GP / SV925GF のような**くっついた品位+めっき表記**は ngPatterns の正規表現で拾う（M5）。
- allowPhrases（「GPではありません」等）に**完全に含まれる**NG出現だけ救済する（M13）。
  部分的に重なるだけで救済すると「スーパーコピーライト」が通ってしまう。
- カタカナNG語は、区切り文字（空白・ハイフン・中黒・アンダースコア）を抜いた文字列でも
  照合する（「レ プ リ カ」対策・Codex#5）。長音「ー」は意味が変わるので抜かない。
- 「風」「タイプ」は単独ではNGにしない。**ブランド名/ライン名の直後に来る形**だけNGにする
  （「ティファニー風」はNG、「春風コレクション」「Aタイプ」は通す・M23）。
"""
from __future__ import annotations

import re
import unicodedata

_ASCII_SHORT = re.compile(r"^[a-z0-9]{1,4}$")
_KATAKANA_RE = re.compile(r"[ァ-ヶ]")
# 区切り挿入による迂回対策で取り除く文字（長音「ー」は含めない）
_SEPARATOR_RE = re.compile(r"[\s　\-‐‑‒–—－_・･.,/]")
# ブランド名とサフィックス（風/タイプ）の間に挟まれても同一視する区切り
_BRAND_GAP_RE = re.compile(r"^[\s　・\-_'\"]{0,3}")


def normalize_for_match(text):
    """照合用の正規化: NFKC（全角→半角・半角カナ→全角カナ）→ 小文字化。"""
    if not text:
        return ""
    return unicodedata.normalize("NFKC", str(text)).lower()


def strip_separators(text):
    """区切り文字を抜いた文字列（「レ プ リ カ」「G-P」対策）。"""
    return _SEPARATOR_RE.sub("", text or "")


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


def _allow_spans(haystack, allow_phrases, allow_patterns=()):
    """救済フレーズの出現位置。

    救済は「完全包含」判定なので（M13）、くっついた表記
    （K18GPではありません）を救うには**正規表現の救済**が必要になる。
    ngPatterns と対になる allowPatterns をここで扱う。
    """
    spans = []
    for phrase in allow_phrases or ():
        spans.extend(_spans_of_word(haystack, phrase))
    for pattern in allow_patterns or ():
        spans.extend(_spans_of_pattern(haystack, pattern))
    return spans


def _covered(span, allow_spans):
    """NG出現が許容フレーズに**完全に含まれる**ときだけ救済する（M13）。"""
    s, e = span
    for a_s, a_e in allow_spans:
        if a_s <= s and e <= a_e:
            return True
    return False


def _ng_hit_in(hay, ng_words, allow_phrases, ng_patterns, allow_patterns=()):
    """NG語/NGパターンの出現を**長いものから**判定する（監査#2）。

    短い語を先に見ると「スーパーコピーではありません」で `コピー` が救済され、
    そのあと `スーパーコピー` がNGになる（救済が効かない）。長い出現から見れば
    救済パターンが覆っているかを正しく判定できる。
    """
    allowed = _allow_spans(hay, allow_phrases, allow_patterns)
    occurrences = []
    for word in ng_words or ():
        for span in _spans_of_word(hay, word):
            occurrences.append((span[1] - span[0], span, word))
    for pattern in ng_patterns or ():
        for span in _spans_of_pattern(hay, pattern):
            occurrences.append((span[1] - span[0], span, hay[span[0]:span[1]]))
    # 長い出現を優先（同長なら出現順）
    occurrences.sort(key=lambda item: (-item[0], item[1][0]))
    for _length, span, label in occurrences:
        if not _covered(span, allowed):
            return label
    return None


def brand_suffix_hit(title, brand_terms, suffix_words, allow_phrases=(),
                     allow_patterns=()):
    """「<ブランド名|ライン名>風」「<ブランド名>タイプ」の形だけ検出する（M23）。

    単独の「風」「タイプ」は NG にしない（「春風コレクション」「Aタイプ」を通すため）。
    """
    hay = normalize_for_match(title)
    if not hay or not brand_terms or not suffix_words:
        return None
    allowed = _allow_spans(hay, allow_phrases, allow_patterns)
    suffixes = [normalize_for_match(s) for s in suffix_words if s]
    for term in brand_terms or ():
        t = normalize_for_match(term)
        if len(t) < 2:
            continue
        for start, end in _spans_of_word(hay, t):
            tail = hay[end:end + 8]
            gap = _BRAND_GAP_RE.match(tail)
            offset = gap.end() if gap else 0
            rest = tail[offset:]
            for suf in suffixes:
                if suf and rest.startswith(suf):
                    span = (start, end + offset + len(suf))
                    if not _covered(span, allowed):
                        return hay[span[0]:span[1]]
    return None


def ng_hit(title, ng_words, allow_phrases=(), ng_patterns=(),
           brand_terms=(), brand_suffix_words=(), allow_patterns=()):
    """NG語に該当すれば「該当した語」を返す。該当しなければ None。"""
    hay = normalize_for_match(title)
    if not hay:
        return None

    hit = _ng_hit_in(hay, ng_words, allow_phrases, ng_patterns, allow_patterns)
    if hit:
        return hit

    # 区切り挿入による迂回対策（「レ プ リ カ」「コ・ピー」・Codex#5）。
    # 救済（allowPhrases / allowPatterns）も**同じ** stripped 文字列に対して適用する
    # （渡し忘れると「ティファニー レプリカじゃない」が除外される・Codex r2）。
    kana_ng = [w for w in (ng_words or ()) if _KATAKANA_RE.search(str(w))]
    if kana_ng:
        hay_nosep = strip_separators(hay)
        if hay_nosep != hay:
            hit = _ng_hit_in(
                hay_nosep,
                [strip_separators(normalize_for_match(w)) for w in kana_ng],
                [strip_separators(normalize_for_match(p)) for p in (allow_phrases or ())],
                (),
                allow_patterns=allow_patterns,  # 正規表現は \s* 任意なので素のまま使える
            )
            if hit:
                return hit

    return brand_suffix_hit(title, brand_terms, brand_suffix_words,
                            allow_phrases, allow_patterns)


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
    brand_terms = profile.get("brandTerms") or []
    brand_suffix_words = profile.get("brandSuffixNgWords") or []
    allow_patterns = profile.get("allowPatterns") or []

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

        hit = ng_hit(c.get("title"), ng_words, allow_phrases, ng_patterns,
                     brand_terms=brand_terms, brand_suffix_words=brand_suffix_words,
                     allow_patterns=allow_patterns)
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
