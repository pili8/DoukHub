# DoukHub 托盘启动方案 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 DoukHub 增加托盘启动:双击启动 → 无黑窗口后台运行 + 托盘图标 + 浏览器自动打开;托盘菜单支持打开界面/重启服务(二级)/退出;开发热重载。

**Architecture:** 新增 `tray.py` 托盘主程序(pystray 画图标、监听点击),以子进程方式启动/停止 uvicorn 服务(隐藏窗口、reload=True 热重载),复用现有 `ServiceManager` 管理 TikTokDownloader/XHS。新增 `DoukHub.bat` 用 `pythonw` 无窗口启动。

**Tech Stack:** Python 3.12, pystray + Pillow(托盘图标), uvicorn reload(热重载), Windows subprocess。

## Global Constraints

- 仅 Windows 目标(用户环境 win32)。
- 隐藏窗口:托盘主程序用 `pythonw.exe` 运行;子进程用 `subprocess.CREATE_NO_WINDOW` 标志。
- 热重载:服务子进程以 reload=True 启动(开发模式)。
- 托盘菜单:打开界面 / 重启服务(二级:只重启 DoukHub、重启全部含下载器) / 退出。
- 下载器管理复用现有 `ServiceManager`(app/services/downloader.py),不重写。
- 新增依赖 pystray、Pillow,写入 requirements.txt。
- 每个任务结束提交 git(中文 commit message,feat 前缀)。

---

### Task 1: 安装依赖 + tray.py 骨架(托盘图标显示 + 菜单结构)

**Files:**
- Modify: `requirements.txt`(追加 pystray、Pillow)
- Create: `tray.py`(项目根目录)
- Test: 手动预览(运行 `venv/Scripts/python.exe tray.py` 应出现托盘图标)

**Interfaces:**
- Consumes: 无(本任务不启动服务,只有托盘骨架)。
- Produces: `tray.py` 可运行,托盘图标出现,菜单结构完整(动作暂为占位打印)。

- [ ] **Step 1: 安装依赖**

Run: `venv/Scripts/python.exe -m pip install pystray Pillow`
Expected: 安装成功。

- [ ] **Step 2: 追加 requirements.txt**

在 `D:\AI\DoukHub\requirements.txt` 末尾追加:

```
pystray
Pillow
```

- [ ] **Step 3: 创建 tray.py 骨架**

创建 `D:\AI\DoukHub\tray.py`:

```python
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
```

- [ ] **Step 4: 语法检查**

Run: `venv/Scripts/python.exe -c "import ast; ast.parse(open('tray.py', encoding='utf-8').read()); print('OK')"`
Expected: OK。

- [ ] **Step 5: 手动画预览(可选,默认跳过以免阻塞)**

Run: `venv/Scripts/python.exe tray.py`
Expected: 任务栏右下角出现青色圆角 D 图标;右键菜单出现"打开界面/重启服务/退出"。
注意:此步骤会阻塞终端,验证后按托盘"退出"结束。若当前环境无法显示 GUI,记录"待端到端验证"即可。

- [ ] **Step 6: 提交**

```bash
git add tray.py requirements.txt
git commit -m "feat(tray): 托盘程序骨架(图标+菜单结构)"
```

---

### Task 2: 服务子进程管理(启动/停止 uvicorn,隐藏窗口,热重载)+ 打开界面

**Files:**
- Modify: `tray.py`
- Test: 手动验证(运行 tray.py,点"打开界面"应打开浏览器)

**Interfaces:**
- Consumes: Task 1 的托盘骨架、`PORT`/`URL` 常量。
- Produces:
  - `start_server()`: 以子进程启动 uvicorn(reload=True, 隐藏窗口),返回 Popen。
  - `stop_server()`: 停止 uvicorn 子进程。
  - 启动时自动 start_server + 浏览器就绪后自动打开。
  - 菜单"打开界面"调 `webbrowser.open(URL)`。

**实现要点:**
- 服务以 `subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", str(PORT), "--reload"], creationflags=subprocess.CREATE_NO_WINDOW)` 启动。
- 注意:`sys.executable` 在 pythonw 下是 pythonw.exe,同样可用;为确保 reload 的 watch files 正常,`cwd` 设为项目根目录。
- 等待端口就绪:循环探测 `http://127.0.0.1:{PORT}/`(最长 20 秒),就绪后 `webbrowser.open(URL)`。
- 停止:`proc.terminate()` + `proc.wait(timeout=10)`,超时则 `proc.kill()`。

- [ ] **Step 1: 在 tray.py 增加服务进程管理函数**

在 `tray.py` 中追加(放在 `make_icon` 之后、菜单动作之前):

```python
import os
import subprocess
import time

import httpx

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
```

- [ ] **Step 2: 实现"打开界面"动作并接入启动流程**

修改 `tray.py` 的 `open_ui` 和 `main`:

```python
def open_ui(icon, item):
    webbrowser.open(URL)


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
        threading.Thread(
            target=lambda: (wait_ready() and webbrowser.open(URL)),
            daemon=True,
        ).start()

    icon.run()
```

- [ ] **Step 3: 语法检查**

Run: `venv/Scripts/python.exe -c "import ast; ast.parse(open('tray.py', encoding='utf-8').read()); print('OK')"`
Expected: OK。

- [ ] **Step 4: 手动验证(若环境可显示 GUI)**

Run: `venv/Scripts/python.exe tray.py`
Expected: 托盘图标出现;数秒后浏览器自动打开 DoukHub 页面;点"打开界面"重新打开浏览器。
注意:此步骤会阻塞,验证后从托盘"退出"。若无法显示 GUI,记录"待端到端验证"。

- [ ] **Step 5: 提交**

```bash
git add tray.py
git commit -m "feat(tray): 服务子进程管理(隐藏窗口+热重载)+打开界面"
```

---

### Task 3: 重启二级菜单 + 下载器管理 + 退出

**Files:**
- Modify: `tray.py`
- Test: 手动验证(重启只重启 DoukHub、重启全部连下载器一起重启、退出干净)

**Interfaces:**
- Consumes: Task 2 的 `start_server`/`stop_server`/`URL`;现有 `ServiceManager`(app/services/downloader.py)。
- Produces:
  - `restart_doukhub_only()`: 只停/起 uvicorn。
  - `restart_all()`: 停/起 uvicorn + 下载器。
  - `quit_app()`: 停全部 + 图标退出。
  - 启动时自动拉起下载器(复用现有 lifespan 逻辑或显式调用)。

**实现要点:**
- 下载器管理:直接 import 并复用 `app.services.downloader.ServiceManager`,构造参数从 `app.core.config.Config` 读取(默认配置 `~/.doukhub/config.json`):
  ```python
  from app.core.config import Config
  from app.services.downloader import ServiceManager
  _cfg = Config()
  _svc = ServiceManager(ttd_path=_cfg.ttd_path, ttd_port=_cfg.ttd_port,
                        xhs_path=_cfg.xhs_path, xhs_port=_cfg.xhs_port)
  ```
- `restart_all`: 先 `_svc.stop_all()`,再 `stop_server()`,再 `start_server()`,再 `_svc.start_all()`。
- `restart_doukhub_only`: 只 `stop_server()` + `start_server()`(下载器不动)。
- `quit_app`: `stop_server()` + `_svc.stop_all()` + `_svc.close()` + `icon.stop()`。
- 注意:uvicorn 服务自身 lifespan 也会自动 start_all(检测到已在运行会跳过),与托盘显式管理不冲突。

- [ ] **Step 1: 在 tray.py 实现重启与退出真实逻辑**

替换 Task 1 中 `restart_doukhub_only`/`restart_all`/`quit_app` 的占位实现:

```python
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


def restart_doukhub_only(icon, item):
    logger.info("只重启 DoukHub")
    stop_server()
    start_server()


def restart_all(icon, item):
    logger.info("重启全部(含下载器)")
    svc = get_service_manager()
    svc.stop_all()
    stop_server()
    start_server()
    svc.start_all()


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
```

- [ ] **Step 2: 语法检查**

Run: `venv/Scripts/python.exe -c "import ast; ast.parse(open('tray.py', encoding='utf-8').read()); print('OK')"`
Expected: OK。

- [ ] **Step 3: 后端回归(确认 import 不破坏现有代码)**

Run: `venv/Scripts/python.exe -m pytest tests/ -q`
Expected: 全部通过(现有测试不受影响)。

- [ ] **Step 4: 手动验证(若环境可显示 GUI)**

Run: `venv/Scripts/python.exe tray.py`
Expected:
- 托盘出现,数秒后浏览器打开。
- 点"重启服务 → 只重启 DoukHub":服务重启,下载器(TikTokDownloader/XHS 若在运行)保持。
- 点"重启服务 → 重启全部":三者都重启。
- 点"退出":服务停止、下载器停止、托盘图标消失。
阻塞说明:若无法显示 GUI,记录"待端到端验证"。

- [ ] **Step 5: 提交**

```bash
git add tray.py
git commit -m "feat(tray): 重启二级菜单+下载器管理+退出"
```

---

### Task 4: 启动脚本(DoukHub.bat)+ 端到端手动验证

**Files:**
- Create: `DoukHub.bat`(项目根目录)
- Modify: 无(若旧 start.bat 保留不动)
- Test: 双击 DoukHub.bat 端到端验证

**Interfaces:**
- Consumes: Task 1-3 的 tray.py。
- Produces: 双击即可启动的 `DoukHub.bat`(pythonw 无窗口运行 tray.py)。

- [ ] **Step 1: 创建 DoukHub.bat**

创建 `D:\AI\DoukHub\DoukHub.bat`:

```bat
@echo off
rem DoukHub 托盘启动(无黑窗口)
cd /d "%~dp0"
rem 清理旧进程(端口 2999)
for /f "tokens=5" %%a in ('netstat -aon ^| findstr ":2999" ^| findstr "LISTENING"') do (
    taskkill /F /PID %%a >nul 2>&1
)
rem 用 pythonw 无窗口启动托盘程序
start "" venv\Scripts\pythonw.exe tray.py
```

- [ ] **Step 2: 端到端手动验证(核心验收)**

双击 `DoukHub.bat`,依次确认:

| 验证项 | 期望 |
|--------|------|
| 双击启动 | 无黑窗口弹出;数秒后托盘图标出现;浏览器自动打开 DoukHub |
| 左键单击托盘图标 | 打开浏览器(若 default=True 生效) |
| 右键菜单 | 打开界面 / 重启服务(二级) / 退出 |
| 只重启 DoukHub | 服务重启,下载器保持 |
| 重启全部 | DoukHub + 下载器都重启 |
| 改代码热重载 | 修改任意 .py 保存后,刷新浏览器即生效(无需手动画重启) |
| 退出 | 所有进程结束、托盘图标消失;netstat 确认 2999 端口无监听 |

- [ ] **Step 3: 记录验证结论到报告**

把验证结果写入 `D:\AI\DoukHub\.superpowers\sdd\2026-08-12-doukhub-tray-launcher\tray-verify-report.md`:
- 逐项验证结果(通过/现象)
- 任何问题及处理

- [ ] **Step 4: 提交**

```bash
git add DoukHub.bat
git commit -m "feat(launcher): DoukHub.bat 托盘无窗口启动脚本"
```

## 自审记录

- **Spec 覆盖**:① 托盘图标 → Task 1;② 隐藏窗口(pythonw + CREATE_NO_WINDOW)→ Task 2/4;③ 热重载(reload=True)→ Task 2;④ 菜单(打开/重启二级/退出)→ Task 1/2/3;⑤ 下载器复用 ServiceManager → Task 3;⑥ 启动脚本 → Task 4。
- **占位符**:无 TBD/TODO,每步含具体代码。
- **类型一致性**:`start_server`/`stop_server`/`restart_doukhub_only`/`restart_all`/`quit_app`/`open_ui` 在 Task 1-3 中签名一致;`PORT`/`URL` 常量贯穿。