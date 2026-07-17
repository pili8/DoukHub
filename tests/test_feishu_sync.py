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
    """API 字段（local_wins）：本地值优先，应该推送到飞书"""
    db.insert_collection({
        "record_id": "r1", "分享码": "abc", "等级": 3,
        "粉丝数": 200, "昵称": "new_name",
    })
    local = db.get_collection_by_id("r1")
    # 飞书：粉丝数=100（旧），本地：粉丝数=200（新）
    feishu_record = {
        "record_id": "r1",
        "fields": {"分享码": "abc", "等级": 3, "粉丝数": 100, "昵称": "old_name"},
    }
    to_feishu, to_local = syncer._compute_field_updates("collection_cache", local, feishu_record)
    # API 字段（粉丝数、昵称）应该推送本地值到飞书
    assert "粉丝数" in to_feishu
    assert to_feishu["粉丝数"] == 200
    assert "昵称" in to_feishu
    assert to_feishu["昵称"] == "new_name"


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
