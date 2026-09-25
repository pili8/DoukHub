"""测试 FeishuSyncer v4（行级 LWW + 映射表）的核心逻辑（不依赖网络）。

重点覆盖：
- 行级 LWW：本地新赢推送 / 飞书新赢回写 / 内容一致跳过
- 映射表：record_id 绑定、时间戳刷新、GC
- 删除传播（双向）与 50% 比例保护
- 全盘覆盖（按键差异，不再清空重建）
- 字段等价比较（标签/验证时间/数值/bool/文本/URL）
- 飞书字段定义：「修改时间」类型 1002
"""
import json
import pathlib
import tempfile
from datetime import datetime
from unittest.mock import MagicMock

import pytest

from app.core.database import Database
from app.core.feishu import FeishuClient
from app.core.feishu_sync import FeishuSyncer, FEISHU_TS_FIELD


def _ms_to_str(ms: int) -> str:
    return datetime.fromtimestamp(int(ms) // 1000).strftime("%Y-%m-%d %H:%M:%S")


T0 = 1700000000000  # 基准毫秒时间戳
T1 = 1700003600000


@pytest.fixture
def db():
    p = pathlib.Path(tempfile.mkdtemp()) / "test.db"
    return Database(db_path=p)


@pytest.fixture
def syncer(db, monkeypatch):
    """构造一个不依赖真实飞书连接的 syncer"""
    mock_feishu = MagicMock()
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
    s.feishu.batch_create_records.return_value = {"code": 0, "data": {"records": []}}
    s.feishu.batch_update_records.return_value = {"code": 0}
    s.feishu.batch_delete_records.return_value = {"code": 0}
    return s


def _acc_fields(key="sec1", **extra):
    f = {"sec_user_id": key, "账号名称": f"name-{key}", "平台": "douyin",
         "等级": 3, "获取状态": "待获取", "启用": True, "采集类型": "发布",
         FEISHU_TS_FIELD: T1}
    f.update(extra)
    return f


def _feishu_rec(rid, fields):
    return {"record_id": rid, "fields": fields}


# ========== 飞书字段定义：修改时间 = 1002 ==========

def test_required_fields_include_modified_time_1002():
    for tt in ("collection", "account", "cookie"):
        fields = FeishuClient._get_required_fields(tt)
        names = [f[0] for f in fields]
        assert "修改时间" in names, f"{tt} 缺少「修改时间」字段"
        target = [f for f in fields if f[0] == "修改时间"][0]
        assert target[1] == 1002, "「修改时间」必须是 1002（最后更新时间），1001 是创建时间"


# ========== 行级等价比较 ==========

def test_field_equal_text_and_int(syncer):
    assert syncer._field_equal("备注", "abc", "abc") is True
    assert syncer._field_equal("备注", " abc ", "abc") is True
    assert syncer._field_equal("等级", 3, "3") is True
    assert syncer._field_equal("等级", 3, 4) is False


def test_field_equal_tags(syncer):
    assert syncer._field_equal("标签", '["个", "图"]', [{"text": "个"}, {"text": "图"}]) is True
    assert syncer._field_equal("标签", '["个"]', ["个", "图"]) is False


def test_fields_equal_excludes_sync_time(syncer):
    local = {"同步时间": 111, "账号名称": "A"}
    feishu = {"同步时间": 999, "账号名称": "A"}
    assert syncer._fields_equal(local, feishu) is True


def test_row_push_fields_clears_empty_text(syncer, db):
    """本地清空了备注、飞书还有 → 推送应显式带 "" 清空"""
    db.insert_account({"record_id": "r1", "sec_user_id": "sec1", "账号名称": "A",
                       "平台": "douyin", "等级": 3})
    local = db.get_account_by_id("r1")
    feishu_fields = _acc_fields(备注="旧备注")
    push = syncer._row_push_fields("account_cache", local, feishu_fields)
    assert push["备注"] == ""


# ========== 映射表 CRUD ==========

def test_sync_map_upsert_and_get(db):
    db.upsert_sync_map("account_cache", "sec1", "r1", T1, T0)
    m = db.get_sync_map("account_cache")
    assert m["sec1"] == {"record_id": "r1", "feishu_ts": T1, "local_ts": T0}
    # 覆盖更新
    db.upsert_sync_map("account_cache", "sec1", "r1", T1 + 1, T0 + 1)
    assert db.get_sync_map("account_cache")["sec1"]["feishu_ts"] == T1 + 1


def test_sync_map_delete(db):
    db.upsert_sync_map("account_cache", "sec1", "r1", T1, T0)
    db.delete_sync_map("account_cache", "sec1")
    assert "sec1" not in db.get_sync_map("account_cache")


# ========== 增量同步：本地新建 → 飞书创建 ==========

def test_sync_creates_local_new_to_feishu(syncer, db):
    db.insert_account({"record_id": "r1", "sec_user_id": "sec1", "账号名称": "A",
                       "平台": "douyin", "等级": 3, "local_updated_at": _ms_to_str(T0)})
    syncer.feishu.get_all_records.return_value = []
    syncer.feishu.batch_create_records.return_value = {
        "code": 0, "data": {"records": [{"record_id": "fs1"}]},
    }

    result = syncer.sync_account_to_feishu()

    assert result["created"] == 1
    # 映射已建立
    m = db.get_sync_map("account_cache")
    assert "sec1" in m and m["sec1"]["record_id"] == "fs1"
    # 本地 record_id 已更新为飞书分配的
    assert db.get_account_by_id("fs1") is not None
    # 推送字段里带了业务键
    payload = syncer.feishu.batch_create_records.call_args[0][2]
    assert payload[0]["fields"]["sec_user_id"] == "sec1"


# ========== 增量同步：飞书新建 → 本地插入 ==========

def test_sync_inserts_feishu_new_to_local(syncer, db):
    syncer.feishu.get_all_records.return_value = [
        _feishu_rec("fs1", _acc_fields("sec9")),
    ]

    result = syncer.sync_account_to_feishu()

    assert result["created"] == 1
    row = db.get_account_by_id("fs1")
    assert row is not None and row["sec_user_id"] == "sec9"
    assert row["synced"] == 1
    m = db.get_sync_map("account_cache")
    assert m["sec9"]["record_id"] == "fs1"
    assert m["sec9"]["feishu_ts"] == T1


# ========== 行级 LWW ==========

def test_lww_local_newer_pushes(syncer, db):
    """本地后改 → 整行推送到飞书"""
    db.insert_account({"record_id": "r1", "sec_user_id": "sec1", "账号名称": "A",
                       "平台": "douyin", "等级": 3, "备注": "本地新备注",
                       "local_updated_at": _ms_to_str(T1)})
    db.upsert_sync_map("account_cache", "sec1", "r1", T0, T0)
    syncer.feishu.get_all_records.return_value = [
        _feishu_rec("r1", _acc_fields("sec1", 备注="旧备注", **{FEISHU_TS_FIELD: T0})),
    ]

    result = syncer.sync_account_to_feishu()

    assert result["updated"] == 1
    payload = syncer.feishu.batch_update_records.call_args[0][2]
    assert payload[0]["record_id"] == "r1"
    assert payload[0]["fields"]["备注"] == "本地新备注"
    # 映射时间戳已刷新
    m = db.get_sync_map("account_cache")["sec1"]
    assert m["local_ts"] == T1


def test_lww_feishu_newer_pulls_to_local(syncer, db):
    """飞书后改 → 整行写回本地，local_updated_at 对齐飞书"""
    db.insert_account({"record_id": "r1", "sec_user_id": "sec1", "账号名称": "A",
                       "平台": "douyin", "等级": 3, "备注": "本地旧备注",
                       "local_updated_at": _ms_to_str(T0)})
    db.upsert_sync_map("account_cache", "sec1", "r1", T0, T0)
    syncer.feishu.get_all_records.return_value = [
        _feishu_rec("r1", _acc_fields("sec1", 备注="飞书新备注")),
    ]

    result = syncer.sync_account_to_feishu()

    assert result["updated"] == 1
    row = db.get_account_by_id("r1")
    assert row["备注"] == "飞书新备注"
    assert row["local_updated_at"] == _ms_to_str(T1)
    # 本地不应被推送到飞书
    syncer.feishu.batch_update_records.assert_not_called()


def test_lww_equal_content_skips(syncer, db):
    """两端内容一致 → 跳过，仅刷新映射"""
    db.insert_account({"record_id": "r1", "sec_user_id": "sec1", "账号名称": "name-sec1",
                       "平台": "douyin", "等级": 3, "local_updated_at": _ms_to_str(T0)})
    syncer.feishu.get_all_records.return_value = [
        _feishu_rec("r1", _acc_fields("sec1")),
    ]

    result = syncer.sync_account_to_feishu()

    assert result["skipped_uptodate"] == 1
    syncer.feishu.batch_update_records.assert_not_called()
    assert db.get_sync_map("account_cache")["sec1"]["record_id"] == "r1"


def test_lww_field_cleared_on_feishu_propagates(syncer, db):
    """飞书端清空备注 → 本地备注也被清空（修复 v3 清空无法同步）"""
    db.insert_account({"record_id": "r1", "sec_user_id": "sec1", "账号名称": "A",
                       "平台": "douyin", "等级": 3, "备注": "要被清空的备注",
                       "local_updated_at": _ms_to_str(T0)})
    db.upsert_sync_map("account_cache", "sec1", "r1", T0, T0)
    fields = _acc_fields("sec1")
    fields.pop("备注", None)  # 飞书端备注已空
    syncer.feishu.get_all_records.return_value = [_feishu_rec("r1", fields)]

    syncer.sync_account_to_feishu()

    assert db.get_account_by_id("r1")["备注"] is None


# ========== 删除传播 ==========

def test_feishu_deletion_propagates_to_local(syncer, db):
    """映射在、飞书记录没了 → 删本地 + 清映射"""
    db.insert_account({"record_id": "r1", "sec_user_id": "sec1", "账号名称": "A",
                       "平台": "douyin", "等级": 3})
    db.upsert_sync_map("account_cache", "sec1", "r1", T0, T0)
    db.insert_account({"record_id": "r2", "sec_user_id": "sec2", "账号名称": "B",
                       "平台": "douyin", "等级": 3})
    db.upsert_sync_map("account_cache", "sec2", "r2", T0, T0)
    # 飞书只剩 sec2
    syncer.feishu.get_all_records.return_value = [
        _feishu_rec("r2", _acc_fields("sec2")),
    ]

    result = syncer.sync_account_to_feishu()

    assert result["deleted"] == 1
    assert db.get_account_by_id("r1") is None
    assert db.get_account_by_id("r2") is not None
    assert "sec1" not in db.get_sync_map("account_cache")


def test_local_deletion_propagates_to_feishu(syncer, db):
    """映射在、本地行没了 → 删飞书 + 清映射"""
    db.upsert_sync_map("account_cache", "sec1", "r1", T0, T0)
    syncer.feishu.get_all_records.return_value = [
        _feishu_rec("r1", _acc_fields("sec1")),
    ]

    result = syncer.sync_account_to_feishu()

    assert result["deleted"] == 1
    syncer.feishu.batch_delete_records.assert_called_once()
    assert "sec1" not in db.get_sync_map("account_cache")


def test_deletion_ratio_guard_protects_local(syncer, db):
    """飞书疑似批量删除（>50%）→ 跳过删除本地"""
    for i in range(4):
        db.insert_account({"record_id": f"r{i}", "sec_user_id": f"sec{i}",
                           "账号名称": f"N{i}", "平台": "douyin", "等级": 3})
        db.upsert_sync_map("account_cache", f"sec{i}", f"r{i}", T0, T0)
    # 飞书只剩 1 条陌生记录（4 条映射全没了 → >50% → 保护）
    syncer.feishu.get_all_records.return_value = [
        _feishu_rec("other", _acc_fields("sec_other")),
    ]

    result = syncer.sync_account_to_feishu()

    assert result["deleted"] == 0
    assert any("安全保护" in e for e in result["errors"])
    assert db.get_account_by_id("r0") is not None


# ========== 全盘覆盖（按键差异，不清空重建） ==========

def test_full_to_feishu_diff_based(syncer, db):
    """覆盖云端：已有的更新、新的创建、多余的删除；record_id 不变"""
    db.insert_account({"record_id": "r1", "sec_user_id": "sec1", "账号名称": "A更新",
                       "平台": "douyin", "等级": 3})
    db.insert_account({"record_id": "r2", "sec_user_id": "sec_new", "账号名称": "B",
                       "平台": "douyin", "等级": 3})
    syncer.feishu.get_all_records.return_value = [
        _feishu_rec("r1", _acc_fields("sec1", 备注="会被覆盖")),
        _feishu_rec("old", _acc_fields("sec_old")),
    ]
    syncer.feishu.batch_create_records.return_value = {
        "code": 0, "data": {"records": [{"record_id": "fs_new"}]},
    }

    result = syncer._full_to_feishu_single("account_cache")

    assert result["updated"] == 1
    assert result["created"] == 1
    assert result["deleted"] == 1
    # 不允许"先清后建"：sec1 的 record_id 保持 r1
    payload = syncer.feishu.batch_update_records.call_args[0][2]
    assert payload[0]["record_id"] == "r1"


def test_full_from_feishu_diff_based(syncer, db):
    """覆盖本地：已有的更新、新的插入、多余的硬删除"""
    db.insert_account({"record_id": "r1", "sec_user_id": "sec1", "账号名称": "A",
                       "平台": "douyin", "等级": 3})
    db.insert_account({"record_id": "r_old", "sec_user_id": "sec_old", "账号名称": "X",
                       "平台": "douyin", "等级": 3})
    syncer.feishu.get_all_records.return_value = [
        _feishu_rec("r1", _acc_fields("sec1", 备注="云端值")),
        _feishu_rec("fs9", _acc_fields("sec9")),
    ]

    result = syncer._full_from_feishu_single("account_cache")

    assert result["updated"] == 1
    assert result["created"] == 1
    assert result["deleted"] == 1
    assert db.get_account_by_id("r1")["备注"] == "云端值"
    assert db.get_account_by_id("fs9") is not None
    assert db.get_account_by_id("r_old") is None


# ========== 综合与兼容 ==========

def test_sync_incremental_empty_tables(syncer, monkeypatch):
    """空库 + 空飞书：不崩、不报错"""
    syncer.feishu.get_all_records.return_value = []
    results = syncer.sync_incremental(trigger="test")
    assert len(results) == 3
    for r in results.values():
        assert r["failed"] == 0


def test_get_incremental_steps(syncer):
    steps = syncer.get_incremental_steps()
    assert len(steps) == 3
    for label, fn in steps:
        assert callable(fn)
        assert isinstance(fn(), dict)


def test_get_full_steps(syncer):
    assert len(syncer.get_full_steps("to-feishu")) == 3
    assert len(syncer.get_full_steps("from-feishu")) == 3


def test_feishu_duplicate_business_key_skipped(syncer, db):
    """飞书端重复业务键 → 跳过多余记录，不崩"""
    db.insert_account({"record_id": "r1", "sec_user_id": "sec1", "账号名称": "A",
                       "平台": "douyin", "等级": 3, "local_updated_at": _ms_to_str(T0)})
    syncer.feishu.get_all_records.return_value = [
        _feishu_rec("r1", _acc_fields("sec1")),
        _feishu_rec("dup", _acc_fields("sec1")),
    ]

    result = syncer.sync_account_to_feishu()

    assert result["skipped_duplicate"] == 1
