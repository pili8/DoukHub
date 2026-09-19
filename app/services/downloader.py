"""Downloader API 服务进程管理"""
import logging
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

import httpx


class DownloaderService:
    """管理单个 Downloader API 服务的生命周期"""

    def __init__(
        self,
        name: str,
        path: str,
        port: int,
        startup_cmd: str = "api",
        repo_url: str = "",
    ):
        self.name = name
        self.path = Path(path).resolve()
        self.port = port
        self.startup_cmd = startup_cmd
        self.repo_url = repo_url
        self.process: Optional[subprocess.Popen] = None
        self._client = httpx.Client(timeout=3)
        self._fail_count = 0
        self._log_path = self.path / "logs" / f"doukhub_{self.name}.log"

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def is_running(self) -> bool:
        """HTTP-only check: process alive does not mean service is responsive"""
        try:
            resp = self._client.get(f"{self.base_url}/")
            return resp.status_code in (200, 307, 404)
        except Exception:
            return False

    def start(self) -> dict:
        """启动 Downloader API 服务"""
        if self.is_running:
            return {"success": True, "message": f"{self.name} 已在运行中"}

        main_py = self.path / "main.py"
        if not main_py.exists():
            return {"success": False, "message": f"找不到 {main_py}"}

        try:
            python = sys.executable  # 使用当前 Python 解释器
            if self.name == "TikTokDownloader":
                # TTD 直接启动 Web API 模式（绕过交互式菜单）
                launcher = self.path / "_doukhub_launcher.py"
                launcher.write_text(
                    "# Patch: rich legacy Windows detect causes OSError on some systems\n"
                    "try:\n"
                    "    import rich.console as _rc\n"
                    "    _rc.detect_legacy_windows = lambda: False\n"
                    "except Exception:\n"
                    "    pass\n"
                    "import asyncio\n"
                    "import aiosqlite\n"
                    "from src.application import TikTokDownloader\n"
                    "from src.custom import ROOT\n\n"
                    "async def init_db():\n"
                    "    db_file = ROOT / 'DouK-Downloader.db'\n"
                    "    async with aiosqlite.connect(db_file, timeout=5.0) as db:\n"
                    "        await db.execute('PRAGMA journal_mode=WAL')\n"
                    "        await db.execute('PRAGMA foreign_keys=ON')\n"
                    "        await db.execute('PRAGMA busy_timeout=5000')\n"
                    "        await db.execute('''CREATE TABLE IF NOT EXISTS config_data (\n"
                    "            NAME TEXT PRIMARY KEY,\n"
                    "            VALUE INTEGER NOT NULL CHECK(VALUE IN (0, 1))\n"
                    "        )''')\n"
                    "        await db.execute('''CREATE TABLE IF NOT EXISTS option_data (\n"
                    "            NAME TEXT PRIMARY KEY,\n"
                    "            VALUE TEXT NOT NULL\n"
                    "        )''')\n"
                    "        await db.execute(\"INSERT OR REPLACE INTO config_data (NAME, VALUE) VALUES ('Disclaimer', 1)\")\n"
                    "        await db.execute(\"INSERT OR REPLACE INTO config_data (NAME, VALUE) VALUES ('Record', 1)\")\n"
                    "        await db.execute(\"INSERT OR REPLACE INTO config_data (NAME, VALUE) VALUES ('Logger', 0)\")\n"
                    "        await db.execute(\"INSERT OR REPLACE INTO option_data (NAME, VALUE) VALUES ('Language', 'zh_CN')\")\n"
                    "        await db.commit()\n\n"
                    "async def main():\n"
                    "    await init_db()\n"
                    "    async with TikTokDownloader() as d:\n"
                    "        d.check_config()\n"
                    "        await d.check_settings(False)\n"
                    "        await d.server()\n\n"
                    "if __name__ == '__main__':\n"
                    "    asyncio.run(main())\n",
                    encoding="utf-8",
                )
                self.path.joinpath('logs').mkdir(exist_ok=True)
                log_file = open(self._log_path, 'w', encoding='utf-8')
                creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
                self.process = subprocess.Popen(
                    [python, str(launcher)],
                    cwd=str(self.path),
                    stdout=log_file,
                    stderr=log_file,
                    creationflags=creationflags,
                )
            else:
                # XHS-Downloader: python main.py API
                self.path.joinpath('logs').mkdir(exist_ok=True)
                log_file = open(self._log_path, 'w', encoding='utf-8')
                creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
                self.process = subprocess.Popen(
                    [python, str(main_py), "API"],
                    cwd=str(self.path),
                    stdout=log_file,
                    stderr=log_file,
                    creationflags=creationflags,
                )

            # 等待服务启动
            for _ in range(30):
                time.sleep(1)
                if self.is_running:
                    return {"success": True, "message": f"{self.name} 启动成功"}

            return {"success": False, "message": f"{self.name} 启动超时"}
        except Exception as e:
            return {"success": False, "message": f"{self.name} 启动失败: {e}"}

    def stop(self) -> dict:
        """停止 Downloader API 服务"""
        # 清理临时启动脚本
        launcher = self.path / "_doukhub_launcher.py"
        if launcher.exists():
            launcher.unlink()
        if not self.process:
            return {"success": True, "message": f"{self.name} 未在运行"}
        try:
            self.process.terminate()
            self.process.wait(timeout=10)
            self.process = None
            return {"success": True, "message": f"{self.name} 已停止"}
        except Exception as e:
            self.process.kill()
            self.process = None
            return {"success": False, "message": f"{self.name} 强制停止: {e}"}


    def health_check(self) -> dict:
        """Heartbeat: restart service after 2 consecutive HTTP failures"""
        if self.is_running:
            self._fail_count = 0
            return {"healthy": True}
        self._fail_count += 1
        logging.info(f"{self.name} health check failed ({self._fail_count}/2)")
        if self._fail_count >= 2:
            logging.warning(f"{self.name} unresponsive, auto-restarting...")
            self.stop()
            result = self.start()
            self._fail_count = 0
            return {"healthy": False, "restarted": True, "message": result.get("message", "")}
        return {"healthy": False}

    def status(self) -> dict:
        """获取服务状态"""
        running = self.is_running
        return {
            "name": self.name,
            "port": self.port,
            "running": running,
            "url": self.base_url,
        }

    def update(self) -> dict:
        """通过 git pull 更新源代码。

        本地有修改时自动 stash → pull → stash pop；
        如果 stash pop 冲突则丢弃本地修改（新版可能已修复同类问题）。
        所有 subprocess 调用隐藏 CMD 黑框。
        """
        # 内核未安装
        if not self.source_exists:
            return {
                "name": self.name,
                "success": False,
                "message": f"{self.name} 内核未安装，请先下载内核源码",
            }
        git_dir = self.path / ".git"
        if not git_dir.exists():
            return {
                "name": self.name,
                "success": False,
                "message": f"{self.name} 不是 git 仓库，无法自动更新",
            }

        # Windows: 隐藏 CMD 黑框
        creationflags = 0
        if sys.platform == "win32":
            creationflags = subprocess.CREATE_NO_WINDOW

        try:
            # 先停止服务
            was_running = self.is_running
            if was_running:
                self.stop()

            # Step 1: stash 本地修改（如果有）
            # 注意：git stash 可能因日志文件被占用等产生警告（stderr），
            #       但 stash 本身可能已成功。用 stash list 判断是否真的 stash 了。
            stash_result = subprocess.run(
                ["git", "stash"],
                cwd=str(self.path),
                capture_output=True,
                text=True,
                timeout=30,
                creationflags=creationflags,
            )
            stash_output = stash_result.stdout + stash_result.stderr
            # 判断是否真的 stash 了：输出含 "Saved working directory" 表示成功 stash
            stashed = "Saved working directory" in stash_output
            if stashed:
                logging.info(f"{self.name} 已 stash 本地修改")
            elif "No local changes" in stash_output or "没有要提交" in stash_output:
                logging.info(f"{self.name} 无本地修改，直接 pull")
            else:
                # stash 出错（可能因文件被占用等），尝试用 checkout 丢弃 tracked 文件的修改
                logging.warning(f"{self.name} stash 异常: {stash_output.strip()}")
                # 强制丢弃 tracked 文件的修改，确保 pull 不被阻塞
                subprocess.run(
                    ["git", "checkout", "--"],
                    cwd=str(self.path),
                    capture_output=True,
                    text=True,
                    timeout=15,
                    creationflags=creationflags,
                )

            # Step 2: git pull
            result = subprocess.run(
                ["git", "pull"],
                cwd=str(self.path),
                capture_output=True,
                text=True,
                timeout=120,
                creationflags=creationflags,
            )

            output = result.stdout + result.stderr

            # Step 3: 如果 stash 了，尝试恢复
            if stashed and result.returncode == 0:
                pop_result = subprocess.run(
                    ["git", "stash", "pop"],
                    cwd=str(self.path),
                    capture_output=True,
                    text=True,
                    timeout=30,
                    creationflags=creationflags,
                )
                pop_output = pop_result.stdout + pop_result.stderr
                if pop_result.returncode != 0 or "CONFLICT" in pop_output:
                    # stash pop 冲突 → 丢弃本地修改，用新版代码
                    logging.warning(f"{self.name} stash pop 冲突，丢弃本地修改: {pop_output.strip()}")
                    # 丢弃所有冲突标记和本地修改，恢复到新版代码
                    subprocess.run(
                        ["git", "checkout", "--", "."],
                        cwd=str(self.path),
                        capture_output=True,
                        text=True,
                        timeout=15,
                        creationflags=creationflags,
                    )
                    # 清理暂存区中可能残留的 stash pop 合并状态
                    subprocess.run(
                        ["git", "reset", "HEAD"],
                        cwd=str(self.path),
                        capture_output=True,
                        text=True,
                        timeout=15,
                        creationflags=creationflags,
                    )
                    subprocess.run(
                        ["git", "checkout", "--", "."],
                        cwd=str(self.path),
                        capture_output=True,
                        text=True,
                        timeout=15,
                        creationflags=creationflags,
                    )
                    subprocess.run(
                        ["git", "stash", "drop"],
                        cwd=str(self.path),
                        capture_output=True,
                        text=True,
                        timeout=15,
                        creationflags=creationflags,
                    )
                    output += "\n（本地修改已丢弃，使用新版代码）"

            if result.returncode == 0:
                # 更新依赖
                req_file = self.path / "requirements.txt"
                if req_file.exists():
                    subprocess.run(
                        [sys.executable, "-m", "pip", "install", "-r", str(req_file)],
                        capture_output=True,
                        timeout=120,
                        creationflags=creationflags,
                    )

                # 如果之前在运行，重新启动
                if was_running:
                    self.start()

                if "Already up to date" in output or "已经是最新" in output:
                    return {"name": self.name, "success": True, "message": f"{self.name} 已是最新版本"}
                return {"name": self.name, "success": True, "message": f"{self.name} 更新完成: {output.strip()}"}
            else:
                # pull 失败 → 如果 stash 了，恢复本地修改
                if stashed:
                    subprocess.run(
                        ["git", "stash", "pop"],
                        cwd=str(self.path),
                        capture_output=True,
                        text=True,
                        timeout=30,
                        creationflags=creationflags,
                    )
                return {"name": self.name, "success": False, "message": f"{self.name} 更新失败: {output.strip()}"}

        except subprocess.TimeoutExpired:
            return {"name": self.name, "success": False, "message": f"{self.name} 更新超时"}
        except Exception as e:
            return {"name": self.name, "success": False, "message": f"{self.name} 更新异常: {e}"}

    def get_version(self) -> str:
        """获取当前版本信息"""
        git_dir = self.path / ".git"
        if not git_dir.exists():
            return "(非 git 仓库)"
        try:
            result = subprocess.run(
                ["git", "log", "-1", "--format=%h %ci"],
                cwd=str(self.path),
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0:
                return result.stdout.strip()
        except Exception:
            pass
        return "(未知)"

    def close(self):
        self.stop()
        self._client.close()


    @property
    def source_exists(self) -> bool:
        """检测内核源码是否已下载"""
        return (self.path / "main.py").exists()


class ServiceManager:
    """管理所有 Downloader 服务"""

    def __init__(self, ttd_path: str, ttd_port: int, xhs_path: str, xhs_port: int):
        self.ttd = DownloaderService("TikTokDownloader", ttd_path, ttd_port, repo_url="https://github.com/JoeanAmier/TikTokDownloader")
        self.xhs = DownloaderService("XHS-Downloader", xhs_path, xhs_port, repo_url="https://github.com/JoeanAmier/XHS-Downloader")

    @property
    def services(self) -> list[DownloaderService]:
        return [self.ttd, self.xhs]

    def start_all(self) -> list[dict]:
        results = []
        for svc in self.services:
            results.append(svc.start())
        return results

    def stop_all(self) -> list[dict]:
        results = []
        for svc in self.services:
            results.append(svc.stop())
        return results

    def status_all(self) -> list[dict]:
        return [svc.status() for svc in self.services]

    def get_service(self, name: str) -> DownloaderService | None:
        for svc in self.services:
            if svc.name.lower().replace("-", "") == name.lower().replace("-", ""):
                return svc
        return None

    def update_all(self) -> list[dict]:
        """更新所有 Downloader"""
        results = []
        for svc in self.services:
            results.append(svc.update())
        return results

    def update(self, name: str) -> dict:
        """更新指定 Downloader"""
        svc = self.get_service(name)
        if svc:
            result = svc.update()
            result["name"] = svc.name
            return result
        return {"name": name, "success": False, "message": f"未找到服务: {name}"}

    def get_versions(self) -> list[dict]:
        """获取所有 Downloader 版本信息"""
        return [
            {"name": svc.name, "version": svc.get_version(), "path": str(svc.path)}
            for svc in self.services
        ]

    def close(self):
        for svc in self.services:
            svc.close()


    def install(self, name: str) -> dict:
        """从 GitHub 克隆内核源码"""
        svc = self.get_service(name)
        if not svc:
            return {"success": False, "message": f"未找到服务: {name}"}
        if svc.source_exists:
            return {"success": True, "message": f"{svc.name} 已安装"}
        if not svc.repo_url:
            return {"success": False, "message": f"{svc.name} 未配置仓库地址"}
        try:
            svc.path.parent.mkdir(parents=True, exist_ok=True)
            creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
            result = subprocess.run(
                ["git", "clone", "--depth", "1", svc.repo_url, str(svc.path)],
                capture_output=True,
                text=True,
                timeout=300,
                creationflags=creationflags,
            )
            output = result.stdout + result.stderr
            if result.returncode == 0:
                req_file = svc.path / "requirements.txt"
                if req_file.exists():
                    subprocess.run(
                        [sys.executable, "-m", "pip", "install", "-r", str(req_file)],
                        capture_output=True,
                        timeout=300,
                        creationflags=creationflags,
                    )
                return {"success": True, "message": f"{svc.name} 下载安装完成"}
            return {"success": False, "message": f"{svc.name} 下载失败:\n{output.strip()}"}
        except subprocess.TimeoutExpired:
            return {"success": False, "message": f"{svc.name} 下载超时"}
        except Exception as e:
            return {"success": False, "message": f"{svc.name} 安装异常: {e}"}
