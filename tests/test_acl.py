"""表级 ACL 与 SQL 表名提取测试（移植自 Go 版 internal/db/acl_test.go）。"""

import pytest

from mysqlagent.db import acl


EXTRACT_CASES = [
    ("单表", "SELECT * FROM users", ["users"]),
    ("带别名", "SELECT u.id FROM users u WHERE u.id > 1", ["users"]),
    ("JOIN", "SELECT * FROM orders o JOIN users u ON o.user_id = u.id", ["orders", "users"]),
    ("LEFT JOIN", "SELECT * FROM order_items oi LEFT JOIN orders o ON oi.order_id = o.id",
     ["order_items", "orders"]),
    ("逗号多表", "SELECT * FROM users, orders", ["users", "orders"]),
    ("逗号多表带别名", "SELECT * FROM users a, orders b WHERE a.id = b.user_id",
     ["users", "orders"]),
    ("schema 前缀", "SELECT * FROM public.users", ["users"]),
    ("双引号标识符", 'SELECT * FROM "UserTable"', ["usertable"]),
    ("子查询", "SELECT * FROM (SELECT * FROM users) t", ["users"]),
    ("子查询内 JOIN", "SELECT * FROM (SELECT o.id FROM orders o JOIN users u ON 1=1) x",
     ["orders", "users"]),
    ("CTE 名不算表，CTE 体内的物理表仍提取",
     "WITH top5 AS (SELECT * FROM orders LIMIT 5) SELECT * FROM top5", ["orders"]),
    ("多 CTE", "WITH a AS (SELECT 1), b AS (SELECT 2) SELECT * FROM a, b, users", ["users"]),
    ("RECURSIVE CTE",
     "WITH RECURSIVE r AS (SELECT 1 AS n UNION ALL SELECT n+1 FROM r WHERE n < 5) "
     "SELECT * FROM r JOIN users ON users.id = r.n", ["users"]),
    ("表函数不算表", "SELECT * FROM generate_series(1, 10) AS n", []),
    ("unnest 不算表", "SELECT * FROM unnest(ARRAY[1,2,3])", []),
    ("DESCRIBE", "DESCRIBE users", ["users"]),
    ("EXPLAIN 表", "EXPLAIN users", ["users"]),
    ("EXPLAIN SELECT 不把 SELECT 当表", "EXPLAIN SELECT * FROM orders", ["orders"]),
    ("EXPLAIN ANALYZE SELECT", "EXPLAIN ANALYZE SELECT count(*) FROM orders", ["orders"]),
    ("函数值 select 无表", "SELECT 1", []),
    ("无引号字符串里的 FROM", "SELECT 'no from here' AS x", []),
    ("字符串里的 FROM 关键字", "SELECT * FROM users WHERE name = 'from orders'", ["users"]),
]


@pytest.mark.parametrize("name,sql,want", EXTRACT_CASES)
def test_extract_tables(name: str, sql: str, want: list[str]):
    got = acl.extract_tables(sql)
    assert got == want, f"{name}: got {got} want {want}"


def test_check_tables_acl():
    allow = ["users", "orders"]
    acl.check_tables_acl(allow, ["users", "orders"])  # 清单内放行
    with pytest.raises(acl.TableNotAllowed):
        acl.check_tables_acl(allow, ["products"])  # 清单外拦截
    acl.check_tables_acl(allow, ["PUBLIC.Users"])  # schema 前缀归一化后放行
    acl.check_tables_acl(None, ["anything"])  # None 清单 = 不限制


def test_effective_tables():
    global_ = ["users", "orders", "products"]
    key = ["users", "orders"]

    # 两层都设限 → 交集
    assert acl.effective_tables(global_, key) == ["users", "orders"]
    # key 不设限 → 继承 global
    assert acl.effective_tables(global_, None) == global_
    # global 不设限 → 以 key 为准
    assert acl.effective_tables(None, key) == key
    # 都不设限 → 不限制
    assert acl.effective_tables(None, None) is None
    # 交集为空 → 空列表（全拒，不能误判为不限制）
    got = acl.effective_tables(["a"], ["b"])
    assert got is not None and len(got) == 0

    with pytest.raises(acl.TableNotAllowed):
        acl.check_tables_acl([], ["users"])  # 空非 None 清单拒绝全部表
    acl.check_tables_acl(None, ["users"])  # None 清单不限制


def test_acl_context_round_trip():
    # 无注入时应返回 None（不限制）
    assert acl.tables_from_context() is None

    token = acl.context_with_tables(["users"])
    try:
        assert acl.tables_from_context() == ["users"]
    finally:
        acl.reset_allowed_tables(token)
    assert acl.tables_from_context() is None
