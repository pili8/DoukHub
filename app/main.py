"""DoukHub 主入口 — FastAPI Web 应用"""
import logging
from contextlib import asynccontextmanager
from pathlib import Path

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


def get_syncer_v2() -> SyncerV2 | None:
    global syncer_v2
    f = get_feishu()
    if f:
        if syncer_v2 is None:
            syncer_v2 = SyncerV2(
                feishu=f,
                collector=get_collector(),
                config=config.feishu,
            )
        return syncer_v2
    return None


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

    yield

    # 关闭
    sched.shutdown()
    svc.close()
    c = get_collector()
    if c:
        await c.close()


app = FastAPI(title="DoukHub", version="1.0.0", lifespan=lifespan)
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
@app.get("/sync", response_class=HTMLResponse)
async def page_sync(request: Request):
    """同步页面 - 整合仪表盘+账号管理"""
    svc = get_services()
    s = get_syncer()
    accounts = s.load_local_accounts() if s else []
    return templates.TemplateResponse(request, "sync.html", context={
        "request": request,
        "services": svc.status_all(),
        "accounts": accounts,
        "page": "sync",
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
    """数据库管理页面"""
    return templates.TemplateResponse(request, "database.html", context={
        "request": request,
        "page": "database",
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


@app.get("/history", response_class=HTMLResponse)
async def page_history(request: Request):
    h = get_history()
    records = h.get_records(limit=200)
    return templates.TemplateResponse(request, "history.html", context={
        "request": request,
        "records": records,
        "page": "history",
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
    feishu_status = {"connected": False, "message": "未配置"}
    if feishu:
        try:
            result = feishu.test_connection()
            feishu_status = {
                "connected": result.get("success", False),
                "message": result.get("message", ""),
            }
        except Exception as e:
            feishu_status = {"connected": False, "message": str(e)}

    # 检测 TTD 连通性（先检查内核是否安装）
    ttd_kernel = svc.ttd.source_exists
    if not ttd_kernel:
        ttd_status = {"connected": False, "message": "内核未安装"}
    else:
        ttd_status = {"connected": False, "message": "未启动"}
        try:
            import httpx
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(f"{collector.ttd_url}/")
                if resp.status_code in (200, 307, 404):
                    ttd_status = {"connected": True, "message": "运行中"}
        except Exception as e:
            ttd_status = {"connected": False, "message": str(e)}

    # 检测 XHS 连通性（先检查内核是否安装）
    xhs_kernel = svc.xhs.source_exists
    if not xhs_kernel:
        xhs_status = {"connected": False, "message": "内核未安装"}
    else:
        xhs_status = {"connected": False, "message": "未启动"}
        try:
            import httpx
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(f"{collector.xhs_url}/")
                if resp.status_code in (200, 307, 404):
                    xhs_status = {"connected": True, "message": "运行中"}
        except Exception as e:
            xhs_status = {"connected": False, "message": str(e)}

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
    """测试飞书 API 连通性"""
    feishu = get_feishu()
    if not feishu:
        return {"success": False, "message": "飞书未配置"}
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
                record_id = ck.get("记录ID", "")
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
            {"success": False, "message": "飞书未配置，请先在设置中填写飞书信息"},
            status_code=400,
        )
    # 同步前自动检测并创建缺失字段
    f = get_feishu()
    if f:
        try:
            f.ensure_fields(
                config.feishu["app_token"],
                config.feishu["account_table_id"],
            )
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
                    from .core.link_resolver import detect_platform, extract_sec_user_id
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
                    s._update_collection_status(record_id, "已同步", "", sec_user_id)
                    
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
        return {
            "success": result.failed == 0,
            "message": f"导入完成: 成功 {result.success} 条，失败 {result.failed} 条，跳过 {result.skipped} 条",
            **result.to_dict()
        }
    except Exception as e:
        logger.error(f"导入失败: {e}")
        return JSONResponse(
            {"success": False, "message": f"导入异常: {str(e)}"},
            status_code=500,
        )


@app.post("/api/sync/v2/update-collection")
async def api_sync_v2_update_collection():
    """步骤2：更新采集表（获取 sec_user_id）- SSE 实时进度"""
    s = get_syncer_v2()
    if not s:
        return JSONResponse(
            {"success": False, "message": "飞书未配置"},
            status_code=400,
        )
    
    import json
    
    async def update_stream():
        try:
            yield f"data: {json.dumps({'type': 'start', 'message': '开始更新采集表'})}\n\n"
            
            # 获取所有未获取 sec_user_id 的记录
            collections = s.db.get_all_collections()
            to_process = [c for c in collections if not c.get("账号标识") and str(c.get("分享码", "")).strip()]
            
            yield f"data: {json.dumps({'type': 'stats', 'total': len(to_process), 'success': 0, 'failed': 0})}\n\n"
            yield f"data: {json.dumps({'type': 'progress', 'message': f'需要处理 {len(to_process)} 条记录'})}\n\n"

            # 加载 Cookie
            db = get_database()
            cookies = db.get_enabled_cookies()
            cookie_list = [ck.get("Cookie", "") for ck in cookies if ck.get("Cookie")]
            if not cookie_list:
                yield f"data: {json.dumps({'type': 'log', 'level': 'error', 'message': '⚠️ Cookie 表为空，无法获取账号详情。请在飞书 Cookie 表中添加有效的抖音 Cookie。'})}\n\n"
                yield f"data: {json.dumps({'type': 'complete', 'success': False, 'message': 'Cookie 表为空，获取详情失败', 'total': len(to_process), 'success_count': 0, 'failed': len(to_process)})}\n\n"
                return
            yield f"data: {json.dumps({'type': 'log', 'level': 'info', 'message': f'已加载 {len(cookie_list)} 个 Cookie'})}\n\n"

            success = 0
            failed = 0
            errors = []
            
            for i, collection in enumerate(to_process):
                yield f"data: {json.dumps({'type': 'progress', 'message': f'处理 [{i+1}/{len(to_process)}]: {collection["分享码"]}'})}\n\n"
                
                try:
                    share = collection["分享码"]
                    platform = collection.get("平台") or "抖音"

                    # 调用 TTD API 解析短链接
                    resolved_url = await s.collector.resolve_short_url(share, platform)
                    sec_user_id = s._extract_sec_user_id(resolved_url, platform)
                    
                    if not sec_user_id:
                        failed += 1
                        errors.append(f"{share}: 无法提取 sec_user_id")
                        s.db.update_collection(collection["记录ID"], {
                            "同步错误": "无法提取 sec_user_id",
                        })
                        yield f"data: {json.dumps({'type': 'log', 'level': 'error', 'message': f'❌ {share}: 无法提取 sec_user_id'})}\n\n"
                        continue
                    
                    # 检查是否已存在
                    existing = s.db.get_collection_by_sec_user_id(sec_user_id)
                    if existing and existing["记录ID"] != collection["记录ID"]:
                        # 去重：等级取高的，标签合并
                        new_level = s.merge_level(existing.get("等级"), collection.get("等级"))
                        existing_tags = json.loads(existing.get("标签", "[]")) if existing.get("标签") else []
                        new_tags = json.loads(collection.get("标签", "[]")) if collection.get("标签") else []
                        merged_tags = s.merge_tags(existing_tags, new_tags)
                        
                        s.db.update_collection(existing["记录ID"], {
                            "等级": new_level,
                            "标签": json.dumps(merged_tags),
                        })
                        # 删除重复记录
                        s.db.delete_collection(collection["记录ID"])
                        success += 1
                        yield f"data: {json.dumps({'type': 'log', 'level': 'ok', 'message': f'✅ 合并重复记录'})}\n\n"
                    else:
                        # 更新 sec_user_id
                        s.db.update_collection(collection["记录ID"], {
                            "账号标识": sec_user_id,
                            "已同步": True,
                            "同步错误": None,
                        })
                        success += 1
                        yield f"data: {json.dumps({'type': 'log', 'level': 'ok', 'message': f'✅ {share}: {sec_user_id}'})}\n\n"
                    
                    # 更新统计
                    yield f"data: {json.dumps({'type': 'stats', 'total': len(to_process), 'success': success, 'failed': failed})}\n\n"
                    
                except Exception as e:
                    failed += 1
                    errors.append(f"{collection.get('分享码')}: {str(e)}")
                    s.db.update_collection(collection["记录ID"], {
                        "同步错误": str(e),
                    })
                    yield f"data: {json.dumps({'type': 'log', 'level': 'error', 'message': f'❌ {collection.get('分享码')}: {e}'})}\n\n"
            
            yield f"data: {json.dumps({'type': 'complete', 'success': failed == 0, 'message': f'更新完成: 成功 {success} 条，失败 {failed} 条', 'total': len(to_process), 'success_count': success, 'failed': failed, 'errors': errors[:5]})}\n\n"
            
        except Exception as e:
            logger.error(f"更新采集表失败: {e}")
            yield f"data: {json.dumps({'type': 'complete', 'success': False, 'message': f'更新失败: {str(e)}', 'total': 0, 'success_count': 0, 'failed': 1, 'errors': [str(e)]})}\n\n"
    
    return StreamingResponse(update_stream(), media_type="text/event-stream")


@app.post("/api/sync/v2/sync-account")
async def api_sync_v2_sync_account():
    """步骤3：同步账号表 - SSE 实时进度"""
    s = get_syncer_v2()
    if not s:
        return JSONResponse(
            {"success": False, "message": "飞书未配置"},
            status_code=400,
        )
    
    import json
    
    async def sync_stream():
        try:
            yield f"data: {json.dumps({'type': 'start', 'message': '开始同步账号表'})}\n\n"
            
            # 获取所有已同步但账号表未更新的记录
            collections = s.db.get_all_collections()
            to_process = [c for c in collections if c.get("已同步") and c.get("账号标识")]
            
            yield f"data: {json.dumps({'type': 'stats', 'total': len(to_process), 'success': 0, 'failed': 0})}\n\n"
            yield f"data: {json.dumps({'type': 'progress', 'message': f'需要处理 {len(to_process)} 条记录'})}\n\n"

            # 加载 Cookie
            db = get_database()
            cookies = db.get_enabled_cookies()
            cookie_list = [ck.get("Cookie", "") for ck in cookies if ck.get("Cookie")]
            if not cookie_list:
                yield f"data: {json.dumps({'type': 'log', 'level': 'error', 'message': 'Cookie 表为空，无法获取账号详情。请在飞书 Cookie 表中添加有效的抖音 Cookie。'})}\n\n"
                yield f"data: {json.dumps({'type': 'complete', 'success': False, 'message': 'Cookie 表为空，获取详情失败', 'total': len(to_process), 'success_count': 0, 'failed': len(to_process)})}\n\n"
                return
            yield f"data: {json.dumps({'type': 'log', 'level': 'info', 'message': f'已加载 {len(cookie_list)} 个 Cookie'})}\n\n"

            success = 0
            failed = 0
            errors = []
            
            for i, collection in enumerate(to_process):
                yield f"data: {json.dumps({'type': 'progress', 'message': f'处理 [{i+1}/{len(to_process)}]: {collection["账号标识"]}'})}\n\n"
                
                try:
                    sec_user_id = collection["账号标识"]
                    platform = collection.get("平台") or "抖音"
                    
                    # 检查账号表是否已存在
                    existing_account = s.db.get_account_by_sec_user_id(sec_user_id)
                    
                    if existing_account:
                        # 去重：等级取高的，标签合并
                        new_level = s.merge_level(existing_account.get("等级"), collection.get("等级"))
                        existing_tags = json.loads(existing_account.get("标签", "[]")) if existing_account.get("标签") else []
                        new_tags = json.loads(collection.get("标签", "[]")) if collection.get("标签") else []
                        merged_tags = s.merge_tags(existing_tags, new_tags)
                        
                        # 更新账号表
                        s.db.update_account(existing_account["记录ID"], {
                            "等级": new_level,
                            "标签": json.dumps(merged_tags),
                            "已更新": True,
                        })
                        success += 1
                        yield f"data: {json.dumps({'type': 'log', 'level': 'ok', 'message': f'✅ 更新账号: {existing_account.get('账号名称')}'})}\n\n"
                    else:
                        # 调用 API 获取账号信息
                        cookie = cookie_list[success % len(cookie_list)]
                        info = await s.collector.get_account_info(sec_user_id, platform, cookie)
                        if not info:
                            failed += 1
                            errors.append(f"{sec_user_id}: 无法获取账号信息")
                            yield f"data: {json.dumps({'type': 'log', 'level': 'error', 'message': f'❌ {sec_user_id}: 无法获取账号信息'})}\n\n"
                            continue
                        
                        # 创建账号记录
                        record_id = f"acc_{datetime.now().strftime('%Y%m%d%H%M%S')}_{success}"
                        s.db.insert_account({
                            "记录ID": record_id,
                            "账号名称": info.get("nickname", ""),
                            "平台": platform,
                            "链接": f"https://www.douyin.com/user/{sec_user_id}",
                            "账号标识": sec_user_id,
                            "等级": collection.get("等级"),
                            "标签": collection.get("标签"),
                            "昵称": info.get("nickname", ""),
                            "粉丝数": info.get("follower_count", 0),
                            "作品数": info.get("aweme_count", 0),
                            "签名": info.get("signature", ""),
                            "头像": info.get("avatar", ""),
                            "已更新": True,
                        })
                        success += 1
                        yield f"data: {json.dumps({'type': 'log', 'level': 'ok', 'message': f'✅ 新增账号: {info.get('nickname')}'})}\n\n"
                    
                    # 更新统计
                    yield f"data: {json.dumps({'type': 'stats', 'total': len(to_process), 'success': success, 'failed': failed})}\n\n"
                    
                except Exception as e:
                    failed += 1
                    errors.append(f"{collection.get('账号标识')}: {str(e)}")
                    yield f"data: {json.dumps({'type': 'log', 'level': 'error', 'message': f'❌ {collection.get('账号标识')}: {e}'})}\n\n"

            # 自动回写飞书账号表
            if s.feishu_syncer and s.feishu_syncer.account_table_id:
                yield f"data: {json.dumps({'type': 'progress', 'message': '正在回写飞书账号表...'})}\n\n"
                try:
                    fs_result = s.feishu_syncer.sync_account_to_feishu()
                    fs_created = fs_result.get("created", 0)
                    fs_updated = fs_result.get("updated", 0)
                    yield f"data: {json.dumps({'type': 'log', 'level': 'ok', 'message': f'✅ 飞书回写完成: 新增 {fs_created}, 更新 {fs_updated}'})}\n\n"
                except Exception as e:
                    yield f"data: {json.dumps({'type': 'log', 'level': 'error', 'message': f'⚠️ 飞书回写失败: {e}'})}\n\n"

            yield f"data: {json.dumps({'type': 'complete', 'success': failed == 0, 'message': f'同步完成: 成功 {success} 条，失败 {failed} 条', 'total': len(to_process), 'success_count': success, 'failed': failed, 'errors': errors[:5]})}\n\n"
            
        except Exception as e:
            logger.error(f"同步账号表失败: {e}")
            yield f"data: {json.dumps({'type': 'complete', 'success': False, 'message': f'同步失败: {str(e)}', 'total': 0, 'success_count': 0, 'failed': 1, 'errors': [str(e)]})}\n\n"
    
    return StreamingResponse(sync_stream(), media_type="text/event-stream")


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
async def api_database_table(table_name: str, limit: int = 100, offset: int = 0):
    """获取表数据"""
    db = get_database()
    
    # 验证表名
    valid_tables = ["collection_cache", "account_cache", "cookie_cache", "collection_history", "scheduled_tasks"]
    if table_name not in valid_tables:
        return JSONResponse({"success": False, "message": "无效的表名"}, status_code=400)
    
    # 获取数据
    with db._connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM {table_name} ORDER BY rowid DESC LIMIT ? OFFSET ?",
            (limit, offset)
        ).fetchall()
        return [dict(row) for row in rows]


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
            with db._connect() as conn:
                conn.execute("DELETE FROM cookie_cache WHERE 记录ID = ?", (record_id,))
                conn.commit()
                success = True
        
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


# --- 飞书同步 ---

@app.post("/api/feishu/sync")
async def api_feishu_sync():
    """飞书双向同步 - SSE 实时进度"""
    fs = get_feishu_syncer()
    if not fs:
        return JSONResponse(
            {"success": False, "message": "飞书未配置"},
            status_code=400,
        )

    import json
    import asyncio

    async def sync_stream():
        try:
            yield f"data: {json.dumps({'type': 'start', 'message': '开始双向同步'})}\n\n"

            steps = [
                ('采集表 → 飞书', fs.sync_collection_to_feishu),
                ('账号表 → 飞书', fs.sync_account_to_feishu),
                ('飞书 → 采集表', fs.sync_collection_from_feishu),
                ('飞书 → 账号表', fs.sync_account_from_feishu),
            ]

            total = len(steps)
            all_errors = []
            results = {}

            for i, (label, fn) in enumerate(steps):
                yield f"data: {json.dumps({'type': 'progress', 'message': f'[{i+1}/{total}] {label}...'})}\n\n"
                try:
                    r = await asyncio.to_thread(fn)
                    created = r.get("created", 0)
                    updated = r.get("updated", 0)
                    failed = r.get("failed", 0)
                    all_errors.extend(r.get("errors", []))
                    results[label] = {"created": created, "updated": updated, "failed": failed}
                    level = "ok" if failed == 0 else "error"
                    icon = "✅" if failed == 0 else "⚠️"
                    msg = f"{icon} {label}: 新增 {created}, 更新 {updated}" + (f", 失败 {failed}" if failed else "")
                    yield f"data: {json.dumps({'type': 'log', 'level': level, 'message': msg})}\n\n"
                    yield f"data: {json.dumps({'type': 'stats', 'step': i+1, 'total': total})}\n\n"
                except Exception as e:
                    all_errors.append(f"{label}: {e}")
                    yield f"data: {json.dumps({'type': 'log', 'level': 'error', 'message': f'❌ {label}: {e}'})}\n\n"

            summary = "双向同步完成" + (f", {len(all_errors)} 个错误" if all_errors else "")
            yield f"data: {json.dumps({'type': 'complete', 'success': len(all_errors) == 0, 'message': summary, 'results': results, 'errors': all_errors[:5]})}\n\n"

        except Exception as e:
            logger.error(f"飞书同步失败: {e}")
            yield f"data: {json.dumps({'type': 'complete', 'success': False, 'message': f'同步失败: {str(e)}'})}\n\n"

    return StreamingResponse(sync_stream(), media_type="text/event-stream")


@app.post("/api/feishu/sync/to-feishu")
async def api_feishu_sync_to():
    """本地 → 飞书"""
    fs = get_feishu_syncer()
    if not fs:
        return JSONResponse({"success": False, "message": "飞书未配置"}, status_code=400)

    try:
        coll_result = fs.sync_collection_to_feishu()
        acc_result = fs.sync_account_to_feishu()
        cookie_result = fs.sync_cookie_to_feishu()
        return {
            "success": True,
            "message": f"同步到飞书完成: 采集表 {coll_result['created']}新增 {coll_result['updated']}更新, 账号表 {acc_result['created']}新增 {acc_result['updated']}更新, Cookie表 {cookie_result['created']}新增 {cookie_result['updated']}更新",
            "collection": coll_result,
            "account": acc_result,
            "cookie": cookie_result,
        }
    except Exception as e:
        return JSONResponse({"success": False, "message": str(e)}, status_code=500)


@app.post("/api/feishu/sync/from-feishu")
async def api_feishu_sync_from():
    """飞书 → 本地"""
    fs = get_feishu_syncer()
    if not fs:
        return JSONResponse({"success": False, "message": "飞书未配置"}, status_code=400)

    try:
        coll_result = fs.sync_collection_from_feishu()
        acc_result = fs.sync_account_from_feishu()
        cookie_result = fs.sync_cookie_from_feishu()
        return {
            "success": True,
            "message": f"从飞书同步完成: 采集表 {coll_result['created']}新增 {coll_result['updated']}更新, 账号表 {acc_result['created']}新增 {acc_result['updated']}更新, Cookie表 {cookie_result['created']}新增 {cookie_result['updated']}更新",
            "collection": coll_result,
            "account": acc_result,
            "cookie": cookie_result,
        }
    except Exception as e:
        return JSONResponse({"success": False, "message": str(e)}, status_code=500)


# --- 飞书同步（全盘/增量）---

def _run_feishu_sync_sse(fs, steps):
    """通用飞书同步 SSE 生成器"""
    import json, asyncio

    async def stream():
        try:
            total = len(steps)
            all_errors = []
            results = {}
            yield f"data: {json.dumps({'type': 'start', 'message': '开始同步', 'total': total})}\n\n"

            for i, (label, fn) in enumerate(steps):
                yield f"data: {json.dumps({'type': 'progress', 'message': f'[{i+1}/{total}] {label}...'})}\n\n"
                try:
                    r = await asyncio.to_thread(fn)
                    created = r.get("created", 0)
                    updated = r.get("updated", 0)
                    skipped = r.get("skipped", 0)
                    failed = r.get("failed", 0)
                    all_errors.extend(r.get("errors", []))
                    results[label] = {"created": created, "updated": updated, "skipped": skipped, "failed": failed}
                    if failed > 0:
                        msg = f"⚠️ {label}: 新增 {created}, 更新 {updated}, 跳过 {skipped}, 失败 {failed}"
                        yield f"data: {json.dumps({'type': 'log', 'level': 'error', 'message': msg})}\n\n"
                    else:
                        parts = []
                        if created: parts.append(f"新增 {created}")
                        if updated: parts.append(f"更新 {updated}")
                        if skipped: parts.append(f"跳过 {skipped}")
                        msg = f"✅ {label}: " + (", ".join(parts) if parts else "无变化")
                        yield f"data: {json.dumps({'type': 'log', 'level': 'ok', 'message': msg})}\n\n"
                    yield f"data: {json.dumps({'type': 'stats', 'step': i+1, 'total': total})}\n\n"
                except Exception as e:
                    all_errors.append(f"{label}: {e}")
                    yield f"data: {json.dumps({'type': 'log', 'level': 'error', 'message': f'❌ {label}: {e}'})}\n\n"

            summary = "同步完成" + (f", {len(all_errors)} 个错误" if all_errors else "")
            yield f"data: {json.dumps({'type': 'complete', 'success': len(all_errors) == 0, 'message': summary, 'results': results, 'errors': all_errors[:5]})}\n\n"
        except Exception as e:
            logger.error(f"飞书同步失败: {e}")
            yield f"data: {json.dumps({'type': 'complete', 'success': False, 'message': f'同步失败: {str(e)}'})}\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")


@app.post("/api/feishu/sync/full/to-feishu")
async def api_feishu_full_to_feishu():
    """全盘同步：本地 → 飞书（覆盖）"""
    fs = get_feishu_syncer()
    if not fs:
        return JSONResponse({"success": False, "message": "飞书未配置"}, status_code=400)
    return _run_feishu_sync_sse(fs, [
        ("采集表 → 飞书", fs.sync_collection_to_feishu),
        ("账号表 → 飞书", fs.sync_account_to_feishu),
        ("Cookie表 → 飞书", fs.sync_cookie_to_feishu),
    ])


@app.post("/api/feishu/sync/full/from-feishu")
async def api_feishu_full_from_feishu():
    """全盘同步：飞书 → 本地（覆盖）"""
    fs = get_feishu_syncer()
    if not fs:
        return JSONResponse({"success": False, "message": "飞书未配置"}, status_code=400)
    return _run_feishu_sync_sse(fs, [
        ("飞书 → 采集表", fs.sync_collection_from_feishu),
        ("飞书 → 账号表", fs.sync_account_from_feishu),
        ("飞书 → Cookie表", fs.sync_cookie_from_feishu),
    ])


@app.post("/api/feishu/sync/incremental")
async def api_feishu_incremental_sync():
    """日常增量同步：双向仅新增，不更新已有记录"""
    fs = get_feishu_syncer()
    if not fs:
        return JSONResponse({"success": False, "message": "飞书未配置"}, status_code=400)
    return _run_feishu_sync_sse(fs, [
        ("本地 → 飞书：采集表", fs._incremental_collection_to_feishu),
        ("本地 → 飞书：账号表", fs._incremental_account_to_feishu),
        ("本地 → 飞书：Cookie表", fs._incremental_cookie_to_feishu),
        ("飞书 → 本地：采集表", fs._incremental_collection_from_feishu),
        ("飞书 → 本地：账号表", fs._incremental_account_from_feishu),
        ("飞书 → 本地：Cookie表", fs._incremental_cookie_from_feishu),
    ])


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
        accounts = [a for a in accounts if a.get("等级", 0) >= rating_min and a.get("账号标识")]

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
                account_name = account.get("账号名称") or account.get("昵称") or account.get("账号标识", "")
                sec_user_id = account.get("账号标识", "")
                platform = account.get("平台", "抖音")

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
                            collection_type="发布",
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
                            "账号标识": sec_user_id,
                            "采集类型": "发布",
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
                            "账号标识": sec_user_id,
                            "采集类型": "发布",
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
    """检测并创建缺失的飞书表格字段，支持指定表类型"""
    f = get_feishu()
    if not f:
        return JSONResponse({"success": False, "message": "飞书未配置"}, status_code=400)

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
        return {"success": False, "message": "飞书未配置，请先填写 App ID 和 App Secret"}
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
