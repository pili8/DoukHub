"""DoukHub 托盘启动程序"""
import logging
import sys
import threading
import webbrowser
from pathlib import Path

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


# --- 菜单动作(占位,Task 2/3 实现真实逻辑) ---
def open_ui(icon, item):
    print("open_ui")


def restart_doukhub_only(icon, item):
    print("restart_doukhub_only")


def restart_all(icon, item):
    print("restart_all")


def quit_app(icon, item):
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
    icon.run()


if __name__ == "__main__":
    main()