"""严格 ACL 模式回归测试：覆盖 review 中实测的全部绕过写法 + 装配层冒烟。"""

import pytest

from mysqlagent.db import acl
from mysqlagent.db.guard import ReadOnlyViolation, validate_read_only
from mysqlagent.tools.dbtools import ExecuteSQL


def strict(driver, sql):
    return acl.extract_tables_strict(driver, validate_read_only(driver, sql, 100))


@pytest.mark.parametrize("driver,sql,expected", [
    # 之前提取为空的绕过写法：现在必须识别出 salaries
    ("mysql", "SELECT * FROM `salaries`", "salaries"),
    ("postgres", "SELECT * FROM generate_series(1,2) g, salaries", "salaries"),
    ("postgres", "SELECT * FROM (SELECT 1) x, salaries", "salaries"),
    ("postgres", "SELECT * FROM a, LATERAL (SELECT * FROM salaries) z", "salaries"),
    ("postgres", "SELECT * FROM ONLY salaries", "salaries"),
    ("postgres", "SELECT * FROM t WHERE x IN (SELECT x FROM salaries)", "salaries"),
    # CTE 同名遮蔽：内层引用的是真实表
    ("postgres", "WITH salaries AS (SELECT * FROM salaries) SELECT * FROM salaries", "salaries"),
    ("postgres", "EXPLAIN ANALYZE SELECT * FROM salaries", "salaries"),
])
def test_strict_finds_hidden_tables(driver, sql, expected):
    assert expected in strict(driver, sql)


@pytest.mark.parametrize("driver,sql", [
    ("postgres", "WITH x AS (TABLE salaries) SELECT * FROM x"),   # 裸 TABLE
    ("postgres", "SELECT 1 UNION TABLE salaries"),                # 裸 TABLE + 解析失败
    ("postgres", "SELECT * FROM other_schema.users"),             # 跨 schema
    ("postgres", "SELECT * FROM pg_catalog.pg_shadow"),           # 系统目录
    ("mysql", "SELECT * FROM mysql.user"),                        # 跨库
    ("postgres", "SHOW ALL"),                                     # SHOW 在 ACL 下拒绝
    ("postgres", "SELECT * FROM ((("),                            # 解析失败 → 拒绝
])
def test_strict_rejects(driver, sql):
    with pytest.raises(acl.TableNotAllowed):
        try:
            validate_read_only(driver, sql, 100)
        except ReadOnlyViolation:
            raise acl.TableNotAllowed("guard")  # guard 先拦也算拒绝
        strict(driver, sql)


def test_public_schema_prefix_allowed():
    assert strict("postgres", "SELECT * FROM public.orders") == ["orders"]


def test_cte_reference_not_treated_as_table():
    assert strict("postgres", "WITH x AS (SELECT * FROM orders) SELECT * FROM x") == ["orders"]


@pytest.mark.parametrize("sql", [
    "SELECT query_to_xml('select * from salaries', true, true, '')",
    "SELECT table_to_xml('salaries', true, true, '')",
    "SELECT dblink_exec('x','y')",
    "SELECT pg_read_binary_file('/etc/passwd')",
    "SELECT nextval('s')",
])
def test_guard_blocks_acl_bypass_functions(sql):
    with pytest.raises(ReadOnlyViolation):
        validate_read_only("postgres", sql, 100)


def test_guard_no_false_positive_on_string_literals():
    assert "please update me" in validate_read_only("postgres", "SELECT * FROM t WHERE note = 'please update me'", 100)
    assert validate_read_only("postgres", "SELECT * FROM t WHERE a = 'x;y'", 100).endswith("LIMIT 100")
    assert validate_read_only("postgres", "SELECT '--'", 100).startswith("SELECT '--'")


def test_guard_limit_handling():
    # FETCH FIRST / 子查询里的 LIMIT 不应影响顶层判定
    assert "LIMIT 100" not in validate_read_only("postgres", "SELECT * FROM t FETCH FIRST 5 ROWS ONLY", 100)
    out = validate_read_only("postgres", "SELECT * FROM (SELECT * FROM t LIMIT 5) x", 100)
    assert out.endswith("LIMIT 100")


class _FakeDB:
    driver = "postgres"

    async def query_json(self, *a, **k):
        return ["a"], [["1"]], False


async def test_execute_sql_denies_bypass_under_acl():
    tool = ExecuteSQL(_FakeDB())
    tok = acl.context_with_tables(["orders"])
    try:
        out = await tool.execute('{"sql": "SELECT * FROM `salaries`"}')  # PG 下反引号会解析失败 → 拒绝
        assert out.startswith("TOOL_ERROR")
        out = await tool.execute('{"sql": "SELECT * FROM (SELECT 1) x, salaries"}')
        assert out.startswith("TOOL_ERROR") and "salaries" in out
        out = await tool.execute('{"sql": "SELECT * FROM orders"}')
        assert not out.startswith("TOOL_ERROR")
    finally:
        acl.reset_allowed_tables(tok)


def test_build_registry_wiring():
    """装配层冒烟：曾因 ExecuteSQL(DB=...) 参数名错误导致启动即崩溃。"""
    from mysqlagent.config import Config, _apply_defaults
    from mysqlagent.main import build_registry

    cfg = Config()
    _apply_defaults(cfg)
    reg = build_registry(_FakeDB(), cfg)
    assert reg.names() == ["describe_table", "execute_sql", "list_tables"]


def test_display_addr():
    from mysqlagent.main import display_addr

    assert display_addr(":8080") == "localhost:8080"
    assert display_addr("0.0.0.0:9000") == "localhost:9000"
    assert display_addr("10.1.2.3:80") == "10.1.2.3:80"
