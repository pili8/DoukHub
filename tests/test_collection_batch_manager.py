import asyncio
import json
import sys
from datetime import datetime as real_datetime

import pytest

from app.core.collection_batch_manager import CollectionBatchManager
from app.core.database import Database
from app.core.ttd_batch_runner import marker_line


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


class FailingStream:
    async def readline(self):
        raise RuntimeError("stdout closed")


class CrashingProcess:
    def __init__(self):
        self.stdout = FailingStream()
        self.returncode = None
        self.pid = 54321
        self.terminated = False

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    async def wait(self):
        if self.returncode is None:
            self.returncode = 1
        return self.returncode


class LaunchRaceProcess:
    def __init__(self):
        self.stdout = FailingStream()
        self.returncode = None
        self.pid = 64321
        self.terminated = False
        self.wait_count = 0

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    async def wait(self):
        self.wait_count += 1
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


def create_running_batch(db, batch_id, log_path="", sec_user_id="sec1"):
    db.create_collection_batch(
        batch_id=batch_id,
        filter_json="{}",
        platform="douyin",
        log_path=log_path,
        items=[
            {
                "account_record_id": "a1",
                "sec_user_id": sec_user_id,
                "account_name": sec_user_id,
                "platform": "douyin",
                "mark": sec_user_id,
                "url": f"https://www.douyin.com/user/{sec_user_id}",
                "earliest": "",
            }
        ],
    )
    db.update_collection_batch(batch_id, status="running")


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
    db.update_collection_batch(batch_id, started_at="2026-08-15 10:00:00")
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


def test_marker_success_uses_persisted_batch_start_date(
    db, manager, monkeypatch
):
    insert_douyin_account(db)
    create_running_batch(db, "midnight")
    db.update_collection_batch(
        "midnight", started_at="2026-08-14 23:59:59"
    )

    class FixedDatetime:
        @staticmethod
        def now():
            return real_datetime(2026, 8, 15, 0, 0, 1)

    monkeypatch.setattr(
        "app.core.collection_batch_manager.datetime", FixedDatetime
    )

    assert manager._apply_marker(
        "midnight",
        {
            "type": "account_result",
            "sec_user_id": "sec1",
            "status": "success",
            "message": "OK",
        },
    )
    assert (
        db.get_account_by_id("a1")["last_collected_at"] == "2026-08-14"
    )


def test_launch_process_forces_utf8_for_non_ascii_markers(
    manager, monkeypatch, tmp_path
):
    monkeypatch.setenv("PYTHONIOENCODING", "gbk")
    command = [
        sys.executable,
        "-c",
        (
            "import json; print('__DOUKHUB__' + json.dumps("
            "{'type': 'account_result', 'sec_user_id': 'sec1', "
            "'account_name': '一号', 'status': 'success'}, ensure_ascii=False))"
        ),
    ]

    async def read_marker():
        process = await manager._launch_process(command, tmp_path)
        raw = await process.stdout.readline()
        await process.wait()
        return marker_line(raw.decode("utf-8", errors="replace").rstrip("\r\n"))

    marker = asyncio.run(read_marker())
    assert marker["account_name"] == "一号"


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


def test_start_recovers_verified_surviving_runner_before_new_batch(
    db, manager, monkeypatch
):
    create_running_batch(db, "stale")
    db.update_collection_batch("stale", process_pid=999)
    verified = []
    terminated = []
    waits = []

    def fake_verify(pid):
        verified.append(pid)
        return True

    def fake_terminate(pid):
        terminated.append(pid)
        return True

    def fake_wait(pid, timeout):
        waits.append((pid, timeout))
        return True

    monkeypatch.setattr(manager, "_verify_recorded_runner", fake_verify)
    monkeypatch.setattr(manager, "_terminate_recorded_runner", fake_terminate)
    monkeypatch.setattr(manager, "_wait_for_recorded_runner", fake_wait)
    monkeypatch.setattr(
        manager,
        "_launch_process",
        lambda command, cwd: asyncio.sleep(0, result=FakeProcess([], returncode=0)),
    )
    insert_douyin_account(db)

    batches = asyncio.run(
        manager.start(
            db.get_all_accounts(),
            rating_min=3,
            platforms=("douyin",),
            mode="incremental",
        )
    )

    assert verified == [999]
    assert terminated == [999]
    assert waits == [(999, manager._recovery_wait_timeout)]
    assert db.get_collection_batch("stale")["status"] == "failed"
    assert db.get_collection_batch(batches[0]["id"])["status"] == "pending"


def test_recovery_does_not_terminate_unverified_runner(db, manager, monkeypatch):
    create_running_batch(db, "stale")
    db.update_collection_batch("stale", process_pid=999)
    terminated = []
    monkeypatch.setattr(manager, "_verify_recorded_runner", lambda pid: False)
    monkeypatch.setattr(
        manager, "_terminate_recorded_runner", terminated.append
    )

    manager.recover_interrupted_batches()

    assert terminated == []
    assert db.get_collection_batch("stale")["status"] == "failed"


@pytest.mark.parametrize(
    ("terminate_result", "wait_result"),
    [(False, None), (True, False)],
)
def test_start_blocked_when_recorded_runner_exit_is_unconfirmed(
    db, manager, monkeypatch, terminate_result, wait_result
):
    create_running_batch(db, "stale")
    db.update_collection_batch("stale", process_pid=999)
    monkeypatch.setattr(manager, "_verify_recorded_runner", lambda pid: True)
    monkeypatch.setattr(
        manager, "_terminate_recorded_runner", lambda pid: terminate_result
    )
    monkeypatch.setattr(
        manager,
        "_wait_for_recorded_runner",
        lambda pid, timeout: wait_result
        if wait_result is not None
        else pytest.fail("termination was not requested"),
    )
    insert_douyin_account(db)

    with pytest.raises(RuntimeError, match="recorded runner"):
        asyncio.run(
            manager.start(
                db.get_all_accounts(),
                rating_min=3,
                platforms=("douyin",),
                mode="incremental",
            )
        )

    assert db.get_collection_batch("stale")["status"] == "running"
    assert db.get_active_collection_batch()["id"] == "stale"
    assert manager._queue.empty()


def test_recovery_handles_more_than_one_hundred_active_batches(db, manager):
    for index in range(101):
        db.create_collection_batch(
            batch_id=f"old-{index}",
            filter_json="{}",
            platform="douyin",
            log_path="",
            items=[],
        )

    manager.recover_interrupted_batches()

    statuses = [batch["status"] for batch in db.list_collection_batches(limit=101)]
    assert set(statuses) == {"cancelled"}
    assert db.get_active_collection_batch() is None


def test_run_batch_persists_skipped_tiktok_and_numeric_earliest(
    db, manager, monkeypatch
):
    db.insert_account(
        {
            "record_id": "tik1",
            "sec_user_id": "tiksec1",
            "账号名称": "一号",
            "平台": "TikTok",
            "链接": "",
            "等级": 4,
            "启用": 1,
        }
    )
    db.insert_account(
        {
            "record_id": "tik2",
            "sec_user_id": "tiksec2",
            "账号名称": "二号",
            "平台": "TikTok",
            "链接": "https://www.tiktok.com/@two",
            "等级": 4,
            "启用": 1,
            "collect_window_days": 200,
        }
    )
    batches = asyncio.run(
        manager.start(
            db.get_all_accounts(),
            rating_min=3,
            platforms=("tiktok",),
            mode="incremental",
        )
    )
    batch_id = batches[0]["id"]
    items = {item["sec_user_id"]: item for item in db.get_collection_batch_items(batch_id)}
    assert items["tiksec1"]["status"] == "skipped"
    assert items["tiksec1"]["message"] == "TikTok 主页链接缺失"
    assert items["tiksec2"]["earliest"] == "200"

    async def fake_launch(command, cwd):
        return FakeProcess(
            [
                '__DOUKHUB__{"type":"account_result","sec_user_id":"tiksec2","status":"success","message":"OK"}'
            ],
            returncode=0,
        )

    monkeypatch.setattr(manager, "_launch_process", fake_launch)
    result = asyncio.run(manager._run_batch(batch_id))
    settings_path = manager.ttd_path / "Volume" / "settings.json"
    entries = json.loads(settings_path.read_text(encoding="utf-8"))[
        "accounts_urls_tiktok"
    ]

    assert result == "completed"
    assert [entry["url"] for entry in entries] == [
        "https://www.tiktok.com/@two"
    ]
    assert type(entries[0]["earliest"]) is int
    assert entries[0]["earliest"] == 200


def test_launch_failure_fails_batch_and_clears_active_refs(
    db, manager, monkeypatch, tmp_path
):
    create_running_batch(db, "broken", str(tmp_path / "broken.log"))

    async def failing_launch(command, cwd):
        raise RuntimeError("launch failed")

    monkeypatch.setattr(manager, "_launch_process", failing_launch)

    result = asyncio.run(manager._run_batch("broken"))

    assert result == "failed"
    assert db.get_collection_batch("broken")["status"] == "failed"
    assert db.get_collection_batch_items("broken")[0]["status"] == "failed"
    assert manager._active_batch_id is None
    assert manager._active_process is None


def test_cancellation_during_preparation_is_not_overwritten(db, manager, monkeypatch):
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

    async def cancel_during_check():
        manager.cancel(batch_id)

    async def fail_launch(command, cwd):
        raise AssertionError("cancelled batch must not launch TTD")

    monkeypatch.setattr(manager, "_check_ttd_api", cancel_during_check)
    monkeypatch.setattr(manager, "_launch_process", fail_launch)

    result = asyncio.run(manager._run_batch(batch_id))

    assert result == "cancelled"
    assert db.get_collection_batch(batch_id)["status"] == "cancelled"


def test_cancellation_during_launch_terminates_child_before_output(
    db, manager, monkeypatch
):
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
    process = LaunchRaceProcess()

    async def fake_launch(command, cwd):
        manager.cancel(batch_id)
        return process

    monkeypatch.setattr(manager, "_launch_process", fake_launch)
    result = asyncio.run(manager._run_batch(batch_id))

    assert result == "cancelled"
    assert process.terminated
    assert process.wait_count == 1
    assert db.get_collection_batch(batch_id)["status"] == "cancelled"


def test_shutdown_during_launch_terminates_child_before_output(
    db, manager, monkeypatch
):
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
    process = LaunchRaceProcess()
    shutdown_task = None

    async def fake_launch(command, cwd):
        nonlocal shutdown_task
        shutdown_task = asyncio.create_task(manager.shutdown())
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return process

    monkeypatch.setattr(manager, "_launch_process", fake_launch)
    result = asyncio.run(manager._run_batch(batch_id))

    assert result == "cancelled"
    assert process.terminated
    assert process.wait_count == 1
    assert shutdown_task.done()
    assert db.get_collection_batch(batch_id)["status"] == "cancelled"


def test_worker_survives_read_failure_and_processes_next_batch(
    db, manager, monkeypatch, tmp_path
):
    bad_process = CrashingProcess()
    create_running_batch(
        db, "bad", str(tmp_path / "bad.log"), sec_user_id="badsec"
    )
    create_running_batch(
        db, "good", str(tmp_path / "good.log"), sec_user_id="goodsec"
    )

    async def fake_launch(command, cwd):
        if manager._active_batch_id == "bad":
            return bad_process
        return FakeProcess(
            [
                '__DOUKHUB__{"type":"account_result","sec_user_id":"goodsec","status":"success","message":"OK"}'
            ],
            returncode=0,
        )

    monkeypatch.setattr(manager, "_launch_process", fake_launch)

    async def run_worker():
        manager._queue.put_nowait("bad")
        manager._queue.put_nowait("good")
        manager._ensure_worker()
        await manager._queue.join()
        manager._closing = True
        manager._worker.cancel()
        try:
            await manager._worker
        except asyncio.CancelledError:
            pass

    asyncio.run(run_worker())

    assert bad_process.terminated
    assert db.get_collection_batch("bad")["status"] == "failed"
    assert db.get_collection_batch("good")["status"] == "completed"
    assert manager._active_batch_id is None
    assert manager._active_process is None
