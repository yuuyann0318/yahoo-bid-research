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

# 品位+めっき表記（K18GP / 18KGP / SV925GP / 925GF ...）。照合側は NFKC+小文字化済み。
# 末尾の (?![0-9a-z]) で "k18gps" のような誤爆を防ぐ（M5）。
_PLATING_PATTERNS = [
    r"(k\d{1,2}|\d{1,2}k|sv\d{3}|925|750|585|417)\s*-?\s*(gp|gf)(?![0-9a-z])",
    r"\d{1,2}\s*金\s*(gp|gf)(?![0-9a-z])",
    r"gold\s*-?\s*(plated|filled)",
    r"(?<![0-9a-z])(gp|gf)\s*-?\s*(刻印|加工|仕上げ)",
]

# NG出現は「救済フレーズに完全に含まれる」ときだけ救済する（M13）ので、
# くっついた表記（K18GPではありません）を救うには正規表現の救済が必要になる。
_PLATING_NEGATION_PATTERNS = [
    r"(?:k\d{1,2}|\d{1,2}k|sv\d{3}|925|750|585|417)?\s*-?\s*"
    r"(?:gp|gf)\s*(?:品|刻印)?\s*(?:では|じゃ)\s*(?:ありません|ない|なく|無い|無く)",
    r"(?:金|銀)?\s*(?:メッキ|めっき)\s*(?:品|加工)?\s*(?:では|じゃ)\s*"
    r"(?:ありません|ない|なく|無い|無く)",
    r"(?:コピー|レプリカ|偽物|模造)\s*(?:品)?\s*(?:では|じゃ)\s*"
    r"(?:ありません|ない|なく|無い|無く)",
]

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
        "めっき", "メッキ", "ノベルティ", "GP", "GF", "破損", "石取れ",
    ],
    # 除外はしないが result.md の「注意」に出す語（accessory-profit-scout の DANGER_WORDS）
    "cautionWords": [
        "刻印なし", "傷あり", "キズあり", "変色", "くすみ", "付属品なし", "社外",
        "訳あり", "難あり",
    ],
    "brandSuffixNgWords": list(_BRAND_SUFFIX_NG),
    # 正規表現での除外（GP/GF の刻印表記ゆれ）
    "ngPatterns": list(_PLATING_PATTERNS),
    # 正規表現での救済（否定表現。完全包含判定のため語リストでは救えない）
    "allowPatterns": list(_PLATING_NEGATION_PATTERNS),
    # NG語を打ち消す否定・正規表現（これに重なる出現は除外扱いにしない）
    "allowPhrases": [
        "GPではありません", "GPではない", "GPではなく", "GP品ではありません",
        "メッキではありません", "メッキではない", "めっきではありません",
        "メッキ品ではありません", "コピーではありません", "レプリカではありません",
        "偽物ではありません", "ロジウムコーティング", "ロジウムメッキ",
        "まとめて購入", "まとめてお取引",
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
    "cautionWords": [
        "色あせ", "色褪せ", "毛玉", "ほつれ", "擦れ", "スレ",
        "日焼け", "リペア", "補修", "アウトレット", "サイズ不明",
        "ダメージ少", "ダメージ少なめ",  # 損傷の肯定表現は救済せず注意に出す（M12）
    ],
    "brandSuffixNgWords": list(_BRAND_SUFFIX_NG),
    "ngPatterns": [],
    "allowPatterns": [
        r"(?:ダメージ|汚れ|破れ|シミ|染み|虫食い)\s*(?:は)?\s*"
        r"(?:ありません|ない|なく|無い|無く|見当たりません)",
    ],
    "allowPhrases": [
        "ダメージ加工", "ヴィンテージ加工", "ビンテージ加工", "加工ダメージ",
        "ダメージなし", "ダメージ無し", "ユーズド加工",
        "汚れありません", "破れありません", "まとめて購入", "まとめてお取引",
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
        # 「<ブランド名|ライン名>風」判定用の語。検索語ファイルから自動で作る（M23）。
        profile["brandTerms"] = brand_terms(category, base_dir=base_dir)
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


def brand_terms(category, base_dir=None):
    """「<ブランド名|ライン名>風」判定に使う語。検索語ファイルのトークンから作る。

    生成語（ダウン・ジャケット等）も入るが、それらに「風」が付く出品
    （フェイクダウン等）も仕入れ対象外なので残して良い。
    """
    return sorted(_token_set(category, base_dir=base_dir))


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
