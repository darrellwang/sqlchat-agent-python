"""mysqlagent.metrics — 纯标准库实现的 Prometheus 文本格式指标。

移植自 Go 版 internal/metrics/metrics.go：
计数器（Counter）+ 运行时 Gauge，不引入外部依赖。
按失败分类学与在线指标的最小可用集设计。
"""

from __future__ import annotations

import threading
from typing import TextIO


class Counter:
    """带标签的计数器（标签值顺序须与定义时一致）。"""

    __slots__ = ("name", "help", "labels", "_mu", "_series")

    def __init__(self, name: str, help_: str, labels: tuple[str, ...] = ()) -> None:
        self.name = name
        self.help = help_
        self.labels = labels
        self._mu = threading.Lock()
        self._series: dict[tuple[str, ...], int] = {}

    def inc(self, *label_values: str) -> None:
        self.add(label_values, 1)

    def add(self, label_values: tuple[str, ...] | list[str], delta: int) -> None:
        if len(label_values) != len(self.labels):
            raise ValueError(
                f"{self.name}: 标签数量不符（定义 {len(self.labels)} 个，传入 {len(label_values)} 个）"
            )
        key = tuple(label_values)
        with self._mu:
            self._series[key] = self._series.get(key, 0) + delta

    def write_to(self, w: TextIO) -> None:
        with self._mu:
            snapshot = sorted(self._series.items())
        w.write(f"# HELP {self.name} {self.help}\n# TYPE {self.name} counter\n")
        for key, value in snapshot:
            if self.labels:
                label_str = "{" + ",".join(
                    f'{k}="{v}"' for k, v in zip(self.labels, key)
                ) + "}"
            else:
                label_str = ""
            w.write(f"{self.name}{label_str} {value}\n")


# 全局指标实例（进程级单例，各模块直接引用 inc）。
REQUESTS_TOTAL = Counter("mysqlagent_requests_total", "HTTP 请求总数", ("path", "code"))

# Agent 会话层
CHAT_SESSIONS = Counter("mysqlagent_chat_sessions_total", "Agent 会话结果分布", ("outcome",))
CHAT_SECONDS = Counter("mysqlagent_chat_seconds_total", "Agent 会话累计耗时（秒）")

# LLM 层
LLM_TOKENS = Counter("mysqlagent_llm_tokens_total", "LLM token 用量", ("kind",))  # prompt | completion
LLM_RETRIES = Counter("mysqlagent_llm_retries_total", "LLM 首 token 前重试次数", ("outcome",))  # retried_ok | exhausted

# 工具层
TOOL_CALLS = Counter("mysqlagent_tool_calls_total", "工具调用总数", ("tool", "ok"))
TOOL_DURATION = Counter("mysqlagent_tool_duration_ms_total", "工具调用累计耗时（毫秒）", ("tool",))

# 护栏层
GUARDRAIL_HITS = Counter(
    "mysqlagent_guardrail_hits_total", "护栏命中总数", ("layer", "rule", "action")
)  # input | tool_isolation | sql_guard

# 失败分类学：model_error | tool_error | guardrail_block | budget_exceeded | loop_detected | client_abort | timeout
FAILURES = Counter("mysqlagent_failures_total", "失败分类计数", ("kind",))

ALL_COUNTERS = (
    REQUESTS_TOTAL, CHAT_SESSIONS, CHAT_SECONDS, LLM_TOKENS, LLM_RETRIES,
    TOOL_CALLS, TOOL_DURATION, GUARDRAIL_HITS, FAILURES,
)


def write_text(w: TextIO) -> None:
    """输出全部指标（Prometheus 文本格式）。"""
    for c in ALL_COUNTERS:
        c.write_to(w)
    # 运行时 Gauge：活跃线程数（对应 Go 版的 goroutine 泄漏监控曲线）
    w.write("# HELP mysqlagent_threads 当前活跃线程数量\n# TYPE mysqlagent_threads gauge\n")
    w.write(f"mysqlagent_threads {threading.active_count()}\n")
