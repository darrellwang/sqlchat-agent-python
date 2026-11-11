"""mysqlagent.tools.registry — 工具注册表。

移植自 Go 版 internal/tools/registry.go。
Registry 管理全部工具并生成 OpenAI tools 定义；工具自身的业务错误
（如 SQL 非法）以 "TOOL_ERROR: ..." 前缀的字符串返回（不中断 Agent 循环，
让 LLM 看到错误后自行修正）；系统级错误才返回 error。
"""

from __future__ import annotations

import json
from typing import Any, Protocol

from mysqlagent.llm import Tool as LLMTool


class Tool(Protocol):
    """Agent 可调用的工具抽象。"""

    def name(self) -> str: ...

    def description(self) -> str: ...

    def parameters(self) -> dict[str, Any]:
        """返回 JSON Schema（OpenAI function calling 格式）。"""
        ...

    async def execute(self, args_json: str) -> str:
        """执行工具，args_json 是模型生成的 JSON 参数字符串，返回给 LLM 的文本结果。"""
        ...


class Registry:
    """管理全部工具并生成 OpenAI tools 定义。"""

    def __init__(self, tools: list = ()) -> None:
        self._order: list[str] = []
        self._tools: dict[str, Any] = {}
        for t in tools:
            self.register(t)

    def register(self, t: Any) -> None:
        """注册单个工具（重复名覆盖，顺序保持首次注册位置）。"""
        if t.name() not in self._tools:
            self._order.append(t.name())
        self._tools[t.name()] = t

    def defs(self) -> list[LLMTool]:
        """生成传给 LLM 的 tools 定义（顺序稳定）。"""
        return [
            LLMTool(
                type="function",
                name=self._tools[name].name(),
                description=self._tools[name].description(),
                parameters=self._tools[name].parameters(),
            )
            for name in self._order
        ]

    def names(self) -> list[str]:
        """返回全部工具名（排序）。"""
        return sorted(self._order)

    async def call(self, name: str, args_json: str) -> str:
        """按名称执行工具。

        业务错误以 "TOOL_ERROR: ..." 字符串返回（不中断 Agent 循环）；
        未知工具与执行异常同样包装为 TOOL_ERROR 交还 LLM。
        """
        t = self._tools.get(name)
        if t is None:
            return "TOOL_ERROR: 未知工具 " + name
        try:
            return await t.execute(args_json)
        except Exception as e:
            return f"TOOL_ERROR: {e}"


def must_json(v: Any) -> str:
    """把结果序列化为字符串（失败属于程序 bug，同样以 TOOL_ERROR 交还 LLM）。"""
    try:
        return json.dumps(v, ensure_ascii=False)
    except (TypeError, ValueError) as e:
        return f"TOOL_ERROR: 序列化结果失败: {e}"
