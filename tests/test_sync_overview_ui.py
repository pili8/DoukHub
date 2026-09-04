from pathlib import Path


def test_overview_flow_copy_matches_actual_steps():
    source = Path("app/templates/sync/overview.html").read_text(encoding="utf-8")

    assert "继续第 2-3 步" in source
    assert "执行第 2-3 步" in source
    assert "第 4 步需单独执行" in source
    assert "第 2-3 步完成" in source
    assert "账号处理完成" not in source
    assert "var status = document.getElementById('sync-console-status');" in source
