from pathlib import Path

import re
import pytest

from tests.test_api import app_env


def test_base_loads_canonical_stylesheet_and_local_lucide():
    source = Path("app/templates/base.html").read_text(encoding="utf-8")
    assert 'href="/static/css/style.css?v=5"' in source
    assert "theme-material.css" not in source
    assert 'src="/static/js/lucide.min.js"' in source
    assert "https://unpkg.com/lucide" not in source


def test_doukhub_design_tokens_are_defined():
    css = Path("app/static/css/style.css").read_text(encoding="utf-8")
    tokens = {
        "--dh-background": "#FBFAF7",
        "--dh-surface": "#FFFFFF",
        "--dh-surface-muted": "#F5F2EC",
        "--dh-text": "#1F1B17",
        "--dh-text-secondary": "#5C564E",
        "--dh-text-muted": "#A39E96",
        "--dh-border": "#ECE7DF",
        "--dh-border-strong": "#D9D3C8",
        "--dh-accent": "#0061A4",
        "--dh-accent-hover": "#004F8A",
        "--dh-accent-soft": "#E7F0F8",
        "--dh-danger": "#C2410C",
        "--dh-warning": "#B45309",
        "--dh-success": "#16A34A",
        "--dh-radius-sm": "6px",
        "--dh-radius": "8px",
        "--dh-radius-lg": "12px",
    }
    for token, value in tokens.items():
        assert f"{token}: {value};" in css


def test_all_template_custom_property_references_are_defined():
    css = Path("app/static/css/style.css").read_text(encoding="utf-8")
    defined_tokens = set(re.findall(r"(?m)^\s*(--[\w-]+)\s*:", css))
    referenced_tokens = set()

    for template in Path("app/templates").rglob("*.html"):
        source = template.read_text(encoding="utf-8")
        referenced_tokens.update(re.findall(r"var\(\s*(--[\w-]+)", source))

    assert referenced_tokens
    assert referenced_tokens <= defined_tokens


def test_doukhub_dark_theme_is_explicit_and_complete():
    css = Path("app/static/css/style.css").read_text(encoding="utf-8")
    selector = ':root[data-theme="dark"]'

    assert selector in css
    assert "@media (prefers-color-scheme: dark)" not in css

    dark_block = css.split(selector, 1)[1].split("}", 1)[0]
    dark_tokens = set(re.findall(r"(--[\w-]+)\s*:", dark_block))
    all_dh_tokens = {
        "--dh-background",
        "--dh-surface",
        "--dh-surface-muted",
        "--dh-text",
        "--dh-text-secondary",
        "--dh-text-muted",
        "--dh-border",
        "--dh-border-strong",
        "--dh-accent",
        "--dh-accent-hover",
        "--dh-accent-soft",
        "--dh-danger",
        "--dh-danger-soft",
        "--dh-warning",
        "--dh-warning-soft",
        "--dh-success",
        "--dh-success-soft",
        "--dh-radius-sm",
        "--dh-radius",
        "--dh-radius-lg",
        "--dh-shadow-sm",
        "--dh-shadow-lg",
        "--dh-font",
        "--dh-mono",
    }
    assert all_dh_tokens <= dark_tokens
    assert "--dh-accent: #4DA3FF;" in dark_block
    assert "--dh-accent-hover: #7BBFFF;" in dark_block


def _declaration_map(block):
    block = re.sub(r"/\*.*?\*/", "", block, flags=re.DOTALL)
    declarations = {}
    for declaration in block.split(";"):
        name, separator, value = declaration.partition(":")
        if separator:
            declarations[name.strip()] = value.strip()
    return declarations


def _style_rules(source):
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.DOTALL)
    rules = {}
    for selectors, declarations in re.findall(r"([^{}]+)\{([^{}]*)\}", source):
        for selector in selectors.split(","):
            rules.setdefault(selector.strip(), declarations.strip())
    return rules


def _resolved_value(value, variables):
    while True:
        match = re.fullmatch(r"var\((--[\w-]+)(?:,\s*([^()]+))?\)", value)
        if not match:
            return value
        name, fallback = match.groups()
        value = variables.get(name, fallback or "")


def _rule_declarations(source, selector):
    match = re.search(rf"{re.escape(selector)}\s*\{{([^{{}}]*)\}}", source)
    return _declaration_map(match.group(1))


def _contrast_ratio(first, second):
    def relative_luminance(hex_color):
        channels = [int(hex_color[index : index + 2], 16) / 255 for index in range(1, 7, 2)]
        linear = [channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4 for channel in channels]
        return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

    lighter = max(relative_luminance(first), relative_luminance(second))
    darker = min(relative_luminance(first), relative_luminance(second))
    return (lighter + 0.05) / (darker + 0.05)


def test_dark_legacy_components_resolve_through_doukhub_palette():
    css = Path("app/static/css/style.css").read_text(encoding="utf-8")
    root = _declaration_map(css.split(":root {", 1)[1].split("}", 1)[0])
    dark = root | _declaration_map(css.split(':root[data-theme="dark"] {', 1)[1].split("}", 1)[0])

    for selector in (".workflow-status.pending", ".toast-info"):
        rule = _rule_declarations(css, selector)
        background = _resolved_value(rule["background"], dark)
        color = _resolved_value(rule["color"], dark)
        assert _resolved_value(root["--md-surface-container-high"], root) == root["--dh-surface-muted"]
        assert background == dark["--dh-surface-muted"]
        assert _contrast_ratio(background, color) >= 4.5

    for role in ("primary", "error", "success", "warning", "info"):
        assert dark[f"--md-on-{role}"] == "#17130F"

    for selector in (".btn-primary", ".btn-danger"):
        rule = _rule_declarations(css, selector)
        background = _resolved_value(rule["background"], dark)
        color = _resolved_value(rule["color"], dark)
        assert _contrast_ratio(background, color) >= 4.5


def test_sidebar_active_contract_is_white_on_readable_blue():
    css = Path("app/static/css/style.css").read_text(encoding="utf-8")
    source = Path("app/templates/base.html").read_text(encoding="utf-8")
    root = _declaration_map(css.split(":root {", 1)[1].split("}", 1)[0])
    dark = root | _declaration_map(css.split(':root[data-theme="dark"] {', 1)[1].split("}", 1)[0])

    assert root["--text-on-accent"] == "#FFFFFF"
    assert dark["--text-on-accent"] == "#FFFFFF"
    assert root["--accent-active"] == "#0061A4"
    assert dark["--accent-active"] == "#0061A4"

    active_selectors = (
        ".sidebar-nav a.active",
        ".nav-group .nav-group-toggle.active",
        ".nav-group .nav-submenu a.active",
    )
    for selector in active_selectors:
        rule = _rule_declarations(source, selector)
        assert rule["background"] == "var(--accent-active)"
        assert rule["color"] == "var(--text-on-accent, #fff)"
        assert _contrast_ratio(root["--accent-active"], root["--text-on-accent"]) >= 4.5
        assert _contrast_ratio(dark["--accent-active"], dark["--text-on-accent"]) >= 4.5

    indicator = _rule_declarations(source, ".sidebar-nav a.active::before")
    assert indicator["background"] == "var(--text-on-accent, #fff)"


def test_dark_soft_tonal_components_have_opaque_accessible_pairs():
    css = Path("app/static/css/style.css").read_text(encoding="utf-8")
    rules = _style_rules(css)
    root = _declaration_map(css.split(":root {", 1)[1].split("}", 1)[0])
    dark = root | _declaration_map(css.split(':root[data-theme="dark"] {', 1)[1].split("}", 1)[0])

    cases = {
        ".btn-success": ("--md-success-container", "--md-on-success-container"),
        ".btn-warning": ("--md-warning-container", "--md-on-warning-container"),
        ".toast-success": ("--md-success-container", "--md-on-success-container"),
        ".toast-error": ("--md-error-container", "--md-on-error-container"),
        ".workflow-status.success": ("--md-success-container", "--md-on-success-container"),
        ".workflow-status.failed": ("--md-error-container", "--md-on-error-container"),
        ".workflow-status.warning": ("--md-warning-container", "--md-on-warning-container"),
    }
    hex_color = re.compile(r"#[0-9A-Fa-f]{6}")
    for selector, (background_token, color_token) in cases.items():
        rule = _declaration_map(rules[selector])
        assert rule["background"] == f"var({background_token})"
        assert rule["color"] == f"var({color_token})"
        background = _resolved_value(dark[background_token], dark)
        color = _resolved_value(dark[color_token], dark)
        assert hex_color.fullmatch(background)
        assert hex_color.fullmatch(color)
        assert _contrast_ratio(background, color) >= 4.5


def test_sidebar_active_indicator_is_preserved():
    source = Path("app/templates/base.html").read_text(encoding="utf-8")
    assert ".sidebar-nav a.active::before" in source
    assert "width: 3px;" in source
    assert "height: 60%;" in source
    assert "background: var(--text-on-accent, #fff);" in source
    assert "border-radius: 0 3px 3px 0;" in source


def test_core_component_rules_use_enthub_scale():
    css = Path("app/static/css/style.css").read_text(encoding="utf-8")
    assert ".page-head {" in css
    assert ".page-title {" in css
    assert ".page-sub {" in css
    assert ".page-actions {" in css
    assert ".workflow-panel {" in css
    assert ".workflow-tab.active {" in css
    assert "height: 34px;" in css
    assert "height: 32px;" in css
    assert "border-radius: var(--dh-radius-sm);" in css


def test_canonical_stylesheet_is_served(app_env):
    client, *_ = app_env
    response = client.get("/static/css/style.css")
    assert response.status_code == 200
    assert "--dh-background" in response.text


def test_global_shell_uses_lucide_icons(app_env):
    client, *_ = app_env
    response = client.get("/status")
    assert response.status_code == 200
    assert 'data-lucide="gauge"' in response.text
    assert 'data-lucide="users"' in response.text
    assert 'data-lucide="download"' in response.text
    assert 'data-lucide="database"' in response.text
    assert 'data-lucide="settings"' in response.text
    assert 'class="ph ph-gauge"' not in response.text


def test_spa_router_refreshes_lucide_icons():
    source = Path("app/templates/base.html").read_text(encoding="utf-8")
    assert "function refreshIcons()" in source
    load_page_body = source.split("async function loadPage", 1)[1].split("\n        }", 1)[0]
    refresh_call = "if (window._doukhubRefreshIcons) window._doukhubRefreshIcons();"
    assert refresh_call in load_page_body
    assert "DOMContentLoaded" not in load_page_body
    assert load_page_body.index("await rebindScripts(ps);") < load_page_body.index(refresh_call)


def test_mode_toggle_refreshes_lucide_icon_after_replacement():
    source = Path("app/templates/base.html").read_text(encoding="utf-8")
    function_body = source.split("function updateModeToggle", 1)[1].split("\n        }", 1)[0]
    refresh_call = "if (window._doukhubRefreshIcons) window._doukhubRefreshIcons();"
    assert "btn.innerHTML" in function_body
    assert refresh_call in function_body
    assert function_body.index("btn.innerHTML") < function_body.index(refresh_call)


def test_shell_css_sizes_lucide_svg_icons():
    source = Path("app/templates/base.html").read_text(encoding="utf-8")
    rules = {}
    for selectors, declarations in re.findall(r"([^{}]+)\{([^{}]*)\}", source):
        for selector in selectors.split(","):
            rules[selector.strip()] = declarations

    expected_sizes = {
        ".sidebar-brand > svg[data-lucide]": "28px",
        ".sb-collapsed .sidebar-brand > svg[data-lucide]": "24px",
        ".sidebar-collapse-btn svg[data-lucide]": "16px",
        ".sb-collapsed .sidebar-collapse-btn svg[data-lucide]": "14px",
        ".sidebar-nav a svg[data-lucide]": "18px",
        ".nav-group .nav-group-toggle svg[data-lucide]:first-child": "18px",
        ".nav-group .nav-arrow": "14px",
        ".nav-group .nav-submenu a svg[data-lucide]": "16px",
        ".dh-modal-header h3 svg[data-lucide]": "18px",
        ".dh-modal-close svg[data-lucide]": "16px",
        "#mode-toggle svg[data-lucide]": "16px",
        "#task-badge svg[data-lucide]": "16px",
    }
    for selector, size in expected_sizes.items():
        assert f"width: {size};" in rules[selector]
        assert f"height: {size};" in rules[selector]


def test_core_page_lucide_runtime_svgs_have_explicit_sizes():
    css = Path("app/static/css/style.css").read_text(encoding="utf-8")
    rules = _style_rules(css)
    expected_sizes = {
        ".btn svg[data-lucide]": "14px",
        ".card h3 svg[data-lucide]": "17px",
        ".workflow-title svg[data-lucide]": "17px",
        ".empty-state svg[data-lucide]": "36px",
        ".workflow-notice svg[data-lucide]": "17px",
        ".workflow-step .step-status svg[data-lucide]": "24px",
    }
    for selector, size in expected_sizes.items():
        assert f"width: {size};" in rules[selector]
        assert f"height: {size};" in rules[selector]


def test_status_page_lucide_runtime_svgs_have_explicit_sizes():
    source = Path("app/templates/status.html").read_text(encoding="utf-8")
    rules = _style_rules(source)
    rule = rules[".status-icon svg[data-lucide]"]

    assert "width: 18px;" in rule
    assert "height: 18px;" in rule


def test_legacy_geometry_aliases_use_doukhub_scale():
    css = Path("app/static/css/style.css").read_text(encoding="utf-8")
    variables = _declaration_map(css.split(":root {", 1)[1].split("}", 1)[0])
    expected_aliases = {
        "--radius-xs": "--dh-radius-sm",
        "--radius-sm": "--dh-radius-sm",
        "--radius": "--dh-radius",
        "--radius-lg": "--dh-radius-lg",
        "--radius-xl": "--dh-radius-lg",
        "--shadow-xs": "--dh-shadow-sm",
        "--shadow-sm": "--dh-shadow-sm",
        "--shadow-md": "--dh-shadow-sm",
        "--shadow-lg": "--dh-shadow-lg",
        "--shadow-xl": "--dh-shadow-lg",
    }
    for alias, canonical in expected_aliases.items():
        assert variables[alias] == f"var({canonical})"

    toast_rule = _rule_declarations(css, ".toast")
    status_rule = _rule_declarations(css, ".workflow-status")
    assert toast_rule["border-radius"] == "var(--radius-sm)"
    assert status_rule["border-radius"] == "var(--radius-sm)"
    assert toast_rule["box-shadow"] == "var(--shadow-lg)"


@pytest.mark.parametrize(
    "path,title",
    [
        ("/sync/overview", "账号状态"),
        ("/sync/import", "导入分享表"),
        ("/sync/resolve", "解析分享表"),
        ("/sync/account", "生成账号表"),
        ("/sync/refresh", "更新账号表"),
    ],
)
def test_sync_pages_use_page_head_and_lucide(app_env, path, title):
    client, *_ = app_env
    response = client.get(path)
    assert response.status_code == 200
    assert 'class="page-head"' in response.text
    assert title in response.text
    assert "data-lucide=" in response.text
    assert 'class="ph ph-' not in response.text


def test_sync_workflow_macros_use_lucide():
    source = Path("app/templates/sync/_workflow.html").read_text(encoding="utf-8")
    assert 'data-lucide="clock-3"' in source
    assert 'class="ph ph-' not in source


def test_sync_runtime_lucide_replacements_refresh_icons():
    refresh_call = "if (window._doukhubRefreshIcons) window._doukhubRefreshIcons();"
    for path in [
        "app/templates/sync/overview.html",
        "app/templates/sync/import.html",
        "app/templates/sync/resolve.html",
        "app/templates/sync/account.html",
        "app/templates/sync/refresh.html",
    ]:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
        replacements = [
            index
            for index, line in enumerate(lines)
            if "btn.innerHTML = '<i data-lucide=" in line
        ]
        assert replacements
        for index in replacements:
            assert refresh_call in lines[index + 1]


def test_collection_console_uses_page_head_and_lucide(app_env):
    client, *_ = app_env
    response = client.get("/collect")
    assert response.status_code == 200
    assert 'class="page-head"' in response.text
    assert "日常增量采集" in response.text
    assert "data-lucide=" in response.text
    assert 'class="ph ph-' not in response.text


def test_collection_page_preserves_workflow_contracts(app_env):
    client, *_ = app_env
    response = client.get("/collect")
    detail_response = client.get("/collect/detail")
    for element_id in (
        "collection-preview-status",
        "collection-status",
        "preview-total",
        "batch-progress-bar",
        "batch-detail-actions",
    ):
        assert f'id="{element_id}"' in response.text
    assert "previewCollectionScope" in response.text
    assert "selectBatchDetail" in response.text

    assert 'id="detail-submit"' in detail_response.text
    assert "invalidateResolvedSingleWorks" in detail_response.text


@pytest.mark.parametrize(
    "path,title",
    [("/status", "服务状态"), ("/settings", "设置")],
)
def test_system_pages_use_page_head_and_lucide(app_env, path, title):
    client, *_ = app_env
    response = client.get(path)
    assert response.status_code == 200
    assert 'class="page-head"' in response.text
    assert title in response.text
    assert "data-lucide=" in response.text
    assert 'class="ph ph-' not in response.text


def test_settings_page_preserves_system_controls_and_api_calls(app_env):
    client, *_ = app_env
    response = client.get("/settings")
    assert response.status_code == 200
    assert '<button type="button" onclick="restartSystem()"' in response.text
    assert '<button type="button" onclick="exitSystem()"' in response.text
    assert "await apiCall('/api/system/restart', 'POST');" in response.text
    assert "await apiCall('/api/system/exit', 'POST');" in response.text


def test_theme_material_is_no_longer_the_canonical_asset(app_env):
    client, *_ = app_env
    response = client.get("/collect")
    assert 'href="/static/css/style.css?v=5"' in response.text
    assert "theme-material.css" not in response.text
    assert "workflow-panel" in response.text


def test_mobile_buttons_allow_wrapping_instead_of_clipping():
    css = Path("app/static/css/style.css").read_text(encoding="utf-8")
    mobile_block = css.split("@media (max-width: 720px)", 1)[1]
    mobile_block = mobile_block.split("@media (max-width: 420px)", 1)[0]

    assert ".btn {" in mobile_block
    assert "height: auto;" in mobile_block
    assert "min-height: 40px;" in mobile_block
    assert "white-space: normal;" in mobile_block
