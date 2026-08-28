# EntHub 架构与偏好复用手册

> 来源项目：`D:\AI\EntHub`（Flask + htmx + SQLite 本地工具型 Web 应用）
> 整理目的：提炼可在其他项目（尤其同类型本地工具型 Web 应用）复用的架构模式、可复用模块、个人偏好与使用习惯。
> 整理日期：2026-08-12

---

## 一、整体架构

### 进程模型：Flask 单进程 + 多辅助线程/进程

- Flask 主进程由 `app.run` 启动；后台模式关闭 reloader 避免双进程（`app.py:282-283` 用 `ENTHUB_BG` 环境变量区分）。
- **模板预热线程**：启动后 1 秒异步 fetch 关键页面，把 Jinja2 编译结果缓存到内存，避免用户首访 5–10s 延迟（`app.py:246-269` `_warmup_templates`）。`start.sh` 后台模式会等日志出现 `[预热] 完成` 再开浏览器。
- **menubar 是独立进程**，与 Flask 解耦，通过 PID 文件通信：`menubar.py` 用 `os.kill(pid, 0)` 探活。menubar 用 `rumps` + `AppKit` 实现 macOS 状态栏图标。
- **后台任务 worker 线程**：`tasks.py` 提供进程内任务跟踪（`queue` + `stop_event` + `status`），`import_flow.py` 用它跑异步导入。

### 目录划分逻辑

```
EntHub/
  app.py              # 入口、Blueprint 注册、lifespan/启动流程
  bootstrap.py        # 引导配置 + 旧架构自动迁移
  config.py           # 配置管理（存 DB settings 表）
  db.py               # 数据库层（sqlite3 原生，无 ORM）
  queries.py          # 共享查询层（收敛重复 SQL + 缓存）
  utils.py            # 规范化工具 + 列名映射
  data_helpers.py     # 数据处理 helpers（sync/merge 成对模式）
  toast.py            # macOS 原生通知（PyObjC）
  tasks.py            # 进程内异步任务跟踪
  menubar.py          # macOS 状态栏进程（rumps）
  routes/             # 路由包，按"功能流"拆分
    __init__.py       # register_blueprints(app) 统一注册
    _base.py          # make_bp 工厂（实际各模块未用，形同虚设）
    pages.py          # 页面路由
    companies.py      # 企业 CRUD
    import_flow.py    # 导入流（流式 openpyxl）
    backup_flow.py    # 备份流
    cleanup_flow.py   # 清理流
    ...
  templates/          # Jinja2 模板（_xxx.html 为片段）
  static/             # 本地化的 htmx.min.js / lucide.min.js / style.css
  EntHub.app/         # macOS .app bundle（双击启动）
  start.sh            # 一键启动脚本
```

### routes 包拆分逻辑：按"功能流"而非纯业务域

- `routes/__init__.py` `register_blueprints(app)` 统一注册 12 个蓝图。
- 命名规律：`xxx_flow` 后缀 = 一条完整业务流程（导入流、备份流、清理流）；单数名词 = 一类实体 CRUD（companies、tags）。
- 每个模块内部 `bp = Blueprint('xxx_bp', __name__)`。
- **共享能力不在 _base.py**：真正的共享在 `queries.py`（查询层）和 `utils.py/data_helpers.py`（工具层），通过模块函数而非基类实现共享。

---

## 二、数据访问层（db.py）

- **连接**：sqlite3 原生，无 ORM。`get_db()` 设 `row_factory = sqlite3.Row`，开 `WAL` + `foreign_keys=ON`。每请求绑定到 `g.db`，`teardown_request` 关闭。
- **查询风格**：统一 `conn.execute(sql, [params]).fetchall/fetchone()`，按列名访问（`r["normalized_phone"]`），偶尔 `dict(row)` 转 dict。**没有分层 DAO**：SQL 散落在 routes 各模块，共享部分被 `queries.py` 抽出。
- **事务**：手动 `conn.commit()`，无 context manager。批量导入 `COMMIT_EVERY = 1000` 分批提交。
- **迁移机制**：无版本化框架，靠 `CREATE TABLE IF NOT EXISTS` + ad-hoc 重建（重命名旧表 → 建新表 → INSERT 迁移 → DROP 旧表）。**无 `PRAGMA user_version`**。FTS5 全文索引 + 触发器同步（trigram 分词器支持中文）。
- **settings 表**：key-value 存储，整个 config JSON 存在 `key='config'` 一行。
- **缓存**：`queries.py` 有进程内 TTL 缓存（`_CACHE` dict + 时间戳），仅缓存"变化少、计算贵"的数据（如筛选下拉值）。`invalidate_cache()` 在写操作后主动清空。

---

## 三、前端架构

### htmx：页内局部刷新

- 典型用法：`hx-get="{{ url_for(...) }}" hx-target="#container" hx-swap="innerHTML"`。
- 分页、tab 切换均走 htmx 局部刷新。
- SPA 切换后重扫新内容：`htmx.process(newMain)`。

### 片段模板（_xxx.html）：核心复用模式

- `_browse_data.html`（浏览数据片段，由 `/browse/data` 端点返回）、`_modal.html`（全局弹窗）、`_relation_groups.html`、`_company_form.html`。
- **骨架 + 异步数据**模式：`pages.py` `browse()` 只渲染筛选器骨架，数据由独立 `/browse/data` htmx 异步填充，首屏 < 100ms。

### Lucide 图标：本地化加载

- `static/lucide.min.js`（本地文件）在 `base.html` 加载。写法 `<i data-lucide="search">`，`lucide.createIcons()` 渲染。每次 SPA/htmx 刷新后都重调 createIcons。

### CSS 变量与主题：单主题，完整语义变量

- `:root` 定义 ~20 个语义变量：`--bg / --surface / --text / --text-secondary / --text-muted / --border / --accent / --danger / --warning / --success / --radius / --font`。
- 暖色调赤陶主题（`--accent: #D97757`），无暗色主题切换。

### SPA 化 vs 整页刷新：两者混合（重点）

- **自研轻量 SPA Router**（`base.html` `loadPage`）：拦截 `.nav-link` 点击 → `fetch` 整页 → `DOMParser` 解析 → 只替换 `#main-content` 和 `#page-scripts` → 重初始化 lucide/htmx/inline script。导航不刷新，体感秒切。约 70 行 JS，无前端框架。
- 页内交互（分页、tab、表格）走 htmx 局部刷新。
- 重定向/错误回退整页导航。

### 表单与弹窗：统一组件，禁用原生

- `_modal.html` 提供 `showModal / showConfirm / showInput / confirmSubmit / confirmLink`。
- DESIGN_GUIDE 立硬规则：**禁止用 `alert/confirm/prompt`**，必须用页面内弹窗（含替换对照表）。

---

## 四、配置管理（config.py）

- **两层配置**：
  - 引导配置（数据位置）：`bootstrap.json`，只含 `db_path` + `backup_dir`。
  - 业务配置（API 密钥、webhook、密码）：存数据库 `settings` 表 `key='config'`，value 是 JSON 字符串。
- **默认值与字段补全**：`DEFAULT_PROVIDERS` / `DEFAULT_LLM` 等字典；读取时遍历默认 dict 补全缺失字段。
- **旧架构自动迁移**：`bootstrap.py` `ensure_bootstrap()` 首次启动自动从旧位置迁移 DB（含 WAL/SHM sidecar）+ 旧 config.json 提取 `backup_dir`。
- **运行时修改**：`save_config` 用 `INSERT ... ON CONFLICT(key) DO UPDATE` 写回，写完即生效，无需重启。menubar 进程直接读写 settings 表，实现 web 与状态栏跨进程共享配置。
- **敏感信息**：访问密码用 werkzeug `generate_password_hash`，API key 存明文（业务调用需要）。

---

## 五、可复用模块清单

| 文件 | 函数/模式 | 职责 | 通用性 |
|---|---|---|---|
| `utils.py` | `clean_val(val, field)` | NaN/None/'-' 转空串，整数字段去 .0 后缀 | 半通用 |
| `utils.py` | `extract_date_from_filename(fn)` / `get_file_date(fp)` | 文件名提取日期，回退 mtime | **通用** |
| `queries.py` | `_CACHE/_cache_get/_cache_set/invalidate_cache` | 进程内 TTL 缓存 + 主动失效 | **通用** |
| `queries.py` | `ALLOWED_SORTS` 白名单 | 排序字段白名单防 SQL 注入 | **模式可复用** |
| `tasks.py` | `create/get/pop/set_status/request_stop` | 进程内异步任务跟踪 | **通用** |
| `bootstrap.py` | `get_bootstrap / ensure_bootstrap` | 引导配置 + 旧架构自动迁移 | **模式可复用** |
| `base.html` | `loadPage(url)` SPA Router | fetch 整页 → DOMParser 局部替换 → 重初始化 | **可直接搬** |
| `base.html` | `_enthubToast(type,title,msg)` | 右上角 toast 通知 | **可直接搬** |
| `_modal.html` | `showConfirm/confirmSubmit/confirmLink` | 统一弹窗替换原生 confirm | **可直接搬** |
| `app.py` | `filesize_filter` | 自适应 B/KB/MB/GB 格式化 | **通用** |
| `app.py` | `_warmup_templates` | 后台线程预热模板编译 | **模式可复用** |
| `data_helpers.py` | `sync_* / merge_*` 成对模式 | 全量重建 vs 增量合并的成对函数 | **模式可复用** |
| `toast.py` | `show_toast` | macOS 原生半透明圆角浮窗（PyObjC） | 通用（仅 macOS） |

---

## 六、个人偏好与使用习惯

### 命名习惯
- 英文函数名 + snake_case；表名/字段名英文。
- 中文出现在：UI 文案、中文注释/docstring、列名别名表的中文 key。

### 错误处理：细粒度 try/except + 多渠道反馈
- 分层捕获：`HTTPError` / `URLError` / `Exception` 分开，给不同提示（服务返回错误 vs 服务未运行 vs 调用失败）。
- 多渠道反馈：失败时同时走 toast + 系统通知 + 菜单标题变 + 终端日志。
- 用户提示用 `flash(..., "success/error/info")` + 重定向，或 `_enthubToast`。

### 注释/文档习惯
- 中文 docstring + 行内中文注释。用 `# ── 标题 ───` 分隔区块。
- 文件头 docstring 说明用途 + 迁移历史。
- 关键设计决策写进注释（解释"为什么"）。

### 函数粒度：小函数，单一职责
- sync/merge 拆成独立函数；`loadPage` 步骤清晰分块但保持单函数。

### 前端资源加载偏好：本地化优先，不用 CDN
- `htmx.min.js` + `lucide.min.js` 都放 `static/`，用 `url_for('static')` 加载。
- 字体用系统栈（`-apple-system, BlinkMacSystemFont, "PingFang SC"...`），不加载 web font。

### 整页刷新 vs 局部刷新：两者都爱用
- 导航走 SPA 局部替换（`loadPage`）。
- 页内交互走 htmx 局部刷新。
- 重定向/错误回退整页。

### 防御性编程倾向：明显
- 排序字段白名单防注入。
- 路径穿越检查 `filepath.resolve().is_relative_to(backup_dir.resolve())`。
- 临时文件白名单 `_is_temp_safe`（源于真实事故，见 AGENTS.md 第 1 条）。
- **空值不覆盖已有数据**（AGENTS.md 第 3 条，UPDATE 只写非空且不同的字段）。
- 备份恢复前校验文件是合法 SQLite。

### 配置项粒度：粗粒度 JSON + 字段补全
- 整个 config 存一行 JSON，读改都要 load→改→save 整个 dict。迁移简单，但并发写可能互相覆盖。

### 文档习惯：四件套各司其职
- `README.md`：产品介绍 + 快速启动 + 功能说明 + API 列表 + 数据模型 + 项目结构。
- `DESIGN_GUIDE.md`：视觉/交互规范，含硬规则（禁原生弹窗）+ 尺寸/颜色/间距数值表。
- `ROADMAP.md`：演进路线 + 方案对比表 + 决策记录。
- `CHANGELOG.md`：按版本/日期记，含破坏性变更、涉及文件表、数据迁移 SQL。
- `AGENTS.md`：工程操作纪律，含事故记录和"为什么"。
- `docs/` 放专题文档。

### 测试习惯：轻量
- `tests/` 聚焦核心解析逻辑，量级不大。

### 启动/部署习惯：bash 脚本 + .app bundle + 状态栏进程
- `start.sh` 一键脚本：前台/后台模式、venv 自愈（缺失依赖补装）、端口占用处理、等预热完成再开浏览器。
- `EntHub.app/` bundle 双击启动。
- `menubar.py` 状态栏进程，rumps 实现，开机自启用 LaunchAgent plist。
- 关键经验：LaunchAgent 必须用 `open .app` 而非直接跑 shell，否则 rumps 图标不显示。

### 工程纪律（AGENTS.md 摘要）
1. 数据文件只读，绝不在原地操作；测试只碰临时副本。
2. 流式导入不退回全量加载。
3. 空值永远不覆盖已有数据。
4. 被质疑时先查证（时间戳/命令记录/代码逻辑），别急着下结论。
5. 改动前先看现状，不臆测结构；不回退用户未提交改动。

---

## 七、作者风格关键词

**本地化优先 · 防御性编程 · 多渠道反馈 · 中文注释+英文命名 · 文档四件套 · 事故驱动纪律 · 小函数单一职责 · 骨架+异步数据 · SPA+htmx 混合刷新**

---

## 八、复用时的取舍

**建议照搬**：轻量 SPA Router（loadPage）· 本地资源加载 · routes 包+Blueprint 拆分 · _modal.html 统一弹窗 · TTL 缓存+invalidate 模式 · bootstrap 引导+迁移模式 · 文档四件套 · AGENTS 工程纪律。

**不建议照搬**：单主题（多主题项目应保留多主题，但可借鉴语义变量命名）· 无版本化迁移框架（schema 变更频繁应上 Alembic/Flyway）· macOS 专属部分（toast.py/menubar.py/rumps/LaunchAgent）· 粗粒度 JSON 配置（并发写场景需加锁或拆细）。
