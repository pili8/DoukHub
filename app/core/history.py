"""采集历史记录管理（SQLite）"""
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any


class HistoryDB:
    """采集历史数据库"""

    def __init__(self, data_dir: Path):
        self.db_path = data_dir / "history.db"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        """Create one SQLite connection with the project's standard pragmas."""
        conn = sqlite3.connect(str(self.db_path), timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _init_db(self):
        """初始化数据库表"""
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS collection_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_name TEXT,
                    platform TEXT,
                    sec_user_id TEXT,
                    collection_type TEXT,
                    works_count INTEGER DEFAULT 0,
                    success_count INTEGER DEFAULT 0,
                    fail_count INTEGER DEFAULT 0,
                    started_at DATETIME,
                    finished_at DATETIME,
                    duration_seconds REAL DEFAULT 0,
                    status TEXT DEFAULT 'pending',
                    error_message TEXT DEFAULT ''
                )
            """)
            # 旧版 scheduled_tasks 表已废弃，功能移至主库 doukhub.db
            # 如存在则删除（安全清理，不影响已有数据）
            conn.execute("DROP TABLE IF EXISTS scheduled_tasks")
            conn.commit()

    def add_record(self, data: dict) -> int:
        """添加采集记录"""
        with self._connect() as conn:
            cursor = conn.execute(
                """INSERT INTO collection_history
                   (account_name, platform, sec_user_id, collection_type,
                    works_count, success_count, fail_count,
                    started_at, finished_at, duration_seconds, status, error_message)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    data.get("account_name", ""),
                    data.get("platform", ""),
                    data.get("sec_user_id", ""),
                    data.get("collection_type", ""),
                    data.get("works_count", 0),
                    data.get("success_count", 0),
                    data.get("fail_count", 0),
                    data.get("started_at", ""),
                    data.get("finished_at", ""),
                    data.get("duration_seconds", 0),
                    data.get("status", "pending"),
                    data.get("error_message", ""),
                ),
            )
            conn.commit()
            return cursor.lastrowid or 0

    def get_records(
        self,
        limit: int = 100,
        offset: int = 0,
        status: str = "",
    ) -> list[dict]:
        """获取采集记录"""
        query = "SELECT * FROM collection_history"
        params: list[Any] = []
        if status:
            query += " WHERE status = ?"
            params.append(status)
        query += " ORDER BY id DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
            return [dict(r) for r in rows]

    def get_stats(self) -> dict:
        """获取统计信息"""
        with self._connect() as conn:
            total = conn.execute("SELECT COUNT(*) FROM collection_history").fetchone()[0]
            today = datetime.now().strftime("%Y-%m-%d")
            today_count = conn.execute(
                "SELECT COUNT(*) FROM collection_history WHERE started_at LIKE ?",
                (f"{today}%",),
            ).fetchone()[0]
            success = conn.execute(
                "SELECT COUNT(*) FROM collection_history WHERE status = 'success'"
            ).fetchone()[0]
            failed = conn.execute(
                "SELECT COUNT(*) FROM collection_history WHERE status = 'failed'"
            ).fetchone()[0]
            return {
                "total": total,
                "today": today_count,
                "success": success,
                "failed": failed,
            }
