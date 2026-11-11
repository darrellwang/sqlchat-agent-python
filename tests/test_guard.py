"""只读 SQL 校验器测试（移植自 Go 版 internal/db/guard_test.go）。

通用用例覆盖两种方言；方言差异用例覆盖 # 注释 / JSONB 操作符 / EXPLAIN ANALYZE 等。
"""

import pytest

from mysqlagent.db.guard import (
    DRIVER_MYSQL,
    DRIVER_POSTGRES,
    ReadOnlyViolation,
    validate_read_only,
)

# (名称, 输入, 期望处理后 SQL, 是否期望拒绝)
COMMON_CASES = [
    # 正常只读语句
    ("select 无 limit 自动追加", "SELECT * FROM users", "SELECT * FROM users LIMIT 100", False),
    ("select 已有 limit 不追加", "SELECT * FROM users LIMIT 5", "SELECT * FROM users LIMIT 5", False),
    ("select 小写 limit 不追加", "select * from users limit 10", "select * from users limit 10", False),
    ("show 语句", "SHOW TABLES", "SHOW TABLES", False),
    ("describe 语句", "DESCRIBE users", "DESCRIBE users", False),
    ("desc 语句", "DESC users", "DESC users", False),
    ("explain 语句", "EXPLAIN SELECT 1", "EXPLAIN SELECT 1", False),
    ("cte 允许", "WITH t AS (SELECT 1 AS n) SELECT * FROM t",
     "WITH t AS (SELECT 1 AS n) SELECT * FROM t LIMIT 100", False),
    ("结尾分号被去除", "SELECT 1;", "SELECT 1 LIMIT 100", False),

    # 写操作拦截
    ("insert", "INSERT INTO users VALUES (1)", "", True),
    ("update", "UPDATE users SET name='x' WHERE id=1", "", True),
    ("delete", "DELETE FROM users WHERE id=1", "", True),
    ("drop", "DROP TABLE users", "", True),
    ("truncate", "TRUNCATE TABLE users", "", True),
    ("alter", "ALTER TABLE users ADD COLUMN c INT", "", True),
    ("create", "CREATE TABLE t (id INT)", "", True),
    ("cte 接 insert 被拦截", "WITH t AS (SELECT 1) INSERT INTO users SELECT * FROM t", "", True),
    ("pg select into 建表被拦截", "SELECT * INTO new_table FROM users", "", True),
    ("for update 被拦截", "SELECT * FROM users WHERE id=1 FOR UPDATE", "", True),
    ("pg for share 被拦截", "SELECT * FROM users WHERE id=1 FOR SHARE", "", True),
    ("into outfile 被拦截", "SELECT * FROM users INTO OUTFILE '/tmp/x'", "", True),
    ("sleep 被拦截", "SELECT SLEEP(10)", "", True),
    ("pg_sleep 被拦截", "SELECT pg_sleep(10)", "", True),
    ("pg_terminate_backend 被拦截", "SELECT pg_terminate_backend(123)", "", True),
    ("set_config 被拦截", "SELECT set_config('search_path', 'evil', false)", "", True),
    ("grant 被拦截", "GRANT ALL ON *.* TO u", "", True),
    ("set 被拦截", "SET @x = 1", "", True),
    ("vacuum 首词被白名单拦截", "VACUUM ANALYZE users", "", True),

    # 注入与多语句拦截
    ("多语句", "SELECT 1; DROP TABLE users", "", True),
    ("行注释", "SELECT 1 -- comment\n", "", True),
    ("块注释", "SELECT /* c */ 1", "", True),

    # 词边界不误伤
    ("user 列名不误伤 use", "SELECT user FROM t", "SELECT user FROM t LIMIT 100", False),
    ("offset 不误伤 set", "SELECT * FROM t ORDER BY id LIMIT 10 OFFSET 20",
     "SELECT * FROM t ORDER BY id LIMIT 10 OFFSET 20", False),
    ("库名.表名 不误伤 use", "SELECT * FROM my_db.user", "SELECT * FROM my_db.user LIMIT 100", False),
]

# (名称, 驱动, 输入, 期望处理后 SQL, 是否期望拒绝)
DIALECT_CASES = [
    # # 在 MySQL 是注释符（拦截）；在 PG 是位运算 / JSONB 路径操作符（放行）
    ("mysql 井号注释被拦截", DRIVER_MYSQL, "SELECT 1 # hack", "", True),
    ("pg jsonb 路径操作符放行", DRIVER_POSTGRES, "SELECT data #> '{a,b}' FROM docs",
     "SELECT data #> '{a,b}' FROM docs LIMIT 100", False),
    ("pg 位异或放行", DRIVER_POSTGRES, "SELECT a # b FROM t", "SELECT a # b FROM t LIMIT 100", False),
    # PG 专有合法只读
    ("pg explain analyze 放行", DRIVER_POSTGRES, "EXPLAIN ANALYZE SELECT * FROM t",
     "EXPLAIN ANALYZE SELECT * FROM t", False),
    ("pg ilike 放行", DRIVER_POSTGRES, "SELECT * FROM t WHERE name ILIKE 'x%'",
     "SELECT * FROM t WHERE name ILIKE 'x%' LIMIT 100", False),
    ("pg show 运行时参数", DRIVER_POSTGRES, "SHOW work_mem", "SHOW work_mem", False),
]


@pytest.mark.parametrize("name,sql,want,want_err", COMMON_CASES)
@pytest.mark.parametrize("driver", [DRIVER_MYSQL, DRIVER_POSTGRES])
def test_validate_read_only(name: str, sql: str, want: str, want_err: bool, driver: str):
    if want_err:
        with pytest.raises(ReadOnlyViolation):
            validate_read_only(driver, sql, 100)
    else:
        got = validate_read_only(driver, sql, 100)
        assert got == want, f"{name}: got {got!r} want {want!r}"


@pytest.mark.parametrize("name,driver,sql,want,want_err", DIALECT_CASES)
def test_validate_read_only_dialect(name: str, driver: str, sql: str, want: str, want_err: bool):
    if want_err:
        with pytest.raises(ReadOnlyViolation):
            validate_read_only(driver, sql, 100)
    else:
        got = validate_read_only(driver, sql, 100)
        assert got == want, f"{name}: got {got!r} want {want!r}"


def test_empty_sql_rejected():
    with pytest.raises(ReadOnlyViolation, match="SQL 为空"):
        validate_read_only(DRIVER_POSTGRES, "   ;  ", 100)
