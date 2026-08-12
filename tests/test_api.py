"""API 端点测试"""
import json
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
from fastapi.testclient import TestClient

from app.core.config import Config
from app.core.history import HistoryDB


@pytest.fixture
def app_env(tmp_path):
    """创建隔离的测试环境，替换 app.main 中的全局对象"""
    config_file = tmp_path / "config.json"
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    test_config = Config(config_file)
    test_history = HistoryDB(data_dir)

    # 用 MagicMock 避免真实启动 Downloader 服务
    mock_services = MagicMock()
    mock_services.status_all.return_value = [
        {"name": "TikTokDownloader", "port": 5555, "running": False, "url": "http://127.0.0.1:5555"},
        {"name": "XHS-Downloader", "port": 5556, "running": False, "url": "http://127.0.0.1:5556"},
    ]
    mock_services.get_versions.return_value = []

    mock_scheduler = MagicMock()
    mock_scheduler.get_jobs_info.return_value = []

    import app.main as app_main

    # 保存原始值
    orig = {
        "config": app_main.config,
        "history": app_main.history,
        "services": app_main.services,
        "scheduler": app_main.scheduler,
        "feishu_client": app_main.feishu_client,
        "collector": app_main.collector,
        "syncer": app_main.syncer,
    }

    # 替换为测试对象
    app_main.config = test_config
    app_main.history = test_history
    app_main.services = mock_services
    app_main.scheduler = mock_scheduler
    app_main.feishu_client = None
    app_main.collector = None
    app_main.syncer = None

    from app.main import app
    client = TestClient(app)

    yield client, test_config, test_history, data_dir

    # 恢复原始值
    for key, val in orig.items():
        setattr(app_main, key, val)


class TestPageRoutes:
    """页面路由测试"""

    def test_sync_page(self, app_env):
        client, *_ = app_env
        r = client.get("/sync")
        assert r.status_code == 200
        assert "同步" in r.text

    def test_collect_page(self, app_env):
        client, *_ = app_env
        r = client.get("/collect")
        assert r.status_code == 200
        assert "采集" in r.text

    def test_history_page(self, app_env):
        client, *_ = app_env
        r = client.get("/history")
        assert r.status_code == 200
        assert "记录" in r.text

    def test_settings_page(self, app_env):
        client, *_ = app_env
        r = client.get("/settings")
        assert r.status_code == 200
        assert "设置" in r.text


class TestAPIEndpoints:
    """API 端点测试"""

    def test_get_stats(self, app_env):
        client, *_ = app_env
        r = client.get("/api/stats")
        assert r.status_code == 200
        data = r.json()
        assert "total" in data
        assert "today" in data
        assert "success" in data
        assert "failed" in data

    def test_get_services_status(self, app_env):
        client, *_ = app_env
        r = client.get("/api/services/status")
        assert r.status_code == 200
        data = r.json()
        assert "services" in data
        assert len(data["services"]) == 2

    def test_get_services_versions(self, app_env):
        client, *_ = app_env
        r = client.get("/api/services/versions")
        assert r.status_code == 200

    def test_get_settings(self, app_env):
        client, *_ = app_env
        r = client.get("/api/settings")
        assert r.status_code == 200
        data = r.json()
        assert "feishu" in data
        assert "downloader" in data
        assert "cookie" in data

    def test_post_settings(self, app_env):
        client, config, *_ = app_env
        r = client.post("/api/settings", json={"concurrent_accounts": 5})
        assert r.status_code == 200
        assert r.json()["success"] is True
        assert config.concurrent_accounts == 5

    def test_get_schedule(self, app_env):
        client, *_ = app_env
        r = client.get("/api/schedule")
        assert r.status_code == 200
        data = r.json()
        assert "tasks" in data

    def test_get_accounts_empty(self, app_env):
        client, *_ = app_env
        r = client.get("/api/accounts")
        assert r.status_code == 200
        data = r.json()
        assert "accounts" in data
        assert data["accounts"] == []

    def test_get_history_empty(self, app_env):
        client, *_ = app_env
        r = client.get("/api/history")
        assert r.status_code == 200
        data = r.json()
        assert "records" in data
        assert data["records"] == []

    def test_get_history_with_params(self, app_env):
        client, *_ = app_env
        r = client.get("/api/history?limit=10&offset=0&status=success")
        assert r.status_code == 200

    def test_history_add_and_retrieve(self, app_env):
        """通过真实 HistoryDB 添加记录并查询"""
        client, _, history, _ = app_env
        history.add_record({
            "account_name": "API测试账号",
            "platform": "抖音",
            "status": "success",
            "works_count": 5,
        })
        r = client.get("/api/history")
        data = r.json()
        assert len(data["records"]) == 1
        assert data["records"][0]["account_name"] == "API测试账号"

    def test_test_feishu_not_configured(self, app_env):
        client, *_ = app_env
        r = client.post("/api/test-feishu")
        assert r.status_code == 200
        data = r.json()
        assert data["success"] is False

    def test_sync_not_configured(self, app_env):
        client, *_ = app_env
        r = client.post("/api/sync")
        assert r.status_code == 400

    def test_ensure_fields_not_configured(self, app_env):
        client, *_ = app_env
        r = client.post("/api/ensure-fields")
        assert r.status_code == 400

    def test_browse_dir_root(self, app_env):
        client, *_ = app_env
        r = client.get("/api/browse-dir?path=")
        assert r.status_code == 200
        data = r.json()
        assert "current" in data
        assert "dirs" in data
        # 空 path 时返回驱动器列表或多盘情况，current 可能为空
        # 非空 path 时返回该目录的子目录

    def test_browse_dir_specific_path(self, app_env, tmp_path):
        client, *_ = app_env
        (tmp_path / "subdir_a").mkdir()
        (tmp_path / "subdir_b").mkdir()
        r = client.get(f"/api/browse-dir?path={tmp_path}")
        assert r.status_code == 200
        data = r.json()
        dir_names = [Path(d).name for d in data["dirs"]]
        assert "subdir_a" in dir_names
        assert "subdir_b" in dir_names

    def test_stats_reflects_history(self, app_env):
        """统计数据随记录变化"""
        client, _, history, _ = app_env
        history.add_record({"status": "success"})
        history.add_record({"status": "failed"})

        r = client.get("/api/stats")
        data = r.json()
        assert data["total"] == 2
        assert data["success"] == 1
        assert data["failed"] == 1

    def test_api_table_filter_contains(self, app_env, tmp_path):
        """API 透传列级筛选：contains 生效"""
        client, *_ = app_env
        import app.main as app_main
        from app.core.database import Database

        # app_env 未替换 app_main.database（会连真实库），这里临时替换为隔离库并恢复
        orig_db = app_main.database
        try:
            app_main.database = Database(tmp_path / "test.db")
            db = app_main.database
            db.insert_cookie({"record_id": "c1", "Cookie": "abc123"})
            db.insert_cookie({"record_id": "c2", "Cookie": "def456"})

            r = client.get(
                "/api/database/table/cookie_cache",
                params={"filter_field": "Cookie", "filter_value": "bc", "filter_op": "contains"},
            )
            assert r.status_code == 200
            data = r.json()
            assert "records" in data and "total" in data
            assert data["total"] == 1
            assert data["records"][0]["record_id"] == "c1"
        finally:
            app_main.database = orig_db

    def test_api_table_filter_equals(self, app_env, tmp_path):
        """API 透传列级筛选：equals 生效"""
        client, *_ = app_env
        import app.main as app_main
        from app.core.database import Database

        orig_db = app_main.database
        try:
            app_main.database = Database(tmp_path / "test.db")
            db = app_main.database
            db.insert_cookie({"record_id": "c1", "Cookie": "aaa", "备注": "测试"})
            db.insert_cookie({"record_id": "c2", "Cookie": "bbb", "备注": "其他"})

            r = client.get(
                "/api/database/table/cookie_cache",
                params={"filter_field": "备注", "filter_value": "测试", "filter_op": "equals"},
            )
            assert r.status_code == 200
            data = r.json()
            assert data["total"] == 1
            assert data["records"][0]["record_id"] == "c1"
        finally:
            app_main.database = orig_db

    def test_api_table_invalid_name(self, app_env):
        """表名校验：无效表名返回 400"""
        client, *_ = app_env
        r = client.get("/api/database/table/unknown_table")
        assert r.status_code == 400
