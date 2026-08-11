# 表浏览页面交互重构 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 重做表浏览页面的排序/筛选交互——点表头不排序、竖点弹自写菜单、4 套主题通用化、后端筛选服务端化。

**Architecture:** 后端在 `query_table()` 增加列级筛选参数并通过 API 透传;前端用 AG Grid 自定义 headerComponent 替换默认表头,竖点按钮弹自写扁平菜单;菜单样式改用 4 套主题共有的通用 CSS 变量。

**Tech Stack:** Python/FastAPI/SQLite,AG Grid 31(community),原生 JS + 4 套自定义 CSS 主题(obsidian/material/glass/slate)。

## Global Constraints

- Python >= 3.12,变量名与现有代码一致(中文业务字段 / 英文系统字段)。
- AG Grid 31:`ag-grid-community.min.js`(CDN),用 `agGrid.createGrid` 全局 API。
- 筛选操作符仅支持 `contains` / `equals` 两种;不做隐藏/显示列、不做复杂操作符(YAGNI)。
- 所有前端样式只能用通用 CSS 变量(`--accent`、`--surface-overlay`、`--surface-raised`、`--menu-hover`、`--text-tertiary`、`--bg-surface`、`--border-default`、`--radius`、`--shadow-lg`、`--success`、`--danger`、`--text-primary`、`--text-secondary`、`--text-muted`、`--bg-hover`、`--bg-muted`、`--bg-input`、`--transition`),不得直接引用 `--md-*` 或 `--glass-*` 专属变量。
- 每个任务结束必须提交 git(中文 commit message,遵循现有 `feat/fix/docs` 前缀风格)。

---

### Task 1: 后端 `query_table()` 支持列级筛选

**Files:**
- Modify: `app/core/database.py:638-678`(query_table 方法)
- Test: `tests/test_database_generic.py`(追加筛选测试)

**Interfaces:**
- Consumes: `Database` 类现有结构、`self._connect()` 上下文管理器、`insert_cookie()` 测试辅助。
- Produces: `query_table()` 新增参数 `filter_field: Optional[str] = None`、`filter_value: Optional[str] = None`、`filter_op: Optional[str] = None`。返回结构不变 `{records, total, limit, offset}`。

- [ ] **Step 1: 追加筛选单元测试**

在 `tests/test_database_generic.py` 末尾追加:

```python
# ========== query_table 列级筛选 ==========

def test_query_table_filter_contains(db):
    db.insert_cookie({"record_id": "c1", "Cookie": "abc123"})
    db.insert_cookie({"record_id": "c2", "Cookie": "def456"})
    r = db.query_table("cookie_cache", filter_field="Cookie", filter_value="bc", filter_op="contains")
    assert r["total"] == 1
    assert r["records"][0]["record_id"] == "c1"


def test_query_table_filter_equals(db):
    db.insert_cookie({"record_id": "c1", "Cookie": "abc"})
    db.insert_cookie({"record_id": "c2", "Cookie": "abd"})
    r = db.query_table("cookie_cache", filter_field="Cookie", filter_value="abc", filter_op="equals")
    assert r["total"] == 1
    assert r["records"][0]["record_id"] == "c1"


def test_query_table_filter_equals_chinese(db):
    db.insert_cookie({"record_id": "c1", "Cookie": "aaa", "备注": "测试"})
    db.insert_cookie({"record_id": "c2", "Cookie": "bbb", "备注": "其他"})
    r = db.query_table("cookie_cache", filter_field="备注", filter_value="测试", filter_op="equals")
    assert r["total"] == 1
    assert r["records"][0]["record_id"] == "c1"


def test_query_table_filter_invalid_field_ignored(db):
    """筛选字段不存在时，应忽略筛选（不报错）"""
    db.insert_cookie({"record_id": "c1", "Cookie": "aaa"})
    r = db.query_table("cookie_cache", filter_field="不存在的字段", filter_value="x", filter_op="contains")
    assert r["total"] == 1


def test_query_table_filter_unknown_op_ignored(db):
    """未识别的操作符应忽略筛选"""
    db.insert_cookie({"record_id": "c1", "Cookie": "aaa"})
    r = db.query_table("cookie_cache", filter_field="Cookie", filter_value="aaa", filter_op="regex")
    assert r["total"] == 1


def test_query_table_search_and_filter_combined(db):
    """search 与 filter 应为 AND 关系"""
    db.insert_cookie({"record_id": "c1", "Cookie": "abc", "备注": "测试"})
    db.insert_cookie({"record_id": "c2", "Cookie": "abc", "备注": "其他"})
    r = db.query_table("cookie_cache", search="abc", filter_field="备注", filter_value="测试", filter_op="equals")
    assert r["total"] == 1
    assert r["records"][0]["record_id"] == "c1"


def test_query_table_filter_and_sort_combined(db):
    db.insert_cookie({"record_id": "c1", "Cookie": "bbb", "备注": "测试"})
    db.insert_cookie({"record_id": "c2", "Cookie": "aaa", "备注": "测试"})
    r = db.query_table("cookie_cache", filter_field="备注", filter_value="测试", filter_op="equals", sort_field="Cookie", sort_order="asc")
    cookies = [x["Cookie"] for x in r["records"]]
    assert cookies == ["aaa", "bbb"]
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_database_generic.py -k "filter" -v`
Expected: FAIL(TypeError: query_table() got an unexpected keyword argument 'filter_field')

- [ ] **Step 3: 实现 `query_table()` 筛选逻辑**

修改 `app/core/database.py` 的 `query_table` 方法签名与 WHERE 构造:

```python
    def query_table(
        self,
        table: str,
        limit: int = 100,
        offset: int = 0,
        search: str = "",
        sort_field: Optional[str] = None,
        sort_order: str = "desc",
        filter_field: Optional[str] = None,
        filter_value: Optional[str] = None,
        filter_op: Optional[str] = None,
    ) -> dict:
        """通用表查询，支持搜索、排序、列级筛选、分页。
        返回 {records, total, limit, offset}
        """
        with self._connect() as conn:
            cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]

            # 构造 WHERE（search 与 filter 为 AND 关系）
            params: list[Any] = []
            where_parts: list[str] = []
            if search:
                where_parts.append("(" + " OR ".join([f'CAST("{c}" AS TEXT) LIKE ?' for c in cols]) + ")")
                params += [f"%{search}%"] * len(cols)
            if filter_field and filter_field in cols and filter_value is not None:
                if filter_op == "equals":
                    where_parts.append(f'CAST("{filter_field}" AS TEXT) = ?')
                    params.append(str(filter_value))
                elif filter_op == "contains":
                    where_parts.append(f'CAST("{filter_field}" AS TEXT) LIKE ?')
                    params.append(f"%{filter_value}%")
            where = (" WHERE " + " AND ".join(where_parts)) if where_parts else ""

            # 构造 ORDER BY
            order = " ORDER BY rowid DESC"
            if sort_field and sort_field in cols:
                direction = "ASC" if sort_order.lower() == "asc" else "DESC"
                order = f' ORDER BY "{sort_field}" {direction}'

            total = conn.execute(
                f"SELECT COUNT(*) FROM {table}{where}", params
            ).fetchone()[0]
            rows = conn.execute(
                f"SELECT * FROM {table}{where}{order} LIMIT ? OFFSET ?",
                params + [limit, offset],
            ).fetchall()
            return {
                "records": [dict(row) for row in rows],
                "total": total,
                "limit": limit,
                "offset": offset,
            }
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_database_generic.py -v`
Expected: PASS(全部通过,含新增筛选测试)

- [ ] **Step 5: 提交**

```bash
git add app/core/database.py tests/test_database_generic.py
git commit -m "feat(db): query_table 支持列级筛选(contains/equals)"
```

---

### Task 2: 后端 API 透传筛选参数

**Files:**
- Modify: `app/main.py:1284-1308`(api_database_table 端点)
- Test: `tests/test_api.py`(追加筛选 API 测试)

**Interfaces:**
- Consumes: Task 1 的 `query_table()` 新参数;`get_database()`;`db.VALID_TABLES`。
- Produces: `GET /api/database/table/{table_name}` 接受 `filter_field`/`filter_value`/`filter_op` 查询参数并透传。

- [ ] **Step 1: 查看现有 API 测试配置**

Run: `head -60 tests/test_api.py`
Expected: 看清该文件如何调用 FastAPI TestClient 或直接调用端点函数。若文件用 TestClient,按同样方式追加;若直接调用函数,则直接调用 `api_database_table`。

- [ ] **Step 2: 追加 API 筛选测试**

在 `tests/test_api.py` 追加(用与现有测试一致的调用方式;此处以 TestClient 为例,若不同请按 Step 1 结论调整):

```python
def test_api_table_filter(create_db):
    """API 支持 filter_field/filter_value/filter_op 参数"""
    from fastapi.testclient import TestClient
    from app.main import app
    client = TestClient(app)
    # 先插入数据（通过现有 API 或直接操作 db fixture）
    resp = client.get(
        "/api/database/table/cookie_cache",
        params={"filter_field": "Cookie", "filter_value": "abc", "filter_op": "contains"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "records" in data and "total" in data
```

注意:若 `tests/test_api.py` 没有 `create_db` fixture 或 TestClient 用法,请先阅读该文件实际结构,复用其现有 fixture 与调用约定,确保测试可独立运行。

- [ ] **Step 3: 运行测试确认失败**

Run: `python -m pytest tests/test_api.py -v`
Expected: FAIL(端点未接受 filter_field 参数,TestClient 400 或忽略该参数导致断言失败)

- [ ] **Step 4: 修改 API 端点透传参数**

修改 `app/main.py` 的 `api_database_table`:

```python
@app.get("/api/database/table/{table_name}")
async def api_database_table(
    table_name: str,
    limit: int = 100,
    offset: int = 0,
    search: str = "",
    sort_field: str = "",
    sort_order: str = "desc",
    filter_field: str = "",
    filter_value: str = "",
    filter_op: str = "",
):
    """获取表数据，支持分页、搜索、排序、列级筛选。返回 {records, total, limit, offset}"""
    db = get_database()

    # 验证表名
    if table_name not in db.VALID_TABLES:
        return JSONResponse({"success": False, "message": "无效的表名"}, status_code=400)

    result = db.query_table(
        table_name,
        limit=limit,
        offset=offset,
        search=search,
        sort_field=sort_field or None,
        sort_order=sort_order,
        filter_field=filter_field or None,
        filter_value=filter_value or None,
        filter_op=filter_op or None,
    )
    return result
```

- [ ] **Step 5: 运行测试确认通过**

Run: `python -m pytest tests/test_api.py -v`
Expected: PASS

- [ ] **Step 6: 提交**

```bash
git add app/main.py tests/test_api.py
git commit -m "feat(api): /api/database/table 透传列级筛选参数"
```

---

### Task 3: 4 套主题补齐通用 CSS 变量

**Files:**
- Modify: `app/static/css/theme-obsidian.css`
- Modify: `app/static/css/theme-material.css`
- Modify: `app/static/css/theme-glass.css`
- Modify: `app/static/css/theme-slate.css`

**Interfaces:**
- Consumes: 各主题现有 `:root` 变量块(在每个文件 `:root { ... }` 内追加)。
- Produces: 4 套主题均定义 `--surface-overlay`、`--surface-raised`、`--menu-hover`、`--text-tertiary` 四个通用变量,供 Task 4 前端样式引用。

- [ ] **Step 1: 为 obsidian 主题追加变量**

在 `app/static/css/theme-obsidian.css` 的 `:root` 块末尾(闭合 `}` 前)追加(映射到 Material 语义):

```css
  --surface-overlay: var(--md-surface-container-high);
  --surface-raised: var(--md-surface-container);
  --menu-hover: var(--md-primary-container);
  --text-tertiary: var(--md-on-surface-variant);
```

- [ ] **Step 2: 为 material 主题追加变量**

在 `app/static/css/theme-material.css` 的 `:root` 块末尾追加(与 obsidian 相同映射):

```css
  --surface-overlay: var(--md-surface-container-high);
  --surface-raised: var(--md-surface-container);
  --menu-hover: var(--md-primary-container);
  --text-tertiary: var(--md-on-surface-variant);
```

- [ ] **Step 3: 为 glass 主题追加变量**

先查看 glass 主题有哪些可用色变量(如 `--glass-surface-elevated`、`--glass-text-muted`),在 `app/static/css/theme-glass.css` 的 `:root` 块末尾追加:

```css
  --surface-overlay: var(--glass-surface-elevated);
  --surface-raised: var(--glass-surface-elevated);
  --menu-hover: var(--glass-accent-light);
  --text-tertiary: var(--glass-text-muted);
```

> 注意:`--surface-overlay` 的取值需根据 glass 主题实际存在的变量名调整。若 glass 没有以 `--glass-surface` 开头的合适变量,可用 rgba 直写,如 `--surface-overlay: rgba(255, 255, 255, 0.85);`。请先 grep glass 主题的已定义变量再定值,确保引用的变量一定存在。

- [ ] **Step 4: 为 slate 主题追加变量**

先查看 slate 主题的可用变量(如 `--bg-surface`、`--text-muted`、`--accent-light`),在 `app/static/css/theme-slate.css` 的 `:root` 块末尾追加:

```css
  --surface-overlay: var(--bg-surface);
  --surface-raised: var(--bg-surface);
  --menu-hover: var(--accent-light);
  --text-tertiary: var(--text-muted);
```

> 注意:同样先 grep slate 主题已定义变量,选择确实存在的变量名,确保不产生无效引用。

- [ ] **Step 5: 校验变量定义**

Run: `grep -l -- "--surface-overlay" app/static/css/theme-*.css`
Expected: 4 个文件都列出(obsidian/material/glass/slate)。
再运行 `grep -nE "^  --(surface-overlay|surface-raised|menu-hover|text-tertiary):" app/static/css/theme-*.css` 确认 4 个变量在 4 套主题中都定义。

- [ ] **Step 6: 提交**

```bash
git add app/static/css/theme-*.css
git commit -m "feat(theme): 4套主题补齐通用CSS变量(surface-overlay/raised/menu-hover/text-tertiary)"
```

---

### Task 4: 前端——自定义表头组件 + 自写菜单 + 状态管理

**Files:**
- Modify: `app/templates/table.html`

**Interfaces:**
- Consumes:
  - Task 1/2 的 API(`filter_field`/`filter_value`/`filter_op`/`sort_field`/`sort_order` 参数)。
  - Task 3 的 4 个通用 CSS 变量。
  - 现有函数:`showTable()`(约 1263 行)、`buildColumnDefs()`(约 1072 行)、`ensureGrid()`(约 1172 行)、`renderTableTabs()`、`apiCall()`、`showToast()`。
- Produces: 前端排序/筛选状态变量、自定义 headerComponent 类、自写菜单渲染函数、`showTable()` 请求参数扩展。

**设计要点：**
- 新增状态变量:`currentSortField`、`currentSortOrder`、`currentFilterField`、`currentFilterOp`、`currentFilterValue`。
- 自定义 headerComponent 类 `DoukHeaderComponent`,渲染列名 + 排序指示 + 竖点按钮;点击竖点调用全局 `openColMenu(event, field)` 弹自写菜单。
- 自写菜单:一个全局浮层 DOM,含升序/降序/清除排序项 + 筛选输入区(操作符 select + 值 input + 应用/清除按钮)。
- 排序列在菜单中高亮,表头显示箭头指示。
- `showTable()` 的 URL 携带 sort_* 与 filter_* 参数;变更排序/筛选后重置到第 1 页。

- [ ] **Step 1: 新增排序/筛选状态变量与重置函数**

在 `table.html` 的"状态变量"段落(约 827-835 行,`currentTotal` 附近)追加:

```js
    let currentSortField = '';
    let currentSortOrder = '';
    let currentFilterField = '';
    let currentFilterOp = '';
    let currentFilterValue = '';
```

在 `showTable()` 函数(约 1263 行)开头(设置 `currentTable` 之后)追加重置逻辑:

```js
    function showTable(tableName, page = 1, keyword = '') {
        currentTable = tableName;
        currentPage = page;
        currentKeyword = keyword;
        // 切换表时重置排序/筛选
        if (typeof currentSortField !== 'undefined' && document.getElementById('table-tabs') &&
            document.getElementById('table-tabs').dataset.lastTable !== tableName) {
            currentSortField = '';
            currentSortOrder = '';
            currentFilterField = '';
            currentFilterOp = '';
            currentFilterValue = '';
        }
        if (document.getElementById('table-tabs')) {
            document.getElementById('table-tabs').dataset.lastTable = tableName;
        }
        updateTabActive();
        // ... 其余现有逻辑保持不变
```

- [ ] **Step 2: 自定义 headerComponent 类**

在 `table.html` 的"自定义单元格渲染器"段落之后(约 953 行 `FIELD_RENDERERS` 之后)追加:

```js
    // =========================================================================
    // 自定义表头组件（点表头不排序，竖点按钮弹自写菜单）
    // =========================================================================
    class DoukHeaderComponent {
        init(params) {
            this.params = params;
            this.field = params.column.getColDef().field;
            this.displayName = params.displayName;
            this.gui = document.createElement('div');
            this.gui.style.cssText = 'display:flex;align-items:center;justify-content:space-between;width:100%;height:100%;padding:0 8px;cursor:default;user-select:none;';
            this._render();
        }
        _render() {
            const isSorted = currentSortField === this.field;
            const order = isSorted ? currentSortOrder : '';
            const arrow = order === 'asc' ? '↑' : (order === 'desc' ? '↓' : '');
            this.gui.innerHTML =
                '<span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="' +
                this.displayName.replace(/"/g, '&quot;') + '">' + this.displayName +
                (isSorted ? '<span style="color:var(--accent);margin-left:4px;font-weight:700;">' + arrow + '</span>' : '') +
                '</span>' +
                '<button class="tbl-header-menu-btn" data-field="' + this.field + '" title="菜单：排序/筛选" style="' +
                'flex-shrink:0;border:none;background:transparent;color:var(--text-muted);cursor:pointer;padding:2px 4px;border-radius:4px;font-size:13px;line-height:1;' +
                '">⋮</button>';
            this.gui.querySelector('.tbl-header-menu-btn').addEventListener('click', (e) => {
                e.stopPropagation();
                openColMenu(e, this.field);
            });
        }
        refresh(params) {
            this._render();
            return true;
        }
        getGui() { return this.gui; }
        destroy() { this.gui = null; }
    }
```

- [ ] **Step 3: 自写菜单渲染与交互函数**

在 `DoukHeaderComponent` 之后追加:

```js
    // =========================================================================
    // 自写列菜单（排序 + 筛选）
    // =========================================================================
    let colMenuEl = null;
    let colMenuField = '';

    function openColMenu(event, field) {
        closeColMenu();
        colMenuField = field;
        const btn = event.target;
        const rect = btn.getBoundingClientRect();
        const menu = document.createElement('div');
        menu.className = 'tbl-col-menu';
        menu.style.cssText =
            'position:fixed;z-index:20000;min-width:200px;background:var(--surface-overlay);' +
            'border:1px solid var(--border-default);border-radius:var(--radius-sm);' +
            'box-shadow:var(--shadow-lg);padding:6px;font-size:13px;color:var(--text-primary);';
        let left = rect.left;
        let top = rect.bottom + 6;
        if (left + 220 > window.innerWidth) left = window.innerWidth - 220;
        if (top + 260 > window.innerHeight) top = rect.top - 260;
        menu.style.left = left + 'px';
        menu.style.top = top + 'px';

        const isSorted = currentSortField === field;
        const curOrder = isSorted ? currentSortOrder : '';

        function item(icon, label, active, onClick) {
            const el = document.createElement('div');
            el.style.cssText =
                'display:flex;align-items:center;gap:8px;padding:7px 10px;border-radius:var(--radius-xs);' +
                'cursor:pointer;color:' + (active ? 'var(--accent)' : 'var(--text-primary)') + ';font-weight:' + (active ? '600' : '400');
            el.innerHTML = '<i class="ph ' + icon + '" style="font-size:15px;"></i><span>' + label + '</span>';
            el.addEventListener('mouseenter', () => { el.style.background = 'var(--menu-hover)'; });
            el.addEventListener('mouseleave', () => { el.style.background = 'transparent'; });
            el.addEventListener('click', onClick);
            return el;
        }

        menu.appendChild(item('ph-sort-ascending', '升序', curOrder === 'asc', (e) => {
            e.stopPropagation();
            applyColSort(field, 'asc');
        }));
        menu.appendChild(item('ph-sort-descending', '降序', curOrder === 'desc', (e) => {
            e.stopPropagation();
            applyColSort(field, 'desc');
        }));
        menu.appendChild(item('ph-x', '清除排序', false, (e) => {
            e.stopPropagation();
            applyColSort(field, '');
        }));

        // 分隔线
        const sep = document.createElement('div');
        sep.style.cssText = 'height:1px;background:var(--border-default);margin:6px 4px;';
        menu.appendChild(sep);

        // 筛选区
        const filterRow = document.createElement('div');
        filterRow.style.cssText = 'display:flex;flex-direction:column;gap:6px;padding:2px 2px;';
        const filterHead = document.createElement('div');
        filterHead.textContent = '筛选';
        filterHead.style.cssText = 'font-size:11px;font-weight:600;color:var(--text-tertiary);text-transform:uppercase;letter-spacing:0.04em;padding:2px 6px;';
        filterRow.appendChild(filterHead);

        const opSelect = document.createElement('select');
        opSelect.style.cssText = 'padding:5px 8px;border:1px solid var(--border-default);border-radius:var(--radius-xs);font-size:12px;background:var(--bg-input);color:var(--text-primary);font-family:inherit;';
        opSelect.innerHTML = '<option value="contains">包含</option><option value="equals">等于</option>';
        if (currentFilterField === field && currentFilterOp) opSelect.value = currentFilterOp;

        const valInput = document.createElement('input');
        valInput.type = 'text';
        valInput.placeholder = '筛选值…';
        valInput.style.cssText = 'padding:5px 8px;border:1px solid var(--border-default);border-radius:var(--radius-xs);font-size:12px;background:var(--bg-input);color:var(--text-primary);font-family:inherit;';
        if (currentFilterField === field) valInput.value = currentFilterValue || '';

        const btnRow = document.createElement('div');
        btnRow.style.cssText = 'display:flex;gap:6px;justify-content:flex-end;';

        const applyBtn = document.createElement('button');
        applyBtn.textContent = '应用';
        applyBtn.className = 'btn btn-primary btn-sm';
        applyBtn.addEventListener('click', (e) => {
            e.stopPropagation();
            applyColFilter(field, opSelect.value, valInput.value.trim());
        });
        btnRow.appendChild(applyBtn);

        const clearBtn = document.createElement('button');
        clearBtn.textContent = '清除';
        clearBtn.className = 'btn btn-secondary btn-sm';
        clearBtn.addEventListener('click', (e) => {
            e.stopPropagation();
            applyColFilter(field, '', '');
        });
        btnRow.appendChild(clearBtn);

        filterRow.appendChild(opSelect);
        filterRow.appendChild(valInput);
        filterRow.appendChild(btnRow);
        menu.appendChild(filterRow);

        document.body.appendChild(menu);
        colMenuEl = menu;
        if (valInput.value) valInput.focus();
    }

    function closeColMenu() {
        if (colMenuEl) { colMenuEl.remove(); colMenuEl = null; }
        colMenuField = '';
    }

    function applyColSort(field, order) {
        closeColMenu();
        currentSortField = field;
        currentSortOrder = order;
        showTable(currentTable, 1, currentKeyword);
    }

    function applyColFilter(field, op, value) {
        closeColMenu();
        if (op && value) {
            currentFilterField = field;
            currentFilterOp = op;
            currentFilterValue = value;
        } else {
            currentFilterField = '';
            currentFilterOp = '';
            currentFilterValue = '';
        }
        showTable(currentTable, 1, currentKeyword);
    }

    // 点击外部区域 / Esc 关闭菜单
    document.addEventListener('click', (e) => {
        if (colMenuEl && !colMenuEl.contains(e.target) && !e.target.classList.contains('tbl-header-menu-btn')) {
            closeColMenu();
        }
    });
    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') closeColMenu();
    });
```

- [ ] **Step 4: 修改 `buildColumnDefs()` 挂载 headerComponent**

在 `app/templates/table.html` 的 `buildColumnDefs()` 中,给每个数据列 colDef(约 1096-1106 行)追加 `headerComponent: DoukHeaderComponent`,并保持 `sortable: false`:

```js
            let colDef = {
                field: field.name,
                headerName: FIELD_DISPLAY_NAMES[field.name] || field.name,
                sortable: false,
                resizable: true,
                editable: isEditable,
                width: FIELD_WIDTHS[field.name] || 120,
                suppressMovable: true,
                filter: false,
                headerComponent: DoukHeaderComponent,
                headerClass: (() => { const c = getFieldCategory(tableName, field.name); return c ? 'col-cat-' + c : null; })(),
            };
```

同时在 `ensureGrid()` 的 `defaultColDef`(约 1183 行)中,将 `sortable: true` 改为 `sortable: false`,`floatingFilter` 保持 `false`:

```js
                defaultColDef: {
                    sortable: false,
                    resizable: true,
                    minWidth: 50,
                    floatingFilter: false,
                },
```

- [ ] **Step 5: 修改 `showTable()` 请求 URL 携带排序/筛选参数**

修改 `showTable()` 中 URL 构造(约 1287-1289 行):

```js
        const offset = (page - 1) * PAGE_SIZE;
        let url = '/api/database/table/' + tableName + '?limit=' + PAGE_SIZE + '&offset=' + offset;
        if (keyword) url += '&search=' + encodeURIComponent(keyword);
        if (currentSortField) url += '&sort_field=' + encodeURIComponent(currentSortField) + '&sort_order=' + encodeURIComponent(currentSortOrder || 'asc');
        if (currentFilterField && currentFilterValue) url += '&filter_field=' + encodeURIComponent(currentFilterField) + '&filter_value=' + encodeURIComponent(currentFilterValue) + '&filter_op=' + encodeURIComponent(currentFilterOp || 'contains');
```

- [ ] **Step 6: 为表头菜单按钮与菜单补充 CSS**

在 `table.html` 的 `<style>` 块内 CSS 追加(放在 AG Grid 头部样式附近):

```css
/* 表头菜单按钮（竖点） */
.tbl-header-menu-btn:hover {
    background: var(--bg-hover);
    color: var(--accent) !important;
}
/* 自写列菜单 */
.tbl-col-menu {
    animation: tbl-menu-in 120ms ease-out;
}
@keyframes tbl-menu-in {
    from { opacity: 0; transform: translateY(-4px); }
    to { opacity: 1; transform: translateY(0); }
}
```

- [ ] **Step 7: 验证前端语法与引用一致性**

Run: `grep -n "DoukHeaderComponent" app/templates/table.html`
Expected: 出现 ≥ 3 处(类定义、buildColumnDefs 引用)。
Run: `grep -nE "currentSortField|currentFilterField" app/templates/table.html`
Expected: 状态变量声明 + 多处使用。
Run: `grep -n "surface-overlay\|menu-hover\|text-tertiary" app/templates/table.html | head`
Expected: openColMenu 中引用了这些变量。
Run: `python -m pytest tests/test_database_generic.py tests/test_api.py -v`
Expected: PASS(确认后端未受影响)。

- [ ] **Step 8: 提交**

```bash
git add app/templates/table.html
git commit -m "feat(ui): 表浏览自定义表头+自写列菜单+服务端排序筛选"
```

---

### Task 5: 端到端手动验证

**Files:** 无(运行应用验证)

**Interfaces:**
- Consumes: Task 1-4 全部产物。
- Produces: 验证通过结论,发现并记录问题。

- [ ] **Step 1: 启动应用**

Run: `python main.py`(或 `start.bat`)
Expected: 应用启动,浏览器打开表浏览页面(http://127.0.0.1:2999 或控制台提示地址)。

- [ ] **Step 2: 验证点表头不排序**

操作:在任一表(如 Cookie表)点击表头单元格文字。
Expected: 不触发排序,数据顺序不变。

- [ ] **Step 3: 验证排序菜单**

操作:点击某列表头右侧竖点按钮 → 菜单弹出 → 点"升序"→ 点"降序"→ 点"清除排序"。
Expected: 每次操作后数据正确重新排序;菜单高亮当前排序状态;表头显示箭头;翻页后排序保持。

- [ ] **Step 4: 验证筛选**

操作:点击竖点 → 选择"包含"输入值 → 应用;再试"等于";点"清除"。
Expected: 数据按筛选条件过滤且作用于全量(总计 count 变化);翻页保持筛选;清除后恢复。

- [ ] **Step 5: 验证菜单关闭交互**

操作:打开菜单后点击页面空白处、按 Esc。
Expected: 菜单关闭。

- [ ] **Step 6: 验证 4 套主题**

操作:右上角主题切换器依次切 obsidian/material/glass/slate,在各主题下打开列菜单。
Expected: 菜单在各主题下背景/文字/hover 观感一致且协调,无失效样式。

- [ ] **Step 7: 验证搜索与筛选组合**

操作:搜索关键词 + 某列筛选同时生效。
Expected: 结果同时满足两者(AND)。

- [ ] **Step 8: 提交验证结论(如有修复)**

若发现问题,修复后提交:
```bash
git add -A
git commit -m "fix(ui): 表浏览交互验证修复"
```

## 自审记录

- **Spec 覆盖**:① 点表头不排序 → Task 4(sortable:false + 自定义表头);② 自写菜单(升序/降序/清除/筛选) → Task 4;③ 主题通用化 → Task 3 + Task 4;④ 后端筛选服务端化 → Task 1 + Task 2;⑤ 排序/筛选状态与翻页重置 → Task 4;⑥ 测试计划 → Task 1/2 单测 + Task 5 手动验证。
- **占位符**:已逐项填充具体代码,无 TBD/TODO。
- **类型一致性**:`filter_field/filter_value/filter_op/sort_field/sort_order` 在 Task 1(query_table)、Task 2(API)、Task 4(前端 URL)三处签名一致(字符串,空串视为 None);`currentSortField/currentSortOrder/currentFilterField/currentFilterOp/currentFilterValue` 在 Task 4 内声明与使用一致。