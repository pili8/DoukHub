import asyncio
import json
from types import SimpleNamespace

from fastapi import Request
from fastapi.responses import JSONResponse

import app.main as main_module
from app.core.database import Database
from app.core.syncer_v2 import Syncer
from app.core.tasks import Task


class RecordingTaskManager:
    def __init__(self):
        self.task = Task(task_id="task-test", type="test")
        self.cancelled = False

    def add_log(self, task_id, message, level="info"):
        pass

    def update(self, task_id, **fields):
        for key, value in fields.items():
            setattr(self.task, key, value)

    def is_cancelled(self, task_id):
        return self.cancelled


class FakeHttpClient:
    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, *args, **kwargs):
        return SimpleNamespace(status_code=200)


def _request_with_text(text):
    body = json.dumps({"text": text}).encode("utf-8")

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/sync/v2/all",
            "headers": [(b"content-type", b"application/json")],
        },
        receive=receive,
    )


def test_sync_v2_all_rejects_large_inline_batches(monkeypatch):
    def fail_get_syncer():
        raise AssertionError("large batch must not start synchronous sync")

    monkeypatch.setattr(main_module, "get_syncer_v2", fail_get_syncer)
    response = asyncio.run(main_module.api_sync_v2_all(_request_with_text("\n".join(f"a@{i}" for i in range(101)))))

    assert isinstance(response, JSONResponse)
    assert response.status_code == 413
    assert response.body.decode("utf-8").find("分批") >= 0


def test_update_collection_stops_after_repeated_ttd_failures(monkeypatch):
    db = SimpleNamespace(
        get_all_collections=lambda: [
            {"record_id": f"c{i}", "share_code": f"s{i}", "平台": "douyin", "解析状态": "待解析"}
            for i in range(30)
        ],
        update_collection=lambda *args, **kwargs: None,
    )
    calls = []

    async def resolve_short_url(share, platform):
        calls.append(share)
        return ""

    syncer = SimpleNamespace(
        db=db,
        collector=SimpleNamespace(ttd_url="http://ttd", resolve_short_url=resolve_short_url),
    )
    tm = RecordingTaskManager()
    monkeypatch.setattr(main_module.httpx, "AsyncClient", FakeHttpClient)
    monkeypatch.setattr(main_module, "get_syncer_v2", lambda: syncer)
    monkeypatch.setattr(main_module, "get_task_manager", lambda: tm)

    task = tm.task
    task.type = "update_collection"
    asyncio.run(main_module._run_update_collection(task))

    assert len(calls) == main_module.MAX_CONSECUTIVE_TTD_FAILURES
    assert task.failed == main_module.MAX_CONSECUTIVE_TTD_FAILURES


def test_sync_account_stops_after_repeated_ttd_failures(tmp_path, monkeypatch):
    db = Database(tmp_path / "step3.db")
    for i in range(30):
        db.insert_collection(
            {"record_id": f"c{i}", "share_code": f"s{i}", "sec_user_id": f"sec-{i}", "解析状态": "已就绪"}
        )
    db.insert_cookie({"record_id": "cookie", "Cookie": "cookie", "启用": 1})
    calls = []

    async def get_account_info(sec_user_id, platform, cookie):
        calls.append(sec_user_id)
        return {}

    async def no_sleep(_seconds):
        return None

    syncer = SimpleNamespace(
        db=db,
        collector=SimpleNamespace(ttd_url="http://ttd", get_account_info=get_account_info),
        is_ready_for_account=Syncer.is_ready_for_account,
        merge_level=Syncer.merge_level,
        merge_tags=Syncer.merge_tags,
    )
    tm = RecordingTaskManager()
    tm.task.type = "sync_account"
    monkeypatch.setattr(main_module.httpx, "AsyncClient", FakeHttpClient)
    monkeypatch.setattr(main_module.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(main_module, "get_syncer_v2", lambda: syncer)
    monkeypatch.setattr(main_module, "get_database", lambda: db)
    monkeypatch.setattr(main_module, "get_task_manager", lambda: tm)

    asyncio.run(main_module._run_sync_account(tm.task))

    assert len(calls) == main_module.MAX_CONSECUTIVE_TTD_FAILURES
    assert tm.task.status == "failed"
    assert tm.task.failed == main_module.MAX_CONSECUTIVE_TTD_FAILURES


def test_refresh_accounts_stops_after_repeated_ttd_failures(tmp_path, monkeypatch):
    db = Database(tmp_path / "step4.db")
    for i in range(30):
        db.insert_account(
            {"record_id": f"a{i}", "sec_user_id": f"sec-{i}", "获取状态": "待获取"}
        )
    db.insert_cookie({"record_id": "cookie", "Cookie": "cookie", "启用": 1})
    calls = []

    async def get_account_info(sec_user_id, platform, cookie):
        calls.append(sec_user_id)
        return {}

    async def no_sleep(_seconds):
        return None

    tm = RecordingTaskManager()
    tm.task.type = "refresh_accounts"
    monkeypatch.setattr(main_module.httpx, "AsyncClient", FakeHttpClient)
    monkeypatch.setattr(main_module.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(main_module, "get_database", lambda: db)
    monkeypatch.setattr(main_module, "get_task_manager", lambda: tm)
    monkeypatch.setattr(
        main_module,
        "get_collector",
        lambda: SimpleNamespace(get_account_info=get_account_info),
    )

    asyncio.run(main_module._run_refresh_accounts(tm.task))

    assert len(calls) == main_module.MAX_CONSECUTIVE_TTD_FAILURES
    assert tm.task.status == "failed"
    assert tm.task.failed == main_module.MAX_CONSECUTIVE_TTD_FAILURES
