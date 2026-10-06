# -*- coding: utf-8 -*-
"""ybr.throttle: プロセス全体で最小リクエスト間隔を保証するスロットル。

accessory-profit-scout の scout/throttle.py と同方式（threading.Lock + time.monotonic、
最小2.0秒のハードfloor）。ヤフオク/メルカリ双方の呼び出しがこの1つのロックを共有するので、
並行実行でも「2秒未満で連続リクエスト」にはならない。
"""
from __future__ import annotations

import threading
import time

MIN_SLEEP = 2.0  # これ以上短くしない（お作法のハードfloor）

_lock = threading.Lock()
_last_request_ts = 0.0


def throttle(sleep_sec=MIN_SLEEP):
    """直前のリクエストから sleep_sec 秒空くまでブロックする（プロセス全体で共有）。"""
    global _last_request_ts
    try:
        sleep_sec = float(sleep_sec)
    except (TypeError, ValueError):
        sleep_sec = MIN_SLEEP
    if sleep_sec != sleep_sec:  # NaN
        sleep_sec = MIN_SLEEP
    sleep_sec = max(MIN_SLEEP, sleep_sec)
    with _lock:
        now = time.monotonic()
        wait = _last_request_ts + sleep_sec - now
        if wait > 0:
            time.sleep(wait)
        _last_request_ts = time.monotonic()
