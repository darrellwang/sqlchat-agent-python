"""mysqlagent.server — HTTP API 与 SSE 流式服务（FastAPI / uvicorn）。

移植自 Go 版 internal/server/（server.go + middleware.go）：
  - POST /api/chat（SSE 流式：meta / message / tool_call / tool_result / done / error + 心跳）
  - GET  /api/health — 健康检查（免认证）
  - GET  /metrics — Prometheus 指标（免认证）
  - GET  /api/tools — 已注册工具列表（需认证）
  - GET  /api/sessions/{id}/messages — 会话消息回放（需认证；需开启持久化）
  - GET  /api/sessions/{id}/audit — 会话关联审计事件检索（需认证；倒序）
  - GET  / — 静态聊天页（公开）

接入层：Bearer 认证、per-key 令牌桶限流、全局并发隔舱、CORS 白名单、
输入护栏、trace_id 全链路、SSE 心跳保活、逐层收敛超时。
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import pathlib
import re
import secrets
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Optional

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, StreamingResponse

from mysqlagent import metrics
from mysqlagent.agent import Agent, Event
from mysqlagent.audit import AuditLogger
from mysqlagent.config import APIKey, Config
from mysqlagent.db import acl
from mysqlagent.guardrails.guardrails import check_input
from mysqlagent.llm import Message
from mysqlagent.store import Store

# SSE 流的特殊队列标记。
_PING = object()        # 心跳注释行
_STREAM_END = object()  # 流结束 sentinel


class _Slot:
    """幂等的并发名额持有者：生产者与消费者（SSE 生成器）都可能先行收尾，
    谁先到谁释放，另一方为无操作（避免重复 release / 名额泄漏）。"""

    def __init__(self, sem: asyncio.Semaphore) -> None:
        self._sem = sem
        self._released = False

    def release(self) -> None:
        if not self._released:
            self._released = True
            self._sem.release()


# ---------- 接入层数据结构 ----------

@dataclass
class Identity:
    """通过认证后的调用者身份，贯穿限流、审计与表级 ACL。"""

    key: str
    name: str
    tables: list[str] = field(default_factory=list)

    @property
    def owner_id(self) -> str:
        """会话归属标识：API Key 的哈希前缀（不在存储 / 日志中落明文 key）。"""
        return hashlib.sha256(self.key.encode("utf-8")).hexdigest()[:16]


@dataclass
class _Session:
    """会话：互斥锁 + 消息历史（对应 Go 版 session 结构体）。"""

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    messages: list[Message] = field(default_factory=list)
    owner: str = ""          # 创建者标识（Identity.owner_id；认证关闭时为 anonymous）
    last_used: float = field(default_factory=time.monotonic)


class SessionForbidden(Exception):
    """调用者不是该会话的创建者。"""


# 会话 ID 白名单：长度 / 字符集受限，避免超长键与存储层注入面。
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")
ANONYMOUS_OWNER = "anonymous"


class TokenBucket:
    """令牌桶限流器（对应 Go 版 tokenBucket，asyncio.Lock 保证并发安全）。"""

    def __init__(self, rate: float, burst: int) -> None:
        self._mu = asyncio.Lock()
        self._tokens = float(burst)
        self._last = time.monotonic()
        self._rate = rate
        self._burst = float(burst)

    async def allow(self) -> bool:
        async with self._mu:
            now = time.monotonic()
            self._tokens += (now - self._last) * self._rate
            if self._tokens > self._burst:
                self._tokens = self._burst
            self._last = now
            if self._tokens >= 1:
                self._tokens -= 1
                return True
            return False


class KeyLimiters:
    """按身份维护令牌桶（惰性创建）。"""

    def __init__(self, rate: float, burst: int) -> None:
        self._mu = asyncio.Lock()
        self._rate = rate
        self._burst = burst
        self._buckets: dict[str, TokenBucket] = {}

    async def allow(self, key: str) -> bool:
        async with self._mu:
            b = self._buckets.get(key)
            if b is None:
                b = TokenBucket(self._rate, self._burst)
                self._buckets[key] = b
        return await b.allow()


# ---------- 中间件 ----------

class MetricsMiddleware:
    """纯 ASGI 中间件：统计 HTTP 请求量（按路径与状态码）。

    对应 Go 版 instrument 包装；只统计业务 API 路径（会话类路径归一）。
    纯 ASGI 实现（非 BaseHTTPMiddleware），对 SSE 流式响应零缓冲。
    """

    def __init__(self, app: Callable) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        status = 500

        async def send_wrapper(message: dict) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            path = _metric_path(scope.get("path", ""))
            if path is not None:
                metrics.REQUESTS_TOTAL.inc(path, str(status))


def _metric_path(path: str) -> Optional[str]:
    if path.startswith("/api/sessions"):
        return "/api/sessions"
    if path in ("/api/chat", "/api/tools"):
        return path
    return None


# ---------- 认证依赖 ----------

async def identity_dependency(request: Request) -> Optional[Identity]:
    """FastAPI 依赖：提取并校验 Bearer 身份（从 app.state 取 Server 实例）。"""
    server: "Server" = request.app.state.server
    return await server.get_identity(request)


# ---------- Server ----------

class Server:
    """装配全部接入层组件的 HTTP 服务。"""

    def __init__(
        self,
        agent: Agent,
        cfg: Config,
        audit_logger: Optional[AuditLogger] = None,
        store: Optional[Store] = None,
    ) -> None:
        self.agent = agent
        self.cfg = cfg
        self.audit_logger = audit_logger
        self.st = store

        self.history_limit = cfg.agent.history_limit
        self.request_timeout = float(cfg.server.request_timeout)
        self.heartbeat = float(cfg.server.heartbeat_interval)
        self.global_tables = list(cfg.database.tables)
        self.input_guard_enabled = bool(cfg.guardrails.input_enabled)
        self.max_input_chars = cfg.guardrails.max_input_chars
        self._static_html: Optional[str] = None

        # 认证
        self.auth_enabled = len(cfg.server.api_keys) > 0
        self.keys: dict[str, APIKey] = {}
        for k in cfg.server.api_keys:
            self.keys[k.key] = k

        # 限流与并发隔舱
        self.limiters = KeyLimiters(cfg.rate_limit.per_key_qps, cfg.rate_limit.per_key_burst)
        self.concurrency = asyncio.Semaphore(cfg.rate_limit.max_concurrent)

        # 会话
        self._sessions: "OrderedDict[str, _Session]" = OrderedDict()
        self._sessions_mu = asyncio.Lock()
        # 内存会话上限与空闲 TTL：防止无限增长（被淘汰的会话在持久化模式下仍可从 SQLite 恢复）
        self.max_sessions = 1000
        self.session_ttl = 24 * 3600.0
        # 请求体上限：输入上限（字符，按最坏 4 字节/字符）+ 余量，先于 JSON 解析限制内存占用
        self.max_body_bytes = max(16 * 1024, self.max_input_chars * 4 + 4096)

        self.app = self._build_app()

    # ---------- 应用装配 ----------

    def _build_app(self) -> FastAPI:
        app = FastAPI(title="mysqlagent", docs_url=None, redoc_url=None, openapi_url=None)
        app.state.server = self

        if self.cfg.server.cors_origins:
            app.add_middleware(
                CORSMiddleware,
                allow_origins=list(self.cfg.server.cors_origins),
                allow_methods=["GET", "POST", "OPTIONS"],
                allow_headers=["Authorization", "Content-Type"],
                max_age=600,
            )
        # 指标中间件最后添加 → 位于最外层（对应 Go 版 instrument 外层包装）
        app.add_middleware(MetricsMiddleware)

        app.add_api_route("/api/health", self.handle_health, methods=["GET"])
        app.add_api_route("/metrics", self.handle_metrics, methods=["GET"])
        app.add_api_route("/api/chat", self.handle_chat, methods=["POST"])
        app.add_api_route(
            "/api/tools", self.handle_tools, methods=["GET"],
            dependencies=[Depends(identity_dependency)],
        )
        app.add_api_route(
            "/api/sessions/{session_id}/messages",
            self.handle_replay_messages, methods=["GET"],
            dependencies=[Depends(identity_dependency)],
        )
        app.add_api_route(
            "/api/sessions/{session_id}/audit",
            self.handle_replay_audit, methods=["GET"],
            dependencies=[Depends(identity_dependency)],
        )
        app.add_api_route("/", self.handle_static, methods=["GET"])

        @app.exception_handler(HTTPException)
        async def _http_exc_handler(_: Request, exc: HTTPException) -> JSONResponse:
            # 统一错误体形状：{"error": "..."}（与 Go 版一致）
            detail = exc.detail if isinstance(exc.detail, str) else json.dumps(exc.detail, ensure_ascii=False)
            return JSONResponse({"error": detail}, status_code=exc.status_code)

        return app

    # ---------- 通用 ----------

    def audit_event(self, event: str, fields: dict[str, Any]) -> None:
        """投递一条结构化安全审计事件（异步落盘；只落哈希与摘要）。"""
        if self.audit_logger is None:
            return
        e: dict[str, Any] = {"event": event}
        e.update(fields)
        self.audit_logger.log(e)

    async def get_identity(self, request: Request) -> Optional[Identity]:
        """API 认证。api_keys 为空时认证关闭（仅限本地开发）。"""
        if not self.auth_enabled:
            return None
        remote = request.client.host if request.client else ""
        token = _bearer_token(request)
        if token == "":
            self.audit_event("auth_denied", {"reason": "missing_token", "remote": remote})
            raise HTTPException(
                status_code=401,
                detail="缺少 API Key：请在请求头携带 Authorization: Bearer <key>",
            )
        ak = self.keys.get(token)
        if ak is None:
            self.audit_event("auth_denied", {"reason": "invalid_token", "remote": remote})
            metrics.FAILURES.inc("auth_denied")
            raise HTTPException(status_code=401, detail="API Key 无效")
        return Identity(key=ak.key, name=ak.name, tables=list(ak.tables))

    # ---------- 简单端点 ----------

    async def handle_health(self) -> JSONResponse:
        return JSONResponse({
            "status": "ok",
            "time": datetime.now().astimezone().isoformat(timespec="seconds"),
        })

    async def handle_metrics(self) -> PlainTextResponse:
        buf = io.StringIO()
        metrics.write_text(buf)
        # 审计缓冲溢出以指标形式暴露（此前每次抓取都 print 一行，会刷屏）
        dropped = self.audit_logger.dropped if self.audit_logger is not None else 0
        buf.write(
            "# HELP mysqlagent_audit_dropped_total 审计事件因缓冲溢出被丢弃的累计数\n"
            "# TYPE mysqlagent_audit_dropped_total counter\n"
            f"mysqlagent_audit_dropped_total {dropped}\n"
        )
        return PlainTextResponse(buf.getvalue(), media_type="text/plain; version=0.0.4; charset=utf-8")

    async def handle_tools(self) -> JSONResponse:
        return JSONResponse({"tools": self.agent.tool_names()})

    async def handle_static(self, request: Request) -> HTMLResponse:
        if request.url.path != "/":
            raise HTTPException(status_code=404, detail="Not Found")
        if self._static_html is None:
            self._static_html = (
                pathlib.Path(__file__).parent / "static" / "index.html"
            ).read_text(encoding="utf-8")
        return HTMLResponse(self._static_html)

    # ---------- SSE 核心 ----------

    async def handle_chat(
        self,
        request: Request,
        identity: Optional[Identity] = Depends(identity_dependency),
    ) -> Any:
        """SSE 核心：限流 → 输入护栏 → 并发隔舱 → 流式响应（Agent 循环在流内执行）。"""
        # 限流：per-key 令牌桶（未启用认证时按 anonymous 维度）
        limiter_key = identity.key if identity is not None else ANONYMOUS_OWNER
        # 审计 / 日志中的用户标识用身份名，绝不落 API Key 明文
        audit_user = identity.name if identity is not None else ANONYMOUS_OWNER
        owner = identity.owner_id if identity is not None else ANONYMOUS_OWNER
        if not await self.limiters.allow(limiter_key):
            self.audit_event("rate_limited", {
                "user": audit_user,
                "remote": request.client.host if request.client else "",
            })
            metrics.FAILURES.inc("rate_limited")
            return JSONResponse(
                {"error": "请求过于频繁，请稍后再试"},
                status_code=429,
                headers={"Retry-After": "3"},
            )

        # 请求体解析（手工解析以保持与 Go 版一致的错误文案）
        # 流式读取并设上限：先限制内存占用，再解析 JSON（max_input_chars 检查在解析之后，
        # 不能防住超大请求体）。
        declared = request.headers.get("content-length")
        if declared is not None and declared.isdigit() and int(declared) > self.max_body_bytes:
            return JSONResponse({"error": "请求体过大"}, status_code=413)
        chunks: list[bytes] = []
        total = 0
        async for chunk in request.stream():
            total += len(chunk)
            if total > self.max_body_bytes:
                return JSONResponse({"error": "请求体过大"}, status_code=413)
            chunks.append(chunk)
        try:
            payload = json.loads(b"".join(chunks))
        except Exception:
            return JSONResponse({"error": "请求体必须是 JSON"}, status_code=400)
        if not isinstance(payload, dict):
            return JSONResponse({"error": "请求体必须是 JSON 对象"}, status_code=400)
        message = payload.get("message")
        if not isinstance(message, str) or message == "":
            return JSONResponse({"error": "message 不能为空"}, status_code=400)
        session_id = payload.get("session_id")
        if session_id is not None and not isinstance(session_id, str):
            return JSONResponse({"error": "session_id 必须是字符串"}, status_code=400)
        if session_id and not _SESSION_ID_RE.match(session_id):
            return JSONResponse(
                {"error": "session_id 只允许字母 / 数字 / _ . - ，长度 1~64"}, status_code=400
            )

        # 输入护栏（提示注入检测，SSE 建立前拦截，返回普通 400）
        if self.input_guard_enabled:
            d = check_input(message, self.max_input_chars)
            if d.blocked():
                # 只落命中片段的哈希与长度，不落用户输入原文（与审计"只落哈希"的承诺一致）
                self.audit_event("guardrail_block", {
                    "user": audit_user, "rule": d.rule, "action": d.action,
                    "desc": d.desc, **_digest_fields(d.matched),
                })
                metrics.GUARDRAIL_HITS.inc("input", d.rule, d.action)
                metrics.FAILURES.inc("guardrail_block")
                return JSONResponse(
                    {"error": "输入被安全策略拦截: " + d.desc, "rule": d.rule},
                    status_code=400,
                )
            if d.rule != "":  # warn 级：放行但记录
                self.audit_event("guardrail_warn", {
                    "user": audit_user, "rule": d.rule, **_digest_fields(d.matched),
                })
                metrics.GUARDRAIL_HITS.inc("input", d.rule, d.action)

        # 会话装配与归属校验：他人的会话一律 403（含 SQLite 里恢复的历史会话）；
        # 同会话上一条消息仍在处理时直接 409，避免排队请求空占全局并发名额。
        try:
            sid, sess = await self._get_or_create_session(session_id or "", owner)
        except SessionForbidden:
            self.audit_event("session_forbidden", {"user": audit_user, "session": session_id})
            metrics.FAILURES.inc("session_forbidden")
            return JSONResponse({"error": "无权访问该会话"}, status_code=403)
        if sess.lock.locked():
            return JSONResponse(
                {"error": "该会话正在处理上一条消息，请稍后重试"},
                status_code=409,
                headers={"Retry-After": "2"},
            )

        # 全局并发隔舱：Agent 会话是长连接，占用 DB 与 LLM 资源。
        # Semaphore.locked() = 无法立即获取（对应 Go 版 select-default 非阻塞尝试）；
        # 检查与获取之间无 await，不会发生竞态。
        if self.concurrency.locked():
            self.audit_event("concurrency_limited", {"user": audit_user})
            metrics.FAILURES.inc("concurrency_limited")
            return JSONResponse(
                {"error": "服务繁忙（并发已满），请稍后再试"},
                status_code=503,
            )
        await self.concurrency.acquire()
        slot = _Slot(self.concurrency)

        return StreamingResponse(
            self._chat_stream(slot, audit_user, identity, message, sid, sess, owner),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    async def _chat_stream(
        self,
        slot: "_Slot",
        user: str,
        identity: Optional[Identity],
        message: str,
        sid: str,
        sess: "_Session",
        owner: str,
    ):
        """SSE 事件流：生产者（Agent 循环 + 收尾）与消费者（写出）分离，保证真流式。"""
        queue: asyncio.Queue = asyncio.Queue()
        producer = asyncio.create_task(
            self._run_chat(queue, slot, user, identity, message, sid, sess, owner)
        )
        # 生产者的异常在此处无人 await：挂回调取走 exception，避免
        # "Task exception was never retrieved" 噪音。
        producer.add_done_callback(_drain_task_exception)
        try:
            while True:
                item = await queue.get()
                if item is _STREAM_END:
                    break
                if item is _PING:
                    yield ": ping\n\n"
                    continue
                name, data = item
                yield f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
        finally:
            # 客户端断开 / 响应结束：取消生产者（其 finally 完成会话内收尾），
            # 并幂等释放并发名额。注意：生成器 finally 内不可 await
            # （aclose 语义下会报 "async generator ignored GeneratorExit"），
            # 因此这里只做同步操作；生产者的异步收尾由它自己完成。
            if not producer.done():
                producer.cancel()
            slot.release()

    async def _run_chat(
        self,
        queue: asyncio.Queue,
        slot: "_Slot",
        user: str,
        identity: Optional[Identity],
        message: str,
        sid: str,
        sess: "_Session",
        owner: str,
    ) -> None:
        """生产者：Agent 循环 → 持久化/指标/审计收尾（会话已在 handle_chat 装配并校验归属）。"""
        hb_task: Optional[asyncio.Task] = None
        try:
            trace_id = _new_trace_id()

            # 会话级互斥：同一会话不并发处理两条消息
            async with sess.lock:
                sess.messages.append(Message(role="user", content=message))

                def emit(ev: Event) -> None:
                    # 无界队列：put_nowait 绝不阻塞，取消场景下同样安全
                    queue.put_nowait((ev.name, ev.data))

                # SSE 心跳保活：注释行（": ping"），对客户端事件解析透明，
                # 防止代理 / 负载均衡器因空闲超时掐断长连接。
                if self.heartbeat > 0:
                    hb_task = asyncio.create_task(self._heartbeat(queue))

                # 表级 ACL（权限交集铁律：调用者 ∩ 工具可访问范围）：
                # 把本请求的有效允许表清单注入上下文，工具执行时读取校验。
                # 未注入 = 不限制；空清单 = 交集为空（拒绝全部表）。
                acl_token = None
                if len(self.global_tables) > 0 or (identity is not None and len(identity.tables) > 0):
                    effective = acl.effective_tables(
                        self.global_tables, identity.tables if identity else None
                    )
                    acl_token = acl.context_with_tables(effective)

                try:
                    # trace_id 首发事件：前端 / 用户反馈问题时可引用
                    emit(Event("meta", {"trace_id": trace_id, "session_id": sid}))

                    # 本请求的审计闭包：为 agent 的 step 级事件补齐公共归属字段
                    def auditor(e: dict[str, Any]) -> None:
                        e["trace_id"] = trace_id
                        e["session"] = sid
                        e["user"] = user
                        if self.audit_logger is not None:
                            self.audit_logger.log(e)

                    self.audit_event("session_start", {
                        "trace_id": trace_id, "session": sid, "user": user,
                        "input_chars": len(message),
                    })

                    start = time.monotonic()
                    updated: Optional[list[Message]] = None
                    reason = "normal"
                    try:
                        # 请求级超时收敛（整体短于客户端连接生命周期；
                        # 内部 LLM 单次调用超时更短，见 Agent 的逐层收敛）
                        async with asyncio.timeout(self.request_timeout):
                            updated, reason = await self.agent.run(sess.messages, emit, auditor)
                    except TimeoutError:
                        emit(Event("error", {
                            "code": "timeout",
                            "message": "请求超时（已达时间上限），请缩小问题范围后重试",
                        }))
                        reason = "timeout"
                    except asyncio.CancelledError:
                        # 客户端断开：对应 Go 版请求 ctx 取消分支
                        emit(Event("error", {
                            "code": "client_abort",
                            "message": "客户端已断开或请求被取消",
                        }))
                        reason = "client_abort"

                    # 异常结束（熔断/超时/取消）时 agent 回退原始历史
                    sess.messages = trim_history(
                        updated if updated is not None else sess.messages, self.history_limit
                    )

                    # 持久化：会话消息落 SQLite（失败仅记日志，不影响响应）
                    if self.st is not None:
                        try:
                            await self.st.save_messages(sid, sess.messages, owner)
                        except Exception as err:
                            print(f"保存会话 {sid} 失败: {err}")

                    elapsed_ms = int((time.monotonic() - start) * 1000)
                    metrics.CHAT_SESSIONS.inc(reason)
                    metrics.CHAT_SECONDS.add([], elapsed_ms // 1000)
                    sess.last_used = time.monotonic()
                    self.audit_event("session_done", {
                        "trace_id": trace_id, "session": sid, "user": user,
                        "reason": reason, "duration_ms": elapsed_ms,
                    })
                finally:
                    if acl_token is not None:
                        acl.reset_allowed_tables(acl_token)
        finally:
            if hb_task is not None:
                hb_task.cancel()
            slot.release()
            queue.put_nowait(_STREAM_END)

    async def _heartbeat(self, queue: asyncio.Queue) -> None:
        try:
            while True:
                await asyncio.sleep(self.heartbeat)
                queue.put_nowait(_PING)
        except asyncio.CancelledError:
            pass

    def _owner_allowed(self, session_owner: str, caller: str) -> bool:
        """会话归属校验。认证关闭时不区分调用者；旧版本遗留的无主会话（owner 为空）
        在认证开启后不再对任何人开放。"""
        if not self.auth_enabled:
            return True
        return session_owner != "" and session_owner == caller

    async def _get_or_create_session(self, sid: str, owner: str) -> tuple[str, _Session]:
        """取得（或创建）会话并校验归属；非创建者访问抛 SessionForbidden。"""
        async with self._sessions_mu:
            if sid == "":
                # 密码学随机（保留 auto- 前缀以兼容既有客户端 / 测试）；
                # 此前用 monotonic_ns，可预测且重启后会撞号。
                sid = "auto-" + secrets.token_urlsafe(12)
            sess = self._sessions.get(sid)
            if sess is None:
                sess = _Session(owner=owner)
                # 持久化模式：重启后从 SQLite 恢复历史与归属（失败降级为空会话）
                if self.st is not None:
                    try:
                        stored_owner = await self.st.get_owner(sid)
                        if stored_owner is not None:
                            sess.owner = stored_owner
                            msgs = await self.st.load_messages(sid)
                            if msgs:
                                sess.messages = msgs
                    except Exception as err:
                        print(f"加载会话 {sid} 失败: {err}")
                if not self._owner_allowed(sess.owner, owner):
                    raise SessionForbidden(sid)  # 不缓存他人的会话
                self._sessions[sid] = sess
                self._evict_locked()
            elif not self._owner_allowed(sess.owner, owner):
                raise SessionForbidden(sid)
            sess.last_used = time.monotonic()
            self._sessions.move_to_end(sid)
            return sid, sess

    def _evict_locked(self) -> None:
        """淘汰空闲超时 / 超出上限的最久未用会话（跳过正在处理中的）。调用方持有 _sessions_mu。"""
        now = time.monotonic()
        for key in list(self._sessions):  # OrderedDict：队首 = 最久未用
            sess = self._sessions[key]
            over = len(self._sessions) > self.max_sessions
            expired = now - sess.last_used > self.session_ttl
            if not (over or expired):
                break
            if sess.lock.locked():
                continue
            del self._sessions[key]

    async def _authorize_replay(self, session_id: str, identity: Optional[Identity]) -> None:
        """回放接口的归属校验：只有会话创建者可读（认证关闭时放行）。"""
        if not self.auth_enabled:
            return
        caller = identity.owner_id if identity is not None else ""
        try:
            stored = await self.st.get_owner(session_id)
        except Exception as e:
            raise HTTPException(status_code=500, detail="读取会话失败: " + str(e)) from e
        if stored is not None and not self._owner_allowed(stored, caller):
            raise HTTPException(status_code=403, detail="无权访问该会话")

    # ---------- 会话回放 ----------

    async def handle_replay_messages(
        self, session_id: str, identity: Optional[Identity] = Depends(identity_dependency)
    ) -> JSONResponse:
        """返回会话的完整消息历史（对话回放，需认证，仅创建者可读）。"""
        if self.st is None:
            raise HTTPException(status_code=501, detail="持久化未启用（store.path 未配置）")
        await self._authorize_replay(session_id, identity)
        try:
            msgs = await self.st.load_messages(session_id)
        except Exception as e:
            raise HTTPException(status_code=500, detail="读取会话失败: " + str(e)) from e
        return JSONResponse({
            "session_id": session_id,
            "messages": [m.to_dict() for m in msgs],
        })

    async def handle_replay_audit(
        self, session_id: str, limit: int = 200,
        identity: Optional[Identity] = Depends(identity_dependency),
    ) -> JSONResponse:
        """返回会话关联的审计事件（倒序，需认证，仅创建者可读）。"""
        if self.st is None:
            raise HTTPException(status_code=501, detail="持久化未启用（store.path 未配置）")
        await self._authorize_replay(session_id, identity)
        try:
            events = await self.st.replay_audit(session_id, limit)
        except Exception as e:
            raise HTTPException(status_code=500, detail="读取审计失败: " + str(e)) from e
        return JSONResponse({"session_id": session_id, "events": events})


# ---------- 辅助函数 ----------

def _drain_task_exception(t: "asyncio.Task") -> None:
    """取走后台任务异常，避免 asyncio 的 "exception was never retrieved" 警告。"""
    if not t.cancelled():
        t.exception()


def _bearer_token(request: Request) -> str:
    """从 Authorization 头提取 Bearer token。"""
    h = request.headers.get("Authorization", "")
    if len(h) > 7 and h[:7].lower() == "bearer ":
        return h[7:].strip()
    return ""


def _digest_fields(text: str) -> dict[str, Any]:
    """把需要留痕的原文换成"哈希 + 长度"，审计里不落用户输入原文。"""
    if not text:
        return {}
    return {
        "matched_sha": "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:8],
        "matched_len": len(text),
    }


def _new_trace_id() -> str:
    """生成请求级追踪 ID（16 位随机十六进制，对应 Go 版 8 字节 hex）。"""
    return secrets.token_hex(8)


def trim_history(msgs: list[Message], limit: int) -> list[Message]:
    """裁剪历史：保留末尾 limit 条。

    始终从一条 user 消息开始（避免悬空的 tool 消息作为开头导致部分 LLM 报错）。
    """
    if len(msgs) <= limit:
        return msgs
    out = msgs[len(msgs) - limit:]
    for i, m in enumerate(out):
        if m.role == "user":
            return out[i:]
    # 窗口内没有 user 消息（单轮里工具调用特别多时会发生）：若直接返回，会以孤立的
    # tool / assistant(tool_calls) 开头，下一次 LLM 请求会被 OpenAI 兼容端点 400 拒绝。
    # 退而保留"最近一条 user 消息起"的完整轮次（长度受 max_loops 约束）。
    for i in range(len(msgs) - 1, -1, -1):
        if msgs[i].role == "user":
            return msgs[i:]
    return []


def create_app(
    agent: Agent,
    cfg: Config,
    audit_logger: Optional[AuditLogger] = None,
    store: Optional[Store] = None,
) -> FastAPI:
    """创建 FastAPI 应用（uvicorn 入口）。"""
    return Server(agent, cfg, audit_logger, store).app
