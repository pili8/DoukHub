from tests.test_api import app_env  # noqa: F401


def test_settings_has_migration_entry(app_env):
    client, *_ = app_env
    response = client.get("/settings")
    assert response.status_code == 200
    assert "应用数据目录" in response.text
    assert "startDataMigration()" in response.text


def test_settings_has_in_app_directory_picker(app_env):
    client, *_ = app_env
    response = client.get("/settings")

    assert response.status_code == 200
    assert 'id="directory-picker-overlay"' in response.text
    assert "/api/data-migration/directories" in response.text
    assert "loadDataRootDirectories('', 1)" in response.text
    assert "选择当前目录" in response.text
    assert "新建" in response.text
    assert "/api/data-migration/browse" not in response.text


def test_dedup_has_in_app_directory_picker(app_env):
    client, *_ = app_env
    response = client.get("/dedup")

    assert response.status_code == 200
    assert 'id="directory-picker-overlay"' in response.text
    assert "/api/data-migration/directories" in response.text
    assert "/api/dedup/browse" not in response.text


def test_settings_explains_storage_roles(app_env):
    client, *_ = app_env
    response = client.get("/settings")
    assert response.status_code == 200
    assert "兜底配置路径" in response.text
    assert "当前应用数据目录" in response.text
    assert "日常设置保存在应用数据目录的数据库中" in response.text
    assert '<span class="card-title">兜底配置文件</span>' not in response.text


def test_base_has_global_migration_overlay(app_env):
    client, *_ = app_env
    response = client.get("/status")
    assert response.status_code == 200
    assert 'id="migration-overlay"' in response.text
    assert "/api/data-migration/status" in response.text
