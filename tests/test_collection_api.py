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


def test_retry_returns_400_when_no_source_accounts_remain_eligible(
    batch_client,
):
    client, database, manager = batch_client
    database.get_collection_batch_items.return_value = [
        {
            "account_record_id": "a1",
            "sec_user_id": "sec1",
            "account_name": "一号",
            "status": "failed",
        }
    ]
    manager.start = AsyncMock(side_effect=ValueError("没有符合条件的账号"))

    response = client.post(
        "/api/collection/batches/b1/retry", json={"mode": "incremental"}
    )

    assert response.status_code == 400
    assert response.json()["message"] == "没有符合条件的账号"


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


@pytest.mark.parametrize(
    "filename_template",
    [
        "../escaped/{title}",
        "..\\escaped\\{title}",
        "C:\\{title}",
        "{title}:\\escaped",
        "{author:/../x}",
    ],
)
def test_download_rejects_unsafe_filename_templates(
    single_client, tmp_path, monkeypatch, filename_template
):
    from app.core import single_work

    async def fake_download(client, work, target_dir, template):
        path = target_dir / "saved.mp4"
        path.write_bytes(b"data")
        return [path]

    monkeypatch.setattr(single_work, "download_work", fake_download)
    link = "https://www.douyin.com/video/1234567890123456789"
    monkeypatch.setattr(
        app_main, "_extract_single_work_links", lambda text: [(link, "douyin")]
    )
    response = single_client.post(
        "/api/collection/works/download",
        json={
            "links": link,
            "target_dir": str(tmp_path),
            "filename_template": filename_template,
        },
    )
    assert response.status_code == 400
    assert response.json()["message"] == "命名模板不能包含路径分隔符或绝对路径"
    assert not list(tmp_path.iterdir())


def test_collect_page_invalidates_resolved_links_on_edit():
    source = Path("app/templates/collect.html").read_text(encoding="utf-8")
    assert "invalidateResolvedSingleWorks" in source
    assert (
        'oninput="invalidateResolvedSingleWorks()" '
        'onchange="invalidateResolvedSingleWorks()"' in source
    )
    assert "resolvedSingleLinks = [];" in source


def test_collect_page_discards_stale_resolve_response():
    source = Path("app/templates/collect.html").read_text(encoding="utf-8")
    assert "var resolveGeneration = 0;" in source
    assert "resolveGeneration += 1;" in source
    assert "const submittedLinks = String(form.get('links') || '');" in source
    assert "const generation = ++resolveGeneration;" in source
    assert "const currentLinks = String(linksInput.value || '');" in source
    assert (
        "if (generation !== resolveGeneration || currentLinks !== submittedLinks)"
        " return;" in source
    )
    assert source.count(
        "if (generation !== resolveGeneration || currentLinks !== submittedLinks)"
        " return;"
    ) == 2


@pytest.mark.parametrize(
    "filename_template",
    ["{title", "{unknown}", "{0}", "{title.foo}"],
)
def test_download_rejects_malformed_filename_templates(
    single_client, tmp_path, monkeypatch, filename_template
):
    link = "https://www.douyin.com/video/1234567890123456789"
    monkeypatch.setattr(
        app_main, "_extract_single_work_links", lambda text: [(link, "douyin")]
    )
    response = single_client.post(
        "/api/collection/works/download",
        json={
            "links": link,
            "target_dir": str(tmp_path),
            "filename_template": filename_template,
        },
    )
    assert response.status_code == 400
    assert response.json()["message"] == "命名模板格式无效"
    assert not list(tmp_path.iterdir())


def test_collect_page_uses_canonical_browse_parent():
    source = Path("app/templates/collect.html").read_text(encoding="utf-8")
    assert "singleDirParent = data.parent || '';" in source
    assert "if (!parent || parent === singleDirCurrent) return;" in source
    assert "singleDirCurrent.replace(/[\\\\/]" not in source


def test_collect_page_formats_single_work_storage_time():
    source = Path("app/templates/collect.html").read_text(encoding="utf-8")
    assert ".replace(/ (\\d\\d)-(\\d\\d)-(\\d\\d)$/, ' $1:$2:$3')" in source


def test_collect_page_shows_batch_progress_summary():
    source = Path("app/templates/collect.html").read_text(encoding="utf-8")
    assert ".batch-summary-grid" not in source
    assert '<div class="workflow-metrics" style="margin-bottom:12px;">' in source
    assert 'class="workflow-log"' in source
    assert "function batchElapsedSeconds(batch)" in source
    assert "function currentAccountIndex(items)" in source
    assert "预计账号" in source
    assert "已运行" in source
    assert "当前账号" in source
    assert 'batch.total_accounts || 0' in source


def test_collection_preview_is_read_only(batch_client):
    client, database, manager = batch_client
    database.get_all_accounts.return_value = [
        {
            "record_id": "a1",
            "账号名称": "新账号",
            "平台": "抖音",
            "链接": "",
            "sec_user_id": "sec1",
            "等级": 4,
            "标签": "",
            "启用": 1,
            "last_collected_at": None,
            "collect_window_days": None,
        },
        {
            "record_id": "a2",
            "账号名称": "已采集账号",
            "平台": "抖音",
            "链接": "",
            "sec_user_id": "sec2",
            "等级": 4,
            "标签": "",
            "启用": 1,
            "last_collected_at": "2026-08-14 10:00:00",
            "collect_window_days": None,
        },
        {
            "record_id": "a3",
            "账号名称": "TikTok",
            "平台": "TikTok",
            "链接": "",
            "sec_user_id": "tiksec",
            "等级": 4,
            "标签": "",
            "启用": 1,
            "last_collected_at": None,
            "collect_window_days": None,
        },
    ]
    response = client.post(
        "/api/collection/batches/preview",
        json={"rating_min": 3, "platform": "all", "mode": "incremental"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True
    assert data["total_accounts"] == 3
    assert data["first_run_accounts"] == 1
    assert data["incremental_accounts"] == 1
    assert data["skipped_accounts"] == 1
    assert data["platforms"][0]["platform"] == "douyin"
    assert data["platforms"][0]["total_accounts"] == 2
    assert manager.start.await_count == 0


def test_collection_preview_returns_400_when_no_accounts_match(batch_client):
    client, database, manager = batch_client
    database.get_all_accounts.return_value = []
    response = client.post("/api/collection/batches/preview", json={})
    assert response.status_code == 400
    assert response.json()["message"] == "没有符合条件的账号"
    manager.start.assert_not_called()


def test_collect_page_calls_preview_without_starting_batch():
    source = Path("app/templates/collect.html").read_text(encoding="utf-8")
    assert "/api/collection/batches/preview" in source
    assert "previewCollectionScope(" in source
    assert "startCollectionBatch" in source
