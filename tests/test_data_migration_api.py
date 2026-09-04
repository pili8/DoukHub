import pytest
from tests.test_api import app_env  # noqa: F401


@pytest.fixture
def migration_env(app_env, tmp_path, monkeypatch):
    import app.main as app_main
    from app.core.data_migration import DataMigration

    # 让 app_data_root() 解析到临时目录，避免测试读写真实 ~/.doukhub
    root = tmp_path / "app-root"
    root.mkdir()
    monkeypatch.setenv("DOUKHUB_DATA_ROOT", str(root))

    original = app_main.data_migration
    app_main.data_migration = DataMigration()
    yield *app_env, tmp_path
    app_main.data_migration = original


def test_validate_rejects_occupied_target(migration_env, tmp_path):
    client, *_ = migration_env
    target = tmp_path / "target"
    target.mkdir()
    (target / "doukhub.db").write_text("x", encoding="utf-8")
    response = client.post("/api/data-migration/validate", json={"target": str(target)})
    assert response.status_code == 200
    assert response.json()["valid"] is False


def test_start_blocks_when_active_task_exists(migration_env):
    import app.main as app_main

    client, *_ = migration_env
    task = app_main.get_task_manager().create("测试任务")
    try:
        response = client.post(
            "/api/data-migration/start",
            json={"target": str(app_main.app_data_root().parent / "migration-target")},
        )
        assert response.status_code == 409
        assert "后台任务" in response.json()["message"]
    finally:
        app_main.get_task_manager().update(task.task_id, status="done")


def test_directory_browser_lists_directories_and_parent(migration_env, tmp_path):
    client, *_ = migration_env
    root = tmp_path / "browser-root"
    root.mkdir()
    (root / "media").mkdir()
    (root / "backup").mkdir()
    (root / "doukhub.db").write_text("db", encoding="utf-8")

    response = client.get("/api/data-migration/directories", params={"path": root})

    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    assert payload["path"] == str(root)
    assert payload["parent"] == str(tmp_path)
    assert [item["name"] for item in payload["directories"]] == ["backup", "media"]


def test_directory_browser_computer_mode_lists_drives(migration_env):
    client, *_ = migration_env

    response = client.get("/api/data-migration/directories", params={"computer": 1})

    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    assert payload["path"] == ""
    assert payload["parent"] is None
    assert payload["directories"]
    assert all(item["path"].endswith(":\\") for item in payload["directories"])


def test_directory_browser_creates_folder(migration_env, tmp_path):
    client, *_ = migration_env
    root = tmp_path / "browser-root"
    root.mkdir()

    response = client.post(
        "/api/data-migration/directories/create",
        json={"parent": str(root), "name": "new-data"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    assert payload["path"] == str(root / "new-data")
    assert (root / "new-data").is_dir()


def test_directory_browser_rejects_invalid_new_folder(migration_env, tmp_path):
    client, *_ = migration_env
    root = tmp_path / "browser-root"
    root.mkdir()

    response = client.post(
        "/api/data-migration/directories/create",
        json={"parent": str(root), "name": "bad/name"},
    )

    assert response.status_code == 400
    assert response.json()["success"] is False


def test_migration_locks_other_requests(migration_env):
    import app.main as app_main

    client, *_ = migration_env
    app_main.data_migration._state.update(status="copying", locked=True)
    blocked = client.get("/api/stats")
    allowed = client.get("/api/data-migration/status")
    assert blocked.status_code == 503
    assert allowed.status_code == 200
