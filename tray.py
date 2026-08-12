"""DoukHub 托盘启动程序"""
import logging
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


def make_icon() -> Image.Image:
    """生成一个简单托盘图标(不依赖外部素材文件)"""
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([4, 4, 60, 60], radius=14, fill=(45, 212, 191, 255))
    d.text((16, 12), "D", fill=(10, 11, 15, 255))
    return img


SERVER_PROC: subprocess.Popen | None = None


def start_server() -> bool:
    """启动 uvicorn 服务子进程(reload=True, 隐藏窗口)。返回是否启动成功。"""
    global SERVER_PROC
    if SERVER_PROC and SERVER_PROC.poll() is None:
        return True  # 已在运行
    root = Path(__file__).resolve().parent
    cmd = [
        sys.executable, "-m", "uvicorn", "app.main:app",
        "--host", "0.0.0.0", "--port", str(PORT), "--reload",
    ]
    try:
        SERVER_PROC = subprocess.Popen(
            cmd,
            cwd=str(root),
            creationflags=subprocess.CREATE_NO_WINDOW,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except Exception as e:
        logger.error(f"启动服务失败: {e}")
        return False


def stop_server() -> None:
    """停止 uvicorn 子进程"""
    global SERVER_PROC
    if SERVER_PROC and SERVER_PROC.poll() is None:
        SERVER_PROC.terminate()
        try:
            SERVER_PROC.wait(timeout=10)
        except subprocess.TimeoutExpired:
            SERVER_PROC.kill()
    SERVER_PROC = None


def wait_ready(timeout: float = 20.0) -> bool:
    """等待端口就绪"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            httpx.get(f"http://127.0.0.1:{PORT}/", timeout=1)
            return True
        except Exception:
            time.sleep(0.5)
    return False


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
    """显式拉起下载器并持有进程句柄,使托盘能真正停/启下载器。

    uvicorn 的 lifespan 也会在后台调用 start_all,但端口已占用时
    DownloaderService.start() 幂等跳过,不冲突。
    """
    try:
        svc = get_service_manager()
        for r in svc.start_all():
            logger.info(r.get("message", str(r)))
    except Exception as e:
        logger.warning(f"启动下载器异常: {e}")


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
    _reopen_ui_after_ready()


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
    _reopen_ui_after_ready()


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


def main():
    menu = pystray.Menu(
        pystray.MenuItem("打开界面", open_ui, default=True),
        pystray.MenuItem("重启服务", pystray.Menu(
            pystray.MenuItem("只重启 DoukHub", restart_doukhub_only),
            pystray.MenuItem("重启全部(含下载器)", restart_all),
        )),
        pystray.MenuItem("退出", quit_app),
    )
    icon = pystray.Icon("doukhub", make_icon(), "DoukHub", menu)

    # 启动服务并在就绪后打开浏览器
    if start_server():
        _reopen_ui_after_ready()

    # 托盘显式拉起下载器并持有进程句柄(uvicorn lifespan 的 start_all 检测到端口已占用会跳过)
    threading.Thread(target=start_downloaders, daemon=True).start()

    icon.run()


if __name__ == "__main__":
    main()