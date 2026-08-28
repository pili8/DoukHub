"""DoukHub 托盘启动程序"""
import ctypes
import logging
import os
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

import httpx
import pystray
from PIL import Image, ImageDraw

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("doukhub.tray")

PORT = 2999
URL = f"http://127.0.0.1:{PORT}"

ROOT = Path(__file__).resolve().parent
APP_DIR = ROOT / "app"

# 锁定 cwd 到项目根目录,避免 pythonw/pystray spawn 子进程时 cwd 漂移导致
# 配置里的相对路径(./TikTokDownloader, ./data 等)解析到错误位置。
os.chdir(ROOT)


def make_icon() -> Image.Image:
    """生成一个简单托盘图标(不依赖外部素材文件)"""
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([4, 4, 60, 60], radius=14, fill=(45, 212, 191, 255))
    d.text((16, 12), "D", fill=(10, 11, 15, 255))
    return img


SERVER_PROC: subprocess.Popen | None = None


def free_port() -> None:
    """清理占用 PORT 端口的残留进程(上次异常退出可能遗留孤儿进程)。"""
    try:
        out = subprocess.run(
            ["netstat", "-aon"], capture_output=True, text=True,
            timeout=10, creationflags=subprocess.CREATE_NO_WINDOW,
        ).stdout
    except Exception:
        return
    for line in out.splitlines():
        if f":{PORT}" in line and "LISTENING" in line:
            pid = line.split()[-1]
            if pid.isdigit() and pid != "0":
                subprocess.run(["taskkill", "/F", "/T", "/PID", pid],
                               capture_output=True, timeout=10,
                               creationflags=subprocess.CREATE_NO_WINDOW)


def start_server() -> bool:
    """启动 uvicorn 服务子进程(隐藏窗口,热重载由托盘文件监控实现)。返回是否启动成功。"""
    global SERVER_PROC
    if SERVER_PROC and SERVER_PROC.poll() is None:
        return True  # 已在运行
    free_port()
    cmd = [
        sys.executable, "-m", "uvicorn", "app.main:app",
        "--host", "0.0.0.0", "--port", str(PORT),
    ]
    try:
        SERVER_PROC = subprocess.Popen(
            cmd,
            cwd=str(ROOT),
            creationflags=subprocess.CREATE_NO_WINDOW,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except Exception as e:
        logger.error(f"启动服务失败: {e}")
        return False


def stop_server() -> None:
    """停止 uvicorn 子进程:taskkill 杀进程树,失败再 terminate/kill 兜底"""
    global SERVER_PROC
    if not (SERVER_PROC and SERVER_PROC.poll() is None):
        SERVER_PROC = None
        return
    try:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(SERVER_PROC.pid)],
                       capture_output=True, timeout=10)
    except Exception:
        pass
    if SERVER_PROC.poll() is None:  # taskkill 失败兜底
        SERVER_PROC.terminate()
        try:
            SERVER_PROC.wait(timeout=10)
        except subprocess.TimeoutExpired:
            SERVER_PROC.kill()
    SERVER_PROC = None


def wait_ready(timeout: float = 20.0) -> bool:
    """等待端口就绪(HTTP 5xx 视为未就绪)"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            resp = httpx.get(f"http://127.0.0.1:{PORT}/", timeout=1)
            if resp.status_code < 500:
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def _watch_files() -> None:
    """后台线程:轮询 app/ 目录 .py 文件 mtime,服务运行中检测到变化则自动重启。

    uvicorn --reload 依赖 Windows 控制台信号,与隐藏窗口互斥,故由托盘自实现。
    stop/start 不修改文件 mtime,且每次检测后重建快照,不会误触发。
    """
    files = {p: p.stat().st_mtime for p in APP_DIR.rglob("*.py")}
    while True:
        time.sleep(1)
        if SERVER_PROC is None or SERVER_PROC.poll() is not None:
            continue  # 服务未在运行,跳过
        try:
            snapshot = {p: p.stat().st_mtime for p in APP_DIR.rglob("*.py")}
        except OSError:
            continue  # 遍历期间文件被删,下轮再试
        if snapshot == files:
            continue
        files = snapshot
        logger.info("检测到 app/ 代码变化,自动重启服务")
        stop_server()
        start_server()


# --- 下载器管理(复用现有 ServiceManager) ---
_svc = None


def get_service_manager():
    global _svc
    if _svc is None:
        from app.core.config import Config
        from app.services.downloader import ServiceManager
        cfg = Config()
        _svc = ServiceManager(
            ttd_path=cfg.ttd_path,
            ttd_port=cfg.ttd_port,
            xhs_path=cfg.xhs_path,
            xhs_port=cfg.xhs_port,
        )
    return _svc


def start_downloaders():
    """在后台线程拉起下载器并持有进程句柄,使托盘能真正停/启下载器。

    必须在后台执行:downloader.start() 启动后最多轮询等待 30 秒,
    若同步调用会阻塞 DoukHub 服务启动。uvicorn 的 lifespan 也会在后台
    调用 start_all,但端口已占用时 DownloaderService.start() 幂等跳过,不冲突。
    """
    try:
        svc = get_service_manager()
        for r in svc.start_all():
            logger.info(r.get("message", str(r)))
    except Exception as e:
        logger.warning(f"启动下载器异常: {e}")


def start_downloaders_async() -> None:
    """后台线程执行 start_downloaders(),避免阻塞服务启动。"""
    threading.Thread(target=start_downloaders, daemon=True).start()


# --- 菜单动作 ---
def open_ui(icon, item):
    webbrowser.open(URL)


def _reopen_ui_after_ready():
    """后台等待端口就绪后重开浏览器,不阻塞菜单"""
    threading.Thread(
        target=lambda: (wait_ready() and webbrowser.open(URL)),
        daemon=True,
    ).start()


def restart_doukhub_only(icon, item):
    logger.info("只重启 DoukHub")
    stop_server()
    start_server()


def restart_all(icon, item):
    logger.info("重启全部(含下载器)")
    svc = get_service_manager()
    try:
        svc.stop_all()
    except Exception as e:
        logger.warning(f"停止下载器异常: {e}")
    stop_server()
    start_server()
    try:
        svc.start_all()
    except Exception as e:
        logger.warning(f"启动下载器异常: {e}")


def quit_app(icon, item):
    logger.info("退出")
    stop_server()
    try:
        svc = get_service_manager()
        svc.stop_all()
        svc.close()
    except Exception as e:
        logger.warning(f"停止下载器异常: {e}")
    icon.stop()


_SINGLE_INSTANCE_MUTEX = "Local\\DoukHub_Tray_Singleton"
_MUTEX_HANDLE = None  # 持有互斥量句柄直到进程退出,防止重复实例


def acquire_single_instance() -> bool:
    """单实例锁:成功返回 True,已有实例在运行则返回 False。

    底层原理:Windows 命名互斥量是一把全系统(当前会话)唯一的锁。
    第一个进程创建并持有它;第二个进程创建同名互斥量时系统返回
    ERROR_ALREADY_EXISTS(183),据此判定"已有实例在运行"。
    进程退出时互斥量由系统自动释放,不会残留误判。
    """
    global _MUTEX_HANDLE
    kernel32 = ctypes.windll.kernel32
    _MUTEX_HANDLE = kernel32.CreateMutexW(None, False, _SINGLE_INSTANCE_MUTEX)
    if not _MUTEX_HANDLE:
        # 创建失败(极端情况),不阻塞启动
        return True
    return kernel32.GetLastError() != 183  # ERROR_ALREADY_EXISTS


def _notify_already_running() -> None:
    """已有实例在运行时:弹窗提示 + 写入日志。"""
    logger.info("检测到 DoukHub 已在运行,拒绝重复启动")
    ctypes.windll.user32.MessageBoxW(
        None,
        "DoukHub 已在运行中，请勿重复启动。",
        "DoukHub",
        0x40 | 0x40000,  # MB_ICONINFORMATION | MB_TOPMOST
    )


def main():
    if not acquire_single_instance():
        _notify_already_running()
        return
    menu = pystray.Menu(
        pystray.MenuItem("打开界面", open_ui, default=True),
        pystray.MenuItem("重启服务", pystray.Menu(
            pystray.MenuItem("只重启 DoukHub", restart_doukhub_only),
            pystray.MenuItem("重启全部(含下载器)", restart_all),
        )),
        pystray.MenuItem("退出", quit_app),
    )
    icon = pystray.Icon("doukhub", make_icon(), "DoukHub", menu)

    # 后台异步拉起下载器(避免阻塞服务启动),再启动服务
    # (uvicorn lifespan 检测到端口占用会幂等跳过)
    start_downloaders_async()
    start_server()

    # 自实现热重载:监控 app/ 目录文件变化(uvicorn --reload 与隐藏窗口互斥)
    threading.Thread(target=_watch_files, daemon=True).start()

    icon.run()


if __name__ == "__main__":
    main()