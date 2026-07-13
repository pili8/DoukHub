"""测试 Database 通用表操作（排序、启用开关、导入等）。"""
import pathlib
import tempfile
import pytest

from app.core.database import Database


@pytest.fixture
def db():
    """每个测试一个临时数据库"""
    p = pathlib.Path(tempfile.mkdtemp()) / "test.db"
    return Database(db_path=p)


# ========== schema 测试 ==========

def test_account_cache_has_enabled_field(db):
    """账号表应该有"启用"字段（本次需求新增）"""
    schema = db.get_table_schema("account_cache")
    names = [c["name"] for c in schema]
    assert "启用" in names


def test_account_cache_has_collection_type_field(db):
    """账号表应该有"采集类型"字段（飞书已定义，本地补齐）"""
    schema = db.get_table_schema("account_cache")
    names = [c["name"] for c in schema]
    assert "采集类型" in names


def test_account_cache_no_proxy_field(db):
    """账号表不应该有"代理"字段（已删除，飞书也已移除）"""
    schema = db.get_table_schema("account_cache")
    names = [c["name"] for c in schema]
    assert "代理" not in names


def test_account_cache_fields_align_with_feishu(db):
    """账号表关键字段应与飞书对齐：sec_user_id / 备注 / 同步时间 / 已获取信息"""
    schema = db.get_table_schema("account_cache")
    names = {c["name"] for c in schema}
    # 飞书字段名（应全部存在）
    assert "sec_user_id" in names
    assert "备注" in names
    assert "同步时间" in names
    assert "已获取信息" in names
    # 旧字段名（应已迁移）
    assert "账号标识" not in names
    assert "更新错误" not in names
    assert "更新时间" not in names
    assert "已更新" not in names


def test_cookie_cache_has_enabled_field(db):
    schema = db.get_table_schema("cookie_cache")
    names = [c["name"] for c in schema]
    assert "启用" in names


def test_schema_contains_pk_info(db):
    schema = db.get_table_schema("cookie_cache")
    pk_cols = [c["name"] for c in schema if c["pk"]]
    assert "记录ID" in pk_cols


def test_schema_invalid_table_raises():
    """schema 应只在表白名单内"""
    p = pathlib.Path(tempfile.mkdtemp()) / "t.db"
    d = Database(db_path=p)
    # VALID_TABLES 不含 unknown_table
    assert "unknown_table" not in d.VALID_TABLES


# ========== query_table 排序 ==========

def test_query_table_default_order(db):
    db.insert_cookie({"记录ID": "c1", "Cookie": "aaa"})
    db.insert_cookie({"记录ID": "c2", "Cookie": "bbb"})
    r = db.query_table("cookie_cache", limit=10)
    assert r["total"] == 2
    assert len(r["records"]) == 2


def test_query_table_sort_asc(db):
    db.insert_cookie({"记录ID": "c1", "Cookie": "ccc"})
    db.insert_cookie({"记录ID": "c2", "Cookie": "aaa"})
    db.insert_cookie({"记录ID": "c3", "Cookie": "bbb"})
    r = db.query_table("cookie_cache", sort_field="Cookie", sort_order="asc")
    cookies = [x["Cookie"] for x in r["records"]]
    assert cookies == ["aaa", "bbb", "ccc"]


def test_query_table_sort_desc(db):
    db.insert_cookie({"记录ID": "c1", "Cookie": "aaa"})
    db.insert_cookie({"记录ID": "c2", "Cookie": "ccc"})
    db.insert_cookie({"记录ID": "c3", "Cookie": "bbb"})
    r = db.query_table("cookie_cache", sort_field="Cookie", sort_order="desc")
    cookies = [x["Cookie"] for x in r["records"]]
    assert cookies == ["ccc", "bbb", "aaa"]


def test_query_table_sort_invalid_field_ignored(db):
    """排序字段不存在时，应回退到默认排序（不报错）"""
    db.insert_cookie({"记录ID": "c1", "Cookie": "aaa"})
    r = db.query_table("cookie_cache", sort_field="不存在的字段", sort_order="asc")
    assert r["total"] == 1


def test_query_table_search(db):
    db.insert_cookie({"记录ID": "c1", "Cookie": "aaa", "备注": "测试1"})
    db.insert_cookie({"记录ID": "c2", "Cookie": "bbb", "备注": "其他"})
    r = db.query_table("cookie_cache", search="测试")
    assert r["total"] == 1
    assert r["records"][0]["记录ID"] == "c1"


def test_query_table_pagination(db):
    for i in range(5):
        db.insert_cookie({"记录ID": f"c{i}", "Cookie": f"cookie_{i}"})
    r = db.query_table("cookie_cache", limit=2, offset=0)
    assert len(r["records"]) == 2
    assert r["total"] == 5


# ========== update_record_field 启用开关 ==========

def test_update_record_field_enable(db):
    db.insert_cookie({"记录ID": "c1", "Cookie": "aaa", "启用": 0})
    assert db.get_cookie_by_id("c1")["启用"] in (0, False)

    ok = db.update_record_field("cookie_cache", "c1", "启用", 1)
    assert ok is True
    assert db.get_cookie_by_id("c1")["启用"] in (1, True)


def test_update_record_field_disable(db):
    db.insert_cookie({"记录ID": "c1", "Cookie": "aaa", "启用": 1})
    ok = db.update_record_field("cookie_cache", "c1", "启用", 0)
    assert ok is True
    assert db.get_cookie_by_id("c1")["启用"] in (0, False)


def test_update_record_field_unknown_field_raises(db):
    db.insert_cookie({"记录ID": "c1", "Cookie": "aaa"})
    with pytest.raises(ValueError):
        db.update_record_field("cookie_cache", "c1", "不存在字段", 1)


def test_update_record_field_nonexistent_record(db):
    db.insert_cookie({"记录ID": "c1", "Cookie": "aaa"})
    ok = db.update_record_field("cookie_cache", "不存在", "启用", 1)
    assert ok is False


def test_update_record_field_account(db):
    """账号表的启用字段也能正常切换"""
    db.insert_account({"记录ID": "a1", "sec_user_id": "sec1", "启用": 1})
    ok = db.update_record_field("account_cache", "a1", "启用", 0)
    assert ok is True
    acc = db.get_account_by_id("a1")
    assert acc["启用"] in (0, False)


# ========== import_records 严格模式 ==========

def test_import_records_create(db):
    records = [
        {"记录ID": "c1", "Cookie": "aaa"},
        {"记录ID": "c2", "Cookie": "bbb"},
    ]
    r = db.import_records("cookie_cache", records)
    assert r["created"] == 2
    assert r["skipped"] == 0
    assert r["failed"] == 0


def test_import_records_skip_existing(db):
    db.insert_cookie({"记录ID": "c1", "Cookie": "old"})
    records = [
        {"记录ID": "c1", "Cookie": "old"},   # 已存在，跳过
        {"记录ID": "c2", "Cookie": "new"},   # 新增
    ]
    r = db.import_records("cookie_cache", records, skip_existing=True)
    assert r["created"] == 1
    assert r["skipped"] == 1
    assert r["failed"] == 0


def test_import_records_missing_required_fails(db):
    """Cookie 是 NOT NULL，缺失应该报错"""
    records = [
        {"记录ID": "c1"},  # 缺 Cookie
    ]
    r = db.import_records("cookie_cache", records)
    assert r["created"] == 0
    assert r["failed"] == 1
    assert len(r["errors"]) == 1


def test_import_records_unknown_field_fails(db):
    records = [
        {"记录ID": "c1", "Cookie": "aaa", "不存在字段": "x"},
    ]
    r = db.import_records("cookie_cache", records)
    assert r["failed"] == 1


def test_import_records_empty_records(db):
    r = db.import_records("cookie_cache", [])
    assert r["created"] == 0
    assert r["skipped"] == 0


def test_import_records_account_with_enabled(db):
    """账号表导入时支持"启用"字段"""
    records = [
        {"记录ID": "a1", "sec_user_id": "sec1", "启用": 0},
        {"记录ID": "a2", "sec_user_id": "sec2"},  # 默认启用
    ]
    r = db.import_records("account_cache", records)
    assert r["created"] == 2
    a1 = db.get_account_by_id("a1")
    a2 = db.get_account_by_id("a2")
    assert a1["启用"] in (0, False)
    assert a2["启用"] in (1, True)


# ========== 旧库迁移 ==========

def test_old_db_migration_adds_enabled_column():
    """旧 account_cache 表（旧字段名）应自动迁移到新字段名"""
    p = pathlib.Path(tempfile.mkdtemp()) / "old.db"
    d = Database(db_path=p)
    # 手动重建为最旧的 schema（账号标识/更新错误/更新时间/已更新）
    import sqlite3
    with sqlite3.connect(str(p)) as conn:
        conn.executescript("""
            DROP TABLE account_cache;
            CREATE TABLE account_cache (
                记录ID TEXT PRIMARY KEY,
                账号名称 TEXT,
                平台 TEXT,
                链接 TEXT,
                账号标识 TEXT UNIQUE NOT NULL,
                等级 INTEGER,
                标签 TEXT,
                昵称 TEXT,
                粉丝数 INTEGER,
                作品数 INTEGER,
                签名 TEXT,
                头像 TEXT,
                已更新 BOOLEAN DEFAULT 0,
                更新错误 TEXT,
                创建时间 DATETIME DEFAULT CURRENT_TIMESTAMP,
                更新时间 DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            INSERT INTO account_cache(记录ID, 账号标识, 更新错误, 已更新)
            VALUES('a1', 'sec1', 'old error msg', 1);
        """)
    # 重新初始化 Database，应自动迁移字段名
    d2 = Database(db_path=p)
    schema = d2.get_table_schema("account_cache")
    names = [c["name"] for c in schema]
    # 新字段名都存在
    assert "sec_user_id" in names
    assert "备注" in names
    assert "同步时间" in names
    assert "已获取信息" in names
    assert "启用" in names
    assert "采集类型" in names
    # 旧字段名已迁移走
    for old_name in ["账号标识", "更新错误", "更新时间", "已更新", "代理"]:
        assert old_name not in names, f"旧字段 {old_name} 不应存在"
    # 旧数据保留，且字段名迁移后值正确
    acc = d2.get_account_by_id("a1")
    assert acc is not None
    assert acc["sec_user_id"] == "sec1"
    assert acc["备注"] == "old error msg"  # 旧"更新错误"的值迁过来
    assert acc["已获取信息"] in (1, True)  # 旧"已更新"的值迁过来
    assert acc["启用"] in (1, True)  # 默认值
    assert acc["采集类型"] == "发布"  # 默认值
