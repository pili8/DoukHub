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


def test_account_sync_ready_depends_on_status(tmp_path, monkeypatch):
    syncer = make_syncer(tmp_path, monkeypatch)

    ready = {"解析状态": "已就绪", "sec_user_id": "sec001"}
    not_ready = {"解析状态": "待解析", "sec_user_id": "sec001"}
    generated = {"解析状态": "已生成", "sec_user_id": "sec001"}
    deleted = {"解析状态": "已删除", "sec_user_id": "sec001"}
    failed = {"解析状态": "解析失败", "sec_user_id": "sec001"}

    assert syncer.is_ready_for_account(ready) is True
    assert syncer.is_ready_for_account(not_ready) is False
    assert syncer.is_ready_for_account(generated) is False
    assert syncer.is_ready_for_account(deleted) is False
    assert syncer.is_ready_for_account(failed) is False


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


def test_import_mixed_real_world_share_formats(tmp_path, monkeypatch):
    syncer = make_syncer(tmp_path, monkeypatch)
    text = """
个，图@ihNoyCMM
个，2\\@ihYfCafE
分享，图，2\\@ihYfWvum
个，商业，2\\@ih2fYvqA
个2，多@if1Mrqtx
COS2\\@if1uhbyh
酒吧2\\@ifJJqJQx
分享2\\@ifJEuXDU

{"ID号" :"","作品" :"","地址" :"Wfdc1A6ewbg","时间" :"20260621231530","用户" :"","等级" :"个3","粉丝" :""}
{"ID号" :"","作品" :"","地址" :"seX062YZFK0","时间" :"20260621232010","用户" :"","等级" :"个3","粉丝" :""}
{"ID号" :"","作品" :"","地址" :"vQ2mKm6YAPo","时间" :"20260622104047","用户" :"","等级" :"自拍3","粉丝" :""}
{"ID号" :"41089775107","作品" :"作品 55","地址" :"VtaXSs2w1P0","时间" :"20250917102014","用户" :"刘鑫泽他爹开的A7L","等级" :"个3","粉丝" :"1.5万"}
{"ID号" :"WMWMWMYYY","作品" :"作品 383","地址" :"1SVatf0jI-s","时间" :"20250917104207","用户" :"一筒","等级" :"个3，多","粉丝" :"24.2万"}
"""

    result = syncer.import_to_collection(text)
    rows = {row["share_code"]: row for row in syncer.db.get_all_collections()}

    assert result.total == 13
    assert (result.created, result.failed, result.skipped) == (13, 0, 0)
    assert result.warnings == []
    assert set(rows) == {
        "ihNoyCMM", "ihYfCafE", "ihYfWvum", "ih2fYvqA", "if1Mrqtx",
        "if1uhbyh", "ifJJqJQx", "ifJEuXDU", "Wfdc1A6ewbg", "seX062YZFK0",
        "vQ2mKm6YAPo", "VtaXSs2w1P0", "1SVatf0jI-s",
    }
    assert rows["ihNoyCMM"]["等级"] == 1
    assert json.loads(rows["ihNoyCMM"]["标签"]) == ["个人", "图"]
    assert rows["ihYfCafE"]["等级"] == 2
    assert json.loads(rows["ihYfCafE"]["标签"]) == ["个人"]
    assert rows["ih2fYvqA"]["等级"] == 2
    assert json.loads(rows["ih2fYvqA"]["标签"]) == ["个人", "商业"]
    assert rows["if1Mrqtx"]["等级"] == 2
    assert json.loads(rows["if1Mrqtx"]["标签"]) == ["个人", "多"]
    assert rows["if1uhbyh"]["等级"] == 2
    assert json.loads(rows["if1uhbyh"]["标签"]) == ["COS"]
    assert rows["vQ2mKm6YAPo"]["等级"] == 3
    assert json.loads(rows["vQ2mKm6YAPo"]["标签"]) == ["自拍"]
    assert rows["VtaXSs2w1P0"]["账号名称"] == "刘鑫泽他爹开的A7L"
    assert rows["VtaXSs2w1P0"]["粉丝数"] == 15000
    assert rows["VtaXSs2w1P0"]["作品数"] == 55
    assert rows["1SVatf0jI-s"]["账号名称"] == "一筒"
    assert rows["1SVatf0jI-s"]["粉丝数"] == 242000
    assert rows["1SVatf0jI-s"]["作品数"] == 383
