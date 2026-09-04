"""DoukHub 本地数据库管理"""
import sqlite3
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from .data_root import app_data_root



class Database:
    """本地 SQLite 数据库管理"""

    SCHEMA_VERSION = 1

    # 去重值缓存存活时间（秒）。用于平台/状态这类低基数字段的筛选下拉，
    # 值几乎不变，没必要每次打开筛选菜单都跑一次 DISTINCT。
    DISTINCT_CACHE_TTL = 120.0

    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = db_path or app_data_root() / "doukhub.db"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # key: "table::field" -> (expire_ts, [values])
        self._distinct_cache: dict[str, tuple[float, list[str]]] = {}
        self._init_database()

    def invalidate_distinct_cache(self, table: Optional[str] = None) -> None:
        """失效去重值缓存。写入/删除/导入后调用；table 为空则清空全部。"""
        if table is None:
            self._distinct_cache.clear()
            return
        prefix = table + "::"
        for key in [k for k in self._distinct_cache if k.startswith(prefix)]:
            self._distinct_cache.pop(key, None)

    def _init_database(self):
        """初始化数据库表结构

        字段命名规范（v3）：
        - 业务字段：中文（分享码/平台/等级/标签/解析状态/获取状态/...）
        - 系统字段：英文，本地专用不进飞书（record_id/is_deleted/deleted_at/synced/created_at）
        - v3 变更：已就绪→解析状态(TEXT枚举), 已获取信息(BOOLEAN)→获取状态(TEXT枚举), 删除同步错误/获取错误
        """
        with self._connect() as conn:
            self._migrate_collection_cache_to_share_cache(conn)

            # 表1：分享表缓存
            conn.execute("""
                CREATE TABLE IF NOT EXISTS share_cache (
                    record_id TEXT PRIMARY KEY,
                    share_code TEXT UNIQUE NOT NULL,
                    平台 TEXT,
                    等级 INTEGER,
                    标签 TEXT,
                    sec_user_id TEXT,
                    解析状态 TEXT DEFAULT '待解析',
                    备注 TEXT,
                    粉丝数 INTEGER,
                    作品数 INTEGER,
                    账号名称 TEXT,
                    同步时间 DATETIME DEFAULT CURRENT_TIMESTAMP,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    is_deleted BOOLEAN DEFAULT 0,
                    deleted_at DATETIME,
                    synced BOOLEAN DEFAULT 0,
                    local_updated_at DATETIME
                )
            """)

            # 表2：账号表缓存（字段名对齐飞书）
            conn.execute("""
                CREATE TABLE IF NOT EXISTS account_cache (
                    record_id TEXT PRIMARY KEY,
                    账号名称 TEXT,
                    平台 TEXT,
                    链接 TEXT,
                    sec_user_id TEXT UNIQUE NOT NULL,
                    等级 INTEGER,
                    标签 TEXT,
                    启用 BOOLEAN DEFAULT 1,
                    采集类型 TEXT DEFAULT '发布',
                    备注 TEXT,
                    粉丝数 INTEGER,
                    作品数 INTEGER,
                    签名 TEXT,
                    头像 TEXT,
                    获取状态 TEXT DEFAULT '待获取',
                    同步时间 DATETIME DEFAULT CURRENT_TIMESTAMP,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    is_deleted BOOLEAN DEFAULT 0,
                    deleted_at DATETIME,
                    synced BOOLEAN DEFAULT 0,
                    local_updated_at DATETIME,
                    last_collected_at DATETIME,
                    collect_window_days INTEGER
                )
            """)

            # 表3：Cookie表缓存
            conn.execute("""
                CREATE TABLE IF NOT EXISTS cookie_cache (
                    record_id TEXT PRIMARY KEY,
                    Cookie TEXT NOT NULL,
                    平台 TEXT,
                    状态 TEXT DEFAULT '正常',
                    启用 BOOLEAN DEFAULT 1,
                    备注 TEXT,
                    验证时间 DATETIME,
                    last_used_at DATETIME,
                    use_count INTEGER DEFAULT 0,
                    同步时间 DATETIME DEFAULT CURRENT_TIMESTAMP,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    is_deleted BOOLEAN DEFAULT 0,
                    deleted_at DATETIME,
                    synced BOOLEAN DEFAULT 0,
                    local_updated_at DATETIME
                )
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS collection_batches (
                    id TEXT PRIMARY KEY,
                    status TEXT NOT NULL DEFAULT 'pending',
                    filter_json TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    preset_name TEXT,
                    process_pid INTEGER,
                    log_path TEXT,
                    started_at DATETIME,
                    finished_at DATETIME,
                    total_accounts INTEGER DEFAULT 0,
                    success_accounts INTEGER DEFAULT 0,
                    failed_accounts INTEGER DEFAULT 0,
                    skipped_accounts INTEGER DEFAULT 0,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS collection_batch_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id TEXT NOT NULL,
                    account_record_id TEXT,
                    sec_user_id TEXT NOT NULL,
                    account_name TEXT,
                    platform TEXT NOT NULL,
                    mark TEXT,
                    url TEXT,
                    earliest TEXT,
                    status TEXT NOT NULL DEFAULT 'pending',
                    message TEXT,
                    started_at DATETIME,
                    finished_at DATETIME,
                    FOREIGN KEY (batch_id) REFERENCES collection_batches(id)
                )
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS single_work_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    work_id TEXT,
                    source_link TEXT,
                    platform TEXT,
                    work_type TEXT,
                    title TEXT,
                    author TEXT,
                    filename_template TEXT,
                    filename_override TEXT,
                    target_dir TEXT,
                    files_json TEXT NOT NULL DEFAULT '[]',
                    request_json TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL DEFAULT 'running',
                    error TEXT,
                    work_json TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # 表6：同步历史（记录每次同步任务执行的摘要+日志）
            conn.execute("""
                CREATE TABLE IF NOT EXISTS sync_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    total INTEGER DEFAULT 0,
                    success INTEGER DEFAULT 0,
                    failed INTEGER DEFAULT 0,
                    skipped INTEGER DEFAULT 0,
                    error TEXT,
                    log_json TEXT,
                    started_at TEXT,
                    finished_at TEXT,
                    duration_sec REAL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # 表8：作品下载明细（批次内每个作品一条，实时写入）
            conn.execute("""
                CREATE TABLE IF NOT EXISTS collection_works (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id TEXT NOT NULL,
                    sec_user_id TEXT DEFAULT '',
                    account_name TEXT DEFAULT '',
                    platform TEXT DEFAULT '',
                    aweme_id TEXT NOT NULL,
                    title TEXT DEFAULT '',
                    kind TEXT DEFAULT 'video',
                    work_url TEXT DEFAULT '',
                    file_name TEXT DEFAULT '',
                    download_dir TEXT DEFAULT '',
                    file_path TEXT DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'success',
                    message TEXT DEFAULT '',
                    collected_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(batch_id, aweme_id)
                )
            """)

            # 表7：应用配置（key-value，配置文件整包存单键 JSON，随库备份）
            conn.execute("""
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT,
                    updated_at TEXT
                )
            """)

            # 兼容旧库：自动迁移字段名（账号标识→sec_user_id 等）
            # 必须在 CREATE INDEX 之前执行，因为索引依赖字段名
            self._migrate_legacy_columns(conn)

            # 创建索引（依赖字段名，必须在迁移之后）
            # 用 try/except 容错：旧库迁移后字段可能仍缺失（如 sec_user_id）
            for sql in [
                "CREATE INDEX IF NOT EXISTS idx_share_share_code ON share_cache(share_code)",
                "CREATE INDEX IF NOT EXISTS idx_share_sec_user_id ON share_cache(sec_user_id)",
                "CREATE INDEX IF NOT EXISTS idx_account_sec_user_id ON account_cache(sec_user_id)",
                "CREATE INDEX IF NOT EXISTS idx_sync_history_type ON sync_history(task_type)",
                "CREATE INDEX IF NOT EXISTS idx_sync_history_created ON sync_history(created_at)",
                "CREATE INDEX IF NOT EXISTS idx_collection_batch_status ON collection_batches(status, created_at)",
                "CREATE INDEX IF NOT EXISTS idx_collection_batch_item_batch ON collection_batch_items(batch_id, sec_user_id)",
                "CREATE INDEX IF NOT EXISTS idx_single_work_history_created ON single_work_history(created_at)",
                "CREATE INDEX IF NOT EXISTS idx_collection_works_batch ON collection_works(batch_id)",
                "CREATE INDEX IF NOT EXISTS idx_collection_works_sec ON collection_works(sec_user_id)",
                "CREATE INDEX IF NOT EXISTS idx_collection_works_aweme ON collection_works(aweme_id)",
            ]:
                try:
                    conn.execute(sql)
                except sqlite3.OperationalError:
                    pass
            self._migrate_schema_version(conn)

    def _migrate_collection_cache_to_share_cache(self, conn: sqlite3.Connection) -> None:
        """Rename the legacy collection_cache table without losing local data."""
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        old_table = "collection_cache"
        new_table = "share_cache"

        if old_table not in tables:
            return

        if new_table not in tables:
            conn.execute(f"ALTER TABLE {old_table} RENAME TO {new_table}")
        else:
            old_columns = {
                row[1] for row in conn.execute(f"PRAGMA table_info({old_table})")
            }
            new_columns = {
                row[1] for row in conn.execute(f"PRAGMA table_info({new_table})")
            }
            shared_columns = old_columns & new_columns
            if shared_columns:
                columns = ", ".join(sorted(shared_columns))
                conn.execute(
                    f"INSERT OR IGNORE INTO {new_table} ({columns}) "
                    f"SELECT {columns} FROM {old_table}"
                )
            conn.execute(f"DROP TABLE {old_table}")

        for index_name in ("idx_collection_share", "idx_collection_sec_user_id"):
            conn.execute(f"DROP INDEX IF EXISTS {index_name}")
        conn.commit()

    def _migrate_legacy_columns(self, conn):
        """迁移旧库字段，使其对齐飞书 + 系统字段英文化（v2）。

        v2 重命名：
        - 记录ID → record_id（三张同步表）
        - 创建时间 → created_at（三张同步表）

        v1 历史迁移（保留兼容）：
        - account_cache: 账号标识→sec_user_id, 更新错误→备注, 更新时间→同步时间, 已更新→已获取信息
        - share_cache: 账号标识→sec_user_id, 更新时间→同步时间
        - cookie_cache: 更新时间→同步时间
        - 删除 account_cache.代理（如存在）

        v2 新增字段：
        - 三张同步表：is_deleted / deleted_at / synced（如缺失则添加）
        - 旧 synced 字段首次添加时把现有记录全部标记为已就绪

        v2 废弃字段（不主动删除，保留兼容）：
        - 最后更新时间（旧增量同步遗留，新方案不再使用）
        """
        # v1 历史重命名（业务字段对齐飞书）
        rename_map = {
            "share_cache": [
                ("账号标识", "sec_user_id"),
                ("更新时间", "同步时间"),
                ("分享码", "share_code"),
                ("已同步", "已就绪"),
                ("已解析", "已就绪"),
                # v3: 已就绪 → 解析状态
                ("已就绪", "解析状态"),
            ],
            "account_cache": [
                ("账号标识", "sec_user_id"),
                ("更新错误", "备注"),
                ("更新时间", "同步时间"),
                ("已更新", "已获取信息"),
                # v3: 已获取信息 → 获取状态
                ("已获取信息", "获取状态"),
            ],
            "cookie_cache": [
                ("更新时间", "同步时间"),
            ],
        }
        # v2 系统字段英文化（三张同步表）
        v2_renames = {
            "share_cache": [("记录ID", "record_id"), ("创建时间", "created_at")],
            "account_cache": [("记录ID", "record_id"), ("创建时间", "created_at")],
            "cookie_cache": [("记录ID", "record_id"), ("创建时间", "created_at")],
        }
        # v2 新增字段
        add_columns = {
            "share_cache": [
                ("账号名称", "TEXT"),
            ],
            "account_cache": [
                ("启用", "BOOLEAN DEFAULT 1"),
                ("采集类型", "TEXT DEFAULT '发布'"),
                ("last_collected_at", "DATETIME"),
                ("collect_window_days", "INTEGER"),
            ],
            "cookie_cache": [
                ("last_used_at", "DATETIME"),
                ("use_count", "INTEGER DEFAULT 0"),
            ],
            "collection_batches": [
                ("preset_name", "TEXT"),
            ],
        }
        # 软删除字段（墓碑）：三张同步表都加上
        for _tbl in ("share_cache", "account_cache", "cookie_cache"):
            add_columns.setdefault(_tbl, []).extend([
                ("is_deleted", "BOOLEAN DEFAULT 0"),
                ("deleted_at", "DATETIME"),
            ])
        # 同步标记：记录是否已确认存在于飞书（区分"本地新建未推送"和"飞书已删除"）
        for _tbl in ("share_cache", "account_cache", "cookie_cache"):
            add_columns.setdefault(_tbl, []).append(
                ("synced", "BOOLEAN DEFAULT 0"),
            )
        # 方案 B：LWW 时间戳（本地最后修改时间，用于与飞书「最后更新时间」比较）
        for _tbl in ("share_cache", "account_cache", "cookie_cache"):
            add_columns.setdefault(_tbl, []).append(
                ("local_updated_at", "DATETIME"),
            )

        # v2.1：删除 account_cache.昵称（与 账号名称 重复，统一用 账号名称）
        # v3：删除 同步错误/获取错误（状态枚举已表达失败语义）
        drop_columns = {
            "account_cache": ["昵称", "获取错误"],
            "share_cache": ["昵称", "签名", "头像", "同步错误"],
        }

        # 执行 v1 业务字段重命名
        for table, renames in rename_map.items():
            for old, new in renames:
                cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
                if old in cols and new not in cols:
                    conn.execute(f'ALTER TABLE {table} RENAME COLUMN "{old}" TO "{new}"')
                elif old in cols and new in cols:
                    # 两个都存在（异常情况），保留新字段
                    pass

        # 执行 v2 系统字段英文化重命名
        for table, renames in v2_renames.items():
            for old, new in renames:
                cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
                if old in cols and new not in cols:
                    conn.execute(f'ALTER TABLE {table} RENAME COLUMN "{old}" TO "{new}"')

        # 执行删除字段（DROP COLUMN，SQLite 3.35.0+）
        for table, drops in drop_columns.items():
            cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
            for col in drops:
                if col in cols:
                    try:
                        conn.execute(f'ALTER TABLE {table} DROP COLUMN "{col}"')
                    except Exception:
                        pass  # 旧版 SQLite 不支持 DROP COLUMN，忽略

        # 添加缺失字段
        for table, additions in add_columns.items():
            cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
            for col, ddl in additions:
                if col not in cols:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
                    # synced 字段首次添加时，把现有记录全部标记为已就绪
                    if col == "synced":
                        conn.execute(f"UPDATE {table} SET synced = 1")

        # 解析状态字段数据迁移：旧的 BOOLEAN 值（0/1/True/False）转为文本枚举
        share_cols_info = {r[1]: r[2] for r in conn.execute("PRAGMA table_info(share_cache)").fetchall()}
        if "解析状态" in share_cols_info:
            # 先更新数据值
            conn.execute("UPDATE share_cache SET 解析状态 = '已就绪' WHERE 解析状态 IN ('1', 'true', 'True', 1)")
            conn.execute("UPDATE share_cache SET 解析状态 = '待解析' WHERE 解析状态 IS NULL OR 解析状态 IN ('0', 'false', 'False', 0) OR 解析状态 = ''")
            # 如果列类型仍是 BOOLEAN，需要重建表改为 TEXT
            col_type = (share_cols_info["解析状态"] or "").upper()
            if "BOOL" in col_type:
                conn.execute("ALTER TABLE share_cache RENAME TO share_cache_old_v3")
                conn.execute("""
                    CREATE TABLE share_cache (
                        record_id TEXT PRIMARY KEY,
                        share_code TEXT UNIQUE NOT NULL,
                        平台 TEXT,
                        等级 INTEGER,
                        标签 TEXT,
                        sec_user_id TEXT,
                        解析状态 TEXT DEFAULT '待解析',
                        备注 TEXT,
                        粉丝数 INTEGER,
                        作品数 INTEGER,
                        账号名称 TEXT,
                        同步时间 DATETIME DEFAULT CURRENT_TIMESTAMP,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        is_deleted BOOLEAN DEFAULT 0,
                        deleted_at DATETIME,
                        synced BOOLEAN DEFAULT 0,
                        local_updated_at DATETIME
                    )
                """)
                old_cols = [r[1] for r in conn.execute("PRAGMA table_info(share_cache_old_v3)").fetchall()]
                new_cols = [r[1] for r in conn.execute("PRAGMA table_info(share_cache)").fetchall()]
                common_cols = [c for c in new_cols if c in old_cols]
                col_list = ", ".join(f'"{c}"' for c in common_cols)
                conn.execute(f"INSERT INTO share_cache ({col_list}) SELECT {col_list} FROM share_cache_old_v3")
                conn.execute("DROP TABLE share_cache_old_v3")

        # 获取状态字段数据迁移：旧的 BOOLEAN 值转为文本枚举
        acc_cols_info = {r[1]: r[2] for r in conn.execute("PRAGMA table_info(account_cache)").fetchall()}
        if "获取状态" in acc_cols_info:
            # 先更新数据值
            conn.execute("UPDATE account_cache SET 获取状态 = '已获取' WHERE 获取状态 IN ('1', 'true', 'True', 1)")
            conn.execute("UPDATE account_cache SET 获取状态 = '待获取' WHERE 获取状态 IS NULL OR 获取状态 IN ('0', 'false', 'False', 0) OR 获取状态 = ''")
            # 如果旧表有获取错误字段且有值，把对应记录标记为'获取失败'
            if "获取错误" in acc_cols_info:
                conn.execute("UPDATE account_cache SET 获取状态 = '获取失败' WHERE 获取错误 IS NOT NULL AND 获取错误 != '' AND 获取状态 = '待获取'")
            # 如果列类型仍是 BOOLEAN，需要重建表改为 TEXT
            col_type = (acc_cols_info["获取状态"] or "").upper()
            if "BOOL" in col_type:
                conn.execute("ALTER TABLE account_cache RENAME TO account_cache_old_v3")
                conn.execute("""
                    CREATE TABLE account_cache (
                        record_id TEXT PRIMARY KEY,
                        账号名称 TEXT,
                        平台 TEXT,
                        链接 TEXT,
                        sec_user_id TEXT UNIQUE NOT NULL,
                        等级 INTEGER,
                        标签 TEXT,
                        启用 BOOLEAN DEFAULT 1,
                        采集类型 TEXT DEFAULT '发布',
                        备注 TEXT,
                        粉丝数 INTEGER,
                        作品数 INTEGER,
                        签名 TEXT,
                        头像 TEXT,
                        获取状态 TEXT DEFAULT '待获取',
                        同步时间 DATETIME DEFAULT CURRENT_TIMESTAMP,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        is_deleted BOOLEAN DEFAULT 0,
                        deleted_at DATETIME,
                        synced BOOLEAN DEFAULT 0,
                        local_updated_at DATETIME,
                        last_collected_at DATETIME,
                        collect_window_days INTEGER
                    )
                """)
                old_cols = [r[1] for r in conn.execute("PRAGMA table_info(account_cache_old_v3)").fetchall()]
                new_cols = [r[1] for r in conn.execute("PRAGMA table_info(account_cache)").fetchall()]
                common_cols = [c for c in new_cols if c in old_cols]
                col_list = ", ".join(f'"{c}"' for c in common_cols)
                conn.execute(f"INSERT INTO account_cache ({col_list}) SELECT {col_list} FROM account_cache_old_v3")
                conn.execute("DROP TABLE account_cache_old_v3")



                # 平台值规范化：抖音/小红书等中文与大小写写法统一为 douyin / tiktok / xhs
        from .platform_utils import normalize_platform
        for _tbl in ("share_cache", "account_cache", "cookie_cache"):
            _cols = [r[1] for r in conn.execute(f"PRAGMA table_info({_tbl})").fetchall()]
            if "平台" not in _cols:
                continue
            for _rid, _pf in conn.execute(f'SELECT record_id, 平台 FROM {_tbl}').fetchall():
                _norm = normalize_platform(_pf)
                if _norm and _norm != _pf:
                    conn.execute(f'UPDATE {_tbl} SET 平台 = ? WHERE record_id = ?', (_norm, _rid))

        # 硬删除迁移：清理旧软删除遗留的墓碑记录
        for _tbl in ("share_cache", "account_cache", "cookie_cache"):
            _cols = [r[1] for r in conn.execute(f"PRAGMA table_info({_tbl})").fetchall()]
            if "is_deleted" in _cols:
                conn.execute(f"DELETE FROM {_tbl} WHERE is_deleted = 1")

    def _connect(self) -> sqlite3.Connection:
        """Create one SQLite connection with the project's standard pragmas."""
        conn = sqlite3.connect(str(self.db_path), timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _migrate_schema_version(self, conn: sqlite3.Connection) -> None:
        """Persist schema version without ever downgrading a newer database."""
        current = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if current < self.SCHEMA_VERSION:
            conn.execute(f"PRAGMA user_version = {self.SCHEMA_VERSION}")

    # ========== 分享表缓存操作 ==========

    def get_all_collections(self) -> list[dict]:
        """获取所有分享表记录"""
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM share_cache ORDER BY created_at DESC").fetchall()
            return [dict(row) for row in rows]

    def get_collection_by_share(self, share: str) -> Optional[dict]:
        """根据 share_code 获取记录"""
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM share_cache WHERE share_code = ?", (share,)).fetchone()
            return dict(row) if row else None

    def get_collection_by_id(self, record_id: str) -> Optional[dict]:
        """根据记录ID获取分享表记录"""
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM share_cache WHERE record_id = ?", (record_id,)).fetchone()
            return dict(row) if row else None

    def get_collection_by_sec_user_id(self, sec_user_id: str) -> Optional[dict]:
        """根据 sec_user_id 获取记录"""
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM share_cache WHERE sec_user_id = ?", (sec_user_id,)).fetchone()
            return dict(row) if row else None

    def insert_collection(self, data: dict) -> bool:
        """插入分享表记录。UNIQUE 冲突会抛出 sqlite3.IntegrityError，调用方按需捕获。"""
        with self._connect() as conn:
            fields = ", ".join(data.keys())
            placeholders = ", ".join(["?" for _ in data])
            conn.execute(f"INSERT INTO share_cache ({fields}) VALUES ({placeholders})", list(data.values()))
            conn.commit()
            return True

    def update_collection(self, record_id: str, data: dict) -> bool:
        """更新分享表记录

        方案 B：自动维护 local_updated_at（用于 LWW 比较）。
        如果 data 中已显式传入 local_updated_at（如同步流程），则用传入值。
        """
        with self._connect() as conn:
            if "local_updated_at" not in data:
                data["local_updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            set_clause = ", ".join([f"{k} = ?" for k in data.keys()])
            conn.execute(f"UPDATE share_cache SET {set_clause} WHERE record_id = ?", list(data.values()) + [record_id])
            conn.commit()
            return True

    def delete_collection(self, record_id: str) -> bool:
        """硬删除分享表记录"""
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM share_cache WHERE record_id = ?",
                (record_id,),
            )
            conn.commit()
            return True

    def clear_share_cache(self) -> bool:
        """清空分享表缓存"""
        with self._connect() as conn:
            conn.execute("DELETE FROM share_cache")
            conn.commit()
            return True

    # ========== 账号表缓存操作 ==========

    def get_all_accounts(self) -> list[dict]:
        """获取所有账号表记录"""
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM account_cache ORDER BY created_at DESC").fetchall()
            return [dict(row) for row in rows]

    def get_account_by_sec_user_id(self, sec_user_id: str) -> Optional[dict]:
        """根据 sec_user_id 获取记录"""
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM account_cache WHERE sec_user_id = ?", (sec_user_id,)).fetchone()
            return dict(row) if row else None

    def get_account_by_id(self, record_id: str) -> Optional[dict]:
        """根据记录ID获取账号表记录"""
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM account_cache WHERE record_id = ?", (record_id,)).fetchone()
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
        """更新账号表记录

        方案 B：自动维护 local_updated_at（用于 LWW 比较）。
        如果 data 中已显式传入 local_updated_at（如同步流程），则用传入值。
        """
        with self._connect() as conn:
            if "local_updated_at" not in data:
                data["local_updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            set_clause = ", ".join([f"{k} = ?" for k in data.keys()])
            conn.execute(f"UPDATE account_cache SET {set_clause} WHERE record_id = ?", list(data.values()) + [record_id])
            conn.commit()
            return True

    def delete_account(self, record_id: str) -> bool:
        """硬删除账号表记录"""
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM account_cache WHERE record_id = ?",
                (record_id,),
            )
            conn.commit()
            return True

    def clear_account_cache(self) -> bool:
        """清空账号表缓存"""
        with self._connect() as conn:
            conn.execute("DELETE FROM account_cache")
            conn.commit()
            return True

    # ========== 采集批次操作 ==========

    _BATCH_FIELDS = {
        "status", "process_pid", "log_path", "started_at", "finished_at",
        "total_accounts", "success_accounts", "failed_accounts", "skipped_accounts",
    }
    _BATCH_ITEM_FIELDS = {
        "status", "message", "started_at", "finished_at",
    }
    _SINGLE_WORK_HISTORY_FIELDS = {
        "work_id", "source_link", "platform", "work_type", "title", "author",
        "filename_template", "filename_override", "target_dir", "files_json",
        "request_json", "status", "error", "work_json",
    }

    def create_collection_batch(
        self,
        batch_id: str,
        filter_json: str,
        platform: str,
        log_path: str,
        items: list[dict],
        preset_name: str = "",
    ) -> None:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO collection_batches
                (id, status, filter_json, platform, preset_name, log_path, total_accounts, created_at)
                VALUES (?, 'pending', ?, ?, ?, ?, ?, ?)
                """,
                (batch_id, filter_json, platform, preset_name, log_path, len(items), now),
            )
            conn.executemany(
                """
                INSERT INTO collection_batch_items
                (batch_id, account_record_id, sec_user_id, account_name, platform,
                 mark, url, earliest, status, message)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        batch_id,
                        item.get("account_record_id"),
                        item["sec_user_id"],
                        item.get("account_name", ""),
                        item.get("platform", platform),
                        item.get("mark", ""),
                        item.get("url", ""),
                        str(item.get("earliest", "")),
                        item.get("status", "pending"),
                        str(item.get("message") or ""),
                    )
                    for item in items
                ],
            )
            conn.commit()

    def get_collection_batch(self, batch_id: str) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM collection_batches WHERE id = ?", (batch_id,)
            ).fetchone()
            return dict(row) if row else None

    def get_active_collection_batch(self) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM collection_batches
                WHERE status IN ('pending', 'running', 'paused', 'cancelling')
                ORDER BY created_at, id
                LIMIT 1
                """
            ).fetchone()
            return dict(row) if row else None

    def has_pending_or_running_single_work(self) -> bool:
        """True when the recoverable single-work queue is not empty."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT EXISTS ("
                "SELECT 1 FROM single_work_history "
                "WHERE status IN ('pending', 'running'))"
            ).fetchone()
            return bool(row[0])

    def list_active_collection_batches(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM collection_batches
                WHERE status IN ('pending', 'running', 'cancelling')
                ORDER BY created_at, id
                """
            ).fetchall()
            return [dict(row) for row in rows]

    def list_collection_batches(self, limit: int = 20) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM collection_batches ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(row) for row in rows]

    def get_collection_batch_items(self, batch_id: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM collection_batch_items WHERE batch_id = ? ORDER BY id",
                (batch_id,),
            ).fetchall()
            return [dict(row) for row in rows]

    def find_collection_batch_item(
        self, batch_id: str, sec_user_id: str
    ) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM collection_batch_items
                WHERE batch_id = ? AND sec_user_id = ?
                LIMIT 1
                """,
                (batch_id, sec_user_id),
            ).fetchone()
            return dict(row) if row else None

    def get_collection_batch_item_by_id(self, item_id: int) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM collection_batch_items WHERE id = ?", (item_id,)
            ).fetchone()
            return dict(row) if row else None

    def update_collection_batch(self, batch_id: str, **fields) -> bool:
        valid = {key: value for key, value in fields.items() if key in self._BATCH_FIELDS}
        if not valid:
            return False
        assignments = ", ".join(f"{key} = ?" for key in valid)
        with self._connect() as conn:
            cursor = conn.execute(
                f"UPDATE collection_batches SET {assignments} WHERE id = ?",
                list(valid.values()) + [batch_id],
            )
            conn.commit()
            return cursor.rowcount > 0

    def update_collection_batch_item(self, item_id: int, **fields) -> bool:
        valid = {key: value for key, value in fields.items() if key in self._BATCH_ITEM_FIELDS}
        if not valid:
            return False
        assignments = ", ".join(f"{key} = ?" for key in valid)
        with self._connect() as conn:
            cursor = conn.execute(
                f"UPDATE collection_batch_items SET {assignments} WHERE id = ?",
                list(valid.values()) + [item_id],
            )
            conn.commit()
            return cursor.rowcount > 0

    # ------------------------------------------------------------------
    # 作品下载明细（collection_works）
    # ------------------------------------------------------------------
    def upsert_collection_work(
        self,
        batch_id: str,
        sec_user_id: str = "",
        account_name: str = "",
        platform: str = "",
        aweme_id: str = "",
        title: str = "",
        kind: str = "video",
        work_url: str = "",
        file_name: str = "",
        download_dir: str = "",
        file_path: str = "",
        status: str = "success",
        message: str = "",
    ) -> bool:
        """写入/更新一条作品明细。

        同批次同作品只保留一行：失败后重试成功会覆盖为成功；
        已是成功的记录不再被覆盖（图集多文件只记首个成功文件）。
        """
        if not batch_id or not aweme_id:
            return False
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO collection_works(
                    batch_id, sec_user_id, account_name, platform, aweme_id,
                    title, kind, work_url, file_name, download_dir, file_path,
                    status, message
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(batch_id, aweme_id) DO UPDATE SET
                    status = excluded.status,
                    file_name = CASE WHEN excluded.status = 'success'
                        THEN excluded.file_name ELSE collection_works.file_name END,
                    download_dir = CASE WHEN excluded.status = 'success'
                        THEN excluded.download_dir ELSE collection_works.download_dir END,
                    file_path = CASE WHEN excluded.status = 'success'
                        THEN excluded.file_path ELSE collection_works.file_path END,
                    message = excluded.message
                WHERE collection_works.status != 'success'
                """,
                (
                    batch_id, sec_user_id or "", account_name or "", platform or "",
                    str(aweme_id), title or "", kind or "video", work_url or "",
                    file_name or "", download_dir or "", file_path or "",
                    status or "success", message or "",
                ),
            )
            conn.commit()
        return True

    def count_batch_works(self, batch_id: str) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM collection_works WHERE batch_id = ?", (batch_id,)
            ).fetchone()
            return int(row[0]) if row else 0

    def list_batch_works(self, batch_id: str) -> list[dict]:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM collection_works WHERE batch_id = ? ORDER BY id",
                (batch_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def list_account_works(
        self, sec_user_id: str = "", account_name: str = "", limit: int = 2000
    ) -> list[dict]:
        """按账号聚合全部批次的作品明细，带跨批次/同批次重复标记。"""
        conds, params = [], []
        if sec_user_id:
            conds.append("sec_user_id = ?")
            params.append(sec_user_id)
        if account_name and not sec_user_id:
            conds.append("account_name = ?")
            params.append(account_name)
        if not conds:
            return []
        where = " AND ".join(conds)
        sql = f"""
            SELECT * FROM (
                SELECT w.*,
                    (SELECT COUNT(*) FROM collection_works d2
                      WHERE d2.aweme_id = w.aweme_id) AS dup_total,
                    (SELECT COUNT(*) FROM collection_works d3
                      WHERE d3.aweme_id = w.aweme_id AND d3.batch_id = w.batch_id) AS dup_in_batch
                FROM collection_works w
                WHERE {where}
                ORDER BY w.id DESC LIMIT ?
            ) ORDER BY id
        """
        params.append(limit)
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]

    def refresh_collection_batch_counts(self, batch_id: str) -> dict:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT
                    SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END) AS success,
                    SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed,
                    SUM(CASE WHEN status = 'skipped' THEN 1 ELSE 0 END) AS skipped
                FROM collection_batch_items
                WHERE batch_id = ?
                """,
                (batch_id,),
            ).fetchone()
            counts = {
                "success": row["success"] or 0,
                "failed": row["failed"] or 0,
                "skipped": row["skipped"] or 0,
            }
            conn.execute(
                """
                UPDATE collection_batches
                SET success_accounts = ?, failed_accounts = ?, skipped_accounts = ?
                WHERE id = ?
                """,
                (counts["success"], counts["failed"], counts["skipped"], batch_id),
            )
            conn.commit()
            return counts

    def get_account_collection_stats(self) -> list[dict]:
        """按账号聚合全部批次明细的采集成败统计（账号健康度数据源）。

        返回 [{ sec_user_id, account_name, success, failed, skipped,
                last_finished_at, last_status, last_message }]
        """
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    sec_user_id,
                    COALESCE(MAX(account_name), '') AS account_name,
                    SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END)   AS success,
                    SUM(CASE WHEN status = 'failed'  THEN 1 ELSE 0 END)   AS failed,
                    SUM(CASE WHEN status = 'skipped' THEN 1 ELSE 0 END)   AS skipped,
                    MAX(finished_at) AS last_finished_at
                FROM collection_batch_items
                GROUP BY sec_user_id
                ORDER BY MAX(finished_at) DESC
                """,
            ).fetchall()
            result = [dict(row) for row in rows]
        # 每条账号最近一条非 pending 明细的状态与原因（用于健康度提示）
        if result:
            with self._connect() as conn:
                for item in result:
                    row = conn.execute(
                        """
                        SELECT status, message FROM collection_batch_items
                        WHERE sec_user_id = ? AND status != 'pending'
                        ORDER BY id DESC LIMIT 1
                        """,
                        (item["sec_user_id"],),
                    ).fetchone()
                    if row:
                        item["last_status"] = row["status"]
                        item["last_message"] = row["message"] or ""
                    else:
                        item["last_status"] = ""
                        item["last_message"] = ""
        return result

    # ========== 单作品下载历史操作 ==========

    def create_single_work_history(
        self,
        work_id: str,
        source_link: str,
        platform: str,
        work_type: str,
        title: str,
        author: str,
        filename_template: str,
        filename_override: str,
        target_dir: str,
        request_json: str,
        status: str = "running",
    ) -> int:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO single_work_history
                (work_id, source_link, platform, work_type, title, author,
                 filename_template, filename_override, target_dir,
                 request_json, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (work_id, source_link, platform, work_type, title, author,
                 filename_template, filename_override, target_dir,
                 request_json, status, now, now),
            )
            conn.commit()
            return cursor.lastrowid

    def get_single_work_history(self, history_id: int) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM single_work_history WHERE id = ?",
                (history_id,),
            ).fetchone()
            return dict(row) if row else None

    def list_single_work_history(self, limit: int = 50) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM single_work_history
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [dict(row) for row in rows]

    def update_single_work_history(self, history_id: int, **fields) -> bool:
        valid = {
            key: value
            for key, value in fields.items()
            if key in self._SINGLE_WORK_HISTORY_FIELDS
        }
        if not valid:
            return False
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        valid["updated_at"] = now
        assignments = ", ".join(f"{key} = ?" for key in valid)
        with self._connect() as conn:
            cursor = conn.execute(
                f"UPDATE single_work_history SET {assignments} WHERE id = ?",
                list(valid.values()) + [history_id],
            )
            conn.commit()
            return cursor.rowcount > 0

    def get_all_cookies(self) -> list[dict]:
        """获取所有 Cookie 记录"""
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM cookie_cache ORDER BY created_at DESC").fetchall()
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
        """更新 Cookie 记录

        方案 B：自动维护 local_updated_at（用于 LWW 比较）。
        如果 data 中已显式传入 local_updated_at（如同步流程），则用传入值。
        """
        with self._connect() as conn:
            if "local_updated_at" not in data:
                data["local_updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            set_clause = ", ".join([f"{k} = ?" for k in data.keys()])
            conn.execute(f"UPDATE cookie_cache SET {set_clause} WHERE record_id = ?", list(data.values()) + [record_id])
            conn.commit()
            return True

    def mark_cookie_invalid(self, record_id: str) -> bool:
        """标记 Cookie 为失效"""
        return self.update_cookie(record_id, {"状态": "失效"})

    def record_cookie_usage(self, record_id: str) -> None:
        """记录 Cookie 被使用一次（use_count+1，更新 last_used_at）。"""
        with self._connect() as conn:
            conn.execute(
                "UPDATE cookie_cache SET use_count = COALESCE(use_count, 0) + 1, "
                "last_used_at = ? WHERE record_id = ?",
                (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), record_id),
            )
            conn.commit()

    def get_cookie_by_id(self, record_id: str) -> Optional[dict]:
        """根据记录ID获取 Cookie 记录"""
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM cookie_cache WHERE record_id = ?", (record_id,)).fetchone()
            return dict(row) if row else None

    def delete_cookie(self, record_id: str) -> bool:
        """硬删除 Cookie 记录"""
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM cookie_cache WHERE record_id = ?",
                (record_id,),
            )
            conn.commit()
            return True

    # ========== 同步辅助 ==========

    # ========== 软删除辅助（删除同步专用） ==========

    _SYNC_TABLES = {"share_cache", "account_cache", "cookie_cache"}

    def get_deleted_ids(self, table: str) -> list[str]:
        """已改为硬删除，不再有墓碑记录。保留接口兼容，始终返回空列表。"""
        return []

    def get_active_ids(self, table: str) -> list[str]:
        """获取全部记录的 record_id 列表（硬删除模式下全部都是活跃记录）"""
        if table not in self._SYNC_TABLES:
            return []
        with self._connect() as conn:
            rows = conn.execute(f"SELECT record_id FROM {table}").fetchall()
            return [row["record_id"] for row in rows]

    def get_synced_active_ids(self, table: str) -> list[str]:
        """获取已就绪且未删除的 record_id 列表（删除检测专用）"""
        if table not in self._SYNC_TABLES:
            return []
        with self._connect() as conn:
            rows = conn.execute(f"SELECT record_id FROM {table} WHERE synced = 1").fetchall()
            return [row["record_id"] for row in rows]

    def hard_delete(self, table: str, record_id: str) -> bool:
        """硬删除（真删），用于飞书→本地方向清理孤儿记录"""
        if table not in self._SYNC_TABLES:
            return False
        with self._connect() as conn:
            conn.execute(f"DELETE FROM {table} WHERE record_id = ?", (record_id,))
            conn.commit()
            return True

    def purge_tombstone(self, table: str, record_id: str) -> bool:
        """清除已就绪删除的墓碑"""
        if table not in self._SYNC_TABLES:
            return False
        with self._connect() as conn:
            conn.execute(f"DELETE FROM {table} WHERE record_id = ?", (record_id,))
            conn.commit()
            return True

    # ========== 统计和查询 ==========

    def get_table_counts(self) -> dict:
        """获取各表记录数"""
        tables = [
            "share_cache",
            "account_cache",
            "cookie_cache",
            "sync_history",
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
        "share_cache", "account_cache", "cookie_cache",
        "sync_history",
    }

    def get_record_by_id(self, table: str, record_id: str) -> Optional[dict]:
        """按主键查询单条记录（通用版）。"""
        if table not in self.VALID_TABLES:
            return None
        schema = self.get_table_schema(table)
        pk_cols = [s["name"] for s in schema if s["pk"]]
        pk = pk_cols[0] if pk_cols else None
        if not pk:
            return None
        with self._connect() as conn:
            row = conn.execute(
                f'SELECT * FROM {table} WHERE "{pk}" = ?', (record_id,)
            ).fetchone()
            return dict(row) if row else None

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
        search_field: Optional[str] = None,
        sort_field: Optional[str] = None,
        sort_order: str = "desc",
        filters: Optional[str] = None,
        need_total: bool = True,
    ) -> dict:
        """通用表查询，支持搜索、排序、多条件筛选、分页。
        返回 {records, total, limit, offset}

        search_field:
            定向搜索字段。指定后只在这一列上做 LIKE，避免把所有列拼成 OR 链
            导致的全表扫描（原实现是主要性能瓶颈）。为空则退化为全列搜索。
        need_total:
            是否需要精确总数。滚动加载后续批次时传 False，复用首批算出的值，
            省掉重复的 COUNT(*) 全表扫描；此时返回的 total 为 -1。
        """
        with self._connect() as conn:
            cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]

            # 构造 WHERE（软删除过滤 + search 与 filter 为 AND 关系）
            params: list[Any] = []
            where_parts: list[str] = []
            if search:
                # 定向搜索：search_field 支持逗号分隔多字段（前端"文本字段"默认档）。
                # 只在指定列上做 LIKE，避免把所有列拼成 OR 链导致的全表扫描。
                # 为空则退化为全列搜索（慢，仅作为旧前端的兼容路径）。
                search_cols: list[str] = []
                if search_field:
                    search_cols = [c.strip() for c in str(search_field).split(",") if c.strip() and c.strip() in cols]
                if search_cols:
                    where_parts.append("(" + " OR ".join([f'CAST("{c}" AS TEXT) LIKE ?' for c in search_cols]) + ")")
                    params += [f"%{search}%"] * len(search_cols)
                else:
                    where_parts.append("(" + " OR ".join([f'CAST("{c}" AS TEXT) LIKE ?' for c in cols]) + ")")
                    params += [f"%{search}%"] * len(cols)

            # filters: JSON 数组字符串，每项 {field, values: [...]}
            # 同一字段多个值 = OR（IN），不同字段 = AND
            if filters:
                try:
                    filter_list = json.loads(filters)
                except (json.JSONDecodeError, TypeError):
                    filter_list = []
                for cond in filter_list:
                    if not isinstance(cond, dict):
                        continue
                    f = cond.get("field", "")
                    values = cond.get("values", [])
                    if f not in cols or not values:
                        continue
                    if f == "标签":
                        # 标签存为 JSON 数组文本，中文原值和 Unicode 转义形式都匹配
                        tag_parts = []
                        for v in values:
                            v_str = str(v)
                            escaped = v_str.encode('unicode_escape').decode('ascii')
                            tag_parts.append(f'CAST("{f}" AS TEXT) LIKE ?')
                            params.append(f"%{v_str}%")
                            tag_parts.append(f'CAST("{f}" AS TEXT) LIKE ?')
                            params.append(f"%{escaped}%")
                        where_parts.append("(" + " OR ".join(tag_parts) + ")")
                    else:
                        placeholders = ",".join(["?"] * len(values))
                        where_parts.append(f'CAST("{f}" AS TEXT) IN ({placeholders})')
                        params += [str(v) for v in values]
            where = (" WHERE " + " AND ".join(where_parts)) if where_parts else ""

            # 构造 ORDER BY
            order = " ORDER BY rowid DESC"
            if sort_field and sort_field in cols:
                direction = "ASC" if sort_order.lower() == "asc" else "DESC"
                order = f' ORDER BY "{sort_field}" {direction}'

            # 总数：need_total=False 时跳过，滚动加载后续批次可复用首批结果。
            # 注意带 LIKE 条件的 COUNT 才是真正的开销（要对同一份 WHERE 再扫一遍全表）。
            total = -1
            if need_total:
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

    def get_distinct_values(self, table: str, field: str) -> list[str]:
        """获取某字段的去重值列表（标签字段会拆开 JSON 数组）。

        带 TTL 缓存：平台/状态这类低基数字段的值几乎不变，
        每次打开筛选菜单都跑一次 DISTINCT 是纯浪费。
        写入类操作会调用 invalidate_distinct_cache() 主动失效。
        """
        cache_key = f"{table}::{field}"
        now = time.monotonic()
        cached = self._distinct_cache.get(cache_key)
        if cached and cached[0] > now:
            return cached[1]

        with self._connect() as conn:
            cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
            if field not in cols:
                return []
            rows = conn.execute(f'SELECT DISTINCT "{field}" FROM {table} WHERE "{field}" IS NOT NULL AND "{field}" != \'\'').fetchall()
            vals = set()
            for row in rows:
                v = row[0]
                if field == "标签":
                    if isinstance(v, str):
                        try:
                            parsed = json.loads(v)
                            if isinstance(parsed, list):
                                v = parsed
                        except (json.JSONDecodeError, TypeError):
                            pass
                    if isinstance(v, list):
                        for item in v:
                            if item:
                                vals.add(str(item))
                    elif v:
                        vals.add(str(v))
                else:
                    vals.add(str(v))
            result = sorted(vals)

        self._distinct_cache[cache_key] = (now + self.DISTINCT_CACHE_TTL, result)
        return result

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
            # 方案 B：同步表自动维护 local_updated_at（用于 LWW 比较）
            if "local_updated_at" in col_names and field != "local_updated_at":
                set_clause += ', "local_updated_at" = ?'
                params.append(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            params.append(record_id)
            cursor = conn.execute(
                f'UPDATE {table} SET {set_clause} WHERE "{pk}" = ?',
                params,
            )
            conn.commit()
            if cursor.rowcount > 0:
                self.invalidate_distinct_cache(table)
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

    # ========== Block 2：批量操作与增强统计 ==========

    def batch_update(self, table: str, record_ids: list[str], updates: dict) -> dict:
        """批量更新多条记录的相同字段。

        Args:
            table: 表名（必须在 VALID_TABLES 中）
            record_ids: 要更新的记录 ID 列表
            updates: {字段名: 值, ...}
        Returns:
            {updated, failed, errors[]}
        """
        if table not in self.VALID_TABLES:
            return {"updated": 0, "failed": 0, "errors": [f"无效的表名: {table}"]}
        schema = self.get_table_schema(table)
        col_names = [s["name"] for s in schema]
        pk_cols = [s["name"] for s in schema if s["pk"]]
        pk = pk_cols[0] if pk_cols else None
        if not pk:
            return {"updated": 0, "failed": len(record_ids), "errors": ["表无主键"]}

        # 过滤掉不在 schema 中的字段
        valid_updates = {k: v for k, v in updates.items() if k in col_names}
        if not valid_updates:
            return {"updated": 0, "failed": 0, "errors": ["无有效字段"]}

        # 自动维护 local_updated_at 和 同步时间
        if "local_updated_at" in col_names and "local_updated_at" not in valid_updates:
            valid_updates["local_updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if "同步时间" in col_names and "同步时间" not in valid_updates:
            valid_updates["同步时间"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        set_clause = ", ".join([f'"{k}" = ?' for k in valid_updates.keys()])
        params_list = list(valid_updates.values())
        result = {"updated": 0, "failed": 0, "errors": []}

        with self._connect() as conn:
            for rid in record_ids:
                try:
                    cursor = conn.execute(
                        f'UPDATE {table} SET {set_clause} WHERE "{pk}" = ?',
                        params_list + [rid],
                    )
                    if cursor.rowcount > 0:
                        result["updated"] += 1
                    else:
                        result["failed"] += 1
                        result["errors"].append(f"{rid}: 记录不存在")
                except Exception as e:
                    result["failed"] += 1
                    result["errors"].append(f"{rid}: {e}")
            conn.commit()
        if result["updated"]:
            self.invalidate_distinct_cache(table)
        return result

    def batch_delete(self, table: str, record_ids: list[str]) -> dict:
        """批量硬删除多条记录。

        Returns:
            {deleted, failed, errors[]}
        """
        if table not in self.VALID_TABLES:
            return {"deleted": 0, "failed": 0, "errors": [f"无效的表名: {table}"]}
        schema = self.get_table_schema(table)
        pk_cols = [s["name"] for s in schema if s["pk"]]
        pk = pk_cols[0] if pk_cols else None
        if not pk:
            return {"deleted": 0, "failed": len(record_ids), "errors": ["表无主键"]}

        result = {"deleted": 0, "failed": 0, "errors": []}

        with self._connect() as conn:
            for rid in record_ids:
                try:
                    cursor = conn.execute(
                        f'DELETE FROM {table} WHERE "{pk}" = ?',
                        (rid,),
                    )
                    if cursor.rowcount > 0:
                        result["deleted"] += 1
                    else:
                        result["failed"] += 1
                        result["errors"].append(f"{rid}: 记录不存在")
                except Exception as e:
                    result["failed"] += 1
                    result["errors"].append(f"{rid}: {e}")
            conn.commit()
        if result["deleted"]:
            self.invalidate_distinct_cache(table)
        return result

    def insert_single(self, table: str, data: dict) -> dict:
        """插入单条记录（通用版）。

        Returns:
            {success, message, record_id?}
        """
        if table not in self.VALID_TABLES:
            return {"success": False, "message": f"无效的表名: {table}"}
        schema = self.get_table_schema(table)
        col_names = [s["name"] for s in schema]
        # 过滤未知字段
        clean = {k: v for k, v in data.items() if k in col_names and v is not None and v != ""}
        if not clean:
            return {"success": False, "message": "无有效字段"}
        # 自动补充 record_id（如果没有提供且有此字段）
        if "record_id" in col_names and "record_id" not in clean:
            clean["record_id"] = f"rec_{datetime.now().strftime('%Y%m%d%H%M%S')}_{id(data) % 10000}"
        try:
            with self._connect() as conn:
                fields = ", ".join([f'"{k}"' for k in clean.keys()])
                placeholders = ", ".join(["?"] * len(clean))
                conn.execute(
                    f'INSERT INTO {table} ({fields}) VALUES ({placeholders})',
                    list(clean.values()),
                )
                conn.commit()
            self.invalidate_distinct_cache(table)
            return {"success": True, "message": "添加成功", "record_id": clean.get("record_id", "")}
        except sqlite3.IntegrityError as e:
            return {"success": False, "message": f"记录已存在或冲突: {e}"}
        except Exception as e:
            return {"success": False, "message": str(e)}

    def update_record(self, table: str, record_id: str, data: dict) -> dict:
        """更新整条记录的多个字段（通用版，行内编辑保存）。

        Returns:
            {success, message}
        """
        if table not in self.VALID_TABLES:
            return {"success": False, "message": f"无效的表名: {table}"}
        schema = self.get_table_schema(table)
        col_names = [s["name"] for s in schema]
        pk_cols = [s["name"] for s in schema if s["pk"]]
        pk = pk_cols[0] if pk_cols else None
        if not pk:
            return {"success": False, "message": "表无主键"}

        valid_updates = {k: v for k, v in data.items() if k in col_names and k != pk}
        if not valid_updates:
            return {"success": False, "message": "无有效字段"}

        if "local_updated_at" in col_names and "local_updated_at" not in valid_updates:
            valid_updates["local_updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if "同步时间" in col_names and "同步时间" not in valid_updates:
            valid_updates["同步时间"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        set_clause = ", ".join([f'"{k}" = ?' for k in valid_updates.keys()])
        params = list(valid_updates.values()) + [record_id]
        try:
            with self._connect() as conn:
                cursor = conn.execute(
                    f'UPDATE {table} SET {set_clause} WHERE "{pk}" = ?',
                    params,
                )
                conn.commit()
                if cursor.rowcount > 0:
                    self.invalidate_distinct_cache(table)
                    return {"success": True, "message": "更新成功"}
                return {"success": False, "message": "记录不存在"}
        except Exception as e:
            return {"success": False, "message": str(e)}

    def get_duplicates(self, table: str) -> list[dict]:
        """检测表中按业务键重复的记录。

        Returns:
            [{business_key, count, records: [{record_id, ...}]}, ...]
        """
        business_keys = {
            "share_cache": "share_code",
            "account_cache": "sec_user_id",
            "cookie_cache": "Cookie",
        }
        bk = business_keys.get(table)
        if not bk:
            return []

        with self._connect() as conn:
            # 找出重复的业务键
            rows = conn.execute(
                f'SELECT "{bk}" as bk_val, COUNT(*) as cnt FROM {table} '
                f'WHERE "{bk}" != "" GROUP BY "{bk}" HAVING cnt > 1'
            ).fetchall()
            results = []
            for row in rows:
                bk_val = row["bk_val"]
                records = conn.execute(
                    f'SELECT * FROM {table} WHERE "{bk}" = ?',
                    (bk_val,),
                ).fetchall()
                results.append({
                    "business_key": bk_val,
                    "count": row["cnt"],
                    "records": [dict(r) for r in records],
                })
            return results

    def get_stats_detailed(self) -> dict:
        """获取各表的详细统计（含同步状态、启用状态等细分）"""
        stats = {}
        with self._connect() as conn:
            # 分享表
            stats["share_cache"] = {
                "total": conn.execute("SELECT COUNT(*) FROM share_cache").fetchone()[0],
                "resolved": conn.execute("SELECT COUNT(*) FROM share_cache WHERE 解析状态='已就绪'").fetchone()[0],
                "not_synced": conn.execute("SELECT COUNT(*) FROM share_cache WHERE 解析状态 != '已就绪'").fetchone()[0],
                "has_error": conn.execute("SELECT COUNT(*) FROM share_cache WHERE 解析状态 = '解析失败'").fetchone()[0],
            }
            # 账号表
            stats["account_cache"] = {
                "total": conn.execute("SELECT COUNT(*) FROM account_cache").fetchone()[0],
                "enabled": conn.execute("SELECT COUNT(*) FROM account_cache WHERE 启用=1").fetchone()[0],
                "disabled": conn.execute("SELECT COUNT(*) FROM account_cache WHERE (启用=0 OR 启用 IS NULL)").fetchone()[0],
                "not_fetched": conn.execute("SELECT COUNT(*) FROM account_cache WHERE 获取状态 != '已获取'").fetchone()[0],
            }
            # Cookie表
            stats["cookie_cache"] = {
                "total": conn.execute("SELECT COUNT(*) FROM cookie_cache").fetchone()[0],
                "normal": conn.execute("SELECT COUNT(*) FROM cookie_cache WHERE 状态='正常'").fetchone()[0],
                "invalid": conn.execute("SELECT COUNT(*) FROM cookie_cache WHERE 状态='失效'").fetchone()[0],
                "enabled": conn.execute("SELECT COUNT(*) FROM cookie_cache WHERE 启用=1").fetchone()[0],
            }
            # 同步历史
            stats["sync_history"] = {
                "total": conn.execute("SELECT COUNT(*) FROM sync_history").fetchone()[0],
            }
        return stats

    # ========== 同步历史操作 ==========

    def add_sync_history(self, data: dict) -> int:
        """添加一条同步历史记录，返回 id"""
        with self._connect() as conn:
            fields = ", ".join(data.keys())
            placeholders = ", ".join(["?" for _ in data])
            cursor = conn.execute(f"INSERT INTO sync_history ({fields}) VALUES ({placeholders})", list(data.values()))
            conn.commit()
            return cursor.lastrowid

    def get_sync_history(self, task_type: Optional[str] = None, limit: int = 50, offset: int = 0) -> list[dict]:
        """获取同步历史记录。可按 task_type 过滤，支持分页。"""
        with self._connect() as conn:
            if task_type:
                rows = conn.execute(
                    "SELECT * FROM sync_history WHERE task_type = ? ORDER BY created_at DESC LIMIT ? OFFSET ?",
                    (task_type, limit, offset)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM sync_history ORDER BY created_at DESC LIMIT ? OFFSET ?",
                    (limit, offset)
                ).fetchall()
            return [dict(row) for row in rows]

    def cleanup_sync_history(self, days: int = 7) -> int:
        """清理 N 天前的同步历史，返回删除条数"""
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM sync_history WHERE created_at < datetime('now', ?)",
                (f"-{days} days",)
            )
            conn.commit()
            return cursor.rowcount
