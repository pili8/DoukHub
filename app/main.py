"""DoukHub 主入口 — FastAPI Web 应用"""
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .core.config import Config
from .core.feishu import FeishuClient
from .core.collector import Collector, Account
from .core.cookie_pool import CookiePool
from .core.syncer import Syncer
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
async def page_dashboard(request: Request):
    svc = get_services()
    h = get_history()
    sched = get_scheduler()
    return templates.TemplateResponse(request, "dashboard.html", context={
        "request": request,
        "services": svc.status_all(),
        "stats": h.get_stats(),
        "jobs": sched.get_jobs_info(),
        "page": "dashboard",
    })


@app.get("/accounts", response_class=HTMLResponse)
async def page_accounts(request: Request):
    s = get_syncer()
    accounts = s.load_local_accounts() if s else []
    return templates.TemplateResponse(request, "accounts.html", context={
        "request": request,
        "accounts": accounts,
        "page": "accounts",
    })


@app.get("/collect", response_class=HTMLResponse)
async def page_collect(request: Request):
    s = get_syncer()
    accounts = s.load_local_accounts() if s else []
    return templates.TemplateResponse(request, "collect.html", context={
        "request": request,
        "accounts": accounts,
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

@app.get("/api/status")
async def api_status():
    svc = get_services()
    h = get_history()
    sched = get_scheduler()
    return {
        "services": svc.status_all(),
        "stats": h.get_stats(),
        "jobs": sched.get_jobs_info(),
    }


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


# --- 飞书同步 ---

@app.post("/api/sync")
async def api_sync():
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
    result = await s.sync()
    return {
        "success": result.success,
        "message": result.message,
        "total": result.total,
        "new_count": len(result.new_accounts),
        "duplicate_count": len(result.duplicates),
        "error_count": len(result.errors),
        "errors": result.errors,
        "duplicates": [
            {
                "new_name": dup[0].name,
                "new_link": dup[0].link,
                "new_sec_user_id": dup[0].sec_user_id,
                "new_rating": dup[0].rating,
                "existing_name": dup[1].name,
                "existing_link": dup[1].link,
                "existing_sec_user_id": dup[1].sec_user_id,
                "existing_rating": dup[1].rating,
                "record_id": dup[0].record_id,
            }
            for dup in result.duplicates
        ],
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


# --- 采集 ---

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


@app.post("/api/ensure-fields")
async def api_ensure_fields():
    """检测并创建缺失的飞书表格字段"""
    f = get_feishu()
    if not f:
        return JSONResponse({"success": False, "message": "飞书未配置"}, status_code=400)
    try:
        result = f.ensure_fields(
            config.feishu["app_token"],
            config.feishu["account_table_id"],
        )
        return result
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
