"""Manage persistent collection batches and one TTD terminal process at a time."""
from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from datetime import datetime, date
from pathlib import Path
from typing import Optional

import httpx


def _parse_date(value: str) -> date | None:
    """将 'YYYY-MM-DD' 字符串转为 date，空或无效返回 None。"""
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except (ValueError, TypeError):
        return None

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
        self._recovery_wait_timeout = 3.0

    async def start(
        self,
        accounts: list[dict],
        rating_min: int = 3,
        tags: list[str] | None = None,
        account_names: str = "",
        record_ids: list[str] | None = None,
        platforms: tuple[str, ...] = ("douyin",),
        mode: str = "incremental",
        preset_name: str = "",
        folder_name: str = "",
        name_format: str = "",
        account_created_after: str = "",
        skip_recent_days: int = 0,
        engine_params: dict | None = None,
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
                created_after=_parse_date(account_created_after),
                skip_recent_days=skip_recent_days,
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
                "folder_name": folder_name,
                "name_format": name_format,
                "account_created_after": account_created_after,
                "skip_recent_days": skip_recent_days,
                "engine_params": engine_params or {},
            }
            self.db.create_collection_batch(
                batch_id=batch_id,
                filter_json=json.dumps(snapshot, ensure_ascii=False),
                platform=platform,
                preset_name=preset_name,
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

    def read_log(self, batch_id: str, max_lines: int | None = None) -> list[str]:
        """读取批次日志(作品级全量 + 每行分类)。

        逐行解析:
        - 结构化标记行(account_start/account_result/summary)全部保留
        - 作品级行(【视频/图集/实况】下载结果)+ 全部普通终端行都保留，
          前端负责"动态保留最近 N 行"避免 DOM 膨胀
        - 输出格式: ["<level>\t<message>", ...]，前端按 \t 切分着色
        """
        batch = self.db.get_collection_batch(batch_id)
        if not batch or not batch.get("log_path"):
            return []
        path = Path(batch["log_path"])
        if not path.exists():
            return []
        with path.open("r", encoding="utf-8", errors="replace") as file:
            lines = [line.rstrip("\r\n") for line in file]
        all_lines = [
            self._classify_log_line(line)
            for line in lines
            if line.strip() and '"type": "work"' not in line and '"type":"work"' not in line
        ]
        if max_lines is not None and max_lines > 0:
            all_lines = all_lines[-max_lines:]
        return all_lines

    def read_account_works(self, batch_id: str) -> dict:
        """从批次日志聚合每个账号的作品下载明细。

        返回 { account_name: {"video": [...], "image": [...], "live": [...], "total": int} }
        只统计日志中 '【类型】...文件下载成功' 形式出现的作品标题。
        处理日志中文件名被换行折断的情况(合并到下一行直到出现'文件下载成功')。
        """
        batch = self.db.get_collection_batch(batch_id)
        if not batch or not batch.get("log_path"):
            return {}
        path = Path(batch["log_path"])
        if not path.exists():
            return {}
        stat = path.stat()
        cache = getattr(self, "_works_cache", None)
        if cache is None:
            cache = self._works_cache = {}
        sig = (stat.st_mtime_ns, stat.st_size)
        cached = cache.get(batch_id)
        if cached and cached[0] == sig:
            return cached[1]
        with path.open("r", encoding="utf-8", errors="replace") as file:
            lines = [line.rstrip("\r\n") for line in file]

        works: dict = {}
        current: str | None = None
        pending_title = ""  # 被换行折断的上一个标题残片
        for line in lines:
            if "__DOUKHUB__" in line:
                try:
                    import json as _json

                    payload_str = line[line.index("__DOUKHUB__") + len("__DOUKHUB__"):].strip()
                    payload = _json.loads(payload_str)
                    if payload.get("type") == "account_start":
                        current = str(payload.get("account_name") or "")
                        works.setdefault(current, {"video": [], "image": [], "live": [], "total": 0})
                    elif payload.get("type") == "account_result":
                        current = None
                except Exception:
                    pass
                pending_title = ""
                continue
            if not current:
                pending_title = ""
                continue
            stripped = line.strip()
            # 作品行起始: 【视频】/【图集】/【实况】
            if stripped.startswith("【"):
                pending_title = stripped
                if "文件下载成功" in pending_title:
                    title = pending_title
                    pending_title = ""
                else:
                    continue  # 等下一行补全
            elif pending_title and "文件下载成功" in stripped:
                title = pending_title + stripped
                pending_title = ""
            else:
                # 标题残片继续累积(可能中间还有描述文字)
                if pending_title:
                    pending_title += stripped
                continue

            kind = "video"
            if "图集" in title:
                kind = "image"
            elif "实况" in title:
                kind = "live"
            clean = title.replace("文件下载成功", "").replace("【视频】", "").replace("【图集】", "").replace("【实况】", "").strip()
            # 时间戳-类型-账号-标题 → 取标题(最后一段)
            parts = clean.split("-", 3)
            if len(parts) >= 4:
                clean = "-".join(parts[3:])
            works[current][kind].append(clean)
            works[current]["total"] += 1
        # 每个类型最多保留前 30 条
        for acc in works:
            for k in ("video", "image", "live"):
                works[acc][k] = works[acc][k][:30]
        cache[batch_id] = (sig, works)
        return works

    @staticmethod
    def _classify_log_line(line: str) -> str:
        """把一行日志转成 'level\tmessage' 格式；无法分类则原样返回。"""
        content = line
        level = "info"
        # 结构化标记行(来自 ttd_batch_runner 的 emit_marker)
        if "__DOUKHUB__" in line:
            try:
                import json as _json

                payload_str = line[line.index("__DOUKHUB__") + len("__DOUKHUB__"):].strip()
                payload = _json.loads(payload_str)
                if isinstance(payload, dict):
                    event = payload.get("type", "")
                    status = payload.get("status", "")
                    message = payload.get("message", "") or payload.get("account_name", "") or ""
                    if event == "account_start":
                        level = "info"
                        message = f"▶ 开始采集: {payload.get('account_name', '')} ({payload.get('index','')}/{payload.get('total','')})"
                    elif event == "account_result":
                        if status == "success":
                            level = "ok"
                            message = f"✔ {payload.get('account_name','')}: {message or '采集完成'}"
                        else:
                            level = "err"
                            message = f"✖ {payload.get('account_name','')}: {message or '失败'}"
                    elif event == "summary":
                        level = "info"
                        message = f"∑ 汇总: 总数 {payload.get('total','')}, 成功 {payload.get('success','')}, 失败 {payload.get('failed','')}"
                return f"{level}\t{message}"
            except Exception:
                pass
        # 作品级行(【视频/图集/实况】): 成功绿 / 失败中断红 / 其余蓝
        if content.strip().startswith(("【", "[", "#")):
            if "成功" in content:
                level = "ok"
            elif any(k in content for k in ("失败", "中断", "错误", "超时", "取消", "error", "fail", "timeout")):
                level = "err"
            else:
                level = "info"
            # 长作品文件名截断, 避免行太长
            if "文件下载" in content and len(content) > 60:
                content = content[:60] + "…"
            return f"{level}\t{content}"
        # 普通终端行: 按关键词着色
        text = content.lower()
        if any(k in text for k in ("成功", "完成", "succeed", "success", "done", "ok")):
            level = "ok"
        elif any(k in text for k in ("失败", "错误", "超时", "取消", "error", "fail", "timeout", "cancel")):
            level = "err"
        elif any(k in text for k in ("开始", "下载", "处理", "正在", "account", "账号")):
            level = "info"
        return f"{level}\t{content}"

    @staticmethod
    def _keep_summary_line(line: str) -> bool:
        """判断一行日志是否应进入主日志(账号级摘要)。

        规则:
        - __DOUKHUB__ 标记行: account/summary 保留，work 行不保留(作品级降噪)
        - 普通终端行: 只保留关键状态行(开始/完成/失败/汇总统计/参数/警告)，
          过滤掉"作品级"行(每个作品一行)避免刷屏
        """
        if "__DOUKHUB__" in line:
            if '"type": "work"' in line or '"type":"work"' in line:
                return False  # 作品级标记行不进主日志
            return True
        # 作品级行过滤: 单个文件下载/图集/视频条目/时间戳残片
        stripped = line.strip()
        if not stripped:
            return False
        if (
            stripped.startswith(("【", "[", "#", "*", "-"))
            or "文件下载成功" in line
            or "文件下载失败" in line
            or "下载中断" in line
            or "下载失败" in line
            or re.match(r"^\d{2}\.\d{2}\.\d{2}[-.]", stripped)   # 时间戳开头的作品条目残片
            or re.match(r"^\d{4}[-/]\d{1,2}[-/]\d{1,2}", stripped)  # 日期开头的残片
        ):
            return False
        # 关键状态行: 开始/完成/失败/统计/参数/警告/账号处理
        key = ["开始", "完成", "失败", "错误", "超时", "取消", "个账号", "个作品",
               "个视频", "个图集", "个实况", "下载", "跳过", "参数", "cookie", "cookie_",
               "警告", "账号", "私密", "登录", "提取", "筛选", "处理"]
        text = stripped.lower()
        return any(k in text for k in key)

    def _pick_cookie(self, platform: str) -> tuple[str, str]:
        """从数据库选取一个有效 Cookie，返回 (cookie, record_id)。

        优选策略：状态正常的候选中取「最久未使用」的一个，均衡轮换，
        避免单一 Cookie 被高频使用触发风控。
        """
        expected = platform
        cookies = self.db.get_enabled_cookies()
        candidates = [c for c in cookies if c.get("平台") == expected]
        if not candidates:
            candidates = [c for c in cookies if c.get("平台") == expected and c.get("启用")]
        if not candidates:
            return "", ""
        candidates.sort(key=lambda c: str(c.get("last_used_at") or ""))
        chosen = candidates[0]
        if chosen.get("record_id"):
            self.db.record_cookie_usage(chosen["record_id"])
        return str(chosen.get("Cookie", "")), str(chosen.get("record_id") or "")

    _LOGIN_FAIL_KEYWORDS = ("登录", "cookie", "Cookie", "验证", "403")

    def _note_account_failure(self, message: str) -> None:
        """批次内累计疑似登录态失效的账号失败，用于批次后闭环标记 Cookie。"""
        msg = str(message or "")
        if any(k in msg for k in self._LOGIN_FAIL_KEYWORDS):
            self._batch_login_fail += 1

    def _close_cookie_loop(self, batch_id: str, cookie_record_id: str) -> None:
        """批次结束：疑似登录态失败账号达到阈值时，把本次使用的 Cookie 标记失效。"""
        if not cookie_record_id or self._batch_login_fail < 3:
            return
        try:
            self.db.mark_cookie_invalid(cookie_record_id)
            batch = self.db.get_collection_batch(batch_id)
            log_path = (batch or {}).get("log_path")
            if log_path:
                with Path(log_path).open("a", encoding="utf-8", errors="replace") as f:
                    f.write(
                        f"[DoukHub] {self._batch_login_fail} 个账号报登录态相关错误，"
                        f"已自动将本次使用的 Cookie（{cookie_record_id}）标记为失效\n"
                    )
        except Exception:
            pass

    def recover_interrupted_batches(self) -> None:
        for batch in self.db.list_active_collection_batches():
            process_pid = batch.get("process_pid")
            if process_pid and self._verify_recorded_runner(process_pid):
                if not self._terminate_recorded_runner(process_pid):
                    raise RuntimeError(
                        f"recorded runner {process_pid} could not be terminated"
                    )
                if not self._wait_for_recorded_runner(
                    process_pid, self._recovery_wait_timeout
                ):
                    raise RuntimeError(
                        f"recorded runner {process_pid} did not exit "
                        f"within {self._recovery_wait_timeout} seconds"
                    )
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
        environment = os.environ.copy()
        environment["PYTHONIOENCODING"] = "utf-8"
        environment["PYTHONUTF8"] = "1"
        return await asyncio.create_subprocess_exec(
            *command,
            cwd=str(cwd),
            env=environment,
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

    def _wait_for_recorded_runner(self, process_pid: int, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while self._verify_recorded_runner(process_pid):
            if time.monotonic() >= deadline:
                return False
            time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
        return True

    async def _run_batch(self, batch_id: str) -> str:
        self._active_batch_id = batch_id
        self._cancel_requested = False
        self._batch_login_fail = 0
        self._batch_cookie_rid = ""
        batch = self.db.get_collection_batch(batch_id)
        items = self.db.get_collection_batch_items(batch_id)
        # 从 filter_json 中解析采集设置
        try:
            filter_data = json.loads(batch.get("filter_json") or "{}")
        except (json.JSONDecodeError, TypeError):
            filter_data = {}
        folder_name = filter_data.get("folder_name", "")
        name_format = filter_data.get("name_format", "")
        engine_params = filter_data.get("engine_params", {})
        # 防御性回退：正常情况下 main.py 已注入全局默认值
        if not folder_name:
            folder_name = "Download"
        if not name_format:
            name_format = "create_time type nickname desc"
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
            cookie_str, cookie_rid = self._pick_cookie(batch["platform"])
            self._batch_cookie_rid = cookie_rid
            write_ttd_accounts(
                self.ttd_path / "Volume" / "settings.json",
                batch["platform"],
                planned_items,
                folder_name=folder_name,
                name_format=name_format,
                cookie=cookie_str,
                engine_params=engine_params,
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
            current_status = self.db.get_collection_batch(batch_id)["status"]
            if (
                self._cancel_requested
                or self._closing
                or current_status == "cancelling"
            ):
                if process.returncode is None:
                    process.terminate()
                    await process.wait()
                return_code = (
                    process.returncode if process.returncode is not None else -1
                )
                self._clear_active_batch()
                self._finalize(batch_id, "cancelled", return_code, "批次已取消")
                return "cancelled"
            log_path = Path(batch["log_path"])
            log_path.parent.mkdir(parents=True, exist_ok=True)

            with log_path.open("w", encoding="utf-8", errors="replace") as log_file:
                last_output = time.monotonic()

                async def _watchdog() -> None:
                    """引擎心跳看门狗：长时间无输出时写入警告日志（不杀进程）。"""
                    while process.returncode is None:
                        await asyncio.sleep(60)
                        silent = time.monotonic() - last_output
                        if silent > 300:
                            log_file.write(
                                f"[DoukHub watchdog] 引擎已 {int(silent)} 秒无输出，"
                                "请检查 TTD 进程是否卡住\n"
                            )
                            log_file.flush()

                watchdog_task = asyncio.create_task(_watchdog())
                try:
                    while True:
                        raw = await process.stdout.readline()
                        if not raw:
                            break
                        last_output = time.monotonic()
                        line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                        log_file.write(line + "\n")
                        log_file.flush()
                        marker = marker_line(line)
                        if marker:
                            self._apply_marker(batch_id, marker)
                finally:
                    watchdog_task.cancel()

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

        self._close_cookie_loop(batch_id, self._batch_cookie_rid)

        # 失败账号自动重试一轮（仅一次）：网络抖动/瞬时风控当场补上，不留给下一轮
        if (
            final_status == "completed"
            and not filter_data.get("retried")
            and not self._cancel_requested
        ):
            failed_items = [
                it for it in self.db.get_collection_batch_items(batch_id)
                if it["status"] == "failed"
            ]
            if failed_items:
                filter_data["retried"] = True
                self.db.update_collection_batch(
                    batch_id,
                    filter_json=json.dumps(filter_data, ensure_ascii=False),
                )
                for it in failed_items:
                    self.db.update_collection_batch_item(
                        it["id"], status="pending", message="首轮失败，自动重试中"
                    )
                self._clear_active_batch()
                await asyncio.sleep(20)  # 降温间隔，降低连续请求触发风控的概率
                return await self._run_batch(batch_id)

        self._clear_active_batch()
        self._finalize(batch_id, final_status, return_code, message)
        return final_status

    def _clear_active_batch(self) -> None:
        self._active_process = None
        self._active_batch_id = None

    def _apply_marker(self, batch_id: str, marker: dict) -> bool:
        marker_type = marker.get("type")
        if marker_type == "work":
            return self._apply_work_marker(batch_id, marker)
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
        batch = self.db.get_collection_batch(batch_id)
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if marker_type == "account_start":
            self.db.update_collection_batch_item(
                item["id"], status="running", started_at=now
            )
            self.db.refresh_collection_batch_counts(batch_id)
            return True

        status = "success" if marker.get("status") == "success" else "failed"
        message = str(marker.get("message") or "")
        if status != "success":
            self._note_account_failure(message)
        self.db.update_collection_batch_item(
            item["id"],
            status=status,
            message=message,
            finished_at=now,
        )
        batch_start_date = str((batch or {}).get("started_at") or "")[:10]
        if status == "success" and item.get("account_record_id") and batch_start_date:
            self.db.update_account(
                item["account_record_id"], {"last_collected_at": batch_start_date}
            )
        self.db.refresh_collection_batch_counts(batch_id)
        return True

    @staticmethod
    def _clean_work_title(show: str) -> tuple[str, str]:
        """从 TTD 下载显示名解析 (kind, title)。

        show 形如 '【视频】2026-08-30-账号名-标题'，与日志解析规则一致：
        去掉类型前缀后按前 3 个 '-' 切分，取剩余部分为标题。
        """
        show = (show or "").strip()
        kind = "video"
        if "【图集】" in show or "图集" in show[:6]:
            kind = "image"
        elif "【实况】" in show or "实况" in show[:6]:
            kind = "live"
        clean = (
            show.replace("【视频】", "")
            .replace("【图集】", "")
            .replace("【实况】", "")
            .strip()
        )
        parts = clean.split("-", 3)
        if len(parts) >= 4:
            clean = "-".join(parts[3:])
        return kind, clean

    @staticmethod
    def _build_work_url(platform: str, kind: str, aweme_id: str) -> str:
        if not aweme_id:
            return ""
        if platform == "douyin":
            if kind == "image":
                return f"https://www.douyin.com/note/{aweme_id}"
            return f"https://www.douyin.com/video/{aweme_id}"
        return ""

    def _apply_work_marker(self, batch_id: str, marker: dict) -> bool:
        """把 TTD 下载挂钩上报的作品事件实时写入 collection_works。"""
        aweme_id = str(marker.get("aweme_id") or "")
        if not aweme_id:
            return False
        batch = self.db.get_collection_batch(batch_id)
        platform = str((batch or {}).get("platform") or "")
        kind, title = self._clean_work_title(str(marker.get("show") or ""))
        status = "success" if marker.get("status") == "success" else "failed"
        file_path = str(marker.get("file_path") or "")
        from pathlib import Path as _Path

        fname = ""
        fdir = ""
        if file_path:
            pp = _Path(file_path)
            fname = pp.name
            fdir = str(pp.parent)
        try:
            self.db.upsert_collection_work(
                batch_id=batch_id,
                sec_user_id=str(marker.get("sec_user_id") or ""),
                account_name=str(marker.get("account_name") or ""),
                platform=platform,
                aweme_id=aweme_id,
                title=title,
                kind=kind,
                work_url=self._build_work_url(platform, kind, aweme_id),
                file_name=fname,
                download_dir=fdir,
                file_path=file_path,
                status=status,
                message="下载失败" if status == "failed" else "",
            )
        except Exception:
            return False
        return True

    def read_batch_works_from_db(self, batch_id: str) -> list[dict]:
        """读取某批次已入库的作品明细（新批次数据源）。"""
        return self.db.list_batch_works(batch_id)

    @staticmethod
    def aggregate_db_works(works: list[dict]) -> dict:
        """把库中的作品明细聚合成旧日志解析的兼容格式。

        返回 { account_name: {"video": [...], "image": [...], "live": [...], "total": int} }
        仅统计成功作品，与日志解析口径一致。
        """
        agg: dict = {}
        for w in works:
            if w.get("status") != "success":
                continue
            name = w.get("account_name") or w.get("sec_user_id") or "未知账号"
            entry = agg.setdefault(
                name, {"video": [], "image": [], "live": [], "total": 0}
            )
            kind = w.get("kind") or "video"
            if kind not in ("video", "image", "live"):
                kind = "video"
            entry[kind].append(w.get("title") or w.get("file_name") or w.get("aweme_id") or "")
            entry["total"] += 1
        return agg

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
