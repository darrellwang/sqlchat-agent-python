"""mysqlagent 入口：装配全部组件并启动 uvicorn。

对应 Go 版 main.go：
  配置加载 → 数据库连接池 → 工具注册 → 审计日志 → 会话持久化 →
  LLM 客户端 + Agent → HTTP 服务（uvicorn）→ 优雅退出（SIGINT/SIGTERM）。
"""

from __future__ import annotations

import argparse
import asyncio
import sys

import uvicorn

from mysqlagent import __version__
from mysqlagent.agent import Agent, Options
from mysqlagent.audit import AuditLogger
from mysqlagent.config import ConfigError, load
from mysqlagent.db.sql import DB, DBError
from mysqlagent.llm import LLMClient
from mysqlagent.server import Server
from mysqlagent.store import Store
from mysqlagent.tools.dbtools import DescribeTable, ExecuteSQL, ListTables
from mysqlagent.tools.registry import Registry


def display_addr(addr: str) -> str:
    """启动日志中展示的访问地址（host 为空 / 0.0.0.0 时显示 localhost）。"""
    host, port = parse_addr(addr)
    if host in ("", "0.0.0.0", "::"):
        host = "localhost"
    return f"{host}:{port}"


def build_registry(database, cfg) -> Registry:
    """装配数据库三件套工具（独立成函数，便于测试装配层）。"""
    return Registry([
        ListTables(database),
        DescribeTable(database),
        ExecuteSQL(
            database,
            query_limit=cfg.database.query_limit,
            max_bytes=cfg.agent.result_max_bytes,
        ),
    ])


def parse_addr(addr: str) -> tuple[str, int]:
    """把 Go 风格监听地址（":8080" / "0.0.0.0:8080" / "localhost:8080"）
    解析为 (host, port)。"""
    addr = addr.strip()
    if ":" in addr:
        host, _, port = addr.rpartition(":")
        return (host or "0.0.0.0"), int(port)
    if addr.isdigit():
        return "0.0.0.0", int(addr)
    return "0.0.0.0", 8080


async def serve(cfg_path: str) -> None:
    cfg = load(cfg_path)

    # 数据库连接池与只读防护（MySQL / PostgreSQL）
    database = DB(
        cfg.database.driver,
        cfg.database.dsn,
        cfg.database.max_open_conns,
        cfg.database.max_idle_conns,
        cfg.database.query_timeout,
    )
    await database.connect()

    # 工具注册
    registry = build_registry(database, cfg)

    # 审计日志（异步 JSONL 落盘；优雅退出时 flush）
    audit_logger = await AuditLogger(cfg.audit.output).start()

    # 会话持久化（SQLite：会话消息 + 审计事件同库；未配置 = 纯内存模式）
    st: Store | None = None
    if cfg.store.path != "":
        st = await Store(cfg.store.path).open()
        audit_logger.add_sink(st.log_audit)  # 审计事件同库一份，便于按会话检索
        print(f"持久化: SQLite → {cfg.store.path}")

    # LLM 客户端（首 token 前重试：429/5xx/连接错误，指数退避+抖动）
    # 与 Agent（单步 LLM 超时 < 请求级超时，逐层收敛）
    client = LLMClient(
        cfg.llm.base_url,
        cfg.llm.api_key,
        cfg.llm.model,
        cfg.llm.temperature,
        cfg.llm.max_tokens,
        max_retries=cfg.llm.max_retries,
        retry_delay=0.5,
    )
    ag = Agent(client, registry, Options(
        max_loops=cfg.agent.max_loops,
        call_timeout=float(cfg.llm.call_timeout),
        tool_isolation=bool(cfg.guardrails.tool_isolation_enabled),
        pii_masking=bool(cfg.guardrails.pii_masking_enabled),
        budget_tokens=cfg.agent.budget_tokens,
        max_tool_repeat=cfg.agent.max_tool_repeat,
    ))

    # HTTP 服务（认证 / 限流 / CORS / 输入护栏 / 超时 / 审计 / 指标 / 持久化在 server 内装配）
    server = Server(ag, cfg, audit_logger, st)

    print(f"mysqlagent {__version__} 已启动: http://{display_addr(cfg.server.addr)}")
    print(f"模型: {cfg.llm.model} @ {cfg.llm.base_url}")
    print(f"数据库: {cfg.database.driver} | 工具: {registry.names()}")
    print(
        f"安全: 认证={len(cfg.server.api_keys) > 0} "
        f"限流={cfg.rate_limit.per_key_qps}qps/并发{cfg.rate_limit.max_concurrent} "
        f"输入护栏={cfg.guardrails.input_enabled} "
        f"工具隔离={cfg.guardrails.tool_isolation_enabled} "
        f"PII掩码={cfg.guardrails.pii_masking_enabled} "
        f"超时={cfg.llm.call_timeout}s/{cfg.server.request_timeout}s "
        f"重试={cfg.llm.max_retries} 预算={cfg.agent.budget_tokens}tokens"
    )
    if cfg.audit.output != "":
        print(f"审计: JSONL → {cfg.audit.output}")
    else:
        print("审计: stdout（生产环境建议配置 audit.output 落盘）")

    host, port = parse_addr(cfg.server.addr)
    config = uvicorn.Config(
        server.app,
        host=host,
        port=port,
        log_level="info",
        access_log=True,
        timeout_graceful_shutdown=10,
    )
    uv_server = uvicorn.Server(config)

    # 优雅退出：SIGINT/SIGTERM 由 uvicorn 自带信号处理捕获（should_exit），
    # serve() 返回后进入 finally：flush 审计、关闭 SQLite 与数据库连接池。
    try:
        await uv_server.serve()
    finally:
        print("正在关闭…")
        await audit_logger.close()
        if st is not None:
            await st.close()
        await database.close()
        await client.aclose()


def main() -> None:
    parser = argparse.ArgumentParser(description="mysqlagent — 对话式数据库分析 AI Agent")
    parser.add_argument("-config", "--config", default="config.yaml", help="配置文件路径")
    parser.add_argument("-version", "--version", action="version", version=f"mysqlagent {__version__}")
    args = parser.parse_args()

    try:
        asyncio.run(serve(args.config))
    except ConfigError as e:
        print(f"加载配置失败: {e}", file=sys.stderr)
        sys.exit(1)
    except DBError as e:
        print(f"初始化数据库失败: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
