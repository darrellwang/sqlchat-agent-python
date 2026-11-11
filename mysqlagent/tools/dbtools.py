"""mysqlagent.tools.dbtools — 数据库工具集：list_tables / describe_table / execute_sql。

移植自 Go 版 internal/tools/dbtools.go，支持 MySQL 与 PostgreSQL 双方言，
元数据查询按驱动分支实现（占位符在 Python 驱动层统一为 %s）。
"""

from __future__ import annotations

import json
from typing import Any, Optional

from mysqlagent.db import acl
from mysqlagent.db.guard import DRIVER_POSTGRES, validate_read_only
from mysqlagent.db.sql import DB
from mysqlagent.tools.registry import must_json

# execute_sql 最大返回行数 / 单元格字符截断 / 元数据类工具的单元格截断。
MAX_ROWS = 100
CELL_LIMIT = 512
CELL_LIMIT_INFO = 256


# ---------- 公共辅助 ----------

def rows_to_objects(cols: list[str], rows: list[list[Any]]) -> list[dict[str, Any]]:
    """把列名 + 行数组 zip 成对象数组，方便 LLM 理解字段含义。"""
    out: list[dict[str, Any]] = []
    for row in rows:
        obj: dict[str, Any] = {}
        for i, c in enumerate(cols):
            if i < len(row):
                obj[c] = row[i]
        out.append(obj)
    return out


def trunc_rows(rows: list[list[Any]]) -> None:
    """对元数据查询结果做单元格截断，避免异常长注释撑爆上下文。"""
    for row in rows:
        for i, cell in enumerate(row):
            if isinstance(cell, str) and len(cell) > CELL_LIMIT_INFO:
                row[i] = cell[:CELL_LIMIT_INFO] + "…"


def norm_table(s: str) -> str:
    """归一化表名（与 db.acl 规则一致：小写、去引号与 schema 前缀）。"""
    return acl.normalize_table_name(s)


# ---------- list_tables ----------

class ListTables:
    """列出当前库的全部表（名称、估算行数、注释）。"""

    def __init__(self, db: DB) -> None:
        self.db = db

    def name(self) -> str:
        return "list_tables"

    def description(self) -> str:
        return "列出当前数据库中的所有表，返回表名、估算行数和表注释。当你不知道库里有哪些表时，先用这个工具。"

    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    def _sql(self) -> str:
        """list_tables 方言 SQL：统一输出列 table_name / row_estimate / table_comment。"""
        if self.db.driver == DRIVER_POSTGRES:
            # reltuples 为估算行数（未分析的表可能为 -1，GREATEST 归零）
            return """SELECT c.relname AS table_name,
                   GREATEST(c.reltuples::bigint, 0)::text AS row_estimate,
                   COALESCE(obj_description(c.oid), '') AS table_comment
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE c.relkind = 'r'
                  AND n.nspname NOT IN ('pg_catalog', 'information_schema')
                ORDER BY c.relname"""
        return """SELECT TABLE_NAME AS table_name,
                   IFNULL(TABLE_ROWS, 0) AS row_estimate,
                   IFNULL(TABLE_COMMENT, '') AS table_comment
                FROM information_schema.TABLES
                WHERE TABLE_SCHEMA = DATABASE() AND TABLE_TYPE = 'BASE TABLE'
                ORDER BY TABLE_NAME"""

    async def execute(self, args_json: str) -> str:
        cols, rows = await self.db.query_rows_json(self._sql())
        trunc_rows(rows)

        # 表级 ACL：配置了允许清单时只展示清单内的表
        allow = acl.tables_from_context()
        tables = rows_to_objects(cols, rows)
        if allow is not None:
            allowed = {norm_table(t) for t in allow}
            tables = [tb for tb in tables if norm_table(str(tb.get("table_name", ""))) in allowed]
        return must_json({
            "driver": self.db.driver,
            "tables": tables,
        })


# ---------- describe_table ----------

class DescribeTable:
    """查看指定表的列结构与索引（参数化查询，无注入风险）。"""

    def __init__(self, db: DB) -> None:
        self.db = db

    def name(self) -> str:
        return "describe_table"

    def description(self) -> str:
        return "查看指定表的详细结构：每列的名称、类型、是否可空、键、默认值、注释，以及表的索引。写 SQL 前先用它确认字段名和类型。"

    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "table": {"type": "string", "description": "表名"},
            },
            "required": ["table"],
        }

    def _columns_sql(self) -> str:
        """describe_table 列信息方言 SQL：统一输出列 name/nullable/key/default_val/extra/comment。"""
        if self.db.driver == DRIVER_POSTGRES:
            return """SELECT c.column_name AS name,
                   c.data_type AS type,
                   c.is_nullable AS nullable,
                   '' AS key,
                   COALESCE(c.column_default, '') AS default_val,
                   '' AS extra,
                   COALESCE(col_description(
                       format('%I.%I', c.table_schema, c.table_name)::regclass,
                       c.ordinal_position), '') AS comment
                FROM information_schema.columns c
                WHERE c.table_schema = current_schema() AND c.table_name = %s
                ORDER BY c.ordinal_position"""
        return """SELECT COLUMN_NAME AS name,
                   COLUMN_TYPE AS type,
                   IS_NULLABLE AS nullable,
                   COLUMN_KEY AS key,
                   IFNULL(COLUMN_DEFAULT, '') AS default_val,
                   IFNULL(EXTRA, '') AS extra,
                   IFNULL(COLUMN_COMMENT, '') AS comment
                FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s
                ORDER BY ORDINAL_POSITION"""

    def _indexes_sql(self) -> str:
        """describe_table 索引方言 SQL：统一输出列 index_name/kind/definition。"""
        if self.db.driver == DRIVER_POSTGRES:
            return """SELECT i.indexname AS index_name,
                   CASE WHEN i.indexdef LIKE 'CREATE UNIQUE%' THEN 'UNIQUE' ELSE 'NORMAL' END AS kind,
                   i.indexdef AS definition
                FROM pg_indexes i
                WHERE i.schemaname = current_schema() AND i.tablename = %s
                ORDER BY i.indexname"""
        return """SELECT INDEX_NAME AS index_name,
                   IF(NON_UNIQUE = 0, 'UNIQUE', 'NORMAL') AS kind,
                   GROUP_CONCAT(COLUMN_NAME ORDER BY SEQ_IN_INDEX SEPARATOR ',') AS definition
                FROM information_schema.STATISTICS
                WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s
                GROUP BY INDEX_NAME, NON_UNIQUE
                ORDER BY INDEX_NAME"""

    async def execute(self, args_json: str) -> str:
        try:
            args = json.loads(args_json)
            table = args.get("table", "") if isinstance(args, dict) else ""
            if not isinstance(table, str):
                raise ValueError("table 必须是字符串")
        except ValueError as e:
            return f"TOOL_ERROR: 解析参数: {e}"
        if table == "":
            return "TOOL_ERROR: 参数 table 不能为空"

        # 表级 ACL：目标表必须在允许清单内
        try:
            acl.check_tables_acl(acl.tables_from_context(), [table])
        except acl.TableNotAllowed as e:
            return "TOOL_ERROR: " + str(e)

        _, cols = await self.db.query_rows_json(self._columns_sql(), table)
        if len(cols) == 0:
            return "TOOL_ERROR: 表不存在或当前库中没有该表（可先调用 list_tables 查看全部表名）"
        trunc_rows(cols)

        _, idx = await self.db.query_rows_json(self._indexes_sql(), table)
        trunc_rows(idx)

        idx_objs = rows_to_objects(["index_name", "kind", "definition"], idx) if idx else []
        return must_json({
            "driver": self.db.driver,
            "table": table,
            "columns": rows_to_objects(
                ["name", "type", "nullable", "key", "default_val", "extra", "comment"], cols
            ),
            "indexes": idx_objs,
        })


# ---------- execute_sql ----------

class ExecuteSQL:
    """执行只读 SQL（SELECT/SHOW/DESCRIBE/EXPLAIN/WITH）。"""

    def __init__(self, db: DB, query_limit: int = 100, max_bytes: int = 24 * 1024) -> None:
        self.db = db
        self.query_limit = query_limit  # 无 LIMIT 时强制追加上限
        self.max_bytes = max_bytes      # 返回给 LLM 的最大字节数

    def name(self) -> str:
        return "execute_sql"

    def description(self) -> str:
        return "执行只读 SQL 查询（仅允许 SELECT / SHOW / DESCRIBE / EXPLAIN / WITH，系统自动拦截一切写操作）。返回列名和行数据（JSON）。没有 LIMIT 的 SELECT 会自动追加 LIMIT。"

    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "sql": {"type": "string", "description": "要执行的只读 SQL 语句"},
            },
            "required": ["sql"],
        }

    async def execute(self, args_json: str) -> str:
        try:
            args = json.loads(args_json)
            sql = args.get("sql", "") if isinstance(args, dict) else ""
            if not isinstance(sql, str):
                raise ValueError("sql 必须是字符串")
        except ValueError as e:
            return f"TOOL_ERROR: 解析参数: {e}"

        try:
            safe_sql = validate_read_only(self.db.driver, sql, self.query_limit)
        except Exception as e:
            # 交给 LLM 自己修正语句，属于业务错误而非系统错误。
            return "TOOL_ERROR: SQL 被只读防护拦截: " + str(e)

        # 表级 ACL：提取 SQL 引用的全部物理表并逐一校验
        # （含子查询、JOIN、逗号多表、CTE 体内的引用；字符串字面量已掩码）
        # 开启 ACL 时用严格提取（解析失败即拒绝）；未开启时无需解析。
        allow = acl.tables_from_context()
        try:
            if allow is not None:
                tables = acl.extract_tables_strict(self.db.driver, safe_sql)
                acl.check_tables_acl(allow, tables)
        except acl.TableNotAllowed as e:
            return "TOOL_ERROR: " + str(e)

        limit = self.max_bytes if self.max_bytes > 0 else 24 * 1024
        try:
            columns, rows, truncated = await self.db.query_json(safe_sql, MAX_ROWS, CELL_LIMIT, limit)
        except Exception as e:
            return "TOOL_ERROR: " + str(e)
        return must_json({
            "sql": safe_sql,
            "columns": columns,
            "rows": rows,
            "row_count": len(rows),
            "truncated": truncated,
        })
