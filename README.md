# DoukHub - 社交媒体数据采集管理平台

<div align="center">

[![Python Version](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.139.0-green.svg)](https://fastapi.tiangolo.com/)
[![SQLite](https://img.shields.io/badge/SQLite-3-blue.svg)](https://www.sqlite.org/)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

**整合 TikTokDownloader 和 XHS-Downloader 的统一管理平台**

[🚀 快速开始](#-快速开始) • [📖 功能介绍](#-功能介绍) • [🛠️ 技术架构](#%EF%B8%8F-技术架构) • [📚 API文档](#-api文档)

</div>

## ✨ 核心特性

### 🔄 **三步同步流程**
1. **导入采集表** - 解析用户输入的账号信息（分享码、等级、标签）
2. **解析账号标识** - 调用API获取 sec_user_id（账号唯一标识） 
3. **同步账号表** - 获取账号详细信息并回写飞书表格

### 📊 **数据管理**
- **双重存储** - 本地SQLite数据库 + 飞书多维表格
- **实时同步** - 双向增量同步，数据保持一致
- **智能去重** - 基于 sec_user_id 的去重和合并机制

### 🎯 **采集功能**
- **多平台支持** - 抖音、TikTok、小红书
- **Cookie轮换** - 自动管理和切换多个Cookie
- **定时任务** - APScheduler定时执行采集任务

### 🛡️ **企业级特性**
- **实时进度** - SSE流式进度显示
- **错误恢复** - 自动重试和错误记录
- **操作审计** - 完整的操作历史追踪

## 🏗️ 系统架构

```
┌─────────────────────────────────────────────────────────┐
│                    DoukHub Web UI                      │
│  ┌───────────────┬────────────────┬─────────────────┐   │
│  │   状态监控    │   同步管理     │    数据采集     │   │
│  └───────────────┴────────────────┴─────────────────┘   │
└──────────────────────────┬─────────────────────────────┘
                           │
┌──────────────────────────▼─────────────────────────────┐
│                    FastAPI 后端                        │
│  ┌───────────────┬────────────────┬─────────────────┐   │
│  │  同步引擎v2   │   飞书API集成   │    SQLite数据库  │   │
│  └───────────────┴────────────────┴─────────────────┘   │
└──────────────────────────┬─────────────────────────────┘
                           │
┌──────────────────────────▼─────────────────────────────┐
│                    外部服务                            │
│  ┌───────────────┬────────────────┬─────────────────┐   │
│  │ TikTokDownloader │  XHS-Downloader │     飞书API      │   │
│  └───────────────┴────────────────┴─────────────────┘   │
└─────────────────────────────────────────────────────────┘
```

## 🚀 快速开始

### 环境要求
- Python 3.12+
- SQLite 3
- 飞书开发者账号（用于多维表格）
- TikTokDownloader + XHS-Downloader（作为采集内核）

### 安装部署

1. **克隆项目**
```bash
git clone https://github.com/yourusername/DoukHub.git
cd DoukHub
```

2. **安装依赖**
```bash
pip install -r requirements.txt
```

3. **配置飞书应用**
编辑 `~/.doukhub/config.json`：
```json
{
  "feishu": {
    "app_id": "your_app_id",
    "app_secret": "your_app_secret",
    "app_token": "your_app_token",
    "collection_table_id": "采集表ID",
    "account_table_id": "账号表ID",
    "cookie_table_id": "Cookie表ID"
  }
}
```

4. **启动服务**
```bash
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

5. **访问界面**
浏览器打开 `http://localhost:8000`

### 📱 页面导航

| 页面 | 功能 | URL路径 |
|-----|-----|--------|
| 📊 仪表盘 | 服务状态监控 | `/dashboard` |
| 🔄 同步管理 | 三步同步流程 | `/sync` |
| 📥 数据采集 | 账号/单品采集 | `/collect` |
| 📋 采集历史 | 历史记录查询 | `/history` |
| 💾 数据库管理 | 本地数据管理 | `/database` |
| ⚙️ 设置 | 配置管理 | `/settings` |

## 📖 详细使用指南

### 🔄 同步流程操作

#### 步骤1：导入采集表
1. 在同步页面粘贴账号信息，支持格式：
   - 文本格式：`分享码 等级 标签`
   - 链接格式：`https://v.douyin.com/分享码`
   - JSON格式：`{"地址":"分享码","等级":"3"}`

2. 点击 **① 导入采集表** 按钮
3. 系统自动去重合并，显示导入结果

#### 步骤2：解析账号标识
1. 点击 **② 解析账号标识** 按钮
2. 系统调用API获取 sec_user_id
3. 实时显示处理进度和结果

#### 步骤3：同步账号表
1. 点击 **③ 同步账号表** 按钮
2. 系统获取账号详细信息（昵称、粉丝数等）
3. 自动回写飞书账号表

#### ⚡ 一键同步
- 点击 **⚡ 一键执行** 按钮
- 按顺序执行上述三个步骤
- 显示完整的执行进度和汇总结果

### 📊 数据表结构

#### 采集表（Collection Table）
| 字段 | 类型 | 说明 |
|-----|-----|-----|
| 记录ID | 文本 | 唯一标识 |
| 分享码 | 文本 | 抖音分享码 |
| 平台 | 单选 | 抖音/TikTok/小红书 |
| 等级 | 数字 | 1-4星评级 |
| 标签 | 多选 | 账号标签 |
| sec_user_id | 文本 | 账号唯一标识 |
| 已同步 | 复选框 | 是否已同步 |
| 同步错误 | 文本 | 错误信息 |

#### 账号表（Account Table）
| 字段 | 类型 | 说明 |
|-----|-----|-----|
| 记录ID | 文本 | 唯一标识 |
| 账号名称 | 文本 | 账号名称 |
| 平台 | 单选 | 抖音/TikTok/小红书 |
| 链接 | URL | 账号主页链接 |
| sec_user_id | 文本 | 账号唯一标识（唯一索引） |
| 等级 | 数字 | 从采集表复制 |
| 标签 | 多选 | 从采集表复制 |
| 昵称 | 文本 | API获取 |
| 粉丝数 | 数字 | API获取 |
| 作品数 | 数字 | API获取 |
| 签名 | 文本 | API获取 |
| 头像 | URL | API获取 |
| 已获取信息 | 复选框 | 是否已获取详细信息 |
| 启用 | 复选框 | 是否启用采集 |

### 🎯 高级功能

#### Cookie管理
- 支持多个Cookie轮换使用
- 自动检测Cookie失效并切换
- Cookie状态监控和管理界面

#### 定时任务
- 基于Cron表达式的定时采集
- 支持等级筛选和标签筛选
- 任务执行历史记录

#### 数据导出
- 支持Excel格式导出
- 可选择导出范围和字段
- 定时自动生成数据报表

## 🛠️ 技术架构

### 核心组件

#### 后端架构
- **FastAPI** - 现代化的Python Web框架
- **SQLite** - 轻量级关系型数据库
- **Jinja2** - 服务器端模板引擎
- **SSE** - 服务器推送事件（实时进度）

#### 同步引擎
- **SyncerV2** - 新一代同步引擎
- **去重合并** - 智能数据处理
- **飞书集成** - 双向数据同步
- **API调度** - 调用外部采集服务

#### 前端技术
- **原生JavaScript** - 轻量级交互
- **HTMX** - 现代前端交互库
- **响应式设计** - 支持移动端
- **实时进度** - SSE流式更新

### 数据流

```
用户输入
   ↓
数据解析 → 去重合并
   ↓
API调用 → 获取账号信息
   ↓
数据处理 → 填充详细信息
   ↓
飞书同步 → 更新表格数据
   ↓
本地缓存 → 存储处理结果
```

## 📚 API文档

### 同步API

#### 导入采集表
```http
POST /api/sync/v2/import
Content-Type: application/json

{
  "text": "iMLuCKjq 3 街拍 户外"
}
```

#### 更新采集表（SSE）
```http
POST /api/sync/v2/update-collection

流式响应：
data: {"type": "start", "message": "开始"}
data: {"type": "progress", "current": 1, "total": 10}
data: {"type": "complete", "success": true}
```

#### 同步账号表（SSE）
```http
POST /api/sync/v2/sync-account

流式响应：
data: {"type": "stats", "total": 10, "success": 5, "failed": 0}
data: {"type": "log", "level": "ok", "message": "✅ 账号A"}
```

#### 一键同步
```http
POST /api/sync/v2/all
Content-Type: application/json

{
  "text": "iMLuCKjq 3 街拍 户外"
}
```

### 数据库API

#### 获取表数据
```http
GET /api/database/table/{table_name}?limit=100&offset=0
```

支持的表名：
- `collection_cache` - 采集表缓存
- `account_cache` - 账号表缓存
- `cookie_cache` - Cookie缓存
- `collection_history` - 采集历史
- `scheduled_tasks` - 定时任务

#### 更新字段
```http
PATCH /api/database/table/{table_name}/record/{record_id}?field={field}&value={value}
```

### 飞书API

#### 同步到飞书
```http
POST /api/feishu/sync/to-feishu
```

#### 从飞书同步
```http
POST /api/feishu/sync/from-feishu
```

## 🧪 测试验证

### 运行测试
```bash
# 完整测试套件
python -m pytest tests/ -v

# 数据库测试
python -m pytest tests/test_database.py -v

# API端点测试  
python -m pytest tests/test_api.py -v
```

### 系统检查
```python
# 基础功能测试
python -c "
from app.core.database import Database
db = Database()
print('✅ 数据库连接正常')

from app.main import app
print('✅ FastAPI应用正常')

from app.core.config import Config
config = Config()
print('✅ 配置系统正常')
"
```

## 🤝 贡献指南

1. Fork 本项目
2. 创建特性分支 (`git checkout -b feature/amazing-feature`)
3. 提交更改 (`git commit -m 'Add amazing feature'`)
4. 推送到分支 (`git push origin feature/amazing-feature`)
5. 开启Pull Request

## 📄 许可证

本项目采用 MIT 许可证 - 查看 [LICENSE](LICENSE) 文件了解详情

## 🙏 致谢

- [TikTokDownloader](https://github.com/JoeanAmier/TikTokDownloader) - 抖音采集内核
- [XHS-Downloader](https://github.com/JoeanAmier/XHS-Downloader) - 小红书采集内核
- [FastAPI](https://fastapi.tiangolo.com/) - Web框架
- [飞书开放平台](https://open.feishu.cn/) - 多维表格API

---

<div align="center">

**[🔝 返回顶部](#doukhub---社交媒体数据采集管理平台)**

Made with ❤️ by the DoukHub Team

</div>