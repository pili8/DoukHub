from unittest.mock import AsyncMock, MagicMock
from pathlib import Path
import re

import pytest
from fastapi.testclient import TestClient

import app.main as app_main


@pytest.fixture
def batch_client():
    database = MagicMock()
    manager = MagicMock()
    manager.start = AsyncMock(
        return_value=[{"id": "b1", "platform": "douyin", "status": "pending"}]
    )
    manager.cancel.return_value = True
    manager.read_log.return_value = ["raw log"]
    database.get_all_accounts.return_value = []
    database.list_collection_batches.return_value = [
        {"id": "b1", "platform": "douyin", "status": "pending"}
    ]
    database.get_collection_batch.return_value = {
        "id": "b1",
        "platform": "douyin",
        "status": "pending",
    }
    database.get_collection_batch_items.return_value = [
        {
            "id": 1,
            "sec_user_id": "sec1",
            "account_name": "一号",
            "status": "pending",
            "message": "",
        }
    ]

    saved = (
        app_main.config,
        app_main.database,
        app_main.collection_batch_manager,
    )
    app_main.config = MagicMock()
    app_main.database = database
    app_main.collection_batch_manager = manager
    try:
        yield TestClient(app_main.app), database, manager
    finally:
        app_main.config, app_main.database, app_main.collection_batch_manager = saved


def test_start_batch(batch_client):
    client, _, manager = batch_client
    response = client.post(
        "/api/collection/batches",
        json={"rating_min": 3, "platform": "douyin", "mode": "incremental"},
    )
    assert response.status_code == 200
    assert response.json()["batches"][0]["id"] == "b1"
    assert manager.start.await_args.kwargs["rating_min"] == 3


def test_start_batch_rejects_empty_selection(batch_client):
    client, _, manager = batch_client
    manager.start = AsyncMock(side_effect=ValueError("没有符合条件的账号"))
    response = client.post("/api/collection/batches", json={})
    assert response.status_code == 400
    assert "没有符合条件的账号" in response.json()["message"]


def test_batch_detail_contains_items_and_log(batch_client):
    client, _, _ = batch_client
    response = client.get("/api/collection/batches/b1")
    assert response.status_code == 200
    data = response.json()
    assert data["batch"]["id"] == "b1"
    assert data["items"][0]["sec_user_id"] == "sec1"
    assert data["log"] == ["raw log"]


def test_cancel_batch(batch_client):
    client, _, manager = batch_client
    response = client.post("/api/collection/batches/b1/cancel")
    assert response.status_code == 200
    assert response.json()["success"] is True
    manager.cancel.assert_called_once_with("b1")


def test_retry_failed_items_creates_new_batch(batch_client):
    client, database, manager = batch_client
    database.get_collection_batch_items.return_value = [
        {
            "account_record_id": "a1",
            "sec_user_id": "sec1",
            "account_name": "一号",
            "status": "failed",
        },
        {
            "account_record_id": "a2",
            "sec_user_id": "sec2",
            "account_name": "二号",
            "status": "success",
        },
    ]
    response = client.post(
        "/api/collection/batches/b1/retry", json={"mode": "full"}
    )
    assert response.status_code == 200
    assert manager.start.await_args.kwargs["record_ids"] == ["a1"]
    assert manager.start.await_args.kwargs["mode"] == "full"


@pytest.fixture
def single_client(monkeypatch):
    saved = app_main.single_work_client
    app_main.single_work_client = MagicMock()
    try:
        yield TestClient(app_main.app)
    finally:
        app_main.single_work_client = saved


def test_resolve_single_works(single_client, monkeypatch):
    from app.core import single_work

    async def fake_fetch(client, ttd_url, link, platform):
        return {
            "id": "1234567890123456789",
            "title": "标题",
            "author": "作者",
            "create_time": "2026-08-15 10-00-00",
            "type": "视频",
            "downloads": ["https://example.com/video"],
            "share_url": link,
            "platform": platform,
        }

    monkeypatch.setattr(single_work, "fetch_work", fake_fetch)
    link = "https://www.douyin.com/video/1234567890123456789"
    monkeypatch.setattr(app_main, "_extract_single_work_links", lambda text: [(link, "douyin")])
    response = single_client.post("/api/collection/works/resolve", json={"links": link})
    assert response.status_code == 200
    assert response.json()["works"][0]["title"] == "标题"


def test_download_single_works(single_client, tmp_path, monkeypatch):
    from app.core import single_work

    async def fake_fetch(client, ttd_url, link, platform):
        return {"id": "1", "title": "标题", "downloads": ["https://example.com/a"]}

    async def fake_download(client, work, target_dir, template):
        path = target_dir / "saved.mp4"
        path.write_bytes(b"data")
        return [path]

    monkeypatch.setattr(single_work, "fetch_work", fake_fetch)
    monkeypatch.setattr(single_work, "download_work", fake_download)
    link = "https://www.douyin.com/video/1234567890123456789"
    monkeypatch.setattr(app_main, "_extract_single_work_links", lambda text: [(link, "douyin")])
    response = single_client.post(
        "/api/collection/works/download",
        json={
            "links": link,
            "target_dir": str(tmp_path),
            "filename_template": "{author} {title}",
        },
    )
    assert response.status_code == 200
    assert response.json()["results"][0]["status"] == "success"


def test_collect_page_state_is_safe_for_spa_script_reload():
    source = Path("app/templates/collect.html").read_text(encoding="utf-8")
    declarations = re.search(
        r"(?m)^\s*let\s+(?:resolvedSingleLinks|singleDirCurrent|singleDirEntries)\b",
        source,
    )
    assert declarations is None
