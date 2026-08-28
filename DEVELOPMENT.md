# DoukHub 开发文档

> 本文档实时记录开发过程中的设计讨论、架构决策和注意事项
> 最后更新：2026-08-28，对应代码版本 v2.2.3

***

## 📋 目录

1. [项目概述](#项目概述)
2. [核心流程设计](#核心流程设计)
3. [数据表设计](#数据表设计)
4. [API 接口设计](#api-接口设计)
5. [技术架构](#技术架构)
6. [关键决策记录](#关键决策记录)
7. [注意事项](#注意事项)

***

## 项目概述

DoukHub 是一个社交媒体数据采集管理平台，整合 TikTokDownloader，提供统一的 Web 界面进行：

* 飞书多维表格数据管理
* 自动化数据采集
* Cookie 轮换管理
* 单作品/批量采集
* 文件查重与数据备份

### 核心原则

1. **以官方文档为准**：优先参考 TikTokDownloader 的官方文档（TTD docs: http://127.0.0.1:5555/docs）

2. **所有平台交互走 TTD API**：DoukHub 不直接请求平台（单作品直连解析除外），平台更新反爬策略时只需更新 TTD 内核

3. **数据表职责分离**：分享表（元信息输入） vs 账号表（详细信息输出）

4. **三层架构分离**：工具层(DoukHub代码) / 配置层(~/.doukhub/) / 内核层(TTD)，互不影响

5. **本地数据库(SQLite)为主**：一期以本地数据库为唯一数据来源，飞书双向同步放到二期

6. **状态字段用枚举文本**：解析状态(待解析/已就绪/解析失败)、获取状态(待获取/已获取/获取失败)

7. **字段尽量少，飞书和本地数据库一致**：缓存表镜像飞书表

8. **中文字段名**：所有业务字段使用中文，系统字段使用英文

9. **所有耗时操作需要 SSE 实时进度 + 停止按钮**：避免用户以为卡死

### TTD API 端点（无 /api/ 前缀，完整列表）

> 来源：TikTokDownloader 5.7.stable 官方文档（`/docs`、`/openapi.json`）

#### 抖音端点

| 端点 | 用途 | Cookie | DoukHub 是否使用 |
| --- | --- | --- | --- |
| `POST /douyin/share` | 解析分享短链接，获取完整 URL | 不需要 | ✅ 已用 |
| `POST /douyin/account` | 获取账号作品数据（含昵称、粉丝数等） | 可选（不带拿不到数据） | ✅ 已用（count=1 优化） |
| `POST /douyin/detail` | 获取单个作品数据 | 可选 | ✅ 已用 |
| `POST /douyin/mix` | 获取合集作品数据 | 可选 | 未用 |
| `POST /douyin/live` | 获取直播数据 | 可选 | 未用 |
| `POST /douyin/comment` | 获取作品评论数据 | 可选 | 未用 |
| `POST /douyin/reply` | 获取评论回复数据 | 可选 | 未用 |
| `POST /douyin/search/general` | 综合搜索 | 可选 | 未用 |
| `POST /douyin/search/video` | 视频搜索 | 可选 | 未用 |
| `POST /douyin/search/user` | 用户搜索 | 可选 | 未用 |
| `POST /douyin/search/live` | 直播搜索 | 可选 | 未用 |

#### TikTok 端点

| 端点 | 用途 |
| --- | --- |
| `POST /tiktok/share` | 解析短链接 |
| `POST /tiktok/account` | 获取账号作品数据 |
| `POST /tiktok/detail` | 获取单个作品数据 |
| `POST /tiktok/mix` | 获取合辑作品数据 |
| `POST /tiktok/live` | 获取直播数据 |

#### 其他端点

| 端点 | 用途 |
| --- | --- |
| `GET /` | 访问 GitHub 仓库 |
| `GET /token` | 测试令牌有效性 |
| `GET /settings` | 获取项目全局配置 |
| `POST /settings` | 更新项目全局配置 |

#### Cookie 说明

* 除 `/douyin/share` 外，所有数据接口都标为 Cookie"可选"，但实际不带 Cookie 只能拿到空数据
* TTD 没有提供 Cookie 验证、登录、扫码等接口
* Cookie 验证方式：DoukHub 用 `/douyin/account` 探测一个已知账号，能拿到 nickname 则有效

### 标签缩写映射（可配置在 config.json）

| 缩写 | 完整标签 | 缩写 | 完整标签   |
| -- | ---- | -- | ------ |
| 个  | 个人   | 展  | 展会     |
| 人  | 个人   | 直  | 直播LIVE |
| 图  | 图集   | 播  | 直播LIVE |
| 集  | 图集   | 长  | 长腿     |
| 自  | 自拍   | 腿  | 长腿     |
| 拍  | 自拍   | 酒  | 酒吧     |
| 分  | 分享   | 吧  | 酒吧     |
| 享  | 分享   | 户  | 户外     |
| 街  | 街拍   | 外  | 户外     |
| 商  | 商业   | 多  | 多（数量多） |
| 业  | 商业   |太多  | 多       |
| 模  | 模特   | 南充 | 南充    |
| 特  | 模特   |    |        |

标签合并去重，大小写不敏感（以小写做去重 key，保留原始大小写）。未匹配的标签作为新标签保留。

***

## 核心流程设计

### 四步同步流程（当前版本）

#### 步骤1：导入分享表

**输入：** 用户粘贴的文本（支持 JSON、`标签+等级@分享码`、空格分隔三种格式）

**操作：**

1. 正则提取信息（share、等级、标签、账号名称、粉丝数、作品数等）
2. 标准化 share 格式（去掉 `https://v.douyin.com/` 前缀、参数等）
3. 如果是完整主页链接，直接提取 sec_user_id
4. 去重检查（基于标准化后的 share 或 sec_user_id）
5. 重复处理：等级取高的，标签合并（去重），软删除的恢复
6. 写入本地数据库 share_cache 表

**解析状态：** 待解析（等待步骤2），或已就绪（直接提取到 sec_user_id）

#### 步骤2：解析分享表（获取 sec_user_id）

**条件：** sec_user_id 为空 且 解析状态 != 已就绪 的记录

**操作：**

1. 筛选待解析的记录
2. 读取 share_code → 调用 `/douyin/share` API
3. 提取 sec_user_id
4. 检查 sec_user_id 是否在分享表已存在（合并等级标签，软删除重复记录）
5. 成功：标记解析状态=已就绪
6. 失败：标记解析状态=解析失败

**API：** `/douyin/share`

#### 步骤3：同步账号表

**条件：** 有 sec_user_id 的分享表记录

**操作：**

1. 加载账号表全部记录到内存做字典查找
2. 检查账号表是否已有该 sec_user_id
3. 如果已有：合并等级和标签，更新账号表
4. 如果没有：调用 `/douyin/account` API 获取账号详细信息，创建新记录
5. 软删除的账号自动复活
6. TTD/Cookie 不可用时仍创建/恢复账号基础数据并合并等级标签，仅跳过详情获取
7. 成功：标记获取状态=已获取
8. 失败：标记获取状态=获取失败

**API：** `/douyin/account`

#### 步骤4：更新账号表

**条件：** 获取状态 != 已获取 的账号记录

**操作：**

1. 筛选未获取信息的账号
2. 按账号自身平台调用详情接口
3. 获取详细信息（昵称、粉丝数、作品数、签名、头像）
4. 成功：标记获取状态=已获取
5. 失败：标记获取状态=获取失败
6. 步骤4只更新账号表，不修改分享表

**API：** `/douyin/account`（按平台调用对应端点）

#### 一键同步

**执行顺序：** 步骤1 → 步骤2 → 步骤3（步骤4天然承担步骤3失败的重试）

**进度显示：** 每个步骤都通过 SSE 显示详细进度

### 数据流向

```
分享表（元信息输入）
  ├─ 读取：share_code、等级、标签、sec_user_id
  ├─ 回写：解析状态（枚举）、sec_user_id
  └─ 不更新：粉丝数、作品数（只在账号表维护）

账号表（详细信息输出）
  ├─ 写入：所有账号详细信息
  ├─ 复制：等级、标签（从分享表）
  ├─ 回写：获取状态（枚举）
  └─ 维护：粉丝数、作品数（API 获取为准）
```

***

## 数据表设计

### 设计原则

1. **字段尽量少**：只保留必要字段
2. **飞书和本地数据库一致**：缓存表镜像飞书表，飞书挂了仍可工作
3. **中文字段名**：业务字段使用中文，系统字段使用英文
4. **包含时间戳**：created_at、同步时间
5. **软删除**：is_deleted / deleted_at 墓碑机制

### 三层架构

```
1. 工具层（DoukHub 代码）
   └─ d:/AI/DoukHub/

2. 配置层（个人信息）
   └─ ~/.doukhub/
      ├─ config.json          # 旧配置文件（迁移后改名为 config.json.migrated）
      └─ doukhub.db          # SQLite 本地数据库

3. 内核层（TTD）
   └─ d:/AI/DoukHub/TikTokDownloader/
```

### 飞书表设计

#### 分享表（Collection Table）

| 字段名  | 类型  | 说明                 | 更新时机         |
| ---- | --- | ------------------ | ------------ |
| 分享码  | 文本  | 抖音分享码（如 iMLuCKjq）  | 导入时写入        |
| 平台   | 单选  | 抖音/TikTok/小红书      | 导入时写入        |
| 等级   | 数字  | 1-4                | 导入时写入，去重时取高的 |
| 标签   | 多选  | 标签列表               | 导入时写入，去重时合并  |
| 账号标识 | 文本  | sec_user_id（步骤2写入） | 步骤2写入        |
| 解析状态 | 单选  | 待解析/已就绪/解析失败       | 步骤2更新        |
| 备注   | 文本  | 用户备注 + 合并信息        | 导入时写入，合并时追加  |
| 账号名称 | 文本  | 手动备注名称（导入时可能有）     | 导入时写入        |
| 粉丝数  | 数字  | 手动备注（导入时可能有）       | 导入时写入        |
| 作品数  | 数字  | 手动备注（导入时可能有）       | 导入时写入        |

#### 账号表（Account Table）

| 字段名  | 类型  | 说明                | 更新时机     |
| ---- | --- | ----------------- | -------- |
| 账号名称 | 文本  | 账号名称              | 步骤3写入    |
| 平台   | 单选  | 抖音/TikTok/小红书     | 步骤3写入    |
| 链接   | URL | 账号主页链接            | 步骤3写入    |
| 账号标识 | 文本  | sec_user_id（唯一索引） | 步骤3写入    |
| 等级   | 数字  | 从分享表复制，去重时取高的     | 步骤3写入    |
| 标签   | 多选  | 从分享表复制，去重时合并      | 步骤3写入    |
| 粉丝数  | 数字  | API 获取（以最新为准）     | 步骤3写入    |
| 作品数  | 数字  | API 获取（以最新为准）     | 步骤3写入    |
| 签名   | 文本  | API 获取            | 步骤3写入    |
| 头像   | URL | API 获取            | 步骤3写入    |
| 获取状态 | 单选  | 待获取/已获取/获取失败       | 步骤3/4更新  |
| 启用   | 复选框 | 是否参与后续采集          | 默认启用      |
| 采集类型 | 单选  | 发布/喜欢/收藏           | 步骤3写入    |
| 备注   | 文本  | 手动备注              | 手动填写      |

#### Cookie 表（Cookie Table）

| 字段名    | 类型  | 说明               |
| ------ | --- | ---------------- |
| Cookie | 文本  | Cookie 字符串       |
| 平台     | 单选  | 抖音/TikTok/小红书/通用 |
| 状态     | 单选  | 正常/失效            |
| 启用     | 复选框 | 是否参与轮换           |
| 备注     | 文本  | Cookie 说明        |
| 验证时间   | 日期  | 上次验证时间           |

***

## 本地数据库设计

### 数据库文件

* **位置：** `~/.doukhub/doukhub.db`
* **格式：** SQLite（WAL 模式）
* **用途：** 本地数据存储、飞书表镜像、历史记录、配置

### 数据库表结构（8张表）

#### 表1：分享表缓存（share_cache）

```sql
CREATE TABLE share_cache (
    record_id TEXT PRIMARY KEY,
    share_code TEXT UNIQUE NOT NULL,
    平台 TEXT,
    等级 INTEGER,
    标签 TEXT,
    sec_user_id TEXT,
    解析状态 TEXT DEFAULT '待解析',
    备注 TEXT,
    粉丝数 INTEGER,
    作品数 INTEGER,
    账号名称 TEXT,
    同步时间 DATETIME DEFAULT CURRENT_TIMESTAMP,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    is_deleted BOOLEAN DEFAULT 0,
    deleted_at DATETIME,
    synced BOOLEAN DEFAULT 0,
    local_updated_at DATETIME
);
```

> 注：旧名 `collection_cache`，已自动迁移为 `share_cache`。

#### 表2：账号表缓存（account_cache）

```sql
CREATE TABLE account_cache (
    record_id TEXT PRIMARY KEY,
    账号名称 TEXT,
    平台 TEXT,
    链接 TEXT,
    sec_user_id TEXT UNIQUE NOT NULL,
    等级 INTEGER,
    标签 TEXT,
    启用 BOOLEAN DEFAULT 1,
    采集类型 TEXT DEFAULT '发布',
    备注 TEXT,
    粉丝数 INTEGER,
    作品数 INTEGER,
    签名 TEXT,
    头像 TEXT,
    获取状态 TEXT DEFAULT '待获取',
    同步时间 DATETIME DEFAULT CURRENT_TIMESTAMP,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    is_deleted BOOLEAN DEFAULT 0,
    deleted_at DATETIME,
    synced BOOLEAN DEFAULT 0,
    local_updated_at DATETIME,
    last_collected_at DATETIME,
    collect_window_days INTEGER
);
```

#### 表3：Cookie表缓存（cookie_cache）

```sql
CREATE TABLE cookie_cache (
    record_id TEXT PRIMARY KEY,
    Cookie TEXT NOT NULL,
    平台 TEXT,
    状态 TEXT DEFAULT '正常',
    启用 BOOLEAN DEFAULT 1,
    备注 TEXT,
    验证时间 DATETIME,
    last_used_at DATETIME,
    use_count INTEGER DEFAULT 0,
    同步时间 DATETIME DEFAULT CURRENT_TIMESTAMP,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    is_deleted BOOLEAN DEFAULT 0,
    deleted_at DATETIME,
    synced BOOLEAN DEFAULT 0,
    local_updated_at DATETIME
);
```

#### 表4：采集批次（collection_batches）

```sql
CREATE TABLE collection_batches (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'pending',
    filter_json TEXT NOT NULL,
    platform TEXT NOT NULL,
    preset_name TEXT,
    process_pid INTEGER,
    log_path TEXT,
    started_at DATETIME,
    finished_at DATETIME,
    total_accounts INTEGER DEFAULT 0,
    success_accounts INTEGER DEFAULT 0,
    failed_accounts INTEGER DEFAULT 0,
    skipped_accounts INTEGER DEFAULT 0,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
```

#### 表5：采集批次明细（collection_batch_items）

```sql
CREATE TABLE collection_batch_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id TEXT NOT NULL,
    account_record_id TEXT,
    sec_user_id TEXT NOT NULL,
    account_name TEXT,
    platform TEXT NOT NULL,
    mark TEXT,
    url TEXT,
    earliest TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    message TEXT,
    started_at DATETIME,
    finished_at DATETIME,
    FOREIGN KEY (batch_id) REFERENCES collection_batches(id)
);
```

#### 表6：单作品历史（single_work_history）

```sql
CREATE TABLE single_work_history (
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
);
```

#### 表7：同步历史（sync_history）

```sql
CREATE TABLE sync_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_type TEXT NOT NULL,
    status TEXT NOT NULL,
    total INTEGER DEFAULT 0,
    success INTEGER DEFAULT 0,
    failed INTEGER DEFAULT 0,
    skipped INTEGER DEFAULT 0,
    error TEXT,
    log_json TEXT,
    started_at TEXT,
    finished_at TEXT,
    duration_sec REAL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
```

#### 表8：应用配置（settings）

```sql
CREATE TABLE settings (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at TEXT
);
```

> 配置整包以 JSON 存入 `key='config'` 单行。数据库不可用时回退到 JSON 文件。

### 数据库管理界面

**功能：**
* 查看所有表（4张用户可见表：share_cache, account_cache, cookie_cache, sync_history）
* 查看每张表的记录数
* 浏览表内容（分页、搜索、排序、列级筛选）
* 删除单条记录（软删除）
* 清空整张表
* 导出为 Excel
* 批量更新、批量删除
* 去重检测
* 行内编辑

**位置：** 侧边栏"数据"按钮

***

## API 接口设计

### TikTokDownloader API

#### 1. /douyin/share - 解析短链接

**用途：** 将抖音分享码解析为完整 URL，提取 sec_user_id

**请求：**
```json
POST /douyin/share
{
  "text": "iMLuCKjq"
}
```

#### 2. /douyin/account - 获取账号作品数据

**用途：** 获取账号的详细信息和作品列表

**请求：**
```json
POST /douyin/account
{
  "sec_user_id": "MS4wLjABAAAAXXX",
  "cookie": "sessionid=xxx",
  "tab": "post",
  "count": 1,
  "source": false
}
```

#### 3. /douyin/detail - 获取单个作品数据

**请求：**
```json
POST /douyin/detail
{
  "detail_id": "作品ID",
  "cookie": "可选",
  "source": false
}
```

#### 通用响应格式

```json
{
  "message": "状态信息",
  "data": {},
  "params": {},
  "time": "时间戳"
}
```

* `data` 为空或 `null` 通常表示 Cookie 无效/过期，或请求参数有误

### DoukHub Web API 路由清单

#### 页面路由（GET，返回 HTML）

| 路径 | 说明 |
|------|------|
| `/` | 仪表盘 |
| `/status` | 服务状态 |
| `/sync` | 同步页面（重定向到 /sync/overview） |
| `/sync/overview` | 同步概览 |
| `/sync/import` | 导入分享表 |
| `/sync/resolve` | 解析分享表 |
| `/sync/account` | 同步账号表 |
| `/sync/refresh` | 更新账号表 |
| `/sync/cloud` | 云端同步（飞书） |
| `/collect` | 采集页面（重定向到 /collect/overview） |
| `/collect/overview` | 采集概览 |
| `/collect/detail` | 采集详情 |
| `/table` | 数据表浏览 |
| `/database` | 数据库管理 |
| `/backup` | 数据备份 |
| `/dedup` | 文件查重 |
| `/duplicates` | 重复记录处理 |
| `/settings` | 设置页面 |

#### 同步 API

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/sync/v2/import` | 步骤1：导入分享表（SSE） |
| POST | `/api/sync/v2/update-collection` | 步骤2：解析分享表（SSE） |
| POST | `/api/sync/v2/sync-account` | 步骤3：同步账号表（SSE） |
| POST | `/api/sync/v2/refresh-accounts` | 步骤4：更新账号表（SSE） |
| POST | `/api/sync/v2/all` | 一键同步 |
| POST | `/api/sync` | 旧版同步（兼容） |
| POST | `/api/sync/fetch-info` | 旧版获取详情 |
| POST | `/api/import/collection` | 旧版导入 |
| POST | `/api/feishu/sync` | 飞书增量同步 |
| POST | `/api/feishu/sync/full` | 飞书全量同步 |
| GET | `/api/sync/history/{task_type}` | 同步历史 |
| GET | `/api/tasks` | 任务列表 |
| GET | `/api/tasks/{task_id}` | 任务详情 |
| GET | `/api/tasks/history` | 任务历史 |
| POST | `/api/tasks/{task_id}/cancel` | 取消任务 |

#### 采集 API

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/collection/batches/preview` | 预览采集批次 |
| POST | `/api/collection/batches` | 创建采集批次 |
| GET | `/api/collection/batches` | 批次列表 |
| GET | `/api/collection/batches/{batch_id}` | 批次详情 |
| POST | `/api/collection/batches/{batch_id}/cancel` | 取消批次 |
| POST | `/api/collection/batches/{batch_id}/retry` | 重试批次 |
| POST | `/api/collect/detail` | 单作品采集 |
| POST | `/api/collection/works/resolve` | 解析作品 |
| POST | `/api/collection/works/resolve-stream` | 解析作品（SSE） |
| POST | `/api/collection/works/download` | 下载作品 |
| POST | `/api/collection/works/download-stream` | 下载作品（SSE） |
| POST | `/api/collection/works/download-task` | 下载任务 |
| GET | `/api/collection/works/history` | 作品下载历史 |
| POST | `/api/collection/works/history/{history_id}/retry` | 重试下载 |
| POST | `/api/collection/works/history/{history_id}/open-dir` | 打开下载目录 |
| GET | `/api/collection/works/proxy-download` | 代理下载 |
| POST | `/api/collection/quick-add-share` | 快速添加分享码 |
| GET | `/api/collection/single-work/preferences` | 单作品偏好 |
| PUT | `/api/collection/single-work/preferences` | 更新单作品偏好 |
| GET | `/api/collection/defaults` | 采集默认设置 |
| PUT | `/api/collection/defaults` | 更新采集默认设置 |
| GET | `/api/collection/storage` | 存储方案列表 |
| PUT | `/api/collection/storage` | 更新存储方案 |
| PUT | `/api/collection/storage/profile/{profile_id}` | 更新单个存储方案 |
| POST | `/api/collection/storage/check` | 检查存储方案 |
| GET | `/api/collection/presets` | 采集预设列表 |
| POST | `/api/collection/presets` | 创建预设 |
| PUT | `/api/collection/presets/{preset_id}` | 更新预设 |
| DELETE | `/api/collection/presets/{preset_id}` | 删除预设 |
| POST | `/api/collection/presets/{preset_id}/preview` | 预览预设 |
| POST | `/api/collection/presets/{preset_id}/default` | 设为默认预设 |

#### 数据库 API

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/database/stats` | 各表记录数 |
| GET | `/api/database/stats-detailed` | 详细统计 |
| GET | `/api/database/table/{table_name}` | 表数据（分页/搜索/排序/筛选） |
| GET | `/api/database/table/{table_name}/schema` | 表结构 |
| GET | `/api/database/table/{table_name}/record/{record_id}` | 单条记录 |
| PATCH | `/api/database/table/{table_name}/record/{record_id}` | 更新单字段 |
| DELETE | `/api/database/table/{table_name}/record/{record_id}` | 删除记录 |
| DELETE | `/api/database/table/{table_name}` | 清空表 |
| GET | `/api/database/table/{table_name}/export` | 导出 Excel |
| POST | `/api/database/table/{table_name}/import/preview` | 导入预览 |
| POST | `/api/database/table/{table_name}/import/confirm` | 确认导入 |
| GET | `/api/database/duplicates/{table_name}` | 重复检测 |
| POST | `/api/database/batch-update` | 批量更新 |
| POST | `/api/database/batch-delete` | 批量删除 |
| POST | `/api/database/insert` | 插入单条 |
| PUT | `/api/database/update` | 更新记录 |
| POST | `/api/duplicates/resolve` | 重复处理 |

#### 备份与维护 API

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/backup/create` | 创建备份 |
| GET | `/api/backup/list` | 备份列表 |
| POST | `/api/backup/restore` | 恢复备份 |
| POST | `/api/backup/delete` | 删除备份 |
| POST | `/api/backup/vacuum` | 压缩数据库 |
| GET | `/api/backup/stats` | 备份统计 |
| GET | `/api/backup/download/{filename}` | 下载备份文件 |
| POST | `/api/backup/open-dir` | 打开备份目录 |
| GET | `/api/maintenance/items` | 维护项列表 |
| POST | `/api/maintenance/clean` | 清理 |

#### 文件查重 API

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/dedup/scope` | 查重范围 |
| POST | `/api/dedup/browse` | 浏览目录 |
| POST | `/api/dedup/scan` | 扫描重复 |
| GET | `/api/dedup/status` | 扫描状态 |
| GET | `/api/dedup/result` | 扫描结果 |
| POST | `/api/dedup/move` | 移到回收区 |
| GET | `/api/dedup/recycle` | 回收区内容 |
| POST | `/api/dedup/restore` | 恢复文件 |
| POST | `/api/dedup/delete` | 删除文件 |
| POST | `/api/dedup/open-recycle` | 打开回收区 |
| GET | `/api/dedup/settings` | 查重设置 |
| POST | `/api/dedup/settings` | 更新查重设置 |

#### 服务与状态 API

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/status` | 服务状态（feishu/ttd/xhs + kernels + services + stats + jobs） |
| POST | `/api/status/test/feishu` | 测试飞书连接 |
| POST | `/api/status/test/ttd` | 测试 TTD 连接 |
| POST | `/api/status/test/xhs` | 测试 XHS 连接 |
| GET | `/api/services/status` | 服务状态 |
| POST | `/api/services/{name}/start` | 启动服务 |
| POST | `/api/services/{name}/stop` | 停止服务 |
| POST | `/api/services/{name}/update` | 更新服务 |
| POST | `/api/services/update-all` | 更新全部 |
| GET | `/api/services/versions` | 服务版本 |
| GET | `/api/kernels/status` | 内核检测 |
| POST | `/api/kernels/{name}/install` | 安装内核 |
| GET | `/api/accounts` | 账号列表 |
| GET | `/api/stats` | 统计信息 |
| GET | `/api/history` | 采集历史 |
| GET | `/api/tags` | 标签列表 |
| GET | `/api/tags/options` | 标签选项 |
| GET | `/api/settings` | 获取设置 |
| POST | `/api/settings` | 保存设置 |
| POST | `/api/test-feishu` | 测试飞书 |
| POST | `/api/ensure-fields` | 确保飞书字段 |
| POST | `/api/cookies/validate` | 验证 Cookie |
| GET | `/api/browse-dir` | 浏览目录 |
| POST | `/api/system/exit` | 退出应用 |
| POST | `/api/system/restart` | 重启应用 |

#### API 请求模式（v2.2.2+）

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/v1/status` | API 模式状态 |
| GET | `/api/v1/api-info` | API 说明信息 |
| POST | `/api/v1/api-key` | 生成/重新生成 API Key |
| POST | `/api/v1/works/resolve` | 解析作品（外部调用） |
| POST | `/api/v1/works/download` | 下载作品（外部调用） |

> API 请求模式通过 `X-API-Key` 请求头鉴权，可在设置页启用/关闭。

***

## 技术架构

### 后端架构

```
app/
├── main.py                        # FastAPI 应用入口（~4200行，包含所有路由）
├── __init__.py
├── api/
│   └── __init__.py
├── core/
│   ├── config.py                  # 配置管理（JSON + 数据库 settings 表）
│   ├── database.py                # SQLite 数据库管理（8张表）
│   ├── feishu.py                  # 飞书 API 客户端
│   ├── feishu_sync.py             # 飞书双向同步器（方案 A：字段级单向同步）
│   ├── syncer.py                  # 旧同步逻辑（飞书依赖版）
│   ├── syncer_v2.py               # 新同步逻辑（本地优先四步流程）
│   ├── collector.py               # 数据采集器（调用 TTD API）
│   ├── cookie_pool.py             # Cookie 轮换管理
│   ├── link_resolver.py           # 短链接解析（正则 + sec_user_id 提取）
│   ├── history.py                 # 旧采集历史管理（独立 SQLite）
│   ├── tasks.py                   # 后台任务管理器（串行队列 + 持久化）
│   ├── collection_batch_manager.py # 批量采集管理器（TTD 终端模式）
│   ├── collection_planner.py      # 采集计划生成
│   ├── ttd_batch_runner.py        # TTD 批量运行器
│   ├── download_worker.py         # 单作品下载器
│   ├── single_work.py             # 单作品采集模块
│   ├── douyin_api.py              # 抖音直连 API（ABogus 签名，绕过 TTD）
│   ├── storage_profiles.py        # 存储方案管理
│   ├── presets.py                 # 采集预设
│   ├── backup.py                  # 数据备份与恢复
│   ├── dedup.py                   # 文件查重
│   ├── maintenance.py             # 系统维护
│   ├── platform_utils.py          # 平台标识规范化
│   └── __init__.py
├── services/
│   ├── downloader.py              # TTD/XHS 进程管理（启停、健康检查、版本检测）
│   └── __init__.py
├── static/
│   ├── css/style.css              # 主样式文件
│   ├── js/
│   │   ├── collect_detail.js      # 采集详情页交互
│   │   ├── lucide.min.js          # Lucide 图标库
│   │   ├── name_editor.js         # 名称编辑器
│   │   └── sync-step.js           # 同步步骤交互
│   ├── images/                    # 图标和图片
│   ├── vendor/                    # 第三方库（ag-grid、phosphor 图标）
│   └── manifest.webmanifest
└── templates/
    ├── base.html                  # 基础模板（侧边栏 + 移动端抽屉导航）
    ├── dashboard.html             # 仪表盘
    ├── status.html                # 服务状态
    ├── collect.html               # 采集页面
    ├── collect_detail.html        # 采集详情
    ├── collect/overview.html      # 采集概览
    ├── table.html                 # 数据表浏览（ag-grid）
    ├── database.html              # 数据库管理
    ├── backup.html                # 数据备份
    ├── dedup.html                 # 文件查重
    ├── duplicates.html             # 重复记录处理
    ├── history.html               # 采集历史
    ├── settings.html              # 设置页面
    ├── accounts.html              # 账号列表
    ├── sync/
    │   ├── overview.html           # 同步概览
    │   ├── import.html             # 导入分享表
    │   ├── resolve.html            # 解析分享表
    │   ├── account.html            # 同步账号表
    │   ├── refresh.html            # 更新账号表
    │   ├── cloud.html              # 云端同步
    │   └── _workflow.html          # 工作流公共模板
    └── partials/
        └── name_editor_modal.html  # 名称编辑弹窗
```

### 前端架构

* **框架：** FastAPI + Jinja2 模板
* **交互：** 原生 JavaScript + fetch API（不使用 HTMX）
* **数据表格：** ag-grid Community Edition
* **图标：** Lucide Icons（本地引入）+ Phosphor Icons（vendor）
* **实时进度：** SSE (Server-Sent Events) + `StreamingResponse`
* **停止按钮：** `AbortController` 取消请求
* **SPA 路由：** 单页切换（collect_detail 等页面使用独立 JS 加载）

### 关键模块

#### 1. SyncerV2（新同步器）

**文件：** `app/core/syncer_v2.py`

**职责：**
* 读取分享表
* 调用 Collector 获取数据
* 更新分享表和账号表
* 管理同步状态

**关键方法：**
* `import_to_collection(text)` - 步骤1：导入分享表
* `update_collection()` - 步骤2：解析分享表（获取 sec_user_id）
* `sync_to_account()` - 步骤3：同步账号表 + 拉详情
* `sync_all(text)` - 一键同步

#### 2. Syncer（旧同步器）

**文件：** `app/core/syncer.py`

**职责：** 旧版飞书依赖同步逻辑，保留兼容

#### 3. Collector（采集器）

**文件：** `app/core/collector.py`

**职责：**
* 调用 TikTokDownloader API
* 解析返回数据

**关键方法：**
* `resolve_short_url(url, platform, proxy)` - 解析短链接
* `get_account_info(sec_user_id, platform, cookie)` - 获取账号信息
* `collect_account(account, cookie)` - 采集账号作品
* `collect_single_detail(...)` - 单作品采集
* `collect_batch(...)` - 批量采集
* `validate_cookie(cookie, platform)` - 验证 Cookie

#### 4. LinkResolver（链接解析器）

**文件：** `app/core/link_resolver.py`

**职责：**
* 使用正则表达式解析 URL
* 提取 sec_user_id
* 平台识别

**关键方法：**
* `extract_sec_user_id(resolved_url, platform)` - 从 URL 提取 sec_user_id
* `build_profile_url(sec_user_id, platform)` - 生成账号主页链接
* `detect_platform(url)` - 根据 URL 识别平台
* `extract_detail_id(resolved_url)` - 提取作品 ID

#### 5. FeishuSyncer（飞书同步器）

**文件：** `app/core/feishu_sync.py`

**职责：** 飞书双向同步（方案 A：字段级单向同步）

**关键端点：**
* `sync_collection_from_feishu()` - 从飞书拉取分享表
* `sync_account_to_feishu()` - 回写账号详情到飞书

#### 6. TaskManager（任务管理器）

**文件：** `app/core/tasks.py`

**职责：** 后台串行任务队列，页面切换/刷新不影响执行

**关键方法：**
* `create(task_type)` - 创建任务
* `add_log(task_id, message, level)` - 添加日志
* `update(task_id, **fields)` - 更新任务状态
* `request_cancel(task_id)` - 请求取消
* `is_cancelled(task_id)` - 检查是否已取消

#### 7. CollectionBatchManager（批量采集管理器）

**文件：** `app/core/collection_batch_manager.py`

**职责：** 管理 TTD 终端模式批量采集

**功能：** 创建批次、启动 TTD 子进程、监控进度、记录结果

#### 8. DownloadWorker（单作品下载器）

**文件：** `app/core/download_worker.py`

**职责：** 单作品下载执行（支持视频、图集、实况、动图）

#### 9. ServiceManager（服务管理器）

**文件：** `app/services/downloader.py`

**职责：** 管理 TTD/XHS API 服务的启停、健康检查、版本检测、自动更新

**关键方法：**
* `start_all()` / `stop_all()` - 启动/停止全部
* `status_all()` - 全部状态
* `install(name)` - 从 GitHub 克隆内核
* `update(name)` - 更新内核

***

> 去重和合并逻辑详见 [docs/DATABASE_GUIDE.md](./docs/DATABASE_GUIDE.md)「重复数据处理」章节。

***

## 错误处理和重试机制

### 失败处理

**规则：**
* API 调用失败：标记记录状态为"解析失败"/"获取失败"
* 不自动重试，下次运行时自动重试失败的记录
* 不需要最大重试次数

**筛选条件：**
* 步骤2：`sec_user_id` 为空 且 `解析状态` != 已就绪
* 步骤3：有 `sec_user_id` 的记录
* 步骤4：`获取状态` != 已获取

### Cookie 失效处理

**规则：**
* API 返回 Cookie 失效时，标记该 Cookie 的"状态"字段为"失效"
* 自动切换到下一个可用的 Cookie
* 如果没有可用 Cookie，记录错误并跳过详情获取（不阻塞流程）

### 用户确认

**规则：**
* 正常操作不需要询问用户
* 只有危险操作（如清空表、删除记录、恢复备份）才需要确认

***

## 前端界面设计

### 同步页面

同步页面拆分为 6 个子页面，侧边栏导航与当前步骤状态保持一致：

1. **概览** (`/sync/overview`) — 本地数据统计 + 推荐下一步操作
2. **导入** (`/sync/import`) — 步骤1：导入分享表
3. **解析** (`/sync/resolve`) — 步骤2：解析分享表
4. **账号** (`/sync/account`) — 步骤3：同步账号表
5. **更新** (`/sync/refresh`) — 步骤4：更新账号表
6. **云端** (`/sync/cloud`) — 飞书双向校准

### 采集页面

1. **概览** (`/collect/overview`) — 采集概览
2. **详情** (`/collect/detail`) — 单作品采集详情页

### 进度显示

每个步骤通过 SSE 显示详细进度：
```
步骤1：导入分享表
  ├─ 新增 X 条
  ├─ 更新 X 条
  ├─ 恢复 X 条
  └─ 跳过 X 条

步骤2：解析分享表
  ├─ 处理 X/Y 条
  ├─ 成功 X 条
  └─ 失败 X 条
```

### 去重功能

* 分享表/账号表/Cookie 表的重复检测自动执行
* 文件查重独立页面 (`/dedup`)，三级过滤（大小 → 部分哈希 → 全量哈希）

***

## 关键决策记录

### 决策 1：数据表职责分离

**日期：** 2026-07-10

* 分享表：元信息输入（用户手动填写）
* 账号表：详细信息输出（API 获取）
* 分享表只包含基础信息：share_code、等级、标签、sec_user_id
* 账号表包含所有详细信息：昵称、粉丝数、作品数等

### 决策 2：状态字段使用枚举文本

**日期：** 2026-08-15（v2.2.0 更新）

* 旧方案：复选框 + 错误字段（已同步 + 同步错误）
* 新方案：枚举文本状态字段
  * 分享表：`解析状态`（待解析/已就绪/解析失败）
  * 账号表：`获取状态`（待获取/已获取/获取失败）
* 删除了单独的 `同步错误`/`获取错误` 字段，失败语义由枚举表达

### 决策 3：sec_user_id 复用优化

**日期：** 2026-07-10

* 如果分享表已有 sec_user_id → 跳过 /douyin/share API
* 如果分享表没有 sec_user_id → 调用 API 后写回

### 决策 4：SSE 实时进度

**日期：** 2026-07-10

* 使用 SSE (Server-Sent Events) 实现实时进度
* 后端使用 `StreamingResponse` + `yield`
* 前端使用 `fetch` + `ReadableStream`

### 决策 5：停止按钮

**日期：** 2026-07-10

* 使用 `AbortController` 取消请求
* 捕获 `AbortError` 显示停止提示

### 决策 6：本地优先策略

**日期：** 2026-08-15（v2.2.0）

* 一期以本地数据库为唯一数据来源，不依赖飞书
* 飞书双向同步放到二期
* 系统字段（record_id/synced/local_updated_at）一期保留不用，二期启用

### 决策 7：配置存储迁移到数据库

**日期：** 2026-08-17（v2.2.1）

* 配置从 `~/.doukhub/config.json` 迁移到 `~/.doukhub/doukhub.db` 的 `settings` 表
* JSON 文件迁移后改名为 `config.json.migrated` 留档
* 数据库不可用时回退到 JSON 文件

### 决策 8：API 请求模式

**日期：** 2026-08-18（v2.2.2）

* 其他设备可通过 HTTP API 调用 DoukHub
* 通过 `X-API-Key` 请求头鉴权
* 可在设置页启用/关闭

***

## 注意事项

### 1. TikTokDownloader API 使用

* **以官方文档为准**：参考 http://127.0.0.1:5555/docs
* **API 端点不带 /api/ 前缀**：正确路径是 `/douyin/share`，不是 `/api/douyin/share`
* **count 参数**：使用 `count=1` 减少数据量，提高性能
* **Cookie 必需**：某些 API 需要 Cookie 才能正常工作

### 2. 飞书 API 使用

* **批量操作**：使用 `batch_create` 和 `batch_update` 减少 API 调用
* **字段类型**：文本=1, 数字=2, 单选=3, 多选=4, 日期=5, 复选框=7, URL=15
* **日期字段**：使用毫秒时间戳

### 3. 数据一致性

* **sec_user_id 是唯一标识**：用于去重和关联
* **等级和标签从分享表复制**：保持一致性
* **粉丝数、作品数以 API 为准**：不信任分享表的旧数据

### 4. 错误处理

* **API 调用失败**：记录错误信息，不中断整体流程
* **数据解析失败**：跳过该记录，继续处理下一条
* **网络超时**：设置合理的超时时间（30秒）

### 5. 性能优化

* **复用 sec_user_id**：避免重复调用 /douyin/share
* **批量操作**：使用飞书批量 API
* **SSE 进度**：避免阻塞 UI
* **Cookie 轮换**：分散请求，避免单一 Cookie 过载

### 6. 安全性

* **Cookie 管理**：不要硬编码 Cookie，使用 Cookie 表管理
* **敏感信息**：配置文件中不要提交到 Git
* **API 访问**：Web UI 默认仅监听 127.0.0.1，远程访问需配置

***

## 采集功能

### 批量采集

批量采集使用 TTD 终端模式执行，DoukHub 只改写 `TikTokDownloader/Volume/settings.json` 的 `accounts_urls` / `accounts_urls_tiktok`。验证增量时注意 TTD 要求具体日期格式为 `YYYY/MM/DD`。

### 单作品采集

支持视频、图集、实况、动图、单资产下载。配置了 Cookie 时可使用直连 API（ABogus 签名）绕过 TTD 直接解析，耗时约 1 秒。

单作品下载不会写入 TTD 的 `download_data`，因此同一作品后续整号归档仍可能再次下载。这是有意设计：单作品是灵活取件，整号批量是 TTD 管理的档案库。

> 版本更新日志详见 [CHANGELOG.md](./CHANGELOG.md)

***

## 参考资料

* [TikTokDownloader API 文档](http://127.0.0.1:5555/docs)
* [TikTokDownloader GitHub](https://github.com/JoeanAmiver/TikTokDownloader)
* [飞书开放平台文档](https://open.feishu.cn/document/)

***

**维护说明：**
* 本文档应随开发进度实时更新
* 重要决策需记录原因和影响
* 代码变更后需同步更新本文档中的模块列表、API 路由、数据库表结构

## 统一工作流界面

同步页面按一次性数据准备流程组织，采集页面按日常增量运行台组织。两者共用 `workflow-*` 样式。验证浏览器时，需要同时打开 `/sync/overview` 与 `/collect`，确认视觉一致、状态文本清晰，并在约 390px 宽度下检查表格只在自己的滚动容器内横向滚动。

## DoukHub 设计语言

DoukHub 使用暖白底面、细边框卡片、紧凑控件和蓝色主题色；布局保留左侧导航，不复制 EntHub 的顶部导航。验证时必须确认侧边栏选中项仍有左侧白色竖向指示条。核心页面使用本地 Lucide 图标，样式入口是 `app/static/css/style.css`。