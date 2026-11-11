# sqlchat-agent-python

用 Python 3 实现的对话式数据库分析 AI Agent——与 Go 版 [sqlchat-agent-go](https://github.com/darrellwang/sqlchat-agent-go) 功能对齐的完整移植（原名 mysqlagent）。用户以自然语言提问，Agent 通过工具调用（Tool Calling）查询数据库，由 LLM 分析并流式（SSE）返回结果。

**支持 MySQL 与 PostgreSQL 双数据库**（默认 PostgreSQL），只读安全防护按方言差异化实现。

技术栈：FastAPI + uvicorn（异步 HTTP/SSE）+ httpx（流式 LLM 客户端）+ psycopg3 / aiomysql（异步数据库驱动）+ aiosqlite（会话持久化），纯异步（asyncio）事件驱动，对应 Go 版的 goroutine 模型。

## 功能

- **对话式查询**：自然语言 → LLM 自主决定调用工具 → 分析 → 中文回答
- **双数据库支持**：`database.driver` 配置项切换 `postgres`（默认）/ `mysql`，元数据工具按方言自动适配
- **完整工具集**（LLM 自主编排调用顺序）：
  | 工具 | 作用 |
  |------|------|
  | `list_tables` | 列出全部表（名称 / 估算行数 / 注释；PG 用 pg_class，MySQL 用 information_schema） |
  | `describe_table` | 查看表结构：列、类型、键、默认值、注释、索引（参数化查询，无注入风险） |
  | `execute_sql` | 执行只读 SQL，返回 JSON 结果 |
- **只读安全防护**（按方言差异化）：
  - 首词白名单：仅允许 `SELECT / SHOW / DESCRIBE / DESC / EXPLAIN / WITH`
  - 关键词黑名单：拦截 DML / DDL / 权限 / 锁 / 文件操作；PG 专属拦截 `SELECT ... INTO` 建表、`pg_sleep`、`pg_terminate_backend`、`set_config` 等；MySQL 专属拦截 `INTO OUTFILE`、`LOAD_FILE` 等
  - 注释拦截方言化：`--` / `/*` 一律拒绝；`#` 仅 MySQL 拦截（PG 中是 JSONB 路径操作符 `#>` 与位运算符）
  - PG 的 `EXPLAIN ANALYZE` 合法放行
  - 拒绝多语句；无 `LIMIT` 的 SELECT 自动强制追加
  - 建议再叠加数据库只读账号做纵深防御
- **接入层安全**：
  - **认证**：`Authorization: Bearer <key>`，key 绑定身份（`server.api_keys`，容器可用 `API_KEYS` 环境变量注入，格式 `key1:name1,key2`）；未配置时认证关闭并打印警告
  - **限流**：per-key 令牌桶（QPS + 突发容量）+ 全局并发隔舱（Agent 会话级，429/503 + Retry-After）
  - **CORS**：白名单精确回显（`server.cors_origins` / `CORS_ORIGINS`），不再无条件 `*`
  - **提示注入护栏**：输入侧规则引擎（block/warn 分级：覆盖指令、越狱、窃取系统提示直接拦截；角色扮演、全量导出告警放行），超长输入拒绝；命中即产生结构化审计事件（`AUDIT` JSONL 日志）
  - **间接注入防护**：工具返回内容一律视为数据，命中注入模式的片段被隔离标记（`[ISOLATED-INSTRUCTION]`）后再进入 LLM 上下文；System Prompt 含指令层级声明
  - **超时逐层收敛**：请求整体 120s > 单次 LLM 调用 90s > 单条 SQL 15s，任一层先到期即优雅终止并释放会话锁
  - 对抗测试集：注入攻击样本 + 正常问题反例（防误伤）全量单测覆盖
- **可观测与熔断**：
  - **trace_id 全链路追踪**：每请求生成 trace ID，SSE 首发 `meta` 事件携带，贯穿全部审计事件
  - **step 级审计**：异步 JSONL 落盘（`audit.output` / `AUDIT_OUTPUT`），只落哈希不落原文；llm_call / tool_call / tool_result / session_start / session_done / 熔断与护栏事件全覆盖
  - **PII 掩码**：工具结果中的邮箱 / 手机号 / 身份证 / 银行卡在进入 LLM 上下文前掩码（保留首尾字符），命中产生 `pii_masked` 审计事件
  - **失控熔断**：步数上限 + 同一(工具,参数)重复调用熔断（默认 3 次）+ 单请求累计 token 预算（默认 100k）
  - **LLM 重试**：首 token 前对 429/5xx/连接错误指数退避重试（默认 2 次），流开始后绝不重试避免重复推送
  - **SSE 心跳**：15s 注释行保活（`: ping`），防代理空闲断连
  - **/metrics**：Prometheus 文本格式（请求量 / 会话结果 / token / 重试 / 工具调用 / 护栏命中 / 失败分类 / 线程数）
- **生产化**：
  - **表级 ACL**（权限交集铁律第一步）：`database.tables` 全局清单 ∩ `api_keys[].tables` 身份清单；SQL 表名提取覆盖子查询 / JOIN / 逗号多表 / CTE / schema 前缀 / 别名 / 表函数，字符串字面量已掩码防绕过；`list_tables` 按清单过滤、`describe_table` / `execute_sql` 逐表校验
  - **SQLite 持久化与回放**：`store.path`（`SESSIONS_DB`）开启后重启不丢会话（aiosqlite，WAL 模式）；审计事件同库一份；回放接口 `GET /api/sessions/{id}/messages` 与 `GET /api/sessions/{id}/audit?limit=200`（需认证）
  - **Helm Chart**：`helm/mysqlagent`（liveness + readiness 探针、resources、非 root + readOnlyRootFilesystem、ServiceMonitor 可选）
  - **Trivy + SBOM**：`make scan`（镜像 HIGH/CRITICAL 判定 + CycloneDX SBOM + 源码密钥/错误配置扫描）
- **SSE 流式**：打字机效果输出，工具调用过程（SQL、结果、耗时）实时可见
- **多轮会话**：会话管理，保留上下文
- **OpenAI 兼容**：适配 DeepSeek / OpenAI / LMStudio / vLLM 等任意兼容端点

## 架构

```
Web 聊天页 / curl
   │ POST /api/chat (SSE)
   ▼
FastAPI (uvicorn) ── 会话管理 / 认证 / 限流 / CORS
   ▼
Agent Loop (asyncio) ── 流式调 LLM ⇄ Tools 工具集
                          │            │
                          │      只读校验 (guard, 方言感知)
                          └──── MySQL / PostgreSQL (异步连接池)
```

SSE 事件：`message`（增量文本）· `tool_call`（工具调用）· `tool_result`（结果摘要）· `done` · `error`

内部并发模型（对应 Go 版的 goroutine）：
- 每个 `/api/chat` 请求：**生产者任务**（Agent 循环 + 收尾）与 **SSE 写出**（StreamingResponse 生成器）通过 `asyncio.Queue` 分离，保证逐事件实时推送；
- 心跳任务周期性投递 `: ping` 注释行；
- 审计事件经有界队列（512）交后台任务落盘，满则丢弃并计数；
- 会话级 `asyncio.Lock` 保证同一会话串行；全局 `asyncio.Semaphore` 做并发隔舱。

## 快速开始

环境要求：Python 3.11+（开发验证用 3.13）。

```bash
# 1. 创建虚拟环境并安装依赖（国内网络建议设置镜像）
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
pip install -e ".[dev]"          # 含 pytest / pytest-asyncio

# 2. 准备配置
cp config.yaml.example config.yaml
#    编辑 llm.base_url / api_key / model 和 database.driver / database.dsn

# 3. 运行
python main.py                   # 或 python -m mysqlagent
#    Windows: python main.py

# 4. 打开聊天页
#    http://localhost:8080
```

## Docker 部署

### 构建镜像

```bash
docker build -t mysqlagent:latest .
```

多阶段构建：`python:3.13-slim` 安装依赖到 venv → 同基座运行阶段（含 ca-certificates / tzdata，非 root 用户运行，`PYTHONUNBUFFERED` + `PYTHONDONTWRITEBYTECODE`）。

### 方式一：环境变量注入（推荐，见 docker-compose.yml）

```bash
docker run -d --name mysqlagent --network host --restart unless-stopped \
  -e SERVER_ADDR=":8080" \
  -e LLM_BASE_URL="http://localhost:1234/v1" \
  -e LLM_MODEL="qwen/qwen3.6-35b-a3b" \
  -e DB_DRIVER="postgres" \
  -e "DATABASE_DSN=postgres://mysqlagent:mysqlagent123@localhost:5432/mysqlagent_demo?sslmode=disable" \
  mysqlagent:latest
```

全部环境变量：`SERVER_ADDR` / `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` / `DB_DRIVER` / `DATABASE_DSN`（另有 `REQUEST_TIMEOUT` / `HEARTBEAT_INTERVAL` / `DB_TABLES` / `API_KEYS` / `CORS_ORIGINS` / `AUDIT_OUTPUT` / `SESSIONS_DB`）。配置文件不存在时自动回退到环境变量 + 默认值。

### 方式二：挂载配置文件

```bash
docker run -d --name mysqlagent -p 8080:8080 --restart unless-stopped \
  -v "$(pwd)/config.yaml:/etc/mysqlagent/config.yaml:ro" \
  mysqlagent:latest
```

### WSL2 + LMStudio + PostgreSQL 部署形态（本项目默认拓扑）

与 Go 版相同：容器 `--network host` 共享 WSL 网络栈，`localhost:1234` 与 `localhost:5432` 直达。

```bash
sudo bash scripts/setup_pg.sh   # 在 WSL 内以 root 运行，初始化演示数据库
```

## API

### POST /api/chat（SSE）

```json
{ "session_id": "可选，缺省自动生成", "message": "这个库有哪些表？各多少行？" }
```

curl 示例：

```bash
curl -N -X POST http://localhost:8080/api/chat \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-local-dev-001" \
  -d '{"message":"用户表里有多少条数据？"}'
```

响应（`text/event-stream`）：

```
event: meta
data: {"trace_id":"3de97c2db79c195a","session_id":"s-xxx"}

event: message
data: {"delta":"根据查询结果","step":1}

event: tool_call
data: {"call_id":"call_0","name":"list_tables","arguments":"{}","step":1}

event: tool_result
data: {"call_id":"call_0","name":"list_tables","ok":true,"result":"…","duration_ms":12,"step":1}

event: done
data: {"steps":2,"usage":{"prompt_tokens":1024,"completion_tokens":256,"total_tokens":1280},"reason":"normal"}
```

- `meta`：首个事件，携带 `trace_id`（全链路审计追踪 ID，反馈问题时可引用）
- `ping`：SSE 心跳保活（注释行形式 `: ping`，客户端无需处理）
- `done.reason`：结束原因——`normal` / `max_loops`（步数上限）/ `loop_detected`（工具重复调用熔断）/ `budget_exceeded`（token 预算熔断）/ `timeout` / `client_abort` / `model_error`
- `error.code`：错误分类码，与 done.reason 取值一致

### 其他

- `GET /api/health` — 健康检查
- `GET /api/tools` — 已注册工具列表
- `GET /metrics` — Prometheus 指标（免认证）：请求量 / 会话结果分布 / token 用量 / LLM 重试 / 工具调用与耗时 / 护栏命中 / 失败分类 / 线程数
- `GET /api/sessions/{id}/messages` — 会话消息回放（需认证；需开启 `store.path` 持久化）
- `GET /api/sessions/{id}/audit?limit=200` — 会话关联审计事件检索（需认证；倒序）

## 项目结构

```
main.py                        根入口 shim（python main.py / python -m mysqlagent 均可）
config.yaml.example            配置样例
requirements.txt               运行时依赖
pyproject.toml                 包元数据 / pytest 配置 / 可选依赖
Makefile                       安装 / 测试 / 运行 / 扫描常用命令
scripts/security-scan.sh       Trivy 镜像扫描 + CycloneDX SBOM + 源码扫描
helm/mysqlagent/               Helm Chart（探针 / 资源 / ServiceMonitor）
mysqlagent/
  main.py                      入口实现：装配全部组件并启动 uvicorn
  config.py                    配置加载（YAML + 环境变量覆盖 + 默认值 + 校验）
  llm.py                       OpenAI 兼容流式客户端（SSE 解析 + 首 token 前重试）
  agent.py                     Agent Loop 编排 + 循环/预算熔断 + step 级审计
  prompt.py                    System Prompt
  tools/
    registry.py                工具注册表
    dbtools.py                 数据库三件套（MySQL/PG 方言分支）
  db/
    sql.py                     异步连接池（psycopg / aiomysql 双驱动）+ 三级截断查询
    guard.py                   只读校验器（方言感知）
    acl.py                     表级 ACL + SQL 表名提取
  guardrails/
    guardrails.py              提示注入检测 + 工具结果隔离
    pii.py                     PII 掩码
  audit.py                     异步 JSONL 审计落盘（队列 + flush，只落哈希）
  metrics.py                   手写 Prometheus 文本格式指标
  store.py                     SQLite 会话持久化 + 审计同库（aiosqlite，WAL）
  server.py                    FastAPI 路由、认证/限流/CORS/ACL、trace_id、心跳、回放、静态页
  static/index.html            聊天页（与 Go 版同一份）
tests/                         pytest 测试（`make test` 查看当前用例数）
```

## 测试

```bash
make test                      # 或 python -m pytest tests/ -q
```

覆盖范围（与 Go 版测试目标一一对应）：

| 测试文件 | 覆盖 |
|------|------|
| `test_guard.py` | 只读校验全量用例（两种方言 × 正常/拦截/误伤边界） |
| `test_acl.py` | SQL 表名提取（子查询/JOIN/CTE/表函数/字符串掩码）+ ACL 交集 + contextvars 往返 |
| `test_guardrails.py` | 注入攻击样本分级 + 正常问题反例 + 工具结果隔离 |
| `test_pii.py` | 邮箱/手机/身份证/银行卡掩码 + 组合管线 |
| `test_config.py` | YAML / 环境变量 / 默认值 / 必填校验 / 超时收敛校验 |
| `test_llm.py` | SSE 解析、tool_calls 增量装配、429/5xx/4xx/连接错误重试策略（MockTransport） |
| `test_agent.py` | 正常流 / max_loops / loop_detected / budget / model_error / step 超时 / 出站管线 / 审计只落哈希 |
| `test_store.py` | 消息存取往返、审计同库与回放 |
| `test_server.py` | SSE 事件序列与帧格式、会话连续性、认证、限流、并发隔舱、护栏、回放、指标（自建 ASGI 线束，真实流式） |

## 安全说明

0. **本版加固要点**（详见各条）：
   - **表级 ACL 失败即拒绝**：开启 ACL（`database.tables` / `api_keys[].tables`）后，`execute_sql` 用 sqlglot AST ＋ 正则双路径提取表名并取并集；解析失败、裸 `TABLE` 语句、`SHOW`、非默认 schema 的限定名（如 `other.users`、`pg_catalog.*`）一律拒绝。只读校验另拦 `query_to_xml` / `dblink*` / `pg_read_*` / `nextval` / `GET_LOCK` 等可绕过表检查或产生副作用的函数。
   - **数据库侧只读**：PostgreSQL 连接带 `default_transaction_read_only=on` 与 `statement_timeout`；MySQL 连接 `SET SESSION transaction_read_only=1, max_execution_time=…`（需 MySQL 5.7.8+，MariaDB 请自行调整 `init_command`）。查询只取回 `max_rows+1` 行，不再 `fetchall`。
   - **会话归属**：会话绑定创建者（API Key 哈希），他人续接或回放返回 `403`；同一会话上一条消息未结束时再发返回 `409`；会话 ID 限 `[A-Za-z0-9_.-]{1,64}`，自动生成的是随机串；内存会话有上限（1000）与空闲 TTL（24h）。开启认证后，旧版本遗留的无主会话（`owner` 为空）不再对任何人开放。
   - **审计不落明文**：`user` 字段为身份名而非 API Key；护栏命中只落片段的 SHA-256 前缀与长度。
   - **请求体上限**：超过 `max_input_chars*4+4096`（至少 16KB）返回 `413`。
   - **PII**：手机号 / 身份证 / 银行卡不再被紧邻的汉字绕过；身份证需出生日期合法、银行卡需通过 Luhn 校验，避免把 18/19 位业务主键误掩码；注入隔离与 PII 掩码两个开关各自独立生效。
   - **配置**：`heartbeat_interval` / `budget_tokens` / `max_retries` 显式写 `0` 表示关闭 / 不限 / 不重试，不再被默认值覆盖。

1. `execute_sql` 的只读校验是**应用层防护**，不是权限边界。生产环境请使用数据库只读账号（PostgreSQL：仅 `SELECT` + `CONNECT` 权限；MySQL：仅 `SELECT` + `SHOW DATABASES`），实现纵深防御。
2. 工具结果有行数（100）、单元格（512 字符）、总字节（24KB）三级截断，防止撑爆 LLM 上下文。
3. 工具结果进入 LLM 上下文前经过出站防护管线：间接注入隔离（疑似指令文本被替换为 `[ISOLATED-INSTRUCTION]` 占位）→ PII 掩码（邮箱/手机/身份证/银行卡保留首尾字符）。
4. 审计只落哈希与摘要（`args_sha256` / `sql_sha256`），不落用户输入、SQL 原文与数据内容；JSONL 追加写（0600 权限），异步落盘不阻塞主链路。
5. Agent 失控三重熔断：步数上限（`max_loops`）、同一(工具,参数)重复调用熔断（`max_tool_repeat`）、单请求累计 token 预算（`budget_tokens`）。
6. 会话默认在内存中，重启即清空；配置 `store.path` 后由 SQLite 持久化（WAL 模式）。
7. 已知取舍：`REPLACE()` 字符串函数会被黑名单误拦（与 `REPLACE INTO` 写操作无法按词区分），LLM 会自动改用 `CONCAT`/`SUBSTRING` 等替代写法。

## 与 Go 版的差异

功能与协议完全对齐（同一份 config.yaml、同一套 SSE 事件、同一套 API 与错误文案），以下为实现层面的差异：

| 维度 | Go 版 | Python 版 |
|------|-------|-----------|
| HTTP / SSE | `net/http` + 手写 SSE | FastAPI + StreamingResponse（事件队列 + 心跳任务） |
| 并发模型 | goroutine + channel + context | asyncio 任务 + `asyncio.Queue` + `asyncio.timeout` + contextvars |
| 限流/隔舱 | 令牌桶结构体 + `select-default` | 令牌桶 + `Semaphore.locked()` 非阻塞获取 |
| PG 驱动 | pgx v5 | psycopg3 + psycopg_pool（异步池，autocommit） |
| MySQL 驱动 | go-sql-driver | aiomysql（DSN 解析为连接参数，`?charset/timeout` 透传） |
| SQLite | modernc.org/sqlite（纯 Go） | aiosqlite（标准库 sqlite3 异步封装） |
| 指标运行时曲线 | `mysqlagent_goroutines` | `mysqlagent_threads`（活跃线程数） |
| 步数超时归类 | 单步 LLM 超时经 ctx 归类为 `client_abort`（Go 版行为） | 单步 LLM 超时归类为 `timeout`（更符合语义，属刻意修正） |
| 优雅退出 | `signal.Notify` + `http.Server.Shutdown` | uvicorn `should_exit` + 审计 flush + 连接池关闭 |

## 许可

与 Go 版保持一致（个人项目，暂未声明开源许可）。
