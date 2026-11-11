"""SQLite 持久化测试（对应 Go 版 internal/store/store_test.go）。"""

import pytest

from mysqlagent.llm import Message, ToolCall, FunctionCall
from mysqlagent.store import Store


@pytest.fixture
async def store(tmp_path):
    st = await Store(str(tmp_path / "sessions.db")).open()
    yield st
    await st.close()


@pytest.mark.asyncio
async def test_save_and_load_messages_roundtrip(store):
    msgs = [
        Message(role="user", content="有哪些表？"),
        Message(role="assistant", content="", tool_calls=[
            ToolCall(id="call_1", type="function",
                     function=FunctionCall(name="list_tables", arguments="{}")),
        ]),
        Message(role="tool", tool_call_id="call_1", name="list_tables", content='{"tables":[]}'),
        Message(role="assistant", content="共 3 张表"),
    ]
    await store.save_messages("s-1", msgs)
    loaded = await store.load_messages("s-1")

    assert len(loaded) == 4
    assert [m.role for m in loaded] == ["user", "assistant", "tool", "assistant"]
    assert loaded[1].tool_calls[0].id == "call_1"
    assert loaded[1].tool_calls[0].function.name == "list_tables"
    assert loaded[1].tool_calls[0].function.arguments == "{}"
    assert loaded[2].tool_call_id == "call_1"
    assert loaded[2].name == "list_tables"
    assert loaded[3].content == "共 3 张表"


@pytest.mark.asyncio
async def test_save_messages_replaces(store):
    await store.save_messages("s-2", [Message(role="user", content="first")])
    await store.save_messages("s-2", [Message(role="user", content="second")])
    loaded = await store.load_messages("s-2")
    assert len(loaded) == 1
    assert loaded[0].content == "second"


@pytest.mark.asyncio
async def test_load_unknown_session(store):
    assert await store.load_messages("nope") == []


@pytest.mark.asyncio
async def test_audit_insert_and_replay(store):
    for i in range(5):
        store.log_audit({
            "ts": f"2026-10-15T10:00:0{i}Z",
            "event": "tool_result",
            "session": "s-3",
            "user": "alice",
            "trace_id": "t-1",
            "step": i,
            "tool": "list_tables",
        })
    # log_audit 内部 create_task 异步入库：等待一拍让任务完成
    import asyncio
    await asyncio.sleep(0.05)

    events = await store.replay_audit("s-3", 3)
    assert len(events) == 3
    # 倒序：最新在前
    assert events[0]["step"] == 4
    assert events[0]["event"] == "tool_result"
    assert events[0]["trace_id"] == "t-1"
    assert events[0]["tool"] == "list_tables"

    # 其他会话无数据
    assert await store.replay_audit("other", 10) == []


@pytest.mark.asyncio
async def test_replay_audit_limit_clamped(store):
    store.log_audit({"ts": "t", "event": "e", "session": "s-4"})
    import asyncio
    await asyncio.sleep(0.05)
    # limit <= 0 → 按 100 处理；limit > 1000 → 按 100 处理（与 Go 版一致）
    events = await store.replay_audit("s-4", 0)
    assert len(events) == 1
