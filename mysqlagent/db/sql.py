"""mysqlagent.db.sql — 数据库连接池与查询辅助（MySQL / PostgreSQL 双方言，异步）。

移植自 Go 版 internal/db/sql.go：
  - PostgreSQL：psycopg3 异步连接池（psycopg_pool）；
  - MySQL：aiomysql 连接池；
  - DSN 保持 Go 版格式（pgx URL / go-sql-driver tcp DSN），config.yaml 可直接复用；
  - 查询结果全部转为字符串（NULL 为 None），三级截断：
    行数（maxRows）、单元格（cellLimit）、总字节（maxBytes）。
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import date, datetime, time as dtime
from decimal import Decimal
from typing import Any, Optional

from mysqlagent.db.guard import DRIVER_MYSQL, DRIVER_POSTGRES

# go-sql-driver/mysql 风格 DSN：user:pass@tcp(host:port)/dbname?params
_MYSQL_DSN = re.compile(
    r"^(?P<user>[^:@]+)(?::(?P<password>[^@]*))?@tcp\((?P<host>[^:)]+)(?::(?P<port>\d+))?\)/(?P<db>[^?]*)"
    r"(?:\?(?P<params>.*))?$"
)


class DBError(Exception):
    """数据库操作失败。"""


def _cell_to_str(v: Any) -> Optional[str]:
    """把驱动返回的值统一转为字符串（NULL → None）。

    对应 Go 版 database/sql RawBytes 的文本协议表现；
    字节/数值/日期/JSON 类型按可读形式转换。
    """
    if v is None:
        return None
    if isinstance(v, str):
        return v
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, (bytes, bytearray, memoryview)):
        return bytes(v).decode("utf-8", errors="replace")
    if isinstance(v, (datetime, date, dtime)):
        return v.isoformat()
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False, default=str)
    return str(v)


_DURATION = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(ms|s|m)?\s*$", re.IGNORECASE)


def _parse_duration_seconds(v: str, default: int) -> int:
    """解析 go-sql-driver 风格时长（5s / 500ms / 1m / 纯数字按秒），向上取整到秒。"""
    m = _DURATION.match(v)
    if m is None:
        return default
    n = float(m.group(1))
    unit = (m.group(2) or "s").lower()
    secs = n / 1000 if unit == "ms" else n * 60 if unit == "m" else n
    return max(1, int(-(-secs // 1)))


def _column_name(d: Any) -> str:
    """列描述取名字：psycopg3 的 Column 对象与 DB-API 2.0 的 7 元组都支持。"""
    name = getattr(d, "name", None)
    if name is None:
        name = d[0]
    return str(name)


class DB:
    """封装数据库连接池与查询辅助，支持 MySQL / PostgreSQL 双方言。"""

    def __init__(
        self,
        driver: str,
        dsn: str,
        max_open: int = 5,
        max_idle: int = 2,
        query_timeout: int = 15,
    ) -> None:
        if driver not in (DRIVER_MYSQL, DRIVER_POSTGRES):
            raise DBError(f"不支持的驱动 {driver!r}（可选: {DRIVER_POSTGRES} / {DRIVER_MYSQL}）")
        self._driver = driver
        self._dsn = dsn
        self._max_open = max_open
        self._max_idle = max_idle
        self._query_timeout = query_timeout
        self._pool: Any = None  # psycopg_pool.AsyncConnectionPool | aiomysql.Pool

    @property
    def driver(self) -> str:
        return self._driver

    async def connect(self) -> "DB":
        """打开连接池并 Ping 验证。"""
        if self._driver == DRIVER_POSTGRES:
            await self._connect_postgres()
        else:
            await self._connect_mysql()
        await self._ping()
        return self

    async def _connect_postgres(self) -> None:
        try:
            import psycopg
            from psycopg_pool import AsyncConnectionPool
        except ImportError as e:
            raise DBError(
                "使用 PostgreSQL 驱动需要安装 psycopg：pip install 'psycopg[binary,pool]'"
            ) from e
        # psycopg 直接接受 pgx 风格的 postgres:// URL conninfo
        self._pool = AsyncConnectionPool(
            self._dsn,
            min_size=max(1, self._max_idle),
            max_size=max(1, self._max_open),
            open=False,
            kwargs={
                "autocommit": True,  # 只读场景：避免池连接长期持有事务快照
                # 数据库侧纵深防御：会话默认只读事务 + 服务端语句超时
                # （仅靠 asyncio.timeout 取消协程并不能保证数据库端查询被终止）。
                "options": (
                    "-c default_transaction_read_only=on "
                    f"-c statement_timeout={int(self._query_timeout) * 1000}"
                ),
            },
        )
        try:
            await self._pool.open(wait=True, timeout=10)
        except Exception as e:
            raise DBError(f"连接数据库失败: {e}") from e

    async def _connect_mysql(self) -> None:
        try:
            import aiomysql
        except ImportError as e:
            raise DBError("使用 MySQL 驱动需要安装 aiomysql：pip install aiomysql") from e
        m = _MYSQL_DSN.match(self._dsn.strip())
        if m is None:
            raise DBError(
                "MySQL DSN 格式应为 user:password@tcp(host:port)/dbname?params，"
                f"当前: {self._dsn!r}"
            )
        params = m.group("params") or ""
        charset = "utf8mb4"
        connect_timeout = 10
        for kv in params.split("&"):
            if not kv:
                continue
            k, _, v = kv.partition("=")
            k = k.strip().lower()
            if k == "charset":
                charset = v.strip()
            elif k == "timeout":
                connect_timeout = _parse_duration_seconds(v, connect_timeout)
        try:
            self._pool = await aiomysql.create_pool(
                user=m.group("user"),
                password=m.group("password") or "",
                host=m.group("host"),
                port=int(m.group("port") or 3306),
                db=m.group("db") or None,
                charset=charset,
                connect_timeout=connect_timeout,
                autocommit=True,
                # 数据库侧纵深防御：会话只读 + 服务端语句超时（毫秒；MySQL 5.7.8+）
                init_command=(
                    "SET SESSION transaction_read_only=1, "
                    f"max_execution_time={int(self._query_timeout) * 1000}"
                ),
                minsize=max(1, self._max_idle),
                maxsize=max(1, self._max_open),
            )
        except Exception as e:
            raise DBError(f"连接数据库失败: {e}") from e

    async def _ping(self) -> None:
        try:
            async with self._conn() as conn:
                async with conn.cursor() as cur:
                    await cur.execute("SELECT 1")
                    await cur.fetchall()
        except Exception as e:
            await self.close()
            raise DBError(f"连接数据库失败: {e}") from e

    def _conn(self):
        """返回异步上下文管理器形式的连接（两种池接口统一）。"""
        if self._driver == DRIVER_POSTGRES:
            return self._pool.connection()
        return self._pool.acquire()

    async def close(self) -> None:
        if self._pool is None:
            return
        try:
            await self._pool.close()
        except Exception:
            pass
        self._pool = None

    async def _run(self, query: str, args: tuple, max_rows: Optional[int] = None):
        """执行查询并返回 (列名, 原始行)。统一超时与错误包装。

        max_rows 不为 None 时只取回 max_rows+1 行（多 1 行用来判断是否被截断），
        避免大结果集被 fetchall 整体读进内存。
        """
        try:
            async with asyncio.timeout(self._query_timeout):
                async with self._conn() as conn:
                    async with conn.cursor() as cur:
                        await cur.execute(query, args)
                        cols = [_column_name(d) for d in cur.description] if cur.description else []
                        if max_rows is None:
                            raw = await cur.fetchall()
                        else:
                            raw = await cur.fetchmany(max_rows + 1)
                        return cols, raw
        except TimeoutError as e:
            raise DBError(f"执行 SQL 超时（{self._query_timeout}s）") from e
        except DBError:
            raise
        except Exception as e:
            raise DBError(f"执行 SQL: {e}") from e

    async def query_json(
        self, query: str, max_rows: int, cell_limit: int, max_bytes: int
    ) -> tuple[list[str], list[list[Optional[str]]], bool]:
        """执行只读 SQL，返回列名与行数据（全部转为字符串，NULL 为 None）。

        maxRows 限制读取行数，cellLimit 限制单元格字符数，maxBytes 限制总输出字节数。
        任一限制触发时 truncated 置 true。
        """
        cols, raw = await self._run(query, (), max_rows)
        n = len(cols)
        rows: list[list[Optional[str]]] = []
        truncated = False
        total = 0
        for raw_row in raw:
            if len(rows) >= max_rows:
                truncated = True
                break
            row: list[Optional[str]] = []
            for i in range(n):
                v = raw_row[i] if i < len(raw_row) else None
                cell = _cell_to_str(v)
                if cell is not None and len(cell) > cell_limit:
                    cell = cell[:cell_limit] + "…"
                    truncated = True
                row.append(cell)
                if cell is not None:
                    total += len(cell.encode("utf-8"))
            if total > max_bytes:
                truncated = True
                break
            rows.append(row)
        return cols, rows, truncated

    async def query_rows_json(
        self, query: str, *args: Any
    ) -> tuple[list[str], list[list[Optional[str]]]]:
        """QueryJSON 的参数化版本，用于元数据查询（无注入风险）。

        占位符方言差异由驱动层抹平：psycopg 与 aiomysql 均使用 %s。
        """
        cols, raw = await self._run(query, tuple(args))
        rows = [[_cell_to_str(raw_row[i]) if i < len(raw_row) else None
                 for i in range(len(cols))] for raw_row in raw]
        return cols, rows
