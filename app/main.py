"""DoukHub 主入口 — FastAPI Web 应用"""
import asyncio
import json
import logging
import os
import secrets
import socket
import httpx
from contextlib import asynccontextmanager
from datetime import datetime, date
from pathlib import Path, PureWindowsPath
from typing import Any, Literal
from urllib.parse import quote

from fastapi import FastAPI, Request, Form, Body
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from .core.config import Config
from .core.feishu import FeishuClient
from .core.collector import Collector, Account, is_cookie_failure
from .core.cookie_pool import CookiePool
from .core.syncer import Syncer
from .core.syncer_v2 import Syncer as SyncerV2
from .core.database import Database
from .core.collection_batch_manager import CollectionBatchManager
from .core.collection_planner import plan_collection
from .core.feishu_sync import FeishuSyncer
from .core.history import HistoryDB
from .core.tasks import get_task_manager
from .core.link_resolver import extract_sec_user_id, build_profile_url
from .core import single_work
from .core import storage_profiles as sp
from .core.download_worker import DownloadWorker
from .core import backup
from .core import dedup
from .core import maintenance
from .core import presets
from .core.scheduler import get_scheduler
from .services.downloader import ServiceManager
from .core.data_migration import DataMigration
from .core.data_root import app_data_root, validate_target

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
logger = logging.getLogger("doukhub")

BASE_DIR = Path(__file__).resolve().parent
config = Config()

# 全局实例
feishu_client: FeishuClient | None = None
collector: Collector | None = None
syncer: Syncer | None = None
syncer_v2: SyncerV2 | None = None
database: Database | None = None
history: HistoryDB | None = None
services: ServiceManager | None = None
collection_batch_manager: CollectionBatchManager | None = None
download_worker: DownloadWorker | None = None
single_work_client: httpx.AsyncClient | None = None
data_migration = DataMigration()


def get_feishu() -> FeishuClient | None:
    global feishu_client
    cfg = config.feishu
    if cfg.get("app_id") and cfg.get("app_secret"):
        if feishu_client is None:
            feishu_client = FeishuClient(cfg["app_id"], cfg["app_secret"])
        return feishu_client
    return None


def get_collector() -> Collector:
    global collector
    if collector is None:
        collector = Collector(
            ttd_url=f"http://127.0.0.1:{config.ttd_port}",
            xhs_url=f"http://127.0.0.1:{config.xhs_port}",
            cookie_mode=config.cookie_config.get("rotation_mode", "random"),
            cookie_usage_limit=config.cookie_config.get("usage_limit", 10),
        )
    return collector


def get_database() -> Database:
    global database
    if database is None:
        database = Database()
    return database



def _migration_busy_reason() -> str:
    """迁移开始前的活动检查：有未完成的后台任务/采集批次/单作品下载时拒绝。"""
    if any(t.status in ("pending", "running") for t in get_task_manager().list()):
        return "后台任务未完成"
    if get_database().get_active_collection_batch():
        return "增量采集批次未完成"
    if get_database().has_pending_or_running_single_work():
        return "单作品下载未完成"
    return ""


def _parse_preset_date(value: str) -> date | None:
    """将 'YYYY-MM-DD' 字符串转为 date，空或无效返回 None。"""
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except (ValueError, TypeError):
        return None


def get_collection_batch_manager() -> CollectionBatchManager:
    global collection_batch_manager
    if collection_batch_manager is None:
        collection_batch_manager = CollectionBatchManager(
            database=get_database(),
            ttd_path=Path(config.ttd_path),
            log_dir=config.app_data_dir / "collection_logs",
            ttd_url=f"http://127.0.0.1:{config.ttd_port}",
        )
    return collection_batch_manager

def get_download_worker() -> DownloadWorker:
    global download_worker
    if download_worker is None:
        download_worker = DownloadWorker(
            db=get_database(),
            client=get_single_work_client(),
            ttd_url=f"http://127.0.0.1:{config.ttd_port}",
        )
        # 单作品优先：下载/解析期间冻结批量子进程，结束自动恢复
        _bm = get_collection_batch_manager()
        download_worker.freeze_hooks = (_bm.suspend_for_single_work, _bm.resume_after_single_work)
    return download_worker


def get_single_work_client() -> httpx.AsyncClient:
    global single_work_client
    if single_work_client is None:
        # trust_env=False：调 TTD 走的是本机回环，不能被系统代理改写路径（会 404）。
        # 注：若将来要挂代理下载海外平台（TikTok），这一处需单独放开。
        single_work_client = httpx.AsyncClient(
            timeout=300,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0"},
            trust_env=False,
        )
    return single_work_client


def get_cookie_list() -> list[str]:
    """获取可用 Cookie 列表，优先从数据库读取，回退到 TTD settings.json。

    数据库中启用的 Cookie 可能为空（用户未启用或批量采集误标记失效），
    此时会回退到 TTD settings.json 中的 cookie / cookie_tiktok 字段。
    """
    db = get_database()
    cookies = db.get_enabled_cookies()
    cookie_list = [ck.get("Cookie", "") for ck in cookies if ck.get("Cookie")]
    if cookie_list:
        return cookie_list
    # Fallback: TTD settings.json
    try:
        ttd_root = Path(config.ttd_path)
        settings_file = ttd_root / "Volume" / "settings.json"
        if settings_file.exists():
            with settings_file.open("r", encoding="utf-8-sig") as f:
                settings = json.load(f)
            cookie = settings.get("cookie", "")
            if cookie:
                return [cookie]
    except Exception:
        pass
    return []


def get_feishu_syncer() -> FeishuSyncer | None:
    """获取飞书同步器"""
    f = get_feishu()
    if f and config.feishu.get("app_token"):
        return FeishuSyncer(f, config.feishu)
    return None


def get_syncer() -> Syncer | None:
    global syncer
    f = get_feishu()
    if f and config.feishu.get("app_token") and config.feishu.get("collection_table_id"):
        if syncer is None:
            syncer = Syncer(
                feishu=f,
                collector=get_collector(),
                app_token=config.feishu["app_token"],
                collection_table_id=config.feishu.get("collection_table_id", ""),
                account_table_id=config.feishu.get("account_table_id", ""),
                cookie_table_id=config.feishu.get("cookie_table_id", ""),
                data_dir=config.data_dir,
            )
        return syncer
    return None


def get_syncer_v2() -> SyncerV2:
    """始终返回实例。三步同步只用本地 DB + TTD API，不依赖飞书。
    飞书仅在云端同步时使用，未配置时 feishu=None，不影响三步同步。"""
    global syncer_v2
    if syncer_v2 is None:
        f = get_feishu()
        syncer_v2 = SyncerV2(
            feishu=f,
            collector=get_collector(),
            config=config.feishu,
            tags_mapping=config._data.get("tags", {}),
        )
    return syncer_v2


def get_history() -> HistoryDB:
    global history
    if history is None:
        history = HistoryDB(config.app_data_dir)
    return history


def get_services() -> ServiceManager:
    global services
    if services is None:
        services = ServiceManager(
            ttd_path=config.ttd_path,
            ttd_port=config.ttd_port,
            xhs_path=config.xhs_path,
            xhs_port=config.xhs_port,
        )
    return services


# 定时任务「上次仍在跑」的判定宽限期。
# 超过这个时长仍停在 running，视为进程崩溃留下的僵尸状态，放行下一次触发；
# 否则该任务会被永久锁死，再也跑不起来。
# 取 12 小时：远大于单次采集的合理耗时，又能当天自愈。
SCHEDULE_STALE_HOURS = 12

# 任务历史（sync_history）保留天数。启动时清理一次。
# 为什么给 30 天而不是默认的 7 天：定时任务现在"成功也写历史"，
# 记录量比原来大一个量级，7 天会让用户想复盘上周的执行节奏时查不到。
SYNC_HISTORY_KEEP_DAYS = 30

# 服务启动事件的最小留痕间隔（分钟）。
# 托盘在改 .py 时会自动重启，开发期一次会话可能重启十几次 ——
# 不去重的话「服务启动」会把历史刷屏，真正有价值的"停机多久"反而看不见。
SERVICE_START_DEDUPE_MINUTES = 5


def _running_stale_minutes(last_run_at) -> float | None:
    """返回上次触发距今的分钟数；解析失败返回 None（视为"不久之前"，走保守的跳过分支）。"""
    if not last_run_at:
        return None
    try:
        started = datetime.strptime(str(last_run_at)[:19], "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return None
    return (datetime.now() - started).total_seconds() / 60


async def _on_scheduled_task_trigger(
    task_id: int, ignore_enabled: bool = False, trigger: str = "schedule"
):
    """定时任务触发回调：执行关联的采集方案。

    ignore_enabled=True 供「立即执行」使用：已禁用的任务也允许手动跑一次。
    trigger：触发来源，写进任务历史的 trigger_source ——
             "manual" 手动立即执行 / "schedule" 定时触发 / "catchup" 启动时补执行。

    冲突处理：如果当前有采集正在运行，排队等待（通过 CollectionBatchManager
    内部的队列机制自动处理 — start() 会在有活跃批次时抛 RuntimeError，
    此处捕获后延迟重试）。
    """
    db = get_database()
    task = db.get_scheduled_task(task_id)
    if not task or (not task["enabled"] and not ignore_enabled):
        logger.info(f"定时任务 #{task_id} 已禁用或不存在，跳过")
        return

    # ===== 任务堆叠保护 =====
    # 上一次触发还停在 running（说明采集没结束），本次直接跳过，不排队、不重试。
    # 为什么不是"排队等上一次"：定时任务的价值在于"按点跑一次"，迟到的第 N 次没有意义，
    # 反而会和正常调度叠在一起把设备压满。手动「立即执行」是有意触发，不受此限制。
    if task.get("last_run_status") == "running" and not ignore_enabled:
        stale_min = _running_stale_minutes(task.get("last_run_at"))
        if stale_min is not None and stale_min > SCHEDULE_STALE_HOURS * 60:
            logger.warning(
                f"定时任务 #{task_id} 上次执行已卡住 {int(stale_min)} 分钟，判定为僵尸状态，本次放行"
            )
        else:
            logger.info(f"定时任务 #{task_id} 上一次仍在执行中，跳过本次触发")
            db.update_scheduled_task(task_id, {"last_run_status": "skipped_busy"})
            return

    logger.info(f"定时任务 #{task_id} '{task['name']}' 开始触发")

    # 更新状态为执行中
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    db.update_scheduled_task(task_id, {
        "last_run_at": now_str,
        "last_run_status": "running",
    })

    try:
        # 执行前同步（如果配置了；总闸关闭时跳过）
        if task["sync_before"]:
            fs = get_feishu_syncer()
            if fs and config.get("feishu.auto_sync", True):
                try:
                    logger.info(f"定时任务 #{task_id}: 执行前同步云端...")
                    fs.sync_incremental(trigger="sync_before")
                except Exception as e:
                    logger.warning(f"定时任务 #{task_id}: 执行前同步失败（继续执行）: {e}")

        # 读取关联的采集方案
        preset = presets.get_preset(config, task["preset_id"])
        if not preset:
            raise ValueError(f"采集方案 #{task['preset_id']} 不存在")

        # 检查服务是否可用
        # ⚠️ 只处理「内核已安装」的服务：未安装的内核（如尚未接入的小红书）永远不可能就绪，
        # 拉它只会白等满整轮等待，没有任何意义 —— 直接排除在等待集合之外。
        svc = get_services()
        needed = [s for s in svc.services if s.source_exists]
        for s in needed:
            if not s.is_running:
                logger.warning(f"定时任务 #{task_id}: 服务 {s.name} 未运行，尝试启动...")
                s.start()

        # 等待服务就绪（最多等 30 秒）
        # ⚠️ is_running 是 HTTP 探测（客户端超时 3 秒），未就绪时每轮实际耗时 ≥3 秒，
        # 30 轮就是 ~90 秒。排除未安装的内核后，这段等待才真的只等"该等的服务"。
        import time as _time
        for _ in range(30):
            if all(s.is_running for s in needed):
                break
            _time.sleep(1)

        # 执行采集（复用现有批次链路）
        manager = get_collection_batch_manager()
        accounts = db.get_all_accounts()

        # 存储方案
        defaults = config.collection_defaults
        folder_name = preset.get("folder_name", "")
        name_format = preset.get("name_format", "")
        storage_primary_id = preset.get("storage_primary_id") or ""
        storage_secondary_id = preset.get("storage_secondary_id") or ""
        legacy_choice = str(preset.get("storage_choice") or "")
        if not storage_primary_id and legacy_choice.startswith("p:"):
            storage_primary_id = legacy_choice[2:]

        root_path = ""
        active_profile = None
        if not folder_name:
            active_profile, _ = sp.resolve_pair(
                config, "batch", storage_primary_id, storage_secondary_id
            )
            if active_profile is not None:
                root_path = active_profile["path"] or ""
        if not root_path and folder_name and Path(folder_name).is_absolute():
            root_path = folder_name
            folder_name = ""
        if not folder_name:
            folder_name = defaults.get("folder_name", "Download")
        if not name_format:
            name_format = sp.resolve_name_format(config, "batch", active_profile)
            if not name_format:
                name_format = defaults.get("name_format", "create_time type nickname desc")
        engine_params = sp.resolve_engine_params(config, "batch", active_profile)
        if engine_params is None:
            engine_params = {
                k: defaults.get(k) for k in
                ("folder_mode", "music", "dynamic_cover", "static_cover", "max_size", "storage_format", "max_pages")
                if k in defaults
            }

        platforms = (
            ("douyin", "tiktok") if preset["platform"] == "all" else (preset["platform"],)
        )
        tags = preset["tags"].split(",") if preset["tags"] else []

        # 重试逻辑：如果当前有活跃批次，等待后重试
        max_retries = 12  # 最多等 12 次 × 5 分钟 = 1 小时
        for attempt in range(max_retries + 1):
            try:
                batches = await manager.start(
                    accounts=accounts,
                    rating_min=preset["rating_min"],
                    tags=tags,
                    account_names=preset.get("account_names", ""),
                    platforms=platforms,
                    mode=preset["mode"],
                    preset_name=preset["name"],
                    folder_name=folder_name,
                    root_path=root_path,
                    name_format=name_format,
                    account_created_after=preset.get("account_created_after", ""),
                    skip_recent_days=int(preset.get("skip_recent_days", 0)),
                    engine_params=engine_params,
                    scheduled_task_id=task_id,
                    preset_id=task["preset_id"],
                )
                logger.info(f"定时任务 #{task_id}: 采集已启动，批次 {len(batches)} 个")
                # 更新状态为成功（采集已启动，不等完成）
                db.update_scheduled_task(task_id, {
                    "last_run_status": "success",
                    "last_batch_ids": ",".join(b["id"] for b in batches),
                })
                # 成功也写一条任务历史：历史才会从「失败清单」变成「执行日志」
                _record_schedule_run(
                    task_id, task, "success",
                    batch_ids=[b["id"] for b in batches],
                    trigger=trigger,
                )
                # 一次性任务：本次是它唯一一次触发，跑完即自动停用并撤掉 job，
                # 避免在列表里留一个永远不再触发的任务让用户困惑。
                if task.get("cron_expression", "").startswith("@once:"):
                    db.update_scheduled_task(task_id, {"enabled": 0})
                    get_scheduler().remove_job_for_task(task_id)
                    logger.info(f"定时任务 #{task_id}: 一次性任务已完成，自动停用")
                break
            except RuntimeError as e:
                if "已有采集批次正在执行" in str(e) and attempt < max_retries:
                    logger.info(
                        f"定时任务 #{task_id}: 当前有采集正在运行，"
                        f"等待 5 分钟后重试（第 {attempt + 1}/{max_retries} 次）"
                    )
                    await asyncio.sleep(300)
                    continue
                raise
        else:
            raise RuntimeError("等待超时，无法启动采集")

    except Exception as e:
        logger.error(f"定时任务 #{task_id} 执行失败: {e}", exc_info=True)
        db.update_scheduled_task(task_id, {
            "last_run_status": "failed",
            "last_batch_ids": "",
        })
        _record_schedule_run(task_id, task, "failed", error=e, trigger=trigger)


def _record_schedule_run(
    task_id: int,
    task: dict,
    status: str,
    error: Exception | None = None,
    batch_ids: list[str] | None = None,
    trigger: str = "schedule",
) -> None:
    """定时任务每次执行都留痕（成功也写），供「失败通知」与「任务历史」共用。

    复用 sync_history 表（task_type='scheduled_task'），不新增表：
    该表已有 status / error / created_at，足够表达「哪个任务、什么时候、结果如何」。
    失败记录在用户点开后端任务面板时被标记为已读（见 /api/schedules/failures/ack）。

    ⚠️ 成功也要写：原先只在失败时写，历史就退化成「失败清单」——
       用户问"今天 12:10 到底跑没跑"在历史里查不到，也没法复盘执行节奏。

    trigger：本次是谁触发的（manual / schedule / catchup），写入 trigger_source。
    batch_ids：本次拉起的采集批次，第一条写进 ref_id，前端据此直达批次详情。
    """
    try:
        db = get_database()
        ok = status == "success"
        ids = [b for b in (batch_ids or []) if b]
        db.add_sync_history({
            "task_type": "scheduled_task",
            "status": "done" if ok else "failed",
            "total": 1,
            "success": 1 if ok else 0,
            "failed": 0 if ok else 1,
            "skipped": 0,
            # error 里带上任务名，前端无需再查一次任务表（形如「任务名#12：具体错误」）
            "error": "" if ok else f"{task.get('name', '')}#{task_id}：{error}",
            "started_at": task.get("last_run_at") or "",
            "finished_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            # 只记第一条批次：一个任务一次触发通常只拉 1 个批次（平台=全部 时才有 2 个）
            "ref_id": ids[0] if ids else "",
            "trigger_source": trigger,
        })
    except Exception as e:  # 通知失败绝不能再抛，否则会盖掉原始错误
        logger.warning(f"记录定时任务执行历史时出错: {e}")


def _record_system_event(
    task_type: str,
    status: str = "done",
    message: str = "",
    trigger: str = "startup",
    dedupe_minutes: int = 0,
) -> None:
    """记录一条「系统事件」历史（服务启停、每日备份等），让这些动作在任务历史里留痕。

    原先这些事件完全无痕：想知道"服务几点起来的、中间断了多久"只能去翻 server.log。
    best-effort：任何异常一律吞掉，绝不能影响启动流程。
    dedupe_minutes：距上一条同类型记录不足该分钟数则跳过。
    """
    try:
        db = get_database()
        now = datetime.now()
        if dedupe_minutes > 0:
            latest = db.get_sync_history(task_type, limit=1)
            prev = (latest[0].get("finished_at") if latest else "") or ""
            if prev:
                try:
                    gap = now - datetime.strptime(str(prev)[:19], "%Y-%m-%d %H:%M:%S")
                    if 0 <= gap.total_seconds() < dedupe_minutes * 60:
                        return
                except ValueError:
                    pass  # 解析不了就不去重 —— 宁可多一条，也别把真事件吞掉
        db.add_sync_history({
            "task_type": task_type,
            "status": status,
            "total": 0,
            "success": 0,
            "failed": 0,
            "skipped": 0,
            "error": message,
            "started_at": "",
            "finished_at": now.strftime("%Y-%m-%d %H:%M:%S"),
            "trigger_source": trigger,
        })
    except Exception as e:
        logger.warning(f"记录系统事件历史失败（{task_type}）: {e}")


def _auto_sync_loop():
    """自动同步后台线程（Syncthing 模式）：

    - 本地变更即时同步：指纹（MAX(local_updated_at)+行数）变化 → 去抖 60 秒 → 触发
    - 云端变更兜底轮询：每 poll_minutes 分钟查一次飞书「修改时间」最大值，有变 → 触发
    - 总闸 feishu.auto_sync=False 时完全空转（纯本地模式）
    """
    import time as _t
    _t.sleep(120)  # 等启动同步跑完、服务稳定
    last_fp = None
    last_feishu_max = None
    last_feishu_check = 0.0
    dirty_since = None
    while True:
        try:
            if not config.get("feishu.auto_sync", True):
                last_fp = None
                dirty_since = None
                _t.sleep(30)
                continue
            fs = get_feishu_syncer()
            if not fs:
                _t.sleep(30)
                continue

            now = _t.time()
            fp = get_database().get_sync_fingerprint()

            if last_fp is None:
                # 首轮只建立基线，不触发
                last_fp = fp
                last_feishu_check = now
                _t.sleep(15)
                continue

            if fp != last_fp:
                if dirty_since is None:
                    dirty_since = now
                if now - dirty_since >= 60:
                    logger.info("检测到本地数据变更，自动增量同步开始...")
                    fs.sync_incremental(trigger="auto_local")
                    last_fp = get_database().get_sync_fingerprint()
                    dirty_since = None
                    last_feishu_check = now
                    last_feishu_max = None
                    logger.info("本地变更自动同步完成")
            else:
                dirty_since = None
                interval_min = max(10, min(240, int(config.get("feishu.poll_minutes", 30) or 30)))
                if now - last_feishu_check >= interval_min * 60:
                    last_feishu_check = now
                    mx = fs.get_feishu_max_modified()
                    if mx and mx != last_feishu_max:
                        if last_feishu_max is not None:
                            logger.info("检测到云端数据变更，自动增量同步开始...")
                            fs.sync_incremental(trigger="auto_remote")
                            last_fp = get_database().get_sync_fingerprint()
                            logger.info("云端变更自动同步完成")
                        last_feishu_max = mx
            _t.sleep(15)
        except Exception as e:
            logger.warning(f"自动同步循环异常（不影响使用，60 秒后重试）: {e}")
            _t.sleep(60)


@asynccontextmanager
async def lifespan(app: FastAPI):
    import threading

    # 收尸：上次进程中断遗留的 running 云同步记录（托盘重启拦腰砍断）标记为中断
    try:
        n = get_database().fail_stale_running_sync()
        if n:
            logger.warning(f"已将 {n} 条遗留 running 状态的云同步记录标记为中断")
    except Exception as e:
        logger.warning(f"清理遗留同步记录失败: {e}")

    # 后台启动 Downloader 服务（不阻塞 UI）
    svc = get_services()
    if config.downloader.get("auto_start_services", True):
        logger.info("正在后台启动 Downloader 服务...")
        threading.Thread(target=svc.start_all, daemon=True).start()
    # TTD/XHS 心跳监控：30秒检查一次，连续2次失败自动重启（可通过 keep_services_alive 开关关闭）
    def _health_loop():
        import time as _t
        _t.sleep(15)
        while True:
            try:
                if config.keep_services_alive:
                    _svc = get_services()
                    for _s in _svc.services:
                        _s.health_check()
            except Exception:
                pass
            _t.sleep(30)
    threading.Thread(target=_health_loop, daemon=True).start()

    # 启动后自动增量同步（不阻塞 UI）
    fs = get_feishu_syncer()
    if fs and config.get("feishu.auto_sync", True):
        def _bg_sync():
            try:
                logger.info("启动时自动增量同步开始...")
                fs.sync_incremental(trigger="startup")
                logger.info("启动时自动增量账号处理完成")
            except Exception as e:
                logger.warning(f"启动时自动增量账号处理失败（不影响使用）: {e}")
        threading.Thread(target=_bg_sync, daemon=True).start()

    # 自动同步循环：本地变更即时 + 云端兜底轮询（受 feishu.auto_sync 总闸控制）
    threading.Thread(target=_auto_sync_loop, daemon=True).start()

    _batch_manager = get_collection_batch_manager()
    _batch_manager.recover_interrupted_batches()
    await _batch_manager.kick_resume()
    get_download_worker().recover()

    # 启动时检查每日备份（距离上次超过 24 小时则自动备份）
    try:
        _bkp = backup.check_daily_backup()
        if _bkp.get("success"):
            logger.info(f"启动时自动备份：{_bkp.get('filename')}")
            _record_system_event(
                "backup", "done",
                message=f"自动备份：{_bkp.get('filename')}",
                trigger="daily",
            )
    except Exception as _e:
        logger.warning(f"启动备份检查失败（不影响使用）: {_e}")

    # 备份定时检查：每小时看一次
    # （原先只在容器启动时检查一次 → 容器长期不重启就永远不会再自动备份）
    def _backup_loop():
        import time as _t
        _t.sleep(120)
        while True:
            try:
                _b = backup.check_daily_backup()
                if _b.get("success"):
                    logger.info(f"定时自动备份：{_b.get('filename')}")
                    _record_system_event(
                        "backup", "done",
                        message=f"自动备份：{_b.get('filename')}",
                        trigger="daily",
                    )
            except Exception as _be:
                logger.warning(f"定时备份检查异常（不影响使用）: {_be}")
            _t.sleep(3600)
    threading.Thread(target=_backup_loop, daemon=True).start()

    # 初始化文件查重的回收区目录
    try:
        dedup.set_recycle_dir(config.get("dedup.recycle_dir", ""))
    except Exception as _e:
        logger.warning(f"回收区目录初始化失败（使用默认）: {_e}")

    # 启动定时任务调度器
    try:
        sched = get_scheduler()
        sched.set_trigger_callback(_on_scheduled_task_trigger)
        sched.start()
        # 全量加载定时任务到调度器
        _db = get_database()
        all_tasks = _db.get_scheduled_tasks()
        sched.reload_all(all_tasks)
        logger.info(f"已加载 {len(all_tasks)} 个定时任务到调度器")
        # 检查错过的任务，补执行
        missed_ids = sched.check_missed_tasks(all_tasks)
        for mid in missed_ids:
            logger.info(f"补执行错过的定时任务 #{mid}")
            asyncio.create_task(_on_scheduled_task_trigger(mid, trigger="catchup"))
    except Exception as _e:
        logger.error(f"定时任务调度器启动失败: {_e}", exc_info=True)

    # 任务历史清理：cleanup_sync_history 早就定义了，却从来没被调用过 → 历史只增不删
    try:
        _removed = get_database().cleanup_sync_history(days=SYNC_HISTORY_KEEP_DAYS)
        if _removed:
            logger.info(f"已清理 {_removed} 条超过 {SYNC_HISTORY_KEEP_DAYS} 天的任务历史")
    except Exception as _e:
        logger.warning(f"任务历史清理失败（不影响使用）: {_e}")

    # 服务启动留痕：这样才回答得了"服务几点起的、中间断了多久"（原先只能翻 server.log）
    _record_system_event(
        "service_start", "done",
        trigger="startup",
        dedupe_minutes=SERVICE_START_DEDUPE_MINUTES,
    )

    yield

    # 关闭
    global single_work_client
    # 关闭定时任务调度器
    try:
        get_scheduler().shutdown()
    except Exception:
        pass
    await get_collection_batch_manager().shutdown()
    svc.close()
    c = get_collector()
    if c:
        await c.close()
    if single_work_client:
        await single_work_client.aclose()
        single_work_client = None


app = FastAPI(title="DoukHub", version="2.4.2", lifespan=lifespan)



@app.middleware("http")
async def migration_lock_middleware(request: Request, call_next):
    path = request.url.path
    if data_migration.snapshot()["locked"] and not (
        path.startswith("/api/data-migration")
        or path == "/api/system/restart"
        or path.startswith("/static/")
        or not path.startswith("/api/")
    ):
        return JSONResponse(
            {"success": False, "message": "应用数据正在迁移，请等待完成"},
            status_code=503,
        )
    return await call_next(request)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


# ========== 工具函数 ==========

def detect_platform(link: str) -> str:
    """根据链接识别平台"""
    if "douyin.com" in link or "iesdouyin.com" in link:
        return "douyin"
    elif "tiktok.com" in link:
        return "tiktok"
    elif "xiaohongshu.com" in link or "xhslink.com" in link or "rednote.com" in link:
        return "xhs"
    return ""


def _extract_single_work_links(text: str) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    seen: set[str] = set()
    for link in single_work.URL.findall(text or ""):
        # 标准化：去掉尾部斜杠和查询参数用于去重比较
        normalized = link.rstrip("/").split("?")[0]
        if normalized in seen:
            continue
        seen.add(normalized)
        platform = single_work.detect_single_platform(link)
        if platform:
            result.append((link, platform))
    return result


def _is_unsafe_filename_template(template: str) -> bool:
    if any(char in template for char in ("/", "\\", ":")) or any(
        ord(char) < 32 for char in template
    ):
        return True
    rendered = template.format(
        create_time="2026-08-15 10-00-00",
        author="作者",
        title="标题",
        id="123",
        type="视频",
        platform="douyin",
    )
    path = PureWindowsPath(rendered)
    return (
        path.is_absolute()
        or any(part in ("", ".", "..") for part in path.parts)
    )


def _sync_workflow_stats(db: Database) -> dict[str, int]:
    """用高级 API 统计，兼容 mock 数据库"""
    collections = db.get_all_collections()
    accounts = db.get_all_accounts()
    cookies = db.get_enabled_cookies()
    collections_total = len(collections)
    pending_resolve = sum(
        1 for c in collections
        if c.get("解析状态") in ("待解析", "解析失败")
        and str(c.get("share_code", "")).strip()
    )
    ready_accounts = sum(1 for c in collections if c.get("解析状态") == "已就绪")
    accounts_total = len(accounts)
    account_sec_ids = {a.get("sec_user_id") for a in accounts if a.get("sec_user_id")}
    ready_to_sync = sum(
        1 for c in collections
        if c.get("sec_user_id") and c["sec_user_id"] not in account_sec_ids
    )
    pending_refresh = sum(
        1 for a in accounts
        if a.get("sec_user_id") and a.get("获取状态") in ("待获取", "获取失败")
    )
    return {
        "collections_total": collections_total,
        "pending_resolve": pending_resolve,
        "ready_accounts": ready_accounts,
        "ready_to_sync": ready_to_sync,
        "accounts_total": accounts_total,
        "pending_refresh": pending_refresh,
        "cookies": len(cookies),
    }


def _sync_recent_results(db: Database) -> tuple[dict[str, dict], dict[str, str]]:
    task_types = {
        "import_collection": "导入分享表",
        "update_collection": "解析分享表",
        "sync_account": "生成账号表",
        "refresh_accounts": "更新账号表",
    }
    histories = {}
    for task_type in task_types:
        items = db.get_sync_history(task_type, limit=1)
        histories[task_type] = items[0] if items else None
    return histories, task_types


def _account_health(db: Database) -> dict:
    """账号健康度：综合解析状态 + 采集成败 + Cookie 可用性，零新表零新字段。

    返回 {
        "accounts": [{ name, platform, fetch_status, success, failed,
                       last_status, last_message, level, level_label }],
        "summary": { total, healthy, attention, abnormal, uncollected },
        "cookie": { total, healthy, disabled },
    }
    level: healthy 健康 / attention 需关注 / abnormal 异常 / uncollected 未采集
    """
    accounts = db.get_all_accounts()
    collect = {s["sec_user_id"]: s for s in db.get_account_collection_stats()}
    cookies = db.get_all_cookies()

    rows = []
    for acc in accounts:
        sec = acc.get("sec_user_id") or ""
        st = collect.get(sec) or {}
        fetch_status = acc.get("获取状态") or "待获取"
        success = st.get("success") or 0
        failed = st.get("failed") or 0
        last_status = st.get("last_status") or ""
        last_message = st.get("last_message") or ""

        # 健康判定：解析失败优先 → 未采集 → 按成败分布
        total = success + failed
        if fetch_status == "获取失败":
            level, label = "abnormal", "解析失败"
        elif total == 0:
            level, label = "uncollected", "未采集"
        elif failed == 0:
            level, label = "healthy", "健康"
        elif success == 0:
            level, label = "abnormal", "从未成功"
        else:
            level, label = "attention", "部分失败"

        rows.append({
            "name": acc.get("账号名称") or acc.get("sec_user_id") or "-",
            "sec_user_id": sec,
            "platform": acc.get("平台") or "",
            "fetch_status": fetch_status,
            "success": success,
            "failed": failed,
            "last_status": last_status,
            "last_message": last_message,
            "level": level,
            "level_label": label,
        })

    order = {"abnormal": 0, "attention": 1, "uncollected": 2, "healthy": 3}
    rows.sort(key=lambda r: (order.get(r["level"], 9), r["name"]))

    summary = {"total": len(rows), "healthy": 0, "attention": 0, "abnormal": 0, "uncollected": 0}
    for r in rows:
        summary[r["level"]] += 1
    cookie_ok = sum(1 for c in cookies if c.get("启用"))
    cookie_disabled = sum(1 for c in cookies if not c.get("启用"))
    return {
        "accounts": rows,
        "summary": summary,
        "cookie": {"total": len(cookies), "healthy": cookie_ok, "disabled": cookie_disabled},
    }

def _sync_overview_context(db: Database) -> dict:
    histories, labels = _sync_recent_results(db)
    return {
        "stats": _sync_workflow_stats(db),
        "recent_histories": histories,
        "recent_labels": labels,
        "account_health": _account_health(db),
    }


# ========== 页面路由 ==========

@app.get("/", response_class=HTMLResponse)
async def page_sync_overview_redirect(request: Request):
    """根路径重定向到账号状态页"""
    db = get_database()
    return templates.TemplateResponse(request, "sync/overview.html", context={
        "request": request,
        "page": "sync_overview",
        **_sync_overview_context(db),
    })


@app.get("/sync", response_class=HTMLResponse)
async def page_sync_overview_redirect2(request: Request):
    """旧 /sync 重定向到 /sync/overview"""
    db = get_database()
    return templates.TemplateResponse(request, "sync/overview.html", context={
        "request": request,
        "page": "sync_overview",
        **_sync_overview_context(db),
    })


@app.get("/sync/overview", response_class=HTMLResponse)
async def page_sync_overview(request: Request):
    """账号状态页 - 判断下一步应该执行什么"""
    db = get_database()
    return templates.TemplateResponse(request, "sync/overview.html", context={
        "request": request,
        "page": "sync_overview",
        **_sync_overview_context(db),
    })


@app.get("/sync/import", response_class=HTMLResponse)
async def page_sync_import(request: Request):
    """导入分享表页面"""
    db = get_database()
    history = db.get_sync_history("import_collection", limit=20)
    return templates.TemplateResponse(request, "sync/import.html", context={
        "request": request,
        "history": history,
        "page": "sync_import",
    })


@app.get("/sync/resolve", response_class=HTMLResponse)
async def page_sync_resolve(request: Request):
    """解析账号标识页面"""
    db = get_database()
    history = db.get_sync_history("update_collection", limit=20)
    return templates.TemplateResponse(request, "sync/resolve.html", context={
        "request": request,
        "history": history,
        "stats": _sync_workflow_stats(db),
        "page": "sync_resolve",
    })


@app.get("/sync/account", response_class=HTMLResponse)
async def page_sync_account(request: Request):
    """生成账号表页面"""
    db = get_database()
    history = db.get_sync_history("sync_account", limit=20)
    return templates.TemplateResponse(request, "sync/account.html", context={
        "request": request,
        "history": history,
        "stats": _sync_workflow_stats(db),
        "page": "sync_account",
    })


@app.get("/sync/refresh", response_class=HTMLResponse)
async def page_sync_refresh(request: Request):
    """刷新账号资料页面"""
    db = get_database()
    history = db.get_sync_history("refresh_accounts", limit=20)
    return templates.TemplateResponse(request, "sync/refresh.html", context={
        "request": request,
        "history": history,
        "stats": _sync_workflow_stats(db),
        "page": "sync_refresh",
    })


@app.get("/sync/cloud", response_class=HTMLResponse)
async def page_sync_cloud(request: Request):
    """云端同步页面"""
    return RedirectResponse("/database", status_code=307)


@app.get("/status", response_class=HTMLResponse)
async def page_status(request: Request):
    """状态页面 - 服务状态和连通性检测"""
    return templates.TemplateResponse(request, "status.html", context={
        "request": request,
        "page": "status",
    })


@app.get("/database", response_class=HTMLResponse)
async def page_database(request: Request):
    """数据管理页面 - 概览 + 云端同步"""
    return templates.TemplateResponse(request, "database.html", context={
        "request": request,
        "page": "database",
    })


@app.get("/table", response_class=HTMLResponse)
async def page_table(request: Request):
    """表浏览页面 - 独立的数据表管理"""
    import json as _json
    return templates.TemplateResponse(request, "table.html", context={
        "request": request,
        "page": "table",
        "tags_mapping_json": _json.dumps(config._data.get("tags", {}), ensure_ascii=False),
    })


@app.get("/backup", response_class=HTMLResponse)
async def page_backup(request: Request):
    """数据备份页面"""
    return templates.TemplateResponse(request, "backup.html", context={
        "request": request,
        "page": "backup",
    })


@app.post("/api/backup/create")
async def api_backup_create():
    """创建数据库备份"""
    result = backup.create_backup(reason="手动备份")
    if result["success"]:
        backup.cleanup_old_backups()
        _record_system_event(
            "backup", "done",
            message=f"手动备份：{result.get('filename')}",
            trigger="manual",
        )
    return result


@app.get("/api/backup/list")
async def api_backup_list():
    """列出所有备份"""
    return {
        "backups": backup.list_backups(),
        "backup_dir": str(backup.get_backup_dir()),
        # 供前端决定是否显示「恢复默认」：目录是自定义的才需要
        "backup_dir_custom": bool(str(config.get("backup.dir", "") or "").strip()),
    }


@app.post("/api/backup/restore")
async def api_backup_restore(payload: dict):
    """从备份恢复数据库"""
    filename = payload.get("filename", "")
    if not filename:
        return {"success": False, "error": "缺少 filename"}
    return backup.restore_backup(filename)


@app.post("/api/backup/delete")
async def api_backup_delete(payload: dict):
    """删除指定备份"""
    filename = payload.get("filename", "")
    if not filename:
        return {"success": False, "error": "缺少 filename"}
    return backup.delete_backup(filename)


@app.post("/api/backup/vacuum")
async def api_backup_vacuum():
    """压缩数据库，回收空间"""
    return backup.vacuum_database()


@app.get("/api/backup/stats")
async def api_backup_stats():
    """数据库统计信息"""
    return backup.get_db_stats()


@app.get("/api/maintenance/items")
async def api_maintenance_items():
    """返回所有可清理项（日志/缓存），供通用清理面板展示"""
    root = BASE_DIR.parent
    items = maintenance.list_items(config, root)
    return {"items": items}


@app.post("/api/maintenance/clean")
async def api_maintenance_clean(payload: dict):
    """按项清理日志/缓存，payload: {"items": [id, ...]} 或 {"item": id}"""
    root = BASE_DIR.parent
    targets = payload.get("items") or ([payload.get("item")] if payload.get("item") else [])
    results = {}
    total_freed = 0
    for item_id in targets:
        r = maintenance.clean_item(item_id, config, root)
        results[item_id] = r
        total_freed += r.get("freed_bytes", 0)
    return {"success": True, "results": results, "total_freed": total_freed}


@app.get("/api/backup/download/{filename}")
async def api_backup_download(filename: str):
    """下载备份文件"""
    backup_dir = backup.get_backup_dir()
    filepath = backup_dir / filename
    try:
        if not filepath.resolve().is_relative_to(backup_dir.resolve()):
            return JSONResponse({"success": False, "error": "非法路径"})
    except AttributeError:
        if backup_dir.resolve() not in filepath.resolve().parents:
            return JSONResponse({"success": False, "error": "非法路径"})
    if not filepath.exists():
        return JSONResponse({"success": False, "error": "文件不存在"})
    return FileResponse(str(filepath), filename=filename, media_type="application/octet-stream")


@app.post("/api/backup/open-dir")
async def api_backup_open_dir():
    """打开备份目录"""
    import subprocess
    import sys
    import os
    backup_dir = backup.get_backup_dir()
    backup_dir.mkdir(parents=True, exist_ok=True)
    try:
        if sys.platform == "win32":
            os.startfile(str(backup_dir))
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(backup_dir)])
        else:
            subprocess.Popen(["xdg-open", str(backup_dir)])
        return {"success": True}
    except Exception as e:
        return {"success": False, "error": str(e)}


@app.post("/api/backup/set-dir")
async def api_backup_set_dir(payload: dict):
    """设置备份目录；dir 传空字符串 = 恢复默认（数据根目录下的 backups/）。

    校验必须是绝对路径，并实际创建 + 试写，避免保存一个根本写不进去的目录，
    等到自动备份时才失败。切换后旧目录里的备份不自动搬移（跨盘移动可能很慢），
    但会在消息里说明旧目录还剩几份，避免用户以为备份丢了。
    """
    raw = str(payload.get("dir", "") or "").strip()
    old_dir = backup.get_backup_dir()
    old_count = len(backup.list_backups())  # 必须改配置前统计，它读的是"当前"目录

    if raw:
        target = Path(raw).expanduser()
        if not target.is_absolute():
            return {"success": False, "message": "请填写绝对路径，例如 D:\\Backups\\DoukHub"}
        try:
            target.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            return {"success": False, "message": f"目录无法创建：{e}"}
        if not target.is_dir():
            return {"success": False, "message": "该路径不是文件夹"}
        probe = target / ".doukhub_write_probe"
        try:
            probe.write_text("ok", encoding="utf-8")
        except Exception as e:
            return {"success": False, "message": f"目录不可写：{e}"}
        finally:
            try:
                probe.unlink()
            except OSError:
                pass

    config.set("backup.dir", raw)
    config.save()

    new_dir = backup.get_backup_dir()
    message = "已恢复默认备份目录" if not raw else "备份目录已更新"
    if str(new_dir) != str(old_dir) and old_count:
        message += f"；旧目录里还有 {old_count} 份备份（未自动搬移）：{old_dir}"
    return {"success": True, "message": message, "backup_dir": str(new_dir)}


@app.get("/dedup", response_class=HTMLResponse)
async def page_dedup(request: Request):
    """文件查重页面"""
    return templates.TemplateResponse(request, "dedup.html", context={
        "request": request,
        "page": "dedup",
    })


@app.get("/api/dedup/scope")
async def api_dedup_scope():
    """返回可扫描的存储方案（含方案名/路径/主次角色）。"""
    state = sp.ensure_migrated(config)
    profiles = []
    for scope in ("single", "batch"):
        for p in sp.get_profiles(state.get(scope)):
            path = (p.get("path") or "").strip()
            if not path:
                continue
            profiles.append({
                "id": p.get("id", ""),
                "name": p.get("name", "") or "未命名方案",
                "path": path,
                "scope": scope,
                "role": p.get("role", ""),
            })
    return {"success": True, "profiles": profiles}


@app.post("/api/dedup/scan")
async def api_dedup_scan(payload: dict):
    """启动后台扫描。"""
    roots = payload.get("roots") or []
    exts = payload.get("exts")
    return dedup.start_scan(roots, exts)


@app.get("/api/dedup/status")
async def api_dedup_status():
    """扫描进度与状态。"""
    return {"success": True, **dedup.get_scan_state()}


@app.get("/api/dedup/result")
async def api_dedup_result():
    """上次扫描结果。"""
    result = dedup.get_result()
    return {"success": result is not None, "result": result}


@app.post("/api/dedup/move")
async def api_dedup_move(payload: dict):
    """把选中文件移动到回收区。"""
    paths = payload.get("paths") or []
    return dedup.move_to_recycle(paths)


@app.get("/api/dedup/recycle")
async def api_dedup_recycle():
    """回收区文件列表。"""
    return {
        "success": True,
        "items": dedup.list_recycle(),
        "recycle_dir": str(dedup.get_recycle_dir()),
    }


@app.post("/api/dedup/restore")
async def api_dedup_restore(payload: dict):
    """从回收区还原。"""
    paths = payload.get("paths") or []
    return dedup.restore_from_recycle(paths)


@app.post("/api/dedup/delete")
async def api_dedup_delete(payload: dict):
    """清理回收区（返回引导，不直接删）。"""
    paths = payload.get("paths") or []
    return dedup.delete_from_recycle(paths)


@app.post("/api/dedup/open-recycle")
async def api_dedup_open_recycle():
    """打开回收区文件夹。"""
    import subprocess
    import sys
    import os
    recycle = dedup.get_recycle_dir()
    recycle.mkdir(parents=True, exist_ok=True)
    try:
        if sys.platform == "win32":
            os.startfile(str(recycle))
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(recycle)])
        else:
            subprocess.Popen(["xdg-open", str(recycle)])
        return {"success": True}
    except Exception as e:
        return {"success": False, "error": str(e)}


@app.get("/api/dedup/settings")
async def api_dedup_settings():
    """查重设置（当前回收区目录）。"""
    return {"success": True, "recycle_dir": str(dedup.get_recycle_dir())}


@app.post("/api/dedup/settings")
async def api_dedup_save_settings(payload: dict):
    """保存查重设置（回收区目录）。"""
    path = payload.get("recycle_dir", "")
    effective = dedup.set_recycle_dir(path)
    config.set("dedup.recycle_dir", str(effective))
    config.save()
    return {"success": True, "recycle_dir": str(effective)}


@app.get("/collect", response_class=HTMLResponse)
async def page_collect(request: Request):
    if request.query_params.get("mode") == "detail":
        return RedirectResponse("/collect/detail", status_code=307)

    return templates.TemplateResponse(request, "collect.html", context={
        "request": request,
        "page": "collect_account",
        "collect_mode": "account",
    })


@app.get("/collect/overview", response_class=HTMLResponse)
async def page_collect_overview(request: Request):
    db = get_database()
    batches = db.list_collection_batches(limit=5)
    # 老批次统计列为 NULL（改造前入库）：按 collection_works + 日志实时回填，
    # 避免"明明采过却显示 -"。新批次结算时已写入，直接用。
    for b in batches:
        if b.get("works_downloaded") is None:
            stats = db.get_batch_work_stats(b["id"], str(b.get("log_path") or ""))
            b.update(stats)
    return templates.TemplateResponse(request, "collect/overview.html", context={
        "request": request,
        "batches": batches,
        "latest_batch": batches[0] if batches else None,
        "cookies": len(db.get_enabled_cookies()),
        "accounts_total": len(db.get_all_accounts()),
        "page": "collect_overview",
    })


@app.get("/collect/detail", response_class=HTMLResponse)
async def page_collect_detail(request: Request):
    return templates.TemplateResponse(request, "collect_detail.html", context={
        "request": request,
        "download_path": str(config.download_path),
        "single_work_preferences": _single_work_preferences(),
        "page": "collect_detail",
        "collect_mode": "detail",
    })



@app.get("/settings", response_class=HTMLResponse)
async def page_settings(request: Request):
    return templates.TemplateResponse(request, "settings.html", context={
        "request": request,
        "config": config._data,
        "config_path": str(config._path),
        "app_data_dir": str(config.app_data_dir),
        "page": "settings",
    })


@app.get("/duplicates", response_class=HTMLResponse)
async def page_duplicates(request: Request):
    """重复账号处理页面"""
    return templates.TemplateResponse(request, "duplicates.html", context={
        "request": request,
        "page": "duplicates",
    })


@app.get("/schedule", response_class=HTMLResponse)
async def page_schedule(request: Request):
    """定时任务页面"""
    return templates.TemplateResponse(request, "schedule.html", context={
        "request": request,
        "page": "schedule",
    })


# ========== API 路由 ==========

# --- 服务管理 ---

@app.post("/api/services/{name}/start")
async def api_service_start(name: str):
    svc = get_services()
    s = svc.get_service(name)
    if s:
        return s.start()
    return {"success": False, "message": f"未找到服务: {name}"}


@app.post("/api/services/{name}/stop")
async def api_service_stop(name: str):
    svc = get_services()
    s = svc.get_service(name)
    if s:
        return s.stop()
    return {"success": False, "message": f"未找到服务: {name}"}



@app.post("/api/services/{name}/update")
async def api_service_update(name: str):
    """更新指定 Downloader 源代码"""
    svc = get_services()
    return svc.update(name)

@app.post("/api/services/update-all")
async def api_services_update_all():
    """更新所有 Downloader 源代码"""
    return {"results": get_services().update_all()}

@app.get("/api/services/versions")
async def api_services_versions():
    """获取版本信息"""
    return {"versions": get_services().get_versions()}


@app.get("/api/services/status")
async def api_services_status():
    """获取所有 Downloader 服务状态"""
    return {"services": get_services().status_all()}


# --- 状态检测 ---

@app.get("/api/status")
async def api_status():
    """获取系统整体状态"""
    svc = get_services()
    feishu = get_feishu()
    collector = get_collector()
    h = get_history()

    # 检测飞书连通性
    feishu_status = {"connected": False, "message": "云端未配置"}
    if feishu:
        try:
            result = feishu.test_connection()
            feishu_status = {
                "connected": result.get("success", False),
                "message": result.get("message", ""),
            }
        except Exception as e:
            feishu_status = {"connected": False, "message": str(e)}

    # 检测 TTD/XHS 连通性:并发执行,避免串行 5s+5s 阻塞
    ttd_kernel = svc.ttd.source_exists
    xhs_kernel = svc.xhs.source_exists

    async def _check_service(url: str) -> dict:
        """探测单个 downloader 服务是否在线"""
        try:
            async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
                resp = await client.get(url)
                if resp.status_code in (200, 307, 404):
                    return {"connected": True, "message": "运行中"}
                return {"connected": False, "message": f"HTTP {resp.status_code}"}
        except Exception as e:
            return {"connected": False, "message": str(e)}

    async def _ttd_task():
        return {"connected": False, "message": "内核未安装"} if not ttd_kernel \
            else await _check_service(f"{collector.ttd_url}/")

    async def _xhs_task():
        return {"connected": False, "message": "内核未安装"} if not xhs_kernel \
            else await _check_service(f"{collector.xhs_url}/")

    ttd_status, xhs_status = await asyncio.gather(_ttd_task(), _xhs_task())

    return {
        "feishu": feishu_status,
        "ttd": ttd_status,
        "xhs": xhs_status,
        "services": svc.status_all(),
        "stats": h.get_stats(),
        "kernels": {
            "ttd": {"installed": ttd_kernel, "path": str(svc.ttd.path), "version": svc.ttd.get_version()},
            "xhs": {"installed": xhs_kernel, "path": str(svc.xhs.path), "version": svc.xhs.get_version()},
        },
    }


@app.post("/api/status/test/feishu")
async def api_test_feishu_status():
    """测试云端 API 连通性"""
    feishu = get_feishu()
    if not feishu:
        return {"success": False, "message": "云端未配置"}
    try:
        result = feishu.test_connection()
        return {
            "success": result.get("success", False),
            "message": result.get("message", ""),
        }
    except Exception as e:
        return {"success": False, "message": str(e)}


@app.post("/api/status/test/ttd")
async def api_test_ttd_status():
    """测试 TTD 连通性"""
    collector = get_collector()
    try:
        import httpx
        async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
            resp = await client.get(f"{collector.ttd_url}/docs")
            if resp.status_code == 200:
                return {"success": True, "message": "TTD API 可用"}
            return {"success": False, "message": f"HTTP {resp.status_code}"}
    except Exception as e:
        return {"success": False, "message": str(e)}


@app.post("/api/status/test/xhs")
async def api_test_xhs_status():
    """测试 XHS 连通性"""
    collector = get_collector()
    try:
        import httpx
        async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
            resp = await client.get(f"{collector.xhs_url}/")
            if resp.status_code in (200, 307, 404):
                return {"success": True, "message": "XHS API 可用"}
            return {"success": False, "message": f"HTTP {resp.status_code}"}
    except Exception as e:
        return {"success": False, "message": str(e)}




# --- 内核管理 ---

@app.get("/api/kernels/status")
async def api_kernels_status():
    """检测内核源码是否存在"""
    svc = get_services()
    return {
        "kernels": [
            {
                "name": s.name,
                "installed": s.source_exists,
                "path": str(s.path),
                "repo_url": s.repo_url,
            }
            for s in svc.services
        ]
    }


@app.post("/api/kernels/{name}/install")
async def api_kernel_install(name: str):
    """从 GitHub 下载内核源码"""
    svc = get_services()
    s = svc.get_service(name)
    if not s:
        return {"success": False, "message": f"未找到内核: {name}"}
    if s.source_exists:
        return {"success": True, "message": f"{s.name} 已安装"}
    result = svc.install(name)
    return result

@app.post("/api/kernels/{name}/update")
async def api_kernel_update(name: str):
    """通过 git pull 更新内核源码"""
    svc = get_services()
    result = svc.update(name)
    return result

@app.get("/api/kernels/{name}/version")
async def api_kernel_version(name: str):
    """获取内核版本信息"""
    svc = get_services()
    s = svc.get_service(name)
    if not s:
        return {"success": False, "message": f"未找到内核: {name}"}
    version = s.get_version()
    return {"success": True, "name": s.name, "version": version, "path": str(s.path)}

# --- Cookie 验证 ---

@app.post("/api/cookies/validate")
async def api_validate_cookies():
    """验证所有 Cookie 有效性 - SSE 实时进度"""
    db = get_database()
    c = get_collector()

    import json

    async def validate_stream():
        try:
            cookies = db.get_all_cookies()
            total = len(cookies)
            valid_count = 0
            invalid_count = 0

            yield f"data: {json.dumps({'type': 'start', 'message': '开始验证', 'total': total})}\n\n"

            if total == 0:
                yield f"data: {json.dumps({'type': 'complete', 'success': True, 'message': '没有 Cookie 可验证', 'total': 0, 'valid': 0, 'invalid': 0})}\n\n"
                return

            for i, ck in enumerate(cookies):
                cookie_str = ck.get("Cookie", "")
                record_id = ck.get("record_id", "")
                label = ck.get("备注", "") or (cookie_str[:24] + "..." if cookie_str else "空Cookie")

                yield f"data: {json.dumps({'type': 'progress', 'message': f'验证 [{i+1}/{total}]: {label}'})}\n\n"

                result = await c.validate_cookie(cookie_str, ck.get("平台", "douyin"))
                valid = isinstance(result, dict) and result.get("status") == "valid"

                from datetime import datetime
                now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                update_data = {
                    "状态": "正常" if valid else "失效",
                    "验证时间": now_str,
                }
                if not valid:
                    update_data["启用"] = 0
                    # 验证原因写入"验证说明"字段，不覆盖用户的"备注"
                    reason = result.get("message", "") if isinstance(result, dict) else ""
                    if reason:
                        update_data["验证说明"] = reason[:80]
                else:
                    # 验证通过时清空验证说明
                    update_data["验证说明"] = ""
                db.update_cookie(record_id, update_data)

                if valid:
                    valid_count += 1
                else:
                    invalid_count += 1

                level = "ok" if valid else "error"
                icon = "✅" if valid else "❌"
                detail_msg = ""
                if not valid and isinstance(result, dict) and result.get("message"):
                    detail_msg = f" ({result['message']})"
                status = "有效" if valid else "已过期"
                msg = f"{icon} {label}: {status}{detail_msg}"
                yield f"data: {json.dumps({'type': 'log', 'level': level, 'message': msg})}\n\n"

                yield f"data: {json.dumps({'type': 'stats', 'total': total, 'valid': valid_count, 'invalid': invalid_count})}\n\n"

            summary = f"验证完成: {valid_count} 个有效, {invalid_count} 个失效"
            yield f"data: {json.dumps({'type': 'complete', 'success': True, 'message': summary, 'total': total, 'valid': valid_count, 'invalid': invalid_count})}\n\n"

        except Exception as e:
            logger.error(f"Cookie 验证失败: {e}")
            yield f"data: {json.dumps({'type': 'complete', 'success': False, 'message': f'验证失败: {str(e)}', 'total': 0, 'valid': 0, 'invalid': 0})}\n\n"

    return StreamingResponse(
        validate_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# --- 飞书同步 ---

from fastapi.responses import StreamingResponse


@app.post("/api/sync")
async def api_sync():
    """第一阶段：快速同步（只解析短链接，不获取账号信息）- SSE 实时进度"""
    s = get_syncer()
    if not s:
        return JSONResponse(
            {"success": False, "message": "云端未配置，请先在设置中填写云端信息"},
            status_code=400,
        )
    # 同步前自动检测并创建缺失字段（三张表都检查）
    f = get_feishu()
    if f:
        app_token = config.feishu.get("app_token", "")
        for table_type, table_id_key in [
            ("account", "account_table_id"),
            ("collection", "collection_table_id"),
            ("cookie", "cookie_table_id"),
        ]:
            table_id = config.feishu.get(table_id_key, "")
            if table_id:
                try:
                    f.ensure_fields(app_token, table_id, table_type=table_type)
                except Exception:
                    pass
    
    import json
    import asyncio
    
    async def sync_stream():
        """SSE 流式同步，实时返回进度"""
        try:
            # 发送开始事件
            yield f"data: {json.dumps({'type': 'start', 'message': '开始处理'})}\n\n"
            
            # 1. 读取分享表
            yield f"data: {json.dumps({'type': 'progress', 'message': '连接飞书...'})}\n\n"
            records = s.feishu.get_all_records(s.app_token, s.collection_table_id)
            total = len(records)
            
            yield f"data: {json.dumps({'type': 'stats', 'total': total, 'success': 0, 'api_calls': 0, 'failed': 0})}\n\n"
            yield f"data: {json.dumps({'type': 'progress', 'message': f'读取分享表: {total} 条记录'})}\n\n"
            
            # 2. 解析记录
            from .core.syncer import _parse_collection_record
            entries = [_parse_collection_record(r) for r in records]
            entries = [e for e in entries if e.get("link")]
            
            # 3. 获取 Cookie 池
            cookies = s.load_local_cookies()
            active_cookies = [c["cookie"] for c in cookies if c.get("enabled", True) and c.get("status", "正常") == "正常"]
            
            # 4. 加载已有账号
            existing_accounts = {}
            if s.accounts_file and s.accounts_file.exists():
                for acc in s.load_local_accounts():
                    if acc.sec_user_id:
                        existing_accounts[acc.sec_user_id] = acc
            
            # 5. 逐条处理
            new_count = 0
            updated_count = 0
            api_calls = 0
            failed = 0
            errors = []
            
            for i, entry in enumerate(entries):
                link = entry["link"]
                rating = entry.get("rating", 3)
                record_id = entry["record_id"]
                
                yield f"data: {json.dumps({'type': 'progress', 'message': f'处理 [{i+1}/{len(entries)}]: {link}'})}\n\n"
                
                try:
                    # 补全短链接前缀
                    if link and not link.startswith("http"):
                        link = f"https://v.douyin.com/{link}"
                    
                    # 平台识别
                    from .core.link_resolver import detect_platform, extract_sec_user_id, build_profile_url
                    platform = entry.get("platform", "") or detect_platform(link)
                    
                    # 解析短链接
                    cookie = active_cookies[0] if active_cookies else ""
                    resolved_url = await s.collector.resolve_short_url(link, platform)
                    api_calls += 1
                    
                    # 提取 sec_user_id
                    sec_user_id = extract_sec_user_id(resolved_url, platform)
                    
                    if not sec_user_id:
                        s._update_collection_status(record_id, "失败", "无法解析短链接")
                        errors.append(f"{link}: 无法解析")
                        failed += 1
                        yield f"data: {json.dumps({'type': 'log', 'level': 'error', 'message': f'❌ {link}: 无法解析'})}\n\n"
                        continue
                    
                    # 检查是否已有账号
                    existing_account = existing_accounts.get(sec_user_id)
                    
                    if existing_account:
                        account = existing_account
                        account.link = resolved_url or link
                        account.rating = rating
                        account.tags = entry.get("tags", [])
                        account.note = entry.get("note", "")
                        from datetime import datetime
                        account.synced_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        updated_count += 1
                    else:
                        from .core.collector import Account
                        from datetime import datetime
                        account = Account(
                            name=entry.get("name", ""),
                            platform=platform,
                            link=resolved_url or link,
                            rating=rating,
                            tags=entry.get("tags", []),
                            note=entry.get("note", ""),
                            sec_user_id=sec_user_id,
                            synced_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                            enabled=True,
                            info_fetched=False,
                        )
                        new_count += 1
                        existing_accounts[sec_user_id] = account
                    
                    # 更新统计
                    yield f"data: {json.dumps({'type': 'stats', 'total': total, 'success': new_count + updated_count, 'api_calls': api_calls, 'failed': failed})}\n\n"
                    yield f"data: {json.dumps({'type': 'log', 'level': 'ok', 'message': f'✅ [{i+1}/{len(entries)}] {account.name or sec_user_id}'})}\n\n"
                    
                    # 更新分享表状态
                    s._update_collection_status(record_id, "已就绪", "", sec_user_id)  # 旧 syncer 兼容
                    
                    # 让出控制权，避免阻塞
                    await asyncio.sleep(0)
                    
                except Exception as e:
                    s._update_collection_status(record_id, "失败", str(e))
                    errors.append(f"{link}: {e}")
                    failed += 1
                    yield f"data: {json.dumps({'type': 'log', 'level': 'error', 'message': f'❌ [{i+1}/{len(entries)}] {link}: {e}'})}\n\n"
            
            # 6. 保存本地账号缓存
            yield f"data: {json.dumps({'type': 'progress', 'message': '保存本地账号缓存...'})}\n\n"
            s._save_local_xlsx(list(existing_accounts.values()))
            
            # 7. 写入飞书账号表
            if s.account_table_id:
                yield f"data: {json.dumps({'type': 'progress', 'message': '写入飞书账号表...'})}\n\n"
                from .core.syncer import SyncResult
                result = SyncResult()
                result.api_calls = api_calls
                s._sync_to_feishu_account_table(list(existing_accounts.values()), result)
            
            # 发送完成事件
            yield f"data: {json.dumps({'type': 'complete', 'success': True, 'message': f'账号处理完成: 新增 {new_count}, 更新 {updated_count}', 'total': total, 'new_count': new_count, 'updated_count': updated_count, 'api_calls': api_calls, 'error_count': failed, 'errors': errors[:5]})}\n\n"
            
        except Exception as e:
            logger.error(f"账号处理失败: {e}")
            yield f"data: {json.dumps({'type': 'complete', 'success': False, 'message': f'账号处理失败: {str(e)}', 'total': 0, 'new_count': 0, 'updated_count': 0, 'api_calls': 0, 'error_count': 1, 'errors': [str(e)]})}\n\n"
    
    return StreamingResponse(
        sync_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/sync/fetch-info")
async def api_sync_fetch_info():
    """第二阶段：获取账号详细信息"""
    s = get_syncer()
    if not s:
        return JSONResponse(
            {"success": False, "message": "飞书未配置"},
            status_code=400,
        )
    
    try:
        result = await s.fetch_account_info()
        return {
            "success": result.success,
            "message": result.message,
            "total": result.total,
            "updated_count": result.updated_accounts,
            "api_calls": result.api_calls,
            "error_count": len(result.errors),
            "errors": result.errors,
        }
    except Exception as e:
        logger.error(f"获取账号信息失败: {e}")
        return JSONResponse(
            {"success": False, "message": f"获取异常: {str(e)}"},
            status_code=500,
        )


# ========== 新同步器 API（使用数据库） ==========
MAX_INLINE_SYNC_ITEMS = 100
# 连续失败达到此值时触发一轮长冷却后自动重试（只统计服务层失败，死链不计入）
MAX_CONSECUTIVE_TTD_FAILURES = 5
# 长冷却后最多自动重试几轮（每轮重新从 0 开始计连续失败）
MAX_TTD_RETRY_ROUNDS = 2
# 长冷却基数：第 n 轮冷却 = 基数 × n，保持递增（15s → 30s）
TTD_RETRY_COOLDOWN_BASE_SECONDS = 15
# 每次失败后的线性退避基数（第 n 次失败等 n × 此值秒 → 2/4/6/8/10）
TTD_BACKOFF_BASE_SECONDS = 2
# 解析终态映射：解析结果类型 → 表里的「解析状态」取值
# 终态 = 已退出流程，不再被重试（区别于循环态的「解析失败」）
TERMINAL_STATUS = {
    "bad_link": "链接失效",      # 分享码残缺 / 账号注销跳首页
    "not_profile": "非主页链接",  # 有效链接，但指向作品或直播，提不出 sec_user_id
    "unsupported": "暂不支持",    # 平台不在解析范围内（如小红书）
}


def _ttd_backoff_seconds(consecutive_failures: int) -> float:
    """线性退避：第 1 次失败等 5s，第 2 次等 10s，… 第 5 次等 25s。"""
    return consecutive_failures * TTD_BACKOFF_BASE_SECONDS


async def _handle_ttd_failures(tm, task_id, consecutive_failures, remaining, retry_round):
    """处理连续失败：退避 → 判定是否进入长冷却自动重试 → 最终终止。

    返回 True 表示应中止整个任务（已耗尽重试轮次），返回 False 表示继续循环。
    """
    backoff = _ttd_backoff_seconds(consecutive_failures)
    tm.add_log(
        task_id,
        f"… TTD 连续失败 {consecutive_failures} 次，退避 {backoff:.0f}s 后继续",
        "warning",
    )
    await asyncio.sleep(backoff)

    if consecutive_failures < MAX_CONSECUTIVE_TTD_FAILURES:
        return False  # 还没到阈值，继续循环

    # 达到阈值，看还有没有重试轮次
    if retry_round < MAX_TTD_RETRY_ROUNDS and remaining > 0:
        # 冷却递增：第 1 轮 15s、第 2 轮 30s……避免每次一刀切等同样久
        cooldown = TTD_RETRY_COOLDOWN_BASE_SECONDS * (retry_round + 1)
        tm.add_log(
            task_id,
            f"⚠ 连续失败 {consecutive_failures} 次，进入第 {retry_round + 1}/{MAX_TTD_RETRY_ROUNDS} 轮冷却 "
            f"({cooldown}s)，剩余 {remaining} 条将自动重试",
            "warning",
        )
        await asyncio.sleep(cooldown)
        return False  # 交由调用方重置计数器并继续

    # 重试轮次已耗尽或没有剩余记录，彻底终止
    error = (
        f"TTD 连续失败 {consecutive_failures} 次，已重试 {retry_round} 轮，已中止"
        if retry_round > 0
        else f"TTD 连续失败 {consecutive_failures} 次，已中止"
    )
    tm.add_log(
        task_id,
        f"X 连续失败 {consecutive_failures} 次，剩余 {remaining} 条暂缓；请检查 TTD 后重试",
        "error",
    )
    tm.update(task_id, status="failed", error=error)
    return True


COOKIE_FAILURE_THRESHOLD = 2

def _cookie_is_dead(cookie_failures: dict, cookie: str) -> bool:
    """同一 Cookie 连续失败达到阈值即视为本轮失效，拉黑不再使用。"""
    return cookie_failures.get(cookie, 0) >= COOKIE_FAILURE_THRESHOLD

async def _fetch_account_info(col, sec_user_id, platform, cookie_list, cookie_failures, start_index, tm, task_id):
    """获取账号资料；Cookie 失效就换下一个。

    以前是 `cookie_list[i % len]` 硬轮换，一个过期 Cookie 照样被反复使用，
    整批账号全挂还不换人。现在：

    - 同一个 Cookie 连续失败 ``COOKIE_FAILURE_THRESHOLD`` 次才拉黑
      （单个异常账号也会让所有 Cookie 返回空，一次失败就拉黑会误伤好 Cookie）
    - 每次调用按 ``start_index`` 起轮换，保证不会只盯着一两个 Cookie 用
    - 返回空 dict 表示「本轮可用 Cookie 都没取到资料」
    """
    n = len(cookie_list)
    for step in range(n):
        cookie = cookie_list[(start_index + step) % n]
        if _cookie_is_dead(cookie_failures, cookie):
            continue
        info = await col.get_account_info(sec_user_id, platform, cookie)
        if info and info.get("nickname"):
            return info
        if not is_cookie_failure(info):
            return info  # 不是 Cookie 的毛病，换了也没用
        cookie_failures[cookie] = cookie_failures.get(cookie, 0) + 1
        if _cookie_is_dead(cookie_failures, cookie):
            dead = sum(1 for c in cookie_list if _cookie_is_dead(cookie_failures, c))
            tm.add_log(task_id, f"… Cookie 连续失效，已拉黑（{dead}/{n}）", "warning")
    return {}


@app.post("/api/sync/v2/import")
async def api_sync_v2_import(request: Request):
    """步骤1：导入分享表（使用新同步器）"""
    s = get_syncer_v2()
    if not s:
        return JSONResponse(
            {"success": False, "message": "飞书未配置"},
            status_code=400,
        )
    
    try:
        data = await request.json()
        text = data.get("text", "")
        
        result = s.import_to_collection(text)
        import_logs = [{"level": "info", "message": "开始导入分享表"}]
        import_logs.extend({"level": "error", "message": err} for err in result.errors)
        import_logs.extend({"level": "warning", "message": warning} for warning in result.warnings)
        detail = (
            f"写入 {result.success} 条"
            f"（新增 {result.created}，更新 {result.updated}），"
            f"重复 {result.duplicates} 条，失败 {result.failed} 条，跳过 {result.skipped} 条"
        )
        summary = f"完成: {detail}"
        import_logs.append({
            "level": "ok" if result.failed == 0 else "error",
            "message": summary,
        })
        # 保存到同步历史
        db = get_database()
        db.add_sync_history({
            "task_type": "import_collection",
            "status": "done" if result.failed == 0 else "failed",
            "total": result.total,
            "success": result.success,
            "failed": result.failed,
            "skipped": result.skipped,
            "log_json": json.dumps(import_logs, ensure_ascii=False),
        })
        return {
            "message": f"导入完成: {detail}",
            **result.to_dict(),
            "success": result.failed == 0,
            "success_count": result.success,
        }
    except Exception as e:
        logger.error(f"导入失败: {e}")
        return JSONResponse(
            {"success": False, "message": f"导入异常: {str(e)}"},
            status_code=500,
        )


@app.post("/api/sync/v2/update-collection")
async def api_sync_v2_update_collection():
    """步骤2:更新分享表(获取 sec_user_id) - 后台任务,立即返回 task_id"""
    s = get_syncer_v2()
    if not s:
        return JSONResponse({"success": False, "message": "飞书未配置"}, status_code=400)
    tm = get_task_manager()
    task = tm.create("update_collection")
    asyncio.create_task(tm.run_serial(task, _run_update_collection))
    return {"task_id": task.task_id, "status": "pending", "message": "已加入队列"}


async def _run_update_collection(task):
    """后台执行:解析账号标识(走 TTD API 拿 sec_user_id)。串行队列内运行。"""
    import json
    tm = get_task_manager()
    s = get_syncer_v2()
    if not s:
        tm.add_log(task.task_id, "飞书未配置", "error")
        tm.update(task.task_id, status="failed", error="飞书未配置")
        return
    tm.add_log(task.task_id, "开始更新分享表", "info")
    # TTD 预检(避免逐条等 30s 超时)
    ttd_available = False
    try:
        async with httpx.AsyncClient(timeout=3, trust_env=False) as client:
            resp = await client.get(f"{s.collector.ttd_url}/")
            if resp.status_code in (200, 307, 404):
                ttd_available = True
    except Exception:
        ttd_available = False
    if not ttd_available:
        tm.add_log(task.task_id, "TTD 服务未运行,无法解析短链接", "error")
        tm.update(task.task_id, status="failed", error="TTD 服务未运行")
        return
    collections = s.db.get_all_collections()
    to_process = [c for c in collections if c.get("解析状态") in ("待解析", "解析失败") and str(c.get("share_code", "")).strip()]
    tm.update(task.task_id, total=len(to_process))
    if not to_process:
        tm.add_log(task.task_id, "没有需要解析的记录（所有分享记录解析状态为已就绪/已生成/已删除）", "info")
        tm.add_log(task.task_id, "完成: 0 条", "ok")
        return
    tm.add_log(task.task_id, f"需要处理 {len(to_process)} 条记录", "info")
    success = 0
    failed = 0
    skipped = 0
    consecutive_ttd_failures = 0
    retry_round = 0
    for i, collection in enumerate(to_process):
        if tm.is_cancelled(task.task_id):
            tm.update(task.task_id, status="cancelled")
            return
        share = collection["share_code"]
        platform = collection.get("平台") or "douyin"
        tm.add_log(task.task_id, f"[{i+1}/{len(to_process)}] {share}", "info")
        try:
            outcome = await s.collector.resolve_short_url_ex(share, platform)

            if outcome.is_terminal:
                # 死链 / 平台不支持：一次判终态，之后永不重试（不进退避、不进冷却）
                new_status = TERMINAL_STATUS.get(outcome.kind, "暂不支持")
                s.db.update_collection(collection["record_id"], {"解析状态": new_status})
                skipped += 1
                # 终态不可逆，留一句回退提示：历史上「疑似死链」里有过被环境因素（代理继承）
                # 误杀的先例，用户手动改回「待解析」即可重试，成本远低于重建记录
                hint = "；若确认链接有效，可在表里改回「待解析」重试" if outcome.kind == "bad_link" else ""
                tm.add_log(task.task_id, f"SKIP {share}: {new_status}（{outcome.detail}）{hint}", "warning")
                tm.update(task.task_id, success=success, failed=failed, skipped=skipped)
                continue

            if outcome.is_service_failure:
                # 只有 TTD 这边的毛病才值得退避重试
                failed += 1
                s.db.update_collection(collection["record_id"], {"解析状态": "解析失败"})
                tm.add_log(task.task_id, f"X {share}: TTD 服务异常（{outcome.detail}）", "error")
                tm.update(task.task_id, success=success, failed=failed, skipped=skipped)
                consecutive_ttd_failures += 1
                remaining = len(to_process) - i - 1
                if await _handle_ttd_failures(tm, task.task_id, consecutive_ttd_failures, remaining, retry_round):
                    return
                # 达到阈值且仍有重试轮次 → 重置计数器，进入下一轮
                if consecutive_ttd_failures >= MAX_CONSECUTIVE_TTD_FAILURES:
                    retry_round += 1
                    consecutive_ttd_failures = 0
                continue

            sec_user_id = extract_sec_user_id(outcome.url, platform)
            existing = s.db.get_collection_by_sec_user_id(sec_user_id)
            if existing and existing["record_id"] != collection["record_id"]:
                new_level = s.merge_level(existing.get("等级"), collection.get("等级"))
                existing_tags = json.loads(existing.get("标签", "[]")) if existing.get("标签") else []
                new_tags = json.loads(collection.get("标签", "[]")) if collection.get("标签") else []
                merged_tags = s.merge_tags(existing_tags, new_tags)
                s.db.update_collection(existing["record_id"], {"等级": new_level, "标签": json.dumps(merged_tags, ensure_ascii=False)})
                s.db.delete_collection(collection["record_id"])
                success += 1
                consecutive_ttd_failures = 0
                tm.add_log(task.task_id, "OK 合并重复记录", "ok")
            else:
                s.db.update_collection(collection["record_id"], {"sec_user_id": sec_user_id, "解析状态": "已就绪"})
                success += 1
                consecutive_ttd_failures = 0
                tm.add_log(task.task_id, f"OK {share}: {sec_user_id}", "ok")
            tm.update(task.task_id, success=success, failed=failed)
        except Exception as e:
            failed += 1
            s.db.update_collection(collection["record_id"], {"解析状态": "解析失败"})
            tm.add_log(task.task_id, f"X {share}: {e}", "error")
            tm.update(task.task_id, success=success, failed=failed)
            consecutive_ttd_failures += 1
            remaining = len(to_process) - i - 1
            if await _handle_ttd_failures(tm, task.task_id, consecutive_ttd_failures, remaining, retry_round):
                return
            if consecutive_ttd_failures >= MAX_CONSECUTIVE_TTD_FAILURES:
                retry_round += 1
                consecutive_ttd_failures = 0
    tm.add_log(task.task_id, f"完成: 成功 {success} 失败 {failed} 跳过 {skipped}（跳过=链接失效/暂不支持，已判终态，下轮不再重试）", "info")


@app.post("/api/sync/v2/sync-account")
async def api_sync_v2_sync_account():
    """步骤3:生成账号表 - 后台任务,立即返回 task_id"""
    s = get_syncer_v2()
    if not s:
        return JSONResponse({"success": False, "message": "飞书未配置"}, status_code=400)
    tm = get_task_manager()
    task = tm.create("sync_account")
    asyncio.create_task(tm.run_serial(task, _run_sync_account))
    return {"task_id": task.task_id, "status": "pending", "message": "已加入队列"}


async def _run_sync_account(task):
    """后台执行:生成账号表(走 TTD API 拉账号详情)。串行队列内运行。"""
    import json
    tm = get_task_manager()
    s = get_syncer_v2()
    if not s:
        tm.add_log(task.task_id, "飞书未配置", "error")
        tm.update(task.task_id, status="failed", error="飞书未配置")
        return
    tm.add_log(task.task_id, "开始生成账号表", "info")
    collections = s.db.get_all_collections()
    to_process = [c for c in collections if s.is_ready_for_account(c)]
    tm.update(task.task_id, total=len(to_process))
    if not to_process:
        tm.add_log(task.task_id, "没有需要生成的记录（请先执行第二步解析账号标识）", "info")
        tm.add_log(task.task_id, "完成: 0 条", "ok")
        return
    tm.add_log(task.task_id, f"需要处理 {len(to_process)} 条记录", "info")
    db = get_database()
    cookie_list = get_cookie_list()
    if not cookie_list:
        tm.add_log(task.task_id, "Cookie 表为空,仅生成账号基础数据,跳过详情获取", "warning")
    else:
        tm.add_log(task.task_id, f"已加载 {len(cookie_list)} 个 Cookie", "info")
    cookie_failures: dict[str, int] = {}  # 本次任务内各 Cookie 的失效次数（≥2 次判失效）
    # 预检 TTD 服务是否可用(避免逐条等 15s 超时)    ttd_available = False
    try:
        async with httpx.AsyncClient(timeout=3, trust_env=False) as client:
            resp = await client.get(f"{s.collector.ttd_url}/")
            if resp.status_code in (200, 307, 404):
                ttd_available = True
    except Exception:
        ttd_available = False
    if not ttd_available:
        tm.add_log(task.task_id, "TTD 服务未运行,将跳过需要获取详情的新账号", "error")
    # 预加载已有账号,避免逐条查询
    existing_accounts_map = {}
    for a in db.get_all_accounts():
        existing_accounts_map[a["sec_user_id"]] = a
    success = 0
    failed = 0
    skipped = 0
    consecutive_ttd_failures = 0
    retry_round = 0
    for i, collection in enumerate(to_process):
        if tm.is_cancelled(task.task_id):
            tm.update(task.task_id, status="cancelled")
            return
        sec_user_id = collection["sec_user_id"]
        platform = collection.get("平台") or "douyin"
        existing_account = existing_accounts_map.get(sec_user_id)
        # 跳过 API 调用的条件：账号表已有 且 获取状态=已获取
        skip_api = existing_account and existing_account.get("获取状态") == "已获取"
        tm.add_log(task.task_id, f"[{i+1}/{len(to_process)}] {sec_user_id}", "info")
        try:
            if existing_account:
                # 账号已存在:合并等级标签备注（无论是否调API都执行）
                new_level = s.merge_level(existing_account.get("等级"), collection.get("等级"))
                existing_tags = json.loads(existing_account.get("标签", "[]")) if existing_account.get("标签") else []
                new_tags = json.loads(collection.get("标签", "[]")) if collection.get("标签") else []
                merged_tags = s.merge_tags(existing_tags, new_tags)
                # 备注合并：账号表已有备注则保留（用户可能已修改），否则用分享表的
                merged_note = existing_account.get("备注") or collection.get("备注") or ""
                s.db.update_account(existing_account["record_id"], {
                    "等级": new_level,
                    "标签": json.dumps(merged_tags, ensure_ascii=False),
                    "备注": merged_note,
                })
                # 标记为已生成
                s.db.update_collection(collection["record_id"], {"解析状态": "已生成"})
                account_id = existing_account["record_id"]
                if skip_api:
                    # 已获取过信息:只合并数据,不重复调API
                    skipped += 1
                    tm.add_log(task.task_id, f"SKIP {sec_user_id}: 获取状态=已获取,仅合并等级标签备注", "info")
                    tm.update(task.task_id, success=success, failed=failed, skipped=skipped)
                    continue
            else:
                record_id = f"acc_{datetime.now().strftime('%Y%m%d%H%M%S')}_{i}"
                s.db.insert_account({
                    "record_id": record_id,
                    "账号名称": "",
                    "平台": platform,
                    "链接": build_profile_url(sec_user_id, platform),
                    "sec_user_id": sec_user_id,
                    "等级": collection.get("等级"),
                    "标签": collection.get("标签"),
                    "备注": collection.get("备注") or "",
                    "获取状态": "待获取",
                })
                # 标记为已生成
                s.db.update_collection(collection["record_id"], {"解析状态": "已生成"})
                account_id = record_id
            # 获取账号详情
            if not ttd_available or not cookie_list:
                skipped += 1
                reason = "TTD 未运行" if not ttd_available else "Cookie 为空"
                tm.add_log(task.task_id, f"SKIP {sec_user_id}: {reason}", "warning")
                tm.update(task.task_id, success=success, failed=failed, skipped=skipped)
                continue
            info = await _fetch_account_info(
                s.collector, sec_user_id, platform, cookie_list, cookie_failures, i, tm, task.task_id
            )
            if not info and all(_cookie_is_dead(cookie_failures, c) for c in cookie_list):
                tm.add_log(task.task_id, "所有 Cookie 均已失效，请更新 Cookie 表", "error")
                tm.update(task.task_id, status="failed", error="所有 Cookie 均已失效")
                return
            if not info:
                info = {"_error": "本轮可用 Cookie 均未取到资料（可能账号异常或 Cookie 失效）"}
            if not info.get("nickname"):
                failed += 1
                reason = info.get("_error") or "TTD 返回空"
                s.db.update_account(account_id, {"获取状态": "获取失败"})
                tm.add_log(task.task_id, f"X {sec_user_id}: {reason}", "error")
                tm.update(task.task_id, success=success, failed=failed, skipped=skipped)
                consecutive_ttd_failures += 1
                remaining = len(to_process) - i - 1
                if await _handle_ttd_failures(tm, task.task_id, consecutive_ttd_failures, remaining, retry_round):
                    return
                if consecutive_ttd_failures >= MAX_CONSECUTIVE_TTD_FAILURES:
                    retry_round += 1
                    consecutive_ttd_failures = 0
                continue
            s.db.update_account(account_id, {
                "账号名称": info.get("nickname", ""),
                "粉丝数": info.get("follower_count", 0),
                "作品数": info.get("aweme_count", 0),
                "签名": info.get("signature", ""),
                "头像": info.get("avatar", ""),
                "uid": info.get("uid", ""),
                "获取状态": "已获取",
            })
            consecutive_ttd_failures = 0
            # 更新内存缓存
            existing_accounts_map[sec_user_id] = {
                **(existing_account or {}),
                "账号名称": info.get("nickname", ""),
                "获取状态": "已获取",
            }
            success += 1
            tm.add_log(task.task_id, f"OK 新增/更新账号: {info.get('nickname')}", "ok")
            tm.update(task.task_id, success=success, failed=failed, skipped=skipped)
        except Exception as e:
            failed += 1
            tm.add_log(task.task_id, f"X {sec_user_id}: {e}", "error")
            tm.update(task.task_id, success=success, failed=failed, skipped=skipped)
            consecutive_ttd_failures += 1
            remaining = len(to_process) - i - 1
            if await _handle_ttd_failures(tm, task.task_id, consecutive_ttd_failures, remaining, retry_round):
                return
            if consecutive_ttd_failures >= MAX_CONSECUTIVE_TTD_FAILURES:
                retry_round += 1
                consecutive_ttd_failures = 0
    tm.add_log(task.task_id, f"完成: 成功 {success} 失败 {failed} 跳过 {skipped}", "info")


# ========== 后台任务查询 ==========

@app.get("/api/tasks")
async def api_tasks_list():
    """列出所有任务(running/pending 在前,完成的按时间倒序)，并附加运行中的采集批次"""
    tm = get_task_manager()
    tasks = [t.to_dict() for t in tm.list()]
    try:
        active_batches = get_database().list_active_collection_batches()
    except Exception:
        active_batches = []
    for b in active_batches:
        tasks.append({
            "task_id": f"batch_{b.get('id', '')}",
            "type": "collection_batch",
            "status": b.get("status", "running"),
            "total": b.get("total_accounts", 0) or 0,
            "success": b.get("success_accounts", 0) or 0,
            "failed": b.get("failed_accounts", 0) or 0,
            "skipped": b.get("skipped_accounts", 0) or 0,
            "log": [],
            "started_at": b.get("started_at"),
            "finished_at": None,
            "error": "",
        })
    return {"tasks": tasks}


@app.get("/api/tasks/history")
async def api_tasks_history(limit: int = 20, offset: int = 0):
    """历史任务(从 sync_history 表读,支持分页)"""
    db = get_database()
    history = db.get_sync_history(limit=limit, offset=offset)
    return {"history": history, "has_more": len(history) == limit}

@app.get("/api/tasks/{task_id}")
async def api_task_detail(task_id: str):
    """单任务详情(含日志)"""
    tm = get_task_manager()
    t = tm.get(task_id)
    if not t:
        return JSONResponse({"success": False, "message": "任务不存在"}, status_code=404)
    return t.to_dict()


@app.post("/api/tasks/{task_id}/cancel")
async def api_task_cancel(task_id: str):
    """取消任务(设标志位,任务循环里检查退出)"""
    tm = get_task_manager()
    ok = tm.request_cancel(task_id)
    return {"success": ok, "message": "已请求取消" if ok else "任务不存在或已结束"}


@app.get("/api/sync/history/{task_type}")
async def api_sync_history(task_type: str):
    """获取指定步骤的同步历史"""
    db = get_database()
    history = db.get_sync_history(task_type, limit=50)
    return {"history": history}


@app.post("/api/sync/v2/refresh-accounts")
async def api_sync_v2_refresh_accounts():
    """批量刷新账号表 — 后台任务,立即返回 task_id"""
    tm = get_task_manager()
    task = tm.create("refresh_accounts")
    asyncio.create_task(tm.run_serial(task, _run_refresh_accounts))
    return {"task_id": task.task_id, "status": "pending", "message": "已加入队列"}


async def _run_refresh_accounts(task):
    """后台执行:获取未获取信息的账号的资料。串行队列内运行。"""
    tm = get_task_manager()
    db = get_database()
    tm.add_log(task.task_id, "开始获取账号资料", "info")
    accounts = db.get_all_accounts()
    to_fetch = [a for a in accounts if a.get("sec_user_id") and a.get("获取状态") in ("待获取", "获取失败")]
    tm.update(task.task_id, total=len(to_fetch))
    if not to_fetch:
        tm.add_log(task.task_id, "没有需要获取的账号", "info")
        tm.add_log(task.task_id, "完成: 0 条", "ok")
        return
    tm.add_log(task.task_id, f"共 {len(to_fetch)} 个账号需要刷新", "info")
    # 加载 Cookie
    cookie_list = get_cookie_list()
    if not cookie_list:
        tm.add_log(task.task_id, "Cookie 表为空,无法获取账号详情", "error")
        tm.update(task.task_id, status="failed", error="Cookie 表为空")
        return
    tm.add_log(task.task_id, f"已加载 {len(cookie_list)} 个 Cookie", "info")
    cookie_failures: dict[str, int] = {}  # 本次任务内各 Cookie 的失效次数（≥2 次判失效）
    # 预检 TTD
    ttd_available = False
    try:
        async with httpx.AsyncClient(timeout=3, trust_env=False) as client:
            resp = await client.get(f"http://127.0.0.1:{config.ttd_port}/")
            if resp.status_code in (200, 307, 404):
                ttd_available = True
    except Exception:
        ttd_available = False
    if not ttd_available:
        tm.add_log(task.task_id, "TTD 服务未运行,无法获取账号详情", "error")
        tm.update(task.task_id, status="failed", error="TTD 服务未运行")
        return
    col = get_collector()
    success = 0
    failed = 0
    consecutive_ttd_failures = 0
    retry_round = 0
    for i, account in enumerate(to_fetch):
        if tm.is_cancelled(task.task_id):
            tm.update(task.task_id, status="cancelled")
            return
        sec_user_id = account["sec_user_id"]
        old_name = account.get("账号名称") or sec_user_id[:20]
        tm.add_log(task.task_id, f"[{i+1}/{len(to_fetch)}] {old_name}", "info")
        try:
            platform = account.get("平台") or "douyin"
            info = await _fetch_account_info(
                col, sec_user_id, platform, cookie_list, cookie_failures, i, tm, task.task_id
            )
            if not info and all(_cookie_is_dead(cookie_failures, c) for c in cookie_list):
                tm.add_log(task.task_id, "所有 Cookie 均已失效，请更新 Cookie 表", "error")
                tm.update(task.task_id, status="failed", error="所有 Cookie 均已失效")
                return
            if not info:
                info = {"_error": "本轮可用 Cookie 均未取到资料（可能账号异常或 Cookie 失效）"}
            nickname = info.get("nickname", "")
            if nickname:
                db.update_account(account.get("record_id", ""), {
                    "账号名称": nickname,
                    "粉丝数": info.get("follower_count", 0),
                    "作品数": info.get("aweme_count", 0),
                    "签名": info.get("signature", ""),
                    "头像": info.get("avatar", ""),
                    "获取状态": "已获取",
                })
                success += 1
                consecutive_ttd_failures = 0
                tm.add_log(task.task_id, f"OK {nickname} | 粉丝 {info.get('follower_count', 0)} | 作品 {info.get('aweme_count', 0)}", "ok")
            else:
                failed += 1
                reason = info.get("_error") or "无法获取资料"
                db.update_account(account.get("record_id", ""), {"获取状态": "获取失败"})
                tm.add_log(task.task_id, f"X {old_name}: {reason}", "error")
                consecutive_ttd_failures += 1
                remaining = len(to_fetch) - i - 1
                tm.update(task.task_id, success=success, failed=failed)
                if await _handle_ttd_failures(tm, task.task_id, consecutive_ttd_failures, remaining, retry_round):
                    return
                if consecutive_ttd_failures >= MAX_CONSECUTIVE_TTD_FAILURES:
                    retry_round += 1
                    consecutive_ttd_failures = 0
            tm.update(task.task_id, success=success, failed=failed)
        except Exception as e:
            failed += 1
            tm.add_log(task.task_id, f"X {old_name}: {e}", "error")
            tm.update(task.task_id, success=success, failed=failed)
            consecutive_ttd_failures += 1
            remaining = len(to_fetch) - i - 1
            if await _handle_ttd_failures(tm, task.task_id, consecutive_ttd_failures, remaining, retry_round):
                return
            if consecutive_ttd_failures >= MAX_CONSECUTIVE_TTD_FAILURES:
                retry_round += 1
                consecutive_ttd_failures = 0
    tm.add_log(task.task_id, f"完成: 成功 {success} 失败 {failed}", "info")


@app.post("/api/sync/v2/all")
async def api_sync_v2_all(request: Request):
    """处理账号数据（使用新同步器）"""
    try:
        data = await request.json()
        text = data.get("text", "")
        inline_count = sum(1 for line in text.splitlines() if line.strip())
        if inline_count > MAX_INLINE_SYNC_ITEMS:
            return JSONResponse(
                {
                    "success": False,
                    "message": f"单次同步接口最多 {MAX_INLINE_SYNC_ITEMS} 条，请分批导入或使用后台任务",
                },
                status_code=413,
            )

        s = get_syncer_v2()
        if not s:
            return JSONResponse(
                {"success": False, "message": "飞书未配置"},
                status_code=400,
            )
        
        results = await s.sync_all(text)
        return {
            "success": True,
            "message": "处理账号数据完成",
            **results
        }
    except Exception as e:
        logger.error(f"处理账号数据失败: {e}")
        return JSONResponse(
            {"success": False, "message": f"同步异常: {str(e)}"},
            status_code=500,
        )


class ImportItem(BaseModel):
    link: str
    rating: int = 3
    tags: list[str] = []
    name: str = ""
    follower_count: int = 0
    aweme_count: int = 0


class ImportCollectionRequest(BaseModel):
    items: list[ImportItem]


class CollectionBatchRequest(BaseModel):
    rating_min: int = 3
    tags: list[str] = []
    account_names: str = ""
    mode: Literal["incremental", "full"] = "incremental"
    platform: Literal["douyin", "tiktok", "all"] = "douyin"
    preset_id: int | None = None
    storage_primary_id: str = ""   # 主方案 ID（空=用预设/默认主）
    storage_secondary_id: str = ""  # 次方案 ID（空=用预设/默认次）
    force_low_space: bool = False   # 低空间提醒确认后重发时为 True


class CollectionRetryRequest(BaseModel):
    mode: Literal["incremental", "full"] = "incremental"


class SingleWorkResolveRequest(BaseModel):
    links: str
    resolve_mode: str = "auto"


class SingleWorkDownloadRequest(SingleWorkResolveRequest):
    target_dir: str
    storage_primary_id: str = ""   # 主方案 ID（空=设置页默认主）
    storage_secondary_id: str = ""  # 次方案 ID（空=设置页默认次）
    storage_choice: str = "auto"  # 旧版兼容字段：p:<id> 自动迁移为主方案 ID
    filename_template: str = "{create_time} {author} {title}"
    filename_overrides: dict[str, str] = Field(default_factory=dict)
    asset_indexes: list[int] = Field(default_factory=list)
    include_music: bool = False
    include_static_cover: bool = False
    include_dynamic_cover: bool = False
    folder_mode: bool = False  # 每作品独立子文件夹（与增量 TTD folder_mode 语义一致）
    work: dict | None = None  # 前端可传入已解析的 work 数据，跳过二次解析


class SingleWorkRetryRequest(BaseModel):
    target_dir: str = ""
    storage_primary_id: str = ""
    storage_secondary_id: str = ""
    filename_template: str = ""
    filename_override: str | None = None
    asset_indexes: list[int] | None = None


@app.post("/api/import/collection")
async def api_import_collection(request: ImportCollectionRequest):
    """将解析后的数据写入飞书分享表（批量写入，一次最多500条）"""
    f = get_feishu()
    if not f:
        return JSONResponse({"success": False, "message": "飞书未配置"}, status_code=400)

    app_token = config.feishu.get("app_token", "")
    table_id = config.feishu.get("collection_table_id", "")
    if not app_token or not table_id:
        return JSONResponse({"success": False, "message": "未配置分享表 Table ID"}, status_code=400)

    # 构建批量记录
    records = []
    for item in request.items:
        fields = {
            "地址": item.link,
            "等级": item.rating,
            "账号状态": "待生成",
        }
        if item.tags:
            fields["标签"] = item.tags
        if item.name:
            fields["账号名称"] = item.name
        if item.follower_count:
            fields["粉丝数"] = item.follower_count
        if item.aweme_count:
            fields["作品数"] = item.aweme_count
        records.append({"fields": fields})

    success_count = 0
    errors = []

    # 分批写入，每批最多 500 条
    batch_size = 500
    for i in range(0, len(records), batch_size):
        batch = records[i:i + batch_size]
        try:
            result = f.batch_create_records(app_token, table_id, batch)
            if result.get("code") == 0:
                success_count += len(batch)
            else:
                errors.append(f"批次 {i//batch_size + 1}: {result.get('msg', '未知错误')}")
        except Exception as e:
            errors.append(f"批次 {i//batch_size + 1}: {e}")

    return {
        "success": len(errors) == 0,
        "message": f"成功写入 {success_count} 条" + (f"，{len(errors)} 条失败" if errors else ""),
        "count": success_count,
        "errors": errors,
    }


@app.post("/api/duplicates/resolve")
async def api_resolve_duplicate(
    request: Request,
):
    """处理重复账号"""
    data = await request.json()
    action = data.get("action")  # keep_new / keep_old / keep_both / skip
    new_record_id = data.get("new_record_id")
    existing_record_id = data.get("existing_record_id")

    f = get_feishu()
    if not f:
        return JSONResponse({"success": False, "message": "飞书未连接"}, status_code=400)

    app_token = config.feishu.get("app_token", "")
    table_id = config.feishu.get("account_table_id", "")

    try:
        if action == "keep_new":
            # 删除旧记录
            if existing_record_id:
                f.delete_record(app_token, table_id, existing_record_id)
            return {"success": True, "message": "已保留新条目，删除旧条目"}

        elif action == "keep_old":
            # 删除新记录
            if new_record_id:
                f.delete_record(app_token, table_id, new_record_id)
            return {"success": True, "message": "已保留旧条目，删除新条目"}

        elif action == "keep_both":
            # 给新条目备注标记
            if new_record_id:
                f.update_record(app_token, table_id, new_record_id, {
                    "备注": "⚠️ 与已有账号重复，已保留两条",
                })
            return {"success": True, "message": "两条都已保留"}

        elif action == "skip":
            return {"success": True, "message": "已跳过"}

        else:
            return JSONResponse({"success": False, "message": f"未知操作: {action}"}, status_code=400)

    except Exception as e:
        return JSONResponse({"success": False, "message": str(e)}, status_code=500)


# --- 数据库管理 ---

@app.get("/api/database/stats")
async def api_database_stats():
    """获取数据库统计信息"""
    db = get_database()
    return db.get_table_counts()


@app.get("/api/database/table/{table_name}")
async def api_database_table(
    table_name: str,
    limit: int = 100,
    offset: int = 0,
    search: str = "",
    search_field: str = "",
    sort_field: str = "",
    sort_order: str = "desc",
    filters: str = "",
    need_total: bool = True,
):
    """获取表数据，支持分页、搜索、排序、多条件筛选。返回 {records, total, limit, offset}

    search_field: 定向搜索字段，为空时退化为全列 LIKE（慢）。
    need_total: 滚动加载后续批次可传 false 复用首批计数，避免重复 COUNT。
    """
    db = get_database()

    # 验证表名
    if table_name not in db.VALID_TABLES:
        return JSONResponse({"success": False, "message": "无效的表名"}, status_code=400)

    result = db.query_table(
        table_name,
        limit=limit,
        offset=offset,
        search=search,
        search_field=search_field or None,
        sort_field=sort_field or None,
        sort_order=sort_order,
        filters=filters or None,
        need_total=need_total,
    )
    return result


@app.get("/api/database/table/{table_name}/distinct/{field_name}")
async def api_database_distinct_values(table_name: str, field_name: str):
    """获取某字段的去重值列表（用于筛选下拉选项）"""
    db = get_database()
    if table_name not in db.VALID_TABLES:
        return JSONResponse({"success": False, "message": "无效的表名"}, status_code=400)
    values = db.get_distinct_values(table_name, field_name)
    return {"values": values}


@app.get("/api/database/table/{table_name}/value-counts/{field_name}")
async def api_database_value_counts(table_name: str, field_name: str, filters: str = ""):
    """筛选选项计数：每个值在"其他筛选条件下"的命中条数（金山多维表口径）"""
    db = get_database()
    if table_name not in db.VALID_TABLES:
        return JSONResponse({"success": False, "message": "无效的表名"}, status_code=400)
    return db.get_value_counts(table_name, field_name, filters=filters or None)


@app.get("/api/database/table/{table_name}/schema")
async def api_database_table_schema(table_name: str):
    """获取表结构（字段列表）"""
    db = get_database()
    if table_name not in db.VALID_TABLES:
        return JSONResponse({"success": False, "message": "无效的表名"}, status_code=400)
    return {"fields": db.get_table_schema(table_name)}


@app.get("/api/database/table/{table_name}/record/{record_id}")
async def api_database_get_record(table_name: str, record_id: str):
    """获取单条记录（用于编辑弹窗回填）"""
    db = get_database()
    if table_name not in db.VALID_TABLES:
        return JSONResponse({"success": False, "message": "无效的表名"}, status_code=400)
    record = db.get_record_by_id(table_name, record_id)
    if record:
        return {"success": True, "record": record}
    return JSONResponse({"success": False, "message": "记录不存在"}, status_code=404)


@app.patch("/api/database/table/{table_name}/record/{record_id}")
async def api_database_update_field(
    table_name: str, record_id: str, field: str = "", value: str = ""
):
    """更新单条记录的单个字段（用于启用/禁用开关等）"""
    db = get_database()
    if table_name not in db.VALID_TABLES:
        return JSONResponse({"success": False, "message": "无效的表名"}, status_code=400)
    if not field:
        return JSONResponse({"success": False, "message": "缺少 field 参数"}, status_code=400)

    # 解析 value：支持布尔/数字/文本
    parsed_value: Any = value
    if value.lower() in ("true", "false"):
        parsed_value = value.lower() == "true"
    elif value in ("1", "0"):
        parsed_value = value == "1"
    elif value.isdigit():
        parsed_value = int(value)

    try:
        ok = db.update_record_field(table_name, record_id, field, parsed_value)
        if ok:
            return {"success": True, "message": "更新成功"}
        return JSONResponse({"success": False, "message": "未找到记录"}, status_code=404)
    except Exception as e:
        return JSONResponse({"success": False, "message": str(e)}, status_code=500)


@app.get("/api/database/table/{table_name}/export")
async def api_database_export_csv(table_name: str, search: str = "", search_field: str = "", filters: str = ""):
    """全量导出表为 CSV（含 BOM 头，Excel 可直接打开）"""
    import csv
    import io
    from fastapi.responses import StreamingResponse

    db = get_database()
    if table_name not in db.VALID_TABLES:
        return JSONResponse({"success": False, "message": "无效的表名"}, status_code=400)

    schema = db.get_table_schema(table_name)
    col_names = [s["name"] for s in schema]
    result = db.query_table(table_name, limit=10**9, offset=0, search=search,
                            search_field=search_field or None, filters=filters or None)
    records = result["records"]

    # 写 CSV
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(col_names)
    for r in records:
        writer.writerow([_csv_cell(r.get(c, "")) for c in col_names])

    content = "\ufeff" + buf.getvalue()  # BOM + 内容
    filename = f"DoukHub_{table_name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    return StreamingResponse(
        iter([content.encode("utf-8")]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _csv_cell(v: Any) -> str:
    """格式化 CSV 单元格值"""
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


@app.post("/api/database/table/{table_name}/import/preview")
async def api_database_import_preview(table_name: str, request: Request):
    """严格模式导入预览：上传 CSV，校验表头，返回统计和前5行预览，不写入。"""
    import csv
    import io

    db = get_database()
    if table_name not in db.VALID_TABLES:
        return JSONResponse({"success": False, "message": "无效的表名"}, status_code=400)

    form = await request.form()
    file = form.get("file")
    if not file:
        return JSONResponse({"success": False, "message": "缺少上传文件"}, status_code=400)

    try:
        content = await file.read()
    except Exception as e:
        return JSONResponse({"success": False, "message": f"读取文件失败: {e}"}, status_code=400)

    # 解码（处理 BOM）
    text = content.decode("utf-8-sig") if content.startswith(b"\xef\xbb\xbf") else content.decode("utf-8", errors="replace")
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    if not rows:
        return JSONResponse({"success": False, "message": "CSV 文件为空"}, status_code=400)

    header = [h.strip() for h in rows[0]]
    schema = db.get_table_schema(table_name)
    col_names = [s["name"] for s in schema]
    pk_cols = [s["name"] for s in schema if s["pk"]]
    pk = pk_cols[0] if pk_cols else None

    # 严格校验表头
    unknown = [h for h in header if h not in col_names]
    if unknown:
        return JSONResponse({
            "success": False,
            "message": f"表头存在未知字段（严格模式要求完全匹配）: {unknown}",
            "expected_fields": col_names,
        }, status_code=400)

    # 解析数据
    parsed_rows: list[dict] = []
    errors: list[str] = []
    for idx, row in enumerate(rows[1:], start=2):
        if not any(c.strip() for c in row):  # 跳过空行
            continue
        if len(row) < len(header):
            errors.append(f"第{idx}行：列数不足")
            continue
        record = {header[i]: _parse_csv_value(row[i], schema, header[i]) for i in range(len(header))}
        parsed_rows.append(record)

    # 模拟统计（不写入）
    new_count = 0
    skip_count = 0
    with db._connect() as conn:
        for r in parsed_rows:
            if pk and r.get(pk):
                existed = conn.execute(
                    f'SELECT 1 FROM {table_name} WHERE "{pk}" = ?', (r[pk],)
                ).fetchone()
                if existed:
                    skip_count += 1
                    continue
            new_count += 1

    preview = parsed_rows[:5]
    return {
        "success": True,
        "total": len(parsed_rows),
        "new": new_count,
        "skipped": skip_count,
        "errors": errors[:20],
        "preview": preview,
        "fields": header,
    }


@app.post("/api/database/table/{table_name}/import/confirm")
async def api_database_import_confirm(table_name: str, request: Request):
    """严格模式导入确认：再次上传 CSV，执行写入（跳过已存在主键）。"""
    import csv
    import io

    db = get_database()
    if table_name not in db.VALID_TABLES:
        return JSONResponse({"success": False, "message": "无效的表名"}, status_code=400)

    form = await request.form()
    file = form.get("file")
    if not file:
        return JSONResponse({"success": False, "message": "缺少上传文件"}, status_code=400)

    try:
        content = await file.read()
    except Exception as e:
        return JSONResponse({"success": False, "message": f"读取文件失败: {e}"}, status_code=400)

    text = content.decode("utf-8-sig") if content.startswith(b"\xef\xbb\xbf") else content.decode("utf-8", errors="replace")
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    if not rows:
        return JSONResponse({"success": False, "message": "CSV 文件为空"}, status_code=400)

    header = [h.strip() for h in rows[0]]
    schema = db.get_table_schema(table_name)
    col_names = [s["name"] for s in schema]
    unknown = [h for h in header if h not in col_names]
    if unknown:
        return JSONResponse({
            "success": False,
            "message": f"表头存在未知字段: {unknown}",
        }, status_code=400)

    parsed = []
    for idx, row in enumerate(rows[1:], start=2):
        if not any(c.strip() for c in row):
            continue
        if len(row) < len(header):
            continue
        record = {header[i]: _parse_csv_value(row[i], schema, header[i]) for i in range(len(header))}
        parsed.append(record)

    result = db.import_records(table_name, parsed, skip_existing=True)
    return {"success": True, **result}


def _parse_csv_value(raw: str, schema: list[dict], field: str) -> Any:
    """根据字段类型把 CSV 字符串解析为合适的 Python 值"""
    s = (raw or "").strip()
    col_def = next((c for c in schema if c["name"] == field), None)
    if not col_def:
        return s
    col_type = (col_def.get("type") or "").upper()
    if s == "":
        return None
    if col_type == "BOOLEAN":
        return s.lower() in ("true", "1", "是", "yes", "y")
    if col_type in ("INTEGER", "INT"):
        try:
            return int(s)
        except ValueError:
            return s
    if col_type == "REAL":
        try:
            return float(s)
        except ValueError:
            return s
    return s


@app.delete("/api/database/table/{table_name}/record/{record_id}")
async def api_database_delete_record(table_name: str, record_id: str):
    """删除记录"""
    db = get_database()

    # 验证表名
    valid_tables = ["share_cache", "account_cache", "cookie_cache"]
    if table_name not in valid_tables:
        return JSONResponse({"success": False, "message": "无效的表名"}, status_code=400)
    
    try:
        if table_name == "share_cache":
            success = db.delete_collection(record_id)
        elif table_name == "account_cache":
            # 先查再删：硬删除后按 ID 查不到了
            acc = db.get_account_by_id(record_id)
            success = db.delete_account(record_id)
            # 联动：把分享表中对应对 sec_user_id 的记录标记为「已删除」
            if success:
                if acc and acc.get("sec_user_id"):
                    share_row = db.get_collection_by_sec_user_id(acc["sec_user_id"])
                    if share_row:
                        db.update_collection(share_row["record_id"], {"解析状态": "已删除"})
        elif table_name == "cookie_cache":
            success = db.delete_cookie(record_id)
        
        if success:
            return {"success": True, "message": "删除成功"}
        return JSONResponse({"success": False, "message": "删除失败"}, status_code=400)
    except Exception as e:
        return JSONResponse({"success": False, "message": str(e)}, status_code=500)


@app.delete("/api/database/table/{table_name}")
async def api_database_clear_table(table_name: str):
    """清空表"""
    db = get_database()
    
    # 清空前自动备份
    backup.create_backup(reason="清空表前自动备份")
    
    # 验证表名
    valid_tables = ["share_cache", "account_cache", "cookie_cache"]
    if table_name not in valid_tables:
        return JSONResponse({"success": False, "message": "无效的表名"}, status_code=400)
    
    try:
        if table_name == "share_cache":
            success = db.clear_share_cache()
        elif table_name == "account_cache":
            success = db.clear_account_cache()
        elif table_name == "cookie_cache":
            with db._connect() as conn:
                conn.execute("DELETE FROM cookie_cache")
                conn.commit()
                success = True
        
        if success:
            return {"success": True, "message": "清空成功"}
        return JSONResponse({"success": False, "message": "清空失败"}, status_code=400)
    except Exception as e:
        return JSONResponse({"success": False, "message": str(e)}, status_code=500)


# ========== Block 2：增强数据管理 API ==========

@app.post("/api/database/batch-update")
async def api_database_batch_update(request: Request):
    """批量更新多条记录的相同字段"""
    body = await request.json()
    table = body.get("table")
    record_ids = body.get("record_ids", [])
    updates = body.get("updates", {})
    if not table or not record_ids or not updates:
        return JSONResponse({"success": False, "message": "缺少参数: table, record_ids, updates"}, status_code=400)
    db = get_database()
    result = db.batch_update(table, record_ids, updates)
    return {"success": result["failed"] == 0, "data": result}


@app.post("/api/database/batch-delete")
async def api_database_batch_delete(request: Request):
    """批量删除多条记录"""
    body = await request.json()
    table = body.get("table")
    record_ids = body.get("record_ids", [])
    if not table or not record_ids:
        return JSONResponse({"success": False, "message": "缺少参数: table, record_ids"}, status_code=400)
    db = get_database()
    
    # 批量删除前自动备份
    backup.create_backup(reason="批量删除前自动备份")
    
    result = db.batch_delete(table, record_ids)
    return {"success": result["failed"] == 0, "data": result}


@app.post("/api/database/insert")
async def api_database_insert(request: Request):
    """插入单条记录"""
    body = await request.json()
    table = body.get("table")
    data = body.get("data", {})
    if not table or not data:
        return JSONResponse({"success": False, "message": "缺少参数: table, data"}, status_code=400)
    db = get_database()
    result = db.insert_single(table, data)
    return result


@app.put("/api/database/update")
async def api_database_update(request: Request):
    """更新单条记录（行内编辑保存）"""
    body = await request.json()
    table = body.get("table")
    record_id = body.get("record_id")
    data = body.get("data", {})
    if not table or not record_id or not data:
        return JSONResponse({"success": False, "message": "缺少参数: table, record_id, data"}, status_code=400)
    db = get_database()
    result = db.update_record(table, record_id, data)
    return result


@app.get("/api/database/duplicates/{table_name}")
async def api_database_duplicates(table_name: str):
    """检测表中的重复记录"""
    db = get_database()
    duplicates = db.get_duplicates(table_name)
    return {"success": True, "data": duplicates, "count": len(duplicates)}


@app.get("/api/database/stats-detailed")
async def api_database_stats_detailed():
    """获取各表的详细统计（含账号状态、启用状态等细分）"""
    db = get_database()
    stats = db.get_stats_detailed()
    return {"success": True, "data": stats}


# --- 云端同步（后台任务）---


@app.post("/api/feishu/sync")
async def api_feishu_sync():
    """增量同步：本地 ↔ 云端 双向 6 步 — 后台任务"""
    if not config.get("feishu.auto_sync", True):
        return JSONResponse({"success": False, "message": "云同步已关闭（纯本地模式），请在同步面板打开开关"}, status_code=400)
    fs = get_feishu_syncer()
    if not fs:
        return JSONResponse({"success": False, "message": "云端未配置"}, status_code=400)
    tm = get_task_manager()
    task = tm.create("cloud_sync")
    asyncio.create_task(tm.run_serial(task, _run_cloud_sync))
    return {"task_id": task.task_id, "status": "pending", "message": "已加入队列"}


_RECONCILE_CACHE: tuple[float, dict] | None = None


@app.get("/api/feishu/reconcile")
async def api_feishu_reconcile():
    """本地 ↔ 云端数据对账：三表行数比对（飞书侧 page_size=1 取 total，结果缓存 60s）"""
    global _RECONCILE_CACHE
    import time

    now = time.time()
    if _RECONCILE_CACHE and now - _RECONCILE_CACHE[0] < 60:
        return _RECONCILE_CACHE[1]

    db = get_database()
    labels = {"share_cache": "分享表", "account_cache": "账号表", "cookie_cache": "Cookie表"}
    local_counts: dict[str, int] = {}
    with db._connect() as conn:
        for t in labels:
            try:
                local_counts[t] = conn.execute(
                    f"SELECT COUNT(*) FROM {t} WHERE IFNULL(is_deleted,0)=0"
                ).fetchone()[0]
            except Exception:
                local_counts[t] = 0

    result: dict = {"success": True, "feishu_ok": False, "tables": [], "match_all": False}
    fs = get_feishu_syncer()
    if not fs:
        result["message"] = "云端未配置"
    else:
        try:
            for t, label in labels.items():
                table_id = {
                    "share_cache": fs.collection_table_id,
                    "account_cache": fs.account_table_id,
                    "cookie_cache": fs.cookie_table_id,
                }.get(t, "")
                cloud = 0
                if table_id:
                    resp = fs.feishu.list_records(fs.app_token, table_id, page_size=1)
                    if resp.get("code") == 0:
                        cloud = int(resp.get("data", {}).get("total") or 0)
                result["tables"].append({
                    "key": t, "label": label,
                    "local": local_counts[t], "cloud": cloud,
                    "match": local_counts[t] == cloud,
                })
            result["feishu_ok"] = True
            result["match_all"] = all(x["match"] for x in result["tables"])
        except Exception as e:
            result["message"] = f"云端不可达: {str(e)[:120]}"

    _RECONCILE_CACHE = (now, result)
    return result


@app.post("/api/feishu/sync/full")
async def api_feishu_sync_full(request: Request):
    """全盘同步：以一端为基准覆盖另一端 — 后台任务"""
    fs = get_feishu_syncer()
    if not fs:
        return JSONResponse({"success": False, "message": "云端未配置"}, status_code=400)
    try:
        data = await request.json()
    except Exception:
        data = {}
    direction = data.get("direction", "")
    if direction not in ("to-feishu", "from-feishu"):
        return JSONResponse({
            "success": False,
            "message": "参数 direction 必须是 to-feishu 或 from-feishu",
        }, status_code=400)
    tm = get_task_manager()
    task = tm.create("cloud_sync_full")
    task._direction = direction
    asyncio.create_task(tm.run_serial(task, _run_cloud_sync_full))
    return {"task_id": task.task_id, "status": "pending", "message": "已加入队列"}


async def _run_cloud_sync(task):
    """后台执行:增量云端同步(6步双向)"""
    import asyncio as _aio
    tm = get_task_manager()
    fs = get_feishu_syncer()
    if not fs:
        tm.add_log(task.task_id, "云端未配置", "error")
        tm.update(task.task_id, status="failed", error="云端未配置")
        return
    tm.add_log(task.task_id, "开始增量云端同步(6步双向)", "info")
    steps = fs.get_incremental_steps()
    tm.update(task.task_id, total=len(steps))
    success = 0
    failed = 0
    for i, (label, fn) in enumerate(steps):
        if tm.is_cancelled(task.task_id):
            tm.update(task.task_id, status="cancelled")
            return
        tm.add_log(task.task_id, f"[{i+1}/{len(steps)}] {label}", "info")
        try:
            r = await _aio.to_thread(fn)
            if r and all(isinstance(v, dict) and not any(k in v for k in ("created", "updated", "failed")) for v in r.values()):
                for sub_label, sub_r in r.items():
                    parts = []
                    if sub_r.get("created"): parts.append(f"新增 {sub_r['created']}")
                    if sub_r.get("updated"): parts.append(f"更新 {sub_r['updated']}")
                    if sub_r.get("deleted"): parts.append(f"删除 {sub_r['deleted']}")
                    if sub_r.get("skipped_uptodate"): parts.append(f"已最新 {sub_r['skipped_uptodate']}")
                    if sub_r.get("failed"): parts.append(f"失败 {sub_r['failed']}")
                    msg = "，".join(parts) if parts else "无变化"
                    lvl = "ok" if not sub_r.get("failed") else "error"
                    tm.add_log(task.task_id, f"{'OK' if lvl=='ok' else 'X'} {sub_label}: {msg}", lvl)
                    if sub_r.get("failed"):
                        failed += 1
                    else:
                        success += 1
            else:
                parts = []
                if r.get("created"): parts.append(f"新增 {r['created']}")
                if r.get("updated"): parts.append(f"更新 {r['updated']}")
                if r.get("deleted"): parts.append(f"删除 {r['deleted']}")
                if r.get("skipped_uptodate"): parts.append(f"已最新 {r['skipped_uptodate']}")
                if r.get("failed"): parts.append(f"失败 {r['failed']}")
                msg = "，".join(parts) if parts else "无变化"
                lvl = "ok" if not r.get("failed") else "error"
                tm.add_log(task.task_id, f"{'OK' if lvl=='ok' else 'X'} {label}: {msg}", lvl)
                if r.get("failed"):
                    failed += 1
                else:
                    success += 1
            tm.update(task.task_id, success=success, failed=failed)
        except Exception as e:
            failed += 1
            tm.add_log(task.task_id, f"X {label}: {e}", "error")
            tm.update(task.task_id, success=success, failed=failed)
    tm.add_log(task.task_id, f"完成: 成功 {success} 失败 {failed}", "info")


async def _run_cloud_sync_full(task):
    """后台执行:全盘云端同步"""
    import asyncio as _aio
    tm = get_task_manager()
    fs = get_feishu_syncer()
    if not fs:
        tm.add_log(task.task_id, "云端未配置", "error")
        tm.update(task.task_id, status="failed", error="云端未配置")
        return
    direction = getattr(task, '_direction', 'to-feishu')
    tm.add_log(task.task_id, f"开始全盘同步({direction})", "info")
    steps = fs.get_full_steps(direction)
    tm.update(task.task_id, total=len(steps))
    success = 0
    failed = 0
    for i, (label, fn) in enumerate(steps):
        if tm.is_cancelled(task.task_id):
            tm.update(task.task_id, status="cancelled")
            return
        tm.add_log(task.task_id, f"[{i+1}/{len(steps)}] {label}", "info")
        try:
            r = await _aio.to_thread(fn)
            if r and all(isinstance(v, dict) and not any(k in v for k in ("created", "updated", "failed")) for v in r.values()):
                for sub_label, sub_r in r.items():
                    parts = []
                    if sub_r.get("created"): parts.append(f"新增 {sub_r['created']}")
                    if sub_r.get("updated"): parts.append(f"更新 {sub_r['updated']}")
                    if sub_r.get("deleted"): parts.append(f"删除 {sub_r['deleted']}")
                    if sub_r.get("failed"): parts.append(f"失败 {sub_r['failed']}")
                    msg = "，".join(parts) if parts else "无变化"
                    lvl = "ok" if not sub_r.get("failed") else "error"
                    tm.add_log(task.task_id, f"{'OK' if lvl=='ok' else 'X'} {sub_label}: {msg}", lvl)
                    if sub_r.get("failed"):
                        failed += 1
                    else:
                        success += 1
            else:
                parts = []
                if r.get("created"): parts.append(f"新增 {r['created']}")
                if r.get("updated"): parts.append(f"更新 {r['updated']}")
                if r.get("deleted"): parts.append(f"删除 {r['deleted']}")
                if r.get("failed"): parts.append(f"失败 {r['failed']}")
                msg = "，".join(parts) if parts else "无变化"
                lvl = "ok" if not r.get("failed") else "error"
                tm.add_log(task.task_id, f"{'OK' if lvl=='ok' else 'X'} {label}: {msg}", lvl)
                if r.get("failed"):
                    failed += 1
                else:
                    success += 1
            tm.update(task.task_id, success=success, failed=failed)
        except Exception as e:
            failed += 1
            tm.add_log(task.task_id, f"X {label}: {e}", "error")
            tm.update(task.task_id, success=success, failed=failed)
    tm.add_log(task.task_id, f"完成: 成功 {success} 失败 {failed}", "info")


@app.post("/api/collect/detail")
async def api_collect_detail(links: str = Form("")):
    """单品采集"""
    if not links.strip():
        return JSONResponse({"success": False, "message": "请输入链接"}, status_code=400)

    c = get_collector()
    h = get_history()
    results = []

    for link in links.strip().split("\n"):
        link = link.strip()
        if not link:
            continue
        platform = detect_platform(link)
        if not platform:
            results.append({"link": link, "status": "failed", "message": "无法识别平台"})
            continue
        r = await c.collect_single_detail(link, platform)
        results.append({
            "link": link,
            "platform": platform,
            "status": r.status,
            "works_count": r.works_count,
            "message": r.message,
        })
        h.add_record({
            "account_name": "单品采集",
            "platform": platform,
            "works_count": r.works_count,
            "success_count": r.works_count if r.status == "success" else 0,
            "fail_count": 0 if r.status == "success" else 1,
            "started_at": r.started_at,
            "finished_at": r.finished_at,
            "duration_seconds": r.duration,
            "status": r.status,
            "error_message": r.message if r.status == "failed" else "",
        })

    return {"success": True, "results": results}


SINGLE_WORK_TEMPLATE_FIELDS = {"create_time", "author", "title", "id", "type", "platform"}


def _single_work_preferences() -> dict:
    """单作品偏好：优先从存储方案列表生成（存储方案为唯一数据源，兼容旧前端视图）。"""
    state = sp.ensure_migrated(config)
    single_state = state.get("single") or {}
    profiles = sp.get_profiles(single_state)
    default_nfmt = (single_state.get("default_name_format") or "").strip() or "{create_time} {author} {title}"
    primary = next((p for p in profiles if p.get("role") == "primary"), None) or (profiles[0] if profiles else None)
    active_path = (primary.get("path") or "").strip() if primary else ""
    if not active_path:
        active_path = (config.single_work.get("download_path") or "").strip()
    templates = []
    for i, p in enumerate(profiles):
        templates.append({
            "id": p.get("id") or f"p{i}",
            "name": (p.get("name") or "未命名").strip(),
            "template": (p.get("name_format") or "").strip() or default_nfmt,
            "download_path": (p.get("path") or "").strip(),
            "is_default": p.get("role") == "primary",
            "created_at": "2026-08-16 00:00:00",
            "updated_at": "2026-08-16 00:00:00",
        })
    return {
        "download_path": active_path,
        "recent_dirs": (config.single_work.get("recent_dirs") or []) or [],
        "default_template_id": (primary.get("id") or "default") if primary else "default",
        "folder_mode": bool((config.single_work.get("folder_mode") or False)),
        "templates": templates,
    }


def _save_single_work_preferences(data: dict) -> dict:
    import time

    prefs = {
        "download_path": str(data.get("download_path") or ""),
        "recent_dirs": [],
        "default_template_id": str(data.get("default_template_id") or ""),
        "folder_mode": bool(data.get("folder_mode", False)),
        "templates": [],
    }

    seen_dirs = set()
    for path in data.get("recent_dirs") or []:
        p = str(path)
        if p and p not in seen_dirs:
            seen_dirs.add(p)
            prefs["recent_dirs"].append(p)
    prefs["recent_dirs"] = prefs["recent_dirs"][:10]

    existing = config.single_work.get("templates", []) or []
    existing_map = {t.get("id"): t for t in existing if t.get("id")}
    for tpl in data.get("templates") or []:
        name = str(tpl.get("name") or "").strip()
        if not name:
            raise ValueError("模板名称不能为空")
        template = str(tpl.get("template") or "")
        try:
            unsafe = _is_unsafe_filename_template(template)
        except (AttributeError, KeyError, IndexError, ValueError):
            raise ValueError("命名模板格式无效")
        if unsafe:
            raise ValueError("命名模板不能包含路径分隔符或绝对路径")
        tpl_id = str(tpl.get("id") or "")
        if not tpl_id or tpl_id == "new":
            tpl_id = f"tpl_{int(time.time() * 1000)}"
        now = "2026-08-16 00:00:00"
        old = existing_map.get(tpl_id, {})
        # 每个模板可携带自己的下载目录（空则用全局默认）
        tpl_dir = str(tpl.get("download_path") or "").strip()
        prefs["templates"].append({
            "id": tpl_id,
            "name": name,
            "template": template,
            "download_path": tpl_dir,
            "is_default": bool(tpl.get("is_default")),
            "created_at": old.get("created_at") or now,
            "updated_at": now,
        })
    if not prefs["templates"]:
        prefs["templates"].append({
            "id": "default",
            "name": "默认模板",
            "template": "{create_time} {author} {title}",
            "download_path": "",
            "is_default": True,
            "created_at": "2026-08-16 00:00:00",
            "updated_at": "2026-08-16 00:00:00",
        })
    if not prefs["default_template_id"] or not any(
        t["id"] == prefs["default_template_id"] for t in prefs["templates"]
    ):
        prefs["default_template_id"] = prefs["templates"][0]["id"]
    for tpl in prefs["templates"]:
        tpl["is_default"] = tpl["id"] == prefs["default_template_id"]
    config.set("single_work", prefs)
    config.save()
    return _single_work_preferences()


@app.get("/api/collection/single-work/preferences")
async def api_get_single_work_preferences():
    return {"preferences": _single_work_preferences()}


@app.put("/api/collection/single-work/preferences")
async def api_save_single_work_preferences(request: Request):
    try:
        prefs = _save_single_work_preferences(await request.json())
    except ValueError as error:
        return JSONResponse(
            {"success": False, "message": str(error)}, status_code=400
        )
    return {"success": True, "message": "单作品偏好已保存", "preferences": prefs}


async def _download_single_work_and_record(
    db: Database,
    client: httpx.AsyncClient,
    ttd_url: str,
    link: str,
    platform: str,
    target_dir: Path,
    template: str,
    filename_override: str = "",
    asset_indexes=None,
    include_music: bool = False,
    include_static_cover: bool = False,
    include_dynamic_cover: bool = False,
    folder_mode: bool = False,
    old_history: dict | None = None,
    work: dict | None = None,
) -> dict:
    source_link = (
        old_history.get("source_link", link) if old_history else link
    )
    history_id = db.create_single_work_history(
        work_id=str(work.get("id", "") if work else ""),
        source_link=source_link,
        platform=platform,
        work_type=str(work.get("type", "") if work else ""),
        title=str(work.get("title", "") if work else ""),
        author=str(work.get("author", "") if work else ""),
        filename_template=template,
        filename_override=filename_override,
        target_dir=str(target_dir),
        request_json=json.dumps({
            "filename_template": template,
            "filename_override": filename_override,
            "asset_indexes": asset_indexes or [],
            "include_music": include_music,
            "include_static_cover": include_static_cover,
            "include_dynamic_cover": include_dynamic_cover,
            "folder_mode": folder_mode,
        }),
    )
    try:
        if work is None:
            work = await single_work.fetch_work(client, ttd_url, link, platform)
        # 单作品优先：下载期间冻结批量子进程，结束自动恢复
        _bm = get_collection_batch_manager()
        _bm.suspend_for_single_work()
        try:
            paths = await single_work.download_work(
                client,
                work,
                target_dir,
                template,
                filename_override=filename_override,
                asset_indexes=asset_indexes,
                include_music=include_music,
                include_static_cover=include_static_cover,
                include_dynamic_cover=include_dynamic_cover,
                folder_mode=folder_mode,
            )
        finally:
            _bm.resume_after_single_work()
        db.update_single_work_history(
            history_id,
            status="success",
            files_json=json.dumps([str(p) for p in paths]),
            work_json=json.dumps(work, ensure_ascii=False),
        )
        return {
            "link": source_link,
            "status": "success",
            "title": work.get("title", ""),
            "files": [str(path) for path in paths],
            "history_id": history_id,
        }
    except Exception as error:
        db.update_single_work_history(
            history_id, status="failed", error=str(error)
        )
        return {
            "link": source_link,
            "status": "failed",
            "message": str(error),
            "history_id": history_id,
        }


@app.post("/api/collection/works/resolve")
async def api_resolve_single_works(request: SingleWorkResolveRequest):
    links = _extract_single_work_links(request.links)
    if not links:
        return JSONResponse(
            {"success": False, "message": "未识别到抖音或 TikTok 作品链接"},
            status_code=400,
        )
    resolve_mode = getattr(request, "resolve_mode", "auto") or "auto"
    client = get_single_work_client()
    ttd_url = f"http://127.0.0.1:{config.ttd_port}"
    db = get_database()
    cookie_list = get_cookie_list()
    # 单作品优先：解析期间冻结批量子进程
    _bm = get_collection_batch_manager()
    _bm.suspend_for_single_work()
    works = []
    errors = []
    try:
        for i, (link, platform) in enumerate(links):
            try:
                cookie = cookie_list[i % len(cookie_list)] if cookie_list else ""
                works.append(
                    await single_work.fetch_work(client, ttd_url, link, platform, cookie, mode=resolve_mode)
                )
            except Exception as error:
                errors.append({"link": link, "message": str(error)})
    finally:
        _bm.resume_after_single_work()
    return {"success": bool(works), "works": works, "errors": errors}


@app.post("/api/collection/works/resolve-stream")
async def api_resolve_single_works_stream(request: SingleWorkResolveRequest):
    """SSE 流式解析，逐个推送解析进度"""
    links = _extract_single_work_links(request.links)
    if not links:
        return JSONResponse(
            {"success": False, "message": "未识别到抖音或 TikTok 作品链接"},
            status_code=400,
        )
    resolve_mode = getattr(request, "resolve_mode", "auto") or "auto"
    client = get_single_work_client()
    ttd_url = f"http://127.0.0.1:{config.ttd_port}"
    total = len(links)
    # 从数据库获取 Cookie
    db = get_database()
    cookie_list = get_cookie_list()

    async def resolve_stream():
        import asyncio as _aio
        # 单作品优先：解析期间冻结批量子进程，流结束（含客户端断开）自动恢复
        _bm = get_collection_batch_manager()
        _bm.suspend_for_single_work()
        try:
            mode_label = {"auto": "自动 (API+TTD)", "api": "仅 API", "ttd": "仅 TTD"}.get(resolve_mode, resolve_mode)
            yield f"data: {json.dumps({'type': 'start', 'total': total, 'mode': resolve_mode, 'message': f'开始解析 {total} 个链接 · 模式: {mode_label}' + ('' if cookie_list else '（警告：无可用 Cookie，可能解析失败）')})}\n\n"
            works = []
            success_count = 0
            failed_count = 0
            for i, (link, platform) in enumerate(links):
                idx = i + 1
                yield f"data: {json.dumps({'type': 'progress', 'phase': 'start', 'index': idx, 'total': total, 'message': f'[{idx}/{total}] 正在解析: {link[:60]}'})}\n\n"

                stage_queue: _aio.Queue = _aio.Queue()

                async def _stage_callback(stage: str, message: str):
                    await stage_queue.put(("stage", stage, message))

                cookie = cookie_list[i % len(cookie_list)] if cookie_list else ""
                fetch_task = _aio.create_task(
                    single_work.fetch_work(client, ttd_url, link, platform, cookie, mode=resolve_mode, on_stage=_stage_callback)
                )

                # Consume stage events while fetch_work is running
                done = False
                while not done:
                    try:
                        item = await _aio.wait_for(stage_queue.get(), timeout=0.05)
                        _, stage, message = item
                        yield f"data: {json.dumps({'type': 'progress', 'phase': 'stage', 'stage': stage, 'index': idx, 'total': total, 'message': f'[{idx}/{total}] {message}'})}\n\n"
                    except _aio.TimeoutError:
                        if fetch_task.done():
                            done = True
                            # Drain remaining items
                            while not stage_queue.empty():
                                item = stage_queue.get_nowait()
                                _, stage, message = item
                                yield f"data: {json.dumps({'type': 'progress', 'phase': 'stage', 'stage': stage, 'index': idx, 'total': total, 'message': f'[{idx}/{total}] {message}'})}\n\n"

                try:
                    work = await fetch_task
                    works.append(work)
                    success_count += 1
                    work_title = work.get('title', '')
                    yield f"data: {json.dumps({'type': 'progress', 'phase': 'done', 'index': idx, 'total': total, 'success_count': success_count, 'failed_count': failed_count, 'work': work, 'message': f'[{idx}/{total}] 解析成功: {work_title[:40]}'})}\n\n"
                except Exception as error:
                    failed_count += 1
                    yield f"data: {json.dumps({'type': 'progress', 'phase': 'failed', 'index': idx, 'total': total, 'success_count': success_count, 'failed_count': failed_count, 'link': link, 'message': f'[{idx}/{total}] 解析失败: {str(error)[:120]}'})}\n\n"
            yield f"data: {json.dumps({'type': 'complete', 'success': success_count > 0, 'total': total, 'success_count': success_count, 'failed_count': failed_count, 'works': works, 'message': f'解析完成: {success_count} 成功, {failed_count} 失败'})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'type': 'complete', 'success': False, 'message': f'异常: {str(e)}'})}\n\n"
        finally:
            _bm.resume_after_single_work()

    return StreamingResponse(
        resolve_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ========== 单作品下载：存储方案接入 ==========

def _resolve_single_target(target_dir_str: str, primary_id: str = "", secondary_id: str = "") -> tuple[Path | None, str | None]:
    """单作品下载目录解析：前端指定目录→直接校验；留空→按主/次方案解析（主失败自动切次）。

    primary_id/secondary_id 为空时用设置页的默认主/次。
    返回 (target, error)。
    """
    target_dir_str = (target_dir_str or "").strip()
    if target_dir_str:
        target = Path(target_dir_str).expanduser()
        if not target.exists() or not target.is_dir():
            return None, "保存目录不存在"
        return target, None
    profile, diagnostics = sp.resolve_pair(config, "single", primary_id, secondary_id)
    if profile is None:
        reasons = "；".join(f"{d.get('name', '?')}: {d.get('reason', '不可用')}" for d in diagnostics)
        return None, f"主/次存储方案均不可用（{reasons}）"
    target = Path(profile["path"]).expanduser()
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return None, f"目录不可创建: {exc}"
    return target, None


def _resolve_single_template(template_str: str, profile: dict | None = None) -> str:
    """单作品命名模板：前端指定→用前端；否则用生效方案/该套默认。"""
    template_str = (template_str or "").strip()
    if template_str:
        return template_str
    return sp.resolve_name_format(config, "single", profile)


@app.post("/api/collection/works/download")
async def api_download_single_works(request: SingleWorkDownloadRequest):
    filename_template = _resolve_single_template(request.filename_template)
    try:
        unsafe_template = _is_unsafe_filename_template(filename_template)
    except (AttributeError, KeyError, IndexError, ValueError):
        return JSONResponse(
            {"success": False, "message": "命名模板格式无效"},
            status_code=400,
        )
    if unsafe_template:
        return JSONResponse(
            {"success": False, "message": "命名模板不能包含路径分隔符或绝对路径"},
            status_code=400,
        )
    links = _extract_single_work_links(request.links)
    if not links:
        return JSONResponse(
            {"success": False, "message": "未识别到抖音或 TikTok 作品链接"},
            status_code=400,
        )
    target, target_err = _resolve_single_target(request.target_dir, request.storage_primary_id, request.storage_secondary_id)
    if target is None:
        return JSONResponse(
            {"success": False, "message": target_err},
            status_code=400,
        )

    db = get_database()
    client = get_single_work_client()
    ttd_url = f"http://127.0.0.1:{config.ttd_port}"
    cookie_list = get_cookie_list()
    results = []
    for i, (link, platform) in enumerate(links):
        work = request.work  # 如果前端传入了已解析的 work，直接使用
        if work is None:
            try:
                cookie = cookie_list[i % len(cookie_list)] if cookie_list else ""
                work = await single_work.fetch_work(client, ttd_url, link, platform, cookie)
            except Exception as error:
                result = await _download_single_work_and_record(
                    db, client, ttd_url, link, platform, target,
                    filename_template,
                    work=work,
                )
                result["message"] = str(error)
                results.append(result)
                continue
        override = request.filename_overrides.get(work.get("id", ""), "")
        results.append(
            await _download_single_work_and_record(
                db, client, ttd_url, link, platform, target,
                filename_template,
                filename_override=override,
                asset_indexes=request.asset_indexes,
                include_music=request.include_music,
                include_static_cover=request.include_static_cover,
                include_dynamic_cover=request.include_dynamic_cover,
                folder_mode=request.folder_mode,
                work=work,
            )
        )
    return {
        "success": any(item["status"] == "success" for item in results),
        "results": results,
    }


@app.post("/api/collection/works/download-task")
async def api_download_single_works_task(request: SingleWorkDownloadRequest):
    """单作品后台下载：创建历史记录并入队，立即返回"""
    filename_template = _resolve_single_template(request.filename_template)
    try:
        unsafe_template = _is_unsafe_filename_template(filename_template)
    except (AttributeError, KeyError, IndexError, ValueError):
        return JSONResponse(
            {"success": False, "message": "命名模板格式无效"},
            status_code=400,
        )
    if unsafe_template:
        return JSONResponse(
            {"success": False, "message": "命名模板不能包含路径分隔符或绝对路径"},
            status_code=400,
        )
    links = _extract_single_work_links(request.links)
    if not links:
        return JSONResponse(
            {"success": False, "message": "未识别到抖音或 TikTok 作品链接"},
            status_code=400,
        )
    target, target_err = _resolve_single_target(request.target_dir, request.storage_primary_id, request.storage_secondary_id)
    if target is None:
        return JSONResponse(
            {"success": False, "message": target_err},
            status_code=400,
        )

    db = get_database()
    worker = get_download_worker()
    history_ids = []
    for link, platform in links:
        history_id = db.create_single_work_history(
            work_id="",
            source_link=link,
            platform=platform,
            work_type="",
            title="",
            author="",
            filename_template=filename_template,
            filename_override="",
            target_dir=str(target),
            request_json=json.dumps({
                "filename_template": filename_template,
                "asset_indexes": request.asset_indexes or [],
                "include_music": request.include_music,
                "include_static_cover": request.include_static_cover,
                "include_dynamic_cover": request.include_dynamic_cover,
                "folder_mode": request.folder_mode,
            }),
            status="pending",
        )
        history_ids.append(history_id)
        worker.enqueue(history_id)

    return {
        "success": True,
        "message": f"已加入后台下载，共 {len(history_ids)} 个作品",
        "history_ids": history_ids,
        "total": len(history_ids),
    }

@app.post("/api/collection/works/download-stream")
async def api_download_single_works_stream(request: SingleWorkDownloadRequest):
    """一键解析+下载 SSE 流式接口，实时推送解析和下载进度"""
    filename_template = _resolve_single_template(request.filename_template)
    try:
        unsafe_template = _is_unsafe_filename_template(filename_template)
    except (AttributeError, KeyError, IndexError, ValueError):
        return JSONResponse(
            {"success": False, "message": "命名模板格式无效"},
            status_code=400,
        )
    if unsafe_template:
        return JSONResponse(
            {"success": False, "message": "命名模板不能包含路径分隔符或绝对路径"},
            status_code=400,
        )
    links = _extract_single_work_links(request.links)
    if not links:
        return JSONResponse(
            {"success": False, "message": "未识别到抖音或 TikTok 作品链接"},
            status_code=400,
        )
    target, target_err = _resolve_single_target(request.target_dir, request.storage_primary_id, request.storage_secondary_id)
    if target is None:
        return JSONResponse(
            {"success": False, "message": target_err},
            status_code=400,
        )

    db = get_database()
    client = get_single_work_client()
    ttd_url = f"http://127.0.0.1:{config.ttd_port}"
    total = len(links)
    # 从数据库获取 Cookie
    cookie_list = get_cookie_list()

    async def download_stream():
        try:
            yield f"data: {json.dumps({'type': 'start', 'total': total, 'message': f'开始处理 {total} 个作品' + ('' if cookie_list else '（警告：无可用 Cookie，可能解析失败）')})}\n\n"

            success_count = 0
            failed_count = 0
            results = []

            for i, (link, platform) in enumerate(links):
                idx = i + 1
                # 阶段1：解析
                yield f"data: {json.dumps({'type': 'progress', 'phase': 'resolve', 'index': idx, 'total': total, 'message': f'[{idx}/{total}] 正在解析: {link[:60]}'})}\n\n"
                work = None
                try:
                    cookie = cookie_list[i % len(cookie_list)] if cookie_list else ""
                    work = await single_work.fetch_work(client, ttd_url, link, platform, cookie)
                    work_title = work.get('title', '')
                    work_author = work.get('author', '')
                    yield f"data: {json.dumps({'type': 'progress', 'phase': 'resolve_done', 'index': idx, 'total': total, 'success_count': success_count, 'failed_count': failed_count, 'title': work_title, 'author': work_author, 'work': work, 'message': f'[{idx}/{total}] 解析成功: {work_title[:40]}'})}\n\n"
                except Exception as error:
                    failed_count += 1
                    results.append({"link": link, "status": "failed", "message": str(error)})
                    yield f"data: {json.dumps({'type': 'progress', 'phase': 'resolve_failed', 'index': idx, 'total': total, 'success_count': success_count, 'failed_count': failed_count, 'message': f'[{idx}/{total}] 解析失败: {str(error)[:120]}'})}\n\n"
                    continue

                # 阶段2：下载
                yield f"data: {json.dumps({'type': 'progress', 'phase': 'download', 'index': idx, 'total': total, 'message': f'[{idx}/{total}] 正在下载: {work_title[:40]}'})}\n\n"
                override = request.filename_overrides.get(work.get("id", ""), "")
                result = await _download_single_work_and_record(
                    db, client, ttd_url, link, platform, target,
                    filename_template,
                    filename_override=override,
                    asset_indexes=request.asset_indexes,
                    include_music=request.include_music,
                    include_static_cover=request.include_static_cover,
                    include_dynamic_cover=request.include_dynamic_cover,
                    folder_mode=request.folder_mode,
                    work=work,
                )
                results.append(result)
                if result["status"] == "success":
                    success_count += 1
                    files = result.get("files", [])
                    yield f"data: {json.dumps({'type': 'progress', 'phase': 'download_done', 'index': idx, 'total': total, 'success_count': success_count, 'failed_count': failed_count, 'title': result.get('title', ''), 'files': files, 'message': f'[{idx}/{total}] 下载完成: {len(files)} 个文件'})}\n\n"
                else:
                    failed_count += 1
                    result_msg = result.get('message', '')
                    yield f"data: {json.dumps({'type': 'progress', 'phase': 'download_failed', 'index': idx, 'total': total, 'success_count': success_count, 'failed_count': failed_count, 'message': f'[{idx}/{total}] 下载失败: {result_msg[:120]}'})}\n\n"

            # 完成
            yield f"data: {json.dumps({'type': 'complete', 'success': success_count > 0, 'total': total, 'success_count': success_count, 'failed_count': failed_count, 'message': f'完成: {success_count} 成功, {failed_count} 失败'})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'type': 'complete', 'success': False, 'message': f'异常: {str(e)}'})}\n\n"

    return StreamingResponse(
        download_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/collection/works/proxy-download")
async def api_proxy_download(url: str, filename: str = "download"):
    """Proxy-download: fetch from Douyin CDN and stream to browser.

    The browser triggers a file download via Content-Disposition header.
    """
    if not url or not url.startswith("http"):
        return JSONResponse({"success": False, "message": "无效的 URL"}, status_code=400)

    # Guess extension from URL or default to .mp4 for video, .jpg for image
    ext = ""
    for candidate in (".mp4", ".mp3", ".jpg", ".jpeg", ".png", ".webp"):
        if candidate in url.lower():
            ext = candidate
            break
    if not ext:
        ext = ".mp4"  # fallback

    # Sanitize filename
    safe_name = "".join(c for c in filename if c not in '<>:"/\\|?*\x00-\x1f').strip()[:160] or "download"
    download_filename = f"{safe_name}{ext}"

    # Use a standalone client to avoid cookie jar / header pollution
    try:
        async with httpx.AsyncClient(
            timeout=120,
            follow_redirects=True,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36",
                "Referer": "https://www.douyin.com/",
                "Accept": "*/*",
                "Accept-Encoding": "identity",
            },
        ) as proxy_client:
            upstream = await proxy_client.get(url)
            upstream.raise_for_status()
    except httpx.HTTPError as e:
        return JSONResponse({"success": False, "message": f"获取文件失败: {e}"}, status_code=502)

    from starlette.responses import StreamingResponse as StarletteStreaming

    content_type = upstream.headers.get("content-type", "application/octet-stream")
    total_size = int(upstream.headers.get("content-length", 0))

    async def stream_upstream():
        # Re-fetch in streaming mode for large files
        async with httpx.AsyncClient(
            timeout=120,
            follow_redirects=True,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36",
                "Referer": "https://www.douyin.com/",
                "Accept": "*/*",
                "Accept-Encoding": "identity",
            },
        ) as stream_client:
            async with stream_client.stream("GET", url) as resp:
                resp.raise_for_status()
                async for chunk in resp.aiter_bytes(65536):
                    yield chunk

    ascii_filename = download_filename.encode("ascii", "ignore").decode("ascii").strip()
    if not ascii_filename or ascii_filename.startswith("."):
        ascii_filename = f"download{ext}"
    headers_out = {
        "Content-Disposition": (
            f'attachment; filename="{ascii_filename}"; '
            f"filename*=UTF-8''{quote(download_filename, safe='')}"
        )
    }
    if total_size:
        headers_out["Content-Length"] = str(total_size)

    return StarletteStreaming(
        stream_upstream(),
        media_type=content_type,
        headers=headers_out,
    )


class OpenAccountDirRequest(BaseModel):
    table: str = "account_cache"
    record_id: str = ""


@app.post("/api/table/open-account-dir")
async def api_open_account_dir(request: OpenAccountDirRequest):
    """打开账号的下载文件夹（表浏览行操作）。"""
    db = get_database()
    table = request.table if request.table in ("account_cache", "share_cache") else "account_cache"
    with db._connect() as conn:
        r = conn.execute(
            f"SELECT sec_user_id, uid FROM {table} WHERE record_id = ?",
            (request.record_id,),
        ).fetchone()
        if not r:
            return {"success": False, "message": "记录不存在"}
        sec_user_id, uid = str(r[0] or ""), str(r[1] or "")
        if not uid:
            return {"success": False, "message": "该账号还没有数字 UID，请先「回填UID」或获取账号信息"}
        # 首选：作品表最近一条的真实目录（可能指向作品子目录，向上找账号根）
        target: Path | None = None
        w = conn.execute(
            "SELECT download_dir FROM collection_works WHERE sec_user_id = ?"
            " AND download_dir IS NOT NULL AND download_dir != ''"
            " ORDER BY id DESC LIMIT 1",
            (sec_user_id,),
        ).fetchone()
    if w and w[0]:
        d = Path(w[0])
        if d.name.startswith(f"UID{uid}_"):
            target = d
        elif d.parent.name.startswith(f"UID{uid}_"):
            target = d.parent
    # 回退：批量存储方案根目录下按 UID 前缀匹配
    if target is None:
        try:
            from .core import storage_profiles as _sp
            profile, _diag = _sp.resolve_pair(config, "batch", None, None)
            if profile:
                base = Path(str(profile.get("path", ""))).expanduser()
                if base.exists():
                    hits = sorted(base.glob(f"UID{uid}_*"))
                    if hits:
                        target = hits[0]
        except Exception:
            pass
    if target is None or not target.exists():
        return {"success": False, "mode": "copy", "path": str(target or ""),
                "message": f"未找到 UID{uid} 的下载文件夹（路径不在服务器上，可复制路径手动打开）"}
    from .core.os_open import open_folder
    ok, err = open_folder(target)
    if not ok:
        # 无 GUI（Docker）或系统调用失败：把路径交回前端复制兜底
        return {"success": False, "mode": "copy", "path": str(target),
                "message": f"服务器无法直接打开，路径已可复制: {err}"}
    return {"success": True, "mode": "opened", "path": str(target), "message": f"已打开: {target.name}"}


class ResolveShareRowRequest(BaseModel):
    record_id: str = ""


@app.post("/api/table/resolve-share-row")
async def api_resolve_share_row(request: ResolveShareRowRequest):
    """分享表「非主页链接」行的下一步动作：重新解析分享码。

    分享码拼回短链 → 解析出作品 → 取作者 sec_user_id/uid/昵称回填该行，
    解析状态改为「已就绪」（后续可正常生成账号表/采集）。
    """
    db = get_database()
    with db._connect() as conn:
        r = conn.execute(
            "SELECT share_code FROM share_cache WHERE record_id = ?",
            (request.record_id,),
        ).fetchone()
    if not r:
        return {"success": False, "message": "记录不存在"}
    share_code = str(r[0] or "").strip()
    if not share_code:
        return {"success": False, "message": "该行没有 share_code，无法解析"}
    link = share_code if share_code.startswith("http") else f"https://v.douyin.com/{share_code}/"
    client = get_single_work_client()
    cookie_list = get_cookie_list()
    cookie = cookie_list[0] if cookie_list else ""
    try:
        work = await single_work.fetch_work(
            client, f"http://127.0.0.1:{config.ttd_port}", link, "douyin", cookie, mode="auto"
        )
    except Exception as e:
        # 区分直播分享链接（webcast.amemv.com）：无法解析为作品账号，标记暂不支持
        try:
            resp = await client.get(link, follow_redirects=True)
            if "webcast.amemv.com" in str(resp.url) or "/webcast/reflow/" in str(resp.url):
                db.update_collection(request.record_id, {"解析状态": "暂不支持"})
                return {"success": False, "message": "这是直播分享链接，无法解析为作品账号（已标记「暂不支持」）"}
        except Exception:
            pass
        return {"success": False, "message": f"解析失败: {str(e)[:150]}"}
    author_info = work.get("author_info") or {}
    sec = str(author_info.get("sec_uid") or "")
    uid = str(author_info.get("uid") or "")
    nickname = str(work.get("author") or author_info.get("nickname") or "")
    if not sec:
        return {"success": False, "message": "未获取到作者主页信息（作品可能已删除或私密）"}
    try:
        db.update_collection(request.record_id, {
            "sec_user_id": sec,
            "uid": uid,
            "账号名称": nickname,
            "解析状态": "已就绪",
        })
    except Exception as e:
        return {"success": False, "message": f"回填失败: {e}"}
    return {
        "success": True,
        "message": f"已识别作者「{nickname}」并回填（解析状态 → 已就绪）",
        "home_url": f"https://www.douyin.com/user/{sec}",
        "work_url": link,
    }


class QuickAddShareRequest(BaseModel):
    sec_user_id: str
    platform: str = "douyin"
    account_name: str = ""
    rating: int = 3
    tags: list[str] = []
    source_link: str = ""
    note: str = "从单作品采集录入"


@app.post("/api/collection/quick-add-share")
async def api_quick_add_share(request: QuickAddShareRequest):
    """从单作品采集快捷录入分享表（share_cache）"""
    if not request.sec_user_id:
        return JSONResponse({"success": False, "message": "缺少 sec_user_id"}, status_code=400)
    db = get_database()
    import json as _json
    from datetime import datetime as _dt

    # 检查是否已存在（按 sec_user_id 去重）
    existing = db.get_collection_by_sec_user_id(request.sec_user_id)

    if existing:
        # 已存在：合并等级（取高的）和标签
        old_rating = existing.get("等级", 3) or 3
        merged_rating = max(old_rating, request.rating)
        old_tags_str = existing.get("标签", "[]") or "[]"
        try:
            old_tags = _json.loads(old_tags_str) if old_tags_str else []
        except (ValueError, TypeError):
            old_tags = []
        merged_tags = list(set(old_tags + request.tags))
        db.update_collection(existing["record_id"], {
            "等级": merged_rating,
            "标签": _json.dumps(merged_tags, ensure_ascii=False),
        })
        return {
            "success": True,
            "message": "已更新现有分享表记录（合并等级和标签）",
            "action": "updated",
        }

    # 新增
    record_id = f"rec_{_dt.now().strftime('%Y%m%d%H%M%S')}"
    insert_data = {
        "record_id": record_id,
        "share_code": request.sec_user_id,
        "平台": request.platform,
        "等级": request.rating,
        "标签": _json.dumps(request.tags, ensure_ascii=False),
        "sec_user_id": request.sec_user_id,
        "解析状态": "已就绪",
        "账号名称": request.account_name,
        "备注": request.note,
    }
    try:
        db.insert_collection(insert_data)
    except Exception as e:
        return JSONResponse(
            {"success": False, "message": f"录入失败（可能已存在）: {e}"},
            status_code=409,
        )
    return {
        "success": True,
        "message": "已录入分享表，可前往「生成账号表」获取详情",
        "action": "created",
    }


@app.post("/api/collection/works/history/{history_id}/open-dir")
async def api_open_history_dir(history_id: int):
    """打开下载历史的保存目录"""
    db = get_database()
    row = db.get_single_work_history(history_id)
    if not row:
        return {"success": False, "message": "记录不存在"}
    target_dir = row.get("target_dir") or ""
    if not target_dir:
        return {"success": False, "message": "该记录无保存目录"}
    import subprocess
    import sys
    import os
    if not os.path.isdir(target_dir):
        return {"success": False, "message": f"目录不存在: {target_dir}"}
    try:
        if sys.platform == "win32":
            os.startfile(target_dir)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", target_dir])
        else:
            subprocess.Popen(["xdg-open", target_dir])
        return {"success": True}
    except Exception as e:
        return {"success": False, "message": str(e)}


@app.get("/api/collection/works/history")
async def api_list_single_work_history(limit: int = 50):
    db = get_database()
    return {"history": db.list_single_work_history(limit=limit)}


@app.post("/api/collection/works/history/{history_id}/retry")
async def api_retry_single_work_history(history_id: int, request: SingleWorkRetryRequest):
    db = get_database()
    old = db.get_single_work_history(history_id)
    if not old:
        return JSONResponse(
            {"success": False, "message": "历史记录不存在"},
            status_code=404,
        )
    link = old.get("source_link") or ""
    platform = single_work.detect_single_platform(link) or old.get("platform", "")
    target, target_err = _resolve_single_target(
        request.target_dir or old.get("target_dir") or "",
        request.storage_primary_id,
        request.storage_secondary_id,
    )
    if target is None:
        return JSONResponse(
            {"success": False, "message": target_err},
            status_code=400,
        )
    template = request.filename_template or old.get("filename_template") or _resolve_single_template("")
    try:
        unsafe_template = _is_unsafe_filename_template(template)
    except (AttributeError, KeyError, IndexError, ValueError):
        return JSONResponse(
            {"success": False, "message": "命名模板格式无效"},
            status_code=400,
        )
    if unsafe_template:
        return JSONResponse(
            {"success": False, "message": "命名模板不能包含路径分隔符或绝对路径"},
            status_code=400,
        )

    old_request = {}
    try:
        old_request = json.loads(old.get("request_json") or "{}")
    except (json.JSONDecodeError, TypeError):
        old_request = {}
    filename_override = request.filename_override
    if filename_override is None:
        filename_override = old.get("filename_override") or old_request.get("filename_override", "")
    asset_indexes = request.asset_indexes
    if asset_indexes is None:
        asset_indexes = old_request.get("asset_indexes", [])
    include_music = old_request.get("include_music", False)
    include_static_cover = old_request.get("include_static_cover", False)
    include_dynamic_cover = old_request.get("include_dynamic_cover", False)
    folder_mode = old_request.get("folder_mode", False)

    client = get_single_work_client()
    ttd_url = f"http://127.0.0.1:{config.ttd_port}"
    result = await _download_single_work_and_record(
        db, client, ttd_url, link, platform, target, template,
        filename_override=filename_override,
        asset_indexes=asset_indexes,
        include_music=include_music,
        include_static_cover=include_static_cover,
        include_dynamic_cover=include_dynamic_cover,
        folder_mode=folder_mode,
        old_history=old,
    )
    return {"success": result["status"] == "success", "results": [result]}


# ========== 存储方案（单作品/增量）==========

@app.get("/api/collection/storage")
async def api_get_storage_profiles():
    """获取两套存储方案（single/batch）+ 各自使用方式与默认命名。"""
    state = sp.ensure_migrated(config)
    return {"success": True, **state}


@app.put("/api/collection/storage")
async def api_save_storage_profiles(request: Request):
    """保存两套存储方案（设置页整体提交）。"""
    data = await request.json()
    existing = sp.ensure_migrated(config)
    allowed_empty = set(data.get("allow_empty_scopes") or [])
    empty_scopes = []
    scope_names = {"single": "单作品", "batch": "增量采集"}
    for scope in ("single", "batch"):
        incoming = data.get(scope)
        old_profiles = (existing.get(scope) or {}).get("profiles") or []
        if (
            old_profiles
            and isinstance(incoming, dict)
            and incoming.get("profiles") == []
            and scope not in allowed_empty
        ):
            empty_scopes.append(scope)

    if empty_scopes:
        names = "、".join(scope_names[scope] for scope in empty_scopes)
        return JSONResponse(
            {
                "success": False,
                "message": f"检测到{names}的存储方案将被清空，请确认这是有意操作",
                "empty_scopes": empty_scopes,
            },
            status_code=409,
        )

    try:
        state = sp.save_state(config, data)
    except ValueError as error:
        return JSONResponse({"success": False, "message": str(error)}, status_code=400)
    return {"success": True, **state}


@app.post("/api/collection/storage/check")
async def api_check_storage_paths(request: Request):
    """探测一组路径可用性（本地=存在且可写；远程=带超时可达性）。"""
    data = await request.json()
    items = data.get("items") or []
    timeout = float(data.get("timeout") or 3.0)
    results = {}
    for item in items:
        path = str(item.get("path") or "").strip()
        ok, reason, exists = sp.check_path(path, timeout, create=False)
        results[item.get("id", path)] = {"ok": ok, "reason": reason, "exists": exists}
    return {"success": True, "results": results}


@app.put("/api/collection/storage/profile/{profile_id}")
async def api_update_storage_profile(profile_id: str, request: Request):
    """就地更新单个存储方案（采集页可视化编辑命名等场景）。
    body: { "scope": "single"|"batch", "patch": { "name_format": "...", ... } }"""
    data = await request.json()
    scope = str(data.get("scope") or "").strip().lower()
    patch = data.get("patch") or {}
    if scope not in ("single", "batch"):
        return JSONResponse({"success": False, "message": "scope 无效"}, status_code=400)
    if not isinstance(patch, dict):
        return JSONResponse({"success": False, "message": "patch 必须是对象"}, status_code=400)
    updated = sp.update_profile(config, scope, profile_id, patch)
    if updated is None:
        return JSONResponse({"success": False, "message": "未找到该方案或更新失败"}, status_code=404)
    return {"success": True, "profile": updated,
            "state": sp.ensure_migrated(config)}


# ========== 采集方案（预设）==========

class CollectionPresetRequest(BaseModel):
    name: str
    rating_min: int = 3
    tags: str = ""
    account_names: str = ""
    platform: Literal["douyin", "tiktok", "all"] = "douyin"
    mode: Literal["incremental", "full"] = "incremental"
    folder_name: str = ""
    name_format: str = ""
    storage_primary_id: str = ""   # 主方案 ID（空=设置页默认主）
    storage_secondary_id: str = ""  # 次方案 ID（空=设置页默认次）
    storage_choice: str = "auto"  # 旧版兼容字段：p:<id> 自动迁移为主方案 ID
    account_created_after: str = ""
    skip_recent_days: int = 0
    is_default: bool = False


@app.get("/api/collection/presets")
async def api_list_collection_presets():
    return {"presets": presets.list_presets(config)}


@app.post("/api/collection/presets")
async def api_create_collection_preset(request: CollectionPresetRequest):
    data = request.model_dump()
    # 旧版 storage_choice=p:<id> 兼容 → 迁移为主方案 ID
    if not data.get("storage_primary_id") and str(data.get("storage_choice") or "").startswith("p:"):
        data["storage_primary_id"] = data["storage_choice"][2:]
    preset = presets.create_preset(config, data)
    return {"success": True, "preset": preset}


@app.put("/api/collection/presets/{preset_id}")
async def api_update_collection_preset(preset_id: int, request: CollectionPresetRequest):
    data = request.model_dump()
    if not data.get("storage_primary_id") and str(data.get("storage_choice") or "").startswith("p:"):
        data["storage_primary_id"] = data["storage_choice"][2:]
    preset = presets.update_preset(config, preset_id, data)
    if not preset:
        return JSONResponse({"success": False, "message": "方案不存在"}, status_code=404)
    return {"success": True, "preset": preset}


@app.delete("/api/collection/presets/{preset_id}")
async def api_delete_collection_preset(preset_id: int):
    preset = presets.get_preset(config, preset_id)
    if preset and preset.get("is_default"):
        return JSONResponse({"success": False, "message": "不能删除默认方案"}, status_code=400)
    ok = presets.delete_preset(config, preset_id)
    return {"success": ok}


@app.post("/api/collection/presets/{preset_id}/default")
async def api_set_default_collection_preset(preset_id: int):
    ok = presets.set_default_preset(config, preset_id)
    return {"success": ok}


@app.get("/api/collection/defaults")
async def api_get_collection_defaults():
    """获取增量采集的全局默认设置"""
    return {"defaults": config.collection_defaults}


@app.put("/api/collection/defaults")
async def api_save_collection_defaults(request: Request):
    """保存增量采集的全局默认设置（含引擎参数）"""
    data = await request.json()
    folder_name = str(data.get("folder_name") or "").strip()
    name_format = str(data.get("name_format") or "").strip()
    config._data["collection_defaults"] = {
        "folder_name": folder_name,
        "name_format": name_format,
        # 固化参数
        "split": "-",
        "date_format": "%Y%m%d_%H%M%S",
        # 引擎级参数（设置页可配）
        "folder_mode": bool(data.get("folder_mode", False)),
        "music": bool(data.get("music", False)),
        "dynamic_cover": bool(data.get("dynamic_cover", False)),
        "static_cover": bool(data.get("static_cover", False)),
        "max_size": int(data.get("max_size") or 0),
        "storage_format": str(data.get("storage_format") or ""),
    }
    config.save()
    return {"success": True, "defaults": config.collection_defaults}


@app.post("/api/collection/presets/{preset_id}/preview")
async def api_preview_collection_preset(preset_id: str):
    """根据方案 ID 预览采集范围统计。"""
    db = get_database()
    # preset_id 可以是 "new"（未保存的新方案），此时不做 DB 查询
    if preset_id == "new":
        return JSONResponse(
            {"success": False, "message": "新方案尚未保存，请先保存后再预览"},
            status_code=400,
        )
    try:
        pid = int(preset_id)
    except ValueError:
        return JSONResponse({"success": False, "message": "无效的方案 ID"}, status_code=400)
    preset = presets.get_preset(config, pid)
    if not preset:
        return JSONResponse({"success": False, "message": "方案不存在"}, status_code=404)

    accounts = db.get_all_accounts()
    platforms = (
        ("douyin", "tiktok") if preset["platform"] == "all" else (preset["platform"],)
    )
    totals = {
        "total_accounts": 0,
        "incremental_accounts": 0,
        "first_run_accounts": 0,
        "skipped_accounts": 0,
    }
    for platform in platforms:
        planned = plan_collection(
            accounts=accounts,
            rating_min=preset["rating_min"],
            tags=preset["tags"].split(",") if preset["tags"] else [],
            account_names=preset.get("account_names", ""),
            platform=platform,
            mode=preset["mode"],
            created_after=_parse_preset_date(preset.get("account_created_after", "")),
            skip_recent_days=int(preset.get("skip_recent_days", 0)),
        )
        if not planned:
            continue
        skipped = sum(item.status == "skipped" for item in planned)
        first_run = sum(
            item.status == "pending" and item.earliest == "" for item in planned
        )
        incremental = sum(
            item.status == "pending" and item.earliest != "" for item in planned
        )
        totals["total_accounts"] += len(planned)
        totals["incremental_accounts"] += incremental
        totals["first_run_accounts"] += first_run
        totals["skipped_accounts"] += skipped

    if totals["total_accounts"] == 0:
        return JSONResponse(
            {"success": False, "message": "没有符合条件的账号"},
            status_code=400,
        )
    return {"success": True, **totals}


# ========== 定时任务 ==========

class ScheduleTaskRequest(BaseModel):
    name: str
    preset_id: int
    cron_expression: str
    enabled: bool = True
    sync_before: bool = False


# 定时采集两条红线：
# - 最短间隔 1 小时（硬拦）。低于此值等于高频轮询平台接口，极易触发风控。
# - 建议间隔 2 小时（只提示不拦）。前端给温和提醒，后端不拒绝。
SCHEDULE_MIN_INTERVAL_MINUTES = 60


def _expand_cron_field(field, low: int, high: int) -> list[int] | None:
    """把一个 cron 字段展开成它可能取到的所有整数值。

    apscheduler 的字段对象不提供 "列出所有取值" 的 API（只有 get_next_value 这种
    逐步推进的接口），所以这里直接读它编译好的 expressions：
    - AllExpression     → `*` 或 `*/n`，值是 low..high 上步长为 n 的等差数列
    - RangeExpression   → `a`、`a-b`、`a-b/n`、`a/n`
    展开不了（含月份名、星期名等）时返回 None，由调用方放弃判断。
    """
    values: set[int] = set()
    for e in getattr(field, "expressions", []) or []:
        step = getattr(e, "step", None) or 1
        first = getattr(e, "first", None)
        last = getattr(e, "last", None)
        if first is None:
            # AllExpression：从范围下界开始的等差数列
            values.update(range(low, high + 1, step))
        else:
            lo = max(low, int(first))
            hi = min(high, int(last)) if last is not None else high
            if lo > hi:
                return None
            values.update(range(lo, hi + 1, step))
    return sorted(values) if values else None


def _estimate_min_interval_minutes(expr: str) -> float | None:
    """估算 cron 表达式的「最小触发间隔」（分钟），无法判断时返回 None。

    只处理常见的 5 段 cron（分 时 日 月 周）。判定思路：
    把「时」「分」两个字段展开成一天内的触发时刻列表，取相邻两次的最小间隔
    （含跨零点回到首个的那一段）。

    这些情况直接返回 None（视为无需限速）：
    - 不是 5 段表达式 / 无法解析
    - 「日」或「周」字段有限制（如每周一、每月 1 号）→ 一天最多触发一次
    - 一天只触发一次
    """
    parts = expr.split()
    if len(parts) != 5:
        return None
    _minute_f, _hour_f, dom_f, _mon_f, dow_f = parts
    # 只有"每天都可能触发"的任务才需要限速
    if dom_f != "*" or dow_f != "*":
        return None

    try:
        from apscheduler.triggers.cron import CronTrigger
        trigger = CronTrigger.from_crontab(expr)
    except Exception:
        return None

    fields = {f.name: f for f in trigger.fields}
    minutes = _expand_cron_field(fields["minute"], 0, 59)
    hours = _expand_cron_field(fields["hour"], 0, 23)
    if not minutes or not hours:
        return None

    # 展开成一天内的分钟偏移，排序后取最小差值（含跨零点）
    offsets = sorted(h * 60 + m for h in hours for m in minutes)
    if len(offsets) < 2:
        return None
    gaps = [offsets[i + 1] - offsets[i] for i in range(len(offsets) - 1)]
    gaps.append(1440 - offsets[-1] + offsets[0])  # 跨零点回到首个
    return float(min(gaps))


def _validate_schedule_expr(expr: str) -> str:
    """校验调度表达式，返回错误信息；合法返回空串。

    支持两种：
    - 标准 5 段 cron（如 `0 2 * * *`）
    - 一次性 `@once:YYYY-MM-DD HH:MM`（跑完自动停用）
    """
    from app.core.scheduler import parse_once_at, build_trigger

    if not expr:
        return "执行计划不能为空"
    if expr.startswith("@once:"):
        once_at = parse_once_at(expr)
        if once_at is None:
            return "一次性执行的时间格式不正确"
        if once_at <= datetime.now():
            return "一次性执行的时间必须晚于当前时间"
        return ""
    try:
        from apscheduler.triggers.cron import CronTrigger
        CronTrigger.from_crontab(expr)
    except Exception:
        return "无效的 cron 表达式"
    # 频率下限：一天内多次触发时，相邻两次不能靠得太近
    gap = _estimate_min_interval_minutes(expr)
    if gap is not None and gap < SCHEDULE_MIN_INTERVAL_MINUTES:
        return (
            f"任务间隔不能短于 {SCHEDULE_MIN_INTERVAL_MINUTES} 分钟"
            f"（当前最短间隔约 {int(gap)} 分钟），过于频繁会触发平台风控"
        )
    return ""


@app.get("/api/schedules")
async def api_list_schedules():
    """获取所有定时任务"""
    db = get_database()
    tasks = db.get_scheduled_tasks()
    # 附加 preset_name 和 next_run_at（从调度器获取）
    sched = get_scheduler()
    all_presets = presets.list_presets(config)
    # ⚠️ 键必须转字符串：老库 scheduled_tasks.preset_id 是 TEXT 列，读回来是 "1"，
    #    而方案 id 是数字 1。dict 查找靠 hash，hash("1") != hash(1) → 永远 miss。
    #    （presets.get_preset() 用的是 ==，不受影响，所以任务实际能跑 —— 只有这里受影响。）
    preset_map = {str(p["id"]): p["name"] for p in all_presets}
    for t in tasks:
        t["preset_name"] = preset_map.get(str(t["preset_id"]), "")
        next_run = sched.get_next_run_time(t["id"])
        t["next_run_at"] = next_run.strftime("%Y-%m-%d %H:%M:%S") if next_run else None
        # enabled 在 SQLite 中是 0/1
        t["enabled"] = bool(t.get("enabled"))
        t["sync_before"] = bool(t.get("sync_before"))
    return {"tasks": tasks}


@app.post("/api/schedules")
async def api_create_schedule(request: ScheduleTaskRequest):
    """创建定时任务"""
    db = get_database()
    # 验证 preset 存在
    preset = presets.get_preset(config, request.preset_id)
    if not preset:
        return JSONResponse({"success": False, "message": "采集方案不存在"}, status_code=400)
    # 验证调度表达式（cron 或一次性）
    err = _validate_schedule_expr(request.cron_expression)
    if err:
        return JSONResponse({"success": False, "message": err}, status_code=400)
    task_id = db.add_scheduled_task(
        name=request.name,
        preset_id=request.preset_id,
        cron_expression=request.cron_expression,
        enabled=request.enabled,
        sync_before=request.sync_before,
    )
    # 注册到调度器
    if request.enabled:
        get_scheduler().add_job_for_task(task_id, request.cron_expression, True)
    return {"success": True, "task_id": task_id}


@app.put("/api/schedules/{task_id}")
async def api_update_schedule(task_id: int, request: Request):
    """更新定时任务（支持部分字段更新）"""
    db = get_database()
    task = db.get_scheduled_task(task_id)
    if not task:
        return JSONResponse({"success": False, "message": "任务不存在"}, status_code=404)
    data = await request.json()
    # 验证调度表达式（如果传了）
    cron_expr = data.get("cron_expression")
    if cron_expr:
        err = _validate_schedule_expr(cron_expr)
        if err:
            return JSONResponse({"success": False, "message": err}, status_code=400)
    # 验证 preset（如果传了）
    preset_id = data.get("preset_id")
    if preset_id is not None:
        preset = presets.get_preset(config, int(preset_id))
        if not preset:
            return JSONResponse({"success": False, "message": "采集方案不存在"}, status_code=400)
    # 构建 update data
    update_data = {}
    for key in ("name", "preset_id", "cron_expression", "enabled", "sync_before"):
        if key in data:
            update_data[key] = data[key]
    # bool 转 int
    if "enabled" in update_data:
        update_data["enabled"] = 1 if update_data["enabled"] else 0
    if "sync_before" in update_data:
        update_data["sync_before"] = 1 if update_data["sync_before"] else 0
    db.update_scheduled_task(task_id, update_data)
    # 更新调度器
    sched = get_scheduler()
    updated_task = db.get_scheduled_task(task_id)
    if updated_task:
        if updated_task["enabled"]:
            sched.add_job_for_task(task_id, updated_task["cron_expression"], True)
        else:
            sched.remove_job_for_task(task_id)
    return {"success": True}


@app.delete("/api/schedules/{task_id}")
async def api_delete_schedule(task_id: int):
    """删除定时任务"""
    db = get_database()
    ok = db.delete_scheduled_task(task_id)
    if not ok:
        return JSONResponse({"success": False, "message": "任务不存在"}, status_code=404)
    get_scheduler().remove_job_for_task(task_id)
    return {"success": True}


@app.get("/api/schedules/failures")
async def api_schedule_failures(since_id: int = 0, limit: int = 20):
    """任务失败通知（供后台任务面板/侧栏角标使用）。

    since_id：只看 id 大于它的记录（前端用本地记录的最后一条 id 做增量拉取）。

    ⚠️ 覆盖 sync_history 里的**全部**失败，不再只盯 task_type='scheduled_task'：
       手动采集、导入分享表、账号表同步失败原先只进「历史」、不进「失败」——
       用户会以为"没失败过"，其实只是没提醒。
    """
    db = get_database()
    # 多取一些：下面还要按 status 过滤，直接用 limit 当窗口会把失败漏在窗口之外
    history = db.get_sync_history(limit=max(limit * 5, 100))
    items = []
    for h in history:
        hid = h.get("id", 0)
        if hid <= since_id:
            continue
        if h.get("status") != "failed":
            continue
        # 已读判定：命中显式 id 集合，或不超过"全部已读"时的水位线
        if hid in _ack_schedule_failures or hid <= _ack_watermark:
            continue
        # 定时任务的 error 形如「任务名#12：具体错误」；其它类型就是一句原始错误
        raw = h.get("error") or ""
        name, sep, detail = raw.partition("：")
        if not sep:
            name, detail = "", raw
        items.append({
            "id": hid,
            "type": h.get("task_type") or "",
            "name": name,
            "detail": detail or raw,
            "at": h.get("finished_at") or h.get("created_at"),
        })
    return {"failures": items, "latest_id": max([h.get("id", 0) for h in history], default=0)}


# 失败通知的"已读"状态。
# 为什么不落库：这只是一个 UI 已读标记，重启后重新提醒一次反而更安全
# （用户重启通常意味着"我要重新看一遍当前状态"），不值得为此加列或加表。
#
# ⚠️ 用「水位线 + 显式 id」两个变量，而不是一个 -1 哨兵：
#    哨兵写法会让「清空记录」把**此后所有新失败**一并永久隐藏 —— 用户再也不知道任务挂过。
#    水位线只盖住"点清空那一刻已存在的记录"，新失败 id 更大、必然穿透。
_ack_schedule_failures: set[int] = set()
_ack_watermark: int = 0


@app.post("/api/schedules/failures/ack")
async def api_schedule_failures_ack(payload: dict = Body(default={})):
    """把失败通知标记为已读。

    传 ids 时只标记指定记录；不传（或传空列表）表示「把当前已有的全部标为已读」。
    注意是"当前已有"——之后新产生的失败仍会照常提醒。
    """
    global _ack_watermark
    ids = payload.get("ids") or []
    if ids:
        _ack_schedule_failures.update(int(i) for i in ids)
        return {"ack": len(ids)}
    db = get_database()
    # 口径必须与 /api/schedules/failures 一致：水位线要盖住「清空那一刻已存在的全部失败」，
    # 只取 scheduled_task 会漏掉其它类型，下次轮询它们又会冒出来
    history = db.get_sync_history(limit=200)
    _ack_watermark = max([h.get("id", 0) for h in history], default=_ack_watermark)
    return {"ack": "all", "watermark": _ack_watermark}


@app.get("/api/schedules/{task_id}/history")
async def api_schedule_history(task_id: int, limit: int = 50):
    """获取定时任务的执行历史。

    精确匹配：优先按 collection_batches.scheduled_task_id = task_id 取；
    老数据（该字段为空）回退到「任务名 == 方案名」的旧口径，仅用于兼容历史批次。
    """
    db = get_database()
    task = db.get_scheduled_task(task_id)
    if not task:
        return JSONResponse({"success": False, "message": "任务不存在"}, status_code=404)
    all_batches = db.list_collection_batches(limit=max(limit, 1))

    def _row(b):
        return {
            "id": b["id"],
            "platform": b.get("platform"),
            "preset_name": b.get("preset_name"),
            "status": b.get("status"),
            "total_accounts": b.get("total_accounts", 0),
            "success_accounts": b.get("success_accounts", 0),
            "failed_accounts": b.get("failed_accounts", 0),
            "skipped_accounts": b.get("skipped_accounts", 0),
            "started_at": b.get("started_at"),
            "finished_at": b.get("finished_at"),
        }

    matched = [b for b in all_batches if b.get("scheduled_task_id") == task_id]
    if not matched:
        # 兼容老批次：任务名与方案名一致时视为该任务的执行记录
        matched = [
            b for b in all_batches
            if b.get("scheduled_task_id") is None
            and b.get("preset_name")
            and b.get("preset_name") == task["name"]
        ]
    return {"batches": [_row(b) for b in matched]}


@app.post("/api/schedules/{task_id}/run")
async def api_run_schedule_now(task_id: int):
    """立即执行一次定时任务（不影响后续 cron 计划）。"""
    db = get_database()
    task = db.get_scheduled_task(task_id)
    if not task:
        return JSONResponse({"success": False, "message": "任务不存在"}, status_code=404)
    if db.get_active_collection_batch():
        return JSONResponse(
            {"success": False, "message": "已有采集批次正在执行，请稍后再试"},
            status_code=409,
        )
    # 后台执行，立刻返回，前端用轮询 /api/schedules 观察 last_run_status
    asyncio.create_task(_on_scheduled_task_trigger(task_id, ignore_enabled=True, trigger="manual"))
    return {"success": True, "message": "已开始执行"}


# ========== 采集批次 ==========

@app.post("/api/collection/batches")
async def api_start_collection_batch(request: CollectionBatchRequest):
    db = get_database()
    manager = get_collection_batch_manager()
    # 如果指定了 preset_id，从方案中读取参数覆盖请求字段
    if request.preset_id is not None:
        preset = presets.get_preset(config, request.preset_id)
        if not preset:
            return JSONResponse({"success": False, "message": "方案不存在"}, status_code=404)
        rating_min = preset["rating_min"]
        tags = preset["tags"].split(",") if preset["tags"] else []
        account_names = preset.get("account_names", "")
        platform = preset["platform"]
        mode = preset["mode"]
        preset_name = preset["name"]
        folder_name = preset.get("folder_name", "")
        name_format = preset.get("name_format", "")
        storage_primary_id = preset.get("storage_primary_id") or ""
        storage_secondary_id = preset.get("storage_secondary_id") or ""
        # 旧版 storage_choice 兼容：p:<id> → 主方案 ID
        legacy_choice = str(preset.get("storage_choice") or "")
        if not storage_primary_id and legacy_choice.startswith("p:"):
            storage_primary_id = legacy_choice[2:]
        if not storage_primary_id:
            storage_primary_id = request.storage_primary_id or ""
        if not storage_secondary_id:
            storage_secondary_id = request.storage_secondary_id or ""
        account_created_after = preset.get("account_created_after", "")
        skip_recent_days = int(preset.get("skip_recent_days", 0))
    else:
        rating_min = request.rating_min
        tags = request.tags
        account_names = request.account_names
        platform = request.platform
        mode = request.mode
        preset_name = ""
        folder_name = ""
        name_format = ""
        storage_primary_id = request.storage_primary_id or ""
        storage_secondary_id = request.storage_secondary_id or ""
        account_created_after = ""
        skip_recent_days = 0
    # 存储方案接入：主/次双下拉（空=设置页默认主/次；主失败自动切次，整套设置随之切换）
    defaults = config.collection_defaults
    active_profile = None
    root_path = ""
    if not folder_name:
        active_profile, storage_diags = sp.resolve_pair(
            config, "batch", storage_primary_id, storage_secondary_id
        )
        if active_profile is not None:
            root_path = active_profile["path"] or ""
        elif storage_diags:
            reasons = "；".join(
                f"{d.get('name', '?')}: {d.get('reason', '不可用')}" for d in storage_diags
            )
            return JSONResponse(
                {"success": False, "message": f"主/次存储方案均不可用（{reasons}）"},
                status_code=400,
            )
    if not root_path and folder_name and Path(folder_name).is_absolute():
        # 兼容旧预设/旧批次：历史上主存储路径被写入 folder_name。
        root_path = folder_name
        folder_name = ""
    if not folder_name:
        folder_name = defaults.get("folder_name", "Download")
    # 磁盘空间检查：剩余不足 5GB 时不直接拒绝，返回 needs_confirm 交由前端确认
    import shutil as _shutil

    free_gb = None
    try:
        free_gb = _shutil.disk_usage(root_path or folder_name).free / 1024**3
    except OSError:
        pass  # 网络盘不可达等场景由存储方案探测负责报错
    if not name_format:
        name_format = sp.resolve_name_format(config, "batch", active_profile)
        if not name_format:
            name_format = defaults.get("name_format", "create_time type nickname desc")
    # 引擎参数：优先取生效方案的设置，方案未配置时回退全局默认
    engine_params = sp.resolve_engine_params(config, "batch", active_profile)
    if engine_params is None:
        engine_params = {
            k: defaults.get(k) for k in (
                "folder_mode", "music", "dynamic_cover", "static_cover", "max_size", "storage_format", "max_pages"
            ) if k in defaults
        }
    platforms = (
        ("douyin", "tiktok") if platform == "all" else (platform,)
    )
    # 低空间确认：剩余不足 5GB 时先返回 needs_confirm，由前端确认后带 force_low_space 重发
    if free_gb is not None and free_gb < 5 and not request.force_low_space:
        return JSONResponse(
            {
                "success": False,
                "needs_confirm": True,
                "message": (
                    f"下载目录剩余空间不足 5GB（当前剩 {free_gb:.1f}GB），"
                    "采集可能中途失败。是否仍然开始？"
                ),
            },
            status_code=200,
        )
    try:
        batches = await manager.start(
            accounts=db.get_all_accounts(),
            rating_min=rating_min,
            tags=tags,
            account_names=account_names,
            platforms=platforms,
            mode=mode,
            preset_name=preset_name,
            folder_name=folder_name,
            root_path=root_path,
            name_format=name_format,
            account_created_after=account_created_after,
            skip_recent_days=skip_recent_days,
            engine_params=engine_params,
        )
        return {"success": True, "batches": batches}
    except ValueError as error:
        return JSONResponse({"success": False, "message": str(error)}, status_code=400)
    except RuntimeError as error:
        return JSONResponse({"success": False, "message": str(error)}, status_code=409)


@app.get("/api/collection/batches")
async def api_list_collection_batches():
    return {"batches": get_database().list_collection_batches()}


@app.post("/api/collection/batches/preview")
async def api_preview_collection_batch(request: CollectionBatchRequest):
    """Preview the account selection without creating a batch."""
    accounts = get_database().get_all_accounts()
    platforms = (
        ("douyin", "tiktok") if request.platform == "all" else (request.platform,)
    )
    platform_results = []
    totals = {
        "total_accounts": 0,
        "incremental_accounts": 0,
        "first_run_accounts": 0,
        "skipped_accounts": 0,
    }

    for platform in platforms:
        planned = plan_collection(
            accounts=accounts,
            rating_min=request.rating_min,
            tags=request.tags,
            account_names=request.account_names,
            platform=platform,
            mode=request.mode,
        )
        if not planned:
            continue
        skipped = sum(item.status == "skipped" for item in planned)
        first_run = sum(
            item.status == "pending" and item.earliest == "" for item in planned
        )
        incremental = sum(
            item.status == "pending" and item.earliest != "" for item in planned
        )
        result = {
            "platform": platform,
            "total_accounts": len(planned),
            "incremental_accounts": incremental,
            "first_run_accounts": first_run,
            "skipped_accounts": skipped,
        }
        platform_results.append(result)
        for key in totals:
            totals[key] += result[key]

    if not platform_results:
        return JSONResponse(
            {"success": False, "message": "没有符合条件的账号"},
            status_code=400,
        )
    return {"success": True, **totals, "platforms": platform_results}


@app.get("/api/collection/batches/{batch_id}")
async def api_collection_batch_detail(batch_id: str, include_log: bool = True):
    """批次详情。

    include_log=False（历史详情弹窗）：跳过日志全量读取与账号表关联，
    只返回批次/明细/聚合作品（works 解析带 mtime 缓存），大幅降低响应耗时。
    运行中面板走默认 include_log=True，保留流式日志。
    """
    db = get_database()
    batch = db.get_collection_batch(batch_id)
    if not batch:
        return JSONResponse({"success": False, "message": "批次不存在"}, status_code=404)
    items = db.get_collection_batch_items(batch_id)
    if include_log:
        accounts = {
            row["record_id"]: row
            for row in db.get_all_accounts()
            if row.get("record_id")
        }
        for item in items:
            account = accounts.get(item.get("account_record_id"))
            item["last_collected_at"] = account.get("last_collected_at") if account else None
    log_path = (batch.get("log_path") or "") if batch else ""
    log_exists = bool(log_path) and Path(log_path).exists()
    manager = get_collection_batch_manager()
    works_db = manager.read_batch_works_from_db(batch_id)
    if works_db:
        works = manager.aggregate_db_works(works_db)
        works_source = "db"
    else:
        works = manager.read_account_works(batch_id)
        works_source = "log"
    return {
        "batch": batch,
        "items": items,
        "log": manager.read_log(batch_id) if include_log else [],
        "works": works,
        "works_db": works_db,
        "works_source": works_source,
        "log_exists": log_exists,
    }



# ---------------- 作品明细（collection_works）----------------

@app.get("/api/collection/account-works")
async def api_account_works(sec_user_id: str = "", name: str = ""):
    """按账号聚合全部批次的作品明细（带跨批次/同批次重复标记）。"""
    db = get_database()
    works = db.list_account_works(sec_user_id=sec_user_id, account_name=name)
    stats = {
        "total": len(works),
        "success": sum(1 for w in works if w.get("status") == "success"),
        "failed": sum(1 for w in works if w.get("status") != "success"),
        "dup_total": sum(1 for w in works if int(w.get("dup_total") or 0) > 1),
        "dup_in_batch": sum(1 for w in works if int(w.get("dup_in_batch") or 0) > 1),
    }
    return {"success": True, "works": works, "stats": stats}


class WorkOpenRequest(BaseModel):
    work_id: int
    mode: str = "file"  # file=资源管理器定位文件, dir=打开目录


@app.post("/api/collection/works/open")
async def api_collection_work_open(request: WorkOpenRequest):
    """打开作品下载文件或其所在目录。"""
    db = get_database()
    with db._connect() as conn:
        import sqlite3

        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM collection_works WHERE id = ?", (request.work_id,)
        ).fetchone()
    if not row:
        return {"success": False, "message": "记录不存在"}
    work = dict(row)
    import os
    import subprocess
    import sys

    if request.mode == "dir":
        target = work.get("download_dir") or ""
        if not target or not os.path.isdir(target):
            return {"success": False, "mode": "copy", "path": target,
                    "message": f"目录不存在: {target or '未记录'}（路径已可复制）"}
        from .core.os_open import open_folder
        ok, err = open_folder(Path(target))
        if not ok:
            return {"success": False, "mode": "copy", "path": target,
                    "message": f"服务器无法直接打开，路径已可复制: {err}"}
        return {"success": True, "mode": "opened"}
    target = work.get("file_path") or ""
    if not target:
        return {"success": False, "message": "该记录无文件路径"}
    if not os.path.isfile(target):
        return {"success": False, "message": f"文件不存在（可能已被移动）: {target}"}
    try:
        if sys.platform == "win32":
            subprocess.Popen(["explorer", "/select,", target])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", target])
        else:
            subprocess.Popen(["xdg-open", os.path.dirname(target)])
        return {"success": True}
    except Exception as e:
        return {"success": False, "message": str(e)}

@app.post("/api/collection/batches/{batch_id}/cancel")
async def api_cancel_collection_batch(batch_id: str):
    ok = get_collection_batch_manager().cancel(batch_id)
    return {
        "success": ok,
        "message": "已请求取消" if ok else "批次不存在或已结束",
    }


@app.post("/api/collection/batches/{batch_id}/pause")
async def api_pause_collection_batch(batch_id: str):
    ok = get_collection_batch_manager().pause(batch_id)
    return {
        "success": ok,
        "message": "已暂停" if ok else "批次不存在或不在运行中",
    }


@app.post("/api/collection/batches/{batch_id}/resume")
async def api_resume_collection_batch(batch_id: str):
    ok = get_collection_batch_manager().resume(batch_id)
    return {
        "success": ok,
        "message": "已继续" if ok else "批次不存在或未暂停",
    }

@app.post("/api/collection/batches/{batch_id}/retry")
async def api_retry_collection_batch(batch_id: str, request: CollectionRetryRequest):
    db = get_database()
    source = db.get_collection_batch_items(batch_id)
    record_ids = [
        item["account_record_id"]
        for item in source
        if item.get("status") in ("failed", "cancelled")
        and item.get("account_record_id")
    ]
    if not record_ids:
        return JSONResponse(
            {"success": False, "message": "没有可重试的账号"},
            status_code=400,
        )
    try:
        batches = await get_collection_batch_manager().start(
            accounts=db.get_all_accounts(),
            rating_min=1,
            record_ids=record_ids,
            platforms=(db.get_collection_batch(batch_id)["platform"],),
            mode=request.mode,
        )
        return {"success": True, "batches": batches}
    except ValueError as error:
        return JSONResponse({"success": False, "message": str(error)}, status_code=400)
    except RuntimeError as error:
        return JSONResponse({"success": False, "message": str(error)}, status_code=409)


# --- 账号 ---

@app.get("/api/accounts")
async def api_accounts():
    s = get_syncer()
    if not s:
        return {"accounts": []}
    accounts = s.load_local_accounts()
    return {
        "accounts": [
            {
                "name": a.name,
                "platform": a.platform,
                "link": a.link,
                "rating": a.rating,
                "enabled": a.enabled,
                "nickname": a.nickname,
                "follower_count": a.follower_count,
                "aweme_count": a.aweme_count,
                "tags": a.tags,
            }
            for a in accounts
        ]
    }


# --- 历史 ---

@app.get("/api/history")
async def api_history(limit: int = 100, offset: int = 0, status: str = ""):
    h = get_history()
    return {"records": h.get_records(limit=limit, offset=offset, status=status)}


@app.get("/api/stats")
async def api_stats():
    """获取统计数据"""
    h = get_history()
    return h.get_stats()


# ---------- 磁盘卷识别（跨平台，2026-09-20 加） ----------
# 为什么需要：判断"两个目录是不是同一块盘"**不能用盘符**（`Path().anchor`）——
# Windows 下它给 `F:\` 能区分，POSIX（NAS/Docker）下**恒为 `/`**，会把所有挂载盘
# 合并成一行、只显示先遇到的那块，名字还显示成 `/`。改用文件系统设备号 st_dev，
# 语义两边一致：同一文件系统相同、不同分区/挂载源不同。
_MOUNT_POINTS_CACHE = None


def _parse_mount_points(text: str) -> list:
    """解析 /proc/mounts 文本 → 挂载点列表（按长度降序）。纯函数，便于跨平台单测。"""
    points = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 2 or not parts[1].startswith("/"):
            continue
        # /proc/mounts 里空格等字符是八进制转义（如 \040）
        point = (parts[1].replace(r"\040", " ").replace(r"\011", "\t")
                 .replace(r"\012", "\n").replace(r"\134", "\\"))
        points.append(point)
    points.sort(key=len, reverse=True)
    return points


def _mount_point_for(points: list, raw: str) -> str:
    """该路径落在哪个挂载点上（最长前缀匹配）；只有根挂载点 '/' 时视为无信息。

    ⚠️ 只做纯字符串比较，**不要用 `os.path.isabs`/`Path.resolve` 做预处理** ——
    本机实测：Windows 的 `isabs('/download')` 返回 False，会把 POSIX 路径
    解析成 `D:/download` 而匹配失败（Linux 上才返回 True，是个平台陷阱）。
    本函数只在 `_disk_volume` 的 Linux 分支被调用，入参必是 POSIX 路径。
    """
    target = raw.replace("\\", "/").rstrip("/") or "/"
    for point in points:
        if point == "/":
            continue
        base = point.rstrip("/") or "/"
        if target == base or target.startswith(base + "/"):
            return point
    return ""


def _mount_points() -> list:
    """当前系统的挂载点表。只读一次 —— 容器生命周期内挂载表不会变。"""
    global _MOUNT_POINTS_CACHE
    if _MOUNT_POINTS_CACHE is None:
        points: list = []
        for name in ("/proc/self/mounts", "/proc/mounts", "/etc/mtab"):
            try:
                text = Path(name).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            points = _parse_mount_points(text)
            break
        _MOUNT_POINTS_CACHE = points
    return _MOUNT_POINTS_CACHE


def _disk_volume(raw: str) -> tuple:
    """返回 (去重键, 显示名)。

    - 去重键：`st_dev` 优先（跨平台正确）；`st_dev == 0` 的极端平台退回卷根做键，
      至少不会把不同盘互吞。路径不存在/不可访问时返回空 → 调用方跳过该条。
    - 显示名：Windows 用卷根（`F:\\`）；POSIX 用挂载点（`/download`），取不到挂载点
      再退化为路径首段（避免整列都显示无意义的 `/`）。
    """
    import os as _os

    try:
        device = _os.stat(raw).st_dev
    except OSError:
        return "", ""
    key = f"dev:{device}" if device else f"root:{(Path(raw).anchor or raw).lower()}"
    anchor = Path(raw).anchor
    if _os.name == "nt" or anchor != "/":
        name = anchor or raw
    else:
        name = _mount_point_for(_mount_points(), raw)
        if not name:
            segments = [s for s in raw.replace("\\", "/").split("/") if s]
            name = "/" + segments[0] if segments else "/"
    return key, name


@app.get("/api/disk")
async def api_disk():
    """磁盘空间：统计「存储方案」（storage_profiles）里实际会用到的目录所在卷 + 应用数据卷。

    口径（2026-09-20 定稿）：
    - 主体 = 存储方案中**已启用**的目录所在卷。没配进方案的盘不统计 —— 既不会
      往里写、也没法在应用里操作，列出来只是噪音。每次请求实时读配置，
      所以新增/停用方案后无需重启，下一次请求即反映（前端面板每次打开都重新拉）。
    - 附加 = 应用数据目录（config.app_data_dir）所在卷，只作定位参考，
      `writable=False` → 不参与告警转红（数据卷满不影响采集能否继续）。
      与某个方案同卷时会合并成同一行，此时因方案来源而恢复参与告警。
    侧栏磁盘图标靠它做「低于 5GB 转红」的常驻状态提示（阈值与采集启动前的
    空间检查一致）。同一卷只报一次，标签标注来源（哪个区、哪个方案）。
    """
    import shutil as _shutil

    # (作用区, 来源名, 目录路径, 是否参与告警)
    targets: list[tuple[str, str, str, bool]] = []
    scope_label = {"batch": "增量采集", "single": "单作品"}
    state = config.storage_profiles if isinstance(config.storage_profiles, dict) else {}

    for scope in ("batch", "single"):
        item = state.get(scope) or {}
        profiles = item.get("profiles") if isinstance(item, dict) else None
        if not isinstance(profiles, list):
            continue
        for p in profiles:
            if not isinstance(p, dict):
                continue
            raw = str(p.get("path") or "").strip()
            # 没路径 = 草稿方案；disabled = 当前不会写入 → 两种都不算"用到的磁盘"
            if not raw or not p.get("enabled", True):
                continue
            name = str(p.get("name") or "").strip() or "未命名方案"
            targets.append((scope_label.get(scope, scope), name, raw, True))

    # 兜底：一个方案都没配时不至于整个面板空白（沿用全局下载目录）
    if not targets:
        fallback = (config.get("local", {}) or {}).get("download_path") or ""
        if fallback:
            targets.append(("增量采集", "默认目录", fallback, True))

    # 应用数据目录所在卷（2026-09-20 加）：只展示、不参与告警。
    # 为什么不参与告警：数据卷写满不会让采集写不进去，让它跟着转红会把用户的
    # 注意力引到错误的盘上；但它又是"程序自己的东西在哪"的必备参考信息，所以保留。
    try:
        targets.append(("应用数据", "数据目录", str(config.app_data_dir), False))
    except Exception:
        pass

    # 按卷聚合。先收集"这个卷被哪些方案用到"，再统一生成标签 ——
    # 若边扫边拼标签，同名方案（两个区都叫「本机F」）会拼出「增量采集 · 本机F、单作品 · 本机F」
    # 这种重复啰嗦的文案，归并后才能压成「本机F（增量采集 / 单作品）」。
    buckets: dict[str, dict] = {}
    order: list[str] = []
    for scope_lbl, name, raw, is_storage in targets:
        if not raw:
            continue
        try:
            usage = _shutil.disk_usage(raw)
        except OSError:
            # 网络盘不可达等场景：直接跳过，不让整个接口失败
            continue
        # 去重键 = 文件系统设备号（同一块盘上的多个目录只报一次）。
        # ⚠️ 不能退回用盘符 anchor：POSIX 下恒为 '/'，NAS/Docker 里会把多块盘并成一行。
        key, volume_name = _disk_volume(raw)
        if not key:
            continue
        if key not in buckets:
            order.append(key)
            buckets[key] = {
                "name": volume_name or raw,
                "total_gb": round(usage.total / 1024**3, 1),
                "used_gb": round((usage.total - usage.free) / 1024**3, 1),
                "free_gb": round(usage.free / 1024**3, 2),
                "writable": False,  # 由来源决定：只有存储方案用到的卷才参与告警
                "sources": [],     # 该卷被哪些来源用到：[{scope, name, path}]
            }
        # 同一卷可能既被方案用到、又是应用数据卷 → 只要有一个"要写入的方案"就参与告警
        if is_storage:
            buckets[key]["writable"] = True
        buckets[key]["sources"].append({"scope": scope_lbl, "name": name, "path": raw})

    drives: list[dict] = []
    for key in order:
        d = buckets[key]
        # 按方案名归并作用区：同名跨区 → 「本机F（增量采集 / 单作品）」
        by_name: dict[str, list[str]] = {}
        for s in d["sources"]:
            scopes = by_name.setdefault(s["name"], [])
            if s["scope"] not in scopes:
                scopes.append(s["scope"])
        d["label"] = "、".join(f"{nm}（{' / '.join(sc)}）" for nm, sc in by_name.items())
        # 路径去重后返回，面板里能让用户核对"这个盘是哪些目录在用"
        paths: list[str] = []
        for s in d["sources"]:
            if s["path"] not in paths:
                paths.append(s["path"])
        d["paths"] = paths
        d["path"] = paths[0] if paths else d["name"]
        drives.append(d)

    low_gb = 5.0
    return {
        "drives": drives,
        "low_gb": low_gb,
        "any_low": any(d["writable"] and d["free_gb"] < low_gb for d in drives),
    }


# --- 设置 ---

@app.get("/api/settings")
async def api_get_settings():
    """获取当前配置信息"""
    return config._data


def _merge_settings_patch(base: dict, patch: dict) -> dict:
    """设置补丁深合并：两侧同为 dict 才递归，其余类型整体覆盖。

    为什么需要：设置表单只提供每个顶层键下的**部分**字段（如 api 只给 enabled），
    若按整键替换，该键下没出现在表单里的字段（api_key / default_resolve_mode）会被抹掉。
    """
    out = dict(base)
    for k, v in patch.items():
        cur = out.get(k)
        out[k] = _merge_settings_patch(cur, v) if isinstance(v, dict) and isinstance(cur, dict) else v
    return out


@app.post("/api/settings")
async def api_save_settings(request: Request):
    global collector, syncer, feishu_client
    data = await request.json()
    for key, value in data.items():
        cur = config.get(key)
        # 补丁合并：只覆盖表单真正提供的字段，保住同键下未提交的字段
        if isinstance(value, dict) and isinstance(cur, dict):
            value = _merge_settings_patch(cur, value)
        config.set(key, value)
    config.save()

    # 重置受影响的实例，使新配置生效
    feishu_client = None
    collector = None
    syncer = None

    return {"success": True, "message": "设置已保存"}


@app.get("/api/tags")
async def api_get_tags():
    """获取标签映射"""
    return config._data.get("tags", {})


@app.get("/api/tags/options")
async def api_tag_options():
    """获取所有账号出现过的标签（去重排序），供方案弹窗多选下拉使用"""
    tags = set()
    # 1) 标签映射的规范标签（values）
    for v in (config._data.get("tags") or {}).values():
        v = str(v).strip()
        if v:
            tags.add(v)
    # 2) 数据库账号实际标签
    for a in get_database().get_all_accounts():
        raw = a.get("标签") or ""
        if isinstance(raw, str):
            try:
                parsed = json.loads(raw)
            except Exception:
                parsed = [raw] if raw.strip() else []
        else:
            parsed = raw if isinstance(raw, list) else []
        for t in parsed:
            t = str(t).strip()
            if t:
                tags.add(t)
    return {"success": True, "tags": sorted(tags)}


@app.post("/api/ensure-fields")
async def api_ensure_fields(request: Request):
    """检测并创建缺失的云端表格字段，支持指定表类型"""
    f = get_feishu()
    if not f:
        return JSONResponse({"success": False, "message": "云端未配置"}, status_code=400)

    try:
        data = await request.json()
        table_type = data.get("table_type", "all")  # account | collection | cookie | all
        app_token = config.feishu.get("app_token", "")

        results = []
        table_map = {
            "account": config.feishu.get("account_table_id", ""),
            "collection": config.feishu.get("collection_table_id", ""),
            "cookie": config.feishu.get("cookie_table_id", ""),
        }

        types_to_process = list(table_map.keys()) if table_type == "all" else [table_type]

        for t in types_to_process:
            table_id = table_map.get(t, "")
            if not table_id:
                results.append({"table": t, "success": False, "message": "未配置 Table ID"})
                continue
            try:
                r = f.ensure_fields(app_token, table_id, table_type=t)
                results.append({"table": t, **r})
            except Exception as e:
                results.append({"table": t, "success": False, "message": str(e)})

        all_ok = all(r.get("success", False) for r in results)
        return {"success": all_ok, "results": results}

    except Exception as e:
        return JSONResponse({"success": False, "message": str(e)}, status_code=500)


@app.post("/api/test-feishu")
async def api_test_feishu():
    f = get_feishu()
    if not f:
        return {"success": False, "message": "云端未配置，请先填写 App ID 和 App Secret"}
    return f.test_connection()


@app.get("/api/browse-dir")
async def api_browse_dir(path: str = ""):
    """浏览目录，返回子目录列表"""
    from pathlib import Path
    import os

    if not path:
        # 默认从用户主目录开始
        home = str(Path.home())
        if os.name == "nt":  # Windows
            # Windows: 先显示驱动器列表
            drives = []
            for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
                drive = f"{letter}:\\"
                if Path(drive).exists():
                    drives.append(drive)
            if len(drives) == 1:
                # 只有一个驱动器，直接进入
                path = drives[0]
            else:
                return {"current": "", "dirs": drives, "home": home}
        else:
            path = home

    p = Path(path)
    if not p.exists() or not p.is_dir():
        p = Path.home()

    dirs = []
    try:
        for item in sorted(p.iterdir()):
            if item.is_dir() and not item.name.startswith("."):
                dirs.append(str(item))
    except PermissionError:
        pass

    return {
        "current": str(p),
        "parent": str(p.parent) if p.parent != p else None,
        "dirs": dirs,
        "home": str(Path.home()),
    }


# --- 应用数据目录迁移 ---

@app.get("/api/data-migration/status")
async def api_data_migration_status():
    state = data_migration.snapshot()
    state["current_root"] = str(app_data_root())
    return state


@app.post("/api/data-migration/validate")
async def api_data_migration_validate(payload: dict):
    return validate_target(payload.get("target", ""))


@app.get("/api/data-migration/directories")
async def api_data_migration_directories(path: str = "", computer: int = 0):
    """返回页面内置目录选择器需要的目录列表。"""
    import os

    p = Path(path).expanduser() if path.strip() else app_data_root()
    if not p.exists() or not p.is_dir():
        p = Path.home()

    if computer:
        if os.name == "nt":
            return {
                "success": True,
                "path": "",
                "parent": None,
                "directories": _windows_drives(),
            }
        p = Path("/")

    directories = []
    if p.parent == p and os.name == "nt":
        directories.extend(_windows_drives())
        try:
            for item in sorted(p.iterdir(), key=lambda x: x.name.lower()):
                if item.is_dir() and not item.name.startswith("."):
                    directories.append({"name": item.name, "path": str(item)})
        except PermissionError:
            pass
        return {"success": True, "path": str(p), "parent": None, "directories": directories}

    try:
        for item in sorted(p.iterdir(), key=lambda x: x.name.lower()):
            if item.is_dir() and not item.name.startswith("."):
                directories.append({"name": item.name, "path": str(item)})
    except PermissionError:
        pass

    return {
        "success": True,
        "path": str(p),
        "parent": str(p.parent) if p.parent != p else None,
        "directories": directories,
    }


def _windows_drives():
    return [
        {"name": f"驱动器 {letter}:", "path": f"{letter}:\\"}
        for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        if Path(f"{letter}:\\").exists()
    ]


@app.post("/api/data-migration/directories/create")
async def api_data_migration_create_directory(payload: dict):
    """在内置目录选择器中新建一个文件夹。"""
    parent = Path(str(payload.get("parent", ""))).expanduser()
    name = str(payload.get("name", "")).strip()
    invalid = not name or name in {".", ".."} or any(sep in name for sep in ("/", "\\", "\0"))
    if invalid or not parent.is_dir():
        return JSONResponse({"success": False, "message": "目录或名称无效"}, status_code=400)

    try:
        target = parent / name
        target.mkdir(exist_ok=False)
    except FileExistsError:
        return JSONResponse({"success": False, "message": "同名文件夹已存在"}, status_code=400)
    except OSError as exc:
        return JSONResponse({"success": False, "message": str(exc)}, status_code=400)

    return {"success": True, "path": str(target), "parent": str(parent), "name": name}


@app.post("/api/data-migration/start")
async def api_data_migration_start(payload: dict):
    import asyncio

    state = data_migration.snapshot()
    if state["status"] in ("preparing", "copying", "verifying", "ready"):
        return JSONResponse({"success": False, "message": "迁移已在进行"}, status_code=409)

    busy = _migration_busy_reason()
    if busy:
        return JSONResponse({"success": False, "message": busy}, status_code=409)

    check = data_migration.prepare(payload.get("target", ""), app_data_root())
    if not check.get("valid"):
        return JSONResponse({"success": False, "message": check["message"]}, status_code=400)

    busy = _migration_busy_reason()
    if busy:
        data_migration.fail(busy)
        return JSONResponse({"success": False, "message": busy}, status_code=409)

    source_root = app_data_root()
    source_db = source_root / "doukhub.db"
    asyncio.create_task(asyncio.to_thread(data_migration.run, source_root, source_db))
    return {"success": True, "message": "迁移已开始"}


@app.get("/api/data-migration/old-cleanup")
async def api_data_migration_old_cleanup():
    marker_path = app_data_root() / ".doukhub-migration.json"
    if not marker_path.exists():
        return {"available": False, "source_root": ""}
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        return {"available": True, "source_root": marker.get("source_root", "")}
    except Exception:
        return {"available": False, "source_root": ""}


@app.post("/api/data-migration/cleanup-old")
async def api_data_migration_cleanup_old(payload: dict):
    import shutil

    if payload.get("confirmed") is not True:
        return {"success": False, "message": "需要用户确认"}
    marker_path = app_data_root() / ".doukhub-migration.json"
    if not marker_path.exists():
        return {"success": False, "message": "没有迁移记录"}
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    old_root = Path(str(marker.get("source_root", "")))
    new_root = app_data_root()
    if not old_root.is_dir() or old_root == new_root:
        return {"success": False, "message": "旧目录不可清理"}
    if new_root in old_root.parents or old_root in new_root.parents:
        return {"success": False, "message": "新旧目录存在嵌套关系"}

    removed = []
    for name in marker.get("items", []):
        target = old_root / name
        resolved = target.resolve()
        if resolved == old_root.resolve() or not resolved.is_relative_to(old_root.resolve()):
            continue
        try:
            if target.is_dir():
                shutil.rmtree(target)
            elif target.exists():
                target.unlink()
            removed.append(name)
        except OSError as exc:
            return {"success": False, "message": f"清理失败：{exc}", "removed": removed}
    return {"success": True, "removed": removed}


# --- 系统控制 ---

@app.post("/api/system/restart")
async def restart_system():
    """重启 DoukHub"""
    import os
    import sys
    import subprocess

    logger.info("正在重启 DoukHub...")

    # 停止 Downloader 服务
    svc = get_services()
    svc.stop_all()

    # 延迟重启（等响应返回后）
    async def _delayed_restart():
        import asyncio
        await asyncio.sleep(1)
        os.execv(sys.executable, [sys.executable] + sys.argv)

    import asyncio
    asyncio.create_task(_delayed_restart())

    return {"success": True, "message": "正在重启..."}


@app.post("/api/system/exit")
async def exit_system():
    """退出 DoukHub"""
    import os
    import signal

    logger.info("正在退出 DoukHub...")

    # 停止 Downloader 服务
    svc = get_services()
    svc.stop_all()

    # 延迟退出（等响应返回后）
    async def _delayed_exit():
        import asyncio
        await asyncio.sleep(1)
        os.kill(os.getpid(), signal.SIGTERM)

    import asyncio
    asyncio.create_task(_delayed_exit())

    return {"success": True, "message": "正在退出..."}


# ========== API v1 — 外部设备调用 ==========

def _generate_api_key() -> str:
    """生成专属 API Key"""
    return "dk_" + secrets.token_hex(24)


def _verify_api_request(request: Request) -> tuple[bool, str]:
    """验证 API 请求鉴权，返回 (是否通过, 错误消息)"""
    if not config.api_enabled:
        return False, "API 请求模式未启用，请在设置中开启"
    expected_key = config.api_key
    if expected_key:
        provided_key = request.headers.get("X-API-Key", "")
        if provided_key != expected_key:
            return False, "API Key 无效"
    return True, ""


def _get_local_ip() -> str:
    """获取本机局域网 IP"""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"


@app.post("/api/v1/api-key")
async def api_v1_regenerate_key(request: Request):
    """生成或重新生成 API Key（仅本机调用，无需鉴权）"""
    new_key = _generate_api_key()
    config.set("api.api_key", new_key)
    config.save()
    logger.info("API Key 已重新生成")
    return {"success": True, "api_key": new_key, "message": "API Key 已生成"}


@app.get("/api/v1/status")
async def api_v1_status(request: Request):
    """外部设备检查 DoukHub 服务状态"""
    ok, msg = _verify_api_request(request)
    if not ok:
        return JSONResponse({"success": False, "message": msg}, status_code=401)
    return {
        "success": True,
        "message": "DoukHub 服务正常",
        "version": "1.0",
        "download_path": str(config.download_path),
    }


class ApiV1ResolveRequest(BaseModel):
    links: str
    resolve_mode: str = "auto"


@app.post("/api/v1/works/resolve")
async def api_v1_resolve_works(request: Request, body: ApiV1ResolveRequest):
    """API 3a：解析链接，返回结构化作品数据（含下载地址）

    其他设备调用后获取 JSON 结果，自行下载。
    """
    ok, msg = _verify_api_request(request)
    if not ok:
        return JSONResponse({"success": False, "message": msg}, status_code=401)

    links = _extract_single_work_links(body.links)
    if not links:
        return JSONResponse(
            {"success": False, "message": "未识别到抖音或 TikTok 作品链接"},
            status_code=400,
        )

    resolve_mode = body.resolve_mode or config.api_config.get("default_resolve_mode", "auto")
    client = get_single_work_client()
    ttd_url = f"http://127.0.0.1:{config.ttd_port}"
    db = get_database()
    cookie_list = get_cookie_list()

    results = []
    for i, (link, platform) in enumerate(links):
        cookie = cookie_list[i % len(cookie_list)] if cookie_list else ""
        try:
            work = await single_work.fetch_work(
                client, ttd_url, link, platform, cookie, mode=resolve_mode
            )
            results.append({
                "link": link,
                "status": "success",
                "work": work,
            })
        except Exception as error:
            results.append({
                "link": link,
                "status": "failed",
                "message": str(error),
            })

    return {
        "success": any(r["status"] == "success" for r in results),
        "total": len(results),
        "success_count": sum(1 for r in results if r["status"] == "success"),
        "failed_count": sum(1 for r in results if r["status"] == "failed"),
        "results": results,
    }


class ApiV1DownloadRequest(BaseModel):
    links: str
    target_dir: str = ""
    filename_template: str = "{create_time} {author} {title}"
    include_music: bool = False
    include_static_cover: bool = False
    include_dynamic_cover: bool = False
    folder_mode: bool = False  # 每作品独立子文件夹（与设置页开关一致）
    resolve_mode: str = "auto"


@app.post("/api/v1/works/download")
async def api_v1_download_works(request: Request, body: ApiV1DownloadRequest):
    """API 3b：解析 + 本地下载，返回下载结果 JSON

    其他设备传参触发 DoukHub 本地下载。
    """
    ok, msg = _verify_api_request(request)
    if not ok:
        return JSONResponse({"success": False, "message": msg}, status_code=401)

    links = _extract_single_work_links(body.links)
    if not links:
        return JSONResponse(
            {"success": False, "message": "未识别到抖音或 TikTok 作品链接"},
            status_code=400,
        )

    target, target_err = _resolve_single_target(body.target_dir)
    if target is None:
        return JSONResponse(
            {"success": False, "message": target_err},
            status_code=400,
        )
    filename_template = _resolve_single_template(body.filename_template)

    resolve_mode = body.resolve_mode or config.api_config.get("default_resolve_mode", "auto")
    db = get_database()
    client = get_single_work_client()
    ttd_url = f"http://127.0.0.1:{config.ttd_port}"
    cookie_list = get_cookie_list()

    results = []
    for i, (link, platform) in enumerate(links):
        cookie = cookie_list[i % len(cookie_list)] if cookie_list else ""
        try:
            work = await single_work.fetch_work(
                client, ttd_url, link, platform, cookie, mode=resolve_mode
            )
            result = await _download_single_work_and_record(
                db, client, ttd_url, link, platform, target,
                filename_template,
                include_music=body.include_music,
                include_static_cover=body.include_static_cover,
                include_dynamic_cover=body.include_dynamic_cover,
                folder_mode=body.folder_mode,
                work=work,
            )
            results.append(result)
        except Exception as error:
            results.append({
                "link": link,
                "status": "failed",
                "message": str(error),
            })

    return {
        "success": any(r["status"] == "success" for r in results),
        "total": len(results),
        "success_count": sum(1 for r in results if r["status"] == "success"),
        "failed_count": sum(1 for r in results if r["status"] == "failed"),
        "results": results,
    }


@app.get("/api/v1/api-info")
async def api_v1_api_info(request: Request):
    """获取 API 信息（本机调用，用于采集页面展示）"""
    local_ip = _get_local_ip()
    return {
        "enabled": config.api_enabled,
        "api_key": config.api_key if config.api_enabled else "",
        "local_ip": local_ip,
        "port": 2999,
        "base_url": f"http://{local_ip}:2999",
        "endpoints": {
            "resolve": f"http://{local_ip}:2999/api/v1/works/resolve",
            "download": f"http://{local_ip}:2999/api/v1/works/download",
            "status": f"http://{local_ip}:2999/api/v1/status",
        },
    }


# ========== 启动 ==========

def run():
    """启动 DoukHub"""
    import uvicorn
    logger.info("DoukHub 正在启动...")
    logger.info(f"本机访问: http://127.0.0.1:2999")
    logger.info(f"局域网访问: http://0.0.0.0:2999")
    uvicorn.run(app, host="0.0.0.0", port=2999, reload=False)
