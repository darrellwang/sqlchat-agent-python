"""mysqlagent.config — 配置加载（YAML + 环境变量覆盖 + 默认值 + 校验）。

移植自 Go 版 internal/config/config.go。配置文件不存在时不报错
（容器纯环境变量部署模式），直接用环境变量 + 默认值。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

import yaml


@dataclass
class APIKey:
    """一个接入凭证：key 绑定身份名，用于认证、限流、审计归属与表级 ACL。"""

    key: str = ""
    name: str = ""
    tables: list[str] = field(default_factory=list)  # 该身份允许访问的表（空 = 继承全局清单）


@dataclass
class ServerConfig:
    addr: str = ""               # 监听地址，如 ":8080"
    api_keys: list[APIKey] = field(default_factory=list)  # 接入凭证；为空 = 认证关闭（仅限本地开发）
    cors_origins: list[str] = field(default_factory=list)  # 跨域白名单；为空 = 不输出 CORS 头（同源）
    request_timeout: int = 0     # Agent 请求整体超时（秒）
    heartbeat_interval: int = -1  # SSE 心跳间隔（秒，0 = 关闭；-1 = 未设置，取默认 15）


@dataclass
class LLMConfig:
    base_url: str = ""      # OpenAI 兼容 API 地址，如 https://api.deepseek.com/v1
    api_key: str = ""       # API Key
    model: str = ""         # 模型名，如 deepseek-chat
    temperature: float = 0.0
    max_tokens: int = 0
    call_timeout: int = 0   # 单次 LLM 调用超时（秒），应小于 request_timeout
    max_retries: int = -1   # 首 token 前重试次数（0 = 不重试；-1 = 未设置，取默认 2）


@dataclass
class DatabaseConfig:
    driver: str = ""        # mysql | postgres（默认 postgres）
    dsn: str = ""           # 数据库 DSN（Go 版同格式，pgx URL / mysql tcp DSN）
    max_open_conns: int = 0
    max_idle_conns: int = 0
    query_limit: int = 0    # 无 LIMIT 的 SELECT 自动追加上限
    query_timeout: int = 0  # 单条查询超时（秒）
    tables: list[str] = field(default_factory=list)  # 表级 ACL 全局清单（空 = 不限制）


@dataclass
class RateLimitConfig:
    per_key_qps: float = 0.0    # 每个身份每秒允许的请求数
    per_key_burst: int = 0      # 每个身份的突发容量
    max_concurrent: int = 0     # 全局并发 Agent 会话上限


@dataclass
class GuardrailsConfig:
    input_enabled: bool | None = None       # 提示注入输入护栏（默认开启；显式 false 关闭）
    max_input_chars: int = 0                # 用户输入长度上限
    tool_isolation_enabled: bool | None = None  # 工具结果注入清洗与隔离（默认开启）
    pii_masking_enabled: bool | None = None     # 工具结果 PII 掩码（默认开启）


@dataclass
class AgentConfig:
    max_loops: int = 0        # Agent 循环步数上限
    history_limit: int = 0    # 会话保留的最大消息条数
    result_max_bytes: int = 0  # 工具结果返回给 LLM 的最大字节数
    budget_tokens: int = -1   # 单请求累计 token 预算（0 = 不限；-1 = 未设置，取默认 100000）
    max_tool_repeat: int = 0  # 同一(工具,参数)重复调用熔断阈值


@dataclass
class AuditConfig:
    output: str = ""  # 审计 JSONL 文件路径；为空 = 输出到 stdout 日志


@dataclass
class StoreConfig:
    path: str = ""  # SQLite 持久化路径（会话+审计同库）；为空 = 纯内存模式


@dataclass
class Config:
    """mysqlagent 的全局配置，对应 config.yaml。"""

    server: ServerConfig = field(default_factory=ServerConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    rate_limit: RateLimitConfig = field(default_factory=RateLimitConfig)
    guardrails: GuardrailsConfig = field(default_factory=GuardrailsConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)
    audit: AuditConfig = field(default_factory=AuditConfig)
    store: StoreConfig = field(default_factory=StoreConfig)


class ConfigError(Exception):
    """配置缺失或非法。"""


def _apply_defaults(c: Config) -> None:
    if c.server.addr == "":
        c.server.addr = ":8080"
    if c.server.request_timeout == 0:
        c.server.request_timeout = 120
    if c.server.heartbeat_interval < 0:
        c.server.heartbeat_interval = 15
    if c.llm.max_tokens == 0:
        c.llm.max_tokens = 4096
    if c.llm.call_timeout == 0:
        c.llm.call_timeout = 90
    if c.llm.max_retries < 0:
        c.llm.max_retries = 2
    if c.database.driver == "":
        c.database.driver = "postgres"
    if c.database.max_open_conns == 0:
        c.database.max_open_conns = 5
    if c.database.max_idle_conns == 0:
        c.database.max_idle_conns = 2
    if c.database.query_limit == 0:
        c.database.query_limit = 100
    if c.database.query_timeout == 0:
        c.database.query_timeout = 15
    if c.rate_limit.per_key_qps == 0:
        c.rate_limit.per_key_qps = 2
    if c.rate_limit.per_key_burst == 0:
        c.rate_limit.per_key_burst = 5
    if c.rate_limit.max_concurrent == 0:
        c.rate_limit.max_concurrent = 8
    if c.guardrails.max_input_chars == 0:
        c.guardrails.max_input_chars = 4000
    if c.guardrails.input_enabled is None:
        c.guardrails.input_enabled = True
    if c.guardrails.tool_isolation_enabled is None:
        c.guardrails.tool_isolation_enabled = True
    if c.guardrails.pii_masking_enabled is None:
        c.guardrails.pii_masking_enabled = True
    if c.agent.max_loops == 0:
        c.agent.max_loops = 10
    if c.agent.history_limit == 0:
        c.agent.history_limit = 40
    if c.agent.result_max_bytes == 0:
        c.agent.result_max_bytes = 24 * 1024
    if c.agent.budget_tokens < 0:
        c.agent.budget_tokens = 100000
    if c.agent.max_tool_repeat == 0:
        c.agent.max_tool_repeat = 3


def _parse_api_keys_env(v: str) -> list[APIKey]:
    """解析 API_KEYS 环境变量。

    单条格式："key" / "key:name" / "key:name:table1+table2"
    （名字缺省取 key 前 8 位；表清单用 + 连接，空缺 = 继承全局 database.tables）。
    """
    keys: list[APIKey] = []
    for part in v.split(","):
        part = part.strip()
        if part == "":
            continue
        k = APIKey(key=part)
        segs = part.split(":", 2)
        if len(segs) >= 2:
            k.key = segs[0].strip()
            k.name = segs[1].strip()
            if len(segs) == 3 and segs[2].strip() != "":
                k.tables = [t.strip() for t in segs[2].split("+") if t.strip() != ""]
        if k.name == "":
            k.name = k.key[:8] if len(k.key) > 8 else k.key
        keys.append(k)
    return keys


def _env_int(name: str) -> int | None:
    v = os.environ.get(name)
    if not v:
        return None
    try:
        return int(v)
    except ValueError:
        return None


def load(path: str = "config.yaml") -> Config:
    """从 path 读取 YAML 配置，应用默认值，并允许环境变量覆盖。"""
    cfg = Config()
    if path:
        try:
            with open(path, "rb") as f:
                raw = yaml.safe_load(f)
        except FileNotFoundError:
            raw = None
        if raw is not None:
            if not isinstance(raw, dict):
                raise ConfigError(f"解析配置文件 {path}: 顶层必须是映射")
            try:
                cfg = _from_dict(raw)
            except (ValueError, TypeError) as e:
                raise ConfigError(f"配置文件 {path} 中存在非法取值: {e}") from e

    _apply_defaults(cfg)

    # 环境变量覆盖（便于容器化部署时注入密钥）
    if v := os.environ.get("SERVER_ADDR"):
        cfg.server.addr = v
    if (n := _env_int("REQUEST_TIMEOUT")) is not None:
        cfg.server.request_timeout = n
    if (n := _env_int("HEARTBEAT_INTERVAL")) is not None:
        cfg.server.heartbeat_interval = n
    if v := os.environ.get("LLM_API_KEY"):
        cfg.llm.api_key = v
    if v := os.environ.get("LLM_BASE_URL"):
        cfg.llm.base_url = v
    if v := os.environ.get("LLM_MODEL"):
        cfg.llm.model = v
    if v := os.environ.get("DATABASE_DSN"):
        cfg.database.dsn = v
    if v := os.environ.get("DB_DRIVER"):
        cfg.database.driver = v
    # DB_TABLES="users,orders"（表级 ACL 全局清单；空 = 不限制）
    if v := os.environ.get("DB_TABLES"):
        cfg.database.tables = _split_csv(v)
    # API_KEYS="key1:alice,key2:bob" 或 "key1,key2"（名字缺省取 key 前 8 位）
    if v := os.environ.get("API_KEYS"):
        cfg.server.api_keys = _parse_api_keys_env(v)
    # CORS_ORIGINS="https://a.example,https://b.example"
    if v := os.environ.get("CORS_ORIGINS"):
        cfg.server.cors_origins = _split_csv(v)
    # AUDIT_OUTPUT="/var/log/mysqlagent/audit.jsonl"（为空 = stdout）
    if v := os.environ.get("AUDIT_OUTPUT"):
        cfg.audit.output = v
    # SESSIONS_DB="/var/lib/mysqlagent/sessions.db"（为空 = 纯内存模式）
    if v := os.environ.get("SESSIONS_DB"):
        cfg.store.path = v

    if cfg.llm.base_url == "":
        raise ConfigError("配置项 llm.base_url 不能为空")
    if cfg.llm.model == "":
        raise ConfigError("配置项 llm.model 不能为空")
    if cfg.database.dsn == "":
        raise ConfigError("配置项 database.dsn 不能为空")
    if cfg.llm.call_timeout >= cfg.server.request_timeout:
        raise ConfigError(
            f"llm.call_timeout ({cfg.llm.call_timeout}s) 必须小于 server.request_timeout "
            f"({cfg.server.request_timeout}s)，超时应逐层收敛"
        )
    return cfg


def _from_dict(raw: dict) -> Config:
    """把 YAML 映射装配为 Config（容忍缺省段落）。"""

    def sec(name: str) -> dict:
        v = raw.get(name)
        return v if isinstance(v, dict) else {}

    c = Config()
    s = sec("server")
    c.server.addr = str(s.get("addr", "") or "")
    c.server.cors_origins = [str(x) for x in (s.get("cors_origins") or [])]
    c.server.request_timeout = int(s.get("request_timeout", 0) or 0)
    c.server.heartbeat_interval = _opt_int(s, "heartbeat_interval")
    for k in s.get("api_keys") or []:
        if not isinstance(k, dict):
            continue
        c.server.api_keys.append(APIKey(
            key=str(k.get("key", "") or ""),
            name=str(k.get("name", "") or ""),
            tables=[str(t) for t in (k.get("tables") or [])],
        ))

    l = sec("llm")
    c.llm.base_url = str(l.get("base_url", "") or "")
    c.llm.api_key = str(l.get("api_key", "") or "")
    c.llm.model = str(l.get("model", "") or "")
    c.llm.temperature = float(l.get("temperature", 0.0) or 0.0)
    c.llm.max_tokens = int(l.get("max_tokens", 0) or 0)
    c.llm.call_timeout = int(l.get("call_timeout", 0) or 0)
    c.llm.max_retries = _opt_int(l, "max_retries")

    d = sec("database")
    c.database.driver = str(d.get("driver", "") or "")
    c.database.dsn = str(d.get("dsn", "") or "")
    c.database.max_open_conns = int(d.get("max_open_conns", 0) or 0)
    c.database.max_idle_conns = int(d.get("max_idle_conns", 0) or 0)
    c.database.query_limit = int(d.get("query_limit", 0) or 0)
    c.database.query_timeout = int(d.get("query_timeout", 0) or 0)
    c.database.tables = [str(t) for t in (d.get("tables") or [])]

    r = sec("rate_limit")
    c.rate_limit.per_key_qps = float(r.get("per_key_qps", 0.0) or 0.0)
    c.rate_limit.per_key_burst = int(r.get("per_key_burst", 0) or 0)
    c.rate_limit.max_concurrent = int(r.get("max_concurrent", 0) or 0)

    g = sec("guardrails")
    gi = g.get("input") if isinstance(g.get("input"), dict) else {}
    gt = g.get("tool_isolation") if isinstance(g.get("tool_isolation"), dict) else {}
    gp = g.get("pii_masking") if isinstance(g.get("pii_masking"), dict) else {}
    c.guardrails.input_enabled = _opt_bool(gi.get("enabled"))
    c.guardrails.max_input_chars = int(gi.get("max_input_chars", 0) or 0)
    c.guardrails.tool_isolation_enabled = _opt_bool(gt.get("enabled"))
    c.guardrails.pii_masking_enabled = _opt_bool(gp.get("enabled"))

    a = sec("agent")
    c.agent.max_loops = int(a.get("max_loops", 0) or 0)
    c.agent.history_limit = int(a.get("history_limit", 0) or 0)
    c.agent.result_max_bytes = int(a.get("result_max_bytes", 0) or 0)
    c.agent.budget_tokens = _opt_int(a, "budget_tokens")
    c.agent.max_tool_repeat = int(a.get("max_tool_repeat", 0) or 0)

    au = sec("audit")
    c.audit.output = str(au.get("output", "") or "")

    st = sec("store")
    c.store.path = str(st.get("path", "") or "")
    return c


def _opt_int(d: dict, key: str) -> int:
    """读取可显式写 0 的整数项：缺省 / null 返回 -1（= 未设置，交给默认值）。"""
    v = d.get(key)
    return -1 if v is None else int(v)


def _split_csv(v: str) -> list[str]:
    """逗号分隔列表：去空白、去空项。"""
    return [x.strip() for x in v.split(",") if x.strip()]


def _opt_bool(v: object) -> bool | None:
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)
