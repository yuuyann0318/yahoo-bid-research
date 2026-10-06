# -*- coding: utf-8 -*-
"""ybr.profiles: カテゴリ別プロファイルと検索語ファイルの読み込み。

- プロファイルの既定値はこのファイルに持つ（profiles/*.json が無くても動く）。
- profiles/<category>.json があれば**上書きマージ**する（1階層のフラットな上書き）。
- 既定値はすべて「本人未承認の初期値」。送料・NG語・ブランド語は運用で調整する前提。

カテゴリは accessory / apparel の2つだけ（1責務＝この2カテゴリのリサーチ）。
"""
from __future__ import annotations

import json
import os
import unicodedata

CATEGORIES = ("accessory", "apparel")

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# --- 共通の既定値 -------------------------------------------------------------
BASE_DEFAULTS = {
    # 利益計算
    "mercariFeeRate": 0.10,            # メルカリ販売手数料10%
    "unknownShippingPenaltyYen": 1000,  # ヤフオク送料未定のペナルティ
    "minProfitYen": 2000,              # 目標利益の下限（円）
    "minBidYen": 1000,                 # これ未満の提案額は採用しない
    "targetMarginPct": 30.0,           # 目標利益率（対売上・%）
    # 一次フィルタ
    "minTotalYen": 3000,
    "maxTotalYen": 150000,
    "minMinutesRemaining": 60,         # 残り60分未満は間に合わないので外す
    "maxHoursRemaining": 72,
    "dedupWindowDays": 7,
    "maxCandidates": 40,               # 1実行でメルカリ照会にかける候補の上限
    # メルカリ
    "mercariMinIntervalSec": 2.5,
    "cacheTtlHours": 24,
}

# アパレル・アクセ共通で相場クエリから落とすノイズ語
_COMMON_NOISE = [
    "美品", "極美品", "超美品", "新品", "未使用", "中古", "送料無料", "正規品", "本物",
    "即決", "即決価格", "スタート", "1円", "1スタ", "売切", "限定", "希少", "レア",
    "激安", "格安", "大特価", "定価", "入手困難", "完売", "人気", "最落なし",
    "ノークレーム", "ノーリターン", "匿名配送", "即日発送", "箱付き", "箱付", "保証書付き",
    "新品未使用", "未使用品", "中古美品", "送料込み", "送料込", "美品級", "美中古",
]

# 相場クエリから落とすトークン（両カテゴリ共通）。
# 「1円スタート」「1000円スタート」「1スタ」のような出品手法の語が
# メルカリ側の関連度検索に残ると、無関係な商品まで拾って相場がぶれる（実測で確認）。
_COMMON_DROP_TOKENS = [
    r"^\d[\d,]*円.*$",      # 1円スタート / 1000円〜 / 2980円即決
    r"^\d+スタート$",
    r"^\d+スタ$",
    r"^(size|サイズ)$",     # 「M size」の size だけ残るのを防ぐ
]
# ※ ASCII のブランド名（burberry / tiffany / b-zero1 等）を落とさないよう、
#    「英数字の羅列だから管理番号」という推測での除去はしない。

# 「風」「タイプ」は単独ではNGにしない。ブランド名/ライン名の直後に来る形だけNG（M23）。
# 「春風コレクション」「Aタイプ」を誤除外しないため。
_BRAND_SUFFIX_NG = ["風", "ふう", "タイプ", "調", "もどき", "style", "like"]

# 検索語ファイルの日本語ブランド名に対応する英語表記（Codex r2 Medium）。
# これが無いと「TIFFANY風」「GUCCIタイプ」「Cartier調」が素通りする。
# profiles/*.json の brandAliases で編集できる（キー=検索語ファイル内の表記）。
_BRAND_ALIASES = {
    "ティファニー": ["tiffany", "tiffany&co", "tiffany & co"],
    "カルティエ": ["cartier"],
    "ブルガリ": ["bvlgari", "bulgari"],
    "グッチ": ["gucci"],
    "エルメス": ["hermes"],
    "ジョージジェンセン": ["georg jensen", "georgjensen"],
    "ミキモト": ["mikimoto"],
    "4℃": ["yondoshi"],
    "バーバリー": ["burberry", "burberrys"],
    "モンクレール": ["moncler"],
    "カナダグース": ["canada goose", "canadagoose"],
    "ノースフェイス": ["the north face", "northface", "north face"],
    "パタゴニア": ["patagonia"],
    "アークテリクス": ["arcteryx", "arc'teryx"],
    "シュプリーム": ["supreme"],
    "ステューシー": ["stussy"],
    "ラルフローレン": ["ralph lauren", "ralphlauren", "polo ralph"],
    "トミーヒルフィガー": ["tommy hilfiger", "tommyhilfiger"],
    "コムデギャルソン": ["comme des garcons", "commedesgarcons"],
    "ヨウジヤマモト": ["yohji yamamoto", "yohjiyamamoto"],
    "イッセイミヤケ": ["issey miyake", "isseymiyake"],
    "メゾンマルジェラ": ["maison margiela", "margiela", "maison martin margiela"],
    "ストーンアイランド": ["stone island", "stoneisland"],
    "バブアー": ["barbour"],
    "リーバイス": ["levis", "levi's", "levi strauss"],
    "チャンピオン": ["champion"],
    "カーハート": ["carhartt"],
}

# 金属の品位表記（この後ろに GP/GF が来たら「めっき」と判断する）
_KARAT = r"(?:k\s*\d{1,2}|\d{1,2}\s*k|sv\s*\d{3}|925|750|585|417)"
# GP / GF の区切り挿入（「K18 G - P」「K18 G.P」「K18 G  F」まで拾う・Codex r3#5）。
_GSEP = r"[\s　\-・･.．]"
# 型番（GF-01 / GP-02）は めっき表記ではないので除外する（監査 A3）。
# 除外は**ハイフン直結＋数字だけ**に限る。区切りを挟んだ数字まで型番扱いすると
# 「K18GP 2.5g」「GP 2本セット」のような実在の表記を取り落とす（退行修正）。
_NOT_MODEL = r"(?![0-9a-z])(?!-\d)"
_GP_SEP = r"g{}{{0,3}}p{}".format(_GSEP, _NOT_MODEL)
_GF_SEP = r"g{}{{0,3}}f{}".format(_GSEP, _NOT_MODEL)

_PLATING_PATTERNS = [
    # 品位表記の直後の GP/GF（区切り挿入も許す）: K18GP / 18K GP / K18 G - P / SV925G.F
    r"{}{}{{0,3}}(?:{}|{})".format(_KARAT, _GSEP, _GP_SEP, _GF_SEP),
    r"\d{{1,2}}{}*金{}*(?:{}|{})".format(_GSEP, _GSEP, _GP_SEP, _GF_SEP),
    r"gold{}*(plated|filled)".format(_GSEP),
    # 単独語としての GP / GF（素の "gp" もここで拾う。GPS・G-SHOCK・GF-01 は拾わない）
    r"(?<![0-9a-z])(?:{}|{})".format(_GP_SEP, _GF_SEP),
]

# --- 否定表現の救済（M13 の完全包含判定を前提にした正規表現） ---
# 否定語そのもの
_NEG = (r"(?:ありません|ございません|なし|無し|ない|無い|なく|無く|"
        r"見当たりません|見られません)")
# 否定語の前後をつなぐ区切り（中黒も許す: 「ダメージ・ありません」・監査 A2）
_NSEP = r"[\s　・･]*"
# **否定を打ち消す後続**だけを拒否する（Codex r3#3/#4・監査#1）。
#   拒否: 「〜ないというわけではありません」「〜なくはない」「〜なしではありません」
#         「〜とは限らない／とは言えない」
#   許可: 「〜ありませんが、使用感はあります」「〜ないことを確認済み」
#        （`が` `こと` は否定を反転させないので拒否しない）
_NEG_REVERSAL = (
    r"(?!"
    r"{sep}(?:という|と言う)?{sep}(?:わけ|訳){sep}(?:では|じゃ)?{sep}"
    r"(?:ない|ありません|ございません|無い)"
    r"|{sep}とは{sep}(?:限らない|限りません|言えない|いえない|言えません)"
    r"|{sep}は{sep}(?:ない|無い|ありません|ございません)"
    r"|{sep}も{sep}(?:ない|無い|ありません|ございません)"
    r"|{sep}くはない"
    r"|{sep}(?:では|じゃ){sep}(?:ない|無い|ありません|ございません)"
    r")".format(sep=_NSEP)
)
_NEG_TAIL = _NEG + _NEG_REVERSAL
# 「〜ではありません」「〜じゃない」の前置き（品/商品/加工 を許す・監査#2 / Codex r3#6）
_NEG_JOIN = (r"{sep}(?:品|商品|加工|仕上げ|刻印|処理)?{sep}"
             r"(?:では|じゃ|で は){sep}").format(sep=_NSEP)

# くっついた表記（K18GPではありません）は語リストでは救えないのでパターンで救う。
_PLATING_NEGATION_PATTERNS = [
    r"(?:{}{}{{0,3}})?(?:gp|gf|{}|{}){}{}".format(
        _KARAT, _GSEP, _GP_SEP, _GF_SEP, _NEG_JOIN, _NEG_TAIL),
    # 「メッキ加工ではありません」も救済する（Codex r3#6）。
    # 「メッキ加工済み」は否定が続かないので引き続きNG。
    r"(?:金|銀)?{}*(?:メッキ|めっき)".format(_GSEP) + _NEG_JOIN + _NEG_TAIL,
]
# 模倣表現の否定。長いNG語（スーパーコピー）ごと覆えるよう接頭辞も含める（監査#2）。
_FAKE_NEGATION_PATTERNS = [
    r"(?:スーパー|ハイ|精巧な|精巧)?(?:コピー|レプリカ|偽物|模造|イミテーション)"
    + _NEG_JOIN + _NEG_TAIL,
]
# 「申し訳ありません」が「訳あり」に誤爆するのを防ぐ（監査#1）
_APOLOGY_PATTERNS = [r"申[しス]?訳(?:あり|ござい)"]

ACCESSORY_DEFAULTS = {
    "label": "アクセサリー",
    "sellShippingYen": 300,  # ネコポス160〜210円＋梱包の目安（初期値）
    # 除外語（該当したら候補から外す）。accessory-profit-scout の NG_WORDS を踏襲し、
    # めっき/GP/GF/ノベルティ/模倣表現を追加した。
    # 「風」「タイプ」は brandSuffixNgWords 側で扱う（単独では除外しない）。
    "ngWords": [
        "コピー", "スーパーコピー", "ノーブランド", "ジャンク",
        "まとめ売り", "まとめて", "セット売り", "部品取り", "偽物", "レプリカ",
        "模造", "イミテーション", "非正規", "自作", "ハンドメイド",
        "めっき", "メッキ", "ノベルティ", "破損", "石取れ",
        # GP / GF は ngPatterns 側で扱う（GF-01 のような型番を除外するため・監査A3）
    ],
    # 除外はしないが result.md の「注意」に出す語（accessory-profit-scout の DANGER_WORDS）
    "cautionWords": [
        "刻印なし", "傷あり", "キズあり", "変色", "くすみ", "付属品なし", "社外",
        "訳あり", "難あり",
    ],
    "brandSuffixNgWords": list(_BRAND_SUFFIX_NG),
    "brandAliases": dict(_BRAND_ALIASES),
    # 正規表現での除外（GP/GF の刻印表記ゆれ）
    "ngPatterns": list(_PLATING_PATTERNS),
    # 正規表現での救済（否定表現。完全包含判定のため語リストでは救えない）
    "allowPatterns": (list(_PLATING_NEGATION_PATTERNS) + list(_FAKE_NEGATION_PATTERNS)
                      + list(_APOLOGY_PATTERNS)),
    # NG語を打ち消す否定・正規表現（これに重なる出現は除外扱いにしない）
    # 否定表現（「GPではありません」等）は **allowPatterns 側だけ** で扱う。
    # ここに平文で置くと二重否定（「GPではないわけではない」）まで救済してしまう。
    "allowPhrases": [
        "ロジウムコーティング", "ロジウムメッキ", "まとめて購入", "まとめてお取引",
    ],
    "noiseWords": list(_COMMON_NOISE) + ["レディース", "メンズ", "ユニセックス"],
    # 相場クエリから落とすトークン（正規化後のトークン全体に対する正規表現）
    "dropTokenPatterns": list(_COMMON_DROP_TOKENS) + [
        r"^\d{1,2}号$", r"^号$", r"^\d{1,3}(cm|mm)$", r"^約\d+(cm|mm)?$",
    ],
}

APPAREL_DEFAULTS = {
    "label": "アパレル",
    "sellShippingYen": 850,  # 宅急便（本人のアパレル講義の数値・初期値）
    # リメイク＝原ブランドの売切相場と比較できず予想売値が構造的に過大になるので除外（M10）。
    # 虫食い・シミ(あり系)は売値が落ちるので除外。「シミ」単独は入れない（カシミヤ誤爆・M23）。
    "ngWords": [
        "コピー", "スーパーコピー", "レプリカ", "偽物", "模造", "イミテーション",
        "ノベルティ", "ジャンク", "難あり", "訳あり", "破れ", "穴あり", "汚れあり",
        "ダメージ", "まとめ売り", "まとめて", "セット売り", "部品取り", "ノーブランド",
        "リメイク", "虫食い", "虫喰い", "シミあり", "シミ有", "シミ多", "染み",
    ],
    # 「ダメージ少」等は ngWords「ダメージ」に先に当たるため到達不能（監査#3）。
    # ダメージ系の扱いは README の「NG語の扱いで知っておくこと」に記載。
    "cautionWords": [
        "色あせ", "色褪せ", "毛玉", "ほつれ", "擦れ", "スレ",
        "日焼け", "リペア", "補修", "アウトレット", "サイズ不明",
    ],
    "brandSuffixNgWords": list(_BRAND_SUFFIX_NG),
    "brandAliases": dict(_BRAND_ALIASES),
    # アパレルでも金具・付属パーツの「GP/GF/gold plated」は仕入れ対象外にする
    # （バッグの金具・ベルトのバックル等。accessory と同じ表記ゆれ対応）。
    "ngPatterns": list(_PLATING_PATTERNS),
    "allowPatterns": (list(_FAKE_NEGATION_PATTERNS) + list(_APOLOGY_PATTERNS)
                      + list(_PLATING_NEGATION_PATTERNS) + [
        r"(?:ダメージ|汚れ|破れ|穴|シミ|染み|虫食い|虫喰い|難|訳|使用感|キズ|傷)"
        + _NSEP + r"(?:は|も)?" + _NSEP + _NEG_TAIL,
    ]),
    # 否定表現（「ダメージなし」「汚れありません」等）は allowPatterns 側だけで扱う。
    "allowPhrases": [
        "ダメージ加工", "ヴィンテージ加工", "ビンテージ加工", "加工ダメージ",
        "ユーズド加工", "まとめて購入", "まとめてお取引",
        # 完全包含だけを救済する実装（M13）なので「スーパーコピーライト」は救済されない
        "コピーライト",
    ],
    "noiseWords": list(_COMMON_NOISE) + [
        "メンズ", "レディース", "男性用", "女性用", "ユニセックス", "キッズ",
        "新品同様", "美中古", "used", "USED",
    ],
    # サイズ表記・色名・状態語を相場クエリから落とす（ヒット数を確保するため）
    "dropTokenPatterns": list(_COMMON_DROP_TOKENS) + [
        # サイズ: S/M/L/XL/F/FREE 等（単独トークンのみ）
        r"^(xxs|xs|s|m|l|xl|xxl|xxxl|2xl|3xl|f|ff|free|フリー|フリーサイズ)$",
        # 数字サイズ 32〜52 / 〜号 / W32 / 28インチ
        r"^(3[2-9]|4\d|5[0-2])$", r"^\d{1,3}号$", r"^号$",
        r"^w\d{2}$", r"^\d{2}インチ$", r"^\d{2,3}(cm|センチ)$",
        # 色名
        r"^(ブラック|ホワイト|ネイビー|ベージュ|カーキ|グレー|グレイ|ブラウン|レッド|"
        r"ブルー|グリーン|イエロー|ピンク|パープル|オレンジ|キャメル|オリーブ|"
        r"チャコール|ワイン|モカ|アイボリー|黒|白|紺|茶|赤|青|緑|黄|灰|生成り)$",
    ],
}

CATEGORY_DEFAULTS = {
    "accessory": ACCESSORY_DEFAULTS,
    "apparel": APPAREL_DEFAULTS,
}


def build_profile(category, overrides=None):
    """カテゴリ既定値 + overrides のプロファイルを作る。未知カテゴリは ValueError。"""
    if category not in CATEGORY_DEFAULTS:
        raise ValueError(
            "未知のカテゴリ: {}（使えるのは {}）".format(category, " / ".join(CATEGORIES))
        )
    profile = dict(BASE_DEFAULTS)
    for key, value in CATEGORY_DEFAULTS[category].items():
        profile[key] = list(value) if isinstance(value, list) else value
    profile["category"] = category
    for key, value in (overrides or {}).items():
        profile[key] = value
    return profile


def profile_path(category, base_dir=None):
    root = base_dir or PROJECT_ROOT
    return os.path.join(root, "profiles", "{}.json".format(category))


def load_profile(category, base_dir=None, overrides=None):
    """profiles/<category>.json を既定値へ上書きマージして返す。

    - ファイルが無ければ既定値のみ（エラーにしない）。
    - JSON が壊れていれば ValueError（黙って既定値で走らない＝設定ミスを隠さない）。
    """
    if category not in CATEGORY_DEFAULTS:
        raise ValueError(
            "未知のカテゴリ: {}（使えるのは {}）".format(category, " / ".join(CATEGORIES))
        )
    path = profile_path(category, base_dir)
    file_over = {}
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                file_over = json.load(f)
        except ValueError as e:
            raise ValueError("プロファイルJSONが壊れています: {} ({})".format(path, e))
        except OSError as e:
            raise ValueError("プロファイルJSONが読めません: {} ({})".format(path, e))
        if not isinstance(file_over, dict):
            raise ValueError("プロファイルJSONの形式が不正（オブジェクトではない）: {}".format(path))
    merged = dict(file_over)
    merged.update(overrides or {})
    profile = build_profile(category, merged)
    if not profile.get("brandTerms"):
        # 「<ブランド名|ライン名>風」判定用の語。検索語ファイル + 英語別名から作る（M23）。
        profile["brandTerms"] = brand_terms(
            category, base_dir=base_dir, aliases=profile.get("brandAliases"))
    return profile


def keywords_path(category, base_dir=None):
    root = base_dir or PROJECT_ROOT
    return os.path.join(root, "keywords", "{}.txt".format(category))


def parse_keyword_lines(text):
    """1行1検索語。`#` 始まりと空行は無視。重複は先着で除去。"""
    out = []
    seen = set()
    for raw in (text or "").split("\n"):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line in seen:
            continue
        seen.add(line)
        out.append(line)
    return out


def _norm(text):
    return unicodedata.normalize("NFKC", str(text or "")).lower()


def load_keywords(category, base_dir=None, brands=None, explicit=None):
    """検索語リストを返す。

    - explicit（--keywords）が与えられればそれを使う（ファイルは読まない）。
    - brands（--brands）が与えられたら、該当する行だけに絞る。
      1行も一致しなければ**ブランド語そのもの**を検索語として使う（0件で終わらせない）。
    """
    if explicit:
        return parse_keyword_lines("\n".join(explicit))
    path = keywords_path(category, base_dir)
    text = ""
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                text = f.read()
        except OSError as e:
            raise ValueError("検索語ファイルが読めません: {} ({})".format(path, e))
    kws = parse_keyword_lines(text)
    if brands:
        wanted = [b.strip() for b in brands if b and b.strip()]
        narrowed = [k for k in kws if any(_norm(b) in _norm(k) for b in wanted)]
        if narrowed:
            return narrowed
        return parse_keyword_lines("\n".join(wanted))
    return kws


def _token_set(category, base_dir=None):
    tokens = set()
    for kw in load_keywords(category, base_dir=base_dir):
        for t in _norm(kw).split():
            if len(t) >= 2:
                tokens.add(t)
    return tokens


def brand_terms(category, base_dir=None, aliases=None):
    """「<ブランド名|ライン名>風」判定に使う語。

    検索語ファイルのトークン + 対応する英語表記（brandAliases）。
    英綴りを入れないと「TIFFANY風」「GUCCIタイプ」が素通りする（Codex r2）。
    生成語（ダウン・ジャケット等）も入るが、それらに「風」が付く出品
    （フェイクダウン等）も仕入れ対象外なので残して良い。
    """
    tokens = _token_set(category, base_dir=base_dir)
    table = _BRAND_ALIASES if aliases is None else aliases
    for brand, alias_list in (table or {}).items():
        key = _norm(brand)
        if key in tokens or any(key in t for t in tokens):
            for alias in alias_list or ():
                a = _norm(alias)
                if len(a) >= 3:
                    tokens.add(a)
    return sorted(tokens)


# ngWords / allowPhrases 等を両カテゴリの和集合にするキー（M11）
_UNION_LIST_KEYS = (
    "ngWords", "ngPatterns", "cautionWords", "allowPhrases", "allowPatterns",
    "noiseWords", "dropTokenPatterns", "brandSuffixNgWords", "brandTerms",
)


def load_union_profile(base_dir=None, overrides=None):
    """カテゴリ不明の検索語に使う保守側プロファイル（M11）。

    送料は apparel（850円＝高い側）、NG語・注意語・ノイズ語は**両カテゴリの和集合**。
    送料だけ保守側にしてNG語が緩くなる穴（「シャネル ピアス」が宝飾NGを外れる）を塞ぐ。
    """
    acc = load_profile("accessory", base_dir=base_dir)
    app = load_profile("apparel", base_dir=base_dir)
    merged = dict(app)
    for key in _UNION_LIST_KEYS:
        seen = []
        for value in list(acc.get(key) or []) + list(app.get(key) or []):
            if value not in seen:
                seen.append(value)
        merged[key] = seen
    merged["label"] = "カテゴリ不明(保守側: アパレル送料 + NG語は両カテゴリの和集合)"
    merged["category"] = "unknown"
    for key, value in (overrides or {}).items():
        merged[key] = value
    return merged


def resolve_category(keyword, base_dir=None):
    """検索語がどちらのカテゴリかを推定する。

    判定できない／両方に出てくる場合は **apparel**（出品送料850円＝保守的な見積り）に倒す。
    """
    kw = _norm(keyword)
    acc = _token_set("accessory", base_dir)
    app = _token_set("apparel", base_dir)
    hit_acc = any(t in kw for t in acc)
    hit_app = any(t in kw for t in app)
    if hit_acc and not hit_app:
        return "accessory"
    if hit_app and not hit_acc:
        return "apparel"
    # どちらとも決まらない語は "unknown"。呼び出し側は load_union_profile() を使う
    # （送料は高い側=850円、NG語は両カテゴリの和集合＝保守側・M11）。
    return "unknown"


def profile_for_resolved(resolved, base_dir=None, overrides=None):
    """resolve_category() の戻り値からプロファイルを作る。"""
    if resolved == "unknown":
        return load_union_profile(base_dir=base_dir, overrides=overrides)
    return load_profile(resolved, base_dir=base_dir, overrides=overrides)
