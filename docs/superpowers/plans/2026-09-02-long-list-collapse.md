# 长列表折叠 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 全站长列表统一折叠：默认只显示前 20 行，超过时出现「展开全部（共 X 条）/ 收起」按钮；增量采集页轮询重渲染不重置展开状态。

**Architecture:** 在 base.html 增加全局 JS 工具 `clipList(container, opts)`；各页面在渲染完成后调用；增量采集的账号明细表用模块级变量记住展开状态，重渲染后恢复。

**Tech Stack:** 原生 JavaScript、Jinja2、pytest（字符串断言，仓库无 JS 测试框架）。

## Global Constraints

- 上限统一 20 行；行数 ≤ 20 不出现按钮，页面与改动前完全一致。
- 纯前端改动：不改任何后端接口、不动服务端渲染的数据量。
- 文案：收起态「展开全部（共 X 条）」，展开态「收起」。
- 工具条按 class `list-clip-bar` 标识，不计入总数、不参与行筛选。
- 增量采集账号明细表：轮询重渲染后保持用户的展开/收起选择；切换批次时重置为收起。
- 只改路由真实存在的页面：collect.html、sync/import.html、base.html。
- 不动已有机制：批次历史「加载更多」、详情弹窗分块、table.html 分页、collect/overview 最近 5 条。

---

### Task 1: base.html 全局 clipList 工具

**Files:**
- Modify: `app/templates/base.html`（`function showToast`/`async function apiCall` 所在 `<script>` 块）
- Modify: `app/templates/base.html`（`.dh-modal-overlay.dh-show` 样式附近 `<style>` 块）
- Test: `tests/test_long_list_ui.py`

**Interfaces:**
- Consumes: none。
- Produces: 全局 `clipList(container, opts) -> {toggle(), isExpanded(), total}`；`opts = {limit=20, rowSelector, rows, onToggle}`。

- [ ] **Step 1: Write the failing test**

Create `tests/test_long_list_ui.py`:

```python
from tests.test_api import app_env  # noqa: F401


def test_base_includes_clip_list_tool(app_env):
    client, *_ = app_env
    response = client.get("/status")
    assert response.status_code == 200
    assert "function clipList" in response.text
    assert "list-clip-bar" in response.text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_long_list_ui.py -q`
Expected: FAIL，断言找不到 `function clipList`。

- [ ] **Step 3: Add the clipList tool to base.html**

在 base.html 中 `async function apiCall(...)` 所在 `<script>` 块内、`apiCall` 函数定义之后，追加：

```html
<script>
// ===== 长列表折叠：默认只显示前 limit 行，超过时追加「展开全部 / 收起」 =====
function clipList(container, opts) {
    opts = opts || {};
    var limit = opts.limit || 20;
    var rows = [];
    for (var i = 0; i < container.children.length; i++) {
        var el = container.children[i];
        if (!el.classList || el.classList.contains('list-clip-bar')) continue;
        if (opts.rows && opts.rows.indexOf(el) < 0) continue;
        if (opts.rowSelector && !el.matches(opts.rowSelector)) continue;
        rows.push(el);
    }
    var total = rows.length;
    var state = { expanded: false };
    var handle = {
        total: total,
        isExpanded: function() { return state.expanded; },
        toggle: function() {
            if (total <= limit) return state.expanded;
            state.expanded = !state.expanded;
            apply();
            return state.expanded;
        }
    };
    if (total <= limit) return handle;

    var bar = makeClipBar(container);
    function apply() {
        rows.forEach(function(row, idx) {
            row.style.display = (state.expanded || idx < limit) ? '' : 'none';
        });
        var btn = bar.querySelector('.list-clip-btn');
        if (btn) btn.textContent = state.expanded ? '收起' : '展开全部（共 ' + total + ' 条）';
        if (opts.onToggle) opts.onToggle(state.expanded);
    }
    bar.querySelector('.list-clip-btn').addEventListener('click', function() {
        state.expanded = !state.expanded;
        apply();
    });
    apply();
    return handle;
}

function makeClipBar(container) {
    var inner = '<button type="button" class="btn btn-secondary btn-sm list-clip-btn"></button>';
    if (container.tagName === 'TBODY') {
        var firstRow = container.querySelector('tr');
        var cols = (firstRow && firstRow.cells) ? firstRow.cells.length : 4;
        var tr = document.createElement('tr');
        tr.className = 'list-clip-bar';
        tr.innerHTML = '<td colspan="' + cols + '" style="text-align:center;padding:10px 0;border:none;">' + inner + '</td>';
        container.appendChild(tr);
        return tr;
    }
    var item = document.createElement(container.tagName === 'UL' || container.tagName === 'OL' ? 'li' : 'div');
    item.className = 'list-clip-bar';
    item.innerHTML = inner;
    container.appendChild(item);
    return item;
}
</script>
```

在 base.html 的 `<style>` 块内、`.dh-modal-overlay.dh-show { ... }` 规则之后追加：

```css
.list-clip-bar { list-style:none; text-align:center; padding:10px 0; }
.list-clip-bar .list-clip-btn { font-size:12px; }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_long_list_ui.py -q`
Expected: PASS, 1 test。

- [ ] **Step 5: Commit**

```bash
git add app/templates/base.html tests/test_long_list_ui.py
git commit -m "feat: global clipList collapse tool for long lists"
```

---

### Task 2: 增量采集账号明细表折叠（跨轮询保持）

**Files:**
- Modify: `app/templates/collect.html`
- Test: `tests/test_long_list_ui.py`

**Interfaces:**
- Consumes: Task 1 的全局 `clipList()`。
- Produces: `applyAccountTableClip()`；模块级 `_accountTableExpanded`；`showRunningDetail()` 每次重渲染后自动调用。

- [ ] **Step 1: Write the failing test**

在 `tests/test_long_list_ui.py` 末尾追加：

```python
def test_collect_wires_account_table_clip(app_env):
    client, *_ = app_env
    response = client.get("/collect")
    assert response.status_code == 200
    assert "applyAccountTableClip" in response.text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_long_list_ui.py::test_collect_wires_account_table_clip -q`
Expected: FAIL，断言找不到 `applyAccountTableClip`。

- [ ] **Step 3: Add the account-table clip**

在 `collect.html` 的 `var runDetailOpen = false;` 声明附近添加状态变量与函数（放在 `async function showRunningDetail` 之前）：

```javascript
var _accountTableExpanded = false;   // 账号明细表是否处于「展开全部」状态（轮询重渲染后保持）

function applyAccountTableClip() {
    var tbody = document.getElementById('run-account-table');
    if (!tbody) return;
    // 只统计账号行：排除作品明细行（run-works-row）与工具条（list-clip-bar）
    var handle = clipList(tbody, {
        rowSelector: 'tr:not(.run-works-row)',
        onToggle: function(expanded) { _accountTableExpanded = expanded; }
    });
    if (_accountTableExpanded && handle.total > 20) handle.toggle();
}
```

在 `showRunningDetail()` 中，`document.getElementById('run-account-table').innerHTML = rows;` 这一行之后添加：

```javascript
        applyAccountTableClip();
```

在 `showRunningDetail()` 内“切换新批次时清空旧日志与明细、重置增量计数”的分支里，把：

```javascript
        if (batchId !== (showRunningDetail._currentBatchId || null)) {
            showRunningDetail._currentBatchId = batchId;
            runLogCount = 0;
            if (logViewEl) logViewEl.innerHTML = '';
        }
```

改为：

```javascript
        if (batchId !== (showRunningDetail._currentBatchId || null)) {
            showRunningDetail._currentBatchId = batchId;
            runLogCount = 0;
            _accountTableExpanded = false;   // 切换批次时重置展开状态
            if (logViewEl) logViewEl.innerHTML = '';
        }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_long_list_ui.py -q`
Expected: PASS, 2 tests。

- [ ] **Step 5: Commit**

```bash
git add app/templates/collect.html tests/test_long_list_ui.py
git commit -m "feat: collapse running-batch account table to 20 rows, keep state across polls"
```

---

### Task 3: 每账号作品明细折叠

**Files:**
- Modify: `app/templates/collect.html`
- Test: 复用 Task 2 的 `test_collect_wires_account_table_clip`（已断言 `clipAllWorksLists`）。

**Interfaces:**
- Consumes: Task 1 的 `clipList()`。
- Produces: `clipAllWorksLists(root)`；`toggleRunWorks()`、`toggleBdWorks()` 展开作品行时调用。

- [ ] **Step 1: Write the failing test**

在 `tests/test_long_list_ui.py` 末尾追加：

```python
def test_collect_has_works_list_clip_helper(app_env):
    client, *_ = app_env
    response = client.get("/collect")
    assert response.status_code == 200
    assert "function clipAllWorksLists" in response.text
    assert "clipAllWorksLists(row)" in response.text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_long_list_ui.py::test_collect_has_works_list_clip_helper -q`
Expected: FAIL，断言找不到 `function clipAllWorksLists`。

- [ ] **Step 3: Add the works-list clip**

在 `collect.html` 中 `function toggleRunWorks(btn) {` 之前添加：

```javascript
// 折叠一个区域内的所有作品标题列表（每个列表限 20 条）
function clipAllWorksLists(root) {
    if (!root) return;
    var lists = root.querySelectorAll('.run-works-list');
    for (var i = 0; i < lists.length; i++) {
        // 已有工具条的列表不再重复添加（同一作品行反复开合时避免按钮堆积）
        if (lists[i].querySelector('.list-clip-bar')) continue;
        clipList(lists[i], { limit: 20 });
    }
}
```

在 `toggleRunWorks(btn)` 中，把展开判定与显示逻辑改为（只加一行：展开时裁剪列表）：

```javascript
    var opened = row.style.display !== 'none';
    row.style.display = opened ? 'none' : '';
    if (!opened) clipAllWorksLists(row);   // 展开时才折叠内部作品列表
```

在 `toggleBdWorks(btn)` 中，把：

```javascript
    var opened = panel.classList.toggle('open');
    btn.classList.toggle('open', opened);
    btn.style.opacity = opened ? '1' : '0.6';
```

改为（`classList.toggle` 返回 `true` 表示刚展开，展开时折叠面板内作品列表）：

```javascript
    var opened = panel.classList.toggle('open');
    if (opened) clipAllWorksLists(panel);
    btn.classList.toggle('open', opened);
    btn.style.opacity = opened ? '1' : '0.6';
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_long_list_ui.py -q`
Expected: PASS, 3 tests。

- [ ] **Step 5: Commit**

```bash
git add app/templates/collect.html tests/test_long_list_ui.py
git commit -m "feat: collapse per-account works lists to 20 titles"
```

---

### Task 4: 导入预览表折叠

**Files:**
- Modify: `app/templates/sync/import.html`
- Test: `tests/test_long_list_ui.py`

**Interfaces:**
- Consumes: Task 1 的 `clipList()`。
- Produces: `parseImport()` 渲染预览后调用 `clipList(#preview-body)`。

- [ ] **Step 1: Write the failing test**

在 `tests/test_long_list_ui.py` 末尾追加：

```python
def test_import_preview_wires_clip(app_env):
    client, *_ = app_env
    response = client.get("/sync/import")
    assert response.status_code == 200
    assert "clipList(document.getElementById('preview-body')" in response.text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_long_list_ui.py::test_import_preview_wires_clip -q`
Expected: FAIL，断言找不到 `clipList(document.getElementById('preview-body')`。

- [ ] **Step 3: Wire the preview clip**

在 `app/templates/sync/import.html` 的 `async function parseImport()` 中，`document.getElementById('preview-body').innerHTML = parsedData.map(...).join('');` 这一行之后添加：

```javascript
        if (window.clipList) clipList(document.getElementById('preview-body'), { rowSelector: 'tr' });
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_long_list_ui.py -q`
Expected: PASS, 4 tests。

- [ ] **Step 5: Commit**

```bash
git add app/templates/sync/import.html tests/test_long_list_ui.py
git commit -m "feat: collapse import preview table to 20 rows"
```

---

### Task 5: 全量验证和手工冒烟

**Files:**
- 无代码改动；只跑验证。

- [ ] **Step 1: Run the verification suite**

Run:

```bash
pytest tests/test_long_list_ui.py tests/test_workflow_ui.py tests/test_api.py tests/test_import_preview_ui.py tests/test_collection_history_ui.py -q -k "not creates_default_config_when_missing"
rg -n "TBD|TODO|implement later|fill in details" docs/superpowers/plans/2026-09-02-long-list-collapse.md
```

Expected: tests PASS（`test_creates_default_config_when_missing` 是既有失败用例，已用 `-k` 排除）；plan placeholder scan returns no matches。

- [ ] **Step 2: Manual Windows smoke test**

1. 启动 DoukHub，运行一个账号数 > 20 的增量批次：运行面板的账号明细表默认只显示 20 行，末尾有「展开全部（共 X 条）」；点击后全部展开。
2. 展开状态下等待几次轮询刷新（约 500ms 间隔）：展开状态保持，不缩回。
3. 再次点击「收起」：恢复 20 行。
4. 点击某账号行前面的文件夹图标展开作品明细：若该账号作品标题 > 20，列表内同样出现「展开全部」按钮。
5. 打开 `/sync/import`，粘贴 30+ 条链接并解析：预览表只显示 20 行 + 展开按钮。
6. 少于 20 行的场景（小批次、少链接）：不出现任何按钮，页面与改动前一致。
7. 切换运行批次（运行另一个批次）：账号明细表重新收起为 20 行（展开状态已重置）。

- [ ] **Step 3: Commit any leftover changes**

若冒烟中发现需要微调，直接修改并提交；否则无需提交。

## Self-Review

- 范围覆盖：统一工具（Task 1）、增量采集账号明细表（Task 2）、作品明细（Task 3）、导入预览（Task 4）、验证（Task 5）。
- 交互行为：默认 20 行；超过才出现按钮；增量采集轮询不重置展开状态；切批次重置。
- 数据边界：纯前端；批次历史「加载更多」、详情弹窗分块、表浏览分页、概览最近 5 条、遗留模板（accounts.html/history.html）均不动。
- 一致性：所有页面复用同一个 `clipList()`；文案统一「展开全部（共 X 条）/ 收起」；工具条 class 统一 `list-clip-bar`。
- 状态保持：`_accountTableExpanded` 仅在增量采集页使用，切换批次时重置，避免跨批次误展开。
