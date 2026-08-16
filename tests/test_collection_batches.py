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


def test_create_collection_batch_preserves_planned_status_and_message(db):
    db.create_collection_batch(
        batch_id="batch1",
        filter_json="{}",
        platform="tiktok",
        log_path="/tmp/batch1.log",
        items=[
            {
                "sec_user_id": "tiksec",
                "status": "skipped",
                "message": "TikTok 主页链接缺失",
            }
        ],
    )

    item = db.find_collection_batch_item("batch1", "tiksec")
    assert item["status"] == "skipped"
    assert item["message"] == "TikTok 主页链接缺失"


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


def test_single_work_history_create_get_update_and_list(db):
    history_id = db.create_single_work_history(
        work_id="1234567890123456789",
        source_link="https://www.douyin.com/video/1234567890123456789",
        platform="douyin",
        work_type="图集",
        title="标题",
        author="作者",
        filename_template="{author} {title}",
        filename_override="",
        target_dir="/tmp/works",
        request_json='{}',
    )
    assert history_id > 0

    row = db.get_single_work_history(history_id)
    assert row["work_id"] == "1234567890123456789"
    assert row["status"] == "running"
    assert row["title"] == "标题"
    assert row["author"] == "作者"
    assert row["filename_template"] == "{author} {title}"
    assert row["target_dir"] == "/tmp/works"

    assert db.update_single_work_history(
        history_id,
        status="success",
        files_json='["/tmp/works/a.jpg"]',
        work_json='{"id":"1234567890123456789"}',
    )
    row = db.get_single_work_history(history_id)
    assert row["status"] == "success"
    assert row["files_json"] == '["/tmp/works/a.jpg"]'
    assert row["error"] is None

    second_id = db.create_single_work_history(
        work_id="2",
        source_link="https://www.tiktok.com/@user/video/2",
        platform="tiktok",
        work_type="视频",
        title="second",
        author="user",
        filename_template="{title}",
        filename_override="",
        target_dir="/tmp/works",
        request_json='{}',
    )
    rows = db.list_single_work_history(limit=10)
    assert [row["id"] for row in rows] == [second_id, history_id]


def test_update_single_work_history_rejects_unknown_field(db):
    history_id = db.create_single_work_history(
        work_id="1",
        source_link="",
        platform="douyin",
        work_type="",
        title="",
        author="",
        filename_template="",
        filename_override="",
        target_dir="",
        request_json='{}',
    )
    assert not db.update_single_work_history(history_id, unknown_field="x")
