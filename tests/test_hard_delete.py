"""硬删除行为测试（原软删除已移除，删除即真删，靠自动备份兜底）。"""
import pathlib
import tempfile

import pytest

from app.core.database import Database


@pytest.fixture
def db():
    p = pathlib.Path(tempfile.mkdtemp()) / "test.db"
    return Database(db_path=p)


def test_hard_delete_collection(db):
    db.insert_collection({"record_id": "c1", "share_code": "abc", "等级": 3})
    assert len(db.get_all_collections()) == 1

    db.delete_collection("c1")
    assert len(db.get_all_collections()) == 0
    assert db.get_collection_by_id("c1") is None
    assert db.get_collection_by_share("abc") is None


def test_hard_delete_allows_reinsert(db):
    """删除后相同业务键可重新插入（不再占 UNIQUE 位置）。"""
    db.insert_account({"record_id": "a1", "sec_user_id": "sec1"})
    db.delete_account("a1")
    # 重新插入相同 sec_user_id 不应撞唯一键
    db.insert_account({"record_id": "a2", "sec_user_id": "sec1"})
    assert len(db.get_all_accounts()) == 1
    assert db.get_all_accounts()[0]["record_id"] == "a2"


def test_hard_delete_cookie(db):
    db.insert_cookie({"record_id": "ck1", "Cookie": "x=y"})
    db.delete_cookie("ck1")
    assert len(db.get_all_cookies()) == 0
    assert db.get_cookie_by_id("ck1") is None


def test_batch_delete_is_hard(db):
    db.insert_collection({"record_id": "c1", "share_code": "aaa", "等级": 3})
    db.insert_collection({"record_id": "c2", "share_code": "bbb", "等级": 3})
    db.batch_delete("share_cache", ["c1", "c2"])
    assert len(db.get_all_collections()) == 0


def test_hard_delete_migration_cleans_tombstones(db):
    """启动迁移自动清理旧软删除遗留的墓碑。"""
    db.insert_collection({"record_id": "c1", "share_code": "aaa", "等级": 3})
    # 模拟旧数据：直接在库里标记墓碑
    with db._connect() as conn:
        conn.execute("UPDATE share_cache SET is_deleted = 1 WHERE record_id = 'c1'")
        conn.commit()

    # 重新初始化触发迁移
    Database(db_path=db.db_path)
    assert len(db.get_all_collections()) == 0


def test_get_deleted_ids_returns_empty(db):
    """硬删除模式下不再有墓碑列表。"""
    db.insert_collection({"record_id": "c1", "share_code": "aaa", "等级": 3})
    assert db.get_deleted_ids("share_cache") == []


def test_get_active_ids_returns_all(db):
    db.insert_collection({"record_id": "c1", "share_code": "aaa", "等级": 3})
    assert db.get_active_ids("share_cache") == ["c1"]


def test_get_synced_active_ids(db):
    db.insert_collection({"record_id": "c1", "share_code": "aaa", "等级": 3, "synced": True})
    db.insert_collection({"record_id": "c2", "share_code": "bbb", "等级": 3})
    synced = db.get_synced_active_ids("share_cache")
    assert "c1" in synced
    assert "c2" not in synced
