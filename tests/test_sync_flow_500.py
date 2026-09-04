import asyncio
from types import SimpleNamespace

from app import main as main_module
from app.core.database import Database
from app.core import syncer_v2
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
    def add_log(self, task_id, message, level="info"):
        pass

    def update(self, task_id, **fields):
        pass

    def is_cancelled(self, task_id):
        return False


def test_account_flow_handles_500_records(tmp_path, monkeypatch):
    db = Database(tmp_path / "stress.db")
    total = 500

    async def resolve_short_url(share, platform):
        sec_user_id = f"sec-{share[-4:]}"
        return f"https://www.douyin.com/user/{sec_user_id}"

    async def get_account_info(sec_user_id, platform, cookie):
        return {
            "nickname": f"账号{sec_user_id[-4:]}",
            "follower_count": 1000,
            "aweme_count": 20,
            "signature": "压测数据",
            "avatar": "avatar.png",
        }

    monkeypatch.setattr(syncer_v2, "Database", lambda: db)
    syncer = Syncer(
        feishu=None,
        collector=SimpleNamespace(
            ttd_url="http://ttd",
            resolve_short_url=resolve_short_url,
            get_account_info=get_account_info,
        ),
        config={},
    )
    syncer.db = db

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(main_module.httpx, "AsyncClient", FakeHttpClient)
    monkeypatch.setattr(main_module.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(main_module, "get_syncer_v2", lambda: syncer)
    monkeypatch.setattr(main_module, "get_database", lambda: db)
    monkeypatch.setattr(
        main_module,
        "get_collector",
        lambda: SimpleNamespace(get_account_info=get_account_info),
    )

    import_result = syncer.import_to_collection(
        "\n".join(f"压测@share{i:04d}" for i in range(total))
    )
    assert import_result.created == total
    assert import_result.failed == 0
    assert len(db.get_all_collections()) == total

    asyncio.run(main_module._run_update_collection(Task(task_id="step2", type="test")))
    assert sum(c["解析状态"] == "已就绪" for c in db.get_all_collections()) == total

    db.insert_cookie({"record_id": "cookie-stress", "Cookie": "stress-cookie", "启用": 1})
    asyncio.run(main_module._run_sync_account(Task(task_id="step3", type="test")))
    accounts = db.get_all_accounts()
    assert len(accounts) == total
    assert all(a["账号名称"] for a in accounts)
    assert all(a["获取状态"] == "已获取" for a in accounts)

    for account in accounts:
        db.update_account(account["record_id"], {"获取状态": "待获取"})
    asyncio.run(main_module._run_refresh_accounts(Task(task_id="step4", type="test")))
    accounts = db.get_all_accounts()
    assert len(accounts) == total
    assert all(a["账号名称"] for a in accounts)
    assert all(a["获取状态"] == "已获取" for a in accounts)
