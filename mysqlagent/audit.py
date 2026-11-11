"""mysqlagent.audit — 异步结构化审计事件记录器（JSONL 落盘）。

移植自 Go 版 internal/audit/audit.go：
  - 每条事件一行 JSON（JSONL，追加写，对齐"审计日志追加写"要求）；
  - 写入不阻塞主链路：事件投递到有界队列，由独立任务落盘；
  - 队列满时丢弃并计数（宁可丢审计不能卡业务，丢弃计数本身可观测）；
  - path 为空时回退到 stdout（本地开发模式）。

安全红线：审计只落哈希与摘要，不落原始用户输入 / SQL 原文 / 数据内容。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from typing import Any, Callable, Optional

QUEUE_SIZE = 512


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class AuditLogger:
    """异步 JSONL 审计记录器。"""

    def __init__(self, path: str = "") -> None:
        self._path = path
        self._queue: asyncio.Queue[Optional[dict[str, Any]]] = asyncio.Queue(maxsize=QUEUE_SIZE)
        self._file = None
        self._sinks: list[Callable[[dict[str, Any]], None]] = []
        self._dropped = 0
        self._closed = False
        self._task: Optional[asyncio.Task] = None
        self._drained = asyncio.Event()

    async def start(self) -> "AuditLogger":
        """打开文件（如需）并启动后台落盘任务。"""
        if self._path:
            dir_ = os.path.dirname(self._path)
            if dir_ and dir_ != ".":
                os.makedirs(dir_, exist_ok=True)
            # 0600 权限：审计文件仅属主可读写
            fd = os.open(self._path, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
            self._file = os.fdopen(fd, "a", encoding="utf-8", buffering=1)
        self._task = asyncio.create_task(self._loop())
        return self

    def log(self, e: Optional[dict[str, Any]]) -> None:
        """投递一条审计事件（自动补 ts）。非阻塞；队列满则丢弃并计数。"""
        if e is None or self._closed:
            return
        if "ts" not in e:
            e["ts"] = _utc_now_iso()
        try:
            self._queue.put_nowait(e)
        except asyncio.QueueFull:
            self._dropped += 1

    @property
    def dropped(self) -> int:
        return self._dropped

    def add_sink(self, f: Callable[[dict[str, Any]], None]) -> None:
        """注册额外的同步消费方（在落盘任务内调用，不得长时间阻塞）。

        典型用途：审计事件同步写入 SQLite，与会话消息同库便于按会话检索回放。
        """
        self._sinks.append(f)

    async def close(self) -> None:
        """停止接收并刷盘（优雅退出时调用）。"""
        if self._closed:
            if self._task is not None:
                await self._drained.wait()
            return
        self._closed = True
        if self._task is not None:
            await self._queue.put(None)  #  sentinel：排空后退出
            try:
                await asyncio.wait_for(self._task, timeout=10)
            except asyncio.TimeoutError:
                self._task.cancel()
        if self._file is not None:
            try:
                self._file.flush()
                os.fsync(self._file.fileno())
            except (OSError, ValueError):
                pass
            self._file.close()
            self._file = None

    async def _loop(self) -> None:
        try:
            while True:
                e = await self._queue.get()
                if e is None:
                    break  # sentinel：优雅退出
                self._write(e)
        finally:
            self._drained.set()

    def _write(self, e: dict[str, Any]) -> None:
        try:
            line = json.dumps(e, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            return
        if self._file is not None:
            try:
                self._file.write(line + "\n")
            except OSError:
                print(f"审计写入失败: {self._path}", file=sys.stderr)
        else:
            print(f"AUDIT {line}")
        # 额外消费方（如 SQLite 同库审计）；串行调用，失败只记日志
        for f in self._sinks:
            try:
                f(e)
            except Exception as err:  # 审计 sink 失败不能影响主审计通道
                print(f"审计 sink 执行失败: {err}", file=sys.stderr)
