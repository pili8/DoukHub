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
                    sec_user_id TEXT,
                    已同步 BOOLEAN DEFAULT 0,
                    同步错误 TEXT,
                    备注 TEXT,
                    昵称 TEXT,
                    粉丝数 INTEGER,
                    作品数 INTEGER,
                    创建时间 DATETIME DEFAULT CURRENT_TIMESTAMP,
                    同步时间 DATETIME DEFAULT CURRENT_TIMESTAMP,
                    is_deleted BOOLEAN DEFAULT 0,
                    deleted_at DATETIME
                )
            """)

            # 表2：账号表缓存（字段名对齐飞书）
            conn.execute("""
                CREATE TABLE IF NOT EXISTS account_cache (
                    记录ID TEXT PRIMARY KEY,
                    账号名称 TEXT,
                    平台 TEXT,
                    链接 TEXT,
                    sec_user_id TEXT UNIQUE NOT NULL,
                    等级 INTEGER,
                    标签 TEXT,
                    昵称 TEXT,
                    粉丝数 INTEGER,
                    作品数 INTEGER,
                    签名 TEXT,
                    头像 TEXT,
                    已获取信息 BOOLEAN DEFAULT 0,
                    备注 TEXT,
                    启用 BOOLEAN DEFAULT 1,
                    采集类型 TEXT DEFAULT '发布',
                    创建时间 DATETIME DEFAULT CURRENT_TIMESTAMP,
                    同步时间 DATETIME DEFAULT CURRENT_TIMESTAMP,
                    is_deleted BOOLEAN DEFAULT 0,
                    deleted_at DATETIME
                )
            """)

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
                    同步时间 DATETIME DEFAULT CURRENT_TIMESTAMP,
                    is_deleted BOOLEAN DEFAULT 0,
                    deleted_at DATETIME
                )
            """)

            # 表4：采集历史（这表的字段不进飞书，但 sec_user_id 与飞书一致）
            conn.execute("""
                CREATE TABLE IF NOT EXISTS collection_history (
                    ID INTEGER PRIMARY KEY AUTOINCREMENT,
                    账号名称 TEXT,
                    平台 TEXT,
                    sec_user_id TEXT,
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
                    同步时间 DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # 兼容旧库：自动迁移字段名（账号标识→sec_user_id 等）
            # 必须在 CREATE INDEX 之前执行，因为索引依赖字段名
            self._migrate_legacy_columns(conn)

            # 创建索引（依赖字段名，必须在迁移之后）
            conn.execute("CREATE INDEX IF NOT EXISTS idx_collection_share ON collection_cache(分享码)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_collection_sec_user_id ON collection_cache(sec_user_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_account_sec_user_id ON account_cache(sec_user_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_history_sec_user_id ON collection_history(sec_user_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_history_created_at ON collection_history(创建时间)")

    def _migrate_legacy_columns(self, conn):
        """迁移旧库字段名，使其对齐飞书。
        - account_cache: 账号标识→sec_user_id, 更新错误→备注, 更新时间→同步时间, 已更新→已获取信息
        - collection_cache: 账号标识→sec_user_id, 更新时间→同步时间
        - collection_history: 账号标识→sec_user_id
        - 其他表: 更新时间→同步时间
        - 删除 account_cache.代理（如存在）
        """
        rename_map = {
            "collection_cache": [
                ("账号标识", "sec_user_id"),
                ("更新时间", "同步时间"),
            ],
            "account_cache": [
                ("账号标识", "sec_user_id"),
                ("更新错误", "备注"),
                ("更新时间", "同步时间"),
                ("已更新", "已获取信息"),
            ],
            "collection_history": [
                ("账号标识", "sec_user_id"),
            ],
            "cookie_cache": [
                ("更新时间", "同步时间"),
            ],
            "scheduled_tasks": [
                ("更新时间", "同步时间"),
            ],
        }
        # 新增字段（旧库可能缺）
        add_columns = {
            "account_cache": [
                ("启用", "BOOLEAN DEFAULT 1"),
                ("采集类型", "TEXT DEFAULT '发布'"),
            ],
        }
        # 软删除字段（墓碑）：三张同步表都加上
        for _tbl in ("collection_cache", "account_cache", "cookie_cache"):
            add_columns.setdefault(_tbl, []).extend([
                ("is_deleted", "BOOLEAN DEFAULT 0"),
                ("deleted_at", "DATETIME"),
            ])
        # 同步标记：记录是否已确认存在于飞书（区分"本地新建未推送"和"飞书已删除"）
        for _tbl in ("collection_cache", "account_cache", "cookie_cache"):
            add_columns.setdefault(_tbl, []).append(
                ("synced", "BOOLEAN DEFAULT 0"),
            )
        # 最后更新时间：用于增量同步检测数据变化
        for _tbl in ("collection_cache", "account_cache", "cookie_cache"):
            add_columns.setdefault(_tbl, []).append(
                ("最后更新时间", "DATETIME"),
            )
        for table, renames in rename_map.items():
            cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
            for old, new in renames:
                if old in cols and new not in cols:
                    conn.execute(f'ALTER TABLE {table} RENAME COLUMN "{old}" TO "{new}"')
                elif old in cols and new in cols:
                    # 两个都存在（异常情况），保留新字段
                    pass
        for table, additions in add_columns.items():
            cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
            for col, ddl in additions:
                if col not in cols:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
                    # synced 字段首次添加时，把现有记录全部标记为已同步
                    if col == "synced":
                        conn.execute(f"UPDATE {table} SET synced = 1")

    def _connect(self) -> sqlite3.Connection:
        """创建数据库连接"""
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    # ========== 采集表缓存操作 ==========

    def get_all_collections(self) -> list[dict]:
        """获取所有采集表记录"""
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM collection_cache WHERE is_deleted = 0 ORDER BY 创建时间 DESC").fetchall()
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
        """根据 sec_user_id 获取记录"""
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM collection_cache WHERE sec_user_id = ?", (sec_user_id,)).fetchone()
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
            # 自动更新「最后更新时间」字段
            if "最后更新时间" not in data:
                data["最后更新时间"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            set_clause = ", ".join([f"{k} = ?" for k in data.keys()])
            conn.execute(f"UPDATE collection_cache SET {set_clause} WHERE 记录ID = ?", list(data.values()) + [record_id])
            conn.commit()
            return True

    def delete_collection(self, record_id: str) -> bool:
        """删除采集表记录"""
        with self._connect() as conn:
            conn.execute(
                "UPDATE collection_cache SET is_deleted = 1, deleted_at = ? WHERE 记录ID = ?",
                (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), record_id),
            )
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
            rows = conn.execute("SELECT * FROM account_cache WHERE is_deleted = 0 ORDER BY 创建时间 DESC").fetchall()
            return [dict(row) for row in rows]

    def get_account_by_sec_user_id(self, sec_user_id: str) -> Optional[dict]:
        """根据 sec_user_id 获取记录"""
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM account_cache WHERE sec_user_id = ?", (sec_user_id,)).fetchone()
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
            # 自动更新「最后更新时间」字段
            if "最后更新时间" not in data:
                data["最后更新时间"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            set_clause = ", ".join([f"{k} = ?" for k in data.keys()])
            conn.execute(f"UPDATE account_cache SET {set_clause} WHERE 记录ID = ?", list(data.values()) + [record_id])
            conn.commit()
            return True

    def delete_account(self, record_id: str) -> bool:
        """删除账号表记录"""
        with self._connect() as conn:
            conn.execute(
                "UPDATE account_cache SET is_deleted = 1, deleted_at = ? WHERE 记录ID = ?",
                (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), record_id),
            )
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
            rows = conn.execute("SELECT * FROM cookie_cache WHERE is_deleted = 0 ORDER BY 创建时间 DESC").fetchall()
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
            # 自动更新「最后更新时间」字段
            if "最后更新时间" not in data:
                data["最后更新时间"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
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
            conn.execute(
                "UPDATE cookie_cache SET is_deleted = 1, deleted_at = ? WHERE 记录ID = ?",
                (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), record_id),
            )
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
            data["同步时间"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
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

    # ========== 软删除辅助（删除同步专用） ==========

    _SYNC_TABLES = {"collection_cache", "account_cache", "cookie_cache"}

    def get_deleted_ids(self, table: str) -> list[str]:
        """获取墓碑记录（is_deleted=1）的 record_id 列表"""
        if table not in self._SYNC_TABLES:
            return []
        with self._connect() as conn:
            rows = conn.execute(f"SELECT 记录ID FROM {table} WHERE is_deleted = 1").fetchall()
            return [row["记录ID"] for row in rows]

    def get_active_ids(self, table: str) -> list[str]:
        """获取正常记录（is_deleted=0）的 record_id 列表"""
        if table not in self._SYNC_TABLES:
            return []
        with self._connect() as conn:
            rows = conn.execute(f"SELECT 记录ID FROM {table} WHERE is_deleted = 0").fetchall()
            return [row["记录ID"] for row in rows]

    def get_synced_active_ids(self, table: str) -> list[str]:
        """获取已同步且未删除的 record_id 列表（删除检测专用）"""
        if table not in self._SYNC_TABLES:
            return []
        with self._connect() as conn:
            rows = conn.execute(f"SELECT 记录ID FROM {table} WHERE is_deleted = 0 AND synced = 1").fetchall()
            return [row["记录ID"] for row in rows]

    def hard_delete(self, table: str, record_id: str) -> bool:
        """硬删除（真删），用于飞书→本地方向清理孤儿记录"""
        if table not in self._SYNC_TABLES:
            return False
        with self._connect() as conn:
            conn.execute(f"DELETE FROM {table} WHERE 记录ID = ?", (record_id,))
            conn.commit()
            return True

    def purge_tombstone(self, table: str, record_id: str) -> bool:
        """清除已同步删除的墓碑"""
        if table not in self._SYNC_TABLES:
            return False
        with self._connect() as conn:
            conn.execute(f"DELETE FROM {table} WHERE 记录ID = ? AND is_deleted = 1", (record_id,))
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

    # ========== 通用表操作（用于数据库管理界面） ==========

    # 允许操作的表白名单
    VALID_TABLES = {
        "collection_cache", "account_cache", "cookie_cache",
        "collection_history", "scheduled_tasks",
    }

    def get_table_schema(self, table: str) -> list[dict]:
        """获取表结构信息。
        返回字段列表，每项: {name, type, pk, notnull, default}
        """
        with self._connect() as conn:
            rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
            return [
                {
                    "name": r[1],
                    "type": r[2],
                    "pk": r[5],
                    "notnull": r[3],
                    "default": r[4],
                }
                for r in rows
            ]

    def query_table(
        self,
        table: str,
        limit: int = 100,
        offset: int = 0,
        search: str = "",
        sort_field: Optional[str] = None,
        sort_order: str = "desc",
    ) -> dict:
        """通用表查询，支持搜索、排序、分页。
        返回 {records, total, limit, offset}
        """
        with self._connect() as conn:
            cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]

            # 构造 WHERE
            params: list[Any] = []
            where = ""
            if search:
                where = " WHERE " + " OR ".join([f'CAST("{c}" AS TEXT) LIKE ?' for c in cols])
                params = [f"%{search}%"] * len(cols)

            # 构造 ORDER BY
            order = " ORDER BY rowid DESC"
            if sort_field and sort_field in cols:
                direction = "ASC" if sort_order.lower() == "asc" else "DESC"
                order = f' ORDER BY "{sort_field}" {direction}'

            total = conn.execute(
                f"SELECT COUNT(*) FROM {table}{where}", params
            ).fetchone()[0]
            rows = conn.execute(
                f"SELECT * FROM {table}{where}{order} LIMIT ? OFFSET ?",
                params + [limit, offset],
            ).fetchall()
            return {
                "records": [dict(row) for row in rows],
                "total": total,
                "limit": limit,
                "offset": offset,
            }

    def update_record_field(self, table: str, record_id: str, field: str, value: Any) -> bool:
        """通用单字段更新（用于启用/禁用开关）。
        record_id 按主键列匹配；如果没有主键则报错。
        """
        schema = self.get_table_schema(table)
        pk_cols = [s["name"] for s in schema if s["pk"]]
        col_names = [s["name"] for s in schema]
        if field not in col_names:
            raise ValueError(f"字段 {field} 不存在于表 {table}")
        if not pk_cols:
            raise ValueError(f"表 {table} 无主键，无法定位记录")
        pk = pk_cols[0]
        with self._connect() as conn:
            # 同步时间自动刷新（如果表有此字段）
            set_clause = f'"{field}" = ?'
            params: list[Any] = [value]
            if "同步时间" in col_names and field != "同步时间":
                set_clause += ', "同步时间" = ?'
                params.append(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            params.append(record_id)
            cursor = conn.execute(
                f'UPDATE {table} SET {set_clause} WHERE "{pk}" = ?',
                params,
            )
            conn.commit()
            return cursor.rowcount > 0

    def import_records(self, table: str, records: list[dict], skip_existing: bool = True) -> dict:
        """通用导入。
        - records: 已经按字段名整理好的字典列表
        - skip_existing: 主键已存在则跳过（不报错）
        返回 {created, skipped, failed, errors[]}
        """
        schema = self.get_table_schema(table)
        pk_cols = [s["name"] for s in schema if s["pk"]]
        col_names = [s["name"] for s in schema]
        result = {"created": 0, "skipped": 0, "failed": 0, "errors": []}
        if not records:
            return result
        with self._connect() as conn:
            for idx, rec in enumerate(records, start=2):  # start=2: 第1行是表头，第2行开始是数据
                # 严格模式：所有键必须在 schema 内
                unknown = [k for k in rec.keys() if k not in col_names]
                if unknown:
                    result["failed"] += 1
                    result["errors"].append(f"第{idx}行：未知字段 {unknown}")
                    continue
                # 必填字段（NOT NULL 且无默认）
                missing = [
                    s["name"] for s in schema
                    if s["notnull"] and s["default"] is None and s["pk"] == 0
                    and (s["name"] not in rec or rec[s["name"]] in (None, ""))
                ]
                if missing:
                    result["failed"] += 1
                    result["errors"].append(f"第{idx}行：缺少必填字段 {missing}")
                    continue
                # 主键已存在跳过
                if pk_cols and pk_cols[0] in rec and rec[pk_cols[0]]:
                    pk_val = rec[pk_cols[0]]
                    existed = conn.execute(
                        f'SELECT 1 FROM {table} WHERE "{pk_cols[0]}" = ?', (pk_val,)
                    ).fetchone()
                    if existed:
                        if skip_existing:
                            result["skipped"] += 1
                            continue
                        else:
                            result["failed"] += 1
                            result["errors"].append(f"第{idx}行：主键 {pk_val} 已存在")
                            continue
                # 过滤 None 值（避免插入 None 覆盖默认值）
                clean = {k: v for k, v in rec.items() if v is not None and v != ""}
                try:
                    if clean:
                        fields = ", ".join([f'"{k}"' for k in clean.keys()])
                        placeholders = ", ".join(["?"] * len(clean))
                        conn.execute(
                            f'INSERT INTO {table} ({fields}) VALUES ({placeholders})',
                            list(clean.values()),
                        )
                        result["created"] += 1
                except Exception as e:
                    result["failed"] += 1
                    result["errors"].append(f"第{idx}行：{str(e)}")
            conn.commit()
        return result
