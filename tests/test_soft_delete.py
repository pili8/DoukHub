"""测试软删除（墓碑）和删除同步逻辑（v2：系统字段英文化）。

v2 字段变化：
- 记录ID → record_id
- 创建时间 → created_at
- 业务字段保持中文，与飞书对齐
"""
import pathlib
import tempfile

import pytest

from app.core.database import Database


@pytest.fixture
def db():
    p = pathlib.Path(tempfile.mkdtemp()) / "test.db"
    return Database(db_path=p)


# ========== schema：is_deleted / deleted_at 字段存在 ==========

def test_sync_tables_have_is_deleted(db):
    """三张同步表都应有 is_deleted 字段"""
    for table in ("collection_cache", "account_cache", "cookie_cache"):
        schema = db.get_table_schema(table)
        names = [c["name"] for c in schema]
        assert "is_deleted" in names, f"{table} 缺 is_deleted"
        assert "deleted_at" in names, f"{table} 缺 deleted_at"


# ========== 系统字段英文（v2）==========

def test_v2_system_fields_exist(db):
    """三张同步表都应有 record_id / created_at 系统字段"""
    for table in ("collection_cache", "account_cache", "cookie_cache"):
        schema = db.get_table_schema(table)
        names = [c["name"] for c in schema]
        assert "record_id" in names, f"{table} 缺 record_id"
        assert "created_at" in names, f"{table} 缺 created_at"
        # 不应有旧字段名
        assert "记录ID" not in names, f"{table} 不应有 记录ID（已 rename 为 record_id）"
        assert "创建时间" not in names, f"{table} 不应有 创建时间（已 rename 为 created_at）"


def test_v2_no_legacy_update_time(db):
    """新库不应自动添加「最后更新时间」字段（v2 已废弃）"""
    for table in ("collection_cache", "account_cache", "cookie_cache"):
        schema = db.get_table_schema(table)
        names = [c["name"] for c in schema]
        # 新建库不应有「最后更新时间」
        assert "最后更新时间" not in names, f"{table} 不应有 最后更新时间"


# ========== 软删除行为 ==========

def test_soft_delete_collection(db):
    """delete_collection 应打墓碑而非真删"""
    db.insert_collection({"record_id": "c1", "share_code": "abc", "等级": 3})
    assert len(db.get_all_collections()) == 1

    db.delete_collection("c1")

    # 列表查不到了
    assert len(db.get_all_collections()) == 0
    # 但 by_id 还查得到（墓碑还在）
    assert db.get_collection_by_id("c1") is not None


def test_soft_delete_account(db):
    db.insert_account({"record_id": "a1", "sec_user_id": "sec1"})
    db.delete_account("a1")
    assert len(db.get_all_accounts()) == 0
    assert db.get_account_by_id("a1") is not None


def test_soft_delete_cookie(db):
    db.insert_cookie({"record_id": "ck1", "Cookie": "x=y"})
    db.delete_cookie("ck1")
    assert len(db.get_all_cookies()) == 0
    assert db.get_cookie_by_id("ck1") is not None


# ========== 墓碑 ID 查询 ==========

def test_get_deleted_ids(db):
    db.insert_collection({"record_id": "c1", "share_code": "aaa", "等级": 3})
    db.insert_collection({"record_id": "c2", "share_code": "bbb", "等级": 3})
    db.insert_collection({"record_id": "c3", "share_code": "ccc", "等级": 3})

    db.delete_collection("c1")
    db.delete_collection("c3")

    deleted = db.get_deleted_ids("collection_cache")
    assert set(deleted) == {"c1", "c3"}


def test_get_active_ids(db):
    db.insert_collection({"record_id": "c1", "share_code": "aaa", "等级": 3})
    db.insert_collection({"record_id": "c2", "share_code": "bbb", "等级": 3})
    db.delete_collection("c1")

    active = db.get_active_ids("collection_cache")
    assert active == ["c2"]


def test_get_deleted_ids_invalid_table(db):
    assert db.get_deleted_ids("unknown_table") == []


def test_get_active_ids_invalid_table(db):
    assert db.get_active_ids("unknown_table") == []


# ========== hard_delete（真删）==========

def test_hard_delete(db):
    db.insert_collection({"record_id": "c1", "share_code": "aaa", "等级": 3})
    db.hard_delete("collection_cache", "c1")
    assert db.get_collection_by_id("c1") is None


def test_hard_delete_invalid_table(db):
    assert db.hard_delete("unknown_table", "c1") is False


# ========== purge_tombstone ==========

def test_purge_tombstone(db):
    db.insert_collection({"record_id": "c1", "share_code": "aaa", "等级": 3})
    db.delete_collection("c1")

    # 墓碑还在
    assert db.get_collection_by_id("c1") is not None
    assert "c1" in db.get_deleted_ids("collection_cache")

    db.purge_tombstone("collection_cache", "c1")
    assert db.get_collection_by_id("c1") is None
    assert "c1" not in db.get_deleted_ids("collection_cache")


def test_purge_tombstone_only_deletes_marked(db):
    """purge_tombstone 不应删除未标记删除的记录"""
    db.insert_collection({"record_id": "c1", "share_code": "aaa", "等级": 3})
    db.purge_tombstone("collection_cache", "c1")
    # 正常记录不受影响
    assert db.get_collection_by_id("c1") is not None


# ========== 全流程：软删 → 墓碑 → 清理 ==========

def test_full_soft_delete_lifecycle(db):
    db.insert_account({"record_id": "a1", "sec_user_id": "sec1"})

    # 1. 正常存在
    assert "a1" in db.get_active_ids("account_cache")
    assert "a1" not in db.get_deleted_ids("account_cache")

    # 2. 软删除
    db.delete_account("a1")
    assert "a1" not in db.get_active_ids("account_cache")
    assert "a1" in db.get_deleted_ids("account_cache")

    # 3. 清理墓碑
    db.purge_tombstone("account_cache", "a1")
    assert "a1" not in db.get_deleted_ids("account_cache")
    assert db.get_account_by_id("a1") is None


# ========== synced 标记 ==========

def test_synced_field_exists(db):
    """三张同步表都应有 synced 字段"""
    for table in ("collection_cache", "account_cache", "cookie_cache"):
        schema = db.get_table_schema(table)
        names = [c["name"] for c in schema]
        assert "synced" in names, f"{table} 缺 synced"


def test_new_record_not_synced_by_default(db):
    """本地新建的记录默认 synced=0"""
    db.insert_collection({"record_id": "local_rec1", "share_code": "aaa", "等级": 3})
    synced_ids = db.get_synced_active_ids("collection_cache")
    assert "local_rec1" not in synced_ids


def test_synced_record_appears_in_synced_ids(db):
    """标记为 synced=1 的记录出现在 get_synced_active_ids 中"""
    db.insert_collection({"record_id": "rec1", "share_code": "aaa", "等级": 3, "synced": True})
    synced_ids = db.get_synced_active_ids("collection_cache")
    assert "rec1" in synced_ids


def test_unsynced_record_not_in_synced_ids(db):
    """synced=0 的记录不出现在 get_synced_active_ids 中"""
    db.insert_collection({"record_id": "rec1", "share_code": "aaa", "等级": 3})
    db.insert_collection({"record_id": "rec2", "share_code": "bbb", "等级": 3, "synced": True})
    synced_ids = db.get_synced_active_ids("collection_cache")
    assert "rec1" not in synced_ids
    assert "rec2" in synced_ids


def test_deleted_record_not_in_synced_ids(db):
    """已删除的记录不出现在 get_synced_active_ids 中"""
    db.insert_collection({"record_id": "rec1", "share_code": "aaa", "等级": 3, "synced": True})
    db.delete_collection("rec1")
    synced_ids = db.get_synced_active_ids("collection_cache")
    assert "rec1" not in synced_ids


def test_synced_lifecycle(db):
    """模拟完整生命周期：新建 → 推送标记 → 飞书删除 → 本地删除"""
    # 1. 本地新建（synced=0），不在删除检测范围内
    db.insert_account({"record_id": "acc_local_1", "sec_user_id": "sec1"})
    assert "acc_local_1" not in db.get_synced_active_ids("account_cache")

    # 2. 推送成功，标记为 synced=1
    db.update_account("acc_local_1", {"synced": True})
    assert "acc_local_1" in db.get_synced_active_ids("account_cache")

    # 3. 飞书删除 → 删除检测发现它是孤儿
    assert "acc_local_1" in db.get_synced_active_ids("account_cache")

    # 4. 本地 hard_delete
    db.hard_delete("account_cache", "acc_local_1")
    assert db.get_account_by_id("acc_local_1") is None


# ========== 迁移：旧库自动 rename + 标记 synced ==========

def test_migration_v2_rename_legacy_columns():
    """v1 旧库（含「记录ID」「创建时间」）迁移后应自动 rename 为 record_id / created_at"""
    import sqlite3
    p = pathlib.Path(tempfile.mkdtemp()) / "v1.db"

    # 模拟 v1 旧库
    with sqlite3.connect(str(p)) as conn:
        conn.executescript("""
            CREATE TABLE collection_cache (
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
                同步时间 DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            INSERT INTO collection_cache(记录ID, 分享码, 等级) VALUES('rec1', 'aaa', 3);
        """)

    # 初始化 Database，触发迁移
    d = Database(db_path=p)

    # 应自动 rename
    schema = d.get_table_schema("collection_cache")
    names = [c["name"] for c in schema]
    assert "record_id" in names, "应自动 rename 记录ID → record_id"
    assert "created_at" in names, "应自动 rename 创建时间 → created_at"
    assert "记录ID" not in names
    assert "创建时间" not in names

    # 数据应保留
    rec = d.get_collection_by_id("rec1")
    assert rec is not None
    assert rec["share_code"] == "aaa"


def test_migration_marks_existing_synced():
    """旧库迁移后，已有的记录应自动标记为 synced=1"""
    import sqlite3
    p = pathlib.Path(tempfile.mkdtemp()) / "old.db"

    # 模拟 v1 完整旧库（包含所有索引需要的字段）
    with sqlite3.connect(str(p)) as conn:
        conn.executescript("""
            CREATE TABLE collection_cache (
                记录ID TEXT PRIMARY KEY,
                分享码 TEXT UNIQUE NOT NULL,
                平台 TEXT,
                等级 INTEGER,
                sec_user_id TEXT,
                创建时间 DATETIME DEFAULT CURRENT_TIMESTAMP,
                同步时间 DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            INSERT INTO collection_cache(记录ID, 分享码, 等级) VALUES('rec1', 'aaa', 3);
        """)

    d = Database(db_path=p)
    synced_ids = d.get_synced_active_ids("collection_cache")
    assert "rec1" in synced_ids, "旧记录应在迁移时被标记为 synced=1"
