# -*- coding: utf-8 -*-
"""账号表 UID 回填（从作品下载路径反推）的用例。

背景：uid 是 v2.4.2 才加的列，历史账号全是空的；而增量采集与「刷新账号」
两条路径都够不着"已获取"的老账号。这里验证免请求的回填路径：
从 collection_works.download_dir 里的 `UID{数字}_...` 反推 uid，精确写回账号表。
"""

import pytest

from app.core.database import Database


@pytest.fixture
def db(tmp_path):
    return Database(db_path=tmp_path / "uid.db")


def _add_account(db, sec_id, name, uid=""):
    db.insert_account({
        "record_id": "rec_" + sec_id,
        "账号名称": name,
        "平台": "douyin",
        "sec_user_id": sec_id,
        "获取状态": "已获取",
        "uid": uid or None,
    })


def _add_work(db, sec_id, download_dir, aweme_id="7001"):
    db.upsert_collection_work(
        batch_id="b1",
        sec_user_id=sec_id,
        account_name="示例",
        platform="douyin",
        aweme_id=aweme_id,
        download_dir=download_dir,
    )


def _uid_of(db, sec_id):
    with db._connect() as conn:
        row = conn.execute(
            "SELECT uid FROM account_cache WHERE sec_user_id = ?", (sec_id,)
        ).fetchone()
    return str(row[0] or "") if row else ""


def test_backfill_reads_uid_from_download_dir(db):
    _add_account(db, "sec_a", "小奶猫")
    _add_work(db, "sec_a", "/media/many/UID100050252999_小奶猫_发布作品")

    assert db.backfill_account_uids() == 1
    assert _uid_of(db, "sec_a") == "100050252999"


def test_backfill_handles_nested_work_subdir(db):
    """路径指向作品子目录时也要能抠出账号层级的 uid。"""
    _add_account(db, "sec_b", "阿垚")
    _add_work(db, "sec_b", "/media/many/UID8888_阿垚_发布作品/2026-09")

    assert db.backfill_account_uids() == 1
    assert _uid_of(db, "sec_b") == "8888"


def test_backfill_does_not_overwrite_existing_uid(db):
    _add_account(db, "sec_c", "已有UID", uid="999")
    _add_work(db, "sec_c", "/media/many/UID1000_已有UID_发布作品")

    assert db.backfill_account_uids() == 0
    assert _uid_of(db, "sec_c") == "999"


def test_backfill_ignores_paths_without_uid(db):
    _add_account(db, "sec_d", "老目录")
    _add_work(db, "sec_d", "/media/many/老目录/2025")

    assert db.backfill_account_uids() == 0
    assert _uid_of(db, "sec_d") == ""


def test_backfill_is_idempotent(db):
    _add_account(db, "sec_e", "重复跑")
    _add_work(db, "sec_e", "/media/many/UID777_重复跑_发布作品")

    assert db.backfill_account_uids() == 1
    # 第二次跑：uid 已有值 → 不再计入，也不重复写
    assert db.backfill_account_uids() == 0
    assert _uid_of(db, "sec_e") == "777"
