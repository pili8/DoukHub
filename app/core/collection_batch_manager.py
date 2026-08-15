"""Manage persistent collection batches and one TTD terminal process at a time."""
from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

import httpx

from .collection_planner import PlannedAccount, plan_collection, write_ttd_accounts
from .database import Database
from .ttd_batch_runner import marker_line


class CollectionBatchManager:
    def __init__(
        self,
        database: Database,
        ttd_path: Path,
        log_dir: Path,
        ttd_url: str,
    ):
        self.db = database
        self.ttd_path = Path(ttd_path).resolve()
        self.log_dir = Path(log_dir).resolve()
        self.ttd_url = ttd_url.rstrip("/")
        self.runner_path = Path(__file__).with_name("ttd_batch_runner.py").resolve()
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._worker: Optional[asyncio.Task] = None
        self._active_batch_id: Optional[str] = None
        self._active_process: Optional[asyncio.subprocess.Process] = None
        self._cancel_requested = False
        self._closing = False

    async def start(
        self,
        accounts: list[dict],
        rating_min: int = 3,
        tags: list[str] | None = None,
        account_names: str = "",
        record_ids: list[str] | None = None,
        platforms: tuple[str, ...] = ("douyin",),
        mode: str = "incremental",
    ) -> list[dict]:
        if self.db.get_active_collection_batch():
            if self._worker and not self._worker.done():
                raise RuntimeError("已有采集批次正在执行或等待执行")
            self.recover_interrupted_batches()
        if self.db.get_active_collection_batch():
            raise RuntimeError("已有采集批次正在执行或等待执行")

        created: list[dict] = []
        stamp = datetime.now().strftime("%Y%m%d%H%M%S")
        self.log_dir.mkdir(parents=True, exist_ok=True)
        for platform in platforms:
            planned = plan_collection(
                accounts=accounts,
                rating_min=rating_min,
                tags=tags,
                account_names=account_names,
                record_ids=record_ids,
                platform=platform,
                mode=mode,
            )
            if not planned:
                continue
            batch_id = f"{stamp}-{platform}-{uuid.uuid4().hex[:6]}"
            log_path = self.log_dir / f"{batch_id}.log"
            snapshot = {
                "rating_min": rating_min,
                "tags": tags or [],
                "account_names": account_names,
                "record_ids": record_ids or [],
                "platform": platform,
                "mode": mode,
            }
            self.db.create_collection_batch(
                batch_id=batch_id,
                filter_json=json.dumps(snapshot, ensure_ascii=False),
                platform=platform,
                log_path=str(log_path),
                items=[{**vars(item), "account_record_id": item.record_id} for item in planned],
            )
            created.append(
                {
                    "id": batch_id,
                    "platform": platform,
                    "total_accounts": len(planned),
                    "status": "pending",
                }
            )
            self._queue.put_nowait(batch_id)

        if not created:
            raise ValueError("没有符合条件的账号")
        self._ensure_worker()
        return created

    def cancel(self, batch_id: str) -> bool:
        batch = self.db.get_collection_batch(batch_id)
        if not batch or batch["status"] not in ("pending", "running"):
            return False
        self.db.update_collection_batch(batch_id, status="cancelling")
        if batch_id == self._active_batch_id:
            self._cancel_requested = True
            if self._active_process and self._active_process.returncode is None:
                self._active_process.terminate()
        return True

    def read_log(self, batch_id: str, max_lines: int = 200) -> list[str]:
        batch = self.db.get_collection_batch(batch_id)
        if not batch or not batch.get("log_path"):
            return []
        path = Path(batch["log_path"])
        if not path.exists():
            return []
        with path.open("r", encoding="utf-8", errors="replace") as file:
            return [line.rstrip("\r\n") for line in file][-max_lines:]

    def recover_interrupted_batches(self) -> None:
        for batch in self.db.list_active_collection_batches():
            process_pid = batch.get("process_pid")
            if process_pid and self._verify_recorded_runner(process_pid):
                self._terminate_recorded_runner(process_pid)
            self._finalize(
                batch["id"],
                "failed" if batch["status"] == "running" else "cancelled",
                -1,
                "DoukHub 重启，批次中断",
            )

    async def shutdown(self) -> None:
        self._closing = True
        if self._active_process and self._active_process.returncode is None:
            self._active_process.terminate()
            await self._active_process.wait()
        if self._active_batch_id:
            self._finalize(
                self._active_batch_id,
                "cancelled",
                self._active_process.returncode if self._active_process else -1,
                "DoukHub 关闭，批次已取消",
            )
        if self._worker:
            self._worker.cancel()
            try:
                await self._worker
            except asyncio.CancelledError:
                pass

    def _ensure_worker(self) -> None:
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._worker_loop())

    async def _worker_loop(self) -> None:
        while True:
            if self._closing and self._queue.empty():
                return
            batch_id = await self._queue.get()
            try:
                batch = self.db.get_collection_batch(batch_id)
                if not batch or batch["status"] == "cancelling":
                    self._finalize(batch_id, "cancelled", -1, "批次已取消")
                    continue
                await self._run_batch(batch_id)
            except Exception as error:
                if self._active_batch_id == batch_id:
                    if self._active_process and self._active_process.returncode is None:
                        self._active_process.terminate()
                    self._clear_active_batch()
                try:
                    self._finalize(batch_id, "failed", -1, str(error))
                except Exception:
                    pass
            finally:
                if self._active_batch_id == batch_id:
                    self._clear_active_batch()
                self._queue.task_done()

    async def _check_ttd_api(self) -> None:
        async with httpx.AsyncClient(timeout=3) as client:
            try:
                response = await client.get(f"{self.ttd_url}/")
            except Exception as error:
                raise RuntimeError(f"TTD API 未运行: {error}") from error
        if response.status_code not in (200, 307, 404):
            raise RuntimeError("TTD API 未运行")

    async def _launch_process(
        self, command: list[str], cwd: Path
    ) -> asyncio.subprocess.Process:
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        return await asyncio.create_subprocess_exec(
            *command,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            creationflags=creationflags,
        )

    def _verify_recorded_runner(self, process_pid: int) -> bool:
        try:
            if os.name == "nt":
                creationflags = subprocess.CREATE_NO_WINDOW
                completed = subprocess.run(
                    [
                        "powershell",
                        "-NoProfile",
                        "-Command",
                        (
                            '(Get-CimInstance Win32_Process -Filter '
                            f'"ProcessId = {process_pid}").CommandLine'
                        ),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=3,
                    creationflags=creationflags,
                )
            else:
                completed = subprocess.run(
                    ["ps", "-p", str(process_pid), "-o", "command="],
                    capture_output=True,
                    text=True,
                    timeout=3,
                )
            command_line = completed.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return False
        return str(self.runner_path).casefold() in command_line.casefold()

    def _terminate_recorded_runner(self, process_pid: int) -> bool:
        if not self._verify_recorded_runner(process_pid):
            return False
        try:
            os.kill(process_pid, signal.SIGTERM)
        except OSError:
            return False
        return True

    async def _run_batch(self, batch_id: str) -> str:
        self._active_batch_id = batch_id
        self._cancel_requested = False
        batch = self.db.get_collection_batch(batch_id)
        items = self.db.get_collection_batch_items(batch_id)
        pending = [item for item in items if item["status"] == "pending"]
        started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        if not pending:
            self._clear_active_batch()
            self._finalize(batch_id, "completed", 0)
            return "completed"

        try:
            await self._check_ttd_api()
            planned_items = [
                PlannedAccount(
                    record_id=str(item.get("account_record_id") or ""),
                    sec_user_id=item["sec_user_id"],
                    account_name=item["account_name"],
                    platform=item["platform"],
                    mark=item["mark"],
                    url=item["url"],
                    earliest=(
                        int(item["earliest"])
                        if str(item["earliest"]).isdigit()
                        else item["earliest"]
                    ),
                )
                for item in pending
            ]
            write_ttd_accounts(
                self.ttd_path / "Volume" / "settings.json",
                batch["platform"],
                planned_items,
            )
        except Exception as error:
            for item in pending:
                self.db.update_collection_batch_item(
                    item["id"], status="failed", message=str(error)
                )
            self._clear_active_batch()
            self._finalize(batch_id, "failed", -1, str(error))
            return "failed"

        self.db.update_collection_batch(
            batch_id, status="running", started_at=started_at
        )
        current_status = self.db.get_collection_batch(batch_id)["status"]
        if self._cancel_requested or self._closing or current_status == "cancelling":
            self._clear_active_batch()
            self._finalize(batch_id, "cancelled", -1, "批次已取消")
            return "cancelled"
        command = [
            sys.executable,
            str(self.runner_path),
            "--platform",
            batch["platform"],
        ]
        process = None
        try:
            process = await self._launch_process(command, self.ttd_path)
            self._active_process = process
            self.db.update_collection_batch(batch_id, process_pid=process.pid)
            log_path = Path(batch["log_path"])
            log_path.parent.mkdir(parents=True, exist_ok=True)

            with log_path.open("w", encoding="utf-8", errors="replace") as log_file:
                while True:
                    raw = await process.stdout.readline()
                    if not raw:
                        break
                    line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                    log_file.write(line + "\n")
                    log_file.flush()
                    marker = marker_line(line)
                    if marker:
                        self._apply_marker(batch_id, marker)

            return_code = await process.wait()
        except Exception as error:
            if process and process.returncode is None:
                process.terminate()
                await process.wait()
            self._clear_active_batch()
            self._finalize(batch_id, "failed", -1, str(error))
            return "failed"

        current_status = self.db.get_collection_batch(batch_id)["status"]
        if self._cancel_requested or current_status == "cancelling":
            final_status = "cancelled"
            message = "批次已取消"
        else:
            final_status = "completed" if return_code == 0 else "failed"
            message = "" if return_code == 0 else f"TTD 进程退出码: {return_code}"

        self._clear_active_batch()
        self._finalize(batch_id, final_status, return_code, message)
        return final_status

    def _clear_active_batch(self) -> None:
        self._active_process = None
        self._active_batch_id = None

    def _apply_marker(self, batch_id: str, marker: dict) -> bool:
        marker_type = marker.get("type")
        if marker_type not in ("account_start", "account_result"):
            return False
        sec_user_id = str(marker.get("sec_user_id") or "")
        item = self.db.find_collection_batch_item(batch_id, sec_user_id)
        if not item and marker.get("url"):
            item = next(
                (
                    current
                    for current in self.db.get_collection_batch_items(batch_id)
                    if current.get("url") == marker.get("url")
                ),
                None,
            )
        if not item:
            return False
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if marker_type == "account_start":
            self.db.update_collection_batch_item(
                item["id"], status="running", started_at=now
            )
            self.db.refresh_collection_batch_counts(batch_id)
            return True

        status = "success" if marker.get("status") == "success" else "failed"
        self.db.update_collection_batch_item(
            item["id"],
            status=status,
            message=str(marker.get("message") or ""),
            finished_at=now,
        )
        if status == "success" and item.get("account_record_id"):
            self.db.update_account(
                item["account_record_id"], {"last_collected_at": now[:10]}
            )
        self.db.refresh_collection_batch_counts(batch_id)
        return True

    def _finalize(
        self,
        batch_id: str,
        status: str,
        return_code: int,
        message: str = "",
    ) -> dict:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        terminal_status = "cancelled" if status == "cancelled" else "failed"
        with self.db._connect() as conn:
            conn.execute(
                """
                UPDATE collection_batch_items
                SET status = ?, message = ?, finished_at = ?
                WHERE batch_id = ? AND status IN ('pending', 'running')
                """,
                (terminal_status, message or "批次结束前未收到账号结果", now, batch_id),
            )
            conn.commit()
        self.db.update_collection_batch(batch_id, status=status, finished_at=now)
        return self.db.refresh_collection_batch_counts(batch_id)
