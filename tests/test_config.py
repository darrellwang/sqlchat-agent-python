"""配置加载测试（YAML + 环境变量覆盖 + 默认值 + 校验）。"""

import os

import pytest

from mysqlagent.config import ConfigError, load


def _write_cfg(tmp_path, content: str) -> str:
    p = tmp_path / "config.yaml"
    p.write_text(content, encoding="utf-8")
    return str(p)


BASE_YAML = """
server:
  addr: ":9090"
  api_keys:
    - key: "sk-a"
      name: "alice"
    - key: "sk-b"
      name: "bob"
      tables: ["orders"]
  cors_origins: ["https://app.example.com"]
  request_timeout: 60
  heartbeat_interval: 10
llm:
  base_url: "http://llm.test/v1"
  api_key: "sk-xxx"
  model: "test-model"
  call_timeout: 30
database:
  driver: "mysql"
  dsn: "u:p@tcp(127.0.0.1:3306)/db"
agent:
  max_loops: 5
"""


def test_load_yaml_full(tmp_path):
    cfg = load(_write_cfg(tmp_path, BASE_YAML))
    assert cfg.server.addr == ":9090"
    assert cfg.server.request_timeout == 60
    assert cfg.server.heartbeat_interval == 10
    assert len(cfg.server.api_keys) == 2
    assert cfg.server.api_keys[0].key == "sk-a"
    assert cfg.server.api_keys[1].tables == ["orders"]
    assert cfg.server.cors_origins == ["https://app.example.com"]
    assert cfg.llm.base_url == "http://llm.test/v1"
    assert cfg.llm.model == "test-model"
    assert cfg.database.driver == "mysql"
    assert cfg.agent.max_loops == 5


def test_defaults_applied(tmp_path):
    cfg = load(_write_cfg(tmp_path, """
llm:
  base_url: "http://llm.test/v1"
  model: "m"
database:
  dsn: "dsn"
"""))
    assert cfg.server.addr == ":8080"
    assert cfg.server.request_timeout == 120
    assert cfg.server.heartbeat_interval == 15
    assert cfg.llm.call_timeout == 90
    assert cfg.llm.max_retries == 2
    assert cfg.llm.max_tokens == 4096
    assert cfg.database.driver == "postgres"
    assert cfg.database.max_open_conns == 5
    assert cfg.database.query_limit == 100
    assert cfg.rate_limit.per_key_qps == 2
    assert cfg.rate_limit.per_key_burst == 5
    assert cfg.rate_limit.max_concurrent == 8
    assert cfg.guardrails.input_enabled is True
    assert cfg.guardrails.tool_isolation_enabled is True
    assert cfg.guardrails.pii_masking_enabled is True
    assert cfg.guardrails.max_input_chars == 4000
    assert cfg.agent.max_loops == 10
    assert cfg.agent.history_limit == 40
    assert cfg.agent.result_max_bytes == 24576
    assert cfg.agent.budget_tokens == 100000
    assert cfg.agent.max_tool_repeat == 3


def test_guardrails_explicit_false(tmp_path):
    cfg = load(_write_cfg(tmp_path, """
llm: {base_url: "u", model: "m"}
database: {dsn: "d"}
guardrails:
  input: {enabled: false}
  tool_isolation: {enabled: false}
  pii_masking: {enabled: false}
"""))
    assert cfg.guardrails.input_enabled is False
    assert cfg.guardrails.tool_isolation_enabled is False
    assert cfg.guardrails.pii_masking_enabled is False


def test_missing_config_file_falls_back_to_env(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_BASE_URL", "http://env.test/v1")
    monkeypatch.setenv("LLM_MODEL", "env-model")
    monkeypatch.setenv("DATABASE_DSN", "env-dsn")
    monkeypatch.setenv("DB_DRIVER", "mysql")
    monkeypatch.setenv("SERVER_ADDR", ":7777")
    monkeypatch.setenv("REQUEST_TIMEOUT", "120")
    monkeypatch.setenv("DB_TABLES", "users,orders")
    monkeypatch.setenv("API_KEYS", "k1:alice,k2:bob:orders+items")
    monkeypatch.setenv("CORS_ORIGINS", "https://a.example,https://b.example")
    monkeypatch.setenv("AUDIT_OUTPUT", "/tmp/audit.jsonl")
    monkeypatch.setenv("SESSIONS_DB", "/tmp/sessions.db")
    monkeypatch.setenv("HEARTBEAT_INTERVAL", "5")

    cfg = load(str(tmp_path / "not-exist.yaml"))
    assert cfg.llm.base_url == "http://env.test/v1"
    assert cfg.llm.model == "env-model"
    assert cfg.database.dsn == "env-dsn"
    assert cfg.database.driver == "mysql"
    assert cfg.server.addr == ":7777"
    assert cfg.server.request_timeout == 120
    assert cfg.database.tables == ["users", "orders"]
    assert cfg.server.cors_origins == ["https://a.example", "https://b.example"]
    assert cfg.audit.output == "/tmp/audit.jsonl"
    assert cfg.store.path == "/tmp/sessions.db"
    assert cfg.server.heartbeat_interval == 5
    # API_KEYS: k1:alice / k2:bob:orders+items
    assert len(cfg.server.api_keys) == 2
    assert cfg.server.api_keys[0].key == "k1"
    assert cfg.server.api_keys[0].name == "alice"
    assert cfg.server.api_keys[0].tables == []
    assert cfg.server.api_keys[1].name == "bob"
    assert cfg.server.api_keys[1].tables == ["orders", "items"]


def test_api_keys_env_name_defaults_to_key_prefix(monkeypatch):
    monkeypatch.setenv("LLM_BASE_URL", "u")
    monkeypatch.setenv("LLM_MODEL", "m")
    monkeypatch.setenv("DATABASE_DSN", "d")
    monkeypatch.setenv("API_KEYS", "sk-local-dev-001")
    cfg = load("")
    assert cfg.server.api_keys[0].name == "sk-local"  # key 前 8 位


def test_required_fields(tmp_path):
    with pytest.raises(ConfigError, match="llm.base_url"):
        load(_write_cfg(tmp_path, "database: {dsn: d}"))
    with pytest.raises(ConfigError, match="llm.model"):
        load(_write_cfg(tmp_path, "llm: {base_url: u}\ndatabase: {dsn: d}"))
    with pytest.raises(ConfigError, match="database.dsn"):
        load(_write_cfg(tmp_path, "llm: {base_url: u, model: m}"))


def test_timeout_convergence_validation(tmp_path):
    with pytest.raises(ConfigError, match="必须小于"):
        load(_write_cfg(tmp_path, """
llm: {base_url: u, model: m, call_timeout: 120}
database: {dsn: d}
server: {request_timeout: 120}
"""))


MIN_YAML = """
llm:
  base_url: "http://llm.test/v1"
  model: "m"
database:
  dsn: "dsn"
"""


def test_explicit_zero_means_disabled(tmp_path):
    """0 = 关闭 / 不限，不能被默认值顶掉（心跳 / token 预算 / LLM 重试）。"""
    cfg = load(_write_cfg(tmp_path, MIN_YAML + """
server:
  heartbeat_interval: 0
agent:
  budget_tokens: 0
"""))
    assert cfg.server.heartbeat_interval == 0
    assert cfg.agent.budget_tokens == 0

    cfg2 = load(_write_cfg(tmp_path, MIN_YAML.replace('model: "m"', 'model: "m"\n  max_retries: 0')))
    assert cfg2.llm.max_retries == 0


def test_invalid_int_raises_config_error(tmp_path):
    with pytest.raises(ConfigError):
        load(_write_cfg(tmp_path, MIN_YAML + "server:\n  request_timeout: abc\n"))


def test_csv_env_is_stripped(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_TABLES", " users , orders ,,")
    monkeypatch.setenv("CORS_ORIGINS", "https://a.example , ,https://b.example")
    cfg = load(_write_cfg(tmp_path, MIN_YAML))
    assert cfg.database.tables == ["users", "orders"]
    assert cfg.server.cors_origins == ["https://a.example", "https://b.example"]
