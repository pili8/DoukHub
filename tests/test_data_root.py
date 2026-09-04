import json
from pathlib import Path

import pytest

from app.core.data_root import (
    DataRootError,
    app_data_root,
    validate_target,
    write_bootstrap,
)


def test_env_override_is_used(tmp_path, monkeypatch):
    root = tmp_path / "app-data"
    root.mkdir()
    monkeypatch.setenv("DOUKHUB_DATA_ROOT", str(root))
    assert app_data_root() == root.resolve()


def test_missing_bootstrap_creates_default_once(tmp_path, monkeypatch):
    bootstrap = tmp_path / "data_root.json"
    default_root = tmp_path / ".doukhub-test"
    monkeypatch.setattr("app.core.data_root.BOOTSTRAP_PATH", bootstrap)
    monkeypatch.setattr("app.core.data_root.DEFAULT_DATA_ROOT", default_root)
    root = app_data_root()
    assert root.name == ".doukhub-test"
    assert json.loads(bootstrap.read_text(encoding="utf-8"))["data_dir"] == str(root)

    moved = tmp_path / "moved"
    moved.mkdir()
    bootstrap.write_text(
        json.dumps({"version": 1, "data_dir": str(moved)}), encoding="utf-8"
    )
    assert app_data_root() == moved


def test_unavailable_bootstrap_target_does_not_fall_back(tmp_path, monkeypatch):
    bootstrap = tmp_path / "data_root.json"
    missing = tmp_path / "missing-root"
    bootstrap.write_text(
        json.dumps({"version": 1, "data_dir": str(missing)}), encoding="utf-8"
    )
    monkeypatch.setattr("app.core.data_root.BOOTSTRAP_PATH", bootstrap)
    with pytest.raises(DataRootError):
        app_data_root()


def test_validate_target_rejects_conflicting_data(tmp_path, monkeypatch):
    current = tmp_path / "current"
    current.mkdir()
    monkeypatch.setenv("DOUKHUB_DATA_ROOT", str(current))
    target = tmp_path / "target"
    target.mkdir()
    (target / "doukhub.db").write_text("occupied", encoding="utf-8")
    result = validate_target(str(target))
    assert result["valid"] is False
    assert "doukhub.db" in result["message"]
