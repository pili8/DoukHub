# Unified Workflow UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Unify synchronization and collection around a shared workflow visual language while preserving sync as a one-time data preparation wizard and collection as a daily incremental run console.

**Architecture:** Add shared Material Design 3 workflow classes to the global stylesheet and reuse them across sync and collection templates. Keep all existing APIs, routes, task managers, and state machines; add only a read-only collection preview endpoint. Rebuild page DOM around workflow panels while retaining element IDs and JavaScript contracts wherever possible.

**Tech Stack:** FastAPI, Jinja2, CSS custom properties, pytest, existing Phosphor icons.

## Global Constraints

- Sync remains a data preparation wizard with `导入 → 解析 → 同步账号 → 更新`.
- Collection is a daily incremental run console, not the fifth sync step.
- Preserve existing routes, APIs, task logic, and data structures.
- Shared workflow classes live in `app/static/css/style.css`.
- Templates must not duplicate shared workflow CSS in page-local `<style>` blocks.
- `POST /api/collection/batches/preview` is read-only and must not create a batch or mutate accounts.
- Collection defaults to `incremental` mode.
- Status must always include text as well as color.
- Desktop-first density; mobile must scroll horizontally without overlap.
- Browser console must have no JavaScript errors.

---

### Task 1: Shared Workflow Design System

**Files:**

- Modify: `app/static/css/style.css`
- Create: `tests/test_workflow_ui.py`

**Interfaces:**

- Produces shared CSS classes:
  - `.workflow-panel`
  - `.workflow-header`
  - `.workflow-title`
  - `.workflow-actions`
  - `.workflow-notice`
  - `.workflow-flow`
  - `.workflow-step`
  - `.workflow-metrics`
  - `.workflow-metric`
  - `.workflow-progress-meta`
  - `.workflow-log`
  - `.workflow-status`
  - `.workflow-history-item`
  - `.workflow-history-header`
  - `.workflow-history-detail`
  - `.workflow-tabs`
  - `.workflow-tab`
  - `.workflow-form-grid`

- [ ] **Step 1: Write failing stylesheet tests**

Create `tests/test_workflow_ui.py`:

```python
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
```

- [ ] **Step 2: Run the failing tests**

```powershell
D:\AI\DoukHub\venv\Scripts\python.exe -m pytest tests\test_workflow_ui.py -v
```

Expected: both tests fail because the classes do not exist.

- [ ] **Step 3: Add shared workflow CSS**

In `app/static/css/style.css`, add this section immediately before `/* ===== 17. Responsive ===== */`:

```css
/* ===== 16B. Shared Workflow Console ===== */
.workflow-panel {
  background: var(--md-surface-container-lowest);
  border: 1px solid var(--md-outline-variant);
  border-radius: var(--radius-lg);
  padding: 20px;
  margin-bottom: 16px;
}

.workflow-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 16px;
}

.workflow-title {
  min-width: 0;
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 16px;
  font-weight: 500;
  color: var(--md-on-surface);
}

.workflow-title i {
  font-size: 22px;
  color: var(--md-primary);
}

.workflow-title .workflow-count {
  font-size: 12px;
  font-weight: 400;
  color: var(--md-on-surface-variant);
}

.workflow-actions {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 8px;
  flex-wrap: wrap;
}

.workflow-notice {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 12px 14px;
  border-radius: var(--radius-sm);
  border-left: 4px solid var(--md-primary);
  background: var(--md-primary-container);
  color: var(--md-on-primary-container);
  font-size: 13px;
  line-height: 1.5;
  margin-bottom: 16px;
}

.workflow-notice i {
  font-size: 18px;
  flex-shrink: 0;
}

.workflow-flow {
  display: grid;
  grid-template-columns: repeat(4, minmax(190px, 1fr));
  gap: 10px;
  margin-bottom: 16px;
}

.workflow-step {
  min-width: 0;
  display: grid;
  grid-template-columns: 28px minmax(0, 1fr);
  gap: 10px;
  align-items: start;
  padding: 14px;
  border: 1px solid var(--md-outline-variant);
  border-radius: var(--radius);
  background: var(--md-surface-container-low);
  cursor: pointer;
  text-align: left;
  transition: border-color var(--transition-fast), background var(--transition-fast), box-shadow var(--transition-fast);
}

.workflow-step:hover {
  border-color: var(--md-primary);
  background: var(--md-surface-container);
  box-shadow: var(--shadow-xs);
}

.workflow-step .step-index {
  width: 28px;
  height: 28px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border-radius: var(--radius-full);
  background: var(--md-primary-container);
  color: var(--md-on-primary-container);
  font-size: 12px;
  font-weight: 600;
}

.workflow-step .step-name {
  margin: 0 0 3px;
  font-size: 13px;
  font-weight: 500;
  color: var(--md-on-surface);
}

.workflow-step .step-desc {
  margin: 0;
  font-size: 12px;
  color: var(--md-on-surface-variant);
  line-height: 1.45;
}

.workflow-metrics {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(118px, 1fr));
  gap: 10px;
}

.workflow-metric {
  min-width: 0;
  padding: 12px;
  border: 1px solid var(--md-outline-variant);
  border-left-width: 3px;
  border-radius: var(--radius-sm);
  background: var(--md-surface-container-low);
}

.workflow-metric .metric-value {
  display: block;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 17px;
  font-weight: 500;
  color: var(--md-on-surface);
  font-variant-numeric: tabular-nums;
}

.workflow-metric .metric-label {
  display: block;
  margin-top: 2px;
  font-size: 11px;
  color: var(--md-on-surface-variant);
  white-space: nowrap;
}

.workflow-progress-meta {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-top: 4px;
  font-size: 12px;
  color: var(--md-on-surface-variant);
}

.workflow-log {
  max-height: 320px;
  overflow-y: auto;
  padding: 12px 14px;
  border-radius: var(--radius-sm);
  background: #1a1c1e;
  color: #c3c7cf;
  border: 1px solid #2b2f33;
  font-family: var(--font-mono);
  font-size: 12px;
  line-height: 1.7;
  white-space: pre-wrap;
  word-break: break-word;
}

.workflow-log .log-ok { color: #6bdb8e; }
.workflow-log .log-err { color: #ffb4ab; }
.workflow-log .log-info { color: #9ecaff; }
.workflow-log .log-warn { color: #ffd8a8; }

.workflow-status {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  min-height: 22px;
  padding: 3px 10px;
  border-radius: var(--radius-sm);
  font-size: 12px;
  font-weight: 500;
  white-space: nowrap;
}

.workflow-status.pending,
.workflow-status.cancelled {
  background: var(--md-surface-container-high);
  color: var(--md-on-surface-variant);
}

.workflow-status.running,
.workflow-status.cancelling,
.workflow-status.enabled {
  background: var(--md-primary-container);
  color: var(--md-on-primary-container);
}

.workflow-status.success,
.workflow-status.completed,
.workflow-status.done {
  background: var(--md-success-container);
  color: var(--md-on-success-container);
}

.workflow-status.failed,
.workflow-status.disabled {
  background: var(--md-error-container);
  color: var(--md-on-error-container);
}

.workflow-status.skipped,
.workflow-status.warning {
  background: var(--md-warning-container);
  color: var(--md-on-warning-container);
}

.workflow-history-item {
  border: 1px solid var(--md-outline-variant);
  border-radius: var(--radius-sm);
  margin-bottom: 8px;
  overflow: hidden;
}

.workflow-history-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 11px 14px;
  background: var(--md-surface-container-low);
  cursor: pointer;
  font-size: 13px;
}

.workflow-history-header:hover {
  background: var(--md-surface-container);
}

.workflow-history-detail {
  max-height: 0;
  overflow: hidden;
  transition: max-height 300ms cubic-bezier(0.2, 0, 0, 1);
}

.workflow-history-detail.expanded {
  max-height: 480px;
}

.workflow-tabs {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 4px;
  border: 1px solid var(--md-outline-variant);
  border-radius: var(--radius-full);
  background: var(--md-surface-container-low);
}

.workflow-tab {
  min-height: 34px;
  padding: 0 16px;
  border: none;
  border-radius: var(--radius-full);
  background: transparent;
  color: var(--md-on-surface-variant);
  font: inherit;
  font-size: 13px;
  font-weight: 500;
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  gap: 6px;
}

.workflow-tab.active {
  background: var(--md-primary);
  color: var(--md-on-primary);
}

.workflow-form-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px 16px;
}
```

Extend the existing responsive block at the bottom of `style.css`:

```css
@media (max-width: 1024px) {
  .workflow-flow { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .workflow-form-grid { grid-template-columns: 1fr; }
  .workflow-header { align-items: flex-start; flex-direction: column; }
  .workflow-actions { justify-content: flex-start; }
}

@media (max-width: 720px) {
  .workflow-flow { grid-template-columns: 1fr; }
  .workflow-metrics { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .workflow-actions .btn { flex: 1 1 100%; }
  .workflow-tabs {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    border-radius: var(--radius);
  }
  .workflow-tab { justify-content: center; }
}

@media (prefers-reduced-motion: reduce) {
  .workflow-step,
  .workflow-history-detail,
  .workflow-tab,
  .progress-bar {
    transition: none;
    animation: none;
  }
}
```

- [ ] **Step 4: Run stylesheet tests**

```powershell
D:\AI\DoukHub\venv\Scripts\python.exe -m pytest tests\test_workflow_ui.py -v
```

Expected: 2 tests pass.

- [ ] **Step 5: Commit**

```powershell
git add app\static\css\style.css tests\test_workflow_ui.py
git commit -m "feat: add shared workflow design system"
```

---

### Task 2: Migrate Sync Templates To Workflow Components

**Files:**

- Create: `app/templates/sync/_workflow.html`
- Modify: `app/templates/sync/overview.html`
- Modify: `app/templates/sync/import.html`
- Modify: `app/templates/sync/resolve.html`
- Modify: `app/templates/sync/account.html`
- Modify: `app/templates/sync/refresh.html`
- Modify: `tests/test_workflow_ui.py`

**Interfaces:**

- Produces Jinja macros:
  - `workflow_notice(icon, text)`
  - `workflow_progress(bar_id, text_id, count_id)`
  - `workflow_metrics(metrics)`
  - `workflow_history(history)`
- Existing JavaScript element IDs remain:
  - `progress-bar`
  - `progress-text`
  - `progress-count`
  - `stat-total`
  - `stat-success`
  - `stat-skipped`
  - `stat-failed`
  - `exec-log`
  - `exec-progress`
  - `exec-btn`
  - `cancel-btn`

- [ ] **Step 1: Add failing template tests**

Append to `tests/test_workflow_ui.py`:

```python
import pytest


@pytest.mark.parametrize(
    "path",
    [
        "/sync/overview",
        "/sync/import",
        "/sync/resolve",
        "/sync/account",
        "/sync/refresh",
    ],
)
def test_sync_pages_use_shared_workflow_panels(app_env, path):
    client, *_ = app_env
    response = client.get(path)
    assert response.status_code == 200
    assert "workflow-panel" in response.text


def test_sync_overview_preserves_four_step_flow(app_env):
    client, *_ = app_env
    response = client.get("/sync/overview")
    assert "workflow-flow" in response.text
    assert "导入采集表" in response.text
    assert "解析采集表" in response.text
    assert "同步账号表" in response.text
    assert "更新账号表" in response.text


def test_sync_templates_do_not_redefine_shared_workflow_css():
    paths = [
        Path("app/templates/sync/overview.html"),
        Path("app/templates/sync/import.html"),
        Path("app/templates/sync/resolve.html"),
        Path("app/templates/sync/account.html"),
        Path("app/templates/sync/refresh.html"),
    ]
    for path in paths:
        source = path.read_text(encoding="utf-8")
        assert ".workflow-" not in source
        assert ".sync-log-box" not in source
        assert ".sync-stat-card" not in source
```

- [ ] **Step 2: Run failing template tests**

```powershell
D:\AI\DoukHub\venv\Scripts\python.exe -m pytest tests\test_workflow_ui.py -v
```

Expected: sync page tests fail because templates still use page-local cards and styles.

- [ ] **Step 3: Create reusable workflow macros**

Create `app/templates/sync/_workflow.html`:

```django
{% macro workflow_notice(icon, text) -%}
<div class="workflow-notice">
    <i class="ph ph-{{ icon }}"></i>
    <span>{{ text }}</span>
</div>
{%- endmacro %}

{% macro workflow_progress(bar_id='progress-bar', text_id='progress-text', count_id='progress-count') -%}
<div class="progress-bar-container">
    <div class="progress-bar" id="{{ bar_id }}" style="width:0%"></div>
</div>
<div class="workflow-progress-meta">
    <span id="{{ text_id }}"></span>
    <span id="{{ count_id }}"></span>
</div>
{%- endmacro %}

{% macro workflow_metrics(metrics) -%}
<div class="workflow-metrics">
    {% for metric in metrics %}
    <div class="workflow-metric" style="border-left-color: var({{ metric.color }});">
        <span class="metric-value" id="{{ metric.id }}" style="color: var({{ metric.color }});">0</span>
        <span class="metric-label">{{ metric.label }}</span>
    </div>
    {% endfor %}
</div>
{%- endmacro %}

{% macro workflow_history(history) -%}
<div class="workflow-panel">
    <div class="workflow-header">
        <div class="workflow-title">
            <i class="ph ph-clock-counter-clockwise"></i>
            <span>历史记录</span>
            <span class="workflow-count">（{{ history|length }} 条）</span>
        </div>
    </div>
    {% if history %}
    {% for item in history %}
    <div class="workflow-history-item">
        <div class="workflow-history-header" onclick="toggleHistory(this)">
            <span>
                <span class="workflow-status {{ item.status }}">{{ item.status }}</span>
                成功 {{ item.success }} / 失败 {{ item.failed }}{% if item.skipped %} / 跳过 {{ item.skipped }}{% endif %}
            </span>
            <span class="text-muted">{{ item.created_at }}</span>
        </div>
        <div class="workflow-history-detail">
            <div class="workflow-log history-log" data-log="{{ item.log_json }}"></div>
        </div>
    </div>
    {% endfor %}
    {% else %}
    <div class="empty-state"><i class="ph ph-clock-counter-clockwise"></i>暂无历史记录</div>
    {% endif %}
</div>
{%- endmacro %}
```

- [ ] **Step 4: Migrate sync overview**

Replace the entire `{% block content %}` portion before `{% block scripts %}` in `overview.html` with:

```django
{% from "sync/_workflow.html" import workflow_progress %}

<div class="page-header">
    <h2>同步概览</h2>
</div>

<div class="workflow-flow">
    <button type="button" class="workflow-step" onclick="loadPage('/sync/import')">
        <span class="step-index">1</span>
        <span>
            <span class="step-name">导入采集表</span>
            <span class="step-desc">粘贴分享链接、等级和标签，写入本地数据库</span>
        </span>
    </button>
    <button type="button" class="workflow-step" onclick="loadPage('/sync/resolve')">
        <span class="step-index">2</span>
        <span>
            <span class="step-name">解析采集表</span>
            <span class="step-desc">解析短链接并获取 sec_user_id</span>
        </span>
    </button>
    <button type="button" class="workflow-step" onclick="loadPage('/sync/account')">
        <span class="step-index">3</span>
        <span>
            <span class="step-name">同步账号表</span>
            <span class="step-desc">生成账号记录并获取账号详情</span>
        </span>
    </button>
    <button type="button" class="workflow-step" onclick="loadPage('/sync/refresh')">
        <span class="step-index">4</span>
        <span>
            <span class="step-name">更新账号表</span>
            <span class="step-desc">补齐未获取详情的账号资料</span>
        </span>
    </button>
</div>

<div class="workflow-panel">
    <div class="workflow-header">
        <div class="workflow-title">
            <i class="ph ph-lightning"></i>
            <span>一键执行</span>
        </div>
        <div class="workflow-actions">
            <button class="btn btn-primary" onclick="syncAll()" id="sync-all-btn">
                <i class="ph ph-lightning"></i> 一键同步
            </button>
            <button class="btn btn-danger" onclick="stopSync()" id="stop-sync-btn" style="display:none;">
                <i class="ph ph-stop"></i> 停止
            </button>
        </div>
    </div>
    <div class="text-muted" style="font-size:12px;line-height:1.7;">
        依次执行：导入采集表 → 解析采集表 → 同步账号表
    </div>
    <div id="sync-progress" style="display:none;margin-top:12px;">
        {{ workflow_progress('sync-progress-bar', 'sync-progress-text', 'sync-progress-count') }}
    </div>
</div>

<div class="workflow-panel">
    <div class="workflow-header">
        <div class="workflow-title">
            <i class="ph ph-users"></i>
            <span>账号列表</span>
            <span class="workflow-count">（{{ accounts|length }} 个）</span>
        </div>
    </div>
    <div class="table-scroll">
        <table>
            <thead>
                <tr>
                    <th>名称</th>
                    <th>平台</th>
                    <th>等级</th>
                    <th>粉丝</th>
                    <th>标签</th>
                    <th>同步时间</th>
                </tr>
            </thead>
            <tbody>
                {% for acc in accounts %}
                <tr>
                    <td>{{ acc['账号名称'] or '-' }}</td>
                    <td><span class="platform-tag platform-{{ acc['平台'] }}">{{ acc['平台'] or '-' }}</span></td>
                    <td>{{ '★' * acc['等级'] if acc['等级'] else '-' }}</td>
                    <td>{{ acc['粉丝数'] if acc['粉丝数'] else '-' }}</td>
                    <td>
                        {% for tag in acc['tags_list'][:3] %}
                        <span class="tag">{{ tag }}</span>
                        {% endfor %}
                    </td>
                    <td class="text-muted">{{ acc['同步时间'] or '-' }}</td>
                </tr>
                {% endfor %}
            </tbody>
        </table>
    </div>
    {% if not accounts %}
    <div class="empty-state"><i class="ph ph-users"></i>暂无账号，请先导入数据并执行同步</div>
    {% endif %}
</div>
```

Remove the entire page-local `<style>` block from `overview.html`. Preserve the whole `{% block scripts %}` unchanged.

- [ ] **Step 5: Migrate resolve, account, and refresh step pages**

For each of `resolve.html`, `account.html`, and `refresh.html`:

1. Add this import at the start of `{% block content %}`:

```django
{% from "sync/_workflow.html" import workflow_history, workflow_metrics, workflow_notice, workflow_progress %}
```

2. Remove all page-local CSS for `.sync-log-box`, `.sync-stat-card`, `.history-item`, `.history-header`, `.history-detail`, `.history-log`, and `.status-badge`.

3. In `resolve.html`, replace the notice and execution card with:

```django
{{ workflow_notice('info', '调用 TTD API 解析短链接，获取 sec_user_id。需要 TTD 服务运行中。') }}

<div class="workflow-panel">
    <div class="workflow-header">
        <div class="workflow-title"><i class="ph ph-keyhole"></i><span>执行</span></div>
        <div class="workflow-actions">
            <button class="btn btn-primary" onclick="execStep()" id="exec-btn"><i class="ph ph-play"></i> 执行</button>
            <button class="btn btn-danger" onclick="cancelStep()" id="cancel-btn" style="display:none;"><i class="ph ph-stop"></i> 取消</button>
        </div>
    </div>
    <div id="exec-progress" style="display:none;">
        {{ workflow_progress() }}
        <div style="margin-top:12px;">
            {{ workflow_metrics([
                {"id": "stat-total", "label": "总记录", "color": "--md-primary"},
                {"id": "stat-success", "label": "成功", "color": "--md-success"},
                {"id": "stat-skipped", "label": "跳过", "color": "--md-warning"},
                {"id": "stat-failed", "label": "失败", "color": "--md-error"}
            ]) }}
        </div>
        <div class="workflow-log" id="exec-log" style="margin-top:12px;"></div>
    </div>
</div>
```

4. In `account.html`, replace the notice and execution card with:

```django
{{ workflow_notice('info', '将采集表记录同步到账号表，并获取新账号详情。需要 TTD 服务和 Cookie。') }}

<div class="workflow-panel">
    <div class="workflow-header">
        <div class="workflow-title"><i class="ph ph-users"></i><span>执行</span></div>
        <div class="workflow-actions">
            <button class="btn btn-primary" onclick="execStep()" id="exec-btn"><i class="ph ph-play"></i> 执行</button>
            <button class="btn btn-danger" onclick="cancelStep()" id="cancel-btn" style="display:none;"><i class="ph ph-stop"></i> 取消</button>
        </div>
    </div>
    <div id="exec-progress" style="display:none;">
        {{ workflow_progress() }}
        <div style="margin-top:12px;">
            {{ workflow_metrics([
                {"id": "stat-total", "label": "总记录", "color": "--md-primary"},
                {"id": "stat-success", "label": "成功", "color": "--md-success"},
                {"id": "stat-skipped", "label": "跳过", "color": "--md-warning"},
                {"id": "stat-failed", "label": "失败", "color": "--md-error"}
            ]) }}
        </div>
        <div class="workflow-log" id="exec-log" style="margin-top:12px;"></div>
    </div>
</div>
```

5. In `refresh.html`, replace the notice and execution card with:

```django
{{ workflow_notice('info', '获取账号表中未获取信息的账号资料。需要 TTD 服务和 Cookie。') }}

<div class="workflow-panel">
    <div class="workflow-header">
        <div class="workflow-title"><i class="ph ph-arrow-clockwise"></i><span>执行</span></div>
        <div class="workflow-actions">
            <button class="btn btn-warning" onclick="execStep()" id="exec-btn"><i class="ph ph-play"></i> 执行</button>
            <button class="btn btn-danger" onclick="cancelStep()" id="cancel-btn" style="display:none;"><i class="ph ph-stop"></i> 取消</button>
        </div>
    </div>
    <div id="exec-progress" style="display:none;">
        {{ workflow_progress() }}
        <div style="margin-top:12px;">
            {{ workflow_metrics([
                {"id": "stat-total", "label": "总账号", "color": "--md-primary"},
                {"id": "stat-success", "label": "成功", "color": "--md-success"},
                {"id": "stat-failed", "label": "失败", "color": "--md-error"}
            ]) }}
        </div>
        <div class="workflow-log" id="exec-log" style="margin-top:12px;"></div>
    </div>
</div>
```

6. Replace the full history card with:

```django
{{ workflow_history(history) }}
```

7. Preserve each page's `{% block scripts %}` unchanged.

- [ ] **Step 6: Migrate import page**

In `import.html`:

1. Add the workflow macro import at the start of `{% block content %}`.
2. Remove all page-local CSS for `.sync-log-box`, `.sync-stat-card`, `.history-item`, `.history-header`, `.history-detail`, `.history-log`, and `.status-badge`.
3. Wrap the input area in:

```django
<div class="workflow-panel">
    <div class="workflow-header">
        <div class="workflow-title">
            <i class="ph ph-download"></i>
            <span>数据导入</span>
        </div>
        <div class="workflow-actions">
            <button class="btn btn-secondary" onclick="parseImport()">
                <i class="ph ph-magnifying-glass"></i> 解析预览
            </button>
            <button class="btn btn-primary" onclick="doImport()" id="exec-btn">
                <i class="ph ph-download"></i> 执行导入
            </button>
        </div>
    </div>
    <textarea id="import-text" rows="8" class="sync-textarea" placeholder="格式1: 个2@iMLuCKjq&#10;格式2: {&quot;地址&quot;:&quot;xxx&quot;,&quot;等级&quot;:&quot;个2，图&quot;}" ondrop="handleDrop(event)" ondragover="event.preventDefault()" ondragenter="this.style.borderColor='var(--md-primary)'" ondragleave="this.style.borderColor=''"></textarea>
    <div id="import-status" class="text-muted" style="margin-top:8px;font-size:13px;"></div>
</div>
```

4. Wrap the preview table in:

```django
<div class="workflow-panel" id="import-preview" style="display:none;">
```

5. Replace the result card with:

```django
<div class="workflow-panel" id="result-card" style="display:none;">
    <div class="workflow-header">
        <div class="workflow-title">
            <i class="ph ph-chart-bar"></i>
            <span>执行结果</span>
        </div>
    </div>
    {{ workflow_metrics([
        {"id":"stat-total","label":"总记录","color":"--md-primary"},
        {"id":"stat-created","label":"新增","color":"--md-success"},
        {"id":"stat-updated","label":"更新/恢复","color":"--md-success"},
        {"id":"stat-duplicate","label":"重复","color":"--md-warning"},
        {"id":"stat-skipped","label":"跳过","color":"--md-warning"},
        {"id":"stat-failed","label":"失败","color":"--md-error"}
    ]) }}
    <div class="workflow-log" id="exec-log" style="margin-top:12px;"></div>
</div>
```

6. Replace history markup with `{{ workflow_history(history) }}`.
7. Preserve scripts unchanged.

- [ ] **Step 7: Run sync page tests**

```powershell
D:\AI\DoukHub\venv\Scripts\python.exe -m pytest tests\test_workflow_ui.py tests\test_api.py -v
```

Expected: all tests pass.

- [ ] **Step 8: Commit**

```powershell
git add app\templates\sync app\static\css\style.css tests\test_workflow_ui.py
git commit -m "feat: unify sync workflow interface"
```

---

### Task 3: Read-Only Collection Preview API

**Files:**

- Modify: `app/main.py`
- Test: `tests/test_collection_api.py`

**Interfaces:**

- Consumes `CollectionBatchRequest`.
- Consumes `plan_collection(accounts, rating_min=3, tags=None, account_names="", record_ids=None, platform="douyin", mode="incremental")`.
- Produces `POST /api/collection/batches/preview`.
- Response shape:

```json
{
  "success": true,
  "total_accounts": 3,
  "incremental_accounts": 1,
  "first_run_accounts": 1,
  "skipped_accounts": 1,
  "platforms": [
    {
      "platform": "douyin",
      "total_accounts": 2,
      "incremental_accounts": 1,
      "first_run_accounts": 1,
      "skipped_accounts": 0
    }
  ]
}
```

- [ ] **Step 1: Write failing preview tests**

Append to `tests/test_collection_api.py`:

```python
def test_collection_preview_is_read_only(batch_client):
    client, database, manager = batch_client
    database.get_all_accounts.return_value = [
        {
            "record_id": "a1",
            "账号名称": "新账号",
            "平台": "抖音",
            "链接": "",
            "sec_user_id": "sec1",
            "等级": 4,
            "标签": "",
            "启用": 1,
            "last_collected_at": None,
            "collect_window_days": None,
        },
        {
            "record_id": "a2",
            "账号名称": "已采账号",
            "平台": "抖音",
            "链接": "",
            "sec_user_id": "sec2",
            "等级": 4,
            "标签": "",
            "启用": 1,
            "last_collected_at": "2026-08-14 10:00:00",
            "collect_window_days": None,
        },
        {
            "record_id": "a3",
            "账号名称": "TikTok",
            "平台": "TikTok",
            "链接": "",
            "sec_user_id": "tiksec",
            "等级": 4,
            "标签": "",
            "启用": 1,
            "last_collected_at": None,
            "collect_window_days": None,
        },
    ]
    response = client.post(
        "/api/collection/batches/preview",
        json={"rating_min": 3, "platform": "all", "mode": "incremental"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True
    assert data["total_accounts"] == 3
    assert data["first_run_accounts"] == 1
    assert data["incremental_accounts"] == 1
    assert data["skipped_accounts"] == 1
    assert data["platforms"][0]["platform"] == "douyin"
    assert data["platforms"][0]["total_accounts"] == 2
    assert manager.start.await_count == 0


def test_collection_preview_returns_400_when_no_accounts_match(batch_client):
    client, database, manager = batch_client
    database.get_all_accounts.return_value = []
    response = client.post("/api/collection/batches/preview", json={})
    assert response.status_code == 400
    assert response.json()["message"] == "没有符合条件的账号"
    manager.start.assert_not_called()
```

- [ ] **Step 2: Run failing preview tests**

```powershell
D:\AI\DoukHub\venv\Scripts\python.exe -m pytest tests\test_collection_api.py -k preview -v
```

Expected: both tests return 404 because the route does not exist.

- [ ] **Step 3: Implement preview route**

In `app/main.py`, add this import near the collection imports if it is not already present:

```python
from .core.collection_planner import plan_collection
```

Insert the route after `GET /api/collection/batches` and before `GET /api/collection/batches/{batch_id}`:

```python
@app.post("/api/collection/batches/preview")
async def api_preview_collection_batch(request: CollectionBatchRequest):
    """Preview the account selection without creating a batch."""
    accounts = get_database().get_all_accounts()
    platforms = (
        ("douyin", "tiktok") if request.platform == "all" else (request.platform,)
    )
    platform_results = []
    totals = {
        "total_accounts": 0,
        "incremental_accounts": 0,
        "first_run_accounts": 0,
        "skipped_accounts": 0,
    }

    for platform in platforms:
        planned = plan_collection(
            accounts=accounts,
            rating_min=request.rating_min,
            tags=request.tags,
            account_names=request.account_names,
            platform=platform,
            mode=request.mode,
        )
        if not planned:
            continue
        skipped = sum(item.status == "skipped" for item in planned)
        first_run = sum(
            item.status == "pending" and item.earliest == "" for item in planned
        )
        incremental = sum(
            item.status == "pending" and item.earliest != "" for item in planned
        )
        result = {
            "platform": platform,
            "total_accounts": len(planned),
            "incremental_accounts": incremental,
            "first_run_accounts": first_run,
            "skipped_accounts": skipped,
        }
        platform_results.append(result)
        for key in totals:
            totals[key] += result[key]

    if not platform_results:
        return JSONResponse(
            {"success": False, "message": "没有符合条件的账号"},
            status_code=400,
        )
    return {"success": True, **totals, "platforms": platform_results}
```

- [ ] **Step 4: Run preview tests and collection API suite**

```powershell
D:\AI\DoukHub\venv\Scripts\python.exe -m pytest tests\test_collection_api.py -v
```

Expected: all collection API tests pass.

- [ ] **Step 5: Commit**

```powershell
git add app\main.py tests\test_collection_api.py
git commit -m "feat: preview collection account scope"
```

---

### Task 4: Collection Daily Run Console

**Files:**

- Modify: `app/templates/collect.html`
- Modify: `tests/test_workflow_ui.py`
- Modify: `tests/test_collection_api.py`

**Interfaces:**

- Consumes `POST /api/collection/batches/preview`.
- Consumes existing collection batch APIs.
- Produces page elements:
  - `collection-tabs`
  - `collection-status`
  - `collection-last-run`
  - `collection-last-success`
  - `preview-total`
  - `preview-first-run`
  - `preview-incremental`
  - `preview-skipped`
  - `account-form`
  - `batch-detail`
  - `batch-table-body`

- [ ] **Step 1: Write failing collection console tests**

Append to `tests/test_workflow_ui.py`:

```python
def test_collect_page_is_daily_incremental_console(app_env):
    client, *_ = app_env
    response = client.get("/collect")
    assert response.status_code == 200
    assert "日常增量采集" in response.text
    assert "workflow-tabs" in response.text
    assert "workflow-panel" in response.text
    assert "单作品采集" in response.text
    assert "开始日常增量采集" in response.text
    assert 'id="collection-last-run"' in response.text
    assert 'id="collection-last-success"' in response.text


def test_collect_page_contains_preview_metrics(app_env):
    client, *_ = app_env
    response = client.get("/collect")
    assert 'id="preview-total"' in response.text
    assert 'id="preview-first-run"' in response.text
    assert 'id="preview-incremental"' in response.text
    assert 'id="preview-skipped"' in response.text


def test_collect_page_uses_workflow_tab_active_state(app_env):
    client, *_ = app_env
    response = client.get("/collect")
    assert "classList.toggle('active', mode === 'account')" in response.text
```

Append to `tests/test_collection_api.py`:

```python
def test_collect_page_calls_preview_without_starting_batch():
    source = Path("app/templates/collect.html").read_text(encoding="utf-8")
    assert "/api/collection/batches/preview" in source
    assert "previewCollectionScope(" in source
    assert "startCollectionBatch" in source
```

- [ ] **Step 2: Run failing collection UI tests**

```powershell
D:\AI\DoukHub\venv\Scripts\python.exe -m pytest tests\test_workflow_ui.py tests\test_collection_api.py -v
```

Expected: collection console tests fail.

- [ ] **Step 3: Replace collection page top-level markup**

In `collect.html`, remove page-local styles for:

```css
.collect-mode-grid
.mode-card
.batch-summary-grid
.batch-summary-item
.batch-summary-label
.batch-summary-value
```

Keep only `#task-modal > div` and page-specific responsive rules.

Replace the page header through the current `batch-card` with:

```django
<div class="page-header">
    <h2>采集</h2>
</div>

<div class="workflow-tabs" id="collection-tabs">
    <button type="button" class="workflow-tab active" id="mode-account-btn" onclick="showCollectMode('account')">
        <i class="ph ph-download"></i> 日常增量采集
    </button>
    <button type="button" class="workflow-tab" id="mode-detail-btn" onclick="showCollectMode('detail')">
        <i class="ph ph-link"></i> 单作品采集
    </button>
</div>

<div class="workflow-panel" id="collection-status-panel">
    <div class="workflow-header">
        <div class="workflow-title">
            <i class="ph ph-pulse"></i>
            <span>采集状态</span>
        </div>
        <span class="workflow-status pending" id="collection-status">等待查看</span>
    </div>
    <div class="workflow-metrics">
        <div class="workflow-metric" style="border-left-color: var(--md-secondary);">
            <span class="metric-value" id="collection-last-run">-</span>
            <span class="metric-label">上次采集</span>
        </div>
        <div class="workflow-metric" style="border-left-color: var(--md-success);">
            <span class="metric-value" id="collection-last-success">-</span>
            <span class="metric-label">上次成功账号</span>
        </div>
        <div class="workflow-metric" style="border-left-color: var(--md-primary);">
            <span class="metric-value" id="preview-total">-</span>
            <span class="metric-label">可采账号</span>
        </div>
        <div class="workflow-metric" style="border-left-color: var(--md-warning);">
            <span class="metric-value" id="preview-first-run">-</span>
            <span class="metric-label">首次全量</span>
        </div>
        <div class="workflow-metric" style="border-left-color: var(--md-success);">
            <span class="metric-value" id="preview-incremental">-</span>
            <span class="metric-label">日常增量</span>
        </div>
        <div class="workflow-metric" style="border-left-color: var(--md-outline);">
            <span class="metric-value" id="preview-skipped">-</span>
            <span class="metric-label">配置跳过</span>
        </div>
    </div>
</div>

<div class="workflow-panel" id="account-form-card">
    <div class="workflow-header">
        <div class="workflow-title">
            <i class="ph ph-download"></i>
            <span>日常增量采集</span>
        </div>
        <div class="workflow-actions">
            <button type="submit" form="account-form" class="btn btn-primary" id="account-submit">
                <i class="ph ph-download-simple"></i> 开始日常增量采集
            </button>
        </div>
    </div>
    <form id="account-form" onsubmit="startCollectionBatch(event)">
        <div class="workflow-form-grid">
            <div class="form-group">
                <label>等级筛选（采集选中等级及以上）</label>
                <div class="btn-group">
                    <label class="tag"><input type="checkbox" name="rating" value="4" checked> 4星</label>
                    <label class="tag"><input type="checkbox" name="rating" value="3" checked> 3星</label>
                    <label class="tag"><input type="checkbox" name="rating" value="2"> 2星</label>
                    <label class="tag"><input type="checkbox" name="rating" value="1"> 1星</label>
                </div>
            </div>
            <div class="form-group">
                <label>标签筛选</label>
                <input type="text" name="tags" placeholder="多, 个人">
            </div>
            <div class="form-group">
                <label>平台</label>
                <select name="platform">
                    <option value="douyin">抖音</option>
                    <option value="tiktok">TikTok</option>
                    <option value="all">全部</option>
                </select>
            </div>
            <div class="form-group">
                <label>采集模式</label>
                <select name="mode">
                    <option value="incremental">首次全量，后续增量</option>
                    <option value="full">重新全量</option>
                </select>
            </div>
            <div class="form-group" style="grid-column: 1 / -1;">
                <label>指定账号（留空为全部符合条件的账号）</label>
                <input type="text" name="account_names" placeholder="账号名称, 多个用逗号分隔">
            </div>
        </div>
    </form>
</div>
```

Keep the existing `detail-form-card` markup, but change its outer class from `card` to `workflow-panel`.

Change the batch card to:

```django
<div class="workflow-panel" id="batch-card" style="display:none;">
    <div class="workflow-header">
        <div class="workflow-title">
            <i class="ph ph-list-checks"></i>
            <span>当前批次</span>
        </div>
        <div class="workflow-actions" id="batch-detail-actions"></div>
    </div>
    <div id="batch-detail"></div>
</div>
```

Change the history card outer class from `card` to `workflow-panel`, preserving table columns and IDs.

- [ ] **Step 4: Update collection JavaScript**

Keep all existing single-work safety logic unchanged. Make these exact additions and replacements:

1. Replace the two `classList.toggle('selected', ...)` lines in `showCollectMode()` with:

```javascript
document.getElementById('mode-account-btn').classList.toggle('active', mode === 'account');
document.getElementById('mode-detail-btn').classList.toggle('active', mode === 'detail');
```

2. Add a payload builder after `resolveGeneration`:

```javascript
function collectionPayloadFromForm(form) {
    const ratings = form.getAll('rating').map(Number);
    return {
        rating_min: ratings.length ? Math.min(...ratings) : 3,
        tags: String(form.get('tags') || '')
            .split(/[,，]/).map(value => value.trim()).filter(Boolean),
        account_names: form.get('account_names') || '',
        platform: form.get('platform') || 'douyin',
        mode: form.get('mode') || 'incremental',
    };
}
```

3. Add preview logic:

```javascript
let previewTimer = null;

async function previewCollectionScope() {
    const form = new FormData(document.getElementById('account-form'));
    const status = document.getElementById('collection-status');
    status.className = 'workflow-status running';
    status.textContent = '正在统计';
    try {
        const data = await apiCall(
            '/api/collection/batches/preview',
            'POST',
            collectionPayloadFromForm(form)
        );
        if (data.success === false) {
            throw new Error(data.message || '无法统计');
        }
        document.getElementById('preview-total').textContent = data.total_accounts || 0;
        document.getElementById('preview-first-run').textContent =
            data.first_run_accounts || 0;
        document.getElementById('preview-incremental').textContent =
            data.incremental_accounts || 0;
        document.getElementById('preview-skipped').textContent =
            data.skipped_accounts || 0;
        status.className = 'workflow-status success';
        status.textContent = '范围已更新';
    } catch (error) {
        document.getElementById('preview-total').textContent = '-';
        document.getElementById('preview-first-run').textContent = '-';
        document.getElementById('preview-incremental').textContent = '-';
        document.getElementById('preview-skipped').textContent = '-';
        status.className = 'workflow-status failed';
        status.textContent = error.message || '无法统计';
    }
}

function queueCollectionPreview() {
    if (previewTimer) clearTimeout(previewTimer);
    previewTimer = setTimeout(previewCollectionScope, 250);
}
```

4. Add batch-state status logic:

```javascript
function updateCollectionStatus(batches) {
    const status = document.getElementById('collection-status');
    const active = batches.find(batch =>
        ['pending', 'running', 'cancelling'].includes(batch.status)
    );
    const latest = batches[0];
    if (active) {
        status.className = `workflow-status ${active.status}`;
        status.textContent = `${formatPlatform(active.platform)} ${formatBatchStatus(active.status)}`;
    } else if (latest) {
        status.className = `workflow-status ${latest.status}`;
        status.textContent = `${formatPlatform(latest.platform)} ${formatBatchStatus(latest.status)}`;
    } else {
        status.className = 'workflow-status pending';
        status.textContent = '空闲';
    }
    document.getElementById('collection-last-run').textContent = latest
        ? formatDateTime(latest.started_at)
        : '-';
    document.getElementById('collection-last-success').textContent = latest
        ? String(latest.success_accounts || 0)
        : '-';
}
```

Call `updateCollectionStatus(data.batches || []);` immediately after fetching batches in `refreshCollectionBatches()`.

5. In `startCollectionBatch()`, replace inline payload construction with:

```javascript
const payload = collectionPayloadFromForm(new FormData(e.target));
```

Change the button restore label to `开始日常增量采集`.

6. Bind preview inputs immediately after `showCollectMode('account');`:

```javascript
document.querySelectorAll('#account-form input, #account-form select').forEach(element => {
    element.addEventListener('change', queueCollectionPreview);
    element.addEventListener('input', queueCollectionPreview);
});
previewCollectionScope();
```

7. In `_spaCleanup`, also clear `previewTimer`.

8. Replace the metric markup in `showBatchDetail()` with:

```javascript
document.getElementById('batch-detail').innerHTML = `
    <div class="workflow-metrics" style="margin-bottom:12px;">
        <div class="workflow-metric">
            <span class="metric-value">${batch.total_accounts || 0}</span>
            <span class="metric-label">预计账号</span>
        </div>
        <div class="workflow-metric">
            <span class="metric-value">${formatCurrentAccount(data.items || [])}</span>
            <span class="metric-label">当前账号</span>
        </div>
        <div class="workflow-metric">
            <span class="metric-value">${formatElapsedSeconds(batchElapsedSeconds(batch))}</span>
            <span class="metric-label">已运行</span>
        </div>
        <div class="workflow-metric">
            <span class="metric-value">${batch.success_accounts || 0}</span>
            <span class="metric-label">成功</span>
        </div>
        <div class="workflow-metric">
            <span class="metric-value">${batch.failed_accounts || 0}</span>
            <span class="metric-label">失败</span>
        </div>
    </div>
    <div class="table-scroll" style="margin-bottom:12px;">
        <table>
            <thead><tr><th>账号</th><th>状态</th><th>上次采集</th><th>信息</th></tr></thead>
            <tbody>${rows}</tbody>
        </table>
    </div>
    <details>
        <summary class="text-muted">批次日志</summary>
        <pre class="workflow-log" style="max-height:240px;">${escapeHtml((data.log || []).join('\\n'))}</pre>
    </details>
`;
```

9. Keep batch polling and actions, but render the history status cell with:

```javascript
<span class="workflow-status ${batch.status}">${formatBatchStatus(batch.status)}</span>
```

instead of plain text.

- [ ] **Step 5: Run collection UI/API tests**

```powershell
D:\AI\DoukHub\venv\Scripts\python.exe -m pytest tests\test_workflow_ui.py tests\test_collection_api.py tests\test_api.py -v
```

Expected: all tests pass.

- [ ] **Step 6: Commit**

```powershell
git add app\templates\collect.html tests\test_workflow_ui.py tests\test_collection_api.py
git commit -m "feat: redesign collection run console"
```

---

### Task 5: Browser And Regression Verification

**Files:**

- Modify: `DEVELOPMENT.md`
- Modify: `tests/test_workflow_ui.py`

**Interfaces:**

- Consumes all workflow UI and preview API changes.

- [ ] **Step 1: Add page-contract test**

Append to `tests/test_workflow_ui.py`:

```python
def test_all_workflow_pages_use_shared_status_component(app_env):
    client, *_ = app_env
    for path in [
        "/sync/overview",
        "/sync/import",
        "/sync/resolve",
        "/sync/account",
        "/sync/refresh",
        "/collect",
    ]:
        response = client.get(path)
        assert response.status_code == 200
        assert "workflow-panel" in response.text
        assert "workflow-status" in response.text or path in ("/sync/import", "/sync/overview")
```

- [ ] **Step 2: Run focused UI tests**

```powershell
D:\AI\DoukHub\venv\Scripts\python.exe -m pytest tests\test_workflow_ui.py tests\test_collection_api.py tests\test_api.py -v
```

Expected: all pass.

- [ ] **Step 3: Run full suite**

```powershell
D:\AI\DoukHub\venv\Scripts\python.exe -m pytest -q
```

Expected: all tests pass, with only the known FastAPI TestClient deprecation warning.

- [ ] **Step 4: Browser visual verification**

Start the worktree app on a temporary port:

```powershell
D:\AI\DoukHub\venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 4299
```

Verify in a browser at `http://127.0.0.1:4299`:

1. `/sync/overview`: four workflow steps render in one row on desktop.
2. `/sync/resolve`, `/sync/account`, `/sync/refresh`: shared notice, execution panel, metrics, and history render.
3. `/collect`: default tab is `日常增量采集`.
4. Preview metrics load without starting a batch.
5. Switch to `单作品采集`; editing the textarea still disables stale downloads.
6. Batch table appears under shared table styling.
7. Narrow viewport to 390px: no page-level horizontal overflow except intended table scroll; no overlap.
8. Browser console has no JavaScript errors.
9. Take desktop and mobile screenshots of `/sync/overview` and `/collect`.

- [ ] **Step 5: Document UI verification**

Append to `DEVELOPMENT.md`:

```markdown
## 统一工作流界面验证

同步页面按一次性数据准备流程组织，采集页面按日常增量运行台组织。两者共用 `workflow-*` 样式。验证浏览器时，需要同时打开 `/sync/overview` 与 `/collect`，确认视觉一致、状态文本清晰，并在约 390px 宽度下检查表格只在自己的滚动容器内横向滚动。
```

- [ ] **Step 6: Commit verification docs**

```powershell
git add DEVELOPMENT.md tests\test_workflow_ui.py
git commit -m "docs: verify unified workflow interface"
```

---

## Plan Self-Review

- Spec coverage:
  - Shared visual language: Tasks 1 and 2.
  - Sync wizard flow and step pages: Task 2.
  - Read-only scope preview: Task 3.
  - Daily incremental run console and secondary single-work tab: Task 4.
  - Responsive, console-error, and browser verification: Tasks 1 and 5.
  - No business/API behavior changes except read-only preview: Tasks 3 and 4.
- Placeholder scan:
  - No `TBD`, `TODO`, or unspecified "appropriate" implementation steps.
  - Every code change has exact CSS, HTML, Jinja, Python, or JavaScript content.
- Type consistency:
  - Preview response fields match collection page IDs and JavaScript.
  - Existing task-driven element IDs remain unchanged.
  - `collectionPayloadFromForm()` output matches both preview and batch-create request models.
