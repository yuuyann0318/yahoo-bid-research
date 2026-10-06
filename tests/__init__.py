"""tests: ネットワークを使わない unittest 群（HTMLフィクスチャ＋スタブ注入）。

sys.path に親ディレクトリ（プロジェクトルート）を通して `ybr` を import できるようにする。
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
