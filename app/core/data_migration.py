"""Copy small application data, verify it, then switch the bootstrap pointer."""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import threading
from pathlib import Path
from typing import Any

from .data_root import BOOTSTRAP_PATH, validate_target, write_bootstrap


def copy_sqlite(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target.unlink()
    source_conn = sqlite3.connect(str(source), timeout=30.0)
    target_conn = sqlite3.connect(str(target), timeout=30.0)
    try:
        # backup() API 自带一致性快照；显式 BEGIN IMMEDIATE 在本环境会与
        # backup 锁互斥导致永久挂起，故不显式开启事务。
        source_conn.backup(target_conn)
    finally:
        source_conn.close()
        target_conn.close()


class DataMigration:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state: dict[str, Any] = self._initial_state()

    @staticmethod
    def _initial_state() -> dict[str, Any]:
        return {
            "status": "idle",
            "locked": False,
            "progress": 0,
            "current": "",
            "target": "",
            "source_root": "",
            "error": "",
            "result": None,
        }

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._state)

    def prepare(self, raw: str, current: Path) -> dict:
        check = validate_target(raw)
        with self._lock:
            self._state = self._initial_state()
            self._state["target"] = check.get("target", "")
            self._state["source_root"] = str(current)
            if not check.get("valid"):
                self._state["status"] = "failed"
                self._state["error"] = check.get("message", "目录不可用")
                return check
            self._state["status"] = "preparing"
            self._state["locked"] = True
            return check

    def fail(self, message: str) -> None:
        with self._lock:
            self._state["status"] = "failed"
            self._state["locked"] = False
            self._state["error"] = message

    def run(self, source_root: Path, source_db: Path) -> dict:
        state = self.snapshot()
        target = Path(state["target"])
        created = [
            target / "doukhub.db",
            target / "history.db",
            target / "backups",
            target / "collection_logs",
        ]
        try:
            with self._lock:
                self._state.update(status="copying", progress=5, current="复制主数据库")

            copy_sqlite(source_db, target / "doukhub.db")
            self._advance(30, "复制历史数据库")
            history = source_root / "history.db"
            if history.exists():
                copy_sqlite(history, target / "history.db")

            self._advance(55, "复制备份")
            self._copy_tree(source_root / "backups", target / "backups")
            self._advance(75, "复制采集批次日志")
            self._copy_tree(
                source_root / "collection_logs", target / "collection_logs"
            )

            with self._lock:
                self._state.update(status="verifying", progress=88, current="完整性校验")
            for db_path in (target / "doukhub.db", target / "history.db"):
                if not db_path.exists():
                    continue
                conn = sqlite3.connect(str(db_path))
                try:
                    check = conn.execute("PRAGMA integrity_check").fetchone()[0]
                    if check != "ok":
                        raise RuntimeError(f"数据库校验失败：{db_path.name}")
                finally:
                    conn.close()

            with self._lock:
                self._state.update(progress=96, current="写入迁移清单")
            marker = {
                "version": 1,
                "source_root": str(source_root),
                "items": [
                    "doukhub.db", "history.db", "backups", "collection_logs"
                ],
            }
            (target / ".doukhub-migration.json").write_text(
                json.dumps(marker, ensure_ascii=False, indent=2), encoding="utf-8"
            )

            # 引导文件最后切换：前面任何一步失败时，旧引导仍指向原目录，回滚安全
            with self._lock:
                self._state.update(progress=98, current="切换引导文件")
            write_bootstrap(target)

            with self._lock:
                self._state.update(
                    status="ready",
                    locked=True,
                    progress=100,
                    current="等待重启",
                    error="",
                    result={"success": True, "target": str(target)},
                )
            return {"success": True, "target": str(target)}
        except Exception as exc:
            try:
                BOOTSTRAP_PATH.with_name(BOOTSTRAP_PATH.name + ".tmp").unlink()
            except OSError:
                pass
            for path in created:
                try:
                    if path.is_dir():
                        shutil.rmtree(path)
                    elif path.exists():
                        path.unlink()
                except OSError:
                    pass
            self.fail(str(exc))
            return {"success": False, "error": str(exc)}

    def _copy_tree(self, source: Path, target: Path) -> None:
        if not source.exists():
            return
        shutil.copytree(source, target, dirs_exist_ok=False)

    def _advance(self, progress: int, label: str) -> None:
        with self._lock:
            self._state.update(progress=progress, current=label)
