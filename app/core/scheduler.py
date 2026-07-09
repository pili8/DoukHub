"""定时任务调度器 — APScheduler 集成"""
import asyncio
import logging
from datetime import datetime
from typing import Callable

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from .history import HistoryDB
from .collector import Collector, Account
from .syncer import Syncer

logger = logging.getLogger("doukhub.scheduler")


class TaskScheduler:
    """定时任务调度管理器"""

    def __init__(
        self,
        history: HistoryDB,
        get_collector: Callable,
        get_syncer: Callable,
        get_accounts: Callable,
    ):
        self.history = history
        self.get_collector = get_collector
        self.get_syncer = get_syncer
        self.get_accounts = get_accounts
        self.scheduler = AsyncIOScheduler()
        self._loaded_task_ids: set[int] = set()

    def start(self) -> None:
        """启动调度器"""
        self._load_tasks_from_db()
        self.scheduler.start()
        logger.info("定时任务调度器已启动")

    def shutdown(self) -> None:
        """停止调度器"""
        self.scheduler.shutdown(wait=False)
        logger.info("定时任务调度器已停止")

    def _load_tasks_from_db(self) -> None:
        """从数据库加载所有启用的定时任务"""
        tasks = self.history.get_tasks()
        for task in tasks:
            if task["enabled"]:
                self._add_job(task)

    def _add_job(self, task: dict) -> None:
        """添加一个定时任务到调度器"""
        task_id = task["id"]
        job_id = f"task_{task_id}"

        # 解析 cron 表达式
        try:
            parts = task["cron_expression"].strip().split()
            if len(parts) != 5:
                logger.error(f"任务 {task['name']} 的 cron 表达式格式错误: {task['cron_expression']}")
                return
            trigger = CronTrigger(
                minute=parts[0],
                hour=parts[1],
                day=parts[2],
                month=parts[3],
                day_of_week=parts[4],
            )
        except Exception as e:
            logger.error(f"任务 {task['name']} 的 cron 表达式解析失败: {e}")
            return

        # 解析评级筛选
        rating_filter = set()
        for r in task.get("rating_filter", "3,4,5").split(","):
            try:
                rating_filter.add(int(r.strip()))
            except ValueError:
                pass

        self.scheduler.add_job(
            self._execute_task,
            trigger=trigger,
            id=job_id,
            name=task["name"],
            kwargs={
                "task_id": task_id,
                "task_name": task["name"],
                "rating_filter": rating_filter,
            },
            replace_existing=True,
        )
        self._loaded_task_ids.add(task_id)
        logger.info(f"已加载定时任务: {task['name']} ({task['cron_expression']})")

    async def _execute_task(self, task_id: int, task_name: str, rating_filter: set[int]) -> None:
        """执行定时采集任务"""
        logger.info(f"开始执行定时任务: {task_name}")

        # 更新上次执行时间
        self.history.update_task(task_id, {
            "last_run_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        })

        try:
            # 读取本地账号缓存（定时任务不调飞书 API）
            s = self.get_syncer()
            if not s:
                logger.error("同步器未初始化")
                return

            accounts = s.load_local_accounts()
            # 筛选启用的 + 符合评级的账号
            accounts = [
                a for a in accounts
                if a.enabled and a.rating in rating_filter
            ]

            if not accounts:
                logger.info(f"任务 {task_name}: 没有符合条件的账号")
                return

            # 按评级排序
            accounts.sort(key=lambda a: a.rating, reverse=True)

            c = self.get_collector()
            results = await c.collect_batch(accounts)

            # 记录历史
            success_count = 0
            for r in results:
                self.history.add_record({
                    "account_name": r.account_name,
                    "platform": r.platform,
                    "works_count": r.works_count,
                    "success_count": r.works_count if r.status == "success" else 0,
                    "fail_count": 0 if r.status == "success" else 1,
                    "started_at": r.started_at,
                    "finished_at": r.finished_at,
                    "duration_seconds": r.duration,
                    "status": r.status,
                    "error_message": r.message if r.status == "failed" else "",
                })
                if r.status == "success":
                    success_count += 1

            logger.info(f"定时任务 {task_name} 完成: {success_count}/{len(results)} 成功")

        except Exception as e:
            logger.error(f"定时任务 {task_name} 执行失败: {e}")

    def reload_tasks(self) -> None:
        """重新加载所有定时任务"""
        # 移除旧任务
        for job in self.scheduler.get_jobs():
            self.scheduler.remove_job(job.id)
        self._loaded_task_ids.clear()
        # 重新加载
        self._load_tasks_from_db()

    def add_task(self, task_id: int) -> None:
        """添加新任务到调度器"""
        tasks = self.history.get_tasks()
        for task in tasks:
            if task["id"] == task_id:
                self._add_job(task)
                break

    def remove_task(self, task_id: int) -> None:
        """从调度器移除任务"""
        job_id = f"task_{task_id}"
        try:
            self.scheduler.remove_job(job_id)
            self._loaded_task_ids.discard(task_id)
        except Exception:
            pass

    def toggle_task(self, task_id: int, enabled: bool) -> None:
        """启用/禁用任务"""
        if enabled:
            self.add_task(task_id)
        else:
            self.remove_task(task_id)

    def get_jobs_info(self) -> list[dict]:
        """获取所有调度任务的信息"""
        jobs = []
        for job in self.scheduler.get_jobs():
            jobs.append({
                "id": job.id,
                "name": job.name,
                "next_run": str(job.next_run_time) if job.next_run_time else None,
            })
        return jobs
