"""mysqlagent.agent — Agent Loop 编排。

移植自 Go 版 internal/agent/agent.go：
流式调用 LLM → 若返回 tool_calls 则执行工具并回填结果 → 再次调用 LLM，
直到 LLM 不再请求工具（正常回答）、达到步数上限、触发循环熔断或预算熔断。

失控三重熔断：步数上限（max_loops）、同一(工具,参数)重复调用熔断
（max_tool_repeat）、单请求累计 token 预算（budget_tokens）。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from mysqlagent import metrics
from mysqlagent.guardrails.guardrails import sanitize_tool_result
from mysqlagent.guardrails.pii import mask_pii
from mysqlagent.llm import LLMClient, LLMError, Message, Usage
from mysqlagent.prompt import SYSTEM_PROMPT
from mysqlagent.tools.registry import Registry


@dataclass
class Event:
    """Agent 运行过程中向前端推送的事件。"""

    name: str  # message / tool_call / tool_result / done / error
    data: Any = None


@dataclass
class Options:
    max_loops: int = 10          # 循环步数上限
    call_timeout: float = 90.0   # 单次 LLM 调用超时（秒，须小于请求级超时，逐层收敛）
    tool_isolation: bool = True  # 工具结果间接注入清洗
    pii_masking: bool = True     # 工具结果 PII 掩码
    budget_tokens: int = 100000  # 单请求 token 预算（累计 prompt+completion，<=0 不限）
    max_tool_repeat: int = 3     # 同一(工具,参数)调用次数熔断阈值（<=0 默认 3）


async def _maybe_await(r: Any) -> None:
    if asyncio.iscoroutine(r):
        await r


class Agent:
    """编排 LLM 与工具集的多轮循环。"""

    def __init__(self, client: LLMClient, registry: Registry, opts: Optional[Options] = None) -> None:
        self.client = client
        self.tools = registry
        self.opts = opts or Options()
        if self.opts.max_loops <= 0:
            self.opts.max_loops = 10
        if self.opts.max_tool_repeat <= 0:
            self.opts.max_tool_repeat = 3

    def tool_names(self) -> list[str]:
        return self.tools.names()

    @staticmethod
    def _arg_hashes(tool_name: str, args: str) -> tuple[str, str]:
        """计算工具参数的哈希摘要；对 execute_sql 额外提取 SQL 哈希
        （审计只落哈希不落原文）。"""
        h = hashlib.sha256(args.encode("utf-8")).hexdigest()[:8]
        args_sha = "sha256:" + h
        sql_sha = ""
        if tool_name == "execute_sql":
            try:
                parsed = json.loads(args)
                sql = parsed.get("sql", "") if isinstance(parsed, dict) else ""
            except ValueError:
                sql = ""
            if sql:
                sh = hashlib.sha256(sql.encode("utf-8")).hexdigest()[:8]
                sql_sha = "sha256:" + sh
        return args_sha, sql_sha

    async def run(
        self,
        history: list[Message],
        emit: Callable[[Event], Any],
        audit_fn: Optional[Callable[[dict[str, Any]], None]] = None,
    ) -> tuple[list[Message], str]:
        """执行一次完整的 Agent 循环。

        history 是当前会话（不含 system）的完整消息历史，末尾是本次用户提问；
        全部事件通过 emit 实时推送（支持 async 回调）；audit_fn 是本请求的审计
        回调（可为 None，server 注入时会携带 trace_id / session / user 等公共字段）。
        返回更新后的消息历史与结束原因（normal / loop_detected / budget_exceeded /
        max_loops / timeout / client_abort / model_error）。
        """

        def audit(event: str, fields: dict[str, Any]) -> None:
            if audit_fn is None:
                return
            e: dict[str, Any] = {"event": event}
            e.update(fields)
            audit_fn(e)

        async def _emit(ev: Event) -> None:
            await _maybe_await(emit(ev))

        messages: list[Message] = [Message(role="system", content=SYSTEM_PROMPT)]
        messages.extend(_copy_message(m) for m in history)

        defs = self.tools.defs()
        total_prompt = 0
        total_completion = 0
        total_all = 0
        steps = 0
        reason = "normal"
        last_had_tools = False  # 最后一步是否仍请求工具（用于区分 max_loops）
        tool_repeat: dict[str, int] = {}  # 循环失控检测：(工具,参数哈希) → 次数

        for step in range(1, self.opts.max_loops + 1):
            steps = step

            # 单次 LLM 调用超时：逐层收敛（请求级超时 > 单步超时）。
            # 请求级取消以 CancelledError 穿透本层，由 server 统一归类；
            # 本层 deadline 到期抛 TimeoutError，归类为 timeout。
            llm_start = time.monotonic()
            try:
                if self.opts.call_timeout > 0:
                    async with asyncio.timeout(self.opts.call_timeout):
                        assistant, usage = await self._chat_step(messages, defs, step, _emit)
                else:
                    assistant, usage = await self._chat_step(messages, defs, step, _emit)
            except TimeoutError:
                audit("llm_call", {
                    "step": step,
                    "duration_ms": int((time.monotonic() - llm_start) * 1000),
                    "usage": None,
                })
                await _emit(Event("error", {
                    "code": "timeout",
                    "message": "请求超时（已达时间上限），请缩小问题范围后重试",
                }))
                metrics.FAILURES.inc("timeout")
                return history, "timeout"
            except LLMError as e:
                audit("llm_call", {
                    "step": step,
                    "duration_ms": int((time.monotonic() - llm_start) * 1000),
                    "usage": None,
                })
                await _emit(Event("error", {
                    "code": "model_error",
                    "message": f"调用 LLM 失败: {e}",
                }))
                metrics.FAILURES.inc("model_error")
                # 只记异常类型，不落异常原文（可能含上游返回的请求片段）
                audit("agent_abort", {"step": step, "reason": "model_error", "error_type": type(e).__name__})
                return history, "model_error"

            llm_duration_ms = int((time.monotonic() - llm_start) * 1000)
            total_prompt += usage.prompt_tokens
            total_completion += usage.completion_tokens
            total_all += usage.total_tokens
            metrics.LLM_TOKENS.add(["prompt"], usage.prompt_tokens)
            metrics.LLM_TOKENS.add(["completion"], usage.completion_tokens)
            audit("llm_call", {"step": step, "duration_ms": llm_duration_ms, "usage": usage.to_dict()})
            messages.append(assistant)

            # token 预算熔断：达到预算即中止（而不是默默继续）
            if self.opts.budget_tokens > 0 and total_all >= self.opts.budget_tokens:
                reason = "budget_exceeded"
                await _emit(Event("error", {
                    "code": "budget_exceeded",
                    "message": (
                        f"已达到单次请求 token 预算（{self.opts.budget_tokens}），"
                        "本次任务停止；请缩小问题范围后重新提问"
                    ),
                }))
                metrics.FAILURES.inc("budget_exceeded")
                audit("budget_exceeded", {"step": step, "tokens": total_all})
                break

            # 没有工具调用 → 本轮回答完成
            if not assistant.tool_calls:
                last_had_tools = False
                break
            last_had_tools = True

            # 逐个执行工具，结果以 role=tool 消息回填
            loop_break = False
            for tc in assistant.tool_calls:
                # 循环失控检测：同一(工具,参数)反复调用 → 熔断
                args_sha, sql_sha = self._arg_hashes(tc.function.name, tc.function.arguments)
                repeat_key = tc.function.name + ":" + args_sha
                tool_repeat[repeat_key] = tool_repeat.get(repeat_key, 0) + 1
                if tool_repeat[repeat_key] >= self.opts.max_tool_repeat:
                    reason = "loop_detected"
                    await _emit(Event("error", {
                        "code": "loop_detected",
                        "message": (
                            f"检测到工具 {tc.function.name} 被以相同参数重复调用 "
                            f"{tool_repeat[repeat_key]} 次，已自动停止"
                        ),
                    }))
                    metrics.FAILURES.inc("loop_detected")
                    audit("loop_detected", {
                        "step": step, "tool": tc.function.name,
                        "args_sha": args_sha, "repeat": tool_repeat[repeat_key],
                    })
                    loop_break = True
                    break

                await _emit(Event("tool_call", {
                    "call_id": tc.id,
                    "name": tc.function.name,
                    "arguments": tc.function.arguments,
                    "step": step,
                }))
                audit("tool_call", {
                    "step": step, "tool": tc.function.name,
                    "args_sha": args_sha, "sql_sha": sql_sha,
                })

                start = time.monotonic()
                try:
                    result = await self.tools.call(tc.function.name, tc.function.arguments)
                except Exception as e:  # 系统级异常也按业务错误回填，让 LLM 自行处理
                    result = f"TOOL_ERROR: {e}"
                duration_ms = int((time.monotonic() - start) * 1000)
                ok = not result.startswith("TOOL_ERROR:")

                # 出站防护管线：间接注入隔离 → PII 掩码（进入 LLM 上下文之前）
                pii_kinds: list[str] = []
                isolated = False
                # 两个开关各自独立生效（此前任一开启会同时执行两种处理）
                if self.opts.tool_isolation:
                    before = result
                    result = sanitize_tool_result(result)
                    isolated = result != before
                if self.opts.pii_masking:
                    result, pii_kinds = mask_pii(result)
                if isolated:
                    metrics.GUARDRAIL_HITS.inc("tool_isolation", "ISOLATION", "transform")
                    audit("tool_isolation", {"step": step, "tool": tc.function.name})
                if pii_kinds:
                    audit("pii_masked", {"step": step, "tool": tc.function.name, "kinds": pii_kinds})
                metrics.TOOL_CALLS.inc(tc.function.name, "true" if ok else "false")
                metrics.TOOL_DURATION.add([tc.function.name], duration_ms)
                audit("tool_result", {
                    "step": step, "tool": tc.function.name,
                    "ok": ok, "duration_ms": duration_ms,
                })

                await _emit(Event("tool_result", {
                    "call_id": tc.id,
                    "name": tc.function.name,
                    "ok": ok,
                    "result": preview(result, 2000),
                    "duration_ms": duration_ms,
                    "step": step,
                }))
                messages.append(Message(
                    role="tool", tool_call_id=tc.id, name=tc.function.name, content=result,
                ))
            if loop_break:
                break

            # 循环继续：携带工具结果再次调用 LLM

        # 循环步数耗尽但最后一步仍在请求工具：与正常结束区分开
        if reason == "normal" and last_had_tools:
            reason = "max_loops"

        await _emit(Event("done", {
            "steps": steps,
            "usage": {
                "prompt_tokens": total_prompt,
                "completion_tokens": total_completion,
                "total_tokens": total_all,
            },
            "reason": reason,
        }))

        # 持久化：仅正常完成时保留本次增量（去掉 system，含 tool 中间消息），
        # 由 server 层按 history_limit 裁剪。异常结束（熔断/超时/错误）回退原始
        # 历史——半途的 assistant(tool_calls) 若无对应 tool 结果会成为悬空消息，
        # 下一次 LLM 调用会被多数 OpenAI 兼容端点直接拒绝。
        if reason == "normal" and len(messages) > 1:
            return messages[1:], reason
        return history, reason

    async def _chat_step(
        self,
        messages: list[Message],
        defs: list,
        step: int,
        emit: Callable[[Event], Any],
    ) -> tuple[Message, Usage]:
        """执行一步流式 LLM 调用；返回 (assistant 消息, token 用量)。"""

        async def _on_delta(delta: str) -> None:
            await _maybe_await(emit(Event("message", {"delta": delta, "step": step})))

        return await self.client.chat_stream(messages, defs, _on_delta)


def _copy_message(m: Message) -> Message:
    return Message(
        role=m.role,
        content=m.content,
        tool_calls=list(m.tool_calls),
        tool_call_id=m.tool_call_id,
        name=m.name,
    )


def preview(s: str, n: int) -> str:
    """按字符截断（对应 Go 的 rune 截断）。"""
    if len(s) <= n:
        return s
    return s[:n] + "…(截断)"
