import pathlib
import tempfile

import pytest

from app.core.database import Database


@pytest.fixture
def db():
    path = pathlib.Path(tempfile.mkdtemp()) / "doukhub.db"
    return Database(db_path=path)


def test_collection_account_fields_are_added(db):
    names = {c["name"] for c in db.get_table_schema("account_cache")}
    assert "last_collected_at" in names
    assert "collect_window_days" in names


def test_existing_account_database_is_migrated(tmp_path):
    import sqlite3

    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
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
                已获取信息 BOOLEAN DEFAULT 0,
                获取错误 TEXT,
                同步时间 DATETIME,
                created_at DATETIME,
                is_deleted BOOLEAN DEFAULT 0,
                deleted_at DATETIME,
                synced BOOLEAN DEFAULT 0,
                local_updated_at DATETIME
            )
            """
        )
        conn.execute(
            "INSERT INTO account_cache(record_id, sec_user_id) VALUES ('a1', 'sec1')"
        )

    database = Database(db_path=path)
    account = database.get_account_by_id("a1")
    assert account["last_collected_at"] is None
    assert account["collect_window_days"] is None


def test_create_and_query_collection_batch(db):
    items = [
        {
            "account_record_id": "a1",
            "sec_user_id": "sec1",
            "account_name": "一号",
            "platform": "douyin",
            "mark": "一号",
            "url": "https://www.douyin.com/user/sec1",
            "earliest": "",
        },
        {
            "account_record_id": "a2",
            "sec_user_id": "sec2",
            "account_name": "二号",
            "platform": "douyin",
            "mark": "二号",
            "url": "https://www.douyin.com/user/sec2",
            "earliest": "2026/08/01",
        },
    ]
    db.create_collection_batch(
        batch_id="batch1",
        filter_json='{"rating_min":3}',
        platform="douyin",
        log_path="/tmp/batch1.log",
        items=items,
    )

    batch = db.get_collection_batch("batch1")
    assert batch["status"] == "pending"
    assert batch["total_accounts"] == 2
    assert batch["process_pid"] is None

    queried = db.get_collection_batch_items("batch1")
    assert [row["sec_user_id"] for row in queried] == ["sec1", "sec2"]
    assert queried[1]["earliest"] == "2026/08/01"
    assert db.find_collection_batch_item("batch1", "sec2")["account_name"] == "二号"


def test_update_batch_and_refresh_counts(db):
    db.create_collection_batch(
        batch_id="batch1",
        filter_json="{}",
        platform="douyin",
        log_path="/tmp/batch1.log",
        items=[
            {
                "account_record_id": "a1",
                "sec_user_id": "sec1",
                "account_name": "一号",
                "platform": "douyin",
                "mark": "一号",
                "url": "https://www.douyin.com/user/sec1",
                "earliest": "",
            }
        ],
    )
    item = db.find_collection_batch_item("batch1", "sec1")
    assert db.update_collection_batch_item(item["id"], status="success", message="OK")
    assert db.update_collection_batch("batch1", status="running", process_pid=123)

    counts = db.refresh_collection_batch_counts("batch1")
    assert counts == {"success": 1, "failed": 0, "skipped": 0}
    assert db.get_collection_batch("batch1")["success_accounts"] == 1
    assert db.get_active_collection_batch()["id"] == "batch1"


def test_list_batches_orders_newest_first(db):
    for index in range(2):
        db.create_collection_batch(
            batch_id=f"batch{index}",
            filter_json="{}",
            platform="douyin",
            log_path=f"/tmp/batch{index}.log",
            items=[],
        )
    assert [batch["id"] for batch in db.list_collection_batches()] == [
        "batch1",
        "batch0",
    ]
