# DoukHub 快速上手指南

## 🎯 3分钟快速开始

### 第一步：环境准备

1. **安装依赖**
```bash
# 安装Python依赖
pip install -r requirements.txt
```

2. **配置飞书**（必需）
- 登录[飞书开放平台](https://open.feishu.cn/)
- 创建企业自建应用
- 获取以下信息：
  - App ID
  - App Secret
  - App Token
- 创建3个多维表格：采集表、账号表、Cookie表

### 第二步：启动服务

```bash
# 启动DoukHub服务
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

### 第三步：配置和同步

1. **访问界面**
   - 打开浏览器访问：`http://localhost:8000`
   - 点击⚙️设置页面配置飞书信息

2. **开始同步**（以抖音账号为例）
   - 在同步页面粘贴数据：`iMLuCKjq 3 街拍 户外`
   - 点击 **⚡ 一键执行** 完成三步同步
   - 🎉 数据自动同步到飞书表格！

## 📖 详细操作说明

### 🔄 同步流程详解

#### 数据格式（任选一种）

**格式1：简洁文本**
```
iMLuCKjq 3 街拍 户外
abc123 2 个人
```

**格式2：完整链接**
```
https://v.douyin.com/iMLuCKjq
https://v.douyin.com/abc123
```

**格式3：JSON格式**
```json
{"地址":"iMLuCKjq","等级":"3","标签":["街拍","户外"]}
```

#### 操作步骤

1. **输入数据** - 在文本框中粘贴账号信息
2. **一键同步** - 点击⚡按钮自动执行
   - ✅ 步骤1：解析文本，去重合并
   - ✅ 步骤2：获取账号标识符
   - ✅ 步骤3：获取详细信息，写入飞书

### 🎛️ 高级配置

#### 配置文件位置
- **用户配置**：`~/.doukhub/config.json`
- **项目配置**：`./config/doukhub.json`（不推荐）

#### 配置示例
```json
{
  "feishu": {
    "app_id": "cli_a1b2c3d4e5f6g7h8",
    "app_secret": "your_app_secret_here",
    "app_token": "bjaL4TkksadsadTKm",
    "collection_table_id": "tblABC123",
    "account_table_id": "tblXYZ789",
    "cookie_table_id": "tblCookie456"
  },
  "downloader": {
    "tiktok_downloader_path": "./TikTokDownloader",
    "xhs_downloader_path": "./XHS-Downloader",
    "auto_start_services": true
  }
}
```

## 🔧 常见问题

### Q1：如何获取飞书表格ID？
**A**：在飞书多维表格的URL中查找：
```
https://example.feishu.cn/base/表格AppToken?table=表格TableID
```

### Q2：同步失败怎么办？
**A**：
1. 检查飞书配置是否正确
2. 查看页面底部日志信息
3. 确认TikTokDownloader服务是否运行
4. 检查网络连接

### Q3：如何提高成功率？
**A**：
1. 配置多个有效的Cookie
2. 控制同步频率（不要太快）
3. 使用高质量的账号链接

### Q4：数据如何备份？
**A**：
- 本地自动备份到`~/.doukhub/doukhub.db`
- 飞书表格也会保留数据
- 可在数据库页面导出Excel

## 📱 页面功能指南

### 首页导航
```
📊 仪表盘 → 查看服务状态和统计数据
🔄 同步     → 核心的同步功能
📥 采集     → 账号和单品数据采集
📋 记录     → 查看采集历史
💾 数据库   → 管理本地数据
⚙️ 设置     → 配置飞书和采集参数
```

### 同步页面按钮说明
```
📥 ① 导入采集表      → 步骤1：解析输入数据
🔄 ② 解析账号标识    → 步骤2：获取sec_user_id
📤 ③ 同步账号表      → 步骤3：获取详情并回写
⚡ 一键执行          → 按顺序执行所有步骤
⏹ 停止              → 中断当前操作
```

### 数据库管理
**操作权限**：
- ✅ 查看记录
- ✅ 搜索筛选
- ✅ 删除单条记录
- ✅ 清空整个表
- ✅ 导出Excel
- ✅ **修改已获取信息字段**（如前述功能）

## 🚨 重要提醒

### 安全提示
- 🔒 不要泄露飞书App Secret
- 🔒 Cookie文件包含敏感信息
- 🔒 配置文件建议设置适当权限

### 使用建议
- ⚡ 首次使用建议少量数据测试
- 🐢 避免过于频繁的API调用
- 📊 定期检查Cookie状态和有效性
- 💾 重要数据及时备份

### 性能优化
- 🔄 Cookie轮换提高成功率
- 📈 合理设置同步批次大小
- 🕒 使用定时任务分散请求
- 🧹 定期清理历史数据

## 🆘 获取帮助

### 排查步骤
1. 查看浏览器控制台错误(F12)
2. 检查服务日志输出
3. 验证配置文件格式
4. 测试飞书API连通性

### 问题反馈
- 检查[常见问题](#-常见问题)
- 查看服务状态页面
- 提供详细的错误信息

---

**🎊 恭喜！现在你已经掌握了DoukHub的基本使用方法。开始你的数据采集之旅吧！**

[🔝 返回顶部](#doukhub-快速上手指南)