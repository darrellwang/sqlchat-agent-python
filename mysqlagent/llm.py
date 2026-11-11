"""mysqlagent.llm — OpenAI 兼容 Chat Completions API 的最小流式客户端。

移植自 Go 版 internal/llm/（types.go + client.go）。
适配 DeepSeek / OpenAI / LMStudio / vLLM 等一切兼容端点：
  - SSE 逐行解析，累积完整 assistant 消息（含 tool_calls 增量装配）；
  - 首 token 前重试：连接错误 / 429 / 5xx 指数退避 + 抖动；流开始后绝不重试。
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import httpx


# ---------- 数据类型（对应 OpenAI 兼容协议） ----------

@dataclass
class FunctionCall:
    name: str = ""
    arguments: str = ""

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "arguments": self.arguments}


@dataclass
class ToolCall:
    id: str = ""
    type: str = "function"
    function: FunctionCall = field(default_factory=FunctionCall)
    index: int = 0  # 流式增量拼接时的排序，不参与序列化

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "type": self.type, "function": self.function.to_dict()}


@dataclass
class Message:
    """聊天消息，覆盖 system / user / assistant / tool 四种角色。"""

    role: str
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)  # 仅 assistant 消息携带
    tool_call_id: str = ""  # 仅 tool 消息携带
    name: str = ""          # 仅 tool 消息携带（工具名）

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"role": self.role, "content": self.content}
        if self.tool_calls:
            d["tool_calls"] = [tc.to_dict() for tc in self.tool_calls]
        if self.tool_call_id:
            d["tool_call_id"] = self.tool_call_id
        if self.name:
            d["name"] = self.name
        return d


@dataclass
class Tool:
    """暴露给模型的工具定义（JSON Schema）。"""

    type: str = "function"
    name: str = ""
    description: str = ""
    parameters: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
        }


def tool_message(call_id: str, name: str, content: str) -> Message:
    """构造一条 role=tool 的结果消息。"""
    return Message(role="tool", tool_call_id=call_id, name=name, content=content)


# ---------- 客户端 ----------

class LLMError(Exception):
    """调用 LLM 失败（模型侧错误，不可重试或重试已耗尽）。"""


class LLMRetriableError(LLMError):
    """可安全重试的失败（尚未向调用方吐出任何增量）。"""


class LLMClient:
    """OpenAI 兼容 Chat Completions 流式客户端。

    base_url 形如 https://api.deepseek.com/v1，会自动拼接 /chat/completions。
    流式响应不能设置整体超时，靠调用方 ctx（asyncio.timeout）控制生命周期。
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        temperature: float = 0.0,
        max_tokens: int = 4096,
        max_retries: int = 2,
        retry_delay: float = 0.5,
        http: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.max_retries = max_retries  # 首 token 前重试次数（<=0 不重试）
        self.retry_delay = retry_delay  # 重试基础退避（指数递增 + 抖动）
        self._http = http if http is not None else httpx.AsyncClient(timeout=None)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def chat_stream(
        self,
        messages: list[Message],
        tools: list[Tool],
        on_delta: Optional[Callable[[str], Any]] = None,
    ) -> tuple[Message, Usage]:
        """发起一次流式对话请求。

        文本增量通过 on_delta 回调实时吐出（可为 async）；
        返回拼装完整的 assistant 消息（含累积好的 tool_calls）与 token 用量，
        供调用方追加到会话历史。

        首 token 前重试：请求失败（连接错误）或返回 429/5xx 时按指数退避+抖动
        重试；一旦流已经开始输出（首 token 已吐出）则绝不重试，
        避免向调用方重复推送增量。
        """
        req_body = {
            "model": self.model,
            "messages": [m.to_dict() for m in messages],
            "tools": [t.to_dict() for t in tools] if tools else [],
            "tool_choice": "auto",
            "stream": True,
            "stream_options": {"include_usage": True},
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        body = json.dumps(req_body, ensure_ascii=False).encode("utf-8")

        for attempt in range(self.max_retries + 1):
            # 退避等待（首个 attempt 不等待）。等待期间被取消则立即放弃。
            if attempt > 0:
                delay = self.retry_delay * (2 ** (attempt - 1))
                delay += random.uniform(0, delay / 2)  # ±50% 抖动
                await asyncio.sleep(delay)
            try:
                return await self._attempt(body, on_delta)
            except LLMRetriableError:
                if attempt >= self.max_retries:
                    raise

        # 理论不可达（max_retries < 0 时直接走一次 attempt）
        return await self._attempt(body, on_delta)

    async def _attempt(
        self,
        body: bytes,
        on_delta: Optional[Callable[[str], Any]],
    ) -> tuple[Message, Usage]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        if self.api_key:
            headers["Authorization"] = "Bearer " + self.api_key

        try:
            req = self._http.build_request(
                "POST", self.base_url + "/chat/completions", content=body, headers=headers
            )
            resp = await self._http.send(req, stream=True)
        except httpx.HTTPError as e:
            # 连接类错误：请求未送达或服务端未产出任何内容，可安全重试
            raise LLMRetriableError(f"请求 LLM: {e}") from e

        try:
            if resp.status_code != 200:
                msg = await _read_error_body(resp)
                err = LLMError(f"LLM 返回 {resp.status_code}: {msg}")
                # 限流与临时性服务端错误可重试；4xx 参数类错误重试无意义
                if resp.status_code == 429 or resp.status_code >= 500:
                    raise LLMRetriableError(str(err)) from err
                raise err

            # 流式消费：一旦 on_delta 已推送过内容，中途失败也不再重试
            emitted = False

            async def _delta(delta: str) -> None:
                nonlocal emitted
                emitted = True
                if on_delta is not None:
                    r = on_delta(delta)
                    if asyncio.iscoroutine(r):
                        await r

            try:
                return await _scan_sse(resp, _delta)
            except _SSEError as e:
                # ctx 已取消/超时的传播由调用方处理；其余失败按 emitted 判断可否重试
                if not emitted:
                    raise LLMRetriableError(f"读取 SSE 流: {e}") from e
                raise LLMError(f"读取 SSE 流: {e}") from e
        finally:
            await resp.aclose()


class _SSEError(Exception):
    """SSE 流读取/解析失败。"""


async def _read_error_body(resp: httpx.Response) -> str:
    try:
        b = await resp.aread()
    except httpx.HTTPError:
        return "unknown error"
    b = b[:4096]
    if not b:
        return "unknown error"
    try:
        ae = json.loads(b)
        msg = ae.get("error", {}).get("message", "")
        if msg:
            return str(msg)
    except (ValueError, AttributeError):
        pass
    return b.decode("utf-8", errors="replace")


async def _scan_sse(
    resp: httpx.Response,
    on_delta: Callable[[str], Any],
) -> tuple[Message, Usage]:
    """逐行解析 SSE 流，累积出完整的 assistant 消息。"""
    msg = Message(role="assistant")
    tool_acc: dict[int, ToolCall] = {}
    usage = Usage()

    try:
        async for line in resp.aiter_lines():
            line = line.rstrip("\r")
            if line == "" or line.startswith(":"):
                continue  # SSE 注释行 / 空行（事件分隔符）
            if not line.startswith("data:"):
                continue  # 忽略 event: 等其他字段
            payload = line[len("data:"):].strip()
            if payload == "[DONE]":
                break

            try:
                ck = json.loads(payload)
            except ValueError:
                continue  # 容忍个别网关插入的非 JSON 心跳行
            if not isinstance(ck, dict):
                continue

            u = ck.get("usage")
            if isinstance(u, dict):
                usage = Usage(
                    prompt_tokens=int(u.get("prompt_tokens", 0) or 0),
                    completion_tokens=int(u.get("completion_tokens", 0) or 0),
                    total_tokens=int(u.get("total_tokens", 0) or 0),
                )

            for ch in ck.get("choices") or []:
                if not isinstance(ch, dict):
                    continue
                delta = ch.get("delta") or {}
                content = delta.get("content")
                if content:
                    msg.content += content
                    await _maybe_await(on_delta(content))
                for tcd in delta.get("tool_calls") or []:
                    idx = int(tcd.get("index", 0) or 0)
                    tc = tool_acc.get(idx)
                    if tc is None:
                        tc = ToolCall(index=idx, type="function")
                        tool_acc[idx] = tc
                    if tcd.get("id"):
                        tc.id = tcd["id"]
                    fn = tcd.get("function") or {}
                    if fn.get("name"):
                        tc.function.name += fn["name"]
                    if fn.get("arguments"):
                        tc.function.arguments += fn["arguments"]
    except httpx.HTTPError as e:
        raise _SSEError(str(e)) from e

    # 按 index 顺序装配 tool_calls；个别网关不回传 ID，做兜底。
    # 按 index 排序遍历（个别网关的 index 不从 0 开始 / 不连续，按 range(len) 会丢调用）
    for i in sorted(tool_acc):
        tc = tool_acc[i]
        if tc.id == "":
            tc.id = f"call_{i}_{int(time.time() * 1000)}"
        msg.tool_calls.append(tc)
    return msg, usage


async def _maybe_await(r: Any) -> None:
    if asyncio.iscoroutine(r):
        await r
