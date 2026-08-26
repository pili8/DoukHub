"""单作品「本地下载」后台 worker。

把「解析 + 下载到本地」从请求生命周期解耦：
点下载 → 创建 single_work_history(pending) → 入队 → 立即返回 history_id。
后台协程逐个执行解析+下载，进度写入 single_work_history 表，
刷新网页 / 关浏览器都不中断；服务重启后恢复 pending/running 继续下载。

与增量采集的 CollectionBatchManager 是同一套模式：
后台协程 + 数据库持久化 + 断点续跑。
"""
import asyncio
import json
from pathlib import Path
from typing import Optional

import httpx

from . import single_work
from .database import Database


class DownloadWorker:
    def __init__(self, db: Database, client: httpx.AsyncClient, ttd_url: str):
        self.db = db
        self.client = client
        self.ttd_url = ttd_url
        self._queue: asyncio.Queue[int] = asyncio.Queue()
        self._worker: Optional[asyncio.Task] = None

    def start(self) -> None:
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._worker_loop())

    def enqueue(self, history_id: int) -> None:
        self._queue.put_nowait(history_id)
        self.start()

    def recover(self) -> None:
        """服务启动时恢复未完成的下载（pending / running）。"""
        rows = self.db.list_single_work_history(limit=200)
        for row in reversed(rows):
            if row.get("status") in ("pending", "running"):
                self.enqueue(row["id"])

    async def _worker_loop(self) -> None:
        while True:
            history_id = await self._queue.get()
            try:
                await self._execute(history_id)
            except Exception:
                pass
            finally:
                self._queue.task_done()

    async def _execute(self, history_id: int) -> None:
        h = self.db.get_single_work_history(history_id)
        if not h or h.get("status") not in ("pending", "running"):
            return
        self.db.update_single_work_history(history_id, status="running")

        req = {}
        try:
            req = json.loads(h.get("request_json") or "{}")
        except Exception:
            req = {}

        template = h.get("filename_template") or req.get("filename_template") or "{create_time} {author} {title}"
        override = h.get("filename_override") or ""
        asset_indexes = req.get("asset_indexes") or []
        include_music = bool(req.get("include_music"))
        include_static_cover = bool(req.get("include_static_cover"))
        include_dynamic_cover = bool(req.get("include_dynamic_cover"))
        folder_mode = bool(req.get("folder_mode"))
        target_dir = Path(h.get("target_dir") or ".")
        link = h.get("source_link") or ""
        platform = h.get("platform") or "douyin"

        try:
            work = None
            if h.get("work_json"):
                try:
                    work = json.loads(h["work_json"])
                except Exception:
                    work = None
            if work is None:
                cookie = self._pick_cookie()
                work = await single_work.fetch_work(self.client, self.ttd_url, link, platform, cookie)

            paths = await single_work.download_work(
                self.client,
                work,
                target_dir,
                template,
                filename_override=override,
                asset_indexes=asset_indexes,
                include_music=include_music,
                include_static_cover=include_static_cover,
                include_dynamic_cover=include_dynamic_cover,
                folder_mode=folder_mode,
            )

            self.db.update_single_work_history(
                history_id,
                status="success",
                work_id=str(work.get("id", "")),
                work_type=str(work.get("type", "")),
                title=str(work.get("title", "")),
                author=str(work.get("author", "")),
                files_json=json.dumps([str(p) for p in paths]),
                work_json=json.dumps(work, ensure_ascii=False),
            )
        except Exception as error:
            self.db.update_single_work_history(history_id, status="failed", error=str(error))

    def _pick_cookie(self) -> str:
        cookies = self.db.get_enabled_cookies()
        cookie_list = [ck.get("Cookie", "") for ck in cookies if ck.get("Cookie")]
        return cookie_list[0] if cookie_list else ""
