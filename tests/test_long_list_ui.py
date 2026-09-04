from tests.test_api import app_env  # noqa: F401


def test_base_includes_clip_list_tool(app_env):
    client, *_ = app_env
    response = client.get("/status")
    assert response.status_code == 200
    assert "function clipList" in response.text
    assert "list-clip-bar" in response.text


def test_collect_wires_account_table_clip(app_env):
    client, *_ = app_env
    response = client.get("/collect")
    assert response.status_code == 200
    assert "applyAccountTableClip" in response.text



def test_collect_has_works_list_clip_helper(app_env):
    client, *_ = app_env
    response = client.get("/collect")
    assert response.status_code == 200
    assert "function clipAllWorksLists" in response.text
    assert "clipAllWorksLists(row)" in response.text



def test_import_preview_wires_clip(app_env):
    client, *_ = app_env
    response = client.get("/sync/import")
    assert response.status_code == 200
    assert "clipList(document.getElementById('preview-body')" in response.text
