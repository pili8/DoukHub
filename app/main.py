"""DoukHub 主入口 — FastAPI Web 应用"""
import asyncio
import json
import logging
import httpx
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from .core.config import Config
from .core.feishu import FeishuClient
from .core.collector import Collector, Account
from .core.cookie_pool import CookiePool
from .core.syncer import Syncer
from .core.syncer_v2 import Syncer as SyncerV2
from .core.database import Database
from .core.feishu_sync import FeishuSyncer
from .core.history import HistoryDB
from .core.scheduler import TaskScheduler
from .core.tasks import get_task_manager
from .core.link_resolver import extract_sec_user_id, build_profile_url
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

    # TTD/XHS 心跳监控：30秒检查一次，连续2次失败自动重启
    def _health_loop():
        import time as _t
        _t.sleep(15)
        while True:
            try:
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
                logger.info("启动时自动增量同步完成")
            except Exception as e:
                logger.warning(f"启动时自动增量同步失败（不影响使用）: {e}")
        threading.Thread(target=_bg_sync, daemon=True).start()

    yield

    # 关闭
    sched.shutdown()
    svc.close()
    c = get_collector()
    if c:
        await c.close()


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


# ========== 页面路由 ==========

@app.get("/", response_class=HTMLResponse)
async def page_sync_overview_redirect(request: Request):
    """根路径重定向到同步概览页"""
    return templates.TemplateResponse(request, "sync/overview.html", context={
        "request": request,
        "page": "sync_overview",
        "accounts": [],
    })


@app.get("/sync", response_class=HTMLResponse)
async def page_sync_overview_redirect2(request: Request):
    """旧 /sync 重定向到 /sync/overview"""
    return templates.TemplateResponse(request, "sync/overview.html", context={
        "request": request,
        "page": "sync_overview",
        "accounts": [],
    })


@app.get("/sync/overview", response_class=HTMLResponse)
async def page_sync_overview(request: Request):
    """同步概览页 - 一键执行 + 账号列表"""
    db = get_database()
    accounts = db.get_all_accounts()
    import json as _json
    for acc in accounts:
        tags_str = acc.get("标签", "")
        if tags_str:
            try:
                acc["tags_list"] = _json.loads(tags_str)
            except (ValueError, TypeError):
                acc["tags_list"] = []
        else:
            acc["tags_list"] = []
    return templates.TemplateResponse(request, "sync/overview.html", context={
        "request": request,
        "accounts": accounts,
        "page": "sync_overview",
    })


@app.get("/sync/import", response_class=HTMLResponse)
async def page_sync_import(request: Request):
    """导入采集表页面"""
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
        "page": "sync_resolve",
    })


@app.get("/sync/account", response_class=HTMLResponse)
async def page_sync_account(request: Request):
    """同步账号表页面"""
    db = get_database()
    history = db.get_sync_history("sync_account", limit=20)
    return templates.TemplateResponse(request, "sync/account.html", context={
        "request": request,
        "history": history,
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
        "page": "sync_refresh",
    })


@app.get("/sync/cloud", response_class=HTMLResponse)
async def page_sync_cloud(request: Request):
    """云端同步页面"""
    db = get_database()
    history = db.get_sync_history("cloud_sync", limit=20)
    feishu_ok = get_feishu() is not None
    return templates.TemplateResponse(request, "sync/cloud.html", context={
        "request": request,
        "history": history,
        "feishu_ok": feishu_ok,
        "page": "sync_cloud",
    })


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
    s = get_syncer()
    accounts = s.load_local_accounts() if s else []
    h = get_history()
    tasks = h.get_tasks()
    sched = get_scheduler()
    jobs_info = {j["id"]: j for j in sched.get_jobs_info()}
    for task in tasks:
        job = jobs_info.get(f"task_{task['id']}")
        task["next_run"] = job["next_run"] if job else None
    return templates.TemplateResponse(request, "collect.html", context={
        "request": request,
        "accounts": accounts,
        "tasks": tasks,
        "page": "collect",
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

    return StreamingResponse(validate_stream(), media_type="text/event-stream")


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
            yield f"data: {json.dumps({'type': 'start', 'message': '开始同步'})}\n\n"
            
            # 1. 读取采集表
            yield f"data: {json.dumps({'type': 'progress', 'message': '连接飞书...'})}\n\n"
            records = s.feishu.get_all_records(s.app_token, s.collection_table_id)
            total = len(records)
            
            yield f"data: {json.dumps({'type': 'stats', 'total': total, 'success': 0, 'api_calls': 0, 'failed': 0})}\n\n"
            yield f"data: {json.dumps({'type': 'progress', 'message': f'读取采集表: {total} 条记录'})}\n\n"
            
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
                    
                    # 更新采集表状态
                    s._update_collection_status(record_id, "已解析", "", sec_user_id)
                    
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
            yield f"data: {json.dumps({'type': 'complete', 'success': True, 'message': f'同步完成: 新增 {new_count}, 更新 {updated_count}', 'total': total, 'new_count': new_count, 'updated_count': updated_count, 'api_calls': api_calls, 'error_count': failed, 'errors': errors[:5]})}\n\n"
            
        except Exception as e:
            logger.error(f"同步失败: {e}")
            yield f"data: {json.dumps({'type': 'complete', 'success': False, 'message': f'同步失败: {str(e)}', 'total': 0, 'new_count': 0, 'updated_count': 0, 'api_calls': 0, 'error_count': 1, 'errors': [str(e)]})}\n\n"
    
    return StreamingResponse(sync_stream(), media_type="text/event-stream")


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
    """步骤1：导入采集表（使用新同步器）"""
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
        import_logs = [{"level": "info", "message": "开始导入采集表"}]
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
    """步骤2:更新采集表(获取 sec_user_id) - 后台任务,立即返回 task_id"""
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
    tm.add_log(task.task_id, "开始更新采集表", "info")
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
    to_process = [c for c in collections if not c.get("sec_user_id") and str(c.get("share_code", "")).strip()]
    tm.update(task.task_id, total=len(to_process))
    if not to_process:
        tm.add_log(task.task_id, "没有需要解析的记录（所有采集记录已获取 sec_user_id）", "info")
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
                s.db.update_collection(collection["record_id"], {"同步错误": reason})
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
                s.db.update_collection(collection["record_id"], {"sec_user_id": sec_user_id, "已解析": True, "同步错误": None})
                success += 1
                tm.add_log(task.task_id, f"OK {share}: {sec_user_id}", "ok")
            tm.update(task.task_id, success=success, failed=failed)
            await asyncio.sleep(0.3)
        except Exception as e:
            failed += 1
            s.db.update_collection(collection["record_id"], {"同步错误": str(e)})
            tm.add_log(task.task_id, f"X {share}: {e}", "error")
            tm.update(task.task_id, success=success, failed=failed)
    tm.add_log(task.task_id, f"完成: 成功 {success} 失败 {failed}", "info")


@app.post("/api/sync/v2/sync-account")
async def api_sync_v2_sync_account():
    """步骤3:同步账号表 - 后台任务,立即返回 task_id"""
    s = get_syncer_v2()
    if not s:
        return JSONResponse({"success": False, "message": "飞书未配置"}, status_code=400)
    tm = get_task_manager()
    task = tm.create("sync_account")
    asyncio.create_task(tm.run_serial(task, _run_sync_account))
    return {"task_id": task.task_id, "status": "pending", "message": "已加入队列"}


async def _run_sync_account(task):
    """后台执行:同步账号表(走 TTD API 拉账号详情)。串行队列内运行。"""
    import json
    tm = get_task_manager()
    s = get_syncer_v2()
    if not s:
        tm.add_log(task.task_id, "飞书未配置", "error")
        tm.update(task.task_id, status="failed", error="飞书未配置")
        return
    tm.add_log(task.task_id, "开始同步账号表", "info")
    collections = s.db.get_all_collections()
    to_process = [c for c in collections if s.is_ready_for_account(c)]
    tm.update(task.task_id, total=len(to_process))
    if not to_process:
        tm.add_log(task.task_id, "没有需要同步的记录（请先执行第二步解析账号标识）", "info")
        tm.add_log(task.task_id, "完成: 0 条", "ok")
        return
    tm.add_log(task.task_id, f"需要处理 {len(to_process)} 条记录", "info")
    db = get_database()
    cookies = db.get_enabled_cookies()
    cookie_list = [ck.get("Cookie", "") for ck in cookies if ck.get("Cookie")]
    if not cookie_list:
        tm.add_log(task.task_id, "Cookie 表为空,仅同步账号基础数据,跳过详情获取", "warning")
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
        # 跳过 API 调用的条件：账号表已有 且 已获取信息=是
        skip_api = existing_account and existing_account.get("已获取信息")
        tm.add_log(task.task_id, f"[{i+1}/{len(to_process)}] {sec_user_id}", "info")
        try:
            if existing_account:
                # 账号已存在:合并等级标签（无论是否调API都执行）
                new_level = s.merge_level(existing_account.get("等级"), collection.get("等级"))
                existing_tags = json.loads(existing_account.get("标签", "[]")) if existing_account.get("标签") else []
                new_tags = json.loads(collection.get("标签", "[]")) if collection.get("标签") else []
                merged_tags = s.merge_tags(existing_tags, new_tags)
                s.db.update_account(existing_account["record_id"], {
                    "等级": new_level,
                    "标签": json.dumps(merged_tags),
                })
                account_id = existing_account["record_id"]
                if skip_api:
                    # 已获取过信息:只合并数据,不重复调API
                    skipped += 1
                    tm.add_log(task.task_id, f"SKIP {sec_user_id}: 已获取信息,仅合并等级标签", "info")
                    tm.update(task.task_id, success=success, failed=failed, skipped=skipped)
                    continue
            else:
                # 新账号:先复活软删除记录(如有),再插入
                revived_id = db.revive_account_if_deleted(sec_user_id)
                if revived_id:
                    s.db.update_account(revived_id, {
                        "账号名称": "",
                        "平台": platform,
                        "链接": build_profile_url(sec_user_id, platform),
                        "等级": collection.get("等级"),
                        "标签": collection.get("标签"),
                        "已获取信息": False,
                    })
                    account_id = revived_id
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
                        "已获取信息": False,
                    })
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
                s.db.update_account(account_id, {"获取错误": reason})
                s.db.update_collection(collection["record_id"], {"同步错误": reason})
                tm.add_log(task.task_id, f"X {sec_user_id}: {reason}", "error")
                tm.update(task.task_id, success=success, failed=failed, skipped=skipped)
                continue
            s.db.update_account(account_id, {
                "账号名称": info.get("nickname", ""),
                "粉丝数": info.get("follower_count", 0),
                "作品数": info.get("aweme_count", 0),
                "签名": info.get("signature", ""),
                "头像": info.get("avatar", ""),
                "已获取信息": True,
                "获取错误": None,
            })
            # 更新内存缓存
            existing_accounts_map[sec_user_id] = {
                **(existing_account or {}),
                "账号名称": info.get("nickname", ""),
                "已获取信息": True,
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
    to_fetch = [a for a in accounts if a.get("sec_user_id") and not a.get("已获取信息")]
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
                    "已获取信息": True,
                    "获取错误": None,
                })
                success += 1
                tm.add_log(task.task_id, f"OK {nickname} | 粉丝 {info.get('follower_count', 0)} | 作品 {info.get('aweme_count', 0)}", "ok")
            else:
                failed += 1
                reason = info.get("_error", "无法获取资料") if info else "TTD 返回空"
                db.update_account(account.get("record_id", ""), {"获取错误": reason})
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
    """一键同步（使用新同步器）"""
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
            "message": "一键同步完成",
            **results
        }
    except Exception as e:
        logger.error(f"一键同步失败: {e}")
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


@app.post("/api/import/collection")
async def api_import_collection(request: ImportCollectionRequest):
    """将解析后的数据写入飞书采集表（批量写入，一次最多500条）"""
    f = get_feishu()
    if not f:
        return JSONResponse({"success": False, "message": "飞书未配置"}, status_code=400)

    app_token = config.feishu.get("app_token", "")
    table_id = config.feishu.get("collection_table_id", "")
    if not app_token or not table_id:
        return JSONResponse({"success": False, "message": "未配置采集表 Table ID"}, status_code=400)

    # 构建批量记录
    records = []
    for item in request.items:
        fields = {
            "地址": item.link,
            "等级": item.rating,
            "同步状态": "待同步",
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
    valid_tables = ["collection_cache", "account_cache", "cookie_cache"]
    if table_name not in valid_tables:
        return JSONResponse({"success": False, "message": "无效的表名"}, status_code=400)
    
    try:
        if table_name == "collection_cache":
            success = db.delete_collection(record_id)
        elif table_name == "account_cache":
            success = db.delete_account(record_id)
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
    valid_tables = ["collection_cache", "account_cache", "cookie_cache", "collection_history", "scheduled_tasks"]
    if table_name not in valid_tables:
        return JSONResponse({"success": False, "message": "无效的表名"}, status_code=400)
    
    try:
        if table_name == "collection_cache":
            success = db.clear_collection_cache()
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
    """获取各表的详细统计（含同步状态、启用状态等细分）"""
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

    return StreamingResponse(collect_stream(), media_type="text/event-stream")


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


# ========== 启动 ==========

def run():
    """启动 DoukHub"""
    import uvicorn
    logger.info("DoukHub 正在启动...")
    logger.info(f"本机访问: http://127.0.0.1:2999")
    logger.info(f"局域网访问: http://0.0.0.0:2999")
    uvicorn.run(app, host="0.0.0.0", port=2999, reload=False)
