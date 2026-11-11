"""pytest 公共 fixtures：假 LLM 客户端、假工具、配置装配。"""

from __future__ import annotations

import json
from typing import Any, Optional

import pytest

from mysqlagent.llm import Message, ToolCall, FunctionCall, Usage


class FakeLLM:
    """按脚本应答的假 LLM 客户端（duck-typed，供 Agent / Server 测试）。

    script 元素：
      - (Message, Usage)：正常返回（有 content 时触发 on_delta）
      - Exception：抛出（模拟 LLMError / 取消）
      - callable：以 (messages, tools) 调用，返回 (Message, Usage)
    """

    def __init__(self, script: list) -> None:
        self.script = list(script)
        self.requests: list[list[dict]] = []

    async def chat_stream(self, messages, tools, on_delta=None):
        self.requests.append([m.to_dict() for m in messages])
        if not self.script:
            item: Any = (Message(role="assistant", content="（脚本已用尽）"), Usage())
        else:
            item = self.script.pop(0)
        if callable(item) and not isinstance(item, BaseException):
            item = item(messages, tools)
        if hasattr(item, "__await__"):  # 异步脚本函数
            item = await item
        if isinstance(item, BaseException):
            raise item
        msg, usage = item
        if on_delta is not None and msg.content:
            await on_delta(msg.content)
        return msg, usage

    async def aclose(self) -> None:
        pass


class FakeTool:
    """按脚本返回结果的假工具。"""

    def __init__(self, name: str, result: str = "FAKE_RESULT", raises: Optional[Exception] = None) -> None:
        self._name = name
        self._result = result
        self._raises = raises
        self.calls: list[str] = []

    def name(self) -> str:
        return self._name

    def description(self) -> str:
        return f"假工具 {self._name}"

    def parameters(self) -> dict:
        return {"type": "object", "properties": {}}

    async def execute(self, args_json: str) -> str:
        self.calls.append(args_json)
        if self._raises is not None:
            raise self._raises
        return self._result


def assistant_msg(content: str = "", tool_calls: Optional[list[ToolCall]] = None) -> Message:
    return Message(role="assistant", content=content, tool_calls=tool_calls or [])


def tool_call(call_id: str, name: str, arguments: str) -> ToolCall:
    return ToolCall(id=call_id, type="function", function=FunctionCall(name=name, arguments=arguments))


@pytest.fixture
def base_yaml(tmp_path) -> str:
    """一份最小合法配置（env 干净）。"""
    p = tmp_path / "config.yaml"
    p.write_text(
        "llm:\n"
        "  base_url: \"http://llm.test/v1\"\n"
        "  model: \"test-model\"\n"
        "database:\n"
        "  dsn: \"postgres://u:p@127.0.0.1:5432/db\"\n",
        encoding="utf-8",
    )
    return str(p)
