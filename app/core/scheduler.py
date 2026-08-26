"""定时任务调度器 — APScheduler 集成"""
import asyncio
import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Callable

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from .database import Database
from .history import HistoryDB
from .collector import Collector, Account
from .syncer import Syncer
from . import storage_profiles as sp

logger = logging.getLogger("doukhub.scheduler")


class TaskScheduler:
    """定时任务调度管理器"""

    def __init__(
        self,
        history: HistoryDB,
        get_collector: Callable,
        get_syncer: Callable,
        get_accounts: Callable,
        config=None,
    ):
        self.history = history
        self.get_collector = get_collector
        self.get_syncer = get_syncer
        self.get_accounts = get_accounts
        self.config = config
        self.scheduler = AsyncIOScheduler()
        self._loaded_task_ids: set[int] = set()
        # 优先使用新数据库
        self.db = Database()

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
        # 优先从新数据库加载
        tasks = self.db.get_all_tasks()
        if not tasks:
            # 兼容旧数据库
            tasks = self.history.get_tasks()
        for task in tasks:
            enabled = task.get("启用", task.get("enabled", 0))
            if enabled:
                self._add_job(task)

    def _add_job(self, task: dict) -> None:
        """添加一个定时任务到调度器"""
        # 兼容新旧数据库字段名
        task_id = task.get("ID", task.get("id", 0))
        task_name = task.get("任务名称", task.get("name", ""))
        cron_expr = task.get("Cron表达式", task.get("cron_expression", ""))
        rating_filter_str = task.get("等级筛选", task.get("rating_filter", "3,4"))
        job_id = f"task_{task_id}"

        # 解析 cron 表达式
        try:
            parts = cron_expr.strip().split()
            if len(parts) != 5:
                logger.error(f"任务 {task_name} 的 cron 表达式格式错误: {cron_expr}")
                return
            trigger = CronTrigger(
                minute=parts[0],
                hour=parts[1],
                day=parts[2],
                month=parts[3],
                day_of_week=parts[4],
            )
        except Exception as e:
            logger.error(f"任务 {task_name} 的 cron 表达式解析失败: {e}")
            return

        # 解析评级筛选
        rating_filter = set()
        for r in rating_filter_str.split(","):
            try:
                rating_filter.add(int(r.strip()))
            except ValueError:
                pass

        self.scheduler.add_job(
            self._execute_task,
            trigger=trigger,
            id=job_id,
            name=task_name,
            kwargs={
                "task_id": task_id,
                "task_name": task_name,
                "rating_filter": rating_filter,
            },
            replace_existing=True,
        )
        self._loaded_task_ids.add(task_id)
        logger.info(f"已加载定时任务: {task_name} ({cron_expr})")

    async def _execute_task(self, task_id: int, task_name: str, rating_filter: set[int]) -> None:
        """执行定时采集任务"""
        logger.info(f"开始执行定时任务: {task_name}")

        # 更新上次执行时间（新数据库）
        self.db.update_task(task_id, {
            "上次运行": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        })

        try:
            # 接入增量存储方案：把生效方案的 folder_name/name_format 写入 TTD settings.json
            self._apply_batch_storage()

            # 从新数据库获取账号
            accounts = self.db.get_all_accounts()
            # 筛选符合等级 + 已启用采集的账号
            accounts = [
                a for a in accounts
                if a.get("等级", 0) in rating_filter
                and a.get("sec_user_id")
                and a.get("启用", 1)  # 默认启用（兼容旧数据）
            ]

            if not accounts:
                logger.info(f"任务 {task_name}: 没有符合条件的账号")
                return

            # 按等级排序
            accounts.sort(key=lambda a: a.get("等级", 0), reverse=True)

            c = self.get_collector()
            cookies = self.db.get_enabled_cookies()

            success_count = 0
            for i, account in enumerate(accounts):
                try:
                    chosen = cookies[i % len(cookies)] if cookies else {}
                    cookie = chosen.get("Cookie", "")
                    if chosen.get("record_id"):
                        self.db.record_cookie_usage(chosen["record_id"])
                    result = await c.collect_account(
                        Account(
                            name=account.get("账号名称", ""),
                            platform=account.get("平台", "douyin"),
                            sec_user_id=account.get("sec_user_id", ""),
                            collection_type=account.get("采集类型", "发布"),
                        ),
                        cookie=cookie,
                    )

                    # 记录到新数据库
                    self.db.add_history({
                        "账号名称": account.get("账号名称", ""),
                        "平台": account.get("平台", ""),
                        "sec_user_id": account.get("sec_user_id", ""),
                        "采集类型": account.get("采集类型", "发布"),
                        "等级": account.get("等级"),
                        "状态": result.status,
                        "作品数": result.works_count,
                        "开始时间": datetime.fromtimestamp(result.started_at).strftime("%Y-%m-%d %H:%M:%S") if result.started_at else None,
                        "结束时间": datetime.fromtimestamp(result.finished_at).strftime("%Y-%m-%d %H:%M:%S") if result.finished_at else None,
                        "耗时秒数": result.duration,
                        "错误信息": result.message if result.status == "failed" else "",
                    })

                    if result.status == "success":
                        success_count += 1

                except Exception as e:
                    logger.error(f"采集 {account.get('账号名称', '')} 失败: {e}")

            logger.info(f"定时任务 {task_name} 完成: {success_count}/{len(accounts)} 成功")

        except Exception as e:
            logger.error(f"定时任务 {task_name} 执行失败: {e}")

    def _apply_batch_storage(self) -> None:
        """把增量存储方案生效的 folder_name/name_format 写入 TTD settings.json。

        定时任务走 Collector 引擎（TTD HTTP API），不经过批次 runner，
        此处与批次保持一致：执行前同步存储方案，TTD 引擎按新目录落盘。
        """
        if self.config is None:
            return
        try:
            profile, diags = sp.resolve_active_profile(self.config, "batch")
            if profile is None:
                logger.warning(f"定时任务: 增量存储方案均不可用，使用 TTD 默认目录"
                               + (f"（{diags[0]['name']}: {diags[0]['reason']}）" if diags else ""))
                return
            name_format = sp.resolve_name_format(self.config, "batch", profile)
            settings_path = Path(self.config.ttd_path) / "Volume" / "settings.json"
            settings_path.parent.mkdir(parents=True, exist_ok=True)
            settings = {}
            if settings_path.exists():
                with settings_path.open("r", encoding="utf-8-sig") as f:
                    settings = json.load(f)
            changed = False
            folder_name = (profile.get("path") or "").strip()
            if folder_name and settings.get("folder_name") != folder_name:
                settings["folder_name"] = folder_name
                changed = True
            if name_format and settings.get("name_format") != name_format:
                settings["name_format"] = name_format
                changed = True
            # 引擎参数（与批次链路一致：方案内设置 → 全局默认兜底）
            engine_params = sp.resolve_engine_params(self.config, "batch", profile) or {}
            for k in sp.ENGINE_KEYS:
                if k in engine_params and settings.get(k) != engine_params[k]:
                    settings[k] = engine_params[k]
                    changed = True
            if not changed:
                return
            tmp = settings_path.with_name(f"{settings_path.name}.doukhub.tmp")
            with tmp.open("w", encoding="utf-8") as f:
                json.dump(settings, f, ensure_ascii=False, indent=4)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, settings_path)
            logger.info(f"定时任务: 已应用增量存储方案 {profile.get('name', '')} → {folder_name}")
        except Exception as exc:
            logger.error(f"定时任务: 应用存储方案失败: {exc}")

    def reload_tasks(self) -> None:
        """重新加载所有定时任务"""
        for job in self.scheduler.get_jobs():
            self.scheduler.remove_job(job.id)
        self._loaded_task_ids.clear()
        self._load_tasks_from_db()

    def add_task(self, task_id: int) -> None:
        """添加新任务到调度器"""
        tasks = self.db.get_all_tasks()
        for task in tasks:
            if task.get("ID") == task_id:
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
