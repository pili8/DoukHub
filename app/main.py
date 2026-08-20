"""DoukHub 主入口 — FastAPI Web 应用"""
import asyncio
import json
import logging
import secrets
import socket
import httpx
from contextlib import asynccontextmanager
from datetime import datetime, date
from pathlib import Path, PureWindowsPath
from typing import Any, Literal
from urllib.parse import quote

from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from .core.config import Config
from .core.feishu import FeishuClient
from .core.collector import Collector, Account
from .core.cookie_pool import CookiePool
from .core.syncer import Syncer
from .core.syncer_v2 import Syncer as SyncerV2
from .core.database import Database
from .core.collection_batch_manager import CollectionBatchManager
from .core.collection_planner import plan_collection
from .core.feishu_sync import FeishuSyncer
from .core.history import HistoryDB
from .core.scheduler import TaskScheduler
from .core.tasks import get_task_manager
from .core.link_resolver import extract_sec_user_id, build_profile_url
from .core import single_work
from .services.downloader import ServiceManager

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
scheduler: TaskScheduler | None = None
collection_batch_manager: CollectionBatchManager | None = None
single_work_client: httpx.AsyncClient | None = None


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
            log_dir=config.data_dir / "collection_logs",
            ttd_url=f"http://127.0.0.1:{config.ttd_port}",
        )
    return collection_batch_manager


def get_single_work_client() -> httpx.AsyncClient:
    global single_work_client
    if single_work_client is None:
        single_work_client = httpx.AsyncClient(
            timeout=300,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0"},
        )
    return single_work_client


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
        history = HistoryDB(config.data_dir)
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


def get_scheduler() -> TaskScheduler:
    global scheduler
    if scheduler is None:
        scheduler = TaskScheduler(
            history=get_history(),
            get_collector=get_collector,
            get_syncer=get_syncer,
            get_accounts=lambda: (get_syncer().load_local_accounts() if get_syncer() else []),
        )
    return scheduler


@asynccontextmanager
async def lifespan(app: FastAPI):
    import threading

    # 后台启动 Downloader 服务（不阻塞 UI）
    svc = get_services()
    if config.downloader.get("auto_start_services", True):
        logger.info("正在后台启动 Downloader 服务...")
        threading.Thread(target=svc.start_all, daemon=True).start()

    # 启动定时任务调度器
    sched = get_scheduler()
    sched.start()

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
    if fs:
        def _bg_sync():
            try:
                logger.info("启动时自动增量同步开始...")
                fs.sync_incremental()
                logger.info("启动时自动增量账号处理完成")
            except Exception as e:
                logger.warning(f"启动时自动增量账号处理失败（不影响使用）: {e}")
        threading.Thread(target=_bg_sync, daemon=True).start()

    get_collection_batch_manager().recover_interrupted_batches()

    yield

    # 关闭
    global single_work_client
    await get_collection_batch_manager().shutdown()
    sched.shutdown()
    svc.close()
    c = get_collector()
    if c:
        await c.close()
    if single_work_client:
        await single_work_client.aclose()
        single_work_client = None


app = FastAPI(title="DoukHub", version="2.2.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


# ========== 工具函数 ==========

def detect_platform(link: str) -> str:
    """根据链接识别平台"""
    if "douyin.com" in link or "iesdouyin.com" in link:
        return "抖音"
    elif "tiktok.com" in link:
        return "TikTok"
    elif "xiaohongshu.com" in link or "xhslink.com" in link or "rednote.com" in link:
        return "小红书"
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


def _sync_overview_context(db: Database) -> dict:
    histories, labels = _sync_recent_results(db)
    return {
        "stats": _sync_workflow_stats(db),
        "recent_histories": histories,
        "recent_labels": labels,
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


@app.get("/collect", response_class=HTMLResponse)
async def page_collect(request: Request):
    if request.query_params.get("mode") == "detail":
        return RedirectResponse("/collect/detail", status_code=307)

    h = get_history()
    tasks = h.get_tasks()
    sched = get_scheduler()
    jobs_info = {j["id"]: j for j in sched.get_jobs_info()}
    for task in tasks:
        job = jobs_info.get(f"task_{task['id']}")
        task["next_run"] = job["next_run"] if job else None
    return templates.TemplateResponse(request, "collect.html", context={
        "request": request,
        "tasks": tasks,
        "page": "collect_account",
        "collect_mode": "account",
    })


@app.get("/collect/overview", response_class=HTMLResponse)
async def page_collect_overview(request: Request):
    db = get_database()
    batches = db.list_collection_batches(limit=5)
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


@app.get("/schedule", response_class=HTMLResponse)
async def page_schedule(request: Request):
    h = get_history()
    tasks = h.get_tasks()
    sched = get_scheduler()
    return templates.TemplateResponse(request, "schedule.html", context={
        "request": request,
        "tasks": tasks,
        "jobs": sched.get_jobs_info(),
        "page": "schedule",
    })


@app.get("/settings", response_class=HTMLResponse)
async def page_settings(request: Request):
    return templates.TemplateResponse(request, "settings.html", context={
        "request": request,
        "config": config._data,
        "config_path": str(config._path),
        "page": "settings",
    })


@app.get("/duplicates", response_class=HTMLResponse)
async def page_duplicates(request: Request):
    """重复账号处理页面"""
    return templates.TemplateResponse(request, "duplicates.html", context={
        "request": request,
        "page": "duplicates",
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
    sched = get_scheduler()

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
            async with httpx.AsyncClient(timeout=5) as client:
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
        "jobs": sched.get_jobs_info(),
        "kernels": {
            "ttd": {"installed": ttd_kernel, "path": str(svc.ttd.path)},
            "xhs": {"installed": xhs_kernel, "path": str(svc.xhs.path)},
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
        async with httpx.AsyncClient(timeout=5) as client:
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
        async with httpx.AsyncClient(timeout=5) as client:
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

                valid = await c.validate_cookie(cookie_str, ck.get("平台", "抶音"))

                from datetime import datetime
                now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                db.update_cookie(record_id, {
                    "状态": "正常" if valid else "失效",
                    "验证时间": now_str,
                })

                if valid:
                    valid_count += 1
                else:
                    invalid_count += 1

                level = "ok" if valid else "error"
                icon = "✅" if valid else "❌"
                status = "有效" if valid else "已过期"
                msg = f"{icon} {label}: {status}"
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
            f"（新增 {result.created}，更新 {result.updated}，恢复 {result.revived}），"
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
        async with httpx.AsyncClient(timeout=3) as client:
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
    for i, collection in enumerate(to_process):
        if tm.is_cancelled(task.task_id):
            tm.update(task.task_id, status="cancelled")
            return
        share = collection["share_code"]
        platform = collection.get("平台") or "抖音"
        tm.add_log(task.task_id, f"[{i+1}/{len(to_process)}] {share}", "info")
        try:
            resolved_url = await s.collector.resolve_short_url(share, platform)
            sec_user_id = extract_sec_user_id(resolved_url, platform)
            if not sec_user_id:
                failed += 1
                if not resolved_url:
                    reason = "TTD 返回空(服务不可用或超时)"
                else:
                    reason = f"URL 无法提取 sec_user_id: {resolved_url[:120]}"
                s.db.update_collection(collection["record_id"], {"解析状态": "解析失败"})
                tm.add_log(task.task_id, f"X {share}: {reason}", "error")
                tm.update(task.task_id, success=success, failed=failed)
                continue
            existing = s.db.get_collection_by_sec_user_id(sec_user_id)
            if existing and existing["record_id"] != collection["record_id"]:
                new_level = s.merge_level(existing.get("等级"), collection.get("等级"))
                existing_tags = json.loads(existing.get("标签", "[]")) if existing.get("标签") else []
                new_tags = json.loads(collection.get("标签", "[]")) if collection.get("标签") else []
                merged_tags = s.merge_tags(existing_tags, new_tags)
                s.db.update_collection(existing["record_id"], {"等级": new_level, "标签": json.dumps(merged_tags)})
                s.db.delete_collection(collection["record_id"])
                success += 1
                tm.add_log(task.task_id, "OK 合并重复记录", "ok")
            else:
                s.db.update_collection(collection["record_id"], {"sec_user_id": sec_user_id, "解析状态": "已就绪"})
                success += 1
                tm.add_log(task.task_id, f"OK {share}: {sec_user_id}", "ok")
            tm.update(task.task_id, success=success, failed=failed)
            await asyncio.sleep(0.3)
        except Exception as e:
            failed += 1
            s.db.update_collection(collection["record_id"], {"解析状态": "解析失败"})
            tm.add_log(task.task_id, f"X {share}: {e}", "error")
            tm.update(task.task_id, success=success, failed=failed)
    tm.add_log(task.task_id, f"完成: 成功 {success} 失败 {failed}", "info")


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
    cookies = db.get_enabled_cookies()
    cookie_list = [ck.get("Cookie", "") for ck in cookies if ck.get("Cookie")]
    if not cookie_list:
        tm.add_log(task.task_id, "Cookie 表为空,仅生成账号基础数据,跳过详情获取", "warning")
    else:
        tm.add_log(task.task_id, f"已加载 {len(cookie_list)} 个 Cookie", "info")
    # 预检 TTD 服务是否可用(避免逐条等 15s 超时)
    ttd_available = False
    try:
        async with httpx.AsyncClient(timeout=3) as client:
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
    for i, collection in enumerate(to_process):
        if tm.is_cancelled(task.task_id):
            tm.update(task.task_id, status="cancelled")
            return
        sec_user_id = collection["sec_user_id"]
        platform = collection.get("平台") or "抖音"
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
                    "标签": json.dumps(merged_tags),
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
            cookie = cookie_list[i % len(cookie_list)]
            info = await s.collector.get_account_info(sec_user_id, platform, cookie)
            if not info or not info.get("nickname"):
                failed += 1
                reason = info.get("_error") if info else "TTD 返回空"
                s.db.update_account(account_id, {"获取状态": "获取失败"})
                tm.add_log(task.task_id, f"X {sec_user_id}: {reason}", "error")
                tm.update(task.task_id, success=success, failed=failed, skipped=skipped)
                continue
                s.db.update_account(account_id, {
                    "账号名称": info.get("nickname", ""),
                    "粉丝数": info.get("follower_count", 0),
                    "作品数": info.get("aweme_count", 0),
                    "签名": info.get("signature", ""),
                    "头像": info.get("avatar", ""),
                    "获取状态": "已获取",
                })
                # 更新内存缓存
                existing_accounts_map[sec_user_id] = {
                    **(existing_account or {}),
                    "账号名称": info.get("nickname", ""),
                    "获取状态": "已获取",
            }
            success += 1
            tm.add_log(task.task_id, f"OK 新增/更新账号: {info.get('nickname')}", "ok")
            tm.update(task.task_id, success=success, failed=failed, skipped=skipped)
            await asyncio.sleep(0.5)
        except Exception as e:
            failed += 1
            tm.add_log(task.task_id, f"X {sec_user_id}: {e}", "error")
            tm.update(task.task_id, success=success, failed=failed, skipped=skipped)
    tm.add_log(task.task_id, f"完成: 成功 {success} 失败 {failed} 跳过 {skipped}", "info")


# ========== 后台任务查询 ==========

@app.get("/api/tasks")
async def api_tasks_list():
    """列出所有任务(running/pending 在前,完成的按时间倒序)"""
    tm = get_task_manager()
    return {"tasks": [t.to_dict() for t in tm.list()]}


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
    cookies = db.get_enabled_cookies()
    cookie_list = [ck.get("Cookie", "") for ck in cookies if ck.get("Cookie")]
    if not cookie_list:
        tm.add_log(task.task_id, "Cookie 表为空,无法获取账号详情", "error")
        tm.update(task.task_id, status="failed", error="Cookie 表为空")
        return
    tm.add_log(task.task_id, f"已加载 {len(cookie_list)} 个 Cookie", "info")
    # 预检 TTD
    ttd_available = False
    try:
        async with httpx.AsyncClient(timeout=3) as client:
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
    for i, account in enumerate(to_fetch):
        if tm.is_cancelled(task.task_id):
            tm.update(task.task_id, status="cancelled")
            return
        sec_user_id = account["sec_user_id"]
        old_name = account.get("账号名称") or sec_user_id[:20]
        tm.add_log(task.task_id, f"[{i+1}/{len(to_fetch)}] {old_name}", "info")
        try:
            cookie = cookie_list[i % len(cookie_list)]
            platform = account.get("平台") or "抖音"
            info = await col.get_account_info(sec_user_id, platform, cookie)
            nickname = info.get("nickname", "") if info else ""
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
                tm.add_log(task.task_id, f"OK {nickname} | 粉丝 {info.get('follower_count', 0)} | 作品 {info.get('aweme_count', 0)}", "ok")
            else:
                failed += 1
                reason = info.get("_error", "无法获取资料") if info else "TTD 返回空"
                db.update_account(account.get("record_id", ""), {"获取状态": "获取失败"})
                tm.add_log(task.task_id, f"X {old_name}: {reason}", "error")
            tm.update(task.task_id, success=success, failed=failed)
            await asyncio.sleep(0.5)
        except Exception as e:
            failed += 1
            tm.add_log(task.task_id, f"X {old_name}: {e}", "error")
            tm.update(task.task_id, success=success, failed=failed)
    tm.add_log(task.task_id, f"完成: 成功 {success} 失败 {failed}", "info")


@app.post("/api/sync/v2/all")
async def api_sync_v2_all(request: Request):
    """处理账号数据（使用新同步器）"""
    s = get_syncer_v2()
    if not s:
        return JSONResponse(
            {"success": False, "message": "飞书未配置"},
            status_code=400,
        )
    
    try:
        data = await request.json()
        text = data.get("text", "")
        
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


class CollectionRetryRequest(BaseModel):
    mode: Literal["incremental", "full"] = "incremental"


class SingleWorkResolveRequest(BaseModel):
    links: str
    resolve_mode: str = "auto"


class SingleWorkDownloadRequest(SingleWorkResolveRequest):
    target_dir: str
    filename_template: str = "{create_time} {author} {title}"
    filename_overrides: dict[str, str] = Field(default_factory=dict)
    asset_indexes: list[int] = Field(default_factory=list)
    include_music: bool = False
    include_static_cover: bool = False
    include_dynamic_cover: bool = False
    work: dict | None = None  # 前端可传入已解析的 work 数据，跳过二次解析


class SingleWorkRetryRequest(BaseModel):
    target_dir: str = ""
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
    sort_field: str = "",
    sort_order: str = "desc",
    filter_field: str = "",
    filter_value: str = "",
    filter_op: str = "",
):
    """获取表数据，支持分页、搜索、排序、列级筛选。返回 {records, total, limit, offset}"""
    db = get_database()

    # 验证表名
    if table_name not in db.VALID_TABLES:
        return JSONResponse({"success": False, "message": "无效的表名"}, status_code=400)

    result = db.query_table(
        table_name,
        limit=limit,
        offset=offset,
        search=search,
        sort_field=sort_field or None,
        sort_order=sort_order,
        filter_field=filter_field or None,
        filter_value=filter_value or None,
        filter_op=filter_op or None,
    )
    return result


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
async def api_database_export_csv(table_name: str, search: str = ""):
    """全量导出表为 CSV（含 BOM 头，Excel 可直接打开）"""
    import csv
    import io
    from fastapi.responses import StreamingResponse

    db = get_database()
    if table_name not in db.VALID_TABLES:
        return JSONResponse({"success": False, "message": "无效的表名"}, status_code=400)

    schema = db.get_table_schema(table_name)
    col_names = [s["name"] for s in schema]
    result = db.query_table(table_name, limit=10**9, offset=0, search=search)
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
            success = db.delete_account(record_id)
            # 联动：把分享表中对应对 sec_user_id 的记录标记为「已删除」
            if success:
                acc = db.get_account_by_id(record_id)
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
    
    # 验证表名
    valid_tables = ["share_cache", "account_cache", "cookie_cache", "collection_history", "scheduled_tasks"]
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
        elif table_name == "collection_history":
            with db._connect() as conn:
                conn.execute("DELETE FROM collection_history")
                conn.commit()
                success = True
        elif table_name == "scheduled_tasks":
            with db._connect() as conn:
                conn.execute("DELETE FROM scheduled_tasks")
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
    fs = get_feishu_syncer()
    if not fs:
        return JSONResponse({"success": False, "message": "云端未配置"}, status_code=400)
    tm = get_task_manager()
    task = tm.create("cloud_sync")
    asyncio.create_task(tm.run_serial(task, _run_cloud_sync))
    return {"task_id": task.task_id, "status": "pending", "message": "已加入队列"}


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


# --- 采集（使用新数据库）---

@app.post("/api/collect/v2/account")
async def api_collect_v2_account(request: Request):
    """整号采集（使用新数据库）- SSE 实时进度"""
    db = get_database()
    c = get_collector()

    data = await request.json()
    account_names = data.get("account_names", "")
    rating_min = data.get("rating_min", 3)

    # 从数据库获取账号
    accounts = db.get_all_accounts()
    if account_names:
        names = [n.strip() for n in account_names.split(",") if n.strip()]
        accounts = [a for a in accounts if a.get("账号名称") in names]
    else:
        accounts = [a for a in accounts if a.get("等级", 0) >= rating_min and a.get("sec_user_id")]

    if not accounts:
        return JSONResponse({"success": False, "message": "没有符合条件的账号"}, status_code=400)

    # 按等级排序
    accounts.sort(key=lambda a: a.get("等级", 0), reverse=True)

    import json

    async def collect_stream():
        try:
            yield f"data: {json.dumps({'type': 'start', 'message': '开始采集'})}\n\n"
            yield f"data: {json.dumps({'type': 'stats', 'total': len(accounts), 'success': 0, 'failed': 0})}\n\n"

            # 获取 Cookie
            cookies = db.get_enabled_cookies()
            cookie_list = [ck.get("Cookie", "") for ck in cookies]

            success = 0
            failed = 0

            for i, account in enumerate(accounts):
                account_name = account.get("账号名称") or account.get("sec_user_id", "")
                sec_user_id = account.get("sec_user_id", "")
                platform = account.get("平台", "抖音")
                collection_type = account.get("采集类型", "发布")

                yield f"data: {json.dumps({'type': 'progress', 'message': f'采集 [{i+1}/{len(accounts)}]: {account_name}'})}\n\n"

                try:
                    import time
                    start_time = time.time()

                    # 获取 Cookie
                    cookie = cookie_list[i % len(cookie_list)] if cookie_list else ""

                    # 调用 TTD API 采集
                    result = await c.collect_account(
                        Account(
                            name=account_name,
                            platform=platform,
                            sec_user_id=sec_user_id,
                            collection_type=collection_type,
                        ),
                        cookie=cookie,
                    )

                    end_time = time.time()

                    if result.status == "success":
                        success += 1
                        # 记录历史
                        db.add_history({
                            "账号名称": account_name,
                            "平台": platform,
                            "sec_user_id": sec_user_id,
                            "采集类型": collection_type,
                            "等级": account.get("等级"),
                            "状态": "成功",
                            "作品数": result.works_count,
                            "开始时间": datetime.fromtimestamp(start_time).strftime("%Y-%m-%d %H:%M:%S"),
                            "结束时间": datetime.fromtimestamp(end_time).strftime("%Y-%m-%d %H:%M:%S"),
                            "耗时秒数": end_time - start_time,
                        })
                        yield f"data: {json.dumps({'type': 'log', 'level': 'ok', 'message': f'✅ {account_name}: {result.works_count} 个作品'})}\n\n"
                    else:
                        failed += 1
                        db.add_history({
                            "账号名称": account_name,
                            "平台": platform,
                            "sec_user_id": sec_user_id,
                            "采集类型": collection_type,
                            "等级": account.get("等级"),
                            "状态": "失败",
                            "作品数": 0,
                            "开始时间": datetime.fromtimestamp(start_time).strftime("%Y-%m-%d %H:%M:%S"),
                            "结束时间": datetime.fromtimestamp(end_time).strftime("%Y-%m-%d %H:%M:%S"),
                            "耗时秒数": end_time - start_time,
                            "错误信息": result.message,
                        })
                        yield f"data: {json.dumps({'type': 'log', 'level': 'error', 'message': f'❌ {account_name}: {result.message}'})}\n\n"

                    yield f"data: {json.dumps({'type': 'stats', 'total': len(accounts), 'success': success, 'failed': failed})}\n\n"

                except Exception as e:
                    failed += 1
                    yield f"data: {json.dumps({'type': 'log', 'level': 'error', 'message': f'❌ {account_name}: {e}'})}\n\n"

            yield f"data: {json.dumps({'type': 'complete', 'success': failed == 0, 'message': f'采集完成: 成功 {success} 个, 失败 {failed} 个', 'total': len(accounts), 'success_count': success, 'failed': failed})}\n\n"

        except Exception as e:
            logger.error(f"采集失败: {e}")
            yield f"data: {json.dumps({'type': 'complete', 'success': False, 'message': f'采集失败: {str(e)}', 'total': 0, 'success_count': 0, 'failed': 1})}\n\n"

    return StreamingResponse(
        collect_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# --- 原有采集 ---

@app.post("/api/collect/account")
async def api_collect_account(
    account_names: str = Form(""),
    rating_min: int = Form(3),
):
    """整号采集"""
    s = get_syncer()
    if not s:
        return JSONResponse({"success": False, "message": "无可用账号"}, status_code=400)

    accounts = s.load_local_accounts()
    if account_names:
        names = [n.strip() for n in account_names.split(",") if n.strip()]
        accounts = [a for a in accounts if a.name in names and a.enabled]
    else:
        accounts = [a for a in accounts if a.enabled and a.rating >= rating_min]

    if not accounts:
        return JSONResponse({"success": False, "message": "没有符合条件的账号"}, status_code=400)

    accounts.sort(key=lambda a: a.rating, reverse=True)

    c = get_collector()
    h = get_history()
    results = await c.collect_batch(
        accounts,
        concurrency=config.concurrent_accounts,
    )

    for r in results:
        h.add_record({
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

    return {
        "success": True,
        "message": f"完成 {len(results)} 个账号的采集",
        "results": [
            {
                "name": r.account_name,
                "platform": r.platform,
                "status": r.status,
                "works_count": r.works_count,
                "message": r.message,
                "duration": f"{r.duration:.1f}s",
            }
            for r in results
        ],
    }


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
    prefs = config.single_work
    templates = prefs.get("templates", []) or []
    if not templates:
        templates = [{
            "id": "default",
            "name": "默认模板",
            "template": "{create_time} {author} {title}",
            "is_default": True,
            "created_at": "2026-08-16 00:00:00",
            "updated_at": "2026-08-16 00:00:00",
        }]
    default_id = prefs.get("default_template_id") or "default"
    if not any(t.get("id") == default_id for t in templates):
        default_id = templates[0]["id"]
    synced = []
    for tpl in templates:
        copy = dict(tpl)
        copy["is_default"] = copy.get("id") == default_id
        synced.append(copy)
    return {
        "download_path": prefs.get("download_path", ""),
        "recent_dirs": prefs.get("recent_dirs", []) or [],
        "default_template_id": default_id,
        "templates": synced,
    }


def _save_single_work_preferences(data: dict) -> dict:
    import time

    prefs = {
        "download_path": str(data.get("download_path") or ""),
        "recent_dirs": [],
        "default_template_id": str(data.get("default_template_id") or ""),
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
        prefs["templates"].append({
            "id": tpl_id,
            "name": name,
            "template": template,
            "is_default": bool(tpl.get("is_default")),
            "created_at": old.get("created_at") or now,
            "updated_at": now,
        })
    if not prefs["templates"]:
        prefs["templates"].append({
            "id": "default",
            "name": "默认模板",
            "template": "{create_time} {author} {title}",
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
        }),
    )
    try:
        if work is None:
            work = await single_work.fetch_work(client, ttd_url, link, platform)
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
        )
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
    cookies = db.get_enabled_cookies()
    cookie_list = [ck.get("Cookie", "") for ck in cookies if ck.get("Cookie")]
    works = []
    errors = []
    for i, (link, platform) in enumerate(links):
        try:
            cookie = cookie_list[i % len(cookie_list)] if cookie_list else ""
            works.append(
                await single_work.fetch_work(client, ttd_url, link, platform, cookie, mode=resolve_mode)
            )
        except Exception as error:
            errors.append({"link": link, "message": str(error)})
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
    cookies = db.get_enabled_cookies()
    cookie_list = [ck.get("Cookie", "") for ck in cookies if ck.get("Cookie")]

    async def resolve_stream():
        import asyncio as _aio
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

    return StreamingResponse(
        resolve_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/collection/works/download")
async def api_download_single_works(request: SingleWorkDownloadRequest):
    try:
        unsafe_template = _is_unsafe_filename_template(request.filename_template)
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
    target = Path(request.target_dir).expanduser()
    if not target.exists() or not target.is_dir():
        return JSONResponse(
            {"success": False, "message": "保存目录不存在"},
            status_code=400,
        )

    db = get_database()
    client = get_single_work_client()
    ttd_url = f"http://127.0.0.1:{config.ttd_port}"
    cookies = db.get_enabled_cookies()
    cookie_list = [ck.get("Cookie", "") for ck in cookies if ck.get("Cookie")]
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
                    request.filename_template,
                    work=work,
                )
                result["message"] = str(error)
                results.append(result)
                continue
        override = request.filename_overrides.get(work.get("id", ""), "")
        results.append(
            await _download_single_work_and_record(
                db, client, ttd_url, link, platform, target,
                request.filename_template,
                filename_override=override,
                asset_indexes=request.asset_indexes,
                include_music=request.include_music,
                include_static_cover=request.include_static_cover,
                include_dynamic_cover=request.include_dynamic_cover,
                work=work,
            )
        )
    return {
        "success": any(item["status"] == "success" for item in results),
        "results": results,
    }


@app.post("/api/collection/works/download-stream")
async def api_download_single_works_stream(request: SingleWorkDownloadRequest):
    """一键解析+下载 SSE 流式接口，实时推送解析和下载进度"""
    try:
        unsafe_template = _is_unsafe_filename_template(request.filename_template)
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
    target = Path(request.target_dir).expanduser()
    if not target.exists() or not target.is_dir():
        return JSONResponse(
            {"success": False, "message": "保存目录不存在"},
            status_code=400,
        )

    db = get_database()
    client = get_single_work_client()
    ttd_url = f"http://127.0.0.1:{config.ttd_port}"
    total = len(links)
    # 从数据库获取 Cookie
    cookies = db.get_enabled_cookies()
    cookie_list = [ck.get("Cookie", "") for ck in cookies if ck.get("Cookie")]

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
                    request.filename_template,
                    filename_override=override,
                    asset_indexes=request.asset_indexes,
                    include_music=request.include_music,
                    include_static_cover=request.include_static_cover,
                    include_dynamic_cover=request.include_dynamic_cover,
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


class QuickAddShareRequest(BaseModel):
    sec_user_id: str
    platform: str = "抖音"
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
    target_dir = Path(request.target_dir or old.get("target_dir") or str(config.download_path)).expanduser()
    if not target_dir.exists() or not target_dir.is_dir():
        return JSONResponse(
            {"success": False, "message": "保存目录不存在"},
            status_code=400,
        )
    template = request.filename_template or old.get("filename_template") or "{create_time} {author} {title}"
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

    client = get_single_work_client()
    ttd_url = f"http://127.0.0.1:{config.ttd_port}"
    result = await _download_single_work_and_record(
        db, client, ttd_url, link, platform, target_dir, template,
        filename_override=filename_override,
        asset_indexes=asset_indexes,
        include_music=include_music,
        include_static_cover=include_static_cover,
        include_dynamic_cover=include_dynamic_cover,
        old_history=old,
    )
    return {"success": result["status"] == "success", "results": [result]}


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
    account_created_after: str = ""
    skip_recent_days: int = 0
    is_default: bool = False


@app.get("/api/collection/presets")
async def api_list_collection_presets():
    presets = get_database().list_collection_presets()
    # 如果没有方案，自动初始化默认方案
    if not presets:
        db = get_database()
        db.create_collection_preset({
            "name": "日常 4星+（增量）",
            "rating_min": 4,
            "tags": "",
            "account_names": "",
            "platform": "douyin",
            "mode": "incremental",
            "is_default": True,
        })
        db.create_collection_preset({
            "name": "日常 3星+（增量）",
            "rating_min": 3,
            "platform": "douyin",
            "mode": "incremental",
        })
        db.create_collection_preset({
            "name": "TikTok 全部（增量）",
            "rating_min": 3,
            "platform": "tiktok",
            "mode": "incremental",
        })
        db.create_collection_preset({
            "name": "连通性测试",
            "rating_min": 4,
            "tags": "",
            "account_names": "",
            "platform": "douyin",
            "mode": "incremental",
        })
        presets = db.list_collection_presets()
    return {"presets": presets}


@app.post("/api/collection/presets")
async def api_create_collection_preset(request: CollectionPresetRequest):
    preset = get_database().create_collection_preset(request.model_dump())
    return {"success": True, "preset": preset}


@app.put("/api/collection/presets/{preset_id}")
async def api_update_collection_preset(preset_id: int, request: CollectionPresetRequest):
    preset = get_database().update_collection_preset(preset_id, request.model_dump())
    if not preset:
        return JSONResponse({"success": False, "message": "方案不存在"}, status_code=404)
    return {"success": True, "preset": preset}


@app.delete("/api/collection/presets/{preset_id}")
async def api_delete_collection_preset(preset_id: int):
    preset = get_database().get_collection_preset(preset_id)
    if preset and preset.get("is_default"):
        return JSONResponse({"success": False, "message": "不能删除默认方案"}, status_code=400)
    ok = get_database().delete_collection_preset(preset_id)
    return {"success": ok}


@app.post("/api/collection/presets/{preset_id}/default")
async def api_set_default_collection_preset(preset_id: int):
    ok = get_database().set_default_collection_preset(preset_id)
    return {"success": ok}


@app.get("/api/collection/defaults")
async def api_get_collection_defaults():
    """获取增量采集的全局默认设置"""
    return {"defaults": config.collection_defaults}


@app.put("/api/collection/defaults")
async def api_save_collection_defaults(request: Request):
    """保存增量采集的全局默认设置"""
    data = await request.json()
    folder_name = str(data.get("folder_name") or "").strip()
    name_format = str(data.get("name_format") or "").strip()
    config._data["collection_defaults"] = {
        "folder_name": folder_name,
        "name_format": name_format,
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
    preset = db.get_collection_preset(pid)
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


# ========== 采集批次 ==========

@app.post("/api/collection/batches")
async def api_start_collection_batch(request: CollectionBatchRequest):
    db = get_database()
    manager = get_collection_batch_manager()
    # 如果指定了 preset_id，从方案中读取参数覆盖请求字段
    if request.preset_id is not None:
        preset = db.get_collection_preset(request.preset_id)
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
        account_created_after = ""
        skip_recent_days = 0
    # 方案未设值时使用全局默认
    defaults = config.collection_defaults
    if not folder_name:
        folder_name = defaults.get("folder_name", "Download")
    if not name_format:
        name_format = defaults.get("name_format", "create_time type nickname desc")
    platforms = (
        ("douyin", "tiktok") if platform == "all" else (platform,)
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
            name_format=name_format,
            account_created_after=account_created_after,
            skip_recent_days=skip_recent_days,
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
async def api_collection_batch_detail(batch_id: str):
    db = get_database()
    batch = db.get_collection_batch(batch_id)
    if not batch:
        return JSONResponse({"success": False, "message": "批次不存在"}, status_code=404)
    items = db.get_collection_batch_items(batch_id)
    accounts = {
        row["record_id"]: row
        for row in db.get_all_accounts()
        if row.get("record_id")
    }
    for item in items:
        account = accounts.get(item.get("account_record_id"))
        item["last_collected_at"] = account.get("last_collected_at") if account else None
    return {
        "batch": batch,
        "items": items,
        "log": get_collection_batch_manager().read_log(batch_id),
    }


@app.post("/api/collection/batches/{batch_id}/cancel")
async def api_cancel_collection_batch(batch_id: str):
    ok = get_collection_batch_manager().cancel(batch_id)
    return {
        "success": ok,
        "message": "已请求取消" if ok else "批次不存在或已结束",
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


# --- 定时任务 ---

@app.get("/api/schedule")
async def api_list_schedule():
    """获取定时任务列表"""
    h = get_history()
    sched = get_scheduler()
    tasks = h.get_tasks()
    jobs_info = {j["id"]: j for j in sched.get_jobs_info()}
    for task in tasks:
        job = jobs_info.get(f"task_{task['id']}")
        task["next_run"] = job["next_run"] if job else None
    return {"tasks": tasks}


@app.post("/api/schedule")
async def api_add_schedule(
    name: str = Form(...),
    cron_expression: str = Form(...),
    rating_filter: str = Form("3,4,5"),
):
    h = get_history()
    sched = get_scheduler()
    task_id = h.add_task(name, cron_expression, rating_filter)
    sched.add_task(task_id)
    return {"success": True, "task_id": task_id}


@app.post("/api/schedule/{task_id}/delete")
async def api_delete_schedule(task_id: int):
    h = get_history()
    sched = get_scheduler()
    sched.remove_task(task_id)
    h.delete_task(task_id)
    return {"success": True}


@app.post("/api/schedule/{task_id}/toggle")
async def api_toggle_schedule(task_id: int):
    h = get_history()
    sched = get_scheduler()
    tasks = h.get_tasks()
    for t in tasks:
        if t["id"] == task_id:
            new_enabled = not t["enabled"]
            h.update_task(task_id, {"enabled": new_enabled})
            sched.toggle_task(task_id, new_enabled)
            return {"success": True, "enabled": new_enabled}
    return {"success": False, "message": "任务不存在"}


@app.post("/api/schedule/{task_id}/run")
async def api_run_schedule(task_id: int):
    """立即执行定时任务"""
    h = get_history()
    tasks = h.get_tasks()
    for t in tasks:
        if t["id"] == task_id:
            rating_filter = set()
            for r in t.get("rating_filter", "3,4,5").split(","):
                try:
                    rating_filter.add(int(r.strip()))
                except ValueError:
                    pass

            sched = get_scheduler()
            await sched._execute_task(
                task_id=task_id,
                task_name=t["name"],
                rating_filter=rating_filter,
            )
            return {"success": True, "message": f"任务 {t['name']} 已执行"}
    return {"success": False, "message": "任务不存在"}


# --- 设置 ---

@app.get("/api/settings")
async def api_get_settings():
    """获取当前配置信息"""
    return config._data


@app.post("/api/settings")
async def api_save_settings(request: Request):
    global collector, syncer, feishu_client
    data = await request.json()
    for key, value in data.items():
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

    # 停止调度器
    sched = get_scheduler()
    sched.shutdown()

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

    # 停止调度器
    sched = get_scheduler()
    sched.shutdown()

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
    cookies = db.get_enabled_cookies()
    cookie_list = [ck.get("Cookie", "") for ck in cookies if ck.get("Cookie")]

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

    target_str = body.target_dir or str(config.download_path)
    target = Path(target_str).expanduser()
    target.mkdir(parents=True, exist_ok=True)

    resolve_mode = body.resolve_mode or config.api_config.get("default_resolve_mode", "auto")
    db = get_database()
    client = get_single_work_client()
    ttd_url = f"http://127.0.0.1:{config.ttd_port}"
    cookies = db.get_enabled_cookies()
    cookie_list = [ck.get("Cookie", "") for ck in cookies if ck.get("Cookie")]

    results = []
    for i, (link, platform) in enumerate(links):
        cookie = cookie_list[i % len(cookie_list)] if cookie_list else ""
        try:
            work = await single_work.fetch_work(
                client, ttd_url, link, platform, cookie, mode=resolve_mode
            )
            result = await _download_single_work_and_record(
                db, client, ttd_url, link, platform, target,
                body.filename_template,
                include_music=body.include_music,
                include_static_cover=body.include_static_cover,
                include_dynamic_cover=body.include_dynamic_cover,
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
