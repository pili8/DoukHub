"""启动 TTD 服务"""
import json
import os
import sys
import time
import subprocess
from pathlib import Path

# 1. 读取配置
config_path = Path.home() / ".doukhub" / "config.json"
print(f"配置文件: {config_path}")
with open(config_path, "r", encoding="utf-8") as f:
    config = json.load(f)

downloader_cfg = config.get("downloader", {})
ttd_path_str = downloader_cfg.get("tiktok_downloader_path", "./TikTokDownloader")
ttd_port = downloader_cfg.get("ttd_port", 5555)

# 解析路径
if not os.path.isabs(ttd_path_str):
    ttd_path = Path("d:/AI/DoukHub") / ttd_path_str
else:
    ttd_path = Path(ttd_path_str)

print(f"TTD 路径: {ttd_path}")
print(f"TTD 端口: {ttd_port}")
print(f"main.py 存在: {(ttd_path / 'main.py').exists()}")

if not (ttd_path / "main.py").exists():
    print(f"❌ TTD 内核未安装: {ttd_path / 'main.py'} 不存在")
    sys.exit(1)

# 2. 检查 TTD 是否已经在运行
import httpx
try:
    resp = httpx.get(f"http://127.0.0.1:{ttd_port}/", timeout=3)
    if resp.status_code in (200, 307, 404):
        print(f"✅ TTD 服务已在运行 (HTTP {resp.status_code})")
        sys.exit(0)
except Exception:
    pass

# 3. 创建启动脚本
launcher = ttd_path / "_doukhub_launcher.py"
launcher.write_text(
    "# Patch: rich legacy Windows\n"
    "try:\n"
    "    import rich.console as _rc\n"
    "    _rc.detect_legacy_windows = lambda: False\n"
    "except Exception:\n"
    "    pass\n"
    "import asyncio\n"
    "import aiosqlite\n"
    "from src.application import TikTokDownloader\n"
    "from src.custom import PROJECT_ROOT\n\n"
    "async def init_db():\n"
    "    db_file = PROJECT_ROOT / 'DouK-Downloader.db'\n"
    "    async with aiosqlite.connect(db_file) as db:\n"
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
print(f"启动脚本已创建: {launcher}")

# 4. 启动 TTD 服务
print("正在启动 TTD 服务...")
python = sys.executable
process = subprocess.Popen(
    [python, str(launcher)],
    cwd=str(ttd_path),
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
)
print(f"进程 PID: {process.pid}")

# 5. 等待服务启动
for i in range(30):
    time.sleep(1)
    try:
        resp = httpx.get(f"http://127.0.0.1:{ttd_port}/", timeout=2)
        if resp.status_code in (200, 307, 404):
            print(f"✅ TTD 服务启动成功! (等待了 {i+1} 秒, HTTP {resp.status_code})")
            print(f"   地址: http://127.0.0.1:{ttd_port}")
            sys.exit(0)
    except Exception:
        pass
    # 检查进程是否已退出
    if process.poll() is not None:
        stdout = process.stdout.read().decode("utf-8", errors="replace") if process.stdout else ""
        stderr = process.stderr.read().decode("utf-8", errors="replace") if process.stderr else ""
        print(f"❌ TTD 进程已退出 (code={process.returncode})")
        if stderr:
            print(f"   stderr: {stderr[:500]}")
        if stdout:
            print(f"   stdout: {stdout[:500]}")
        sys.exit(1)
    print(f"  等待中... ({i+1}/30)")

print("❌ TTD 服务启动超时")
sys.exit(1)
