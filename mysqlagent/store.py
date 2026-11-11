"""mysqlagent.store — SQLite 持久化存储（会话消息 + 审计事件同库）。

移植自 Go 版 internal/store/store.go：
  - sessions / messages：会话与消息，重启后可恢复、可回放；
  - audit_events：审计事件副本（JSONL 文件仍为主审计通道，同库便于按会话检索）。
单文件、零运维；WAL 模式 + busy_timeout 承接跨会话并发。

并发说明：server 侧已有会话级互斥，同会话串行写；本类内部再用一把
asyncio 锁串行化全部数据库操作（aiosqlite 单连接语义，与 Go 版
SetMaxOpenConns(1) 对齐）。
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any, Optional

import aiosqlite

from mysqlagent.llm import Message, ToolCall, FunctionCall


class StoreError(Exception):
    """存储操作失败。"""


class Store:
    """SQLite 持久化存储。"""

    def __init__(self, path: str) -> None:
        self._path = path
        self._db: Optional[aiosqlite.Connection] = None
        self._mu = asyncio.Lock()
        self._bg_tasks: set[asyncio.Task] = set()

    async def open(self) -> "Store":
        """打开（或创建）SQLite 存储并初始化表结构。"""
        # isolation_level=None → 手动事务控制（BEGIN IMMEDIATE / COMMIT）
        self._db = await aiosqlite.connect(self._path, isolation_level=None)
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA busy_timeout=5000")
        await self._db.execute("PRAGMA synchronous=NORMAL")
        await self._init_schema()
        return self

    async def _init_schema(self) -> None:
        assert self._db is not None
        stmts = [
            """CREATE TABLE IF NOT EXISTS sessions (
                id         TEXT PRIMARY KEY,
                created_at TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS messages (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id   TEXT NOT NULL,
                seq          INTEGER NOT NULL,
                role         TEXT NOT NULL,
                content      TEXT NOT NULL DEFAULT '',
                tool_calls   TEXT NOT NULL DEFAULT '[]',
                tool_call_id TEXT NOT NULL DEFAULT '',
                name         TEXT NOT NULL DEFAULT '',
                created_at   TEXT NOT NULL
            )""",
            "CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id)",
            """CREATE TABLE IF NOT EXISTS audit_events (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                ts       TEXT NOT NULL,
                event    TEXT NOT NULL,
                session  TEXT NOT NULL DEFAULT '',
                user     TEXT NOT NULL DEFAULT '',
                trace_id TEXT NOT NULL DEFAULT '',
                data     TEXT NOT NULL DEFAULT '{}'
            )""",
            "CREATE INDEX IF NOT EXISTS idx_audit_session ON audit_events(session)",
        ]
        async with self._mu:
            for q in stmts:
                await self._db.execute(q)
            # 迁移：会话归属（旧库无 owner 列；列已存在时 ALTER 会报错，忽略即可）
            try:
                await self._db.execute("ALTER TABLE sessions ADD COLUMN owner TEXT NOT NULL DEFAULT ''")
            except Exception:  # noqa: BLE001
                pass

    async def get_owner(self, session_id: str) -> Optional[str]:
        """返回会话归属标识；会话不存在返回 None（旧数据 owner 为空串）。"""
        assert self._db is not None
        async with self._mu:
            async with self._db.execute("SELECT owner FROM sessions WHERE id = ?", (session_id,)) as cur:
                row = await cur.fetchone()
        return None if row is None else (row[0] or "")

    async def close(self) -> None:
        # 先等后台审计入库任务排空，避免退出时丢最后一批事件
        if self._bg_tasks:
            await asyncio.gather(*list(self._bg_tasks), return_exceptions=True)
        if self._db is not None:
            await self._db.close()
            self._db = None

    async def load_messages(self, session_id: str) -> list[Message]:
        """加载会话的完整消息历史（按写入顺序）。"""
        assert self._db is not None
        async with self._mu:
            async with self._db.execute(
                "SELECT role, content, tool_calls, tool_call_id, name "
                "FROM messages WHERE session_id = ? ORDER BY seq, id",
                (session_id,),
            ) as cur:
                rows = await cur.fetchall()
        msgs: list[Message] = []
        for role, content, tool_calls, call_id, name in rows:
            m = Message(role=role, content=content or "", tool_call_id=call_id or "", name=name or "")
            if tool_calls and tool_calls != "[]":
                try:
                    parsed = json.loads(tool_calls)
                    m.tool_calls = [
                        ToolCall(
                            id=tc.get("id", ""),
                            type=tc.get("type", "function"),
                            function=FunctionCall(
                                name=(tc.get("function") or {}).get("name", ""),
                                arguments=(tc.get("function") or {}).get("arguments", ""),
                            ),
                        )
                        for tc in parsed
                        if isinstance(tc, dict)
                    ]
                except ValueError as e:
                    raise StoreError(f"反序列化 tool_calls: {e}") from e
            msgs.append(m)
        return msgs

    async def save_messages(self, session_id: str, msgs: list[Message], owner: str = "") -> None:
        """全量替换会话消息（事务内 delete + insert，崩溃安全）。

        owner 仅在会话首次创建时写入（不会被后续调用覆盖）。
        """
        assert self._db is not None
        now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        async with self._mu:
            await self._db.execute("BEGIN IMMEDIATE")
            try:
                await self._db.execute(
                    "INSERT INTO sessions(id, created_at, owner) VALUES(?, ?, ?) "
                    "ON CONFLICT(id) DO NOTHING",
                    (session_id, now, owner),
                )
                await self._db.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
                for i, m in enumerate(msgs):
                    tc = json.dumps([t.to_dict() for t in m.tool_calls], ensure_ascii=False)
                    await self._db.execute(
                        "INSERT INTO messages(session_id, seq, role, content, tool_calls, "
                        "tool_call_id, name, created_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                        (session_id, i, m.role, m.content, tc, m.tool_call_id, m.name, now),
                    )
                await self._db.execute("COMMIT")
            except Exception:
                await self._db.execute("ROLLBACK")
                raise

    def log_audit(self, e: dict[str, Any]) -> None:
        """把审计事件写入 SQLite（由 AuditLogger 的落盘任务调用，同步非阻塞）。

        失败只记日志，不影响主审计通道（JSONL）。
        注意：同步方法——内部通过 create_task 异步入库，避免阻塞落盘循环。
        """
        ts = e.get("ts") or datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        event = str(e.get("event", ""))
        session = str(e.get("session", ""))
        user = str(e.get("user", ""))
        trace_id = str(e.get("trace_id", ""))
        try:
            data = json.dumps(e, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            data = "{}"
        if self._db is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # 无事件循环（测试同步场景）：跳过同库存档
        task = loop.create_task(self._insert_audit(ts, event, session, user, trace_id, data))
        self._bg_tasks.add(task)  # 持有强引用，防止任务被 GC
        task.add_done_callback(self._bg_tasks.discard)

    async def _insert_audit(
        self, ts: str, event: str, session: str, user: str, trace_id: str, data: str
    ) -> None:
        if self._db is None:
            return
        try:
            async with self._mu:
                await self._db.execute(
                    "INSERT INTO audit_events(ts, event, session, user, trace_id, data) "
                    "VALUES(?, ?, ?, ?, ?, ?)",
                    (ts, event, session, user, trace_id, data),
                )
        except Exception as err:
            print(f"审计写入 SQLite 失败: {err}")

    async def replay_audit(self, session_id: str, limit: int = 200) -> list[dict[str, Any]]:
        """按会话检索审计事件（倒序，最多 limit 条）。"""
        if limit <= 0 or limit > 1000:
            limit = 100
        assert self._db is not None
        async with self._mu:
            async with self._db.execute(
                "SELECT id, ts, event, trace_id, data FROM audit_events "
                "WHERE session = ? ORDER BY id DESC LIMIT ?",
                (session_id, limit),
            ) as cur:
                rows = await cur.fetchall()
        out: list[dict[str, Any]] = []
        for id_, ts, event, trace_id, data in rows:
            item: dict[str, Any] = {"id": id_, "ts": ts, "event": event, "trace_id": trace_id}
            try:
                detail = json.loads(data)
                if isinstance(detail, dict):
                    for k, v in detail.items():
                        if k not in ("id", "ts", "event", "trace_id"):
                            item[k] = v
            except ValueError:
                pass
            out.append(item)
        return out
