"""任务记录的持久化。

**为什么必须持久化**：Ark 契约要求 `GET /tasks/{id}` 在任务保留窗口内始终可查
（本服务沿用 **7 天**）。进程内 dict 一重启就清空 —— 调用方手里正在轮询的 `cgt-*`
会突然 404，表现为"任务凭空消失"。

本项目实测踩到过：为了加载一处代码修正而重启服务，正在轮询的任务**立刻查不到**了。
这类缺陷只在重启时暴露，平时看不出来，所以不能靠"进程内 dict + 记得别重启"。

**归属（owner）**：透传模式下每个调用方带自己的凭据，任务表就变成了多租户的
——`GET /tasks` 若不过滤，A 会看到 B 的任务（含 `requested` 里的 prompt 原文）。
所以每条记录带一个 `owner`，其值是**凭据的 sha256 前 16 位**（`app._owner_of`），
**绝不落凭据原文**。非透传模式 `owner` 为空串且查询不做归属过滤（进程内只有一份凭据）。

后端（`AVM_TASK_STORE`）：

===========  ================================================================
`sqlite`     默认。标准库、零依赖、单文件；WAL 模式，多线程/多 worker 可共享。
`memory`     **显式的**开发/测试开关 —— 不是 sqlite 失败后的隐式兜底。
             隐式降级会让"跑着跑着任务丢了"变成一个只在重启时才现形的偶发问题。
===========  ================================================================

线程安全策略：`sqlite3` 的连接**不可跨线程共享**，而本服务的路由会在
`asyncio.to_thread` 里访问存储 ⇒ 每次操作开一条独立连接（任务量是每小时几条，
这点开销可忽略），并用一把进程内锁把"开连接→事务→关连接"整段串起来。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id         TEXT PRIMARY KEY,
    task_id    TEXT NOT NULL,
    upstream   TEXT NOT NULL,
    model      TEXT NOT NULL DEFAULT '',
    owner      TEXT NOT NULL DEFAULT '',
    created_ms INTEGER NOT NULL,
    entry      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_created ON tasks (created_ms DESC);
"""

# ⚠️ owner 索引**不能放进 SCHEMA**：老库的 tasks 表还没有这一列，而
# `executescript(SCHEMA)` 在补列之前跑 —— 那时建索引会以 "no such column: owner"
# 直接失败，服务起不来。所以它在 __init__ 里补完列之后再建。
OWNER_INDEX = "CREATE INDEX IF NOT EXISTS idx_tasks_owner ON tasks (owner, created_ms DESC)"

DEFAULT_DB_PATH = ".ark-tasks.db"
DEFAULT_RETENTION_DAYS = 7
DAY_MS = 86_400_000


def _belongs_to(entry: dict, owner: str | None) -> bool:
    """owner=None 表示**不做归属过滤**（非透传模式），不是"匹配空串"。"""
    return owner is None or (entry.get("owner") or "") == owner


class MemoryTaskStore:
    """进程内存储。**只用于测试与显式本地调试**，重启即丢。"""

    kind = "memory"

    def __init__(self, path: str = "") -> None:
        self.path = ":memory:"
        self._rows: dict[str, dict] = {}
        self._lock = threading.Lock()

    def put(self, entry: dict) -> None:
        with self._lock:
            self._rows[entry["id"]] = dict(entry)

    def get(self, ark_id: str) -> dict | None:
        with self._lock:
            row = self._rows.get(ark_id)
            return dict(row) if row else None

    def delete(self, ark_id: str) -> dict | None:
        with self._lock:
            row = self._rows.pop(ark_id, None)
            return dict(row) if row else None

    def list_recent(self, limit: int, offset: int = 0, owner: str | None = None) -> list[dict]:
        with self._lock:
            rows = [dict(v) for v in self._rows.values() if _belongs_to(v, owner)]
        rows.sort(key=lambda e: e.get("createdAtMs") or 0, reverse=True)
        return rows[offset : offset + limit]

    def count(self, owner: str | None = None) -> int:
        with self._lock:
            return sum(1 for v in self._rows.values() if _belongs_to(v, owner))

    def prune(self, retention_days: int = DEFAULT_RETENTION_DAYS) -> int:
        cutoff = int(time.time() * 1000) - retention_days * DAY_MS
        with self._lock:
            dead = [k for k, v in self._rows.items() if (v.get("createdAtMs") or 0) < cutoff]
            for k in dead:
                del self._rows[k]
            return len(dead)

    def describe(self) -> dict:
        return {"kind": self.kind, "path": ":memory:", "durable": False}


class SqliteTaskStore:
    """SQLite 存储（默认后端）。单文件、WAL、跨重启与跨进程可读。"""

    kind = "sqlite"

    def __init__(self, path: str = DEFAULT_DB_PATH, retention_days: int = DEFAULT_RETENTION_DAYS):
        raw = str(path or DEFAULT_DB_PATH).strip() or DEFAULT_DB_PATH
        self.path = str(Path(raw).expanduser())
        self.retention_days = retention_days
        parent = Path(self.path).parent
        if str(parent) not in ("", "."):
            parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._session() as conn:
            conn.executescript(SCHEMA)
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)").fetchall()}
            if "owner" not in cols:
                # 就地升级：老库补列，不让调用方删库重来。已有行的 owner 为空串 ⇒
                # 在透传模式下查不到它们；这比"替它们猜一个归属"安全，而保留窗口
                # 只有 7 天，很快自然淘汰。
                conn.execute("ALTER TABLE tasks ADD COLUMN owner TEXT NOT NULL DEFAULT ''")
            conn.execute(OWNER_INDEX)

    # ---- plumbing ----------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    @contextmanager
    def _session(self):
        """一把进程内锁 + 一条独立连接 + 一个事务。

        ⚠️ `with sqlite3.connect(...)` 只管事务、**不会关闭连接** —— 必须显式 close，
        否则每次调用都会漏一条 fd。
        """
        with self._lock:
            conn = self._connect()
            try:
                with conn:
                    yield conn
            finally:
                conn.close()

    # ---- api ---------------------------------------------------------------

    def put(self, entry: dict) -> None:
        with self._session() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO tasks"
                " (id, task_id, upstream, model, owner, created_ms, entry)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    entry["id"],
                    entry.get("taskId") or "",
                    entry.get("upstream") or "",
                    entry.get("model") or "",
                    entry.get("owner") or "",
                    int(entry.get("createdAtMs") or 0),
                    json.dumps(entry, ensure_ascii=False),
                ),
            )

    def get(self, ark_id: str) -> dict | None:
        with self._session() as conn:
            row = conn.execute("SELECT entry FROM tasks WHERE id = ?", (ark_id,)).fetchone()
        return json.loads(row["entry"]) if row else None

    def delete(self, ark_id: str) -> dict | None:
        with self._session() as conn:
            row = conn.execute("SELECT entry FROM tasks WHERE id = ?", (ark_id,)).fetchone()
            if row is None:
                return None
            conn.execute("DELETE FROM tasks WHERE id = ?", (ark_id,))
        return json.loads(row["entry"])

    def list_recent(self, limit: int, offset: int = 0, owner: str | None = None) -> list[dict]:
        sql = "SELECT entry FROM tasks"
        args: tuple = ()
        if owner is not None:
            sql += " WHERE owner = ?"
            args = (owner,)
        sql += " ORDER BY created_ms DESC LIMIT ? OFFSET ?"
        with self._session() as conn:
            rows = conn.execute(sql, (*args, int(limit), int(offset))).fetchall()
        return [json.loads(r["entry"]) for r in rows]

    def count(self, owner: str | None = None) -> int:
        sql = "SELECT COUNT(*) AS n FROM tasks"
        args: tuple = ()
        if owner is not None:
            sql += " WHERE owner = ?"
            args = (owner,)
        with self._session() as conn:
            return int(conn.execute(sql, args).fetchone()["n"])

    def prune(self, retention_days: int | None = None) -> int:
        days = self.retention_days if retention_days is None else retention_days
        cutoff = int(time.time() * 1000) - days * DAY_MS
        with self._session() as conn:
            cur = conn.execute("DELETE FROM tasks WHERE created_ms < ?", (cutoff,))
            return int(cur.rowcount or 0)

    def describe(self) -> dict:
        return {
            "kind": self.kind,
            "path": self.path,
            "durable": True,
            "retention_days": self.retention_days,
        }


def build_task_store(store: str = "sqlite", path: str = DEFAULT_DB_PATH, retention_days: int = DEFAULT_RETENTION_DAYS):
    """按配置构造存储。

    `memory` 是**显式**开关：写错配置就抛错，不静默退回内存 —— 那会让持久化
    在没人注意的情况下悄悄失效。
    """
    kind = str(store or "sqlite").strip().lower()
    if kind == "memory":
        return MemoryTaskStore(path)
    if kind == "sqlite":
        return SqliteTaskStore(path, retention_days)
    raise ValueError(f"unknown task store {kind!r} (expected 'sqlite' | 'memory')")
