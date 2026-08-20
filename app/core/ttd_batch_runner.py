"""Run one TTD terminal account batch without interactive menu input.

This script is launched with TTD's repository as the current working directory.
It must not import TTD at module import time so DoukHub can compile and test it
even when TTD dependencies are absent.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace


MARKER_PREFIX = "__DOUKHUB__"


def marker_line(line: str) -> dict | None:
    if MARKER_PREFIX not in line:
        return None
    payload = line[line.index(MARKER_PREFIX) + len(MARKER_PREFIX):].strip()
    try:
        value = json.loads(payload)
        return value if isinstance(value, dict) else None
    except json.JSONDecodeError:
        return None


def emit_marker(payload: dict) -> None:
    print(f"{MARKER_PREFIX}{json.dumps(payload, ensure_ascii=False)}", flush=True)


def init_ttd_database(root: Path) -> None:
    database = root / "DouK-Downloader.db"
    with sqlite3.connect(database, timeout=5.0) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS config_data (
                NAME TEXT PRIMARY KEY,
                VALUE INTEGER NOT NULL CHECK(VALUE IN (0, 1))
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS option_data (
                NAME TEXT PRIMARY KEY,
                VALUE TEXT NOT NULL
            )
            """
        )
        conn.executemany(
            "INSERT OR IGNORE INTO config_data(NAME, VALUE) VALUES (?, ?)",
            [("Disclaimer", 1), ("Record", 1), ("Logger", 0)],
        )
        conn.execute(
            "INSERT OR IGNORE INTO option_data(NAME, VALUE) VALUES ('Language', 'zh_CN')"
        )
        conn.commit()


async def run_platform(platform: str) -> int:
    root = Path.cwd().resolve()
    sys.path.insert(0, str(root))
    init_ttd_database(root)

    try:
        import rich.console as rich_console
        rich_console.detect_legacy_windows = lambda: False
    except Exception:
        pass

    from src.application import TikTokDownloader
    from src.application.main_terminal import TikTok
    from src.custom import suspend

    with (root / "Volume" / "settings.json").open("r", encoding="utf-8-sig") as file:
        settings = json.load(file)
    key = "accounts_urls" if platform == "douyin" else "accounts_urls_tiktok"
    accounts = [
        SimpleNamespace(**item)
        for item in settings.get(key, [])
        if item.get("enable", True)
    ]
    if not accounts:
        emit_marker({"type": "summary", "total": 0, "success": 0, "failed": 0})
        return 0

    async with TikTokDownloader() as downloader:
        downloader.check_config()
        await downloader.check_settings(False)
        terminal = TikTok(downloader.parameter, downloader.database)
        tiktok = platform == "tiktok"
        success = 0
        failed = 0
        total = len(accounts)

        for index, item in enumerate(accounts, start=1):
            name = item.mark or getattr(item, "url", "")
            emit_marker(
                {
                    "type": "account_start",
                    "index": index,
                    "total": total,
                    "sec_user_id": "",
                    "url": item.url,
                    "account_name": name,
                }
            )
            result = False
            resolved = ""
            message = ""
            try:
                resolved = await terminal.check_sec_user_id(item.url, tiktok)
                if not resolved:
                    raise RuntimeError("无法从账号链接提取 sec_user_id")
                result = bool(
                    await terminal.deal_account_detail(
                        index,
                        resolved,
                        mark=item.mark,
                        tab="post",
                        earliest=getattr(item, "earliest", "") or "",
                        latest=getattr(item, "latest", "") or "",
                        tiktok=tiktok,
                    )
                )
                if result:
                    success += 1
                    message = "下载完成"
                else:
                    failed += 1
                    message = "TTD 返回账号处理失败"
            except Exception as error:
                failed += 1
                message = str(error)

            emit_marker(
                {
                    "type": "account_result",
                    "index": index,
                    "total": total,
                    "sec_user_id": resolved,
                    "account_name": name,
                    "status": "success" if result else "failed",
                    "message": message,
                }
            )
            if index != total and result:
                await suspend(index, terminal.console)

        emit_marker(
            {"type": "summary", "total": total, "success": success, "failed": failed}
        )
        return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--platform", choices=("douyin", "tiktok"), required=True)
    args = parser.parse_args()
    try:
        return asyncio.run(run_platform(args.platform))
    except Exception as error:
        emit_marker(
            {
                "type": "summary",
                "total": 0,
                "success": 0,
                "failed": 1,
                "message": str(error),
            }
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
