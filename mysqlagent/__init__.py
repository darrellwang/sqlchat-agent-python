"""mysqlagent 包：对话式数据库分析 AI Agent 的 Python 3 实现。

与 Go 版（D:/Work/mysqlagent）功能对齐：
  - OpenAI 兼容 LLM 流式客户端（SSE 解析 + 首 token 前重试）
  - Agent Loop 编排 + 循环/预算熔断 + step 级审计
  - 工具注册表 + 数据库三件套（MySQL/PG 方言分支）
  - 连接池 + 只读校验器 + 表级 ACL
  - 提示注入护栏 + 工具结果隔离 + PII 掩码
  - 异步审计落盘（只落哈希）+ Prometheus 文本格式指标
  - SQLite 会话持久化 + 审计同库
  - HTTP/SSE 路由、认证/限流/CORS/ACL、trace_id、心跳、回放
"""

__version__ = "0.3.0"
