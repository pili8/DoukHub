"""同步器 v2 导入行为测试"""
import json

from app.core import syncer_v2
from app.core.database import Database
from app.core.syncer_v2 import Syncer


def make_syncer(tmp_path, monkeypatch):
    db = Database(tmp_path / "test.db")
    monkeypatch.setattr(syncer_v2, "Database", lambda: db)
    return Syncer(None, None, {}, {"个": "个人"})


def test_import_counts_duplicates_separately(tmp_path, monkeypatch):
    syncer = make_syncer(tmp_path, monkeypatch)

    result = syncer.import_to_collection("个2@abc123\n图3@abc123")

    assert result.total == 2
    assert result.success == 1
    assert result.created == 1
    assert result.duplicates == 1
    assert result.failed == 0
    rows = syncer.db.get_all_collections()
    assert len(rows) == 1
    assert rows[0]["等级"] == 3
    assert json.loads(rows[0]["标签"]) == ["个人", "图"]


def test_import_counts_existing_records_as_updates(tmp_path, monkeypatch):
    syncer = make_syncer(tmp_path, monkeypatch)

    first = syncer.import_to_collection("个1@abc123")
    second = syncer.import_to_collection("个2@abc123")

    assert (first.created, first.updated, first.duplicates) == (1, 0, 0)
    assert (second.created, second.updated, second.duplicates) == (0, 1, 0)
    assert len(syncer.db.get_all_collections()) == 1


def test_import_records_skip_reason(tmp_path, monkeypatch):
    syncer = make_syncer(tmp_path, monkeypatch)

    result = syncer.import_to_collection('{"地址":""}')

    assert result.skipped == 1
    assert result.warnings == ['{"地址":""}: 缺少地址']


def test_account_sync_ready_depends_on_sec_user_id(tmp_path, monkeypatch):
    syncer = make_syncer(tmp_path, monkeypatch)

    with_id = {"已解析": False, "sec_user_id": "sec001"}
    without_id = {"已解析": True, "sec_user_id": ""}

    assert syncer.is_ready_for_account(with_id) is True
    assert syncer.is_ready_for_account(without_id) is False


def test_import_full_profile_merges_existing_sec_user_id(tmp_path, monkeypatch):
    syncer = make_syncer(tmp_path, monkeypatch)
    syncer.db.insert_collection({
        "record_id": "rec_short",
        "share_code": "short-code",
        "sec_user_id": "sec001",
        "等级": 1,
        "标签": '["个"]',
    })

    result = syncer.import_to_collection("图4@https://www.douyin.com/user/sec001")

    rows = syncer.db.get_all_collections()
    assert result.updated == 1
    assert len(rows) == 1
    assert rows[0]["record_id"] == "rec_short"
    assert rows[0]["sec_user_id"] == "sec001"
    assert rows[0]["等级"] == 4
