"""进程内后台任务管理器(串行队列)。

把同步/采集这类长耗时操作从请求生命周期解耦:
点按钮 → 后端起后台协程 → 立即返回 task_id → 前端轮询 /api/tasks 看进度。
切页面 / F5 / 关浏览器都不中断后台运行。

串行:同一时刻只跑一个任务,其余 pending 排队,避免 TTD 单服务被并发拖垮。
参考 EntHub tasks.py 的进程内任务跟踪模式。
"""
import asyncio
import json
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional


@dataclass
class Task:
    task_id: str
    type: str                                   # update_collection / sync_account / ...
    status: str = "pending"                     # pending / running / done / failed / cancelled
    total: int = 0
    success: int = 0
    failed: int = 0
    skipped: int = 0
    log: deque = field(default_factory=lambda: deque(maxlen=200))
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    error: Optional[str] = None
    _cancel: asyncio.Event = field(default_factory=asyncio.Event, repr=False)

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "type": self.type,
            "status": self.status,
            "total": self.total,
            "success": self.success,
            "failed": self.failed,
            "skipped": self.skipped,
            "log": list(self.log),
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error": self.error,
        }

    def _save_history(self):
        """任务完成时持久化到 sync_history 表（best-effort, 不影响主流程）。"""
        try:
            from .database import Database
            db = Database()
            started = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.started_at)) if self.started_at else None
            finished = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.finished_at)) if self.finished_at else None
            dur = (self.finished_at - self.started_at) if (self.started_at and self.finished_at) else None
            db.add_sync_history({
                "task_type": self.type,
                "status": self.status,
                "total": self.total,
                "success": self.success,
                "failed": self.failed,
                "skipped": self.skipped,
                "error": self.error,
                "log_json": json.dumps(list(self.log), ensure_ascii=False),
                "started_at": started,
                "finished_at": finished,
                "duration_sec": round(dur, 1) if dur else None,
            })
        except Exception:
            pass  # 持久化失败不影响任务流程


class TaskManager:
    """进程内单例。串行队列:同时只一个任务 running。"""

    MAX_HISTORY = 20  # 完成的任务最多保留多少条

    def __init__(self):
        self._tasks: dict[str, Task] = {}
        self._order: list[str] = []          # 插入顺序
        self._lock = asyncio.Lock()          # 串行执行锁
        self._counter = 0

    def _new_id(self) -> str:
        self._counter += 1
        return f"task_{self._counter}"

    def _trim(self):
        """保留 running/pending + 最近 MAX_HISTORY 条完成的。"""
        finished = [tid for tid in self._order
                    if self._tasks[tid].status not in ("pending", "running")]
        over = len(finished) - self.MAX_HISTORY
        for tid in finished[:max(0, over)]:
            self._tasks.pop(tid, None)
            if tid in self._order:
                self._order.remove(tid)

    # ── 查询 ──────────────────────────────────────
    def get(self, task_id: str) -> Optional[Task]:
        return self._tasks.get(task_id)

    def list(self) -> list[Task]:
        """running/pending 在前,完成的按时间倒序。"""
        active = [self._tasks[tid] for tid in self._order
                  if self._tasks[tid].status in ("pending", "running")]
        done = [self._tasks[tid] for tid in reversed(self._order)
                if self._tasks[tid].status not in ("pending", "running")]
        return active + done

    # ── 创建 / 更新 ────────────────────────────────
    def create(self, task_type: str) -> Task:
        t = Task(task_id=self._new_id(), type=task_type)
        self._tasks[t.task_id] = t
        self._order.append(t.task_id)
        return t

    def add_log(self, task_id: str, message: str, level: str = "info"):
        t = self._tasks.get(task_id)
        if t:
            t.log.append({"level": level, "message": message, "ts": time.time()})

    def update(self, task_id: str, **fields):
        t = self._tasks.get(task_id)
        if not t:
            return
        for k, v in fields.items():
            if k == "status":
                t.status = v
                if v == "running" and t.started_at is None:
                    t.started_at = time.time()
                if v in ("done", "failed", "cancelled"):
                    t.finished_at = time.time()
            elif hasattr(t, k):
                setattr(t, k, v)

    # ── 取消 ──────────────────────────────────────
    def request_cancel(self, task_id: str) -> bool:
        t = self._tasks.get(task_id)
        if not t or t.status not in ("pending", "running"):
            return False
        t._cancel.set()
        return True

    def is_cancelled(self, task_id: str) -> bool:
        t = self._tasks.get(task_id)
        return bool(t and t._cancel.is_set())

    # ── 串行执行 ──────────────────────────────────
    async def run_serial(self, task: Task, coro: Callable[[Task], Awaitable]):
        """排队等锁,拿到锁后跑 coro(task)。任务循环里用 is_cancelled 检查退出。"""
        async with self._lock:
            if self.is_cancelled(task.task_id):
                self.update(task.task_id, status="cancelled")
                task._save_history()
                return
            self.update(task.task_id, status="running")
            try:
                await coro(task)
                if task.status == "running":
                    self.update(task.task_id, status="done")
            except Exception as e:
                self.update(task.task_id, status="failed", error=str(e))
            finally:
                task._save_history()
                self._trim()


_task_manager: Optional[TaskManager] = None


def get_task_manager() -> TaskManager:
    global _task_manager
    if _task_manager is None:
        _task_manager = TaskManager()
    return _task_manager
