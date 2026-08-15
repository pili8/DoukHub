# DoukHub EntHub-Style Design Language Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give DoukHub an EntHub-like author identity while preserving DoukHub's left navigation, blue accent, business logic, and sidebar active indicator.

**Architecture:** Make `app/static/css/style.css` the single canonical stylesheet, introduce DoukHub design tokens and compact component rules, then migrate the global shell and core workflow pages to those tokens. Keep the existing FastAPI, polling, workflow, and collection behavior unchanged. Add only database connection and schema-version foundations, without moving business tables.

**Tech Stack:** Jinja2, CSS custom properties, local Lucide icons, FastAPI TestClient, SQLite PRAGMAs, pytest.

## Global Constraints

- Preserve DoukHub's left navigation and blue accent.
- Preserve the sidebar active blue block and left white 3px vertical indicator.
- Do not copy EntHub's top navigation or terracotta accent.
- Do not change collection APIs, TTD batch logic, Feishu sync logic, or table data APIs.
- Use `app/static/css/style.css` as the canonical stylesheet.
- Do not load Lucide from a CDN.
- No browser `alert`, `confirm`, or `prompt`.
- Core page text is Chinese; code identifiers remain English.
- Desktop-first density, with usable 390px mobile layout.
- Browser console must have no JavaScript errors.
- Existing tests must pass.

---

### Task 1: Canonical Stylesheet And DoukHub Design Tokens

**Files:**

- Modify: `app/templates/base.html`
- Modify: `app/static/css/style.css`
- Create: `app/static/js/lucide.min.js`
- Test: `tests/test_design_language.py`

**Interfaces:**

- Produces CSS custom properties:
  - `--dh-background`
  - `--dh-surface`
  - `--dh-surface-muted`
  - `--dh-text`
  - `--dh-text-secondary`
  - `--dh-text-muted`
  - `--dh-border`
  - `--dh-border-strong`
  - `--dh-accent`
  - `--dh-accent-hover`
  - `--dh-accent-soft`
  - `--dh-danger`
  - `--dh-warning`
  - `--dh-success`
  - `--dh-radius-sm`
  - `--dh-radius`
  - `--dh-radius-lg`
- Produces component classes:
  - `.page-head`
  - `.page-title`
  - `.page-sub`
  - `.page-actions`
  - `.icon-button`

- [ ] **Step 1: Write failing design-system tests**

Create `tests/test_design_language.py`:

```python
from pathlib import Path


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
```

The file needs the existing API fixture import:

```python
from tests.test_api import app_env
```

- [ ] **Step 2: Run tests and verify RED**

```powershell
D:\AI\DoukHub\venv\Scripts\python.exe -m pytest tests\test_design_language.py -v
```

Expected: all tests fail. The base still links `theme-material.css`, Lucide is not local, and `--dh-*` tokens do not exist.

- [ ] **Step 3: Copy local Lucide runtime**

Copy the already vendored EntHub runtime:

```powershell
Copy-Item D:\AI\EntHub\static\lucide.min.js D:\AI\DoukHub\app\static\js\lucide.min.js
```

Do not download anything.

- [ ] **Step 4: Point base at canonical assets**

In `app/templates/base.html`, replace only the stylesheet link:

```html
<link id="theme-css" rel="stylesheet" href="/static/css/theme-material.css?v=4">
```

with:

```html
<link id="theme-css" rel="stylesheet" href="/static/css/style.css?v=5">
```

Add the local Lucide runtime before the existing Phosphor script:

```html
<script src="/static/js/lucide.min.js"></script>
<script src="https://unpkg.com/@phosphor-icons/web@2.1.1"></script>
```

The Phosphor script remains during this phase only because non-core templates still use Phosphor classes. Tasks 2-5 migrate the global shell and core pages to Lucide. Do not load Lucide from a CDN.

Immediately before the closing `</body>`, add:

```html
<script>
    function refreshIcons() {
        if (window.lucide?.createIcons) window.lucide.createIcons();
    }
    document.addEventListener('DOMContentLoaded', refreshIcons);
    window._doukhubRefreshIcons = refreshIcons;
</script>
```

- [ ] **Step 5: Add tokens and compact component system**

At the top of `app/static/css/style.css`, replace the stylesheet header with:

```css
/* ==========================================================================
   DoukHub - Compact Local Tool Design Language
   Identity: EntHub-like warm paper surfaces + DoukHub blue accent
   Layout: Left navigation (DoukHub), not EntHub's top navigation
   Icons: Local Lucide for core shell/pages
   ========================================================================== */
```

Inside `:root`, before all existing Material variables, add:

```css
  --dh-background: #FBFAF7;
  --dh-surface: #FFFFFF;
  --dh-surface-muted: #F5F2EC;
  --dh-text: #1F1B17;
  --dh-text-secondary: #5C564E;
  --dh-text-muted: #A39E96;
  --dh-border: #ECE7DF;
  --dh-border-strong: #D9D3C8;
  --dh-accent: #0061A4;
  --dh-accent-hover: #004F8A;
  --dh-accent-soft: #E7F0F8;
  --dh-danger: #C2410C;
  --dh-danger-soft: #FEF1EB;
  --dh-warning: #B45309;
  --dh-warning-soft: #FEF3C7;
  --dh-success: #16A34A;
  --dh-success-soft: #ECFDF3;
  --dh-radius-sm: 6px;
  --dh-radius: 8px;
  --dh-radius-lg: 12px;
  --dh-shadow-sm: 0 1px 2px rgba(31, 27, 23, 0.04);
  --dh-shadow-lg: 0 8px 24px rgba(31, 27, 23, 0.12);
  --dh-font: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC",
             "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
  --dh-mono: "SF Mono", "Fira Code", Menlo, Consolas, monospace;
```

Remap the existing compatibility tokens to the new system:

```css
  --md-primary: var(--dh-accent);
  --md-on-primary: #FFFFFF;
  --md-primary-container: var(--dh-accent-soft);
  --md-on-primary-container: #003A5F;

  --md-error: var(--dh-danger);
  --md-error-container: var(--dh-danger-soft);
  --md-success: var(--dh-success);
  --md-success-container: var(--dh-success-soft);
  --md-warning: var(--dh-warning);
  --md-warning-container: var(--dh-warning-soft);

  --md-background: var(--dh-background);
  --md-on-background: var(--dh-text);
  --md-surface: var(--dh-surface);
  --md-on-surface: var(--dh-text);
  --md-on-surface-variant: var(--dh-text-secondary);
  --md-surface-container-lowest: var(--dh-surface);
  --md-surface-container-low: var(--dh-surface);
  --md-surface-container: var(--dh-surface-muted);
  --md-surface-container-high: #EFEAE1;
  --md-surface-container-highest: #EAE4D9;
  --md-outline: var(--dh-text-muted);
  --md-outline-variant: var(--dh-border);

  --bg-base: var(--dh-background);
  --bg-surface: var(--dh-surface);
  --bg-sidebar: var(--dh-surface);
  --bg-sidebar-hover: var(--dh-surface-muted);
  --bg-hover: var(--dh-surface-muted);
  --bg-muted: var(--dh-surface-muted);
  --bg-input: var(--dh-surface);
  --text-primary: var(--dh-text);
  --text-secondary: var(--dh-text-secondary);
  --text-muted: var(--dh-text-muted);
  --border-default: var(--dh-border);
  --border-strong: var(--dh-border-strong);
  --accent: var(--dh-accent);
  --accent-hover: var(--dh-accent-hover);
  --accent-light: var(--dh-accent-soft);
```

Update the shared component rules to this compact scale:

```css
body {
  font-family: var(--dh-font);
  font-size: 13px;
  color: var(--dh-text);
  background: var(--dh-background);
  line-height: 1.5;
}

.main {
  padding: 24px 28px;
}

.page-head {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 20px;
}

.page-title {
  margin: 0;
  font-size: 20px;
  font-weight: 600;
  line-height: 1.3;
  color: var(--dh-text);
}

.page-sub {
  margin-top: 3px;
  font-size: 12px;
  color: var(--dh-text-secondary);
}

.page-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.card,
.workflow-panel {
  background: var(--dh-surface);
  border: 1px solid var(--dh-border);
  border-radius: var(--dh-radius);
  box-shadow: none;
  padding: 18px;
  margin-bottom: 14px;
}

.card:hover,
.workflow-panel:hover {
  box-shadow: none;
}

.card h3,
.workflow-title {
  font-size: 14px;
  font-weight: 600;
  color: var(--dh-text);
}

.card h3 i,
.workflow-title i {
  font-size: 17px;
  color: var(--dh-accent);
}

.btn {
  height: 34px;
  padding: 0 13px;
  border-radius: var(--dh-radius-sm);
  font-size: 13px;
  font-weight: 500;
  box-shadow: none;
}

.btn-sm {
  height: 28px;
  padding: 0 10px;
  font-size: 12px;
}

.btn-secondary {
  background: var(--dh-surface);
  color: var(--dh-text-secondary);
  border: 1px solid var(--dh-border-strong);
}

.form-group {
  margin-bottom: 14px;
}

.form-group label {
  margin-bottom: 5px;
  font-size: 12px;
  font-weight: 500;
  color: var(--dh-text-secondary);
}

.form-group input,
.form-group select,
.form-group textarea {
  min-height: 32px;
  padding: 6px 9px;
  border: 1px solid var(--dh-border);
  border-radius: var(--dh-radius-sm);
  background: var(--dh-surface);
  color: var(--dh-text);
  font-size: 12px;
}

.form-group input:focus,
.form-group select:focus,
.form-group textarea:focus {
  border-color: var(--dh-accent);
  box-shadow: 0 0 0 2px var(--dh-accent-soft);
  padding: 6px 9px;
}

.icon-button {
  width: 28px;
  height: 28px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: 1px solid var(--dh-border);
  border-radius: var(--dh-radius-sm);
  background: var(--dh-surface);
  color: var(--dh-text-secondary);
  cursor: pointer;
}

.icon-button:hover {
  border-color: var(--dh-accent);
  color: var(--dh-accent);
}

th, td {
  padding: 10px 12px;
  font-size: 12px;
}

th {
  background: var(--dh-surface-muted);
  color: var(--dh-text-secondary);
  font-weight: 600;
}

.workflow-step,
.workflow-metric,
.workflow-tabs {
  border-color: var(--dh-border);
  background: var(--dh-surface);
}

.workflow-tab.active {
  background: var(--dh-accent);
  color: white;
}
```

Keep every existing workflow selector. Do not delete responsive or ARIA-related rules.

- [ ] **Step 6: Run design tests**

```powershell
D:\AI\DoukHub\venv\Scripts\python.exe -m pytest tests\test_design_language.py tests\test_workflow_ui.py -v
```

Expected: design tests pass and workflow tests remain green.

- [ ] **Step 7: Commit**

```powershell
git add app\templates\base.html app\static\css\style.css app\static\js\lucide.min.js tests\test_design_language.py
git commit -m "feat: establish DoukHub design language"
```

---

### Task 2: Global Shell Icon Migration

**Files:**

- Modify: `app/templates/base.html`
- Test: `tests/test_design_language.py`

**Interfaces:**

- Consumes local `lucide.min.js`.
- Produces Lucide-based global navigation and shared dialogs.

- [ ] **Step 1: Add failing global icon tests**

Append:

```python
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
```

- [ ] **Step 2: Verify RED**

```powershell
D:\AI\DoukHub\venv\Scripts\python.exe -m pytest tests\test_design_language.py -k lucide -v
```

Expected: both tests fail because the shell still uses Phosphor classes.

- [ ] **Step 3: Replace shell icons**

Use this exact mapping in `base.html`:

| Current | Replacement |
|---|---|
| `ph-bold ph-rocket-launch` | `rocket` |
| `ph ph-sidebar-simple` | `panel-left-close` |
| `ph ph-gauge` | `gauge` |
| `ph ph-arrows-clockwise` | `refresh-cw` |
| `ph ph-caret-right` | `chevron-right` |
| `ph ph-house` | `house` |
| `ph ph-download` | `download` |
| `ph ph-keyhole` | `key-round` |
| `ph ph-users` | `users` |
| `ph ph-arrow-clockwise` | `rotate-cw` |
| `ph ph-download-simple` | `download` |
| `ph ph-database` | `database` |
| `ph ph-squares-four` | `layout-grid` |
| `ph ph-cloud-arrow-up` | `cloud-upload` |
| `ph ph-table` | `table-2` |
| `ph ph-gear` | `settings` |
| `ph ph-moon` | `moon` |
| `ph ph-sun` | `sun` |
| `ph ph-list-checks` | `list-checks` |
| `ph ph-question` | `help-circle` |
| `ph ph-x` | `x` |

Pattern:

```html
<i data-lucide="gauge"></i>
```

Do not change nav hrefs, Jinja conditionals, collapse behavior, task badge, or sidebar indicator CSS.

In the SPA `loadPage()` function, after replacing main/page scripts and before returning, call:

```javascript
if (window._doukhubRefreshIcons) window._doukhubRefreshIcons();
```

- [ ] **Step 4: Run focused tests**

```powershell
D:\AI\DoukHub\venv\Scripts\python.exe -m pytest tests\test_design_language.py tests\test_api.py -v
```

Expected: all pass.

- [ ] **Step 5: Commit**

```powershell
git add app\templates\base.html tests\test_design_language.py
git commit -m "feat: use Lucide icons in global shell"
```

---

### Task 3: Sync Wizard Page Refactor

**Files:**

- Modify: `app/templates/sync/_workflow.html`
- Modify: `app/templates/sync/overview.html`
- Modify: `app/templates/sync/import.html`
- Modify: `app/templates/sync/resolve.html`
- Modify: `app/templates/sync/account.html`
- Modify: `app/templates/sync/refresh.html`
- Test: `tests/test_design_language.py`

**Interfaces:**

- Consumes `.page-head`, `.page-title`, `.page-sub`, `.page-actions`, and `workflow-*`.
- Preserves script element IDs from Task 1.

- [ ] **Step 1: Add failing sync-page tests**

Append:

```python
@pytest.mark.parametrize(
    "path,title",
    [
        ("/sync/overview", "同步概览"),
        ("/sync/import", "导入采集表"),
        ("/sync/resolve", "解析采集表"),
        ("/sync/account", "同步账号表"),
        ("/sync/refresh", "更新账号表"),
    ],
)
def test_sync_pages_use_page_head_and_lucide(app_env, path, title):
    client, *_ = app_env
    response = client.get(path)
    assert response.status_code == 200
    assert 'class="page-head"' in response.text
    assert title in response.text
    assert "data-lucide=" in response.text
    assert 'class="ph ph-' not in response.text


def test_sync_workflow_macros_use_lucide():
    source = Path("app/templates/sync/_workflow.html").read_text(encoding="utf-8")
    assert 'data-lucide="clock-3"' in source
    assert 'class="ph ph-' not in source
```

The test file needs `import pytest`.

- [ ] **Step 2: Verify RED**

```powershell
D:\AI\DoukHub\venv\Scripts\python.exe -m pytest tests\test_design_language.py -k sync -v
```

Expected: all new tests fail.

- [ ] **Step 3: Replace page headers**

For each sync template, replace the current `<div class="page-header"><h2>...</h2></div>` with:

```django
<div class="page-head">
    <div>
        <h1 class="page-title">PAGE_TITLE</h1>
        <div class="page-sub">PAGE_SUBTITLE</div>
    </div>
    <div class="page-actions">PAGE_ACTION</div>
</div>
```

Use these exact substitutions:

| Template | `PAGE_TITLE` | `PAGE_SUBTITLE` | `PAGE_ACTION` |
|---|---|---|---|
| `overview.html` | 同步概览 | 数据准备流程 · {{ accounts|length }} 个账号 | Existing `sync-all-btn` and `stop-sync-btn` buttons |
| `import.html` | 导入采集表 | 粘贴或拖入分享数据，预览后写入本地库 | 解析预览 button |
| `resolve.html` | 解析采集表 | 解析短链接并获取 sec_user_id | Existing `exec-btn` and `cancel-btn` |
| `account.html` | 同步账号表 | 将采集记录整理为账号档案 | Existing `exec-btn` and `cancel-btn` |
| `refresh.html` | 更新账号表 | 补齐未获取详情的账号资料 | Existing `exec-btn` and `cancel-btn` |

Move duplicated execution buttons out of panel headers when they become page actions. Preserve IDs and onclick handlers exactly.

- [ ] **Step 4: Replace sync icons**

Use this mapping:

| Current | Lucide |
|---|---|
| `ph-lightning` | `zap` |
| `ph-download` | `download` |
| `ph-keyhole` | `key-round` |
| `ph-users` | `users` |
| `ph-arrow-clockwise` | `rotate-cw` |
| `ph-clock-counter-clockwise` | `clock-3` |
| `ph-chart-bar` | `bar-chart-3` |
| `ph-magnifying-glass` | `search` |
| `ph-play` | `play` |
| `ph-stop` | `square` |
| `ph-info` | `info` |
| `ph-check-circle` | `check-circle-2` |
| `ph-x-circle` | `x-circle` |
| `ph-caret-down` | `chevron-down` |
| `ph-upload` | `upload` |

In `_workflow.html`, history icons become:

```html
<i data-lucide="clock-3"></i>
```

- [ ] **Step 5: Run focused tests**

```powershell
D:\AI\DoukHub\venv\Scripts\python.exe -m pytest tests\test_design_language.py tests\test_workflow_ui.py tests\test_api.py -v
```

Expected: all pass.

- [ ] **Step 6: Commit**

```powershell
git add app\templates\sync tests\test_design_language.py
git commit -m "feat: refine sync wizard interface"
```

---

### Task 4: Collection Console Refactor

**Files:**

- Modify: `app/templates/collect.html`
- Test: `tests/test_design_language.py`

**Interfaces:**

- Preserves all existing collection APIs and JavaScript functions.
- Preserves `collection-preview-status` and `collection-status` separation.
- Preserves batch progress, cancel, retry, and single-work safety behavior.

- [ ] **Step 1: Add failing collection-page tests**

Append:

```python
def test_collection_console_uses_page_head_and_lucide(app_env):
    client, *_ = app_env
    response = client.get("/collect")
    assert response.status_code == 200
    assert 'class="page-head"' in response.text
    assert "日常增量采集" in response.text
    assert "data-lucide=" in response.text
    assert 'class="ph ph-' not in response.text


def test_collection_page_preserves_workflow_contracts(app_env):
    client, *_ = app_env
    response = client.get("/collect")
    for element_id in (
        "collection-preview-status",
        "collection-status",
        "preview-total",
        "batch-progress-bar",
        "batch-detail-actions",
        "detail-submit",
    ):
        assert f'id="{element_id}"' in response.text
    assert "previewCollectionScope" in response.text
    assert "selectBatchDetail" in response.text
    assert "invalidateResolvedSingleWorks" in response.text
```

- [ ] **Step 2: Verify RED**

```powershell
D:\AI\DoukHub\venv\Scripts\python.exe -m pytest tests\test_design_language.py -k collection -v
```

Expected: page-head/Lucide test fails; contract test should pass and serve as a regression guard.

- [ ] **Step 3: Refactor page header**

Replace the current page header with:

```django
<div class="page-head">
    <div>
        <h1 class="page-title">采集</h1>
        <div class="page-sub">日常增量运行台 · 上次采集 <span id="collection-last-run">-</span></div>
    </div>
    <div class="page-actions">
        <span class="workflow-status pending" id="collection-status">等待查看</span>
    </div>
</div>
```

Remove the duplicate `collection-status` element from the status panel. Keep `collection-preview-status` there.

- [ ] **Step 4: Replace collection icons**

Use this mapping:

| Current | Lucide |
|---|---|
| `ph-download` | `download` |
| `ph-download-simple` | `download` |
| `ph-link` | `link` |
| `ph-pulse` | `activity` |
| `ph-list-checks` | `list-checks` |
| `ph-clock-counter-clockwise` | `clock-3` |
| `ph-calendar-clock` | `calendar-clock` |
| `ph-plus` | `plus` |
| `ph-trash` | `trash-2` |
| `ph-calendar-x` | `calendar-x` |
| `ph-plus-circle` | `circle-plus` |
| `ph-magnifying-glass` | `search` |
| `ph-folder-open` | `folder-open` |
| `ph-folder` | `folder` |

- [ ] **Step 5: Run focused tests**

```powershell
D:\AI\DoukHub\venv\Scripts\python.exe -m pytest tests\test_design_language.py tests\test_workflow_ui.py tests\test_collection_api.py -v
```

Expected: all pass.

- [ ] **Step 6: Commit**

```powershell
git add app\templates\collect.html tests\test_design_language.py
git commit -m "feat: refine collection console interface"
```

---

### Task 5: Status And Settings Page Refactor

**Files:**

- Modify: `app/templates/status.html`
- Modify: `app/templates/settings.html`
- Test: `tests/test_design_language.py`

**Interfaces:**

- Consumes `.page-head` and compact card/form/table styles.
- Does not change settings APIs or submitted field names.

- [ ] **Step 1: Add failing tests**

Append:

```python
@pytest.mark.parametrize(
    "path,title",
    [("/status", "服务状态"), ("/settings", "设置")],
)
def test_system_pages_use_page_head_and_lucide(app_env, path, title):
    client, *_ = app_env
    response = client.get(path)
    assert response.status_code == 200
    assert 'class="page-head"' in response.text
    assert title in response.text
    assert "data-lucide=" in response.text
    assert 'class="ph ph-' not in response.text
```

- [ ] **Step 2: Verify RED**

```powershell
D:\AI\DoukHub\venv\Scripts\python.exe -m pytest tests\test_design_language.py -k system_pages -v
```

Expected: both fail.

- [ ] **Step 3: Refactor headers**

Use:

```django
<div class="page-head">
    <div>
        <h1 class="page-title">PAGE_TITLE</h1>
        <div class="page-sub">PAGE_SUBTITLE</div>
    </div>
    <div class="page-actions">PAGE_ACTION</div>
</div>
```

Substitutions:

| Template | Title | Subtitle | Action |
|---|---|---|---|
| `status.html` | 服务状态 | TTD、XHS 与后台任务健康状态 | Keep the current refresh/open buttons and their handlers |
| `settings.html` | 设置 | 服务、存储与同步偏好 | Keep the current save button and its handler |

Keep all form names, IDs, and submission handlers unchanged.

- [ ] **Step 4: Replace icons**

Use the shared Lucide mapping from Tasks 2-4. For settings-specific icons:

| Current | Lucide |
|---|---|
| `ph-gear` | `settings` |
| `ph-folder` | `folder` |
| `ph-plugs` | `plug-zap` |
| `ph-floppy-disk` | `save` |
| `ph-arrow-counter-clockwise` | `undo-2` |

- [ ] **Step 5: Run tests**

```powershell
D:\AI\DoukHub\venv\Scripts\python.exe -m pytest tests\test_design_language.py tests\test_api.py -v
```

Expected: all pass.

- [ ] **Step 6: Commit**

```powershell
git add app\templates\status.html app\templates\settings.html tests\test_design_language.py
git commit -m "feat: refine system pages"
```

---

### Task 6: SQLite Connection And Versioned Migration Foundation

**Files:**

- Modify: `app/core/database.py`
- Test: `tests/test_database_foundation.py`

**Interfaces:**

- Produces `Database.SCHEMA_VERSION = 1`.
- Produces `Database._connect() -> sqlite3.Connection` with:
  - `PRAGMA journal_mode=WAL`
  - `PRAGMA foreign_keys=ON`
  - `PRAGMA busy_timeout=5000`
- Produces `Database._migrate_schema_version(conn) -> None`.

- [ ] **Step 1: Write failing database tests**

Create `tests/test_database_foundation.py`:

```python
import pathlib
import sqlite3
import tempfile

import pytest

from app.core.database import Database


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "foundation.db"


def test_connection_enables_sqlite_foundations(db_path):
    database = Database(db_path=db_path)
    with database._connect() as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000


def test_schema_version_is_persisted_once(db_path):
    database = Database(db_path=db_path)
    with database._connect() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1

    Database(db_path=db_path)
    with database._connect() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1


def test_future_version_is_not_downgraded(db_path, monkeypatch):
    database = Database(db_path=db_path)
    with database._connect() as conn:
        conn.execute("PRAGMA user_version = 2")
        conn.commit()

    monkeypatch.setattr(Database, "SCHEMA_VERSION", 1)
    Database(db_path=db_path)
    with database._connect() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
```

- [ ] **Step 2: Verify RED**

```powershell
D:\AI\DoukHub\venv\Scripts\python.exe -m pytest tests\test_database_foundation.py -v
```

Expected: PRAGMA and schema-version tests fail.

- [ ] **Step 3: Implement foundation**

Add inside `Database`, before `__init__`:

```python
    SCHEMA_VERSION = 1
```

At the end of `_init_database()`, after index creation and before leaving the `with` block:

```python
            self._migrate_schema_version(conn)
```

Replace `_connect()` with:

```python
    def _connect(self) -> sqlite3.Connection:
        """Create one SQLite connection with the project's standard pragmas."""
        conn = sqlite3.connect(str(self.db_path), timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn
```

Add:

```python
    def _migrate_schema_version(self, conn: sqlite3.Connection) -> None:
        """Persist schema version without ever downgrading a newer database."""
        current = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if current < self.SCHEMA_VERSION:
            conn.execute(f"PRAGMA user_version = {self.SCHEMA_VERSION}")
```

- [ ] **Step 4: Run database tests and existing database suites**

```powershell
D:\AI\DoukHub\venv\Scripts\python.exe -m pytest tests\test_database_foundation.py tests\test_database_generic.py tests\test_collection_batches.py -v
```

Expected: all pass.

- [ ] **Step 5: Commit**

```powershell
git add app\core\database.py tests\test_database_foundation.py
git commit -m "feat: harden SQLite foundation"
```

---

### Task 7: Visual Regression And Browser Verification

**Files:**

- Modify: `DEVELOPMENT.md`
- Test: `tests/test_design_language.py`

**Interfaces:**

- Consumes all prior tasks.

- [ ] **Step 1: Add final stylesheet contract**

Append:

```python
def test_theme_material_is_no_longer_the_canonical_asset(app_env):
    client, *_ = app_env
    response = client.get("/collect")
    assert 'href="/static/css/style.css?v=5"' in response.text
    assert "theme-material.css" not in response.text
    assert "workflow-panel" in response.text
```

- [ ] **Step 2: Run focused suites**

```powershell
D:\AI\DoukHub\venv\Scripts\python.exe -m pytest tests\test_design_language.py tests\test_workflow_ui.py tests\test_collection_api.py tests\test_database_foundation.py -v
```

Expected: all pass.

- [ ] **Step 3: Run full DoukHub suite**

```powershell
D:\AI\DoukHub\venv\Scripts\python.exe -m pytest tests -q
```

Expected: all tests pass, with only the known FastAPI TestClient deprecation warning.

- [ ] **Step 4: Browser verification**

Start or reuse `http://127.0.0.1:2999`. Verify:

1. Sidebar active item is blue and retains the left white vertical indicator.
2. `/sync/overview`, `/sync/import`, `/sync/resolve`, `/sync/account`, `/sync/refresh` use page heads, compact panels, and Lucide icons.
3. `/collect` defaults to daily incremental collection.
4. Preview updates without creating a batch.
5. Active and idle batch panels render progress and actions from existing tests.
6. `/status` and `/settings` use page heads and compact cards.
7. At 1440px, core pages have no page-level horizontal overflow.
8. At 390px, core pages have no overlap; tables scroll only in their containers.
9. Browser console has no JavaScript errors.
10. Take desktop and mobile screenshots of `/sync/overview`, `/collect`, `/status`, and `/settings`.

- [ ] **Step 5: Document the design language**

Append to `DEVELOPMENT.md`:

```markdown
## DoukHub 设计语言验证

DoukHub 使用暖白纸面、细边框卡片、紧凑控件和蓝色主色；布局保留左侧导航，不复制 EntHub 的顶部导航。验证时必须确认侧边栏选中项仍有左侧白色竖向指示条。核心页面使用本地 Lucide 图标，样式入口是 `app/static/css/style.css`，不要再把新样式写入 `theme-material.css`。
```

- [ ] **Step 6: Commit**

```powershell
git add DEVELOPMENT.md tests\test_design_language.py
git commit -m "docs: verify DoukHub design language"
```

---

## Plan Self-Review

- Spec coverage:
  - Warm surfaces, compact controls, blue accent, and compact radii: Task 1.
  - Local Lucide and SPA icon refresh: Tasks 1-5.
  - Sidebar indicator preservation: Task 1 test and global constraint.
  - Sync wizard refactor: Task 3.
  - Collection console refactor: Task 4.
  - System pages: Task 5.
  - SQLite WAL, foreign keys, timeout, and versioned migration: Task 6.
  - Browser, responsive, console, and documentation verification: Task 7.
  - No business/API changes: Tasks 2-5 are template-only; Task 6 changes connection/migration foundation only.
- Placeholder scan: no `TBD`, `TODO`, or unspecified implementation work.
- Type consistency:
  - `app_env` comes from `tests.test_api`.
  - All page contracts preserve existing IDs and function names.
  - `Database.SCHEMA_VERSION` and `_migrate_schema_version(conn)` are used consistently.
