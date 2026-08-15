from pathlib import Path

import re

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
    assert 'data-lucide="refresh-cw"' in response.text
    assert 'data-lucide="download"' in response.text
    assert 'data-lucide="database"' in response.text
    assert 'data-lucide="settings"' in response.text
    assert 'class="ph ph-gauge"' not in response.text


def test_spa_router_refreshes_lucide_icons():
    source = Path("app/templates/base.html").read_text(encoding="utf-8")
    assert "function refreshIcons()" in source
    assert "refreshIcons()" in source.split("async function loadPage", 1)[1]
