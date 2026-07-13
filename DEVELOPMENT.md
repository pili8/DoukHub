# DoukHub 开发文档

> 本文档实时记录开发过程中的设计讨论、架构决策和注意事项

***

## 📋 目录

1. [项目概述](#项目概述)

2. [核心流程设计](#核心流程设计)

3. [数据表设计](#数据表设计)

4. [API 接口设计](#api-接口设计)

5. [技术架构](#技术架构)

6. [关键决策记录](#关键决策记录)

7. [注意事项](#注意事项)

8. [待讨论问题](#待讨论问题)

***

## 项目概述

DoukHub 是一个社交媒体数据采集管理平台，整合 TikTokDownloader 和 XHS-Downloader，提供统一的 Web 界面进行：

* 飞书多维表格数据管理

* 自动化数据采集

* Cookie 轮换管理

* 定时任务调度

### 核心原则

1. **以官方文档为准**：优先参考 TikTokDownloader 和 XHS-Downloader 的官方文档（TTD docs: http://127.0.0.1:5555/docs）TikTokDownloader-API接口文档.mhtml

2. **所有平台交互走 TTD/XHS API**：DoukHub 不直接请求平台，平台更新反爬策略时只需更新 TTD/XHS 内核

3. **数据表职责分离**：采集表（元信息输入） vs 账号表（详细信息输出）

4. **三层架构分离**：工具层(DoukHub代码) / 配置层(~/.doukhub/) / 内核层(TTD/XHS)，互不影响

5. **本地数据库(SQLite)作为飞书的镜像和备份**：飞书挂了仍可工作，飞书作为管理窗口和备份

6. **状态字段用复选框 + 错误字段**：简单直观，便于扩展

7. **字段尽量少，飞书和本地数据库一致**：缓存表镜像飞书表

8. **中文字段名**：所有数据库字段使用中文

9. **所有耗时操作需要 SSE 实时进度 + 停止按钮**：避免用户以为卡死

10. **代码测试通过后直接推送 GitHub**：不用问用户

### TTD API 端点（无 /api/ 前缀，完整列表）

> 来源：TikTokDownloader 5.7.stable 官方文档（`/docs`、`/openapi.json`）
> 完整文档文件：`TikTokDownloader-API接口文档.mhtml`（Swagger）、`TikTokDownloader-API接口文档- ReDoc.mhtml`（ReDoc）

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

### 标签缩写映射（可配置在设置页）

| 缩写 | 完整标签 | 缩写 | 完整标签   |
| -- | ---- | -- | ------ |
| 个  | 个人   | 展  | 展会     |
| 图  | 图集   | 直  | 直播LIVE |
| 自  | 自拍   | 腿  | 长腿     |
| 分  | 分享   | 酒  | 酒吧     |
| 街  | 街拍   | 户  | 户外     |
| 商  | 商业   | 多  | 多（数量多） |
| 模  | 模特   |    |        |

标签合并去重，大小写不敏感。未匹配的标签作为新标签保留。

***

## 核心流程设计

### 三步同步流程（已确认）

#### 步骤1：导入采集表

**输入：** 用户粘贴的文本（支持多种格式）

**操作：**

1. 正则提取信息（share、等级、标签等）

2. 标准化 share 格式（去掉 `https://v.douyin.com/` 前缀、参数等）

3. 未匹配字段写入备注

4. 去重检查（基于标准化后的 share）

5. 重复处理：等级取高的，标签合并（去重），删除重复项

6. 写入飞书采集表 + 本地数据库缓存

**状态：** 已同步=否（等待步骤2）

#### 步骤2：更新采集表（获取 sec_user_id）

**条件：** sec_user_id 为空 的记录

**操作：**

1. 筛选 sec_user_id 为空的记录

2. 读取 share → 调用 `/douyin/share` API

3. 提取 sec_user_id

4. 检查 sec_user_id 是否在采集表已存在

5. 如果已存在：

   * 等级取高的

   * 标签合并

   * 删除重复记录（保留有 sec_user_id 的记录）

6. 如果不存在：

   * 更新当前记录的 sec_user_id

7. 成功：标记已同步=是

8. 失败：记录同步错误，下次运行自动重试

**API：** `/douyin/share`

#### 步骤3：同步账号表

**条件：** 已同步=否 的记录

**操作：**

1. 筛选已同步=否 的记录

2. 读取 sec_user_id

3. 检查账号表是否已有该 sec_user_id

4. 如果已有：

   * 等级取高的

   * 标签合并

   * 更新账号表

   * 标记采集表已同步=是

5. 如果没有：

   * 调用 `/douyin/account` API 获取账号详细信息

   * 创建新记录

   * 标记采集表已同步=是

6. 失败：记录更新错误，下次运行自动重试

**API：** `/douyin/account`

#### 一键同步

**执行顺序：** 步骤1 → 步骤2 → 步骤3

**进度显示：** 每个步骤都显示详细进度（处理 X/Y 条，成功 X 条，失败 X 条）

**错误处理：** 即使某步骤失败，也继续后续步骤，最后汇总显示结果

### 数据流向

```
采集表（元信息输入）
  ├─ 读取：share、等级、标签、sec_user_id
  ├─ 回写：已同步（复选框）、同步错误（文本）
  └─ 不更新：粉丝数、作品数（只在账号表维护）

账号表（详细信息输出）
  ├─ 写入：所有账号详细信息
  ├─ 复制：等级、标签（从采集表）
  ├─ 回写：已更新（复选框）、更新错误（文本）
  └─ 维护：粉丝数、作品数（API 获取为准）
```

***

## 数据表设计

### 设计原则

1. **字段尽量少**：只保留必要字段

2. **飞书和本地数据库一致**：缓存表镜像飞书表，飞书挂了仍可工作

3. **中文字段名**：所有字段使用中文

4. **包含时间戳**：创建时间、更新时间

### 三层架构

```
1. 工具层（DoukHub 代码）
   └─ /Users/gm/AI/DoukHub/

2. 配置层（个人信息）
   └─ ~/.doukhub/
      ├─ config.json          # 飞书凭证、路径设置（保留 JSON）
      └─ doukhub.db           # SQLite 本地数据库

3. 内核层（TTD/XHS）
   └─ /Users/gm/AI/DoukHub/TikTokDownloader/
   └─ /Users/gm/AI/DoukHub/XHS-Downloader/
```

### 飞书表设计

#### 采集表（Collection Table）

| 字段名  | 类型  | 说明                 | 更新时机         |
| ---- | --- | ------------------ | ------------ |
| 分享码  | 文本  | 抖音分享码（如 iMLuCKjq）  | 导入时写入        |
| 平台   | 单选  | 抖音/小红书/TikTok      | 导入时写入        |
| 等级   | 数字  | 1-4                | 导入时写入，去重时取高的 |
| 标签   | 多选  | 标签列表               | 导入时写入，去重时合并  |
| 账号标识 | 文本  | sec_user_id（步骤2写入） | 步骤2写入        |
| 已同步  | 复选框 | 是否已同步到账号表          | 步骤3更新        |
| 同步错误 | 文本  | 失败原因               | 步骤2/3失败时写入   |
| 备注   | 文本  | 用户备注 + 合并信息        | 导入时写入，合并时追加  |
| 昵称   | 文本  | （次要，导入时可能有）        | 导入时写入        |
| 粉丝数  | 数字  | （次要，导入时可能有）        | 导入时写入        |
| 作品数  | 数字  | （次要，导入时可能有）        | 导入时写入        |

#### 账号表（Account Table）

| 字段名  | 类型  | 说明                | 更新时机     |
| ---- | --- | ----------------- | -------- |
| 账号名称 | 文本  | 账号名称              | 步骤3写入    |
| 平台   | 单选  | 抖音/小红书/TikTok     | 步骤3写入    |
| 链接   | URL | 账号主页链接            | 步骤3写入    |
| 账号标识 | 文本  | sec_user_id（唯一索引） | 步骤3写入    |
| 等级   | 数字  | 从采集表复制，去重时取高的     | 步骤3写入    |
| 标签   | 多选  | 从采集表复制，去重时合并      | 步骤3写入    |
| 昵称   | 文本  | API 获取            | 步骤3写入    |
| 粉丝数  | 数字  | API 获取（以最新为准）     | 步骤3写入    |
| 作品数  | 数字  | API 获取（以最新为准）     | 步骤3写入    |
| 签名   | 文本  | API 获取            | 步骤3写入    |
| 头像   | URL | API 获取            | 步骤3写入    |
| 已更新  | 复选框 | 是否已获取详细信息         | 步骤3更新    |
| 更新错误 | 文本  | 失败原因              | 步骤3失败时写入 |

#### Cookie 表（Cookie Table）

| 字段名    | 类型  | 说明               |
| ------ | --- | ---------------- |
| Cookie | 文本  | Cookie 字符串       |
| 平台     | 单选  | 抖音/小红书/TikTok/通用 |
| 状态     | 单选  | 正常/失效            |
| 启用     | 复选框 | 是否参与轮换           |
| 备注     | 文本  | Cookie 说明        |
| 验证时间   | 日期  | 上次验证时间           |

***

## 本地数据库设计

### 数据库文件

* **位置：** `~/.doukhub/doukhub.db`

* **格式：** SQLite

* **用途：** 飞书表的本地镜像、缓存、历史记录

### 数据库表结构（5张表）

#### 表1：采集表缓存（collection_cache）

```sql
CREATE TABLE collection_cache (
    记录ID TEXT PRIMARY KEY,
    分享码 TEXT UNIQUE NOT NULL,
    平台 TEXT,
    等级 INTEGER,
    标签 TEXT,
    账号标识 TEXT,
    已同步 BOOLEAN DEFAULT 0,
    同步错误 TEXT,
    备注 TEXT,
    昵称 TEXT,
    粉丝数 INTEGER,
    作品数 INTEGER,
    创建时间 DATETIME DEFAULT CURRENT_TIMESTAMP,
    更新时间 DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_collection_share ON collection_cache(分享码);
CREATE INDEX idx_collection_sec_user_id ON collection_cache(账号标识);
```

#### 表2：账号表缓存（account_cache）

```sql
CREATE TABLE account_cache (
    记录ID TEXT PRIMARY KEY,
    账号名称 TEXT,
    平台 TEXT,
    链接 TEXT,
    账号标识 TEXT UNIQUE NOT NULL,
    等级 INTEGER,
    标签 TEXT,
    昵称 TEXT,
    粉丝数 INTEGER,
    作品数 INTEGER,
    签名 TEXT,
    头像 TEXT,
    已更新 BOOLEAN DEFAULT 0,
    更新错误 TEXT,
    创建时间 DATETIME DEFAULT CURRENT_TIMESTAMP,
    更新时间 DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_account_sec_user_id ON account_cache(账号标识);
```

#### 表3：Cookie表缓存（cookie_cache）

```sql
CREATE TABLE cookie_cache (
    记录ID TEXT PRIMARY KEY,
    Cookie TEXT NOT NULL,
    平台 TEXT,
    状态 TEXT DEFAULT '正常',
    启用 BOOLEAN DEFAULT 1,
    备注 TEXT,
    验证时间 DATETIME,
    创建时间 DATETIME DEFAULT CURRENT_TIMESTAMP,
    更新时间 DATETIME DEFAULT CURRENT_TIMESTAMP
);
```

#### 表4：采集历史（collection_history）

```sql
CREATE TABLE collection_history (
    ID INTEGER PRIMARY KEY AUTOINCREMENT,
    账号名称 TEXT,
    平台 TEXT,
    账号标识 TEXT,
    采集类型 TEXT,
    等级 INTEGER,
    标签 TEXT,
    状态 TEXT,
    作品数 INTEGER,
    开始时间 DATETIME,
    结束时间 DATETIME,
    耗时秒数 REAL,
    错误信息 TEXT,
    创建时间 DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_history_sec_user_id ON collection_history(账号标识);
CREATE INDEX idx_history_created_at ON collection_history(创建时间);
```

#### 表5：定时任务（scheduled_tasks）

```sql
CREATE TABLE scheduled_tasks (
    ID INTEGER PRIMARY KEY AUTOINCREMENT,
    任务名称 TEXT NOT NULL,
    Cron表达式 TEXT NOT NULL,
    等级筛选 TEXT,
    启用 BOOLEAN DEFAULT 1,
    上次运行 DATETIME,
    下次运行 DATETIME,
    创建时间 DATETIME DEFAULT CURRENT_TIMESTAMP,
    更新时间 DATETIME DEFAULT CURRENT_TIMESTAMP
);
```

### 缓存同步策略

```
飞书表 → 本地缓存（读取飞书后立即更新）
本地缓存 → 飞书表（修改后尽快写回，或记录待同步状态）

同步状态字段（可选）：
  - synced：已同步
  - pending：待同步（本地修改，等待写回飞书）
  - failed：同步失败
```

### 数据库管理界面

**功能：**

* 查看所有表（5张表）

* 查看每张表的记录数

* 浏览表内容（分页显示）

* 支持搜索/筛选

* 删除单条记录

* 清空整张表

* 导出为 Excel

* 刷新缓存（从飞书重新同步）

**位置：** 侧边栏增加"数据库"按钮

**实现复杂度：** 中等（2-3小时）

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

**响应：**

```json
{
  "url": "https://www.douyin.com/user/MS4wLjABAAAAXXX",
  "params": {"text": "iMLuCKjq"}
}
```

**提取 sec_user_id：**

* 使用正则从 URL 中提取

* 正则模式：`douyin\.com/user/([A-Za-z0-9_-]+)`

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

**响应：**

```json
{
  "data": [
    {
      "author": {
        "nickname": "用户名",
        "signature": "个性签名",
        "follower_count": 10000,
        "aweme_count": 100,
        "avatar_larger": {
          "url_list": ["https://..."]
        }
      }
    }
  ]
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

#### 4. /douyin/comment - 获取作品评论（未使用）

**请求：**

```json
POST /douyin/comment
{
  "detail_id": "作品ID",
  "cookie": "可选",
  "pages": 1,
  "cursor": 0,
  "count": 20,
  "count_reply": 3,
  "reply": false,
  "source": false
}
```

#### 5. /douyin/search/general - 综合搜索（未使用）

**请求：**

```json
POST /douyin/search/general
{
  "keyword": "搜索关键词",
  "cookie": "可选",
  "pages": 1,
  "channel": 0,
  "sort_type": 0,
  "publish_time": 0,
  "duration": 0,
  "search_range": 0,
  "content_type": 0,
  "source": false
}
```

#### 6. /douyin/search/user - 用户搜索（未使用）

**请求：**

```json
POST /douyin/search/user
{
  "keyword": "搜索关键词",
  "cookie": "可选",
  "pages": 1,
  "channel": 2,
  "douyin_user_fans": 0,
  "douyin_user_type": 0,
  "source": false
}
```

#### 7. /douyin/mix - 合集作品（未使用）

**请求：**

```json
POST /douyin/mix
{
  "mix_id": "合集ID",
  "cookie": "可选",
  "cursor": 0,
  "count": 18,
  "source": false
}
```

#### 通用响应格式

所有 POST 接口统一返回：

```json
{
  "message": "状态信息",
  "data": {},
  "params": {},
  "time": "时间戳"
}
```

* `data` 为空或 `null` 通常表示 Cookie 无效/过期，或请求参数有误

### XHS-Downloader API

#### /xhs/detail - 获取作品详情

**请求：**

```json
POST /xhs/detail
{
  "url": "https://www.xiaohongshu.com/explore/xxx",
  "cookie": "xxx",
  "download": false
}
```

### 数据库管理 API

#### 统计信息

```
GET /api/database/stats
```

返回各表记录数：

```json
{
  "collection_cache": 0,
  "account_cache": 0,
  "cookie_cache": 0,
  "collection_history": 0,
  "scheduled_tasks": 0
}
```

#### 表数据

```
GET /api/database/table/{table_name}?limit=100&offset=0
```

**支持的表名：**

* `collection_cache` - 采集表缓存

* `account_cache` - 账号表缓存

* `cookie_cache` - Cookie缓存

* `collection_history` - 采集历史

* `scheduled_tasks` - 定时任务

#### 删除记录

```
DELETE /api/database/table/{table_name}/record/{record_id}
```

#### 清空表

```
DELETE /api/database/table/{table_name}
```

### 新同步器 API（v2）

#### 步骤1：导入采集表

```
POST /api/sync/v2/import
```

**请求：**

```json
{
  "text": "iMLuCKjq 3 街拍 户外\niMLuGfEv 2 个人"
}
```

**响应：**

```json
{
  "success": 2,
  "message": "导入完成: 成功 2 条，失败 0 条，跳过 0 条",
  "total": 2,
  "failed": 0,
  "skipped": 0,
  "errors": []
}
```

#### 步骤2：更新采集表（SSE）

```
POST /api/sync/v2/update-collection
```

**响应：** SSE 流式进度

```
data: {"type": "start", "message": "开始更新采集表"}
data: {"type": "stats", "total": 10, "success": 0, "failed": 0}
data: {"type": "progress", "message": "处理 [1/10]: iMLuCKjq"}
data: {"type": "log", "level": "ok", "message": "✅ iMLuCKjq: MS4wLjAB..."}
data: {"type": "complete", "success": true, "message": "更新完成"}
```

#### 步骤3：同步账号表（SSE）

```
POST /api/sync/v2/sync-account
```

**响应：** SSE 流式进度（同上）

#### 一键同步

```
POST /api/sync/v2/all
```

**请求：**

```json
{
  "text": "iMLuCKjq 3 街拍 户外"
}
```

**响应：**

```json
{
  "success": true,
  "message": "一键同步完成",
  "step1": {"total": 1, "success": 1, "failed": 0, "skipped": 0},
  "step2": {"total": 1, "success": 1, "failed": 0, "skipped": 0},
  "step3": {"total": 1, "success": 1, "failed": 0, "skipped": 0}
}
```

***

## 技术架构

### 后端架构

```
app/
├── main.py              # FastAPI 应用入口
├── core/
│   ├── config.py        # 配置管理
│   ├── database.py      # SQLite 数据库管理（新增）
│   ├── feishu.py        # 飞书 API 客户端
│   ├── syncer.py        # 旧同步逻辑（采集表→账号表）
│   ├── syncer_v2.py     # 新同步逻辑（使用数据库）（新增）
│   ├── collector.py     # 数据采集器（调用 TTD/XHS API）
│   ├── cookie_pool.py   # Cookie 轮换管理
│   ├── history.py       # 历史记录管理
│   ├── scheduler.py     # 定时任务调度
│   └── link_resolver.py # 短链接解析（正则）
├── services/
│   └── downloader.py    # TTD/XHS 服务管理
└── templates/
    ├── base.html        # 基础模板
    ├── sync.html        # 同步页面
    ├── database.html    # 数据库管理页面（新增）
    ├── status.html      # 状态页面
    └── settings.html    # 设置页面
```

### 前端架构

* **框架：** FastAPI + Jinja2 模板

* **交互：** HTMX + 原生 JavaScript

* **实时进度：** SSE (Server-Sent Events)

### 关键模块

#### 1. Syncer（同步器）

**文件：** `app/core/syncer.py`

**职责：**

* 读取采集表

* 调用 Collector 获取数据

* 更新采集表和账号表

* 管理同步状态

**关键方法：**

* `sync()` - 第一阶段：快速同步（只解析短链接）

* `fetch_account_info()` - 第二阶段：获取账号详细信息

#### 2. Collector（采集器）

**文件：** `app/core/collector.py`

**职责：**

* 调用 TikTokDownloader API

* 调用 XHS-Downloader API

* 解析返回数据

**关键方法：**

* `resolve_short_url()` - 解析短链接

* `get_account_info()` - 获取账号信息

* `collect_account()` - 采集账号作品

#### 3. LinkResolver（链接解析器）

**文件：** `app/core/link_resolver.py`

**职责：**

* 使用正则表达式解析 URL

* 提取 sec_user_id

* 平台识别

**关键方法：**

* `resolve_short_url()` - 跟随重定向获取完整 URL

* `extract_sec_user_id()` - 从 URL 提取 sec_user_id

* `detect_platform()` - 根据 URL 识别平台

***

## 去重和合并逻辑

### 去重场景（4个）

| 场景        | 触发时机             | 判断依据        | 处理方式              |
| --------- | ---------------- | ----------- | ----------------- |
| **导入去重**  | 导入采集表时           | 分享码相同       | 等级取高的，标签合并，删除重复项  |
| **步骤2去重** | 获取 sec_user_id 后 | 账号标识在采集表已存在 | 等级取高的，标签合并，删除重复记录 |
| **步骤3去重** | 同步账号表时           | 账号标识在账号表已存在 | 等级取高的，标签合并，更新账号表  |
| **账号表去重** | 手动触发             | 账号标识在账号表重复  | 等级取高的，标签合并，删除重复记录 |

### 等级更新逻辑

**规则：等级取高的**

```python
# 伪代码
new_level = max(existing_level, new_level)
```

**更新时机：**

1. 导入采集表时：如果分享码重复，等级取高的

2. 步骤2去重时：如果账号标识已存在，等级取高的

3. 步骤3去重时：如果账号标识已存在，等级取高的

### 标签合并逻辑

**规则：合并去重，大小写不敏感**

```python
# 伪代码
existing_tags = set(tag.lower() for tag in existing_tags)
new_tags = set(tag.lower() for tag in new_tags)
merged_tags = existing_tags.union(new_tags)
```

**示例：**

```
采集表标签：["街拍", "户外"]
账号表标签：["街拍", "个人"]
合并后：["街拍", "户外", "个人"]  ← "街拍"只保留一个
```

**大小写处理：**

* "COS" 和 "cos" 视为相同标签

* 合并后统一转换为小写或保持原样

### 备注字段格式

**合并信息记录格式：**

```
[操作类型] 详细信息

示例：
[导入合并] 原等级:1→3, 新增标签:[街拍,户外]
[步骤2合并] 原等级:2→3
[步骤3合并] 同步到账号表
[去重合并] 合并自记录 recXXX
[重试] 重试 2 次，原因：API 超时
```

***

## 错误处理和重试机制

### 失败处理

**规则：**

* API 调用失败：标记记录状态为"失败"，在错误字段记录原因

* 不自动重试，下次运行时自动重试失败的记录

* 不需要最大重试次数

**筛选条件：**

* 步骤2：`账号标识` 为空 且 `同步错误` 为空

* 步骤3：`已同步` = 否 且 `更新错误` 为空

### Cookie 失效处理

**规则：**

* API 返回 Cookie 失效时，标记该 Cookie 的"状态"字段为"失效"

* 自动切换到下一个可用的 Cookie

* 如果没有可用 Cookie，记录错误并停止

**Cookie 轮换逻辑：**

```python
# 筛选启用的 Cookie
enabled_cookies = [c for c in cookies if c.is_enabled and c.status == '正常']

# 选择一个 Cookie
cookie = select_cookie(enabled_cookies)

# 调用 API
try:
    result = call_api(cookie)
except CookieExpiredError:
    # 标记 Cookie 失效
    cookie.status = '失效'
    # 切换到下一个
    cookie = select_cookie(enabled_cookies)
    result = call_api(cookie)
```

### 用户确认

**规则：**

* 正常操作不需要询问用户

* 只有危险操作（如清空表、删除记录）才需要确认

***

## 前端界面设计

### 同步页面按钮

**4个按钮：**

1. 📥 导入采集表（步骤1）

2. 🔄 更新采集表（步骤2）

3. 📤 更新账号表（步骤3）

4. ⚡ 一键同步（执行步骤1→2→3）

### 进度显示

**每个步骤都显示详细进度：**

```
步骤1：导入采集表
  ├─ 解析 X 条
  ├─ 新增 X 条
  ├─ 更新 X 条
  └─ 跳过 X 条（重复）

步骤2：更新采集表
  ├─ 处理 X/Y 条
  ├─ 成功 X 条
  └─ 失败 X 条

步骤3：更新账号表
  ├─ 处理 X/Y 条
  ├─ 成功 X 条
  └─ 失败 X 条

汇总：
  ├─ 步骤1：新增 X，更新 X，跳过 X
  ├─ 步骤2：成功 X，失败 X
  └─ 步骤3：成功 X，失败 X
```

### 去重功能

**前端按钮：**

* 在"数据库"页面增加按钮："去除账号表重复"

* 其他去重自动执行，不需要用户手动触发

**去重结果显示：**

```
去重完成：
  - 发现 X 个重复账号
  - 合并 X 个标签
  - 删除 X 条重复记录
```

***

## 标签合并规则

### 去重规则

**合并时去重，大小写不敏感**

**示例：**

```
采集表标签：["街拍", "户外"]
账号表标签：["街拍", "个人"]
合并后：["街拍", "户外", "个人"]  ← "街拍"只保留一个

"COS" 和 "cos" 视为相同标签
```

***

## 关键决策记录

### 决策 1：数据表职责分离

**日期：** 2026-07-10

**讨论：**

* 采集表：元信息输入（用户手动填写）

* 账号表：详细信息输出（API 获取）

**决策：**

* 采集表只包含基础信息：share、等级、标签、sec_user_id

* 账号表包含所有详细信息：昵称、粉丝数、作品数等

* 采集表的粉丝数、作品数等字段有则保留，没有则不管

* 粉丝数、作品数始终以 API 获取为准

**原因：**

* 避免数据冗余

* 确保数据准确性

* 简化维护逻辑

### 决策 2：状态字段使用复选框 + 错误字段

**日期：** 2026-07-10

**讨论：**

* 方案1：复选框（已同步、已更新）

* 方案2：文本字段（同步状态）

* 方案3：单选字段（待同步/已同步/失败）

**决策：**

* 采用方案1：复选框 + 错误字段

* 采集表：已同步（复选框）+ 同步错误（文本）

* 账号表：已更新（复选框）+ 更新错误（文本）

**原因：**

* 90% 的情况只需要是/否

* 10% 失败的情况可以记录原因

* 飞书界面上复选框最直观

* 扩展性好（可以加同步时间等字段）

### 决策 3：sec_user_id 复用优化

**日期：** 2026-07-10

**讨论：**

* 如果采集表已有 sec_user_id，是否跳过 API 调用？

**决策：**

* 如果采集表已有 sec_user_id → 跳过 /douyin/share API

* 如果采集表没有 sec_user_id → 调用 API 后写回采集表

**原因：**

* 减少 API 调用次数

* 加快同步速度

* sec_user_id 是稳定值，不会变化

### 决策 4：SSE 实时进度

**日期：** 2026-07-10

**讨论：**

* 同步过程需要实时显示进度

* 避免用户等待时不知道状态

**决策：**

* 使用 SSE (Server-Sent Events) 实现实时进度

* 后端使用 `StreamingResponse` + `yield`

* 前端使用 `fetch` + `ReadableStream`

**原因：**

* 比轮询更高效

* 比 WebSocket 简单

* 支持实时统计更新

### 决策 5：停止按钮

**日期：** 2026-07-10

**讨论：**

* 同步过程可能耗时较长

* 需要能够中途停止

**决策：**

* 添加停止按钮

* 使用 `AbortController` 取消请求

* 捕获 `AbortError` 显示停止提示

**原因：**

* 避免长时间等待

* 用户控制体验更好

***

## 注意事项

### 1. TikTokDownloader API 使用

* **以官方文档为准**：参考 http://127.0.0.1:5555/docs

* **API 端点不带 /api/ 前缀**：正确路径是 `/douyin/share`，不是 `/api/douyin/share`

* **count 参数**：使用 `count=1` 减少数据量，提高性能

* **Cookie 必需**：某些 API 需要 Cookie 才能正常工作

### 2. 飞书 API 使用

* **批量操作**：使用 `batch_create` 和 `batch_update` 减少 API 调用

* **字段类型**：

  * 文本：1

  * 数字：2

  * 单选：3

  * 多选：4

  * 日期：5

  * 复选框：7

  * URL：15

* **日期字段**：使用毫秒时间戳

### 3. 数据一致性

* **sec_user_id 是唯一标识**：用于去重和关联

* **等级和标签从采集表复制**：保持一致性

* **粉丝数、作品数以 API 为准**：不信任采集表的旧数据

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

* **API 访问**：飞书应用需要正确的权限配置

***

## 待讨论问题

### 1. 采集表是否需要区分平台？

**问题：**

* 采集表需要区分抖音、小红书吗？

* 还是用一个表，通过字段区分平台？

**选项：**

* A. 一个采集表，通过"平台"字段区分

* B. 多个采集表，每个平台一个表

**当前倾向：** A（一个表，通过字段区分）

### 2. 未匹配字段写入备注

**决策（已确认）：**

* 采集表导入阶段，如果有的信息没有匹配到相应的字段，则写入备注字段

* 不要一味跳过，保留信息以便后续处理

### 3. 步骤2 和步骤3 的关系

**决策（已确认）：**

* 步骤2 和步骤3 是分开的两个步骤，可以独立执行

* 账号表的信息来源于采集表，采集表没有更新的话，账号表是没有信息的

* 各管各的，互不依赖

### 4. 已同步 字段的含义

**决策（已确认）：**

* 不需要单独的"已获取sec_user_id"字段

* 直接判断 `sec_user_id` 是不是空值

* `已同步` 表示采集表已经同步到账号表了，不用下次再传入到账号表去了

**判断逻辑：**

* `sec_user_id` 为空 → 需要执行步骤2（获取 sec_user_id）

* `sec_user_id` 不为空 → 步骤2 已完成

* `已同步` = 否 → 需要执行步骤3（同步到账号表）

* `已同步` = 是 → 步骤3 已完成，跳过

### 5. 前端按钮设计

**决策（已确认）：**

* 三个按钮：导入、更新采集表、更新账号表

* 一个"一键同步"按钮：依次执行三个步骤

* 步骤2 和步骤3 可以独立执行

### 6. 增量更新的含义

**澄清：**

* 增量更新 ≠ 处理重复内容

* 增量更新 = 只处理新增或变化的数据

* 重复内容处理 = 去重逻辑（基于 sec_user_id）

**当前实现：**

* 基于 `sec_user_id` 去重

* 基于 `已同步` 字段判断是否需要同步到账号表

* 基于 `已更新` 字段判断是否需要重新获取账号信息

***

## 设计确认记录

### 2026-07-10 设计讨论确认

**确认的设计点：**

1. ✅ 未匹配字段写入备注（不跳过）

2. ✅ 采集表通过"平台"字段区分（不区分表）

3. ✅ 步骤2 和步骤3 独立执行

4. ✅ 通过 `sec_user_id` 是否为空判断步骤2 状态

5. ✅ `已同步` 表示已同步到账号表

6. ✅ 三个独立按钮 + 一键同步按钮

7. ✅ 增量更新基于状态字段判断

**选项：**

* A. 保持现状，`已同步` 表示步骤2完成

* B. 分为两个字段：`已获取sec_user_id` + `已同步到账号表`

* C. `已同步` 表示步骤3完成（最终状态）

**当前倾向：** 待讨论

### 3. 前端界面设计

**问题：**

* 三个步骤是否需要三个按钮？

* 还是一个"同步"按钮自动执行所有步骤？

**选项：**

* A. 三个独立按钮：导入、更新采集表、更新账号表

* B. 一个"同步"按钮，自动执行所有步骤

* C. 混合：导入按钮 + 同步按钮（自动执行步骤2和3）

**当前倾向：** 待讨论

### 4. 错误处理策略

**问题：**

* 如果步骤2 失败，步骤3 是否应该跳过？

* 是否需要重试机制？

**选项：**

* A. 失败就跳过，不重试

* B. 失败后记录错误，下次重试

* C. 提供手动重试按钮

**当前倾向：** 待讨论

### 5. 增量更新逻辑

**问题：**

* 如果账号表已有该账号，步骤3 是更新还是跳过？

* 是否需要"强制更新"选项？

**选项：**

* A. 跳过已更新的账号

* B. 总是更新（覆盖旧数据）

* C. 根据 `已更新` 字段判断

* D. 提供"强制更新"选项

**当前倾向：** 待讨论

***

## 更新日志

### 2026-07-10（初始版本）

* 初始文档创建

* 记录核心流程设计讨论

* 记录数据表设计决策

* 记录技术方案决策（复选框 + 错误字段、SSE、停止按钮）

* 列出待讨论问题

### 2026-07-10（功能实现）

**新增模块：**

* `app/core/database.py` - SQLite 数据库管理（5张表，中文字段名）

* `app/core/syncer_v2.py` - 新同步器（使用数据库，去重合并逻辑）

* `app/core/feishu_sync.py` - 飞书双向同步器

* `app/templates/database.html` - 数据库管理界面

**新增 API 端点：**

* 数据库管理：`GET /api/database/stats`、`GET /api/database/table/{table}`、`DELETE` 操作

* 新同步器 v2：`POST /api/sync/v2/import`、`POST /api/sync/v2/update-collection`（SSE）、`POST /api/sync/v2/sync-account`（SSE）、`POST /api/sync/v2/all`

* 飞书同步：`POST /api/feishu/sync`、`POST /api/feishu/sync/to-feishu`、`POST /api/feishu/sync/from-feishu`

* 采集 v2：`POST /api/collect/v2/account`（SSE 实时进度）

**功能实现：**

* ✅ SQLite 数据库（5张表，中文字段名，完整 CRUD）

* ✅ 数据库管理界面（查看、删除、清空、搜索）

* ✅ 三步同步流程（导入→更新采集表→同步账号表）

* ✅ SSE 实时进度（所有耗时操作）

* ✅ 停止按钮（AbortController）

* ✅ 去重和合并（等级取高的，标签合并去重，大小写不敏感）

* ✅ 飞书双向同步（本地↔飞书）

* ✅ 账号作品采集（SSE实时进度，记录历史）

* ✅ 定时任务（使用新数据库存储）

* ✅ Cookie 轮换（自动切换失效Cookie）

* ✅ 标签映射（可配置，大小写不敏感）

**页面结构：**

```
侧边栏：
├── 📊 状态     - 服务状态、连通性检测
├── 🔄 同步     - 三步同步 + 飞书同步
├── 📥 采集     - 整号采集 + 单品采集 + 定时任务
├── 📋 记录     - 采集历史
├── 💾 数据库   - 数据库管理
└── ⚙️ 设置     - 飞书配置、标签映射
```

**测试结果：**

* 79 个单元测试全部通过（含 API 端点、配置、解析、历史、Cookie 池等；运行方式 `pytest tests/`）

* 完整流程测试通过

* 所有页面和 API 端点正常

***

## 参考资料

* [TikTokDownloader API 文档](http://127.0.0.1:5555/docs)

* [TikTokDownloader GitHub](https://github.com/JoeanAmier/TikTokDownloader)

* [XHS-Downloader GitHub](https://github.com/JoeanAmier/XHS-Downloader)

* [飞书开放平台文档](https://open.feishu.cn/document/)

***

**维护说明：**

* 本文档应随开发进度实时更新

* 重要决策需记录原因和影响

* 待讨论问题需及时跟进和更新
