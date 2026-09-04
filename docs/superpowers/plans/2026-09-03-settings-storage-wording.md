# Settings Storage Wording Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Clarify the relationship between the fallback config file and the application data directory in Settings.

**Architecture:** The settings route supplies both storage paths. The settings template presents the fixed fallback JSON file separately from the active, migratable application data root. Existing migration APIs and data logic remain unchanged.

**Tech Stack:** FastAPI, Jinja2 templates, pytest.

## Global Constraints

- Do not change application data migration behavior.
- Do not move media files.
- Preserve existing settings-page layout patterns.

---

### Task 1: Clarify Storage Settings

**Files:**
- Modify: `app/main.py`
- Modify: `app/templates/settings.html`
- Test: `tests/test_data_migration_ui.py`

**Interfaces:**
- Consumes: `Config.app_data_dir: Path`
- Produces: Settings template context field `app_data_dir: str`

- [ ] **Step 1: Write the failing test**

```python
def test_settings_explains_storage_roles(app_env):
    client, *_ = app_env
    response = client.get("/settings")
    assert response.status_code == 200
    assert "兜底配置文件" in response.text
    assert "当前应用数据目录" in response.text
    assert "日常设置保存在应用数据目录的数据库中" in response.text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_data_migration_ui.py::test_settings_explains_storage_roles -q`

- [ ] **Step 3: Implement minimal UI and route changes**

Add `app_data_dir` to the `/settings` context. Rename the config card, add a current-directory row to the data card, and replace both explanatory captions.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_data_migration_ui.py -q`
