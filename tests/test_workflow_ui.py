from pathlib import Path

import pytest

from tests.test_api import app_env


def test_stylesheet_defines_shared_workflow_components():
    css = Path("app/static/css/style.css").read_text(encoding="utf-8")
    required = [
        ".workflow-panel",
        ".workflow-header",
        ".workflow-title",
        ".workflow-actions",
        ".workflow-notice",
        ".workflow-flow",
        ".workflow-step",
        ".workflow-metrics",
        ".workflow-metric",
        ".workflow-progress-meta",
        ".workflow-log",
        ".workflow-status",
        ".workflow-history-item",
        ".workflow-history-header",
        ".workflow-history-detail",
        ".workflow-tabs",
        ".workflow-tab",
        ".workflow-form-grid",
    ]
    for selector in required:
        assert selector in css


def test_workflow_system_is_responsive_and_motion_restrained():
    css = Path("app/static/css/style.css").read_text(encoding="utf-8")
    assert ".workflow-flow" in css
    assert ".workflow-log" in css
    assert "prefers-reduced-motion" in css


@pytest.mark.parametrize(
    "path",
    [
        "/sync/overview",
        "/sync/import",
        "/sync/resolve",
        "/sync/account",
        "/sync/refresh",
    ],
)
def test_sync_pages_use_shared_workflow_panels(app_env, path):
    client, *_ = app_env
    response = client.get(path)
    assert response.status_code == 200
    assert "workflow-panel" in response.text


def test_sync_overview_preserves_four_step_flow(app_env):
    client, *_ = app_env
    response = client.get("/sync/overview")
    assert "workflow-flow" in response.text
    assert "导入采集表" in response.text
    assert "解析采集表" in response.text
    assert "同步账号表" in response.text
    assert "更新账号表" in response.text


def test_sync_templates_do_not_redefine_shared_workflow_css():
    paths = [
        Path("app/templates/sync/overview.html"),
        Path("app/templates/sync/import.html"),
        Path("app/templates/sync/resolve.html"),
        Path("app/templates/sync/account.html"),
        Path("app/templates/sync/refresh.html"),
    ]
    for path in paths:
        source = path.read_text(encoding="utf-8")
        assert ".workflow-" not in source
        assert ".sync-log-box" not in source
        assert ".sync-stat-card" not in source
