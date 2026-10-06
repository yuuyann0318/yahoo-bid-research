# -*- coding: utf-8 -*-
"""ybr.cache: 永続化まわり（相場キャッシュ / 日次リクエスト予算 / 重複履歴）。

- CompsCache: query -> comps の SQLite キャッシュ（TTL 24時間）。破損は削除→再作成で自己修復。
- DailyBudget: JST 日付でリセットする日次リクエスト上限（accessory-profit-scout とは別ファイル）。
- History: 直近 N 日に報告した商品を記録して再掲を防ぐ。

すべて「例外を外へ漏らさない / 書けなくてもパイプラインを止めない」方針。
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import sqlite3
import unicodedata
from datetime import datetime, timedelta, timezone

JST = timezone(timedelta(hours=9))
DEFAULT_TTL_HOURS = 24.0


def today_str():
    """JST の YYYY-MM-DD。"""
    return datetime.now(JST).strftime("%Y-%m-%d")


def _save_json_atomic(path, data):
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = "{}.tmp-{}".format(path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _load_json_safe(path, fallback):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return fallback


# --- メルカリ相場キャッシュ ---------------------------------------------------
class NullCompsCache:
    """キャッシュ無効時の代替（常にミス・保存は捨てる）。"""

    disabled = True

    def get(self, query):
        return None

    def put(self, query, comps):
        pass


class CompsCache:
    """query -> comps の SQLite キャッシュ。TTL 超過は無効。"""

    def __init__(self, db_path, ttl_hours=DEFAULT_TTL_HOURS):
        self.db_path = str(db_path)
        self.ttl_hours = float(ttl_hours)
        self.disabled = False
        parent = os.path.dirname(self.db_path)
        if parent:
            try:
                os.makedirs(parent, exist_ok=True)
            except OSError:
                self.disabled = True
                return
        try:
            self._ensure_schema()
        except sqlite3.OperationalError:
            self.disabled = True
        except sqlite3.DatabaseError:
            self._recreate()

    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=8000")
        return conn

    def _ensure_schema(self):
        conn = self._connect()
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS comps ("
                "query TEXT PRIMARY KEY, payload TEXT, created_at TEXT)"
            )
            conn.commit()
        finally:
            conn.close()

    def _recreate(self):
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(self.db_path + suffix)
            except OSError:
                pass
        try:
            self._ensure_schema()
        except sqlite3.Error:
            self.disabled = True

    def _expired(self, created_at):
        try:
            created = datetime.fromisoformat(created_at)
        except (TypeError, ValueError):
            return True
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - created) > timedelta(hours=self.ttl_hours)

    def get(self, query):
        if self.disabled:
            return None
        conn = None
        try:
            conn = self._connect()
            row = conn.execute(
                "SELECT payload, created_at FROM comps WHERE query=?", (query,)
            ).fetchone()
            if row is None:
                return None
            payload, created_at = row
            if self._expired(created_at):
                conn.execute("DELETE FROM comps WHERE query=?", (query,))
                conn.commit()
                return None
            try:
                return json.loads(payload)
            except (TypeError, ValueError):
                return None
        except sqlite3.OperationalError:
            return None
        except sqlite3.DatabaseError:
            self._recreate()
            return None
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:  # noqa: BLE001
                    pass

    def put(self, query, comps):
        if self.disabled:
            return
        conn = None
        try:
            conn = self._connect()
            conn.execute(
                "INSERT INTO comps (query, payload, created_at) VALUES (?, ?, ?) "
                "ON CONFLICT(query) DO UPDATE SET "
                "payload=excluded.payload, created_at=excluded.created_at",
                (query, json.dumps(comps, ensure_ascii=False),
                 datetime.now(timezone.utc).isoformat()),
            )
            conn.commit()
        except sqlite3.OperationalError:
            pass
        except sqlite3.DatabaseError:
            self._recreate()
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:  # noqa: BLE001
                    pass


# --- 日次リクエスト予算 -------------------------------------------------------
class DailyBudget:
    """state/daily-budget.json。spend(kind, n) は上限内なら消費して True。

    m21: 読み→判定→更新を **fcntl.flock（排他）下で一体化**する。起動時スナップショットで
    判定すると、同時実行した2プロセスが残り1回を両方消費できてしまう。
    保存に失敗したときは**停止せず警告を残して続行**する（監査判断。ただし
    warnings に出して result.md / meta から見えるようにする）。
    """

    DEFAULT_LIMITS = {"yahoo": 120, "mercari": 360}
    LOCK_SUFFIX = ".lock"

    def __init__(self, path, limits=None):
        self.path = path
        self.limits = dict(self.DEFAULT_LIMITS)
        self.limits.update(limits or {})
        self.warnings = []
        self.save_failed = False
        # H3: 保存に失敗した消費はディスクに残らない。同一実行内で消えないよう
        # メモリに保持し、used()/spend() で必ず合算する（上限1なら1回しか通らない）。
        self._unsaved = {}
        parent = os.path.dirname(path)
        if parent:
            try:
                os.makedirs(parent, exist_ok=True)
            except OSError as e:
                self._warn("予算ファイルのディレクトリを作れません: {}".format(e))

    def _warn(self, message):
        if message not in self.warnings:
            self.warnings.append(message)

    def _read_state(self):
        data = _load_json_safe(self.path, {"date": None, "counts": {}})
        if not isinstance(data, dict):
            data = {"date": None, "counts": {}}
        if not isinstance(data.get("counts"), dict):
            data["counts"] = {}
        if data.get("date") != today_str():
            data = {"date": today_str(), "counts": {}}
        return data

    @contextlib.contextmanager
    def _locked(self):
        """予算ファイル専用ロック。取れなければ警告して続行（止めない）。"""
        lock_path = self.path + self.LOCK_SUFFIX
        fd = None
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
        except OSError as e:
            self._warn("予算ファイルのロックを取得できません（同時実行の二重消費の恐れ）: {}".format(e))
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
                fd = None
        try:
            yield
        finally:
            if fd is not None:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                finally:
                    try:
                        os.close(fd)
                    except OSError:
                        pass

    def used(self, kind):
        """ディスク上の消費 + **保存できなかった消費**（H3）。"""
        with self._locked():
            on_disk = int((self._read_state().get("counts") or {}).get(kind, 0))
        return on_disk + int(self._unsaved.get(kind, 0))

    def remaining(self, kind):
        return max(0, int(self.limits.get(kind, 0)) - self.used(kind))

    def spend(self, kind, n=1):
        """ロック下で 読み→判定→更新 を一体で行う（m21）。

        保存に失敗しても**消費を忘れない**（H3）。忘れると同一実行内で日次上限を
        いくらでも超えられる（上限1で3回成功していた）。
        """
        n = int(n)
        if n <= 0:
            return True
        unsaved = int(self._unsaved.get(kind, 0))
        with self._locked():
            state = self._read_state()
            counts = state.setdefault("counts", {})
            on_disk = int(counts.get(kind, 0))
            used = on_disk + unsaved
            limit = int(self.limits.get(kind, 0))
            if used + n > limit:
                return False
            counts[kind] = used + n
            try:
                _save_json_atomic(self.path, state)
            except OSError as e:
                self.save_failed = True
                self._unsaved[kind] = unsaved + n
                self._warn(
                    "日次予算の保存に失敗（この実行の消費はメモリで数える / "
                    "次回起動で予算が巻き戻る恐れ）: {}".format(e))
            else:
                # 保存できたので未保存分はディスクに取り込まれた
                if unsaved:
                    self._unsaved[kind] = 0
            return True


# --- 重複履歴 -----------------------------------------------------------------
def normalize_title(title):
    if not title:
        return ""
    t = unicodedata.normalize("NFKC", str(title)).lower()
    t = re.sub(r"[\s　]+", " ", t)
    t = re.sub(r"[!-/:-@\[-`{-~。、！？「」『』・]", "", t)
    return t.strip()


def _days_between(a, b):
    da = datetime.strptime(a, "%Y-%m-%d")
    db = datetime.strptime(b, "%Y-%m-%d")
    return (db - da).days


def _prune(entries, today, window_days):
    out = []
    for h in entries:
        if not isinstance(h, dict):
            continue
        try:
            diff = _days_between(h.get("date") or today, today)
        except ValueError:
            continue
        if 0 <= diff <= window_days:
            out.append(h)
    return out


class History:
    """state/history.json: [{"date","id","titleNorm"}] を dedupWindowDays 分だけ保持。"""

    def __init__(self, path, dedup_window_days=7):
        self.path = path
        self.window_days = int(dedup_window_days)
        data = _load_json_safe(path, [])
        self._entries = _prune(data if isinstance(data, list) else [],
                               today_str(), self.window_days)

    def is_duplicate(self, candidate):
        cid = (candidate or {}).get("auction_id")
        norm = normalize_title((candidate or {}).get("title"))
        for h in self._entries:
            if cid and h.get("id") == cid:
                return True
            if norm and h.get("titleNorm") == norm:
                return True
        return False

    def mark(self, candidates):
        today = today_str()
        additions = [
            {"date": today,
             "id": (c or {}).get("auction_id"),
             "titleNorm": normalize_title((c or {}).get("title"))}
            for c in (candidates or [])
        ]
        self._entries = _prune(self._entries + additions, today, self.window_days)
        try:
            _save_json_atomic(self.path, self._entries)
        except OSError:
            pass


class NullHistory:
    def is_duplicate(self, candidate):
        return False

    def mark(self, candidates):
        pass
