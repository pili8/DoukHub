"""定时任务调度器 — 基于 APScheduler 的进程内 cron 调度。

职责：
1. 从数据库加载 enabled 的定时任务，注册到 APScheduler
2. 到点触发时，通过回调函数通知 main.py 执行采集
3. 启动时检查错过的任务（next_run_at < now），立即补执行
4. 任务 CRUD 后同步更新 APScheduler 中的 job

设计要点：
- 调度器本身不直接执行采集，只通过 on_trigger 回调通知外部
- 冲突处理（有采集正在运行时排队等待）由外部在回调中实现
- APScheduler 使用 AsyncIOScheduler，与 FastAPI 事件循环一致
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Callable, Awaitable, Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

logger = logging.getLogger("doukhub.scheduler")

# APScheduler job ID 前缀，便于区分定时任务和其他 job
_JOB_PREFIX = "scheduled_task_"

# 一次性任务的表达式前缀：`@once:2026-09-25 20:00`
# 为什么复用 cron_expression 字段而不是加列：不需要改表结构，且所有既有读写路径
# （增删改查、校验、展示）都能原样工作，只在调度与展示两处特判即可。
ONCE_PREFIX = "@once:"


def parse_once_at(expr: str):
    """若 expr 是一次性表达式，返回 datetime；否则返回 None。"""
    if not expr or not expr.startswith(ONCE_PREFIX):
        return None
    raw = expr[len(ONCE_PREFIX):].strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


def build_trigger(expr: str):
    """按表达式类型构建 APScheduler trigger：一次性走 DateTrigger，其余走 cron。"""
    once_at = parse_once_at(expr)
    if once_at is not None:
        return DateTrigger(run_date=once_at)
    return CronTrigger.from_crontab(expr)


class Scheduler:
    """定时任务调度器"""

    def __init__(self):
        self._scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")
        self._on_trigger: Optional[Callable[[int], Awaitable[None]]] = None
        self._started = False

    def set_trigger_callback(self, callback: Callable[[int], Awaitable[None]]):
        """设置任务触发回调函数，参数为 scheduled_task.id"""
        self._on_trigger = callback

    def start(self):
        """启动调度器"""
        if self._started:
            return
        self._scheduler.start()
        self._started = True
        logger.info("定时任务调度器已启动")

    def shutdown(self):
        """关闭调度器"""
        if not self._started:
            return
        self._scheduler.shutdown(wait=False)
        self._started = False
        logger.info("定时任务调度器已关闭")

    def reload_all(self, tasks: list[dict]):
        """从数据库全量重载定时任务到调度器。

        在启动时和任务 CRUD 后调用。
        tasks: get_scheduled_tasks() 的返回值。
        """
        # 先清除所有已有的定时任务 job
        for job in self._scheduler.get_jobs():
            if job.id.startswith(_JOB_PREFIX):
                job.remove()

        now = datetime.now()
        for task in tasks:
            if not task.get("enabled"):
                continue
            task_id = task["id"]
            cron_expr = task.get("cron_expression", "")
            if not cron_expr:
                continue
            try:
                trigger = build_trigger(cron_expr)
                next_run = trigger.get_next_fire_time(None, now)
                self._scheduler.add_job(
                    func=self._on_job_trigger,
                    trigger=trigger,
                    id=f"{_JOB_PREFIX}{task_id}",
                    args=[task_id],
                    replace_existing=True,
                    misfire_grace_time=3600,  # 1小时内的错过都允许补执行
                    coalesce=True,  # 多次错过只执行一次
                )
                logger.debug(
                    f"已注册定时任务 #{task_id} '{task.get('name', '')}' "
                    f"cron={cron_expr} next_run={next_run}"
                )
            except Exception as e:
                logger.error(
                    f"注册定时任务 #{task_id} 失败 (cron='{cron_expr}'): {e}"
                )

    def add_job_for_task(self, task_id: int, cron_expression: str, enabled: bool = True):
        """添加单个定时任务的 job"""
        if not enabled or not cron_expression:
            return
        try:
            trigger = build_trigger(cron_expression)
            self._scheduler.add_job(
                func=self._on_job_trigger,
                trigger=trigger,
                id=f"{_JOB_PREFIX}{task_id}",
                args=[task_id],
                replace_existing=True,
                misfire_grace_time=3600,
                coalesce=True,
            )
            logger.info(f"已添加定时任务 #{task_id} expr={cron_expression}")
        except Exception as e:
            logger.error(f"添加定时任务 #{task_id} 失败: {e}")

    def remove_job_for_task(self, task_id: int):
        """移除单个定时任务的 job"""
        job_id = f"{_JOB_PREFIX}{task_id}"
        try:
            self._scheduler.remove_job(job_id)
            logger.info(f"已移除定时任务 #{task_id}")
        except Exception:
            pass  # job 可能已不存在

    def get_next_run_time(self, task_id: int) -> Optional[datetime]:
        """获取指定任务的下次执行时间"""
        job = self._scheduler.get_job(f"{_JOB_PREFIX}{task_id}")
        if job and job.next_run_time:
            return job.next_run_time
        return None

    async def _on_job_trigger(self, task_id: int):
        """APScheduler job 触发时的回调"""
        logger.info(f"定时任务 #{task_id} 触发")
        if self._on_trigger:
            try:
                await self._on_trigger(task_id)
            except Exception as e:
                logger.error(f"定时任务 #{task_id} 执行失败: {e}", exc_info=True)

    def check_missed_tasks(self, tasks: list[dict]) -> list[int]:
        """检查错过的任务。

        返回需要补执行的任务 ID 列表：
        next_run_at 存在且小于当前时间、且 enabled 的任务。

        在启动时调用，检查 DoukHub 关闭期间错过的定时任务。
        """
        now = datetime.now()
        missed: list[int] = []
        for task in tasks:
            if not task.get("enabled"):
                continue
            next_run_str = task.get("next_run_at")
            if not next_run_str:
                continue
            try:
                # SQLite DATETIME 格式: "YYYY-MM-DD HH:MM:SS"
                next_run = datetime.strptime(
                    str(next_run_str)[:19], "%Y-%m-%d %H:%M:%S"
                )
                if next_run < now:
                    missed.append(task["id"])
                    logger.info(
                        f"定时任务 #{task['id']} '{task.get('name', '')}' "
                        f"错过执行时间 {next_run_str}，将补执行"
                    )
            except (ValueError, TypeError) as e:
                logger.warning(
                    f"解析 next_run_at 失败 task=#{task['id']}: {next_run_str} ({e})"
                )
        return missed


# 全局单例
_scheduler: Optional[Scheduler] = None


def get_scheduler() -> Scheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = Scheduler()
    return _scheduler
