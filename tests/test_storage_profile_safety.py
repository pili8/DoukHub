import pytest
from fastapi.testclient import TestClient

from app.core.config import Config


@pytest.fixture
def storage_env(tmp_path, monkeypatch):
    root = tmp_path / "app-root"
    root.mkdir()
    monkeypatch.setenv("DOUKHUB_DATA_ROOT", str(root))

    test_config = Config(tmp_path / "config.json")
    import app.main as app_main

    original = app_main.config
    app_main.config = test_config
    client = TestClient(app_main.app)
    yield client, test_config
    app_main.config = original


def _with_profile(config):
    config._data["storage_profiles"] = {
        "single": {
            "default_name_format": "{create_time} {author} {title}",
            "profiles": [{
                "id": "sp_test",
                "name": "主方案",
                "path": "D:/Media",
                "name_format": "",
                "role": "primary",
                "enabled": True,
            }],
        },
        "batch": {
            "default_name_format": "create_time type nickname desc",
            "profiles": [],
        },
    }


def test_empty_storage_save_requires_confirmation(storage_env):
    client, config = storage_env
    _with_profile(config)
    payload = {
        "single": {"default_name_format": "{create_time} {author} {title}", "profiles": []},
        "batch": {"default_name_format": "create_time type nickname desc", "profiles": []},
    }

    blocked = client.put("/api/collection/storage", json=payload)
    assert blocked.status_code == 409
    assert blocked.json()["success"] is False
    assert blocked.json()["empty_scopes"] == ["single"]
    assert "单作品" in blocked.json()["message"]
    assert config.storage_profiles["single"]["profiles"]

    confirmed = client.put(
        "/api/collection/storage",
        json={**payload, "allow_empty_scopes": ["single"]},
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["success"] is True
    assert config.storage_profiles["single"]["profiles"] == []
