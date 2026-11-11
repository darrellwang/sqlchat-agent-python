"""mysqlagent.db.acl — 表级 ACL（权限交集铁律）+ SQL 表名提取。

移植自 Go 版 internal/db/acl.go：
  - 工具可访问范围：database.tables（全局清单；空 = 不限制）
  - 调用者范围：api_keys[].tables（每身份清单；空 = 继承全局）
  - 实际允许 = 两者的交集（任一为空集约束时取另一方）

SQL 表名提取覆盖：FROM / JOIN 子句（含逗号分隔多表、schema.table、
双引号标识符、别名）、DESCRIBE/DESC/EXPLAIN 的目标表；
剔除 CTE 名（WITH 子句定义）与表函数（后跟 "(" 的标识符，如 generate_series）。

并发请求的允许表清单通过 contextvars 传递（对应 Go 的 context value）。
"""

from __future__ import annotations

import re
from contextvars import ContextVar
from typing import Optional

# 本请求允许访问的表清单注入点。None = 未注入（不限制）；
# 空列表 = 交集为空（拒绝全部表）——与 Go 版 nil / 空切片语义一致。
_allowed_tables: ContextVar[Optional[list[str]]] = ContextVar("mysqlagent_allowed_tables", default=None)


def context_with_tables(tables: Optional[list[str]]) -> "object":
    """把本请求允许访问的表清单注入当前上下文，返回重置令牌。

    用法：
        token = context_with_tables(["users"])
        try:
            ...
        finally:
            reset_allowed_tables(token)
    """
    return _allowed_tables.set(tables)


def reset_allowed_tables(token: object) -> None:
    _allowed_tables.reset(token)  # type: ignore[arg-type]


def tables_from_context() -> Optional[list[str]]:
    """取出当前请求的允许表清单（None = 不限制）。"""
    return _allowed_tables.get()


def normalize_table_name(s: str) -> str:
    """归一化表名：去双引号、去 schema 前缀、转小写。"""
    s = s.strip()
    s = s.strip('"')
    i = s.rfind(".")
    if i >= 0:
        s = s[i + 1:]
    return s.strip().lower()


def effective_tables(global_: Optional[list[str]], key: Optional[list[str]]) -> Optional[list[str]]:
    """计算实际允许表清单（权限交集）。

    global 与 key 任一为空表示该层不设限；两者都设限时取交集。
    返回 None 表示不限制；返回空列表表示交集为空（拒绝全部表）。
    """
    if not key:
        return global_
    if not global_:
        return key
    allowed = {normalize_table_name(t) for t in global_}
    # 空列表（非 None）：交集为空 = 全拒（不能误判为不限制）
    return [normalize_table_name(t) for t in key if normalize_table_name(t) in allowed]


class TableNotAllowed(Exception):
    """表不在允许清单内。"""


def check_tables_acl(allow: Optional[list[str]], names: list[str]) -> None:
    """校验 names 中的每个表名是否都在允许清单内。

    allow 为 None 表示不限制；空列表表示拒绝全部表。
    """
    if allow is None:
        return
    allowed = {normalize_table_name(t) for t in allow}
    for n in names:
        if normalize_table_name(n) not in allowed:
            raise TableNotAllowed(f"表 {n!r} 不在当前身份允许访问的表清单内")


# ---------- SQL 表名提取 ----------

# FROM / JOIN 关键字定位
FROM_JOIN_POS = re.compile(r"(?i)\b(FROM|JOIN)\b")
# 标识符：双引号包裹 或 字母/下划线开头（re.ASCII 对齐 Go 的 \w = [0-9A-Za-z_]）
IDENT_START = re.compile(r'^("[^"]+"|[A-Za-z_][\w$]*)', re.ASCII)
# DESCRIBE / DESC / EXPLAIN 的目标表（首词语句，非 EXPLAIN SELECT 形态）
DESCRIBE_TARGET = re.compile(
    r'(?i)^\s*(?:DESCRIBE|DESC|EXPLAIN)\s+(?:(?:ANALYZE|VERBOSE)\s+)*(?:"([^"]+)"|([A-Za-z_][\w$]*))',
    re.ASCII,
)
# 语句关键字（EXPLAIN 后跟这些词时目标是语句而非表）
STATEMENT_KEYWORD = re.compile(r"(?i)^(SELECT|SHOW|DESCRIBE|DESC|EXPLAIN|WITH|TABLE)\b")

# SQL 子句关键字——FROM/JOIN 目标后出现时不应被当作表别名消费。
SQL_KEYWORD = {
    "as", "on", "where", "group", "order", "limit", "offset", "join",
    "left", "right", "inner", "outer", "cross", "full", "natural",
    "union", "intersect", "except", "using", "having", "for", "window",
    "returning", "set", "fetch", "only", "lateral", "tablesample",
}


def extract_tables(sql: str) -> list[str]:
    """提取 SQL 中引用的全部物理表名（归一化后的小写裸表名）。

    已剔除 CTE 名与表函数。仅用于 ACL 校验，不追求完整 SQL 解析。
    """
    seen: set[str] = set()
    out: list[str] = []

    def add(name: str) -> None:
        n = normalize_table_name(name)
        if n != "" and n not in seen:
            seen.add(n)
            out.append(n)

    # 先掩码单引号字符串字面量，避免 'no from here' 之类内容干扰关键字匹配
    sql = mask_string_literals(sql)
    ctes = extract_cte_names(sql)

    # DESCRIBE/DESC/EXPLAIN 目标表
    m = DESCRIBE_TARGET.match(sql)
    if m:
        name = m.group(1) or m.group(2) or ""
        # EXPLAIN SELECT / SHOW / WITH 等目标为语句时不算表
        if not STATEMENT_KEYWORD.match(name):
            add(name)

    # FROM / JOIN 子句（含逗号分隔的多表列表）
    s = sql
    while True:
        loc = FROM_JOIN_POS.search(s)
        if loc is None:
            break
        rest = s[loc.end():]
        s = s[loc.end():]  # 继续从当前位置向后扫描
        while True:
            rest = rest.lstrip(" \t\r\n")
            if rest.startswith("("):
                break  # 子查询：跳到下一个 FROM/JOIN（内层由后续扫描覆盖）
            m = IDENT_START.match(rest)
            if m is None:
                break
            ident = m.group(0)
            rest = rest[len(ident):]
            # schema.table：取点号后的部分作为表名
            table_name = ident
            rest2 = rest.lstrip(" \t\r\n")
            if rest2.startswith("."):
                rest3 = rest2[1:].lstrip(" \t\r\n")
                m2 = IDENT_START.match(rest3)
                if m2 is not None:
                    table_name = m2.group(0)
                    rest = rest3[len(m2.group(0)):]
            # 表函数（generate_series 等）：标识符后紧跟 "("，不是物理表
            after = rest.lstrip(" \t\r\n")
            if after.startswith("("):
                break
            # 非 CTE 名才是物理表
            if normalize_table_name(table_name) not in ctes:
                add(table_name)
            # 跳过可选别名（alias / AS alias），再看是否逗号分隔的多表
            rest = skip_alias(rest)
            after = rest.lstrip(" \t\r\n")
            if after.startswith(","):
                rest = after[1:]
                continue
            break
    return out


def mask_string_literals(s: str) -> str:
    """把单引号字符串字面量替换为等长空白。

    （避免字符串内容里的 FROM/JOIN/逗号等干扰表名提取。）
    """
    b = list(s)
    in_str = False
    for i, c in enumerate(b):
        if in_str:
            if c == "'":
                in_str = False
            b[i] = " "
        elif c == "'":
            in_str = True
            b[i] = " "
    return "".join(b)


def skip_alias(s: str) -> str:
    """跳过 FROM/JOIN 目标表后可选的别名（alias 或 AS alias 形态），返回剩余未消费的字符串。"""
    s = s.lstrip(" \t\r\n")
    m = IDENT_START.match(s)
    if m is None:
        return s
    if m.group(0).strip('"').lower() in SQL_KEYWORD:
        return s  # 是子句关键字，不是别名
    s = s[len(m.group(0)):]
    # AS alias：AS 后必跟标识符
    s2 = s.lstrip(" \t\r\n")
    if s2[:2].upper() == "AS":
        rest = s2[2:].lstrip(" \t\r\n")
        m2 = IDENT_START.match(rest)
        if m2 is not None:
            return rest[len(m2.group(0)):]
    return s


def extract_cte_names(sql: str) -> set[str]:
    """解析 WITH 子句前缀定义的 CTE 名（小写）。

    仅当语句以 WITH 开头时有效；用括号配对扫描跳过 CTE 体，
    避免把 SELECT 列表中的 ", x AS" 别名误判为 CTE。
    """
    s = sql.strip()
    if not s[:4].upper() == "WITH":
        return set()
    s = s[4:].strip()
    if s[:9].upper() == "RECURSIVE":
        s = s[9:].strip()
    names: set[str] = set()
    while True:
        m = IDENT_START.match(s)
        if m is None:
            return names
        names.add(normalize_table_name(m.group(0)))
        s = s[len(m.group(0)):].strip()
        # 可选的列名列表 name(a, b) AS (...)
        if s.startswith("("):
            end, ok = _match_paren(s)
            if not ok:
                return names
            s = s[end + 1:].strip()
        if not s[:2].upper() == "AS":
            return names
        s = s[2:].strip()
        if not s.startswith("("):
            return names  # AS MATERIALIZED (...) 等罕见形态：放弃后续解析
        end, ok = _match_paren(s)
        if not ok:
            return names
        s = s[end + 1:].strip()
        if s.startswith(","):
            s = s[1:].strip()
            continue
        return names


def _match_paren(s: str) -> tuple[int, bool]:
    """返回与 s[0]=='(' 配对的右括号下标（跳过单引号字符串字面量）。"""
    depth = 0
    in_str = False
    for i, c in enumerate(s):
        if in_str:
            if c == "'":
                in_str = False
            continue
        if c == "'":
            in_str = True
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i, True
    return 0, False


# ---------- 严格模式（失败即拒绝） ----------
# 正则提取只是"尽力而为"；开启 ACL 时必须保证"提取不到 ≠ 放行"。
# 严格模式叠加三层：
#   1. sqlglot 解析 AST，取全部物理表（覆盖反引号 / 子查询 / LATERAL / ONLY 等）；
#   2. 与正则提取结果取并集（两条路径任一发现的表都要过 ACL）；
#   3. 解析失败、裸 TABLE 语句、SHOW、非默认 schema 的限定名一律拒绝。

_BARE_TABLE_KW = re.compile(r"(?i)\bTABLE\b")
_EXPLAIN_PREFIX = re.compile(r"(?is)^\s*EXPLAIN\s+(?:\([^)]*\)\s*|ANALYZE\s+|VERBOSE\s+)*")
# 允许作为限定名前缀的默认 schema（PG 的 public）。
_DEFAULT_SCHEMAS = {"", "public"}


def extract_tables_strict(driver: str, sql: str) -> list[str]:
    """严格提取 SQL 引用的全部物理表（已归一化）。无法确定时抛 TableNotAllowed。"""
    import sqlglot
    from sqlglot import exp

    dialect = "mysql" if driver == "mysql" else "postgres"
    masked = mask_string_literals(sql)

    if _BARE_TABLE_KW.search(masked):
        raise TableNotAllowed("启用表级 ACL 时不允许使用 TABLE 语句，请改用 SELECT ... FROM")

    names: list[str] = list(extract_tables(sql))  # 路径 2：正则

    stripped = sql
    head = masked.lstrip()[:8].upper()
    if head.startswith("EXPLAIN"):
        # EXPLAIN [ANALYZE] <语句>：对被解释的语句本体做 AST 检查
        stripped = sql.lstrip()
        stripped = stripped[_EXPLAIN_PREFIX.match(stripped).end():]
    elif head.startswith("SHOW"):
        raise TableNotAllowed("启用表级 ACL 时不允许 SHOW 语句，请使用 list_tables / describe_table 工具")
    elif head.startswith("DESC"):
        return names  # DESCRIBE 目标已由正则提取

    try:
        trees = sqlglot.parse(stripped, read=dialect)
    except Exception as e:  # noqa: BLE001 - 任何解析失败都按拒绝处理
        raise TableNotAllowed(f"无法解析 SQL 以校验表权限，已拒绝: {type(e).__name__}") from e
    if len(trees) != 1 or trees[0] is None:
        raise TableNotAllowed("无法解析 SQL 以校验表权限，已拒绝")
    tree = trees[0]
    if not isinstance(tree, exp.Query):
        raise TableNotAllowed("无法识别的语句类型，已拒绝（启用表级 ACL 时仅允许 SELECT / WITH / EXPLAIN SELECT）")

    # CTE：仅"无限定名 + 不在自身定义体内"的引用才算 CTE，
    # 防 WITH salaries AS (SELECT * FROM salaries) 里的内层 salaries 被当成 CTE 漏检。
    cte_names = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
    recursive = any(w.args.get("recursive") for w in tree.find_all(exp.With))
    for t in tree.find_all(exp.Table):
        if not isinstance(t.this, exp.Identifier):
            continue  # 表函数（generate_series / unnest 等）
        schema = (t.db or "").lower()
        if t.catalog or schema not in _DEFAULT_SCHEMAS:
            raise TableNotAllowed(f"不允许跨 schema / 库引用表 {t.sql(dialect=dialect)!r}")
        name = t.name
        if schema == "" and name.lower() in cte_names:
            inside_own_def = False
            p = t.parent
            while p is not None:
                if isinstance(p, exp.CTE) and p.alias_or_name.lower() == name.lower():
                    inside_own_def = True
                    break
                p = p.parent
            if not inside_own_def or recursive:
                continue  # 对 CTE 的正常引用
        names.append(name)

    seen: set[str] = set()
    out: list[str] = []
    for n in names:
        k = normalize_table_name(n)
        if k and k not in seen:
            seen.add(k)
            out.append(k)
    return out
