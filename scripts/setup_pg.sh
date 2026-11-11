#!/usr/bin/env bash
# mysqlagent 演示环境：WSL 内 PostgreSQL 安装 + 初始化 + 模拟数据（以 root 运行）
set -euo pipefail

SEED_SQL="/mnt/d/Work/mysqlagent/scripts/seed.sql"
DB_NAME="mysqlagent_demo"
DB_USER="mysqlagent"
DB_PASS="mysqlagent123"

echo "== 1. 安装 PostgreSQL =="
if ! command -v psql >/dev/null 2>&1; then
    apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq postgresql postgresql-contrib
else
    echo "已安装: $(psql --version)"
fi

echo "== 2. 启动 PostgreSQL =="
if command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then
    systemctl enable --now postgresql 2>/dev/null || true
    systemctl start postgresql 2>/dev/null || true
else
    service postgresql start || pg_ctlcluster "$(ls /etc/postgresql | head -1)" main start
fi
sleep 1

echo "== 3. 创建数据库与账号 =="
sudo -u postgres psql -v ON_ERROR_STOP=1 <<SQL
DO \$\$ BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '${DB_USER}') THEN
        CREATE ROLE ${DB_USER} LOGIN PASSWORD '${DB_PASS}';
    ELSE
        ALTER ROLE ${DB_USER} LOGIN PASSWORD '${DB_PASS}';
    END IF;
END \$\$;
SQL
sudo -u postgres psql -tc "SELECT 1 FROM pg_database WHERE datname='${DB_NAME}'" | grep -q 1 \
    || sudo -u postgres createdb -O "${DB_USER}" "${DB_NAME}"

echo "== 4. 写入模拟数据（以业务账号执行，表 owner 对齐） =="
PGPASSWORD="${DB_PASS}" psql -v ON_ERROR_STOP=1 -h 127.0.0.1 -U "${DB_USER}" -d "${DB_NAME}" -f "${SEED_SQL}"

echo "== 5. 验证 =="
PGPASSWORD="${DB_PASS}" psql -h 127.0.0.1 -U "${DB_USER}" -d "${DB_NAME}" -c "\dt"

echo "== 完成 =="
