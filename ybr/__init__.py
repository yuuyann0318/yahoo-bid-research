# -*- coding: utf-8 -*-
"""ybr: ヤフオク入札リサーチ（アクセサリー / アパレル）。

標準ライブラリ + mercapi==0.4.2 のみに依存する自己完結パッケージ。
accessory-profit-scout の scout/{yahoo,mercari,stats,throttle,cache,profit,report}.py の
考え方を移植・簡素化したもの（import 依存はしない）。

責務はリストアップまで。入札の自動実行・外部送信は一切行わない。
"""
from __future__ import annotations

__version__ = "0.1.0"
