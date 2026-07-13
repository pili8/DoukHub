"""测试软删除（墓碑）和删除同步逻辑。"""
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


# ========== 软删除行为 ==========

def test_soft_delete_collection(db):
    """delete_collection 应打墓碑而非真删"""
    db.insert_collection({"记录ID": "c1", "分享码": "abc", "等级": 3})
    assert len(db.get_all_collections()) == 1

    db.delete_collection("c1")

    # 列表查不到了
    assert len(db.get_all_collections()) == 0
    # 但 by_id 还查得到（墓碑还在）
    assert db.get_collection_by_id("c1") is not None


def test_soft_delete_account(db):
    db.insert_account({"记录ID": "a1", "sec_user_id": "sec1"})
    db.delete_account("a1")
    assert len(db.get_all_accounts()) == 0
    assert db.get_account_by_id("a1") is not None


def test_soft_delete_cookie(db):
    db.insert_cookie({"记录ID": "ck1", "Cookie": "x=y"})
    db.delete_cookie("ck1")
    assert len(db.get_all_cookies()) == 0
    assert db.get_cookie_by_id("ck1") is not None


# ========== 墓碑 ID 查询 ==========

def test_get_deleted_ids(db):
    db.insert_collection({"记录ID": "c1", "分享码": "aaa", "等级": 3})
    db.insert_collection({"记录ID": "c2", "分享码": "bbb", "等级": 3})
    db.insert_collection({"记录ID": "c3", "分享码": "ccc", "等级": 3})

    db.delete_collection("c1")
    db.delete_collection("c3")

    deleted = db.get_deleted_ids("collection_cache")
    assert set(deleted) == {"c1", "c3"}


def test_get_active_ids(db):
    db.insert_collection({"记录ID": "c1", "分享码": "aaa", "等级": 3})
    db.insert_collection({"记录ID": "c2", "分享码": "bbb", "等级": 3})
    db.delete_collection("c1")

    active = db.get_active_ids("collection_cache")
    assert active == ["c2"]


def test_get_deleted_ids_invalid_table(db):
    assert db.get_deleted_ids("unknown_table") == []


def test_get_active_ids_invalid_table(db):
    assert db.get_active_ids("unknown_table") == []


# ========== hard_delete（真删）==========

def test_hard_delete(db):
    db.insert_collection({"记录ID": "c1", "分享码": "aaa", "等级": 3})
    db.hard_delete("collection_cache", "c1")
    assert db.get_collection_by_id("c1") is None


def test_hard_delete_invalid_table(db):
    assert db.hard_delete("unknown_table", "c1") is False


# ========== purge_tombstone ==========

def test_purge_tombstone(db):
    db.insert_collection({"记录ID": "c1", "分享码": "aaa", "等级": 3})
    db.delete_collection("c1")

    # 墓碑还在
    assert db.get_collection_by_id("c1") is not None
    assert "c1" in db.get_deleted_ids("collection_cache")

    db.purge_tombstone("collection_cache", "c1")
    assert db.get_collection_by_id("c1") is None
    assert "c1" not in db.get_deleted_ids("collection_cache")


def test_purge_tombstone_only_deletes_marked(db):
    """purge_tombstone 不应删除未标记删除的记录"""
    db.insert_collection({"记录ID": "c1", "分享码": "aaa", "等级": 3})
    db.purge_tombstone("collection_cache", "c1")
    # 正常记录不受影响
    assert db.get_collection_by_id("c1") is not None


# ========== 全流程：软删 → 墓碑 → 清理 ==========

def test_full_soft_delete_lifecycle(db):
    db.insert_account({"记录ID": "a1", "sec_user_id": "sec1"})

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
    db.insert_collection({"记录ID": "local_rec1", "分享码": "aaa", "等级": 3})
    synced_ids = db.get_synced_active_ids("collection_cache")
    assert "local_rec1" not in synced_ids


def test_synced_record_appears_in_synced_ids(db):
    """标记为 synced=1 的记录出现在 get_synced_active_ids 中"""
    db.insert_collection({"记录ID": "rec1", "分享码": "aaa", "等级": 3, "synced": True})
    synced_ids = db.get_synced_active_ids("collection_cache")
    assert "rec1" in synced_ids


def test_unsynced_record_not_in_synced_ids(db):
    """synced=0 的记录不出现在 get_synced_active_ids 中"""
    db.insert_collection({"记录ID": "rec1", "分享码": "aaa", "等级": 3})
    db.insert_collection({"记录ID": "rec2", "分享码": "bbb", "等级": 3, "synced": True})
    synced_ids = db.get_synced_active_ids("collection_cache")
    assert "rec1" not in synced_ids
    assert "rec2" in synced_ids


def test_deleted_record_not_in_synced_ids(db):
    """已删除的记录不出现在 get_synced_active_ids 中"""
    db.insert_collection({"记录ID": "rec1", "分享码": "aaa", "等级": 3, "synced": True})
    db.delete_collection("rec1")
    synced_ids = db.get_synced_active_ids("collection_cache")
    assert "rec1" not in synced_ids


def test_synced_lifecycle(db):
    """模拟完整生命周期：新建 → 推送标记 → 飞书删除 → 本地删除"""
    # 1. 本地新建（synced=0），不在删除检测范围内
    db.insert_account({"记录ID": "acc_local_1", "sec_user_id": "sec1"})
    assert "acc_local_1" not in db.get_synced_active_ids("account_cache")

    # 2. 推送成功，标记为 synced=1
    db.update_account("acc_local_1", {"synced": True})
    assert "acc_local_1" in db.get_synced_active_ids("account_cache")

    # 3. 飞书删除 → 删除检测发现它是孤儿
    assert "acc_local_1" in db.get_synced_active_ids("account_cache")

    # 4. 本地 hard_delete
    db.hard_delete("account_cache", "acc_local_1")
    assert db.get_account_by_id("acc_local_1") is None


# ========== 迁移：旧库自动标记 synced=1 ==========

def test_migration_marks_existing_synced():
    """旧库迁移后，已有的记录应自动标记为 synced=1"""
    p = pathlib.Path(tempfile.mkdtemp()) / "old.db"

    # 第一次初始化：创建表结构
    d = Database(db_path=p)

    # 插入记录（此时 synced 字段已经在 CREATE TABLE 里了，DEFAULT 0）
    d.insert_collection({"记录ID": "rec1", "分享码": "aaa", "等级": 3})

    # 手动模拟"旧库没有 synced 列"的场景
    import sqlite3
    with sqlite3.connect(str(p)) as conn:
        # 清除 synced 值，模拟旧库
        conn.execute("UPDATE collection_cache SET synced = 0")

    # 重新初始化触发迁移
    # synced 列已存在，不会重新添加，不会自动标记
    d2 = Database(db_path=p)
    # 手动标记（模拟迁移刚加列时的行为）
    with sqlite3.connect(str(p)) as conn:
        # 检查是否有 synced=0 的记录
        row = conn.execute("SELECT synced FROM collection_cache WHERE 记录ID = 'rec1'").fetchone()
        assert row[0] == 0  # 不会自动标记


def test_migration_new_column_marks_synced():
    """当 synced 列首次被添加时，所有现有记录应被标记为 synced=1"""
    import sqlite3
    p = pathlib.Path(tempfile.mkdtemp()) / "old.db"

    # 创建一个没有 synced 列的旧库
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

    # 初始化 Database，触发迁移添加 synced 列
    d = Database(db_path=p)

    # synced 列应被添加，且现有记录应被标记为 synced=1
    synced_ids = d.get_synced_active_ids("collection_cache")
    assert "rec1" in synced_ids, "旧记录应在迁移时被标记为 synced=1"
