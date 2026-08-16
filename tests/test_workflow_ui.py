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


def test_account_navigation_and_terminology_are_renamed(app_env):
    client, *_ = app_env
    response = client.get("/sync/overview")

    assert '<a href="/sync/overview" title="账号" class="nav-group-toggle active"' in response.text
    assert '<h1 class="page-title">账号状态</h1>' in response.text
    for label in (
        "导入分享表",
        "解析分享表",
        "生成账号表",
        "更新账号表",
    ):
        assert f'<span>{label}</span>' in response.text

    assert "采集表" not in response.text
    assert "同步账号表" not in response.text


@pytest.mark.parametrize(
    ("path", "parent_href", "parent_title", "overview_label", "child_href"),
    [
        ("/sync/overview", "/sync/overview", "账号", "账号状态", "/sync/import"),
        ("/collect/overview", "/collect/overview", "采集", "采集概览", "/collect/detail"),
        ("/database", "/database", "数据", "数据概览", "/table"),
    ],
)
def test_overview_belongs_to_parent_not_submenu(
    app_env, path, parent_href, parent_title, overview_label, child_href
):
    client, *_ = app_env
    response = client.get(path)

    assert re.search(
        rf'<a href="{re.escape(parent_href)}" title="{parent_title}" '
        r'class="nav-group-toggle active"',
        response.text,
    )

    group_match = re.search(
        rf'<a href="{re.escape(parent_href)}" title="{parent_title}".*?'
        r'<div class="nav-submenu">(.*?)</div>',
        response.text,
        re.DOTALL,
    )
    assert group_match is not None
    submenu = group_match.group(1)
    assert f'href="{parent_href}"' not in submenu
    assert f'<span>{overview_label}</span>' not in submenu
    assert f'href="{child_href}"' in submenu


def test_account_workflow_pages_use_share_table_copy(app_env):
    client, *_ = app_env
    for path in ("/sync/overview", "/sync/import", "/sync/resolve", "/sync/account"):
        response = client.get(path)
        assert response.status_code == 200
        assert "分享表" in response.text

    generate_page = client.get("/sync/account")
    assert "生成账号表" in generate_page.text
    assert "生成待处理账号" in generate_page.text

    for template in ("overview", "import", "resolve", "account"):
        source = Path(f"app/templates/sync/{template}.html").read_text(encoding="utf-8")
        assert "采集表" not in source
        assert "同步账号表" not in source


def test_account_step_cards_use_text_status_instead_of_circles(app_env, monkeypatch):
    import app.main as app_main

    monkeypatch.setattr(app_main, "get_database", lambda: SyncOverviewFakeDatabase())
    client, *_ = app_env
    response = client.get("/sync/overview")

    assert response.text.count('class="step-status"') == 4
    assert 'data-lucide="circle"' not in response.text
    for text in ("数据源", "待解析", "待生成", "待补齐"):
        assert text in response.text
    assert "分享表数据源为空" in response.text


def test_account_status_distinguishes_empty_source_from_idle_source(
    app_env, monkeypatch
):
    import app.main as app_main

    monkeypatch.setattr(
        app_main,
        "get_database",
        lambda: SyncOverviewFakeDatabase(
            collections=[{"share_code": "a", "sec_user_id": "sec_old"}],
            accounts=[{"sec_user_id": "sec_old", "已获取信息": True}],
        ),
    )
    client, *_ = app_env
    response = client.get("/sync/overview")

    assert "数据源就绪" in response.text
    assert "无待处理" in response.text


def test_nav_parent_click_navigates_and_arrow_toggles():
    source = Path("app/templates/base.html").read_text(encoding="utf-8")

    assert 'class="nav-arrow" onclick="toggleNavGroup(event, this)"' in source
    assert 'onclick="toggleNavGroup(event, this)"' not in source.replace(
        'class="nav-arrow" onclick="toggleNavGroup(event, this)"', ""
    )
    assert "function toggleNavGroup" in source


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


@pytest.mark.parametrize(
    ("path", "button_text"),
    [
        ("/sync/resolve", "解析待处理记录"),
        ("/sync/account", "生成待处理账号"),
        ("/sync/refresh", "更新账号资料"),
    ],
)
def test_sync_step_primary_action_lives_in_workflow_panel(app_env, path, button_text):
    client, *_ = app_env
    response = client.get(path)
    page_head = response.text.split('<div class="workflow-flow"', 1)[0]
    panel = response.text.split('<div class="workflow-panel">', 1)[1]

    assert button_text in panel
    assert "workflow-primary" in panel
    assert 'id="workflow-ready-status"' in panel
    assert "page-actions" not in page_head


def test_sync_overview_is_an_execution_workbench(app_env):
    client, *_ = app_env
    response = client.get("/sync/overview")
    page_head = response.text.split('<div class="workflow-flow"', 1)[0]

    assert "账号工作台" in response.text
    assert 'id="sync-console-status"' in response.text
    assert "待解析" in response.text
    assert "待生成" in response.text
    assert "待补齐" in response.text
    assert "可用 Cookie" in response.text
    assert "TTD 服务" in response.text
    assert 'id="sync-all-btn"' not in page_head
    assert "workflow-primary" in response.text


def test_workflow_primary_button_has_prominent_stable_style():
    css = Path("app/static/css/style.css").read_text(encoding="utf-8")

    assert ".workflow-primary" in css
    assert ".workflow-checklist" in css


def test_sync_workflow_stats_count_ready_and_pending_work():
    from app.main import _sync_workflow_stats

    class FakeDatabase:
        def get_all_collections(self):
            return [
                {"share_code": "a", "sec_user_id": ""},
                {"share_code": "b", "sec_user_id": "sec1"},
                {"share_code": "", "sec_user_id": ""},
            ]

        def get_all_accounts(self):
            return [
                {"sec_user_id": "sec1", "已获取信息": False},
                {"sec_user_id": "sec2", "已获取信息": True},
                {"sec_user_id": "", "已获取信息": False},
            ]

        def get_enabled_cookies(self):
            return [{"Cookie": "ok"}]

    stats = _sync_workflow_stats(FakeDatabase())

    assert stats == {
        "pending_resolve": 1,
        "ready_accounts": 1,
        "collections_total": 3,
        "ready_to_sync": 0,
        "accounts_total": 3,
        "pending_refresh": 1,
        "cookies": 1,
    }


def test_sync_workflow_stats_separate_resolved_from_existing_accounts():
    from app.main import _sync_workflow_stats

    class FakeDatabase:
        def get_all_collections(self):
            return [
                {"share_code": "new", "sec_user_id": "sec_new"},
                {"share_code": "old", "sec_user_id": "sec_old"},
            ]

        def get_all_accounts(self):
            return [{"sec_user_id": "sec_old", "已获取信息": True}]

        def get_enabled_cookies(self):
            return []

    stats = _sync_workflow_stats(FakeDatabase())

    assert stats["ready_accounts"] == 2
    assert stats["ready_to_sync"] == 1


class SyncOverviewFakeDatabase:
    def __init__(self, collections=(), accounts=(), histories=None):
        self.collections = list(collections)
        self.accounts = list(accounts)
        self.histories = histories or {}

    def get_all_collections(self):
        return self.collections

    def get_all_accounts(self):
        return self.accounts

    def get_enabled_cookies(self):
        return [{"Cookie": "ok"}]

    def get_sync_history(self, task_type, limit=20):
        return self.histories.get(task_type, [])


@pytest.mark.parametrize(
    (
        "collections",
        "accounts",
        "contains",
        "absent",
    ),
    [
        (
            [],
            [],
            ['href="/sync/import"', "去导入分享表", "分享表数据源为空"],
            ['id="sync-all-btn"', "处理账号数据"],
        ),
        (
            [{"share_code": "a", "sec_user_id": ""}],
            [],
            ['id="sync-all-btn"', "继续处理", "待解析 1 条"],
            ["去导入分享表"],
        ),
        (
            [{"share_code": "a", "sec_user_id": "sec_new"}],
            [],
            ['id="sync-all-btn"', "生成待处理账号", "待生成 1 条"],
            ["去导入分享表"],
        ),
        (
            [{"share_code": "a", "sec_user_id": "sec_old"}],
            [{"sec_user_id": "sec_old", "已获取信息": True}],
            ["暂无可处理数据", 'id="sync-all-btn"', "disabled"],
            ["去导入分享表", ">继续处理</button>"],
        ),
    ],
)
def test_sync_overview_recommends_next_action_from_local_data(
    app_env, monkeypatch, collections, accounts, contains, absent
):
    import app.main as app_main

    fake_database = SyncOverviewFakeDatabase(
        collections=collections,
        accounts=accounts,
        histories={
            "import_collection": [
                {"status": "success", "success": 3, "failed": 0, "created_at": "2026-08-15 10:00:00"}
            ]
        },
    )
    monkeypatch.setattr(app_main, "get_database", lambda: fake_database)
    client, *_ = app_env
    response = client.get("/sync/overview")

    assert response.status_code == 200
    assert "账号状态" in response.text
    assert "处理本地已导入的分享表数据" in response.text
    assert "最近导入" in response.text
    assert "最近处理结果" in response.text
    for text in contains:
        assert text in response.text
    for text in absent:
        assert text not in response.text


def test_collect_navigation_uses_grouped_submenu(app_env):
    client, *_ = app_env
    response = client.get("/collect")
    source = Path("app/templates/base.html").read_text(encoding="utf-8")

    assert '<a href="/collect/overview" title="采集" class="nav-group-toggle"' in response.text
    assert 'href="/collect" title="日常增量采集"' in response.text
    assert 'href="/collect/detail" title="单作品采集"' in response.text
    assert "querySelectorAll('.nav-group').forEach" in source
    assert "groups[0]" not in source
    assert "groups[1]" not in source


def test_collect_overview_is_parent_default_page(app_env):
    client, *_ = app_env
    response = client.get("/collect/overview")

    assert response.status_code == 200
    assert "采集概览" in response.text
    assert "当前批次" in response.text
    assert "可用 Cookie" in response.text
    assert 'data-ttd-status="true"' in response.text
    assert 'href="/collect"' in response.text
    assert 'href="/collect/detail"' in response.text
    assert '<a href="/collect/overview" title="采集" class="nav-group-toggle active"' in response.text


def test_data_module_keeps_cloud_sync_inside_overview(app_env):
    client, *_ = app_env
    response = client.get("/database")
    cloud_response = client.get("/sync/cloud", follow_redirects=False)

    assert "数据概览" in response.text
    assert "云端同步" in response.text
    assert 'href="/sync/cloud" title="云端同步"' not in response.text
    assert cloud_response.status_code == 307
    assert cloud_response.headers["location"] == "/database"


def test_collect_pages_are_separated_by_route(app_env):
    client, *_ = app_env
    account_page = client.get("/collect")
    detail_page = client.get("/collect/detail")

    assert account_page.status_code == 200
    assert "日常增量采集" in account_page.text
    assert 'id="account-form"' in account_page.text
    assert 'id="detail-form"' not in account_page.text
    assert 'id="collection-tabs"' not in account_page.text

    assert detail_page.status_code == 200
    assert "单作品采集" in detail_page.text
    assert 'id="detail-form"' in detail_page.text
    assert 'id="account-form"' not in detail_page.text
    assert 'id="collection-tabs"' not in detail_page.text
    assert "命名模板" in detail_page.text
    assert "下载历史" in detail_page.text


def test_collect_detail_route_marks_its_own_submenu_item(app_env):
    client, *_ = app_env
    response = client.get("/collect/detail")

    assert '<a href="/collect/detail" title="单作品采集" class="active"' in response.text
    assert '<a href="/collect" title="日常增量采集" class="active"' not in response.text


def test_legacy_collect_detail_query_redirects_to_dedicated_route(app_env):
    client, *_ = app_env
    response = client.get("/collect?mode=detail", follow_redirects=False)

    assert response.status_code == 307
    assert response.headers["location"] == "/collect/detail"


def test_nav_group_toggle_does_not_navigate_away():
    source = Path("app/templates/base.html").read_text(encoding="utf-8")
    match = re.search(r"function toggleNavGroup\(.*?\n        \}", source, re.DOTALL)
    assert match is not None

    script = f"""
        {match.group(0)}
        globalThis.loadPage = () => {{ throw new Error('parent toggle must not navigate'); }};
        globalThis.document = {{
            querySelector: () => null
        }};
        const event = {{
            preventDefault: () => {{}},
            stopPropagation: () => {{}}
        }};
        let expanded = false;
        const element = {{
            closest: () => ({{ classList: {{ toggle: () => {{ expanded = !expanded; }} }} }})
        }};
        toggleNavGroup(event, element);
        console.log(JSON.stringify({{expanded}}));
    """
    result = subprocess.run(
        ["node", "-e", script],
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(result.stdout) == {"expanded": True}


def test_collect_submenu_places_single_work_first(app_env):
    client, *_ = app_env
    response = client.get("/collect")
    submenu = response.text.split('title="采集" class="nav-group-toggle"', 1)[1]
    submenu = submenu.split('title="数据" class="nav-group-toggle"', 1)[0]

    assert submenu.index('href="/collect/detail"') < submenu.index('href="/collect"')


@pytest.mark.parametrize(
    "path",
    ["/sync/overview", "/sync/resolve", "/sync/account", "/sync/refresh"],
)
def test_sync_pages_probe_ttd_after_render(app_env, monkeypatch, path):
    import app.main as app_main

    def blocked_probe():
        raise AssertionError("page render must not probe external services")

    monkeypatch.setattr(app_main, "get_services", blocked_probe)
    client, *_ = app_env
    response = client.get(path)
    source = Path("app/templates/base.html").read_text(encoding="utf-8")

    assert response.status_code == 200
    assert 'data-ttd-status="true"' in response.text
    assert "_doukhubRefreshTTDStatus" in source


def test_sync_overview_preserves_four_step_flow(app_env):
    client, *_ = app_env
    response = client.get("/sync/overview")
    assert "workflow-flow" in response.text
    assert "导入分享表" in response.text
    assert "解析分享表" in response.text
    assert "生成账号表" in response.text
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
    assert "workflow-panel" in response.text
    assert 'id="detail-form"' not in response.text
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


def test_all_workflow_pages_use_shared_status_component(app_env):
    client, *_ = app_env
    for path in [
        "/sync/overview",
        "/sync/import",
        "/sync/resolve",
        "/sync/account",
        "/sync/refresh",
        "/collect",
        "/collect/detail",
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
