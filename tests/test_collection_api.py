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
    saved_client = app_main.single_work_client
    saved_db = app_main.database
    app_main.single_work_client = MagicMock()
    mock_db = MagicMock()
    mock_db.create_single_work_history.return_value = 1
    app_main.database = mock_db
    try:
        yield TestClient(app_main.app)
    finally:
        app_main.single_work_client = saved_client
        app_main.database = saved_db


@pytest.fixture
def prefs_client(tmp_path):
    """提供临时 Config，隔离单作品偏好持久化"""
    from app.core.config import Config
    saved_config = app_main.config
    app_main.config = Config(tmp_path / "config.json")
    try:
        yield TestClient(app_main.app), app_main.config, tmp_path
    finally:
        app_main.config = saved_config


def test_get_single_work_preferences_returns_defaults(prefs_client):
    client, config, _ = prefs_client
    response = client.get("/api/collection/single-work/preferences")
    assert response.status_code == 200
    prefs = response.json()["preferences"]
    assert prefs["default_template_id"] == "default"
    assert prefs["templates"][0]["template"] == "{create_time} {author} {title}"
    assert prefs["recent_dirs"] == []


def test_save_single_work_preferences_persists(prefs_client):
    client, config, tmp_path = prefs_client
    download_dir = tmp_path / "SingleWorks"
    download_dir.mkdir()
    response = client.put(
        "/api/collection/single-work/preferences",
        json={
            "download_path": str(download_dir),
            "recent_dirs": [str(download_dir), str(tmp_path)],
            "default_template_id": "archival",
            "templates": [{
                "id": "archival",
                "name": "归档",
                "template": "{create_time} {id} {title}",
                "is_default": True,
            }],
        },
    )
    assert response.status_code == 200
    assert response.json()["success"] is True
    assert config.download_path == download_dir
    assert config.single_work["recent_dirs"][0] == str(download_dir)
    assert config.single_work["default_template_id"] == "archival"


def test_save_single_work_preferences_rejects_unsafe_template(prefs_client):
    client, config, tmp_path = prefs_client
    response = client.put(
        "/api/collection/single-work/preferences",
        json={
            "templates": [{
                "id": "bad",
                "name": "坏",
                "template": "../{title}",
            }],
        },
    )
    assert response.status_code == 400
    assert response.json()["message"] == "命名模板不能包含路径分隔符或绝对路径"


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

    async def fake_download(client, work, target_dir, template="", **kwargs):
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
    source = Path("app/templates/collect_detail.html").read_text(encoding="utf-8")
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

    async def fake_download(client, work, target_dir, template="", **kwargs):
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
    source = Path("app/templates/collect_detail.html").read_text(encoding="utf-8")
    assert "invalidateResolvedSingleWorks" in source
    assert (
        'oninput="invalidateResolvedSingleWorks()" '
        'onchange="invalidateResolvedSingleWorks()"' in source
    )
    assert "resolvedSingleLinks = [];" in source


def test_collect_page_discards_stale_resolve_response():
    source = Path("app/templates/collect_detail.html").read_text(encoding="utf-8")
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
    source = Path("app/templates/collect_detail.html").read_text(encoding="utf-8")
    assert "singleDirParent = data.parent || '';" in source
    assert "if (!parent || parent === singleDirCurrent) return;" in source
    assert "singleDirCurrent.replace(/[\\\\/]" not in source


def test_collect_page_formats_single_work_storage_time():
    source = Path("app/templates/collect_detail.html").read_text(encoding="utf-8")
    assert ".replace(/ (\\d\\d)-(\\d\\d)-(\\d\\d)$/, ' $1:$2:$3')" in source


def test_collect_detail_page_contains_asset_template_and_history_controls():
    source = Path("app/templates/collect_detail.html").read_text(encoding="utf-8")
    for token in (
        'id="single-work-list"',
        'id="single-history-list"',
        'id="template-modal"',
        'id="template-parts"',
        "downloadSingleAsset",
        "retrySingleWorkHistory",
        "dragSingleTemplatePart",
    ):
        assert token in source


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
    database.create_collection_batch.assert_not_called()
    database.insert_account.assert_not_called()
    database.update_account.assert_not_called()
    database.delete_account.assert_not_called()


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


def test_collect_page_discards_stale_preview_response():
    source = Path("app/templates/collect.html").read_text(encoding="utf-8")
    assert "var previewGeneration = 0;" in source
    assert source.index("var previewGeneration = 0;") < source.index(
        "previewCollectionScope();"
    )
    preview = re.search(
        r"async function previewCollectionScope\(\) \{([\s\S]*?)\n    \}",
        source,
    )
    assert preview is not None
    body = preview.group(1)
    assert "const previewGenerationToken = ++previewGeneration;" in body
    guard = "if (previewGenerationToken !== previewGeneration) return;"
    assert body.count(guard) == 2

    success_body, _, error_body = body.partition("} catch (error) {")
    success_updates = success_body.partition(guard)[2]
    error_updates = error_body.partition(guard)[2]
    for update_body in (success_updates, error_updates):
        assert "preview-total" in update_body
        assert "status.className" in update_body

    queue = re.search(
        r"function queueCollectionPreview\(\) \{([\s\S]*?)\n    \}", source
    )
    cleanup = re.search(
        r"window\._spaCleanup = \(\) => \{([\s\S]*?)\n    \};", source
    )
    assert queue is not None and "previewGeneration += 1;" in queue.group(1)
    assert cleanup is not None and "previewGeneration += 1;" in cleanup.group(1)


@pytest.fixture
def history_client(tmp_path, monkeypatch):
    """提供临时 Config + mock 数据库的客户端，用于下载历史和重试测试"""
    from app.core.config import Config
    saved_config = app_main.config
    saved_db = app_main.database
    saved_client = app_main.single_work_client
    app_main.config = Config(tmp_path / "config.json")
    mock_db = MagicMock()
    mock_db.create_single_work_history.return_value = 1
    mock_db.get_single_work_history.return_value = {
        "id": 1,
        "work_id": "1234567890123456789",
        "source_link": "https://www.douyin.com/video/1234567890123456789",
        "platform": "douyin",
        "work_type": "图集",
        "title": "标题",
        "author": "作者",
        "filename_template": "{author} {title}",
        "filename_override": "",
        "target_dir": str(tmp_path),
        "files_json": "[]",
        "request_json": '{"asset_indexes":[2]}',
        "status": "failed",
        "error": "timeout",
        "work_json": None,
        "created_at": "2026-08-16 10:00:00",
        "updated_at": "2026-08-16 10:01:00",
    }
    mock_db.list_single_work_history.return_value = [mock_db.get_single_work_history.return_value]
    app_main.database = mock_db
    app_main.single_work_client = MagicMock()
    try:
        yield TestClient(app_main.app), mock_db, tmp_path
    finally:
        app_main.config = saved_config
        app_main.database = saved_db
        app_main.single_work_client = saved_client


def test_download_records_history_with_asset_selection(history_client, monkeypatch):
    from app.core import single_work

    async def fake_fetch(client, ttd_url, link, platform):
        return {
            "id": "1234567890123456789",
            "title": "标题",
            "author": "作者",
            "type": "图集",
            "platform": "douyin",
            "downloads": ["https://example.com/a", "https://example.com/b"],
            "assets": [
                {"kind": "image", "index": 1, "url": "https://example.com/a"},
                {"kind": "image", "index": 2, "url": "https://example.com/b"},
            ],
        }

    async def fake_download(client, work, target_dir, template="", **kwargs):
        path = target_dir / "saved.jpg"
        path.write_bytes(b"data")
        return [path]

    monkeypatch.setattr(single_work, "fetch_work", fake_fetch)
    monkeypatch.setattr(single_work, "download_work", fake_download)
    link = "https://www.douyin.com/video/1234567890123456789"
    monkeypatch.setattr(
        app_main, "_extract_single_work_links", lambda text: [(link, "douyin")]
    )
    client, mock_db, tmp_path = history_client
    response = client.post(
        "/api/collection/works/download",
        json={
            "links": link,
            "target_dir": str(tmp_path),
            "filename_template": "{author} {title}",
            "filename_overrides": {"1234567890123456789": "自定义"},
            "asset_indexes": [2],
        },
    )
    assert response.status_code == 200
    result = response.json()["results"][0]
    assert result["status"] == "success"
    assert "history_id" in result
    mock_db.create_single_work_history.assert_called_once()
    mock_db.update_single_work_history.assert_called()


def test_download_records_failed_history(history_client, monkeypatch):
    from app.core import single_work

    async def fake_fetch(client, ttd_url, link, platform):
        raise RuntimeError("network error")

    monkeypatch.setattr(single_work, "fetch_work", fake_fetch)
    link = "https://www.douyin.com/video/1234567890123456789"
    monkeypatch.setattr(
        app_main, "_extract_single_work_links", lambda text: [(link, "douyin")]
    )
    client, mock_db, tmp_path = history_client
    response = client.post(
        "/api/collection/works/download",
        json={
            "links": link,
            "target_dir": str(tmp_path),
            "filename_template": "{author} {title}",
        },
    )
    assert response.status_code == 200
    result = response.json()["results"][0]
    assert result["status"] == "failed"
    assert "history_id" in result
    mock_db.update_single_work_history.assert_called_with(
        1, status="failed", error="network error"
    )


def test_get_single_work_history_list(history_client):
    client, mock_db, _ = history_client
    response = client.get("/api/collection/works/history")
    assert response.status_code == 200
    data = response.json()
    assert "history" in data
    assert len(data["history"]) == 1


def test_retry_single_work_history(history_client, monkeypatch):
    from app.core import single_work

    async def fake_fetch(client, ttd_url, link, platform):
        return {
            "id": "1234567890123456789",
            "title": "标题",
            "author": "作者",
            "type": "图集",
            "platform": "douyin",
            "downloads": ["https://example.com/a"],
            "assets": [
                {"kind": "image", "index": 1, "url": "https://example.com/a"},
            ],
        }

    async def fake_download(client, work, target_dir, template="", **kwargs):
        path = target_dir / "saved.jpg"
        path.write_bytes(b"data")
        return [path]

    monkeypatch.setattr(single_work, "fetch_work", fake_fetch)
    monkeypatch.setattr(single_work, "download_work", fake_download)
    client, mock_db, tmp_path = history_client
    mock_db.create_single_work_history.return_value = 2
    response = client.post(
        "/api/collection/works/history/1/retry",
        json={"target_dir": str(tmp_path)},
    )
    assert response.status_code == 200
    result = response.json()["results"][0]
    assert result["status"] == "success"
    assert result["history_id"] == 2


def test_retry_returns_404_for_missing_history(history_client):
    client, mock_db, _ = history_client
    mock_db.get_single_work_history.return_value = None
    response = client.post(
        "/api/collection/works/history/999/retry",
        json={"target_dir": str(history_client[2])},
    )
    assert response.status_code == 404
