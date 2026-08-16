# 单作品采集闭环实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**目标：** 完成第一阶段单作品采集闭环，使视频、图集、实况、动图、模板命名、目录偏好、下载历史和失败重试全部可在 DoukHub UI 中完成。

**架构：** TTD Web API 负责作品元数据解析，DoukHub 自己下载单作品文件，不写 TTD `download_data`。模板和目录偏好保存在用户配置 JSON，下载历史保存在 DoukHub SQLite。页面保持“先解析、再确认下载”的两步流。

**技术栈：** FastAPI、Pydantic、httpx、SQLite、Jinja2、原生 HTML5 Drag and Drop、pytest。

## 全局约束

- 所有用户操作通过 DoukHub UI 完成，不要求使用终端。
- 单作品下载不写、不改、不清空 TTD 的 `download_data`。
- 保留 `.part` 临时文件和成功后原子替换行为。
- 目标文件已存在时使用 `(2)`、`(3)` 后缀，不覆盖。
- 文件名验证发生在网络请求和文件写入之前。
- 不做 GIF 转换；TTD 返回的动图按视频保存。
- 不实现账号批量、直播、评论、搜索、收藏夹或合集。
- 不新增第三方依赖。
- 命令统一使用 `.\venv\Scripts\python.exe`。
- 当前工作区有用户未提交改动，只修改本计划列出的文件，不回滚他人改动。

---

### 任务 1：标准化作品资产并扩展下载器

**文件：**

- 修改：`app/core/single_work.py`
- 测试：`tests/test_single_work.py`

**接口：**

- 消费：TTD 详情响应的 `type`、`downloads`、`music_url`、`static_cover`、`dynamic_cover`。
- 产出：`normalize_assets`、`normalize_work(...)["assets"]`、支持覆盖文件名和资产选择的 `download_work`。

- [ ] **步骤 1：写失败测试**

在 `tests/test_single_work.py` 追加：

```python
def test_normalize_work_preserves_asset_types_and_order():
    work = normalize_work(
        {
            "id": "1234567890123456789",
            "desc": "实况标题",
            "nickname": "作者",
            "create_time": "2026-08-15 10:00:00",
            "type": "实况",
            "downloads": [
                "https://cdn.example/live-1.mp4",
                "https://cdn.example/live-2.mp4",
            ],
            "music_url": "https://cdn.example/music.mp3",
            "static_cover": "https://cdn.example/static.jpg",
            "dynamic_cover": "https://cdn.example/dynamic.jpg",
        },
        "douyin",
    )
    assert [asset["kind"] for asset in work["assets"]] == [
        "live_photo", "live_photo", "music", "static_cover", "dynamic_cover"
    ]
    assert [asset["index"] for asset in work["assets"]] == [1, 2, 3, 4, 5]
    assert work["downloads"] == [
        "https://cdn.example/live-1.mp4",
        "https://cdn.example/live-2.mp4",
    ]
```

```python
def test_download_work_selects_asset_and_uses_override(tmp_path):
    async def handler(request):
        return httpx.Response(
            200,
            content=b"data",
            headers={"content-type": "image/jpeg"},
        )

    async def run():
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            downloads = [
                "https://cdn.example/a.jpg",
                "https://cdn.example/b.jpg",
            ]
            work = {
                "id": "1",
                "title": "标题",
                "author": "作者",
                "create_time": "2026-08-15 10-00-00",
                "type": "图集",
                "platform": "douyin",
                "downloads": downloads,
                "assets": normalize_assets("图集", downloads),
            }
            return await download_work(
                client,
                work,
                tmp_path,
                filename_override="自定义名字",
                asset_indexes=[2],
            )

    paths = asyncio.run(run())
    assert [path.name for path in paths] == ["自定义名字.jpg"]
```

- [ ] **步骤 2：确认失败**

运行：`.\venv\Scripts\python.exe -m pytest tests\test_single_work.py -v`

预期：新增测试因 `normalize_assets` 和新下载参数不存在而失败。

- [ ] **步骤 3：实现**

在 `app/core/single_work.py` 增加：

```python
PRIMARY_ASSET_KINDS = {"video", "image", "live_photo"}


def _asset(kind: str, index: int, url: str) -> dict:
    return {"kind": kind, "index": index, "url": str(url or "")}


def normalize_assets(
    work_type: str,
    downloads: list[str],
    music_url: str = "",
    static_cover: str = "",
    dynamic_cover: str = "",
) -> list[dict]:
    work_type = str(work_type or "")
    if "实况" in work_type:
        primary_kind = "live_photo"
    elif any(word in work_type for word in ("视频", "动图")):
        primary_kind = "video"
    else:
        primary_kind = "image"

    assets = []
    index = 1
    for url in downloads:
        if url:
            assets.append(_asset(primary_kind, index, url))
            index += 1
    for kind, url in (
        ("music", music_url),
        ("static_cover", static_cover),
        ("dynamic_cover", dynamic_cover),
    ):
        if url:
            assets.append(_asset(kind, index, url))
            index += 1
    return assets
```

`normalize_work` 返回中保留 `downloads`，并新增：

```python
"assets": normalize_assets(
    work_type,
    downloads,
    raw.get("music_url") or "",
    raw.get("static_cover") or "",
    raw.get("dynamic_cover") or "",
)
```

`build_filename` 增加参数 `override: str = ""`，并支持 `type`、`platform` 字段：

```python
if override:
    stem = sanitize_filename_part(override, MAX_FILENAME_STEM)
else:
    stem = template.format(
        create_time=sanitize_filename_part(work.get("create_time"), 24),
        author=sanitize_filename_part(work.get("author")),
        title=sanitize_filename_part(work.get("title")),
        id=sanitize_filename_part(work.get("id"), 24),
        type=sanitize_filename_part(work.get("type")),
        platform=sanitize_filename_part(work.get("platform")),
    ).strip()
```

`_extension` 根据 `asset_kind` 回退：`video/live_photo -> .mp4`，`music -> .mp3`，其他默认 `.jpg`。

`download_work` 签名改为：

```python
async def download_work(
    client, work, target_dir,
    template="{create_time} {author} {title}",
    filename_override="",
    asset_indexes=None,
    include_music=False,
    include_static_cover=False,
    include_dynamic_cover=False,
):
```

选择规则：

```python
if asset_indexes:
    selected = [a for a in assets if a["index"] in set(asset_indexes)]
else:
    selected = [a for a in assets if a["kind"] in PRIMARY_ASSET_KINDS]
    selected.extend(a for a in assets if (
        (a["kind"] == "music" and include_music)
        or (a["kind"] == "static_cover" and include_static_cover)
        or (a["kind"] == "dynamic_cover" and include_dynamic_cover)
    ))
```

下载循环继续使用临时文件、`_unique_path`、原子替换；多文件时追加 `_1`、`_2`。

- [ ] **步骤 4：验证并提交**

```powershell
.\venv\Scripts\python.exe -m pytest tests\test_single_work.py -v
git add app\core\single_work.py tests\test_single_work.py
git commit -m "feat: normalize single-work assets"
```

---

### 任务 2：持久化单作品偏好

**文件：**

- 修改：`app/core/config.py`
- 测试：`tests/test_config.py`

**接口：**

- 产出：`DEFAULT_CONFIG["single_work"]`、`Config.single_work`，并让 `Config.download_path` 优先读取单作品目录。

- [ ] **步骤 1：写失败测试**

```python
def test_single_work_preferences_have_defaults(self, tmp_path):
    cfg = Config(tmp_path / "config.json")
    assert cfg.single_work["default_template_id"] == "default"
    assert cfg.single_work["templates"][0]["template"] == (
        "{create_time} {author} {title}"
    )
    assert cfg.single_work["recent_dirs"] == []


def test_single_work_download_path_overrides_local_path(self, tmp_path):
    cfg = Config(tmp_path / "config.json")
    expected = tmp_path / "SingleWorks"
    cfg.set("single_work.download_path", str(expected))
    assert cfg.download_path == expected
```

- [ ] **步骤 2：确认失败**

运行：`.\venv\Scripts\python.exe -m pytest tests\test_config.py -v`

- [ ] **步骤 3：实现**

在 `DEFAULT_CONFIG["local"]` 后加入：

```python
"single_work": {
    "download_path": "",
    "recent_dirs": [],
    "default_template_id": "default",
    "templates": [{
        "id": "default",
        "name": "默认模板",
        "template": "{create_time} {author} {title}",
        "is_default": True,
        "created_at": "2026-08-16 00:00:00",
        "updated_at": "2026-08-16 00:00:00",
    }],
}
```

加入属性：

```python
@property
def single_work(self) -> dict:
    return self._data.get("single_work", {})
```

`download_path` 读取顺序改为：`single_work.download_path`、`local.download_path`、默认 `DoukHub/Download`。

- [ ] **步骤 4：验证并提交**

```powershell
.\venv\Scripts\python.exe -m pytest tests\test_config.py -v
git add app\core\config.py tests\test_config.py
git commit -m "feat: persist single-work preferences"
```

---

### 任务 3：偏好和模板 API

**文件：**

- 修改：`app/main.py`
- 测试：`tests/test_collection_api.py`

**接口：**

- 产出：`GET/PUT /api/collection/single-work/preferences`。
- 偏好结构：`download_path`、`recent_dirs`、`default_template_id`、`templates`。
- 模板结构：`id`、`name`、`template`、`is_default`、`created_at`、`updated_at`。

- [ ] **步骤 1：写失败测试**

新增 fixture 使用临时 `Config` 替换 `app_main.config`，并测试：

```python
response = client.put(
    "/api/collection/single-work/preferences",
    json={
        "download_path": str(download_dir),
        "recent_dirs": [str(download_dir), str(tmp_path)],
        "default_template_id": "archival",
        "templates": [{
            "id": "archival",
            "name": "归档",
            "template": "{create_time} {id} {title}",
            "is_default": True,
        }],
    },
)
assert response.status_code == 200
assert config.download_path == download_dir
assert config.single_work["recent_dirs"][0] == str(download_dir)
```

再测试 `../{title}` 返回 400 和现有错误文案。

- [ ] **步骤 2：实现**

在 `app/main.py` 中：

1. 定义模板字段集合：`create_time`、`author`、`title`、`id`、`type`、`platform`。
2. 扩展 `_is_unsafe_filename_template`：拒绝路径字符、绝对路径、未知格式字段和格式异常。
3. 新增 `_single_work_preferences()`，补齐默认值并同步 `is_default`。
4. 新增 `_save_single_work_preferences(data)`：
   - 去重并最多保留 10 个最近目录。
   - 模板名不能为空。
   - 至少保留一个模板。
   - 新模板生成 `tpl_时间戳` ID。
   - 默认模板 ID 必须存在。
   - 写回 `config.set("single_work", prefs)` 并 `config.save()`。

核心保存代码：

```python
config.set("single_work", prefs)
config.save()
return _single_work_preferences()
```

路由：

```python
@app.get("/api/collection/single-work/preferences")
async def api_get_single_work_preferences():
    return {"preferences": _single_work_preferences()}


@app.put("/api/collection/single-work/preferences")
async def api_save_single_work_preferences(request: Request):
    try:
        prefs = _save_single_work_preferences(await request.json())
    except ValueError as error:
        return JSONResponse({"success": False, "message": str(error)}, status_code=400)
    return {"success": True, "message": "单作品偏好已保存", "preferences": prefs}
```

- [ ] **步骤 3：验证并提交**

```powershell
.\venv\Scripts\python.exe -m pytest tests\test_collection_api.py -v
git add app\main.py tests\test_collection_api.py
git commit -m "feat: add single-work preference APIs"
```

---

### 任务 4：单作品下载历史表

**文件：**

- 修改：`app/core/database.py`
- 测试：`tests/test_collection_batches.py`

**接口：**

- 产出表：`single_work_history`。
- 字段：`id`、`work_id`、`source_link`、`platform`、`work_type`、`title`、`author`、`filename_template`、`filename_override`、`target_dir`、`files_json`、`request_json`、`status`、`error`、`work_json`、`created_at`、`updated_at`。
- 方法：`create_single_work_history`、`get_single_work_history`、`list_single_work_history`、`update_single_work_history`。

- [ ] **步骤 1：写失败测试**

测试创建、更新、读取、按时间倒序列表，以及未知字段更新返回 `False`。

- [ ] **步骤 2：实现表**

在 `collection_batch_items` 后创建表：

```sql
CREATE TABLE IF NOT EXISTS single_work_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    work_id TEXT,
    source_link TEXT,
    platform TEXT,
    work_type TEXT,
    title TEXT,
    author TEXT,
    filename_template TEXT,
    filename_override TEXT,
    target_dir TEXT,
    files_json TEXT NOT NULL DEFAULT '[]',
    request_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'running',
    error TEXT,
    work_json TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
)
```

索引：

```sql
CREATE INDEX IF NOT EXISTS idx_single_work_history_created
ON single_work_history(created_at)
```

- [ ] **步骤 3：实现仓储方法**

更新字段白名单只允许业务字段和 `status/error/files_json/work_json` 等列；每次更新自动写 `updated_at`。列表查询：

```sql
SELECT * FROM single_work_history
ORDER BY created_at DESC, id DESC LIMIT ?
```

- [ ] **步骤 4：验证并提交**

```powershell
.\venv\Scripts\python.exe -m pytest tests\test_collection_batches.py -v
git add app\core\database.py tests\test_collection_batches.py
git commit -m "feat: store single-work download history"
```

---

### 任务 5：下载 API 集成历史、资产选择和重试

**文件：**

- 修改：`app/main.py`
- 测试：`tests/test_collection_api.py`

**接口：**

- 请求模型扩展：

```python
class SingleWorkDownloadRequest(BaseModel):
    links: str
    target_dir: str
    filename_template: str = "{create_time} {author} {title}"
    filename_overrides: dict[str, str] = Field(default_factory=dict)
    asset_indexes: list[int] = Field(default_factory=list)
    include_music: bool = False
    include_static_cover: bool = False
    include_dynamic_cover: bool = False
```

- 新路由：
  - `GET /api/collection/works/history`
  - `POST /api/collection/works/history/{history_id}/retry`

- [ ] **步骤 1：写失败测试**

测试点：

1. 下载图集第 2 个资产时传入 `asset_indexes=[2]`。
2. 作品 ID 命中 `filename_overrides`。
3. 成功后写历史并返回 `history_id`。
4. 失败记录错误。
5. 重试读取旧历史、复用目录和模板，并生成新历史行。

- [ ] **步骤 2：实现下载记录函数**

新增 `_download_single_work_and_record(...)`，参数包含 TTD client、链接、平台、目录、模板、覆盖名、资产序号、可选资产开关、旧历史和可选已解析 work。逻辑：

1. 创建 `running` 历史。
2. 未传入 work 时调用 `fetch_work`。
3. 调用任务 1 的 `download_work`。
4. 成功更新 `status=success`、`files_json`、`work_json`。
5. 失败更新 `status=failed`、`error`。
6. 返回现有下载结果结构并附带 `history_id`。

`request_json` 保存：

```python
{
    "filename_template": template,
    "filename_override": filename_override,
    "asset_indexes": asset_indexes or [],
    "include_music": include_music,
    "include_static_cover": include_static_cover,
    "include_dynamic_cover": include_dynamic_cover
}
```

- [ ] **步骤 3：改造下载路由**

路由必须先校验模板和目录，再解析作品。每个作品先 `fetch_work`，按作品 ID 取覆盖名，再调用记录函数，避免同一次请求重复解析。解析或下载异常也要写入失败历史。

- [ ] **步骤 4：实现历史和重试路由**

重试请求模型：

```python
class SingleWorkRetryRequest(BaseModel):
    target_dir: str = ""
    filename_template: str = ""
    filename_override: str | None = None
    asset_indexes: list[int] | None = None
```

重试规则：

1. 读取旧历史，不存在返回 404。
2. 从 `source_link` 提取平台。
3. 目录优先级：请求参数、旧历史、当前默认目录。
4. 模板优先级：请求参数、旧历史、默认模板。
5. 覆盖名和资产序号优先级：请求参数、旧 `request_json`、空值。
6. 生成新历史行，不覆盖旧失败记录。

- [ ] **步骤 5：验证并提交**

```powershell
.\venv\Scripts\python.exe -m pytest tests\test_collection_api.py -v
git add app\main.py tests\test_collection_api.py
git commit -m "feat: integrate single-work history and retry"
```

---

### 任务 6：单作品页面完整 UI

**文件：**

- 修改：`app/templates/collect_detail.html`
- 修改：`app/static/css/style.css`
- 修改：`app/main.py`
- 测试：`tests/test_collection_api.py`
- 测试：`tests/test_workflow_ui.py`

**接口：**

- 页面上下文新增 `single_work_preferences`。
- 页面包含：最近目录、模板选择、模板管理弹层、拖拽模板构建器、资产表、单资产下载、覆盖文件名、下载历史、失败重试。

- [ ] **步骤 1：写失败页面测试**

在 `tests/test_collection_api.py`：

```python
def test_collect_detail_page_contains_asset_template_and_history_controls():
    source = Path("app/templates/collect_detail.html").read_text(encoding="utf-8")
    for token in (
        'id="single-work-list"',
        'id="single-history-list"',
        'id="template-modal"',
        'id="template-parts"',
        "downloadSingleAsset",
        "retrySingleWorkHistory",
        "dragSingleTemplatePart",
    ):
        assert token in source
```

在 `tests/test_workflow_ui.py` 的采集详情测试中断言页面包含“单作品采集”“命名模板”“下载历史”。

- [ ] **步骤 2：传递配置**

`/collect/detail` 上下文加入：

```python
"single_work_preferences": _single_work_preferences()
```

模板中放 JSON：

```html
<script type="application/json" id="single-work-preferences">
{{ single_work_preferences | tojson }}
</script>
```

- [ ] **步骤 3：实现表单区**

保存目录旁增加最近目录 `select`。命名模板改为模板 `select`、管理按钮、预览文本和隐藏模板输入。

表单下方增加下载历史面板。模板管理使用现有 modal/workflow 样式，包含：

- 模板库选择。
- 模板名称。
- 可拖字段：发布时间、作者、标题、作品 ID、类型、平台。
- 分隔文本输入和添加按钮。
- 已排序字段列表。
- 实时预览。
- 保存模板、使用此模板。

- [ ] **步骤 4：实现状态和模板逻辑**

JavaScript 状态：

```javascript
var singleWorkState = {
    works: [],
    preferences: JSON.parse(document.getElementById('single-work-preferences').textContent),
    selectedTemplateId: '',
    templateParts: [],
    draggedTemplateIndex: null
};
```

必须实现：

- `parseSingleTemplate(template)`：把模板拆成 field/text parts。
- `templatePartsToString()`：parts 还原模板。
- `renderTemplateBuilder()`。
- `dragSingleField(event, key)`。
- `dragSingleTemplatePart(event, index)`。
- `dropSingleTemplatePart(event, index)`。
- `appendTemplateField(key)`。
- `appendTemplateSeparator()`。
- `saveSingleTemplate()`。
- `saveSinglePreferences()`。
- `buildFilenamePreview(template, work)`。

拖拽实现要求：

1. 字段按钮可拖入序列末尾，也可点击追加。
2. 已有序列项可互相拖拽排序。
3. 每次 drop 后重新渲染和预览。
4. 非 JSON 模板内容拖入时忽略，不报错。

- [ ] **步骤 5：实现资产表**

解析结果按作品分组，每个作品显示：

- 标题、作者、类型、资产数。
- 覆盖文件名输入框，`data-filename-work` 使用作品 ID。
- 资产表：序号、类型、URL、复制、打开、下载。

函数：

- `renderSingleWorks(works)`。
- `assetKindLabel(kind)`。
- `copyAssetUrl(url)`。
- `downloadSingleAsset(link, assetIndex)`。
- `downloadSingleWorks(links, assetIndexes)`。

下载请求提交：

```javascript
{
    links: links.join('\n'),
    target_dir: form.get('target_dir'),
    filename_template: document.getElementById('single-template-input').value,
    filename_overrides: overrides,
    asset_indexes: assetIndexes
}
```

- [ ] **步骤 6：实现目录和历史**

函数：

- `useRecentSingleDir(path)`。
- `saveRecentSingleDir(path)`：最新在前、去重、最多 10 个。
- `loadSingleWorkHistory()`。
- `renderSingleWorkHistory(rows)`。
- `safeParseJsonArray(value)`。
- `retrySingleWorkHistory(historyId)`。

历史表显示作品、状态、文件或错误、时间、操作；失败行显示重试图标按钮。

- [ ] **步骤 7：样式**

新增类：

```css
.single-work-result { display:grid; gap:12px; padding:14px 0; border-bottom:1px solid var(--border-default); }
.single-work-summary { display:flex; align-items:baseline; justify-content:space-between; gap:12px; min-width:0; }
.single-work-summary strong, .asset-url { overflow-wrap:anywhere; }
.template-field-row, .template-part-list { display:flex; align-items:center; gap:8px; flex-wrap:wrap; min-height:40px; }
.template-part-list { padding:10px; border:1px dashed var(--border-default); border-radius:var(--radius-sm); }
.template-part { display:inline-flex; align-items:center; gap:6px; min-height:32px; padding:6px 10px; border:1px solid var(--border-default); border-radius:var(--radius-sm); background:var(--bg-muted); cursor:grab; }
```

- [ ] **步骤 8：验证并提交**

```powershell
.\venv\Scripts\python.exe -m pytest tests\test_collection_api.py tests\test_workflow_ui.py -v
git add app\main.py app\templates\collect_detail.html app\static\css\style.css tests\test_collection_api.py tests\test_workflow_ui.py
git commit -m "feat: build single-work collection UI"
```

---

### 任务 7：全量验证和交付

**文件：**

- 修改：`CHANGELOG.md`

- [ ] 运行全量测试：

```powershell
.\venv\Scripts\python.exe -m pytest -v
```

- [ ] 运行差异检查：

```powershell
git diff --check
git status --short
```

- [ ] 启动应用：

```powershell
.\venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

手工验收：

1. 解析视频、图集、实况链接。
2. 资产类型和顺序正确。
3. 复制、打开、单资产下载可用。
4. 拖拽模板字段并保存。
5. 覆盖文件名生效，扩展名正确。
6. 目录和最近目录持久化。
7. 下载历史正确显示文件路径。
8. 失败后可重试。
9. 无布局重叠、文本溢出、控制台错误。

- [ ] 在 `CHANGELOG.md` 未发布区加入：

```markdown
- 完善单作品采集闭环：支持视频、图集、实况、动图、单资产下载、文件名模板库、目录偏好、下载历史和失败重试。
```

- [ ] 提交：

```powershell
git add CHANGELOG.md
git commit -m "docs: document single-work collection"
```

---

## 给执行 AI 的额外提醒

- 以本计划为准实施第一阶段，不扩大到后续阶段。
- 每个任务先测试、再实现、再验证、再提交。
- 不要修改或回滚本计划外用户已有未提交改动。
- 真实 TTD 字段以 `TikTokDownloader/src/extract/extractor.py` 为准。
- 遇到实现与计划冲突时，优先保持接口名称和测试行为稳定，并在提交说明中记录差异。
