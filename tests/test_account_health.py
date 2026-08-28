import pytest

from app.core.database import Database
from app.main import _account_health


@pytest.fixture
def db(tmp_path):
    return Database(db_path=tmp_path / "health.db")


def _add_account(db, sec_id, name, fetch_status="已获取"):
    db.insert_account({
        "record_id": "rec_" + sec_id,
        "账号名称": name,
        "平台": "douyin",
        "sec_user_id": sec_id,
        "获取状态": fetch_status,
    })


def _add_batch(db, batch_id, items):
    db.create_collection_batch(
        batch_id=batch_id,
        filter_json="{}",
        platform="douyin",
        log_path="",
        items=items,
    )


def test_health_levels_cover_all_cases(db):
    # 健康：解析成功 + 全部采集成功
    _add_account(db, "sec_healthy", "健康号")
    _add_batch(db, "b1", [{"sec_user_id": "sec_healthy", "status": "success", "message": "下载完成"}])
    db.update_collection_batch_item(1, status="success", message="下载完成", finished_at="2026-08-28 10:00:00")

    # 需关注：有成功也有失败
    _add_account(db, "sec_attention", "需关注号")
    _add_batch(db, "b2", [
        {"sec_user_id": "sec_attention", "status": "success", "message": "下载完成"},
        {"sec_user_id": "sec_attention", "status": "failed", "message": "TTD 失败"},
    ])
    db.update_collection_batch_item(2, status="success", message="下载完成", finished_at="2026-08-28 10:00:00")
    db.update_collection_batch_item(3, status="failed", message="TTD 失败", finished_at="2026-08-28 10:01:00")

    # 异常：解析失败
    _add_account(db, "sec_parsefail", "解析失败号", fetch_status="获取失败")

    # 异常：最近采集失败（0成功多失败）
    _add_account(db, "sec_collectfail", "采集失败号")
    _add_batch(db, "b3", [
        {"sec_user_id": "sec_collectfail", "status": "failed", "message": "TTD 返回账号处理失败"},
        {"sec_user_id": "sec_collectfail", "status": "failed", "message": "TTD 返回账号处理失败"},
    ])
    db.update_collection_batch_item(4, status="failed", message="TTD 返回账号处理失败", finished_at="2026-08-28 10:00:00")
    db.update_collection_batch_item(5, status="failed", message="TTD 返回账号处理失败", finished_at="2026-08-28 10:01:00")

    # 未采集
    _add_account(db, "sec_none", "未采集号")

    health = _account_health(db)
    summary = health["summary"]
    assert summary["total"] == 5
    assert summary["healthy"] == 1
    assert summary["attention"] == 1
    assert summary["abnormal"] == 2
    assert summary["uncollected"] == 1

    by_name = {a["name"]: a for a in health["accounts"]}
    assert by_name["健康号"]["level"] == "healthy"
    assert by_name["需关注号"]["level"] == "attention"
    assert by_name["解析失败号"]["level"] == "abnormal"
    assert by_name["采集失败号"]["level"] == "abnormal"
    assert by_name["未采集号"]["level"] == "uncollected"


def test_health_abnormal_sorted_first(db):
    _add_account(db, "sec_a", "A未采集")
    _add_account(db, "sec_b", "B异常", fetch_status="获取失败")
    _add_account(db, "sec_c", "C未采集")
    health = _account_health(db)
    names = [a["name"] for a in health["accounts"]]
    # abnormal 在前，未采集其次（同级别按名称排序），无采集记录的账号
    assert names == ["B异常", "A未采集", "C未采集"]


def test_health_cookie_counts(db):
    health = _account_health(db)
    assert "cookie" in health
    assert health["cookie"]["total"] == 0
    assert health["cookie"]["healthy"] == 0
