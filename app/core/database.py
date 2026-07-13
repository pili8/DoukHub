"""DoukHub 本地数据库管理"""
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

# 数据库文件路径
DB_PATH = Path.home() / ".doukhub" / "doukhub.db"


class Database:
    """本地 SQLite 数据库管理"""

    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = db_path or DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_database()

    def _init_database(self):
        """初始化数据库表结构"""
        with self._connect() as conn:
            # 表1：采集表缓存
            conn.execute("""
                CREATE TABLE IF NOT EXISTS collection_cache (
                    记录ID TEXT PRIMARY KEY,
                    分享码 TEXT UNIQUE NOT NULL,
                    平台 TEXT,
                    等级 INTEGER,
                    标签 TEXT,
                    账号标识 TEXT,
                    已同步 BOOLEAN DEFAULT 0,
                    同步错误 TEXT,
                    备注 TEXT,
                    昵称 TEXT,
                    粉丝数 INTEGER,
                    作品数 INTEGER,
                    创建时间 DATETIME DEFAULT CURRENT_TIMESTAMP,
                    更新时间 DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_collection_share ON collection_cache(分享码)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_collection_sec_user_id ON collection_cache(账号标识)")

            # 表2：账号表缓存
            conn.execute("""
                CREATE TABLE IF NOT EXISTS account_cache (
                    记录ID TEXT PRIMARY KEY,
                    账号名称 TEXT,
                    平台 TEXT,
                    链接 TEXT,
                    账号标识 TEXT UNIQUE NOT NULL,
                    等级 INTEGER,
                    标签 TEXT,
                    昵称 TEXT,
                    粉丝数 INTEGER,
                    作品数 INTEGER,
                    签名 TEXT,
                    头像 TEXT,
                    已更新 BOOLEAN DEFAULT 0,
                    更新错误 TEXT,
                    创建时间 DATETIME DEFAULT CURRENT_TIMESTAMP,
                    更新时间 DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_account_sec_user_id ON account_cache(账号标识)")

            # 表3：Cookie表缓存
            conn.execute("""
                CREATE TABLE IF NOT EXISTS cookie_cache (
                    记录ID TEXT PRIMARY KEY,
                    Cookie TEXT NOT NULL,
                    平台 TEXT,
                    状态 TEXT DEFAULT '正常',
                    启用 BOOLEAN DEFAULT 1,
                    备注 TEXT,
                    验证时间 DATETIME,
                    创建时间 DATETIME DEFAULT CURRENT_TIMESTAMP,
                    更新时间 DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # 表4：采集历史
            conn.execute("""
                CREATE TABLE IF NOT EXISTS collection_history (
                    ID INTEGER PRIMARY KEY AUTOINCREMENT,
                    账号名称 TEXT,
                    平台 TEXT,
                    账号标识 TEXT,
                    采集类型 TEXT,
                    等级 INTEGER,
                    标签 TEXT,
                    状态 TEXT,
                    作品数 INTEGER,
                    开始时间 DATETIME,
                    结束时间 DATETIME,
                    耗时秒数 REAL,
                    错误信息 TEXT,
                    创建时间 DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_history_sec_user_id ON collection_history(账号标识)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_history_created_at ON collection_history(创建时间)")

            # 表5：定时任务
            conn.execute("""
                CREATE TABLE IF NOT EXISTS scheduled_tasks (
                    ID INTEGER PRIMARY KEY AUTOINCREMENT,
                    任务名称 TEXT NOT NULL,
                    Cron表达式 TEXT NOT NULL,
                    等级筛选 TEXT,
                    启用 BOOLEAN DEFAULT 1,
                    上次运行 DATETIME,
                    下次运行 DATETIME,
                    创建时间 DATETIME DEFAULT CURRENT_TIMESTAMP,
                    更新时间 DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)

    def _connect(self) -> sqlite3.Connection:
        """创建数据库连接"""
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    # ========== 采集表缓存操作 ==========

    def get_all_collections(self) -> list[dict]:
        """获取所有采集表记录"""
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM collection_cache ORDER BY 创建时间 DESC").fetchall()
            return [dict(row) for row in rows]

    def get_collection_by_share(self, share: str) -> Optional[dict]:
        """根据分享码获取记录"""
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM collection_cache WHERE 分享码 = ?", (share,)).fetchone()
            return dict(row) if row else None

    def get_collection_by_id(self, record_id: str) -> Optional[dict]:
        """根据记录ID获取采集表记录"""
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM collection_cache WHERE 记录ID = ?", (record_id,)).fetchone()
            return dict(row) if row else None

    def get_collection_by_sec_user_id(self, sec_user_id: str) -> Optional[dict]:
        """根据账号标识获取记录"""
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM collection_cache WHERE 账号标识 = ?", (sec_user_id,)).fetchone()
            return dict(row) if row else None

    def insert_collection(self, data: dict) -> bool:
        """插入采集表记录。UNIQUE 冲突会抛出 sqlite3.IntegrityError，调用方按需捕获。"""
        with self._connect() as conn:
            fields = ", ".join(data.keys())
            placeholders = ", ".join(["?" for _ in data])
            conn.execute(f"INSERT INTO collection_cache ({fields}) VALUES ({placeholders})", list(data.values()))
            conn.commit()
            return True

    def update_collection(self, record_id: str, data: dict) -> bool:
        """更新采集表记录"""
        with self._connect() as conn:
            if "更新时间" not in data:
                data["更新时间"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            set_clause = ", ".join([f"{k} = ?" for k in data.keys()])
            conn.execute(f"UPDATE collection_cache SET {set_clause} WHERE 记录ID = ?", list(data.values()) + [record_id])
            conn.commit()
            return True

    def delete_collection(self, record_id: str) -> bool:
        """删除采集表记录"""
        with self._connect() as conn:
            conn.execute("DELETE FROM collection_cache WHERE 记录ID = ?", (record_id,))
            conn.commit()
            return True

    def clear_collection_cache(self) -> bool:
        """清空采集表缓存"""
        with self._connect() as conn:
            conn.execute("DELETE FROM collection_cache")
            conn.commit()
            return True

    # ========== 账号表缓存操作 ==========

    def get_all_accounts(self) -> list[dict]:
        """获取所有账号表记录"""
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM account_cache ORDER BY 创建时间 DESC").fetchall()
            return [dict(row) for row in rows]

    def get_account_by_sec_user_id(self, sec_user_id: str) -> Optional[dict]:
        """根据账号标识获取记录"""
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM account_cache WHERE 账号标识 = ?", (sec_user_id,)).fetchone()
            return dict(row) if row else None

    def get_account_by_id(self, record_id: str) -> Optional[dict]:
        """根据记录ID获取账号表记录"""
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM account_cache WHERE 记录ID = ?", (record_id,)).fetchone()
            return dict(row) if row else None

    def insert_account(self, data: dict) -> bool:
        """插入账号表记录。UNIQUE 冲突会抛出 sqlite3.IntegrityError，调用方按需捕获。"""
        with self._connect() as conn:
            fields = ", ".join(data.keys())
            placeholders = ", ".join(["?" for _ in data])
            conn.execute(f"INSERT INTO account_cache ({fields}) VALUES ({placeholders})", list(data.values()))
            conn.commit()
            return True

    def update_account(self, record_id: str, data: dict) -> bool:
        """更新账号表记录"""
        with self._connect() as conn:
            if "更新时间" not in data:
                data["更新时间"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            set_clause = ", ".join([f"{k} = ?" for k in data.keys()])
            conn.execute(f"UPDATE account_cache SET {set_clause} WHERE 记录ID = ?", list(data.values()) + [record_id])
            conn.commit()
            return True

    def delete_account(self, record_id: str) -> bool:
        """删除账号表记录"""
        with self._connect() as conn:
            conn.execute("DELETE FROM account_cache WHERE 记录ID = ?", (record_id,))
            conn.commit()
            return True

    def clear_account_cache(self) -> bool:
        """清空账号表缓存"""
        with self._connect() as conn:
            conn.execute("DELETE FROM account_cache")
            conn.commit()
            return True

    # ========== Cookie表缓存操作 ==========

    def get_all_cookies(self) -> list[dict]:
        """获取所有 Cookie 记录"""
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM cookie_cache ORDER BY 创建时间 DESC").fetchall()
            return [dict(row) for row in rows]

    def get_enabled_cookies(self) -> list[dict]:
        """获取启用的 Cookie"""
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM cookie_cache WHERE 启用 = 1 AND 状态 = '正常'").fetchall()
            return [dict(row) for row in rows]

    def insert_cookie(self, data: dict) -> bool:
        """插入 Cookie 记录。UNIQUE 冲突会抛出 sqlite3.IntegrityError，调用方按需捕获。"""
        with self._connect() as conn:
            fields = ", ".join(data.keys())
            placeholders = ", ".join(["?" for _ in data])
            conn.execute(f"INSERT INTO cookie_cache ({fields}) VALUES ({placeholders})", list(data.values()))
            conn.commit()
            return True

    def update_cookie(self, record_id: str, data: dict) -> bool:
        """更新 Cookie 记录"""
        with self._connect() as conn:
            if "更新时间" not in data:
                data["更新时间"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            set_clause = ", ".join([f"{k} = ?" for k in data.keys()])
            conn.execute(f"UPDATE cookie_cache SET {set_clause} WHERE 记录ID = ?", list(data.values()) + [record_id])
            conn.commit()
            return True

    def mark_cookie_invalid(self, record_id: str) -> bool:
        """标记 Cookie 为失效"""
        return self.update_cookie(record_id, {"状态": "失效"})

    def get_cookie_by_id(self, record_id: str) -> Optional[dict]:
        """根据记录ID获取 Cookie 记录"""
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM cookie_cache WHERE 记录ID = ?", (record_id,)).fetchone()
            return dict(row) if row else None

    def delete_cookie(self, record_id: str) -> bool:
        """删除 Cookie 记录"""
        with self._connect() as conn:
            conn.execute("DELETE FROM cookie_cache WHERE 记录ID = ?", (record_id,))
            conn.commit()
            return True

    # ========== 采集历史操作 ==========

    def add_history(self, data: dict) -> int:
        """添加采集历史记录"""
        with self._connect() as conn:
            fields = ", ".join(data.keys())
            placeholders = ", ".join(["?" for _ in data])
            cursor = conn.execute(f"INSERT INTO collection_history ({fields}) VALUES ({placeholders})", list(data.values()))
            conn.commit()
            return cursor.lastrowid

    def get_history(self, limit: int = 100, offset: int = 0) -> list[dict]:
        """获取采集历史记录"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM collection_history ORDER BY 创建时间 DESC LIMIT ? OFFSET ?",
                (limit, offset)
            ).fetchall()
            return [dict(row) for row in rows]

    def get_history_count(self) -> int:
        """获取采集历史记录总数"""
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) as count FROM collection_history").fetchone()
            return row["count"] if row else 0

    # ========== 定时任务操作 ==========

    def get_all_tasks(self) -> list[dict]:
        """获取所有定时任务"""
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM scheduled_tasks ORDER BY ID").fetchall()
            return [dict(row) for row in rows]

    def add_task(self, data: dict) -> int:
        """添加定时任务"""
        with self._connect() as conn:
            fields = ", ".join(data.keys())
            placeholders = ", ".join(["?" for _ in data])
            cursor = conn.execute(f"INSERT INTO scheduled_tasks ({fields}) VALUES ({placeholders})", list(data.values()))
            conn.commit()
            return cursor.lastrowid

    def update_task(self, task_id: int, data: dict) -> bool:
        """更新定时任务"""
        with self._connect() as conn:
            data["更新时间"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            set_clause = ", ".join([f"{k} = ?" for k in data.keys()])
            conn.execute(f"UPDATE scheduled_tasks SET {set_clause} WHERE ID = ?", list(data.values()) + [task_id])
            conn.commit()
            return True

    def delete_task(self, task_id: int) -> bool:
        """删除定时任务"""
        with self._connect() as conn:
            conn.execute("DELETE FROM scheduled_tasks WHERE ID = ?", (task_id,))
            conn.commit()
            return True

    # ========== 统计和查询 ==========

    def get_table_counts(self) -> dict:
        """获取各表记录数"""
        tables = [
            "collection_cache",
            "account_cache",
            "cookie_cache",
            "collection_history",
            "scheduled_tasks"
        ]
        counts = {}
        with self._connect() as conn:
            for table in tables:
                row = conn.execute(f"SELECT COUNT(*) as count FROM {table}").fetchone()
                counts[table] = row["count"] if row else 0
        return counts

    def search_table(self, table: str, keyword: str, field: Optional[str] = None) -> list[dict]:
        """搜索表记录"""
        with self._connect() as conn:
            if field:
                rows = conn.execute(
                    f"SELECT * FROM {table} WHERE {field} LIKE ?",
                    (f"%{keyword}%",)
                ).fetchall()
            else:
                # 搜索所有字段
                columns = [col[1] for col in conn.execute(f"PRAGMA table_info({table})").fetchall()]
                conditions = " OR ".join([f"{col} LIKE ?" for col in columns])
                params = [f"%{keyword}%" for _ in columns]
                rows = conn.execute(f"SELECT * FROM {table} WHERE {conditions}", params).fetchall()
            return [dict(row) for row in rows]
