from pathlib import Path

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
