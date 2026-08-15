import json
import re
import subprocess
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
    assert "历史记录" in response.text or path in ("/sync/overview", "/sync/import")
    assert 'class="workflow-log history-log"' in response.text or path == "/sync/overview"

    if path == "/sync/overview":
        for element_id in ("sync-all-btn", "sync-progress", "sync-progress-bar", "sync-progress-text"):
            assert f'id="{element_id}"' in response.text
    elif path == "/sync/import":
        for element_id in ("import-text", "exec-btn", "result-card", "exec-log"):
            assert f'id="{element_id}"' in response.text
        assert 'class="workflow-log"' in response.text
    else:
        for element_id in (
            "exec-btn",
            "exec-progress",
            "progress-bar",
            "progress-text",
            "progress-count",
            "exec-log",
        ):
            assert f'id="{element_id}"' in response.text


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


def test_collect_page_is_daily_incremental_console(app_env):
    client, *_ = app_env
    response = client.get("/collect")
    assert response.status_code == 200
    assert "日常增量采集" in response.text
    assert "workflow-tabs" in response.text
    assert "workflow-panel" in response.text
    assert "单作品采集" in response.text
    assert "开始日常增量采集" in response.text
    assert 'id="collection-last-run"' in response.text
    assert 'id="collection-last-success"' in response.text


def test_collect_page_contains_preview_metrics(app_env):
    client, *_ = app_env
    response = client.get("/collect")
    assert 'id="preview-total"' in response.text
    assert 'id="preview-first-run"' in response.text
    assert 'id="preview-incremental"' in response.text
    assert 'id="preview-skipped"' in response.text


def test_collect_page_uses_workflow_tab_active_state(app_env):
    client, *_ = app_env
    response = client.get("/collect")
    assert "classList.toggle('active', mode === 'account')" in response.text


def test_all_workflow_pages_use_shared_status_component(app_env):
    client, *_ = app_env
    for path in [
        "/sync/overview",
        "/sync/import",
        "/sync/resolve",
        "/sync/account",
        "/sync/refresh",
        "/collect",
    ]:
        response = client.get(path)
        assert response.status_code == 200
        assert "workflow-panel" in response.text
        assert "workflow-status" in response.text or path in ("/sync/import", "/sync/overview")


def test_collection_batch_panel_renders_live_progress_and_actions(app_env):
    client, *_ = app_env
    response = client.get("/collect")
    source = Path("app/templates/collect.html").read_text(encoding="utf-8")

    assert "function batchProgressPercent(items)" in source
    assert 'id="batch-progress-bar"' in source
    assert 'id="batch-progress-text"' in source
    assert "function renderBatchDetailActions(batch)" in source
    assert "renderBatchDetailActions(batch)" in source
    assert "cancelCollectionBatch('${batch.id}')" in source
    assert "retryCollectionBatch('${batch.id}')" in source
    assert "['pending', 'running', 'cancelling'].includes(batch.status)" in source
    assert "const automaticBatch = selectBatchDetail(data.batches || [])" in source
    assert "showBatchDetail(automaticBatch.id, true)" in source
    assert "batch-detail-actions" in response.text


def test_collection_detail_selection_executes_without_active_batch():
    source = Path("app/templates/collect.html").read_text(encoding="utf-8")
    match = re.search(
        r"function selectBatchDetail\(batches\) \{.*?\n    \}",
        source,
        re.DOTALL,
    )
    assert match is not None
    cases = [
        [],
        [{"id": "latest", "status": "completed"}],
        [
            {"id": "latest", "status": "completed"},
            {"id": "active", "status": "running"},
        ],
    ]
    script = (
        f"{match.group(0)}; "
        f"console.log(JSON.stringify({cases}.map(selectBatchDetail)))"
    )
    result = subprocess.run(
        ["node", "-e", script],
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(result.stdout) == [
        None,
        {"id": "latest", "status": "completed"},
        {"id": "active", "status": "running"},
    ]


def test_collection_preview_cannot_overwrite_batch_status(app_env):
    client, *_ = app_env
    response = client.get("/collect")
    source = Path("app/templates/collect.html").read_text(encoding="utf-8")

    preview_body = source.split("async function previewCollectionScope()", 1)[1]
    preview_body = preview_body.split("function queueCollectionPreview()", 1)[0]
    batch_body = source.split("function updateCollectionStatus(batches)", 1)[1]
    batch_body = batch_body.split("async function refreshCollectionBatches()", 1)[0]

    assert 'id="collection-status"' in response.text
    assert 'id="collection-preview-status"' in response.text
    assert "collection-preview-status" in preview_body
    assert "collection-status" not in preview_body
    assert "collection-status" in batch_body
    assert "collection-preview-status" not in batch_body


def test_cancelled_workflow_status_uses_warning_style():
    css = Path("app/static/css/style.css").read_text(encoding="utf-8")
    pending_group = css.split(".workflow-status.pending", 1)[1]
    pending_group = pending_group.split(".workflow-status.running", 1)[0]
    warning_group = css.split(".workflow-status.skipped", 1)[1]
    warning_group = warning_group.split("}\n", 1)[0]

    assert ".workflow-status.cancelled" not in pending_group
    assert ".workflow-status.cancelled" in warning_group


def test_sync_overview_workflow_steps_include_status_icons(app_env):
    client, *_ = app_env
    response = client.get("/sync/overview")
    css = Path("app/static/css/style.css").read_text(encoding="utf-8")

    assert response.text.count('class="step-status"') == 4
    assert ".workflow-step .step-status" in css


def test_sync_import_page_includes_dependency_notice(app_env):
    client, *_ = app_env
    response = client.get("/sync/import")

    assert 'class="workflow-notice"' in response.text


def test_workflow_flow_scrolls_horizontally_on_mobile():
    css = Path("app/static/css/style.css").read_text(encoding="utf-8")
    mobile_media = css.split("@media (max-width: 720px)", 1)[1]
    mobile_media = mobile_media.split("@media", 1)[0]
    flow_rule = mobile_media.split(".workflow-flow", 1)[1]
    flow_rule = flow_rule.split(".workflow-metrics", 1)[0]

    assert "overflow-x: auto" in flow_rule
    assert "grid-template-columns: repeat(4, minmax(220px, 1fr))" in flow_rule


def test_collection_tabs_expose_aria_semantics_and_keyboard_navigation(app_env):
    client, *_ = app_env
    response = client.get("/collect")
    source = Path("app/templates/collect.html").read_text(encoding="utf-8")

    assert 'role="tablist"' in response.text
    assert response.text.count('role="tab"') == 2
    assert 'aria-selected="true"' in response.text
    assert 'aria-selected="false"' in response.text
    assert 'aria-controls="account-form-card"' in response.text
    assert 'aria-controls="detail-form-card"' in response.text
    assert response.text.count('role="tabpanel"') == 2
    assert "collectionTabKeydown" in source
    assert "ArrowRight" in source
    assert "tabIndex" in source
    assert "setAttribute('aria-selected'" in source


def test_shared_history_headers_use_expanded_button_semantics():
    macro = Path("app/templates/sync/_workflow.html").read_text(encoding="utf-8")
    css = Path("app/static/css/style.css").read_text(encoding="utf-8")
    script_paths = [
        Path("app/templates/sync/import.html"),
        Path("app/templates/sync/resolve.html"),
        Path("app/templates/sync/account.html"),
        Path("app/templates/sync/refresh.html"),
    ]

    assert '<button type="button" class="workflow-history-header"' in macro
    assert 'aria-expanded="false"' in macro
    assert 'aria-controls="workflow-history-detail-{{ loop.index }}"' in macro
    assert 'id="workflow-history-detail-{{ loop.index }}"' in macro
    assert "width: 100%" in css
    assert "border: none" in css
    for path in script_paths:
        source = path.read_text(encoding="utf-8")
        assert "el.setAttribute('aria-expanded', String(expanded));" in source
