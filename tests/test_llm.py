"""LLM 流式客户端测试（SSE 解析 / tool_calls 增量装配 / 重试策略）。

用 httpx.MockTransport 模拟 OpenAI 兼容端点的 SSE 响应。
"""

import json

import httpx
import pytest

from mysqlagent.llm import FunctionCall, LLMClient, LLMError, LLMRetriableError, Message, Tool, ToolCall


def _sse(chunks: list[dict]) -> str:
    """把 chunk 列表序列化为 SSE 文本。"""
    lines = []
    for c in chunks:
        lines.append("data: " + json.dumps(c, ensure_ascii=False))
        lines.append("")
    lines.append("data: [DONE]")
    lines.append("")
    return "\n".join(lines)


def _content_chunks(deltas: list[str], usage: dict | None = None):
    out = []
    for d in deltas:
        out.append({"choices": [{"delta": {"content": d}}]})
    if usage:
        out.append({"choices": [], "usage": usage})
    return out


def _make_client(handler, **kwargs) -> LLMClient:
    transport = httpx.MockTransport(handler)
    return LLMClient(
        "http://llm.test/v1", "sk-test", "test-model",
        http=httpx.AsyncClient(transport=transport),
        **kwargs,
    )


async def _collect(client: LLMClient):
    deltas: list[str] = []

    async def on_delta(d: str):
        deltas.append(d)

    msg, usage = await client.chat_stream(
        [Message(role="user", content="hi")],
        [Tool(type="function", name="t", description="d", parameters={})],
        on_delta,
    )
    return msg, usage, deltas


@pytest.mark.asyncio
async def test_content_streaming():
    async def handler(request: httpx.Request) -> httpx.Response:
        body = _sse(_content_chunks(["你好", "，世界"], {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}))
        return httpx.Response(200, text=body, headers={"Content-Type": "text/event-stream"})

    client = _make_client(handler)
    msg, usage, deltas = await _collect(client)
    assert msg.role == "assistant"
    assert msg.content == "你好，世界"
    assert deltas == ["你好", "，世界"]
    assert usage.prompt_tokens == 10
    assert usage.completion_tokens == 5
    assert usage.total_tokens == 15


@pytest.mark.asyncio
async def test_tool_calls_assembly_by_index():
    """tool_calls 按 index 分片到达：id / name / arguments 增量拼接。"""
    chunks = [
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "call_1", "function": {"name": "execute_sql", "arguments": ""}},
        ]}}]},
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "function": {"name": "", "arguments": '{"sql":'}},
        ]}}]},
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "function": {"name": "", "arguments": '"SELECT 1"}'}},
        ]}}]},
        {"choices": [{"delta": {"tool_calls": [
            {"index": 1, "id": "call_2", "function": {"name": "list_tables", "arguments": "{}"}},
        ]}}]},
        {"choices": [], "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}},
    ]

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_sse(chunks), headers={"Content-Type": "text/event-stream"})

    client = _make_client(handler)
    msg, usage, _ = await _collect(client)
    assert len(msg.tool_calls) == 2
    assert msg.tool_calls[0].id == "call_1"
    assert msg.tool_calls[0].function.name == "execute_sql"
    assert msg.tool_calls[0].function.arguments == '{"sql":"SELECT 1"}'
    assert msg.tool_calls[1].id == "call_2"
    assert msg.tool_calls[1].function.name == "list_tables"
    # 序列化不含 index
    d = msg.tool_calls[0].to_dict()
    assert "index" not in d
    assert d == {
        "id": "call_1", "type": "function",
        "function": {"name": "execute_sql", "arguments": '{"sql":"SELECT 1"}'},
    }


@pytest.mark.asyncio
async def test_tool_call_id_fallback():
    """个别网关不回传 ID：按 index 兜底生成。"""
    chunks = [
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "function": {"name": "list_tables", "arguments": "{}"}},
        ]}}]},
    ]

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_sse(chunks), headers={"Content-Type": "text/event-stream"})

    client = _make_client(handler)
    msg, _, _ = await _collect(client)
    assert msg.tool_calls[0].id.startswith("call_0_")


@pytest.mark.asyncio
async def test_non_json_heartbeat_lines_tolerated():
    async def handler(request: httpx.Request) -> httpx.Response:
        body = ": keep-alive\n\ndata: not-json\n\n" + _sse(_content_chunks(["ok"]))
        return httpx.Response(200, text=body, headers={"Content-Type": "text/event-stream"})

    client = _make_client(handler)
    msg, _, _ = await _collect(client)
    assert msg.content == "ok"


@pytest.mark.asyncio
async def test_retry_on_429_then_success():
    calls = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, json={"error": {"message": "rate limited"}})
        return httpx.Response(200, text=_sse(_content_chunks(["ok"])),
                              headers={"Content-Type": "text/event-stream"})

    client = _make_client(handler, max_retries=2, retry_delay=0.01)
    msg, _, _ = await _collect(client)
    assert msg.content == "ok"
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_retry_on_5xx_then_success():
    calls = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503, text="upstream error")
        return httpx.Response(200, text=_sse(_content_chunks(["ok"])),
                              headers={"Content-Type": "text/event-stream"})

    client = _make_client(handler, max_retries=2, retry_delay=0.01)
    msg, _, _ = await _collect(client)
    assert msg.content == "ok"
    assert calls["n"] == 3


@pytest.mark.asyncio
async def test_retry_exhausted_raises():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    client = _make_client(handler, max_retries=1, retry_delay=0.01)
    with pytest.raises(LLMError):
        await _collect(client)


@pytest.mark.asyncio
async def test_4xx_not_retried():
    calls = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(400, json={"error": {"message": "bad request"}})

    client = _make_client(handler, max_retries=3, retry_delay=0.01)
    with pytest.raises(LLMError, match="bad request"):
        await _collect(client)
    assert calls["n"] == 1  # 4xx 参数类错误重试无意义


@pytest.mark.asyncio
async def test_connection_error_retriable():
    calls = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("connection refused")
        return httpx.Response(200, text=_sse(_content_chunks(["ok"])),
                              headers={"Content-Type": "text/event-stream"})

    client = _make_client(handler, max_retries=2, retry_delay=0.01)
    msg, _, _ = await _collect(client)
    assert msg.content == "ok"


@pytest.mark.asyncio
async def test_error_body_message_extracted():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": "invalid api key", "type": "auth"}})

    client = _make_client(handler, max_retries=0)
    with pytest.raises(LLMError, match="invalid api key"):
        await _collect(client)
