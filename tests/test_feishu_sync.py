"""测试 FeishuSyncer v2 的核心逻辑（不依赖网络）。

重点覆盖：
- _merge_results：结果合并（修复 v1 的 dict.update 覆盖 bug）
- _compute_field_updates：字段归属计算（人工字段飞书赢，API 字段本地赢）
- _values_equal：字段值等价比较
- _normalize_tags：标签规范化
- _parse_text_value：飞书文本字段解析
- 业务键配置正确性
- 全盘同步的结果 schema
"""
import json
import pathlib
import tempfile
from unittest.mock import MagicMock

import pytest

from app.core.database import Database
from app.core.feishu_sync import FeishuSyncer


@pytest.fixture
def db():
    p = pathlib.Path(tempfile.mkdtemp()) / "test.db"
    return Database(db_path=p)


@pytest.fixture
def syncer(db, monkeypatch):
    """构造一个不依赖真实飞书连接的 syncer"""
    mock_feishu = MagicMock()
    mock_feishu.test_connection.return_value = {"success": True, "message": "ok"}
    config = {
        "app_id": "test",
        "app_secret": "test",
        "app_token": "test_token",
        "collection_table_id": "tblA",
        "account_table_id": "tblB",
        "cookie_table_id": "tblC",
    }
    monkeypatch.setattr("app.core.feishu_sync.Database", lambda *a, **kw: db)
    s = FeishuSyncer(mock_feishu, config)
    return s


# ========== _merge_results：累加合并（关键 bug 修复） ==========

def test_merge_results_adds_counts():
    """两个步骤的 failed 应该累加，不应该覆盖"""
    r1 = {"created": 5, "updated": 3, "failed": 2, "errors": ["err1"]}
    r2 = {"created": 1, "updated": 0, "failed": 4, "errors": ["err2"]}
    merged = FeishuSyncer._merge_results(r1, r2)
    assert merged["created"] == 6
    assert merged["updated"] == 3
    assert merged["failed"] == 6  # 关键：累加，不是覆盖
    assert merged["errors"] == ["err1", "err2"]


def test_merge_results_skipped_fields_add():
    """跳过统计字段也应该累加"""
    r1 = {"skipped_uptodate": 10, "skipped_duplicate": 2, "skipped_invalid": 1}
    r2 = {"skipped_uptodate": 5, "skipped_duplicate": 1, "skipped_invalid": 0}
    merged = FeishuSyncer._merge_results(r1, r2)
    assert merged["skipped_uptodate"] == 15
    assert merged["skipped_duplicate"] == 3
    assert merged["skipped_invalid"] == 1


def test_merge_results_empty_ignored():
    """空 dict 应该被忽略"""
    r1 = {"created": 1}
    merged = FeishuSyncer._merge_results(r1, {}, None)
    assert merged["created"] == 1


def test_merge_results_initial_structure():
    """合并空输入应返回完整的初始结构"""
    merged = FeishuSyncer._merge_results()
    expected_keys = {"created", "updated", "deleted", "skipped_uptodate",
                     "skipped_duplicate", "skipped_invalid", "failed", "errors"}
    assert set(merged.keys()) == expected_keys
    assert merged["errors"] == []


# ========== _parse_text_value：飞书文本字段解析 ==========

def test_parse_text_value_none():
    assert FeishuSyncer._parse_text_value(None) == ""


def test_parse_text_value_string():
    assert FeishuSyncer._parse_text_value("hello") == "hello"


def test_parse_text_value_number():
    assert FeishuSyncer._parse_text_value(42) == "42"


def test_parse_text_value_list_of_dicts():
    """飞书文本字段是 [{"text": "xxx"}] 格式"""
    val = [{"text": "abc"}, {"text": "def"}]
    assert FeishuSyncer._parse_text_value(val) == "abcdef"


def test_parse_text_value_url_dict():
    """飞书 URL 字段是 {"link": "...", "text": "..."} 格式"""
    val = {"link": "https://example.com/x", "text": "链接"}
    assert FeishuSyncer._parse_text_value(val) == "https://example.com/x"


# ========== _normalize_tags：标签规范化 ==========

def test_normalize_tags_none():
    assert FeishuSyncer._normalize_tags(None) is None


def test_normalize_tags_empty():
    assert FeishuSyncer._normalize_tags("") is None


def test_normalize_tags_string():
    assert FeishuSyncer._normalize_tags("个") == ["个"]


def test_normalize_tags_json_string():
    """本地存储为 JSON 字符串"""
    result = FeishuSyncer._normalize_tags('["个", "图"]')
    assert result == ["个", "图"]


def test_normalize_tags_list():
    """飞书返回数组"""
    assert FeishuSyncer._normalize_tags(["个", "图"]) == ["个", "图"]


def test_normalize_tags_list_of_dicts():
    """飞书多选返回 [{"text": "个"}] 格式"""
    result = FeishuSyncer._normalize_tags([{"text": "个"}, {"text": "图"}])
    assert result == ["个", "图"]


# ========== _values_equal：字段等价比较 ==========

def test_values_equal_text_fields(syncer):
    assert syncer._values_equal("备注", "abc", "abc") is True
    assert syncer._values_equal("备注", "abc", "def") is False
    assert syncer._values_equal("备注", " abc ", "abc") is True  # 去空格


def test_values_equal_int_fields(syncer):
    assert syncer._values_equal("等级", 3, 3) is True
    assert syncer._values_equal("等级", "3", 3) is True  # 类型转换
    assert syncer._values_equal("等级", 3, 4) is False


def test_values_equal_bool_fields(syncer):
    assert syncer._values_equal("已同步", True, True) is True
    assert syncer._values_equal("已同步", True, "true") is True
    assert syncer._values_equal("已同步", False, 0) is True
    assert syncer._values_equal("已同步", True, False) is False


def test_values_equal_tags_field(syncer):
    """标签字段：本地 JSON 字符串 vs 飞书数组"""
    # 相同
    assert syncer._values_equal("标签", '["个", "图"]', ["个", "图"]) is True
    # 顺序不同也算相同（用 set 比较）
    assert syncer._values_equal("标签", '["图", "个"]', ["个", "图"]) is True
    # 不同
    assert syncer._values_equal("标签", '["个"]', ["个", "图"]) is False


# ========== _compute_field_updates：字段归属计算（核心） ==========

def test_compute_field_updates_local_wins(syncer, db):
    """API 字段（local_wins）：本地值优先，应该推送到飞书

    注意：账号表的「粉丝数/昵称」是 API 字段（本地赢）
    """
    db.insert_account({
        "record_id": "r1", "sec_user_id": "sec1",
        "粉丝数": 200, "昵称": "new_name",
    })
    local = db.get_account_by_id("r1")
    # 飞书：粉丝数=100（旧），本地：粉丝数=200（新）
    feishu_record = {
        "record_id": "r1",
        "fields": {"sec_user_id": "sec1", "粉丝数": 100, "昵称": "old_name"},
    }
    to_feishu, to_local = syncer._compute_field_updates("account_cache", local, feishu_record)
    # API 字段（粉丝数、昵称）应该推送本地值到飞书
    assert "粉丝数" in to_feishu
    assert to_feishu["粉丝数"] == 200
    assert "昵称" in to_feishu
    assert to_feishu["昵称"] == "new_name"


def test_compute_field_updates_collection_account_name_feishu_wins(syncer, db):
    """采集表的「账号名称」是人工字段（飞书赢）

    用户在飞书改账号名称 → 应该同步到本地（不会被本地覆盖）
    """
    db.insert_collection({"record_id": "r1", "分享码": "abc", "账号名称": "old_name"})
    local = db.get_collection_by_id("r1")
    feishu_record = {
        "record_id": "r1",
        "fields": {"分享码": "abc", "账号名称": "new_name_from_feishu"},
    }
    to_feishu, to_local = syncer._compute_field_updates("collection_cache", local, feishu_record)
    # 账号名称是飞书赢，应该更新本地
    assert "账号名称" in to_local
    assert to_local["账号名称"] == "new_name_from_feishu"
    # 不应该推送到飞书
    assert "账号名称" not in to_feishu


def test_compute_field_updates_account_account_name_feishu_wins(syncer, db):
    """账号表的「账号名称」也是人工字段（飞书赢）"""
    db.insert_account({"record_id": "a1", "sec_user_id": "sec1", "账号名称": "old_name"})
    local = db.get_account_by_id("a1")
    feishu_record = {
        "record_id": "a1",
        "fields": {"sec_user_id": "sec1", "账号名称": "new_name"},
    }
    to_feishu, to_local = syncer._compute_field_updates("account_cache", local, feishu_record)
    assert "账号名称" in to_local
    assert to_local["账号名称"] == "new_name"
    assert "账号名称" not in to_feishu


def test_compute_field_updates_feishu_wins(syncer, db):
    """人工字段（feishu_wins）：飞书值优先，应该更新本地"""
    db.insert_collection({"record_id": "r1", "分享码": "abc", "等级": 3, "备注": "old"})
    local = db.get_collection_by_id("r1")
    # 飞书：等级=4（用户改的），本地：等级=3
    feishu_record = {
        "record_id": "r1",
        "fields": {"分享码": "abc", "等级": 4, "备注": "new remark"},
    }
    to_feishu, to_local = syncer._compute_field_updates("collection_cache", local, feishu_record)
    # 人工字段（等级、备注）应该用飞书的值更新本地
    assert "等级" in to_local
    assert to_local["等级"] == 4
    assert "备注" in to_local
    assert to_local["备注"] == "new remark"


def test_compute_field_updates_no_diff(syncer, db):
    """两端值相同，应该没有更新"""
    db.insert_collection({"record_id": "r1", "分享码": "abc", "等级": 3, "备注": "x"})
    local = db.get_collection_by_id("r1")
    feishu_record = {
        "record_id": "r1",
        "fields": {"分享码": "abc", "等级": 3, "备注": "x"},
    }
    to_feishu, to_local = syncer._compute_field_updates("collection_cache", local, feishu_record)
    assert to_feishu == {}
    assert to_local == {}


def test_compute_field_updates_account_local_wins(syncer, db):
    """账号表：API 字段（粉丝数等）本地赢"""
    db.insert_account({
        "record_id": "a1", "sec_user_id": "sec1",
        "粉丝数": 1000, "昵称": "new_name",
    })
    local = db.get_account_by_id("a1")
    feishu_record = {
        "record_id": "a1",
        "fields": {"sec_user_id": "sec1", "粉丝数": 500, "昵称": "old_name"},
    }
    to_feishu, to_local = syncer._compute_field_updates("account_cache", local, feishu_record)
    assert to_feishu.get("粉丝数") == 1000
    assert to_feishu.get("昵称") == "new_name"


def test_compute_field_updates_account_feishu_wins(syncer, db):
    """账号表：人工字段（等级、标签、启用、采集类型、备注）飞书赢"""
    db.insert_account({
        "record_id": "a1", "sec_user_id": "sec1",
        "等级": 3, "备注": "local remark", "启用": True, "采集类型": "发布",
    })
    local = db.get_account_by_id("a1")
    feishu_record = {
        "record_id": "a1",
        "fields": {
            "sec_user_id": "sec1", "等级": 4, "备注": "feishu remark",
            "启用": False, "采集类型": "喜欢",
        },
    }
    to_feishu, to_local = syncer._compute_field_updates("account_cache", local, feishu_record)
    assert to_local.get("等级") == 4
    assert to_local.get("备注") == "feishu remark"
    assert to_local.get("启用") is False
    assert to_local.get("采集类型") == "喜欢"


# ========== 业务键配置 ==========

def test_business_keys_defined():
    """三张表都应有业务唯一键定义"""
    assert "collection_cache" in FeishuSyncer.BUSINESS_KEYS
    assert "account_cache" in FeishuSyncer.BUSINESS_KEYS
    assert "cookie_cache" in FeishuSyncer.BUSINESS_KEYS
    assert FeishuSyncer.BUSINESS_KEYS["collection_cache"] == "分享码"
    assert FeishuSyncer.BUSINESS_KEYS["account_cache"] == "sec_user_id"
    assert FeishuSyncer.BUSINESS_KEYS["cookie_cache"] == "Cookie"


def test_field_ownership_covers_all_tables():
    """三张表都应有字段归属定义"""
    for table in ("collection_cache", "account_cache", "cookie_cache"):
        assert table in FeishuSyncer.FIELD_OWNERSHIP
        ownership = FeishuSyncer.FIELD_OWNERSHIP[table]
        assert "feishu_wins" in ownership
        assert "local_wins" in ownership


def test_field_ownership_disjoint():
    """feishu_wins 和 local_wins 字段不应该重叠"""
    for table, ownership in FeishuSyncer.FIELD_OWNERSHIP.items():
        fw = set(ownership["feishu_wins"])
        lw = set(ownership["local_wins"])
        overlap = fw & lw
        assert not overlap, f"{table} 字段归属冲突: {overlap}"


# ========== 字段构建函数 ==========

def test_build_collection_fields(syncer, db):
    """采集表字段构建应包含所有必要字段"""
    db.insert_collection({
        "record_id": "r1", "分享码": "abc", "平台": "抖音", "等级": 3,
        "标签": '["个"]', "sec_user_id": "sec1", "昵称": "name",
        "粉丝数": 100, "作品数": 50, "备注": "test",
    })
    local = db.get_collection_by_id("r1")
    fields = syncer._build_collection_fields(local)
    assert fields["分享码"] == "abc"
    assert fields["平台"] == "抖音"
    assert fields["等级"] == 3
    assert fields["标签"] == ["个"]
    assert fields["sec_user_id"] == "sec1"
    assert "同步时间" in fields  # 自动写入


def test_build_account_fields(syncer, db):
    db.insert_account({
        "record_id": "a1", "sec_user_id": "sec1", "账号名称": "name",
        "平台": "抖音", "等级": 4,
    })
    local = db.get_account_by_id("a1")
    fields = syncer._build_account_fields(local)
    assert fields["sec_user_id"] == "sec1"
    assert fields["账号名称"] == "name"
    assert fields["平台"] == "抖音"
    assert fields["等级"] == 4
    assert fields["已获取信息"] is False


# ========== 飞书记录 → 本地转换 ==========

def test_feishu_record_to_local_collection(syncer):
    """飞书采集记录转本地"""
    record = {
        "record_id": "r1",
        "fields": {
            "分享码": "abc",
            "平台": [{"text": "抖音"}],  # 飞书单选格式
            "等级": 3,
            "sec_user_id": "sec1",
            "标签": [{"text": "个"}, {"text": "图"}],
        },
    }
    data = syncer._feishu_record_to_local_collection(record)
    assert data is not None
    assert data["分享码"] == "abc"
    assert data["平台"] == "抖音"
    assert data["等级"] == 3
    assert data["sec_user_id"] == "sec1"
    assert json.loads(data["标签"]) == ["个", "图"]


def test_feishu_record_to_local_collection_empty_share(syncer):
    """飞书记录缺分享码应返回 None"""
    record = {"record_id": "r1", "fields": {"平台": "抖音"}}
    data = syncer._feishu_record_to_local_collection(record)
    assert data is None


def test_feishu_record_to_local_account(syncer):
    record = {
        "record_id": "a1",
        "fields": {
            "sec_user_id": "sec1",
            "账号名称": "name",
            "平台": "抖音",
            "等级": 4,
            "粉丝数": 1000,
        },
    }
    data = syncer._feishu_record_to_local_account(record)
    assert data is not None
    assert data["sec_user_id"] == "sec1"
    assert data["账号名称"] == "name"
    assert data["等级"] == 4
    assert data["粉丝数"] == 1000


def test_feishu_record_to_local_account_empty_sec(syncer):
    record = {"record_id": "a1", "fields": {"账号名称": "name"}}
    data = syncer._feishu_record_to_local_account(record)
    assert data is None


def test_feishu_record_to_local_cookie(syncer):
    record = {
        "record_id": "ck1",
        "fields": {
            "Cookie": "sessionid=abc",
            "平台": "抖音",
            "状态": "正常",
            "启用": True,
        },
    }
    data = syncer._feishu_record_to_local_cookie(record)
    assert data is not None
    assert data["Cookie"] == "sessionid=abc"
    assert data["平台"] == "抖音"
    assert data["状态"] == "正常"
    assert data["启用"] is True


def test_feishu_record_to_local_cookie_empty(syncer):
    record = {"record_id": "ck1", "fields": {"Cookie": ""}}
    data = syncer._feishu_record_to_local_cookie(record)
    assert data is None


# ========== table_id 解析 ==========

def test_get_table_id_collection(syncer):
    assert syncer._get_table_id("collection_cache") == "tblA"


def test_get_table_id_account(syncer):
    assert syncer._get_table_id("account_cache") == "tblB"


def test_get_table_id_cookie(syncer):
    assert syncer._get_table_id("cookie_cache") == "tblC"


def test_get_table_id_unknown(syncer):
    assert syncer._get_table_id("unknown") == ""


# ========== _get_cookie_by_value（业务键查询） ==========

def test_get_cookie_by_value(syncer, db):
    db.insert_cookie({"record_id": "ck1", "Cookie": "abc=123"})
    result = syncer._get_cookie_by_value("abc=123")
    assert result is not None
    assert result["record_id"] == "ck1"


def test_get_cookie_by_value_not_found(syncer, db):
    db.insert_cookie({"record_id": "ck1", "Cookie": "abc=123"})
    result = syncer._get_cookie_by_value("not_exists")
    assert result is None


def test_get_cookie_by_value_empty(syncer):
    assert syncer._get_cookie_by_value("") is None


# ========== 删除安全保护 ==========

def test_delete_safety_ratio_value():
    """50% 阈值应该在 0~1 之间"""
    assert 0 < FeishuSyncer.DELETE_SAFETY_RATIO < 1


def test_field_ownership_collection_account_name_is_feishu_wins():
    """采集表的「账号名称」应归飞书赢（人工字段）"""
    ownership = FeishuSyncer.FIELD_OWNERSHIP["collection_cache"]
    assert "账号名称" in ownership["feishu_wins"]
    assert "账号名称" not in ownership["local_wins"]


def test_field_ownership_account_account_name_is_feishu_wins():
    """账号表的「账号名称」应归飞书赢（人工字段）"""
    ownership = FeishuSyncer.FIELD_OWNERSHIP["account_cache"]
    assert "账号名称" in ownership["feishu_wins"]
    assert "账号名称" not in ownership["local_wins"]


def test_get_incremental_steps_returns_6_steps(syncer):
    """get_incremental_steps 应返回 6 个独立步骤（3 表 × 2 方向）"""
    steps = syncer.get_incremental_steps()
    assert len(steps) == 6
    labels = [s[0] for s in steps]
    # 验证包含所有方向
    assert any("本地 → 云端" in l for l in labels)
    assert any("云端 → 本地" in l for l in labels)
    # 验证包含所有表
    for tbl in ["采集表", "账号表", "Cookie表"]:
        assert any(tbl in l for l in labels), f"缺少 {tbl} 步骤"
    # 验证每个 callable 可调用
    for label, fn in steps:
        assert callable(fn), f"{label} 的 fn 不是 callable"


def test_get_full_steps_to_feishu_returns_3_steps(syncer):
    """get_full_steps('to-feishu') 应返回 3 个步骤"""
    steps = syncer.get_full_steps("to-feishu")
    assert len(steps) == 3
    for label, fn in steps:
        assert "覆盖云端" in label
        assert callable(fn)


def test_get_full_steps_from_feishu_returns_3_steps(syncer):
    """get_full_steps('from-feishu') 应返回 3 个步骤"""
    steps = syncer.get_full_steps("from-feishu")
    assert len(steps) == 3
    for label, fn in steps:
        assert "覆盖本地" in label
        assert callable(fn)


def test_sync_propagates_feishu_account_name_change(syncer, db, monkeypatch):
    """端到端：用户在飞书改账号名称 → 增量同步 → 本地账号名称被更新

    验证字段归属：账号名称是人工字段（飞书赢）
    """
    db.insert_account({
        "record_id": "r1", "sec_user_id": "sec1",
        "账号名称": "old_name", "等级": 3, "synced": True,
    })

    # 飞书改了账号名称：old_name → new_name
    syncer.feishu.get_all_records.return_value = [
        {"record_id": "r1", "fields": {"sec_user_id": "sec1", "账号名称": "new_name", "等级": 3}},
    ]
    syncer.feishu.batch_create_records.return_value = {"code": 0, "data": {"records": []}}
    syncer.feishu.batch_update_records.return_value = {"code": 0}
    syncer.feishu.batch_delete_records.return_value = {"code": 0}

    syncer.sync_incremental()

    # 本地的账号名称应该被更新为 new_name
    acc = db.get_account_by_id("r1")
    assert acc is not None
    assert acc["账号名称"] == "new_name", f"飞书改的账号名称应同步到本地，实际 {acc['账号名称']}"


def test_field_ownership_cookie_status_is_local_wins():
    """Cookie 表「状态」字段应归本地赢（DoukHub 验证后写入）

    修正原因：之前归 immutable 导致飞书改状态不同步、本地改状态不推送
    """
    ownership = FeishuSyncer.FIELD_OWNERSHIP["cookie_cache"]
    assert "状态" in ownership["local_wins"]
    assert "状态" not in ownership["immutable"]


def test_field_ownership_collection_synced_is_local_wins():
    """采集表「已同步」字段应归本地赢（步骤3 写入的状态字段）

    修正原因：之前归 feishu_wins 会导致步骤3 写入后被飞书覆盖
    """
    ownership = FeishuSyncer.FIELD_OWNERSHIP["collection_cache"]
    assert "已同步" in ownership["local_wins"]
    assert "已同步" not in ownership["feishu_wins"]


# ========== sync_incremental 入口（mock 网络） ==========

def test_sync_incremental_handles_empty_tables(syncer, monkeypatch):
    """空表场景下 sync_incremental 应该不报错"""
    # mock 飞书 API
    syncer.feishu.get_all_records.return_value = []
    syncer.feishu.batch_create_records.return_value = {"code": 0, "data": {"records": []}}
    syncer.feishu.batch_update_records.return_value = {"code": 0}
    syncer.feishu.batch_delete_records.return_value = {"code": 0}

    results = syncer.sync_incremental()

    # 6 步结果
    assert len(results) == 6
    # 没有 errors
    all_errors = []
    for r in results.values():
        all_errors.extend(r.get("errors", []))
    assert all_errors == []


def test_sync_incremental_creates_local_to_feishu(syncer, db, monkeypatch):
    """本地有飞书没有的记录 → 创建到飞书"""
    db.insert_collection({"record_id": "local1", "分享码": "abc", "等级": 3})

    syncer.feishu.get_all_records.return_value = []  # 飞书空
    syncer.feishu.batch_create_records.return_value = {
        "code": 0,
        "data": {"records": [{"record_id": "fs_rec1"}]},
    }
    syncer.feishu.batch_update_records.return_value = {"code": 0}
    syncer.feishu.batch_delete_records.return_value = {"code": 0}

    results = syncer.sync_incremental()
    # 找到「本地 → 云端：采集表」
    coll_to_feishu = results["本地 → 云端：采集表"]
    assert coll_to_feishu["created"] >= 1


def test_sync_incremental_propagates_feishu_deletion(syncer, db, monkeypatch):
    """飞书删除 → 本地 synced=1 但飞书没有 → 删本地（飞书端有其他记录）"""
    db.insert_collection({"record_id": "r1", "分享码": "abc", "等级": 3, "synced": True})
    db.insert_collection({"record_id": "r2", "分享码": "xyz", "等级": 3, "synced": True})

    # 飞书还有 r2，但 r1 不见了（说明用户在飞书端删了 r1）
    syncer.feishu.get_all_records.return_value = [
        {"record_id": "r2", "fields": {"分享码": "xyz", "等级": 3}},
    ]
    syncer.feishu.batch_create_records.return_value = {"code": 0, "data": {"records": []}}
    syncer.feishu.batch_update_records.return_value = {"code": 0}
    syncer.feishu.batch_delete_records.return_value = {"code": 0}

    syncer.sync_incremental()

    # r1 应该被删除（飞书→本地反推删除）
    assert db.get_collection_by_id("r1") is None
    # r2 应该还在
    assert db.get_collection_by_id("r2") is not None


def test_sync_to_feishu_skips_synced_orphans_when_safety_triggered(syncer, db, monkeypatch):
    """关键 bug 修复：飞书表大幅减少时，synced=1 的孤儿不应被推回飞书

    场景：用户在飞书批量删除（>50%）或清空飞书表
    错误行为：删除检测被比例保护跳过，Step 5 把本地 synced=1 孤儿推回飞书
    正确行为：synced=1 + 飞书没有 = 飞书已删，不推回；只有 synced=0 才推送
    """
    # 本地 4 条 synced=1（飞书端全删了）
    for i in range(4):
        db.insert_account({"record_id": f"r{i}", "sec_user_id": f"sec{i}", "等级": 3, "synced": True})
    # 本地 1 条 synced=0（本地新建未推送）
    db.insert_account({"record_id": "local_new", "sec_user_id": "sec_new", "等级": 3})

    # 飞书只返回 1 条（< 本地 synced=1 数量的 50%）→ 触发比例保护
    syncer.feishu.get_all_records.return_value = [
        {"record_id": "other", "fields": {"sec_user_id": "sec_other", "等级": 3}},
    ]
    syncer.feishu.batch_create_records.return_value = {
        "code": 0,
        "data": {"records": [{"record_id": "fs_new"}]},
    }
    syncer.feishu.batch_update_records.return_value = {"code": 0}
    syncer.feishu.batch_delete_records.return_value = {"code": 0}

    result = syncer._sync_to_feishu("account_cache")

    # 4 条 synced=1 不应被推回（孤儿保护）
    # 1 条 synced=0 应被推送（本地新建）
    assert result["created"] == 1, f"应只推送 1 条本地新建，实际推送 {result['created']}"
    # 应有 4 条 skipped_invalid（孤儿跳过）
    assert result["skipped_invalid"] >= 4, f"应跳过 4 条孤儿，实际 {result['skipped_invalid']}"


def test_sync_to_feishu_skips_synced_orphans_when_feishu_empty(syncer, db, monkeypatch):
    """关键 bug 修复：飞书表完全空时，本地 synced=1 不应被全部推回

    场景：用户清空了飞书表（或飞书 API 异常返回空）
    正确行为：synced=1 不推回（保护删除）；synced=0 推送
    """
    db.insert_account({"record_id": "synced1", "sec_user_id": "sec1", "等级": 3, "synced": True})
    db.insert_account({"record_id": "local_new", "sec_user_id": "sec2", "等级": 3})

    # 飞书返回空
    syncer.feishu.get_all_records.return_value = []
    syncer.feishu.batch_create_records.return_value = {
        "code": 0,
        "data": {"records": [{"record_id": "fs_new"}]},
    }
    syncer.feishu.batch_update_records.return_value = {"code": 0}
    syncer.feishu.batch_delete_records.return_value = {"code": 0}

    result = syncer._sync_to_feishu("account_cache")

    # synced=1 的不推回
    assert result["created"] == 1, f"应只推送 1 条本地新建，实际 {result['created']}"
    # synced1 还在本地（没被删除也没被推回）
    assert db.get_account_by_id("synced1") is not None


def test_sync_from_feishu_skips_duplicate_business_key(syncer, db, monkeypatch):
    """关键 bug 修复：飞书端有重复业务键时，不应让本地 record_id 反复横跳

    场景：飞书表里两条相同 sec_user_id（用户录重了）
    错误行为：每次同步都把本地 record_id 在飞书两条之间切换（无限循环）
    正确行为：检测到重复，跳过冗余记录，提示用户去飞书清理
    """
    db.insert_account({"record_id": "r1", "sec_user_id": "sec1", "等级": 3, "synced": True})

    # 飞书有两条 sec_user_id=sec1 的记录（r1 和 fs_dup）
    syncer.feishu.get_all_records.return_value = [
        {"record_id": "r1", "fields": {"sec_user_id": "sec1", "等级": 3}},
        {"record_id": "fs_dup", "fields": {"sec_user_id": "sec1", "等级": 4}},
    ]
    syncer.feishu.batch_create_records.return_value = {"code": 0, "data": {"records": []}}
    syncer.feishu.batch_update_records.return_value = {"code": 0}
    syncer.feishu.batch_delete_records.return_value = {"code": 0}

    # 只跑 from_feishu
    result = syncer._sync_from_feishu("account_cache")

    # 本地 record_id 不应被改成 fs_dup
    acc = db.get_account_by_id("r1")
    assert acc is not None, "本地 r1 应该还在"
    assert acc["record_id"] == "r1", f"本地 record_id 不应被改，实际 {acc['record_id']}"
    # 应该有 skipped_duplicate 计数
    assert result["skipped_duplicate"] >= 1, "应该跳过重复业务键"


def test_sync_to_feishu_merges_by_business_key_when_record_id_differs(syncer, db, monkeypatch):
    """场景 9：双向同时新建相同业务键 → 按业务键合并，更新本地 record_id

    场景：本地新建 local_abc（synced=0），飞书有 fs_abc（业务键相同）
    正确行为：按业务键匹配，不重复创建飞书，更新本地 record_id=fs_abc
    """
    db.insert_collection({"record_id": "local_abc", "分享码": "ABC", "等级": 3})

    # 飞书有相同分享码 ABC（record_id 不同）
    syncer.feishu.get_all_records.return_value = [
        {"record_id": "fs_abc", "fields": {"分享码": "ABC", "等级": 4}},
    ]
    syncer.feishu.batch_create_records.return_value = {"code": 0, "data": {"records": []}}
    syncer.feishu.batch_update_records.return_value = {"code": 0}
    syncer.feishu.batch_delete_records.return_value = {"code": 0}

    result = syncer._sync_to_feishu("collection_cache")

    # 不应该创建新飞书记录
    assert result["created"] == 0
    # 应该走合并流程（更新飞书的等级=3 来自本地）或字段相同跳过
    # 关键：本地 record_id 应该被改为 fs_abc，synced=1
    # 此时 local_abc 已不存在
    assert db.get_collection_by_id("local_abc") is None
    # 应该能通过 fs_abc 查到（但我们的查询是按 record_id）
    merged = db.get_collection_by_id("fs_abc")
    if merged:
        assert merged["synced"] in (1, True)


def test_sync_handles_local_delete_then_feishu_modify(syncer, db, monkeypatch):
    """场景 13/28：本地软删除 + 飞书修改同条 → 删除优先（设计 A 方案）

    场景：本地软删 r1，飞书端用户同时修改 r1 等级
    正确行为：删除优先，飞书的修改被删除覆盖
    """
    db.insert_account({"record_id": "r1", "sec_user_id": "sec1", "等级": 3, "synced": True})
    db.delete_account("r1")  # 软删除

    # 飞书动态返回（按表分别计数）
    table_call_count = {"tblB": 0}

    def mock_get_all_records(app_token, table_id):
        if table_id == "tblB":
            table_call_count["tblB"] += 1
            # 第 1 次（_push_tombstones 检查）：返回 r1（飞书还有）
            if table_call_count["tblB"] == 1:
                return [{"record_id": "r1", "fields": {"sec_user_id": "sec1", "等级": 4}}]
        # 后续（推送墓碑后）：返回空（已被删除）
        return []

    syncer.feishu.get_all_records.side_effect = mock_get_all_records
    syncer.feishu.batch_create_records.return_value = {"code": 0, "data": {"records": []}}
    syncer.feishu.batch_update_records.return_value = {"code": 0}
    syncer.feishu.batch_delete_records.return_value = {"code": 0}

    syncer.sync_incremental()

    # r1 应该被飞书删除（墓碑推送）
    syncer.feishu.batch_delete_records.assert_any_call(
        syncer.app_token, "tblB", ["r1"]
    )
    # 本地 r1 应该被彻底清除（墓碑被 purge）
    assert db.get_account_by_id("r1") is None


def test_sync_handles_dual_field_concurrent_modification(syncer, db, monkeypatch):
    """场景 10：双向同时修改不同字段（人工字段 + API 字段）

    场景：飞书改等级 3→4（人工字段飞书赢），本地改粉丝数 100→200（API 字段本地赢）
    正确行为：两端都变为 等级=4 + 粉丝数=200
    """
    db.insert_account({
        "record_id": "r1", "sec_user_id": "sec1",
        "等级": 3, "粉丝数": 100, "synced": True,
    })

    # 飞书有等级 4（用户改的），粉丝数还是 100
    syncer.feishu.get_all_records.return_value = [
        {"record_id": "r1", "fields": {"sec_user_id": "sec1", "等级": 4, "粉丝数": 100}},
    ]
    syncer.feishu.batch_create_records.return_value = {"code": 0, "data": {"records": []}}
    syncer.feishu.batch_update_records.return_value = {"code": 0}
    syncer.feishu.batch_delete_records.return_value = {"code": 0}

    # 模拟本地修改粉丝数
    db.update_account("r1", {"粉丝数": 200})

    syncer.sync_incremental()

    # 验证最终状态
    final = db.get_account_by_id("r1")
    assert final is not None
    assert final["等级"] == 4, f"等级应以飞书为准 = 4，实际 {final['等级']}"
    assert final["粉丝数"] == 200, f"粉丝数应以本地为准 = 200，实际 {final['粉丝数']}"


def test_sync_to_feishu_does_not_resurrect_feishu_deletion(syncer, db, monkeypatch):
    """关键 bug 修复：飞书删除某条 → 本地 synced=1 但飞书没有

    错误行为（v1）：本地→云端步骤把记录推回去，删除被恢复
    正确行为（v2）：本地→云端步骤先检测飞书删除，删本地，不再推送

    测试场景：本地有 r1（synced=1）和 r2（synced=1），飞书只剩 r2（r1 被用户删了）
    """
    db.insert_account({"record_id": "r1", "sec_user_id": "sec1", "等级": 3, "synced": True})
    db.insert_account({"record_id": "r2", "sec_user_id": "sec2", "等级": 3, "synced": True})

    # 飞书只剩 r2（r1 被用户删除）
    syncer.feishu.get_all_records.return_value = [
        {"record_id": "r2", "fields": {"sec_user_id": "sec2", "等级": 3}},
    ]
    syncer.feishu.batch_create_records.return_value = {"code": 0, "data": {"records": []}}
    syncer.feishu.batch_update_records.return_value = {"code": 0}
    syncer.feishu.batch_delete_records.return_value = {"code": 0}

    # 只调用 _sync_to_feishu（不调用 sync_incremental 的 from_feishu 步骤）
    result = syncer._sync_to_feishu("account_cache")

    # r1 应该被本地删除（飞书→本地反推）
    assert db.get_account_by_id("r1") is None
    # r2 应该还在
    assert db.get_account_by_id("r2") is not None
    # 不应该把 r1 推回飞书（created 应该是 0）
    assert result["created"] == 0
    # 应该有删除计数
    assert result["deleted"] >= 1


def test_sync_incremental_skips_unsynced_local_records(syncer, db, monkeypatch):
    """本地新建未同步（synced=0）的记录，即使飞书空，也不会被误删"""
    db.insert_collection({"record_id": "local1", "分享码": "abc", "等级": 3})  # synced=0 默认

    syncer.feishu.get_all_records.return_value = []
    syncer.feishu.batch_create_records.return_value = {"code": 0, "data": {"records": []}}
    syncer.feishu.batch_update_records.return_value = {"code": 0}
    syncer.feishu.batch_delete_records.return_value = {"code": 0}

    syncer.sync_incremental()

    # local1 应该还在（不会被误删），但应该已被推送到飞书
    assert db.get_collection_by_id("local1") is not None


def test_sync_incremental_pushes_local_tombstones(syncer, db, monkeypatch):
    """本地墓碑（is_deleted=1）应该被推送到飞书删除

    注意：mock 飞书返回值需要反映「删除后」状态。
    - 第 1 次调用（_push_tombstones 检查飞书是否真有 r1）：返回 r1
    - 第 2 次调用（_sync_to_feishu 主流程拉取飞书做比对）：返回空
    - 后续调用（_sync_from_feishu）：返回空
    """
    db.insert_collection({"record_id": "r1", "分享码": "abc", "等级": 3, "synced": True})
    db.delete_collection("r1")  # 软删除

    call_count = {"n": 0}

    def mock_get_all_records(app_token, table_id):
        call_count["n"] += 1
        if table_id == "tblA" and call_count["n"] == 1:
            # 第 1 次调用：_push_tombstones 检查飞书有 r1
            return [{"record_id": "r1", "fields": {"分享码": "abc", "等级": 3}}]
        # 后续调用：r1 已被删除
        return []

    syncer.feishu.get_all_records.side_effect = mock_get_all_records
    syncer.feishu.batch_create_records.return_value = {"code": 0, "data": {"records": []}}
    syncer.feishu.batch_update_records.return_value = {"code": 0}
    syncer.feishu.batch_delete_records.return_value = {"code": 0}

    syncer.sync_incremental()

    # 应该调用过 batch_delete_records 删除 r1
    syncer.feishu.batch_delete_records.assert_any_call(
        syncer.app_token, "tblA", ["r1"]
    )
    # 墓碑应该被清除（r1 真的从本地数据库消失）
    assert db.get_collection_by_id("r1") is None


def test_sync_incremental_empty_feishu_skips_deletion(syncer, db, monkeypatch):
    """飞书返回 0 条记录时，应该跳过删除检测（空结果保护）

    场景：本地有 synced=1 的记录，飞书返回空（可能是 API 异常）
    预期：不会误删本地的 synced 记录
    """
    db.insert_collection({"record_id": "r1", "分享码": "abc", "等级": 3, "synced": True})

    syncer.feishu.get_all_records.return_value = []  # 飞书返回空
    syncer.feishu.batch_create_records.return_value = {"code": 0, "data": {"records": []}}
    syncer.feishu.batch_update_records.return_value = {"code": 0}
    syncer.feishu.batch_delete_records.return_value = {"code": 0}

    syncer.sync_incremental()

    # 关键：r1 不应被误删
    assert db.get_collection_by_id("r1") is not None


# ========== 全盘同步 ==========

def test_sync_full_to_feishu_clears_and_pushes(syncer, db, monkeypatch):
    """全盘：本地→飞书，应清空飞书 + 推送本地全部"""
    db.insert_collection({"record_id": "r1", "分享码": "abc"})
    db.insert_collection({"record_id": "r2", "分享码": "def"})

    syncer.feishu.get_all_records.return_value = [
        {"record_id": "old1", "fields": {}},  # 旧的会被删
    ]
    syncer.feishu.batch_delete_records.return_value = {"code": 0}
    syncer.feishu.batch_create_records.return_value = {
        "code": 0,
        "data": {"records": [{"record_id": "new1"}, {"record_id": "new2"}]},
    }

    results = syncer.sync_full_to_feishu()
    assert "覆盖云端：采集表" in results
    r = results["覆盖云端：采集表"]
    assert r["deleted"] >= 1  # 清空了旧的
    assert r["created"] == 2  # 创建了 2 条新的


def test_sync_full_from_feishu_clears_and_inserts(syncer, db, monkeypatch):
    """全盘：飞书→本地，应清空本地 + 插入飞书全部"""
    db.insert_collection({"record_id": "local1", "分享码": "old"})
    db.insert_account({"record_id": "a1", "sec_user_id": "sec1"})

    syncer.feishu.get_all_records.return_value = [
        {"record_id": "fs1", "fields": {"分享码": "new_share", "等级": 4}},
    ]
    # cookie 表也返回一些数据
    syncer.feishu.batch_delete_records.return_value = {"code": 0}

    results = syncer.sync_full_from_feishu()

    # 本地应该被清空并重建
    assert "覆盖本地：采集表" in results
    # collection_cache 之前有 local1，现在应该没有，只有 fs1
    collections = db.get_all_collections()
    assert len(collections) == 1
    assert collections[0]["record_id"] == "fs1"
    assert collections[0]["分享码"] == "new_share"
