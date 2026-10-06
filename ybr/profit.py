# -*- coding: utf-8 -*-
"""ybr.profit: 予想売値 → 入札提案価格の逆算（すべて「目安」）。

計算式（profiles/*.json で係数を変えられる）:

  n = comps.count（自分で数えた売切件数）
    n >= 8      → 確度「高」・係数 1.00（median は 1.5*IQR トリム後）
    3 <= n <= 7 → 確度「低」・係数 0.90
    n <  3      → 相場不明。採用しない（0埋め・推測で埋めない）

  fb = クエリ縮退の係数（fallback_level 0→1.00 / 1→0.95 / 2以上→0.90）
  予想売値 expected_sale = floor(median * n係数 * fb)

  手数料 fee      = ceil(expected_sale * mercariFeeRate)        ← 切り上げ（保守側）
  手取り  net      = expected_sale - fee - sellShippingYen
  仕入送料 ship_in = postage（0=送料無料 / None は unknownShippingPenaltyYen=1000）
  目標利益 target  = max(floor(expected_sale * margin%), minProfitYen)

  **入札提案価格 suggested_bid = floor100(net - target - ship_in)**
  損益分岐入札額 break_even_bid = net - ship_in   ← これ以上出すと赤字
  提案額での見込み利益 profit_at_bid = net - ship_in - suggested_bid

  採用条件 = suggested_bid >= 現在価格 かつ suggested_bid >= minBidYen

数値例: 予想売値10,000円・手数料10%・出品送料300円・仕入送料0円・目標30%
  → 手取り8,700円 / 目標利益3,000円 / 入札提案価格 5,700円 / 見込み利益 3,000円（目安）
"""
from __future__ import annotations

import math

DEFAULT_TARGET_MARGIN_PCT = 30.0
MIN_TARGET_MARGIN_PCT = 5.0
MAX_TARGET_MARGIN_PCT = 90.0

MIN_COUNT_FOR_PRICE = 3        # これ未満は「相場不明」
HIGH_CONFIDENCE_COUNT = 8      # これ以上で確度「高」
LOW_CONFIDENCE_FACTOR = 0.90   # 確度「低」のときの掛け目
FALLBACK_FACTORS = {0: 1.00, 1: 0.95, 2: 0.90}

REASON_UNKNOWN_PRICE = "相場不明(メルカリ売切{}件 < {}件)"
REASON_OVER_BID = "入札上限超過(現在{:,}円 > 入札提案価格{:,}円)"
REASON_UNDER_MIN_BID = "提案額が下限未満({:,}円 < {:,}円)"


def floor100(value):
    """100円単位に切り捨て（負値はより小さい側へ）。"""
    return int(math.floor(float(value) / 100.0) * 100)


def target_margin_pct(profile):
    """目標利益率(%)。未設定・非数値・範囲外（5%未満/90%超）は既定30%へ戻す。

    0%や負値を通すと採用条件が事実上無効化され、逆に手数料率との合計が100%以上だと
    提案額が常に負になって全件落ちるため、どちらも既定へ倒す（fail-safe）。
    """
    raw = (profile or {}).get("targetMarginPct", DEFAULT_TARGET_MARGIN_PCT)
    try:
        pct = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_TARGET_MARGIN_PCT
    if pct != pct:  # NaN
        return DEFAULT_TARGET_MARGIN_PCT
    if pct < MIN_TARGET_MARGIN_PCT or pct > MAX_TARGET_MARGIN_PCT:
        return DEFAULT_TARGET_MARGIN_PCT
    try:
        fee_pct = float((profile or {}).get("mercariFeeRate", 0.0)) * 100.0
    except (TypeError, ValueError):
        fee_pct = 0.0
    if fee_pct != fee_pct:
        fee_pct = 0.0
    if pct + fee_pct >= 100.0:
        return DEFAULT_TARGET_MARGIN_PCT
    return pct


def _fallback_factor(level):
    try:
        lv = int(level or 0)
    except (TypeError, ValueError):
        lv = 0
    if lv <= 0:
        return FALLBACK_FACTORS[0]
    return FALLBACK_FACTORS.get(lv, FALLBACK_FACTORS[2])


def ship_in_yen(candidate, profile):
    """ヤフオク側の送料。0=送料無料 / None（未定）はペナルティ額。"""
    postage = (candidate or {}).get("postage")
    if postage is None:
        return int((profile or {}).get("unknownShippingPenaltyYen", 1000))
    return int(postage)


def evaluate(candidate, profile):
    """1候補の利益計算と採用判定。常に dict を返す（ok / reason を含む）。

    副作用なし（candidate は変更しない）。呼び出し側が candidate["profit"] に入れる。
    """
    profile = profile or {}
    comps = (candidate or {}).get("comps") or {}
    count = int(comps.get("count", 0) or 0)
    ship_out = int(profile.get("sellShippingYen", 0))
    ship_in = ship_in_yen(candidate, profile)
    margin = target_margin_pct(profile)

    base = {
        "ok": False,
        "reason": None,
        "expected_sale": None,
        "confidence": None,
        "comps_count": count,
        "fee": 0,
        "ship_out": ship_out,
        "ship_in": ship_in,
        "net": 0,
        "target_profit": 0,
        "suggested_bid": 0,
        "break_even_bid": 0,
        "profit_at_bid": 0,
        "margin_pct": margin,
        "fallback_level": int(comps.get("fallback_level", 0) or 0),
    }

    if count < MIN_COUNT_FOR_PRICE:
        base["reason"] = REASON_UNKNOWN_PRICE.format(count, MIN_COUNT_FOR_PRICE)
        return base

    median = int(comps.get("median") or 0)
    if median <= 0:
        base["reason"] = REASON_UNKNOWN_PRICE.format(count, MIN_COUNT_FOR_PRICE)
        return base

    if count >= HIGH_CONFIDENCE_COUNT:
        confidence = "高"
        n_factor = 1.0
    else:
        confidence = "低"
        n_factor = LOW_CONFIDENCE_FACTOR

    expected_sale = int(math.floor(median * n_factor * _fallback_factor(base["fallback_level"])))
    fee_rate = float(profile.get("mercariFeeRate", 0.10))
    fee = int(math.ceil(expected_sale * fee_rate))
    net = expected_sale - fee - ship_out
    target_profit = max(
        int(math.floor(expected_sale * margin / 100.0)),
        int(profile.get("minProfitYen", 0)),
    )
    suggested_bid = floor100(net - target_profit - ship_in)
    break_even_bid = net - ship_in
    profit_at_bid = net - ship_in - suggested_bid

    base.update({
        "expected_sale": expected_sale,
        "confidence": confidence,
        "fee": fee,
        "net": net,
        "target_profit": target_profit,
        "suggested_bid": suggested_bid,
        "break_even_bid": break_even_bid,
        "profit_at_bid": profit_at_bid,
    })

    min_bid = int(profile.get("minBidYen", 0))
    if suggested_bid < min_bid:
        base["reason"] = REASON_UNDER_MIN_BID.format(suggested_bid, min_bid)
        return base

    price = int((candidate or {}).get("price") or 0)
    if price > suggested_bid:
        base["reason"] = REASON_OVER_BID.format(price, suggested_bid)
        return base

    base["ok"] = True
    return base
