"""mysqlagent.db.guard — 只读 SQL 校验器（MySQL / PostgreSQL 方言感知）。

移植自 Go 版 internal/db/guard.go，行为逐条对齐：
  1. 去首尾空白与结尾分号后，不允许残留任何分号（拒绝多语句）；
  2. 拒绝注释（-- / /* 一律拒绝；# 仅 MySQL 方言拒绝）防注入；
  3. 首词必须在白名单内（SELECT / SHOW / DESCRIBE / DESC / EXPLAIN / WITH）；
  4. 全文不得命中危险关键词黑名单（含 WITH ... INSERT、SELECT ... INTO 等组合）；
  5. SELECT / WITH 语句若无 LIMIT，自动追加 forceLimit，防止全表扫描拖垮库。
"""

from __future__ import annotations

import re

from mysqlagent.db.acl import mask_string_literals

# 支持的数据库驱动（与 db/sql.py 的常量保持一致）。
DRIVER_MYSQL = "mysql"
DRIVER_POSTGRES = "postgres"

# 只读语句首词白名单：SELECT / SHOW / DESCRIBE / DESC / EXPLAIN / WITH。
# 对 MySQL 与 PostgreSQL 同时适用（DESCRIBE 仅 MySQL 有效，误用时由
# 数据库报语法错误、LLM 自行纠正，不会产生安全风险）。
ALLOWED_FIRST_WORDS = {
    "SELECT": True,
    "SHOW": True,
    "DESCRIBE": True,
    "DESC": True,
    "EXPLAIN": True,
    "WITH": True,
}

# 危险关键词黑名单（词边界匹配，大小写不敏感），统一覆盖两种方言：
#   - DML / DDL / 权限 / 会话：拦截 WITH ... INSERT/UPDATE/DELETE 等 CTE 后接写语句
#   - INTO：拦截 PG 的 SELECT ... INTO 建表、MySQL 的 SELECT ... INTO OUTFILE
#   - 行锁模式：FOR UPDATE / FOR SHARE / FOR NO KEY UPDATE / FOR KEY SHARE / LOCK IN SHARE MODE
#   - 文件与危险函数：MySQL 的 LOAD_FILE/SLEEP/BENCHMARK；PG 的 pg_sleep/pg_read_file/
#     pg_ls_dir/pg_terminate_backend/pg_cancel_backend/set_config/dblink
#   - 首词类语句（VACUUM/REINDEX/COPY 等）已被白名单拦截，此处为纵深防御
#
# 说明：ANALYZE 不在黑名单中，因为 PG 的 EXPLAIN ANALYZE 是合法只读语句，
# 而单独的 ANALYZE 语句会被首词白名单拦截。
FORBIDDEN_PATTERN = re.compile(
    r"(?i)\b(INSERT|UPDATE|DELETE|REPLACE|CREATE|DROP|ALTER|TRUNCATE|RENAME|GRANT|REVOKE|"
    r"LOCK|UNLOCK|CALL|SET|USE|LOAD|HANDLER|PREPARE|EXECUTE|DEALLOCATE|"
    r"VACUUM|REINDEX|CLUSTER|LISTEN|NOTIFY|CHECKPOINT|DISCARD|"
    r"BENCHMARK|SLEEP|PG_SLEEP|LOAD_FILE|PG_READ_FILE|PG_LS_DIR|"
    r"PG_TERMINATE_BACKEND|PG_CANCEL_BACKEND|SET_CONFIG|INTO)\b"
    # 函数族（前缀匹配）：能读文件 / 执行动态 SQL / 产生锁与序列副作用 / 绕过表级 ACL
    r"|\b(DBLINK\w*|PG_READ_\w+|PG_LS_\w+|PG_STAT_FILE|PG_RELOAD_CONF|PG_NOTIFY|PG_ADVISORY_\w+|"
    r"LO_(IMPORT|EXPORT|CREATE|CREAT|UNLINK|PUT|FROM_BYTEA|OPEN|WRITE|GET)|NEXTVAL|SETVAL|"
    r"QUERY_TO_XML\w*|TABLE_TO_XML\w*|CURSOR_TO_XML\w*|SCHEMA_TO_XML\w*|DATABASE_TO_XML\w*|"
    r"GET_LOCK|RELEASE_LOCK|RELEASE_ALL_LOCKS|IS_FREE_LOCK|IS_USED_LOCK)\b"
    r"|\bFOR\s+(UPDATE|SHARE|NO\s+KEY\s+UPDATE|KEY\s+SHARE)\b"
    r"|\bLOCK\s+IN\s+SHARE\s+MODE\b"
)

# 注释检测：-- 与 /* 在两种方言中都是注释，一律拒绝。
# # 仅在 MySQL 中是注释符；PostgreSQL 中 # 是位运算符和 JSONB 路径操作符
# （如 data #> '{a,b}'），因此对 PG 不拦截。
DASH_COMMENT = re.compile(r"--|/\*")
HASH_COMMENT = re.compile(r"#")
LIMIT_PATTERN = re.compile(r"(?i)\bLIMIT\b|\bFETCH\s+(FIRST|NEXT)\b")
_PAREN_INNER = re.compile(r"\([^()]*\)")


class ReadOnlyViolation(Exception):
    """SQL 未通过只读校验。"""


def _top_level(s: str) -> str:
    """去掉全部括号内容，只留最外层文本（用于判断顶层是否已有 LIMIT）。"""
    prev = None
    while prev != s:
        prev = s
        s = _PAREN_INNER.sub(" ", s)
    return s


def validate_read_only(driver: str, sql: str, force_limit: int) -> str:
    """校验一条 SQL 是否为安全的只读语句，返回处理后的 SQL；不合法时抛出 ReadOnlyViolation。

    driver 取值见 DRIVER_MYSQL / DRIVER_POSTGRES 常量。
    所有结构性检查（分号 / 注释 / 关键词）都在「字符串字面量已掩码」的文本上进行，
    避免 'please update me' 这类数据内容被误拦；掩码方向是保守的（遇到反斜杠转义、
    美元引用等歧义时，被掩码的范围只会更小，即检查更严格）。
    """
    s = sql.strip()
    s = s.rstrip(";").strip()

    if s == "":
        raise ReadOnlyViolation("SQL 为空")
    m = mask_string_literals(s)
    if ";" in m:
        raise ReadOnlyViolation("不允许执行多语句")
    if DASH_COMMENT.search(m):
        raise ReadOnlyViolation("不允许包含注释")
    if driver == DRIVER_MYSQL and HASH_COMMENT.search(m):
        raise ReadOnlyViolation("不允许包含注释")

    upper = m.upper()
    first = upper
    idx = min(
        (p for p in (upper.find(c) for c in " \t\r\n(") if p >= 0),
        default=-1,
    )
    if idx >= 0:
        first = upper[:idx]
    if not ALLOWED_FIRST_WORDS.get(first):
        raise ReadOnlyViolation(
            f"只允许只读语句（SELECT/SHOW/DESCRIBE/EXPLAIN/WITH），当前语句以 {first!r} 开头"
        )
    if FORBIDDEN_PATTERN.search(m):
        raise ReadOnlyViolation("语句包含被禁止的关键词（写操作 / DDL / 权限 / 锁 / 文件操作）")

    if force_limit > 0 and first in ("SELECT", "WITH") and not LIMIT_PATTERN.search(_top_level(m)):
        s = s + f" LIMIT {force_limit}"
    return s
