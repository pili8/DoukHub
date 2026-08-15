from pathlib import Path

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
