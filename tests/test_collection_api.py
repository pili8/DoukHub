from unittest.mock import AsyncMock, MagicMock
from pathlib import Path
from urllib.parse import quote
import re

import pytest
from fastapi.testclient import TestClient

import app.main as app_main


@pytest.fixture
def batch_client(tmp_path, monkeypatch):
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

    # v2.2.5 起：批次/单作品入口都要求可用的存储方案，测试用临时目录充当主方案
    storage_profile = {
        "profiles": [
            {
                "id": "sp1",
                "name": "测试方案",
                "path": str(tmp_path),
                "role": "primary",
                "enabled": True,
            }
        ]
    }
    config = MagicMock()
    config.storage_profiles = {"batch": storage_profile, "single": storage_profile}

    # 低空间确认分支：磁盘紧张时 /api/collection/batches 会提前返回 needs_confirm，
    # 根本不会调用 manager.start。宿主机剩余空间不该左右测试结果
    # （本机 C: 只剩 2.3GB 时这两个用例就误报过），这里统一伪装成空间充足；
    # 低空间分支本身由文件末尾 test_start_batch_asks_confirm_when_disk_low 覆盖。
    import shutil as _shutil

    class _RoomyUsage:
        total = 100 * 1024**3
        used = 0
        free = 100 * 1024**3

    monkeypatch.setattr(_shutil, "disk_usage", lambda _path: _RoomyUsage)

    saved = (
        app_main.config,
        app_main.database,
        app_main.collection_batch_manager,
    )
    app_main.config = config
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


def test_start_batch_asks_confirm_when_disk_low(batch_client, monkeypatch):
    """剩余空间 <5GB 时先返回 needs_confirm（不启动批次）；带 force_low_space 才继续。

    这是 /api/collection/batches 里真实存在的分支，原先没有任何用例覆盖，
    导致它一旦被误触发（例如宿主机磁盘只剩 2GB），别的用例会以 KeyError: 'batches' 的形式误报。
    """
    client, _, manager = batch_client
    import shutil as _shutil

    class _TightUsage:
        total = 10 * 1024**3
        used = 9 * 1024**3
        free = 1 * 1024**3  # 剩 1GB，低于 5GB 阈值

    monkeypatch.setattr(_shutil, "disk_usage", lambda _path: _TightUsage)

    response = client.post("/api/collection/batches", json={})
    assert response.status_code == 200
    data = response.json()
    assert data["needs_confirm"] is True
    assert "剩余空间不足" in data["message"]
    assert "batches" not in data
    assert manager.start.await_count == 0  # 确认前不得真的启动批次

    # 用户确认后带 force_low_space 重发 → 走正常流程、返回批次
    response = client.post(
        "/api/collection/batches", json={"force_low_space": True}
    )
    assert response.status_code == 200
    assert response.json()["batches"][0]["id"] == "b1"


def test_batch_detail_contains_items_and_log(batch_client):
    client, _, _ = batch_client
    response = client.get("/api/collection/batches/b1")
    assert response.status_code == 200
    data = response.json()
    assert data["batch"]["id"] == "b1"
    assert data["items"][0]["sec_user_id"] == "sec1"
    assert data["log"] == ["raw log"]
    # 批次无 log_path → log_exists 应为 False
    assert data["log_exists"] is False


def test_batch_detail_log_exists_true_when_file_present(batch_client, tmp_path):
    client, database, _ = batch_client
    log_file = tmp_path / "batch.log"
    log_file.write_text("line\n", encoding="utf-8")

    database.get_collection_batch.return_value = {
        "id": "b1",
        "platform": "douyin",
        "status": "pending",
        "log_path": str(log_file),
    }
    response = client.get("/api/collection/batches/b1")
    assert response.status_code == 200
    assert response.json()["log_exists"] is True


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
def prefs_client(tmp_path, monkeypatch):
    """提供临时 Config，隔离单作品偏好持久化"""
    from app.core.config import Config
    # 屏蔽 DB 回退：否则配置会读写真实用户库，造成跨测试污染（历史遗留 bug）
    monkeypatch.setattr("app.core.config._try_load_db_config", lambda: None)
    monkeypatch.setattr("app.core.config._try_save_db_config", lambda data: False)
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
    # v2.2.5 起偏好由存储方案生成：无任何方案时（全新安装）为空列表
    assert prefs["templates"] == []
    assert prefs["default_template_id"] == "default"
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

    async def fake_fetch(client, ttd_url, link, platform, cookie="", mode="auto", **kwargs):
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


def test_spa_rebind_scripts_ignores_non_javascript_scripts():
    source = Path("app/templates/base.html").read_text(encoding="utf-8")
    assert "function isExecutableScript(" in source
    assert ".filter(isExecutableScript)" in source
    assert "'application/json'" not in source.split(
        "function isExecutableScript("
    )[1].split("function ")[0]


def test_proxy_download_supports_unicode_filenames(single_client, monkeypatch):
    class FakeUpstream:
        headers = {"content-type": "video/mp4", "content-length": "4"}

        def raise_for_status(self):
            pass

    class FakeStreamResponse:
        headers = FakeUpstream.headers

        def raise_for_status(self):
            pass

        async def aiter_bytes(self, chunk_size):
            yield b"data"

    class FakeStreamContext:
        async def __aenter__(self):
            return FakeStreamResponse()

        async def __aexit__(self, exc_type, exc, tb):
            pass

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            pass

        async def get(self, url):
            return FakeUpstream()

        def stream(self, method, url):
            return FakeStreamContext()

    monkeypatch.setattr(app_main.httpx, "AsyncClient", FakeAsyncClient)
    response = single_client.get(
        "/api/collection/works/proxy-download",
        params={"url": "https://example.test/video", "filename": "闫梦茹 测试"},
    )

    assert response.status_code == 200
    assert response.content == b"data"
    expected_name = quote("闫梦茹 测试.mp4", safe="")
    assert response.headers["Content-Disposition"] == (
        f'attachment; filename="download.mp4"; '
        f"filename*=UTF-8''{expected_name}"
    )


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
    js = Path("app/static/js/collect_detail.js").read_text(encoding="utf-8")
    assert "invalidateResolvedSingleWorks" in source
    # 链接输入即失效已解析结果（oninput 挂在链接输入框上）
    assert 'oninput="renderLinkLines(); invalidateResolvedSingleWorks()"' in source
    # JS 侧：失效时清空已解析集合并推进代数
    assert "resolvedSingleLinks = [];" in js
    assert "resolveGeneration += 1;" in js


def test_collect_page_discards_stale_resolve_response():
    # 解析已改为 SSE 流式（/resolve-stream）；进行中禁用解析/下载按钮防并发旧响应
    js = Path("app/static/js/collect_detail.js").read_text(encoding="utf-8")
    assert "var resolveGeneration = 0;" in js
    assert "resolveGeneration += 1;" in js
    assert "/api/collection/works/resolve-stream" in js
    assert "btnR.disabled = true;" in js
    assert "btnR.disabled = false;" in js


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


def test_collect_page_targets_dir_from_storage_profiles():
    # v2.2.5 起目录浏览被移除，保存目录由主/次存储方案下拉决定
    source = Path("app/templates/collect_detail.html").read_text(encoding="utf-8")
    assert 'id="single-storage-primary"' in source
    assert 'id="single-storage-secondary"' in source
    assert "onStorageSelectChange" in source


def test_collect_page_formats_single_work_storage_time():
    # 时间格式化已统一到 collect.html 的 formatDateTime（detail 页共用）
    source = Path("app/templates/collect.html").read_text(encoding="utf-8")
    assert ".replace(/ (\\d\\d)-(\\d\\d)-(\\d\\d)$/, ' $1:$2:$3')" in source


def test_collect_detail_page_contains_asset_template_and_history_controls():
    source = Path("app/templates/collect_detail.html").read_text(encoding="utf-8")
    js = Path("app/static/js/collect_detail.js").read_text(encoding="utf-8")
    for token, haystack in (
        ('id="single-work-list"', source),
        ('id="single-history-list"', source),
        # 命名模板编辑器收敛为共享组件（name_editor.js），不再有独立 template-modal
        ("name_editor.js", source),
        ("downloadSingleAsset", js),
        ("retrySingleWorkHistory", js),
    ):
        assert token in haystack


def test_collect_page_shows_batch_progress_summary():
    source = Path("app/templates/collect.html").read_text(encoding="utf-8")
    assert ".batch-summary-grid" not in source
    # 运行中详情：已处理计数、细进度条、当前账号行、流式日志区
    assert 'id="run-done-count"' in source
    assert 'id="run-progress-bar"' in source
    assert 'id="run-current-account"' in source
    assert 'class="run-log-section"' in source
    assert "function batchElapsedSeconds(batch)" in source
    assert "已运行" in source
    assert "当前账号" in source
    assert 'batch.total_accounts || 0' in source


def _fake_preset(**overrides):
    preset = {
        "id": 1,
        "name": "测试方案",
        "rating_min": 3,
        "tags": "",
        "platform": "all",
        "mode": "incremental",
        "account_names": "",
        "folder_name": "",
        "name_format": "",
    }
    preset.update(overrides)
    return preset


def test_collection_preview_is_read_only(batch_client, monkeypatch):
    client, database, manager = batch_client
    database.get_all_accounts.return_value = [
        {
            "record_id": "a1",
            "账号名称": "新账号",
            "平台": "douyin",
            "链接": "",
            "sec_user_id": "sec1",
            "获取状态": "已获取",
            "等级": 4,
            "标签": "",
            "启用": 1,
            "last_collected_at": None,
            "collect_window_days": None,
        },
        {
            "record_id": "a2",
            "账号名称": "已采集账号",
            "平台": "douyin",
            "链接": "",
            "sec_user_id": "sec2",
            "获取状态": "已获取",
            "等级": 4,
            "标签": "",
            "启用": 1,
            "last_collected_at": "2026-08-14 10:00:00",
            "collect_window_days": None,
        },
        {
            "record_id": "a3",
            "账号名称": "TikTok",
            "平台": "tiktok",
            "链接": "",
            "sec_user_id": "tiksec",
            "获取状态": "已获取",
            "等级": 4,
            "标签": "",
            "启用": 1,
            "last_collected_at": None,
            "collect_window_days": None,
        },
    ]
    # v2.2.5 起预览走方案（preset）：/api/collection/presets/{id}/preview
    monkeypatch.setattr(
        app_main.presets, "get_preset", lambda config, pid: _fake_preset()
    )
    response = client.post("/api/collection/presets/1/preview")
    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True
    assert data["total_accounts"] == 3
    # a1 首采、a2 增量、a3 因缺 TikTok 主页链接记为 skipped
    assert data["first_run_accounts"] == 1
    assert data["incremental_accounts"] == 1
    assert data["skipped_accounts"] == 1
    assert manager.start.await_count == 0
    database.create_collection_batch.assert_not_called()
    database.insert_account.assert_not_called()
    database.update_account.assert_not_called()
    database.delete_account.assert_not_called()


def test_collection_preview_returns_400_when_no_accounts_match(
    batch_client, monkeypatch
):
    client, database, manager = batch_client
    database.get_all_accounts.return_value = []
    monkeypatch.setattr(
        app_main.presets, "get_preset", lambda config, pid: _fake_preset()
    )
    response = client.post("/api/collection/presets/1/preview")
    assert response.status_code == 400
    assert response.json()["message"] == "没有符合条件的账号"
    manager.start.assert_not_called()


def test_collect_page_calls_preview_without_starting_batch():
    # 预览（方案维度）与启动批次是两条独立调用，页面加载不触发启动
    source = Path("app/templates/collect.html").read_text(encoding="utf-8")
    assert "/api/collection/presets/' + presetId + '/preview'" in source
    assert "'/api/collection/batches', 'POST'" in source
    assert "function startCollection()" in source
    assert "async function previewPreset(presetId)" in source


def test_collect_page_preview_resets_before_request():
    # 每次预览先重置旧结果；未选方案不发请求；失败回落为 0
    source = Path("app/templates/collect.html").read_text(encoding="utf-8")
    assert "els[k].textContent = '-');" in source
    assert "if (!presetId) return;" in source
    assert "els[k].textContent = '0');" in source


def test_collect_page_discards_stale_preview_response():
    # 旧版 generation 令牌已随方案化重构移除；现行契约 = 请求前先重置 + 未选方案短路
    source = Path("app/templates/collect.html").read_text(encoding="utf-8")
    assert "previewGeneration" not in source
    assert "async function previewPreset(presetId)" in source
    assert "Object.keys(els).forEach(k => els[k].textContent = '-');" in source
    assert "if (!presetId) return;" in source


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
