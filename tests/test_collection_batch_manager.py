import asyncio

import pytest

from app.core.collection_batch_manager import CollectionBatchManager
from app.core.database import Database


class FakeStream:
    def __init__(self, lines):
        self.lines = [line.encode("utf-8") for line in lines]

    async def readline(self):
        if not self.lines:
            return b""
        return self.lines.pop(0)


class FakeProcess:
    def __init__(self, lines, returncode=0):
        self.stdout = FakeStream(lines)
        self.returncode = None
        self._returncode = returncode
        self.pid = 12345
        self.terminated = False

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    async def wait(self):
        if self.returncode is None:
            self.returncode = self._returncode
        return self.returncode


@pytest.fixture
def db(tmp_path):
    return Database(db_path=tmp_path / "doukhub.db")


@pytest.fixture
def manager(db, tmp_path, monkeypatch):
    instance = CollectionBatchManager(
        database=db,
        ttd_path=tmp_path / "TikTokDownloader",
        log_dir=tmp_path / "logs",
        ttd_url="http://127.0.0.1:5555",
    )
    monkeypatch.setattr(instance, "_check_ttd_api", lambda: asyncio.sleep(0))
    try:
        yield instance
    finally:
        if instance._worker:
            instance._worker.cancel()


def insert_douyin_account(db):
    db.insert_account(
        {
            "record_id": "a1",
            "sec_user_id": "sec1",
            "账号名称": "一号",
            "平台": "抖音",
            "等级": 4,
            "启用": 1,
        }
    )


def test_marker_updates_item_account_and_counts(db, manager):
    insert_douyin_account(db)
    batches = asyncio.run(
        manager.start(
            db.get_all_accounts(),
            rating_min=3,
            platforms=("douyin",),
            mode="incremental",
        )
    )
    batch_id = batches[0]["id"]
    item = db.find_collection_batch_item(batch_id, "sec1")

    marker = {
        "type": "account_result",
        "sec_user_id": "sec1",
        "status": "success",
        "message": "OK",
    }
    assert manager._apply_marker(batch_id, marker)
    assert db.get_collection_batch_item_by_id(item["id"])["status"] == "success"
    assert db.get_account_by_id("a1")["last_collected_at"] is not None

    counts = manager._finalize(batch_id, "completed", 0)
    assert counts["success"] == 1
    assert db.get_collection_batch(batch_id)["status"] == "completed"


def test_run_batch_uses_ttd_process_and_persists_log(db, manager, monkeypatch):
    insert_douyin_account(db)
    batches = asyncio.run(
        manager.start(
            db.get_all_accounts(),
            rating_min=3,
            platforms=("douyin",),
            mode="incremental",
        )
    )
    batch_id = batches[0]["id"]

    async def fake_launch(command, cwd):
        return FakeProcess(
            [
                "TTD raw output",
                '__DOUKHUB__{"type":"account_result","sec_user_id":"sec1","status":"success","message":"OK"}',
            ],
            returncode=0,
        )

    monkeypatch.setattr(manager, "_launch_process", fake_launch)
    result = asyncio.run(manager._run_batch(batch_id))
    assert result == "completed"
    assert "TTD raw output" in manager.read_log(batch_id)
    assert db.get_collection_batch(batch_id)["process_pid"] == 12345


def test_interrupted_batches_are_recovered(db, manager):
    db.create_collection_batch(
        batch_id="old",
        filter_json="{}",
        platform="douyin",
        log_path="",
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
    db.update_collection_batch("old", status="running", process_pid=999)
    manager.recover_interrupted_batches()

    assert db.get_collection_batch("old")["status"] == "failed"
    assert db.get_collection_batch_items("old")[0]["status"] == "failed"
