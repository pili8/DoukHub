"""步骤3/4的流程约束测试"""
import asyncio
from types import SimpleNamespace

from app import main as main_module
from app.core.database import Database
from app.core.syncer_v2 import Syncer
from app.core.tasks import Task


class FakeResponse:
    status_code = 200


class FakeHttpClient:
    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, *args, **kwargs):
        return FakeResponse()


class FakeTaskManager:
    def __init__(self):
        self.task = Task(task_id="task-test", type="test")

    def add_log(self, task_id, message, level="info"):
        pass

    def update(self, task_id, **fields):
        for key, value in fields.items():
            setattr(self.task, key, value)

    def is_cancelled(self, task_id):
        return False


def test_sync_account_without_cookie_still_creates_account_shell(tmp_path, monkeypatch):
    db = Database(tmp_path / "test.db")
    db.insert_collection({"record_id": "c1", "share_code": "short", "sec_user_id": "sec1"})
    syncer = SimpleNamespace(
        db=db,
        collector=SimpleNamespace(ttd_url="http://ttd"),
        is_ready_for_account=Syncer.is_ready_for_account,
        merge_level=Syncer.merge_level,
        merge_tags=Syncer.merge_tags,
    )
    tm = FakeTaskManager()
    monkeypatch.setattr(main_module.httpx, "AsyncClient", FakeHttpClient)
    monkeypatch.setattr(main_module, "get_syncer_v2", lambda: syncer)
    monkeypatch.setattr(main_module, "get_database", lambda: db)

    asyncio.run(main_module._run_sync_account(tm.task))

    account = db.get_account_by_sec_user_id("sec1")
    assert account is not None
    assert account["已获取信息"] in (0, False)


def test_refresh_accounts_uses_account_platform(tmp_path, monkeypatch):
    db = Database(tmp_path / "test.db")
    db.insert_account({"record_id": "a1", "sec_user_id": "sec1", "平台": "小红书", "已获取信息": 0})
    db.insert_cookie({"record_id": "ck1", "Cookie": "cookie", "启用": 1})
    platforms = []

    async def get_account_info(sec_user_id, platform, cookie):
        platforms.append(platform)
        return {"nickname": "测试账号"}

    tm = FakeTaskManager()
    monkeypatch.setattr(main_module.httpx, "AsyncClient", FakeHttpClient)
    monkeypatch.setattr(main_module, "get_database", lambda: db)
    monkeypatch.setattr(main_module, "get_collector", lambda: SimpleNamespace(get_account_info=get_account_info))

    asyncio.run(main_module._run_refresh_accounts(tm.task))

    assert platforms == ["小红书"]
    assert db.get_account_by_sec_user_id("sec1")["账号名称"] == "测试账号"
