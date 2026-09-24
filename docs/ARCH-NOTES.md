# DoukHub 架构笔记

> 从 `.workbuddy/memory/MEMORY.md` 抽出的实现细节。MEMORY.md 只留红线与索引，**具体字段名、函数名、
> 代码位置、操作步骤**看这里。

## 数据与配置

- **`sp.save_state` 联动**：保存方案时按主方案自动回填 `local.download_path`（取 batch 主）与
  `single_work.download_path`（取 single 主）。
- **备份触发点**：`main.py:253`（启动时距上次 >24h 则跑一次）。
- **备份目录可自定义**：`backup.dir`（空 = 数据根下 `backups/`）。`POST /api/backup/set-dir` 会校验
  绝对路径 + 建目录 + 试写；入口在**设置 → 应用数据目录 → 第二节**（`settings.html`，复用
  `partials/directory_picker.html`）。切换目录不自动搬移旧备份，但返回消息会说明旧目录还剩几份。
- **核对设置表单覆盖面**：比对表单 `name` 清单 vs `config._data` 各顶层键。
- **存储方案 API**：`PUT /api/collection/storage`。

## 表浏览（/table）实现细节

- **AG Grid 31.3.4 社区版**，位于 `app/static/vendor/ag-grid/`（1.80MB）。
- **Infinite Row Model 社区版可用**（无需自研虚拟滚动）。已落地 `rowModelType:'infinite'` + 自研 datasource：
  100 行/块 + 预取 + `_dsSeq` 竞态保护；分页条改为加载状态条。
- 后端 `query_table` 支持 `search_field`（逗号分隔）与 `need_total`；distinct 带 TTL 缓存（写入失效）；
  schema 前端缓存。
- `animateRows` 已关、`undoRedoCellEditing` 已移除。
- 主题已有 `--ag-*` → `--dh-*` 映射，明暗自动跟随。

## Git 恢复（仓库损坏时）

若 stash 等问题导致 .git 对象库损坏（表现为 refs/reflog 指向丢失对象）：

1. `mv .git` 留残骸
2. `git init -b main`
3. `git remote add origin <url>` → `git fetch`
4. 手写 `refs/remotes/origin/main`
5. `git reset --mixed origin/main`（工作区文件不动）

## 版本维护

版本号在 `app/main.py:307`（`FastAPI(version="x.y.z")`）与 `CHANGELOG.md` 两处维护，**必须同步更新**。

## 运行时机制

### 托盘自动重启（`tray.py`）
- `_watch_files()` 轮询 `app/` 下 `.py` 的 mtime，变化即杀进程树 + 重启，**含任务闸**：先读 `/api/tasks`，
  有 pending/running 则挂起、每 3 秒复查，任务一结束立即执行。
  ⚠️ **`tray.py` 自身不在监控范围，改动需手动重启托盘**。监控不含 `.html`（模板每次请求重渲染，刷新即生效）。
- **重启后恢复矩阵**：增量采集批次 ✅（`recover_interrupted_batches` + `kick_resume`）；单作品下载 ✅
  （`download_worker.recover` 重新入队）；**同步 v2 四步 ❌**（`TaskManager._tasks` 纯内存，重启后任务面板归零、
  不自动重跑）。但四步进度不丢——每处理完一条立即写库，取数条件天然构成断点续跑。

### 回环请求必须禁代理（关键坑）
- **所有访问本机下载器的 httpx client 必须 `trust_env=False`**（`collector._client` + main.py 的 5 处预检/健康检查
  + 单作品 client）。否则继承系统代理后回环请求被改写成 absolute-form `POST http%3A//127.0.0.1%3A5555/...`
  → TTD 路由 404 → 上层误判"服务故障"→ 退避冷却 → **表现为卡死且"有时行有时不行"**。
  对照实验：`trust_env=False` 5/5 成功，默认继承 1/5。例外：单作品 client 注释写明"将来挂代理下载海外平台
  （TikTok）时这一处需单独放开"。
- httpx 的 `trust_env` **不读** Windows 的 `ProxyOverride` 排除名单。助手侧代理 = `sandbox-cli.exe`
  监听 127.0.0.1:5758 注入 `HTTP_PROXY`；**用户机器本身无代理**。

### 抖音直连 API（`app/core/douyin_api.py`）
- **必须用 curl_cffi 模拟 Chrome TLS 指纹**（`impersonate="chrome146"`，与 TTD 一致），httpx 会被 403。
- **Argus 风控要求 `uifid`**：URL 参数与 Header 都要带（从 Cookie 提取），否则
  `403 Blocked by ArgusSecurityPlugin Uifid Not Found`。Header 另需 `x-tt-argus: 1`。
- **`ABogus(user_agent)` 新版构造函数需要参数**；签名用 `ab.get_value(params_str)`。
- `_BASE_PARAMS_DICT` 的 `version_code/version_name` 要跟随抖音版本（当前 290100 / 29.1.0）。
- 已实现：`fetch_detail_direct`、`fetch_user_profile_direct`、`fetch_account_works_direct`（预留）。

### TTD 内核（TikTokDownloader）
- **本机采集走 TTD 子进程**。`downloader.py` 生成的 `_doukhub_launcher.py` 需 patch
  `rich.console.detect_legacy_windows = lambda: False`（避免 OSError）。
- **`source=True` 拿原始 API 数据**，不经 TTD 文件处理流程 —— 新版 `source=False` 需要 Volume 目录等配置，会 500。
- **TTD 5.8 的 `im/user/info` 被风控**（Cookie 有效也返回空）。`deal_account_detail` 内部 info=None 后会继续取
  作品列表并下载。因此 `ttd_batch_runner` patch 了 `Extractor.__select_item`：匹配 sec_uid 失败时返回第一个作品
  而非抛 `DownloaderError`（应对共创作品）。
- 账号资料优先走**直连**（~1s），回退 `/douyin/account`（~6s）。该端点返回**作品列表**，账号资料在
  `data[0].author` 里 —— 解析要兼容 list 与 dict 两种形态。
- 采集间隔：每个账号处理完都 `suspend`（不管成功失败），降低风控概率。
- 内核支持 git pull 更新（`/api/kernels/{name}/update`）：自动 stash → pull → stash pop，冲突则丢弃本地修改；
  所有 subprocess 走 `CREATE_NO_WINDOW` 隐藏 CMD 黑框。

### Cookie 管理
- **`get_cookie_list()`（main.py）**：优先库中启用的 Cookie，为空则回退 TTD `Volume/settings.json` 的
  `cookie`/`cookie_tiktok`。原 8 处各自读库的逻辑已统一走它。
- **`download_worker._pick_cookie()` 按 LRU 轮换**（`last_used_at` 最久优先），并回写 `record_cookie_usage`。
- **`cookie_cache` 表新增 `验证说明`**：失败原因写这里（截断 80 字），**不覆盖用户手填的「备注」**；通过则清空。
  表格中该列只读，用 `verifyNoteRenderer`（含"过期/超时/异常/失败/失效"→ 红）。
- `validate_cookie` 返回 **dict**（`{"status","message","nickname"}`），不再是 bool。抖音优先直连验证，
  TikTok 回退 TTD。调用方按 dict 取 `status == "valid"`。

### 增量采集 Fail Fast（`collection_batch_manager.py`）
- 连续 **5 个**账号返回登录态类错误（`_LOGIN_FAIL_KEYWORDS`）→ 立即 `terminate()` TTD 进程中止批次，
  标记该 Cookie 失效（`状态=失效, 启用=0`），并在 `collection_batches.message` 写明原因。
- 若还有其他可用 Cookie 且未重试过 → 自动换 Cookie 重跑（failed/pending/running 全重置为 pending），
  用 `filter_data["retried"]` 保证只换一次。已在「失败账号自动重试一轮」判定里排除 Fail Fast 场景。
- **计数在 item 查找之前**（sec_user_id 不匹配时也要计上）；**只对登录态类错误计数**，避免 API 风控临时失败误触发。

## 磁盘空间接口（2026-09-20 新增）

- **`GET /api/disk`（`app/main.py`，紧跟 `/api/stats`）**：
  统计范围 = **「存储方案」**（`config.storage_profiles` 的 `batch` + `single` 两区里
  `enabled=True` 且有 `path` 的方案目录）**＋ 应用数据目录** `app_data_dir` 所在卷（2026-09-20 定稿）。
  返回 `{drives:[{name,label,paths,path,total_gb,used_gb,free_gb,writable,sources}], low_gb, any_low}`。
  - `writable` 语义 = **是否参与 5GB 转红告警**，由来源决定：被存储方案用到 → `True`；
    只被应用数据目录用到 → `False`（数据卷满不影响采集能否继续）；同卷两者都占 → `True`（OR）。
  - 应用数据目录条目**排在最后**（主次：先列采集真正会写进去的盘）。
  - 同一卷只报一次；`label` 按方案名归并为 `本机F（增量采集 / 单作品）`；
    `paths` 是该卷关联的目录（去重）；`sources` 保留 `[{scope,name,path}]` 明细。
  - 一个方案都没配时兜底用 `config.local.download_path`；网络盘/不可达盘跳过。
  - 禁用（`enabled=False`）与无路径（草稿）的方案**不计入**——它们当前不会被写入。
  - **实时读配置、无缓存**：每次请求重算 → 经 `PUT /api/collection/storage` 新增或停用方案后
    立即反映，**不需要重启服务**（实测：临时加含 D 盘的方案 → 立刻出现在列表；还原 → 立刻消失）。
- 阈值 `DISK_LOW_GB = 5`，**与采集启动前的空间检查同一口径**（不另立一套）。
- 前端：侧栏 footer 第四个图标（`#disk-badge`）常驻，**有 `writable=true` 的盘剩余 < 5GB** 时加
  `.is-low`（转红 + 「!」角标）；点击开 `#disk-panel-overlay` 详情面板（每次打开都重拉接口，不缓存）。
  面板里 `writable=false` 的行右侧标「不参与告警」，**不能显示「空间不足」**。
- **跨平台卷识别（2026-09-20 加，NAS/Docker 必需）**：判断"两个目录是不是同一块盘"用
  **`st_dev`（文件系统设备号）**，**不能用 `Path().anchor`** —— Windows 下它给 `F:\` 能区分，
  **POSIX 下恒为 `/`**（实测 `/download`、`/dan`、`/data` 全是 `/`），会把 NAS 上所有挂载盘
  **合并成一行**、只显示先遇到的那块，名字还显示成 `/`。
  - 显示名：Windows 用卷根（`F:\`）；POSIX 用**挂载点**（读 `/proc/self/mounts` → `/proc/mounts`
    → `/etc/mtab`，最长前缀匹配，如 `/download`），取不到挂载点退化为路径首段。挂载点表只读一次并缓存。
  - `st_dev == 0` 的极端平台退回卷根做键（至少不会把不同盘互吞）；路径不可访问 → 返回空、跳过该条。
  - ⚠️ `_mount_point_for` **只做纯字符串比较**：别加 `os.path.isabs` / `Path.resolve` 预处理 ——
    Windows 的 `isabs('/download')` 返回 **False**，会把 POSIX 路径解析成 `D:/download` 导致匹配失败
    （本机实测踩到；Linux 上才返回 True，是个平台陷阱）。
  - Linux 逻辑可在 Windows 上单测：`_parse_mount_points` / `_mount_point_for` 是纯函数，
    脚本 `.tmp/linux_disk_check2.py` 模拟容器 `/proc/mounts` 验证（含 `\040` 空格转义、最长前缀）。
  - Docker 部署要点：必须挂载**应用数据目录**（否则容器重建丢库）与**存储方案里的目录**；
    存储方案里填**容器内路径**（填宿主路径容器里不存在 → 盘被跳过、采集写失败）；
    应用数据可用 `DOUKHUB_DATA_ROOT` 改位（目录必须已存在，否则 `DataRootError` 启动失败）。
    `statvfs` 会穿透 bind mount 反映宿主那块盘的真实容量，粒度是**文件系统级**（QNAP 存储池）。
- ⚠️ 改 `app/main.py` 后**必须等托盘重启**才生效；若有任务在跑，托盘按任务闸延后重启，端点会暂时 404。

## 侧栏底部控件结构（`base.html`）

- DOM 顺序：`.gpc-slot`（采集进度 chip）→ `.sidebar-footer`（`#disk-badge` → `#mode-toggle` → `#task-badge`）。
  **两态顺序必须一一对应**（折叠态自上而下 = 展开态自左至右），否则 FLIP 补间时图标会交叉乱飞。
- 展开态：`.sidebar-footer > #task-badge { margin-left: auto; }` 把后台推到最右
  （⚠️ 不用 `space-between`：那会把三个按钮均匀铺开、看不出主次；折叠态要 `margin-left: 0` 还原）。
- 折叠态悬停展开（peek）：JS 加 `.is-peek`，chip 由 36×36 **原地展宽**到 200×36（left 不动，防闪烁）。
- 采集 chip 点击 = `openTaskPanel()`（与后台按钮同一入口）。chip 内部是两行堆叠容器 `.gpc-main`
  （第一行 `.gpc-head` = 图标+百分比+项数；第二行 `.gpc-sub` = 其它任务名），
  有额外任务时加 `.has-extras` 让高度 40 → 50px。
- 浮层通用关闭（点遮罩 / ESC）= base.html 里的派发器；豁免属性与 z-index 层级见 UI-DESIGN-SYSTEM §17。

## 定时任务频率校验（`app/main.py`）

**校验入口**：`_validate_schedule_expr(expr)` —— 先 `CronTrigger.from_crontab(expr)` 语法校验
（但校验前先特判 `@once:` 前缀），再走频率下限检查。常量：

```python
SCHEDULE_MIN_INTERVAL_MINUTES = 60   # 硬拦；< 此值直接拒
```

**间隔估算**：`_estimate_min_interval_minutes(expr)`。关键实现在
`_expand_cron_field(field, low, high)` —— apscheduler 的字段对象**没有"列出所有取值"的 API**
（只有 `get_next_value(dateval)` 这种逐步推进的接口），所以直接读它编译好的 `expressions`：

| 表达式对象 | 对应写法 | 有 first/last？ |
|---|---|---|
| `AllExpression(step)` | `*` 或 `*/n` | **无**（`step` 为 None 或 int） |
| `RangeExpression(first, last, step)` | `a` / `a-b` / `a-b/n` / `a/n` | 有 |

判定：`getattr(e, 'first', None) is None` → 按 `range(low, high+1, step)` 展开；
否则按 `range(max(low, first), min(high, last)+1, step)`。展开不了（含月份/星期名）返回 `None`，
由调用方放弃限速判断。

**最小间隔**：把「时」「分」展开成一天内的分钟偏移集合 → 排序 → 取相邻差值
**+ 跨零点回绕差值** `1440 - offsets[-1] + offsets[0]` → 取最小值。
`dom != '*'` 或 `dow != '*'` 时直接返回 `None`（一天最多一次，无需限速）。

⚠️ **为什么"一天多次"必须在同一个任务里表达**：`_on_scheduled_task_trigger` 遇到
`RuntimeError("已有采集批次正在执行")` 会 `await asyncio.sleep(300)` 重试、最多 12 次（耗 1 小时）。
若把一个"一天 4 次"拆成 4 个任务，它们会互相干等、互相跳过，实际谁也跑不稳。

**前端对应**（`schedule.html`）：`MIN_INTERVAL_MINUTES = 60` / `WARN_INTERVAL_MINUTES = 120`
需与后端保持一致（改一处要同步另一处）。

## `preset_id` 类型漂移（2026-09-21 修复）

**症状**：任务卡片把好好的采集方案标成红色「方案已删除」。

**根因**：`scheduled_tasks.preset_id` 的**列类型在两种库里不一致**。

| 库 | 列类型 | 来源 |
|---|---|---|
| 迁移过的老库（本机） | **TEXT** | 旧列名「等级筛选」是文本，`ALTER TABLE ... RENAME COLUMN` **只改名、不改类型** |
| 新装库 | INTEGER | 建表 DDL `database.py` 里写的是 `preset_id INTEGER` |

于是同一字段：写入是数字 `1`，读回来是字符串 `"1"`，而方案 id（存在 config 里）是数字 `1`。

**三处失配**：

| 位置 | 机制 | 症状 |
|---|---|---|
| `schedule.html` 列表查找 | JS `===` 严格比较，`1 === "1"` 为假 | 误报「方案已删除」 |
| `schedule.html` `fillPresetOptions` | 同上 | **编辑任务时方案下拉回填成第一个方案**，直接保存会静默换方案 |
| `main.py` `api_list_schedules` | Python **dict 查找靠 hash**，`hash("1") != hash(1)` → miss | API 的 `preset_name` 恒为空串 |

⚠️ **注意 `get_preset()` 用的是 `==`（`1 == "1"` 为真），所以定时任务本身一直能正常执行** ——
只有"按名字查找"和"前端比对"这两类走 `===`/hash 的路径会翻车。排查时别被"任务能跑"误导。

**修法**：
1. `database.py` 新增 `_normalize_scheduled_task()`，在 `get_scheduled_task(s)` **出口**把 `preset_id`
   统一转 int —— 一处治本，上层只面对一种类型；
2. 前端 2 处改 `String(p.id) === String(t.preset_id)` 兜底；
3. 后端 `preset_map` 改用 `str()` 键。

**刻意不做**表重建改列类型：SQLite 改列类型要「建新表 → 拷数据 → 换名」，风险远大于收益；
在读取出口归一化即可，且对将来新装库（本来就是 INTEGER）无副作用。

⚠️ **通用教训**：从旧表 `RENAME COLUMN` 迁移过来的字段，**类型可能与建表 DDL 不符**。
凡是跨越「DB ↔ Python ↔ JS」比对的 id，一律显式转字符串。

## 任务历史（`sync_history`）的写入点（2026-09-21 v2.3.9 补全）

「后台任务 → 历史」读的就是这张表。写入点：

| `task_type` | 写入位置 | 时机 |
|---|---|---|
| `import_collection` | `main.py` 导入分享表接口 | 导入完成 |
| `update_collection` / `sync_account` / `refresh_accounts` / `cloud_sync` / `dedup` | `core/tasks.py` → `TaskManager._save_history()` | 后台任务结束（**best-effort**，异常静默）|
| `collection_batch` | `core/collection_batch_manager.py` → `_record_history()` | 批次结算时（**v2.3.8 新增**）|
| `scheduled_task` | `main.py` → `_record_schedule_run()` | **每次执行都写**（v2.3.9 起含成功；此前仅失败）|
| `feishu_sync` | `core/feishu_sync.py` → `_record_sync_history()` | `sync_incremental()` 收尾（**v2.3.9 新增**）|
| `service_start` / `backup` | `main.py` → `_record_system_event()` | 服务启动 / 备份完成（**v2.3.9 新增**）|

**两列关联字段**（都追加在表末尾 —— 与老库 `ADD COLUMN` 的物理位置一致）：

- `ref_id`（v2.3.8）：**关联对象 ID**。采集批次写批次 ID；`scheduled_task` 写本次拉起的
  **第一个批次 ID**。前端据此把条目做成 `/collect?batch=<ref_id>` 直达链接。
- `trigger_source`（v2.3.9）：**谁触发的**。取值 `manual` / `schedule` / `catchup` /
  `startup` / `daily` / `sync_before`，前端渲染成小胶囊；**字典里没有的值不显示**（不裸露英文）。

**`_record_history()` 的三条约定**（其它写入点同理）：
1. **幂等** —— 写前查 `task_type='collection_batch' AND ref_id=?`；`_finalize()` 在
   「取消收尾」「重启恢复」「worker 兜底」路径都可能重入，不查就会写重复条目。
2. **best-effort** —— 异常一律吞掉，绝不影响主流程（`_record_schedule_run` /
   `_record_sync_history` / `_record_system_event` 同此约定）。
3. **`total` = `success + failed + skipped`**（实际处理数），不是批次的 `total_accounts`（计划数）；
   批次被取消时两者会差很多，历史里要表达"实际跑了多少"。

⚠️ **`service_start` 必须去重**（`SERVICE_START_DEDUPE_MINUTES = 5`）：托盘见 `.py` 变化就自动重启，
开发期一次会话能重启十几次，不去重历史会被刷屏，真正有价值的"停机多久"反而看不见。

**失败通知（`/api/schedules/failures`）的口径**：扫**全部** `status='failed'`，不再限定
`task_type='scheduled_task'`（手动采集、导入、账号表失败原先都只进历史、不进失败）。
已读语义不变 —— **水位线 + 显式 id 集合**，别用 `-1` 哨兵。
⚠️ **ack 接口的取数窗口必须与它一致**（`get_sync_history(limit=200)`，不带 `task_type`），
否则清空后其它类型的失败会在下一轮轮询又冒出来。
⚠️ 口径放开后首次拉取可能一次带回几十条历史失败 → 前端 toast **每轮最多 2 条 + 1 条汇总**，
不能逐条弹（会刷满屏幕）。

**历史清理**：`cleanup_sync_history(days=SYNC_HISTORY_KEEP_DAYS)` 挂在 lifespan，保留 **30 天**。
（v2.3.9 前该函数定义了却从未被调用 → 历史只增不删，最早可追到 2026-08-13。）
注意 `created_at` 是 SQLite `CURRENT_TIMESTAMP`（**UTC**）而清理也用 `datetime('now')`（UTC）——
两边口径一致所以比较正确；但**跨端显示**这条时间会差 8 小时，`finished_at` 才是本地时间。

---

## 飞书「字段定义」接口（bitable fields）—— 三个已修的坑（2026-09-23）

**① PUT fields 强制要求 `field_name` + `type` 同时传。** 只传 `property`（改选项）或只传 `field_name`（改名）
都返回 `99992402 field validation failed`，`field_violations` 会写明 `field_name is required` / `type is required`。
正确写法 `{field_name, type, property}`，`type` 从 `list_fields()` 的 items 里取。

**② 该接口 HTTP 状态常是 400，真实业务码在 body 里。** 只 `raise_for_status()` 只能看到 `HTTPError 400`、
**看不到 99992402** → 错误被静默吞掉。必须**先解析 body 再判断**。
白名单 `_FIELD_OK_CODES = (0, 1254606)`：**`1254606 = DataNotChange`（值未变化）等价成功**，
幂等重跑（传原值）必然返回它，当失败会误报。`create_field` / `update_field` 都照此检查。

**③ 修好一个"静默失败"，必须排查它以前替谁兜底。** `ensure_fields` 的冲突分支（新旧字段名并存）
原会把旧字段改名成 `<old>_legacy`，因 ① 一直失败；修好后会**真的改名**，等于擅自改用户表结构
（飞书视图/公式引用会失效）。但目标名本来已占用、**没有冲突需要让位** →
改为**只 `logger.warning`、保留原样**，返回值新增 `field_conflicts` 供前端提示。

**三处调用点**：`ensure_fields` 的两处 legacy 改名、`_sync_field_options`（补单选/多选选项）、`create_field`。
⚠️ 补选项时 `property.options` 是**整体替换**，必须把现有项**连同 `id` 原样回传**，
否则飞书会把旧选项当新选项重建，引用它的单元格数据会错位。

## 云端同步按钮的反馈方式：提交任务 + 轮询，不是 SSE

`/api/feishu/sync` 与 `/api/feishu/sync/full` 都是**后台任务模式**：立即返回 JSON `{task_id, status:"pending"}`，
过程日志写进任务对象（`tm.add_log`）。前端必须**轮询** `GET /api/tasks/{task_id}` 渲染。
⚠️ 曾误按 **SSE 流**解析（只认 `data: ` 开头的行）→ 整行 JSON 被跳过 → `complete` 分支永不执行
**且不报错** → 界面永远停在「正在同步…」（任务其实成功）。
判据：**该接口返回 `application/json` 就不是 SSE**；`table.html` 的 `/api/cookies/validate` 才是真 SSE。
前端统一执行器 `runSyncTask()`（`database.html`）：轮询 900ms、`cursor` 记已渲染的**绝对日志行号**
（兼容 `log_total` 截断）、连续 5 次查不到才报错、30 分钟超时。


