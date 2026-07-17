# DoukHub 更新日志

## 2026-07-17 云端同步 v2 重构（方案 A 稳定版 v2.1.0-sync-a）

### 同步架构重写

完整重写 `feishu_sync.py`，引入「字段级单向同步」（方案 A）。

**核心改动：**
- **字段命名规范**：业务字段中文（与飞书 100% 一致），系统字段英文（`record_id`/`is_deleted`/`synced`/`created_at`）
- **冲突解决**：按字段归属决定方向（人工字段飞书赢，API 字段本地赢），详见 [SYNC_DESIGN.md](./SYNC_DESIGN.md)
- **业务唯一键去重**：采集表=分享码，账号表=sec_user_id，Cookie表=Cookie
- **删除同步**：飞书端直接删除 + 本地端软删除墓碑，三层安全保护
- **路由简化**：从 6 个简化为 2 个（`/api/feishu/sync` 增量、`/api/feishu/sync/full` 全盘）
- **启动时自动增量同步**：FastAPI lifespan 后台触发

**修复的 bug：**
- 飞书端删除记录被本地推回去恢复（关键修复）
- 飞书端重复业务键导致 record_id 反复横跳
- 比例保护/空结果保护触发时 synced=1 孤儿被推回飞书
- `result.update()` 覆盖前面步骤的 failed/errors
- 飞书表字段「Share→分享码」「同步状态→已同步」自动重命名

**测试：** 195 个测试通过（含 60 个同步逻辑测试）

**已知限制（方案 B 将解决）：**
- 不是真正的双向同步，在错的端修改会被覆盖
- 详见 [SYNC_DESIGN.md](./SYNC_DESIGN.md)

### 字段归属规则

- ☁️ 云端赢：等级、标签、备注、启用、采集类型、账号名称等（在云端修改有效）
- 💻 本地赢：sec_user_id、粉丝数、作品数、昵称等（在本地修改有效）
- 🔒 元数据：分享码、平台、Cookie 字符串（创建后不变）

## 2026-07-11 同步流程重构 + 飞书自动集成

### 交互重构

- **步骤重命名**：三步同步按钮从"快速同步/更新采集表/同步账号表"改为更自解释的"导入账号/解析链接/获取详情"。一键同步改为"一键执行"。
- **飞书按钮精简**：删除了同步页面原有的三个飞书按钮（本地→飞书、飞书→本地、双向同步），替换为一个"双向校准"按钮，放在步骤区底部，用于手动纠偏。

### 飞书自动集成（syncer_v2.py）

- **步骤1 自动拉取**：`import_to_collection()` 结束后自动调用 `FeishuSyncer.sync_collection_from_feishu()`，把飞书采集表的新增记录拉到本地 DB，与文本导入的记录合并去重。飞书拉取失败不影响文本导入结果。
- **步骤3 自动回写**：`sync_to_account()` 结束后自动调用 `FeishuSyncer.sync_account_to_feishu()`，把获取到的账号详情（昵称、粉丝数等）推回飞书账号表。SSE 流中会实时报告"正在回写飞书..."和回写结果。回写失败不影响本地同步结果。
- **飞书同步保留手动入口**：`/api/feishu/sync/all` 端点不变，前端"双向校准"按钮调用它做全量对齐。

### 设计原则

用户只点一次"一键执行"，数据就在本地 DB 和飞书表之间自动流转完毕。手动校准按钮仅用于数据不一致时的纠偏，不是日常操作。

---

## 2026-07-11 综合审查与优化

### 代码修复

- **collector.py**：`resolve_short_url` 方法引用了未定义的 `logger`，异常路径会抛 `NameError`。改为使用模块级 `_logger`（`import logging` + `logging.getLogger("doukhub.collector")`）。该 bug 在短链接解析失败时会吞掉原始异常，导致同步步骤 2 静默失败。
- **syncer_v2.py**：`merge_tags` 方法把所有标签强制转小写，违反文档约定（"大小写不敏感，保留原样"）。改为以小写做去重 key、保留原始大小写。例如 "COS" 不再被降为 "cos"。
- **Dockerfile**：`EXPOSE 8080` 与 `main.py` 实际监听的 2999 不一致。改为 `EXPOSE 2999`；同时在 Docker 构建阶段安装 pytest 便于 CI。
- **tests/test_api.py**：`test_browse_dir_root` 在多盘符 Windows 上断言 `current == Path.home()`，但多盘符时 API 先返回驱动器列表（current 为空），导致 79 项测试中 1 项失败。放宽断言为仅检查返回结构合法。

### UI 移动端适配

- **base.html**：完整重写响应式布局。768px 以下侧边栏变为抽屉式（汉堡菜单 + 遮罩），统计卡片 4 列降为 2 列，状态卡片垂直堆叠，按钮全宽，Toast 占满宽度。420px 以下进一步降为单列。所有中文内容修复为正确的 UTF-8（之前 PowerShell 写入导致 `??` 乱码）。
- **status.html**：内核检测卡片在移动端垂直堆叠，操作按钮居中换行。修复全部中文乱码。
- **collect.html**：采集方式选择网格在移动端降为单列，评级筛选项换行，进度统计 4 列降为 2 列，定时任务表格添加横向滚动容器，新建任务弹窗限制 `max-width:90vw` 防溢出。
- **sync.html**：同步进度统计 4 列在移动端降为 2 列，文本域 font-size 提升到 16px 避免 iOS 自动缩放。
- **history.html**：表格包裹横向滚动容器。
- **database.html**：统计概览 5 列在移动端降为 2 列，表切换按钮栏支持横向滚动，搜索栏 flex-wrap。

### 文档与代码一致性

- **DESIGN.md vs 实际代码**存在多处偏差（DESIGN.md 是 v1.0 设计稿，代码已迭代到 v2 架构）。主要差异：
  - 配置文件路径：DESIGN.md 写的是 `config/doukhub.json`，实际代码用 `~/.doukhub/config.json`（三层架构：工具层/配置层/内核层）。
  - DESIGN.md 提到飞书"全局配置表"，实际代码已废弃，全局配置改用 JSON 文件。
  - DESIGN.md §8 的 API 清单缺少 v2 端点（`/api/sync/v2/*`、`/api/kernels/*`、`/api/database/*`、`/api/collect/v2/*`）。
  - DESIGN.md §9.1 的 `config_table_id` 在实际 Config 中已不存在。
  - DEVELOPMENT.md 中的三层架构、5 表 SQLite、SSE 进度等描述与代码一致，是当前权威文档。
- **建议**：DESIGN.md 标注为 "v1.0 设计稿（历史归档）"，以 DEVELOPMENT.md 为准。

### 测试验证

- 79 个单元测试全部通过（`pytest tests/`）。
- 6 个页面（status/sync/collect/history/database/settings）全部 HTTP 200，中文渲染正常，无乱码。
- `/api/status` 正确返回 feishu/ttd/xhs 连通性 + kernels 安装状态 + services + stats + jobs。

---

## 2026-07-10 内核检测与状态修复

### 新增功能

- **内核检测 UI**：状态页面新增"内核检测"卡片。TikTokDownloader / XHS-Downloader 源码未安装时显示警告 + "从 GitHub 下载"按钮，点击后自动 `git clone --depth 1` 并安装依赖。
- **API 端点**：`GET /api/kernels/status`、`POST /api/kernels/{name}/install`。
- **ServiceManager.install()**：从 GitHub 克隆内核源码 + 安装 requirements.txt。
- **DownloaderService.source_exists**：检测 `main.py` 是否存在判断内核是否已安装。

### Bug 修复

- **重复路由**：`/api/status` 在 main.py 中定义了两次，FastAPI 匹配第一个（只返回 services/stats/jobs），前端拿不到 feishu/ttd/xhs 字段导致状态卡片永远停在"检测中"。删除重复定义，合并为统一的 status 端点。
- **一键更新 undefined**：`ServiceManager.update()` 返回值缺少 `name` 字段，前端模板 `${r.name}` 显示 `undefined`。所有返回值补齐 `name`，并在内核未安装时给出明确提示。
- **start.bat 乱码**：PowerShell here-string 写入时中文变 `??`。改为纯 ASCII 英文 + 直接调用 `venv\Scripts\python.exe main.py`（绕开 activate 不生效导致加载全局 fastapi 的问题）。

---

## 2026-07-08 ~ 09 初始版本

- 项目骨架：FastAPI + Jinja2 + HTMX + APScheduler。
- 飞书 API 交互模块（httpx 封装，无 SDK 依赖）。
- 三步同步流程：导入采集表 → 更新采集表（获取 sec_user_id）→ 同步账号表。
- SQLite 本地数据库（5 张表，中文字段名）。
- SSE 实时进度 + 停止按钮（AbortController）。
- Cookie 轮换管理（随机/顺序，使用上限）。
- 定时任务（APScheduler + 数据库持久化）。
- 数据库管理界面（查看/删除/清空/搜索）。
 - 飞书双向同步（本地 ↔ 飞书）。
