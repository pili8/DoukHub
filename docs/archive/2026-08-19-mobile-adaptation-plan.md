# DoukHub 移动端网页适配 · 实施方案

> 本文档自包含，面向无上下文的执行 AI。阅读本文即掌握全部背景，无需其他对话记录。
> 项目根目录：`D:\AI\DoukHub`（下文相对路径均基于此）。

## 0. 背景与目标

DoukHub 是本地工具：FastAPI + Jinja2 模板 + 前端 SPA 局部刷新。用户希望在手机（与电脑同一局域网）上通过浏览器舒适使用，重点保障**采集相关页面**的查看与轻操作。

- 架构要点：`app/templates/base.html`（约 439 行处）的 `loadPage` 函数 fetch 整页 HTML → DOMParser 抽取 `#main-content` 与 `#page-scripts` 局部替换 → `history.pushState`。侧边栏 `nav.sidebar` 不参与局部刷新，常驻。
- 服务已绑定 `0.0.0.0:2999`（`app/main.py` 约 3944 行），手机连同一 Wi-Fi 访问 `http://<电脑IP>:2999` 即可。
- **后端、API、数据库一律不改。** 全部改动限于前端模板 / CSS / 少量内联 JS。
- 现有响应式基础：`base.html` 已有 viewport meta；`app/static/css/style.css` 已有 1024 / 768 / 720 / 420px 多档断点，卡片、表单、按钮、toast 已做缩放。

## 1. 分步任务（按序连续执行，全部完成后交用户一次性终验，中途不向用户汇报）

### 步骤 1：全站基础层 —— 侧边栏抽屉化 + 触控优化

**文件：`app/templates/base.html`、`app/static/css/style.css`**

1.1 抽屉导航（核心）：
- 现状：`style.css` 第 1293 行 `@media (max-width: 768px)` 下侧边栏仅缩为 `--sidebar-w-mobile: 160px`（变量定义在第 174 行），手机上仍占约半屏。
- 目标：≤768px 时侧边栏变为固定定位抽屉（默认 `transform: translateX(-100%)` 隐藏），顶部导航栏出现汉堡按钮，点击滑出并显示半透明遮罩；点击遮罩、再次点汉堡、或导航项点击（loadPage 触发）后自动收回。
- 实现要点：
  - 在 `base.html` 顶部主内容区加汉堡按钮（仅小屏显示），侧边栏外加 `<div class="sidebar-mask">` 遮罩元素。
  - JS 在 base.html 底部脚本区实现（侧边栏常驻不随 loadPage 替换，事件绑定一次即可，不会被局部刷新冲掉）。
  - 利用现有 `#sidebar-collapse-btn`（第 26 行）的折叠状态逻辑时注意：小屏抽屉态与桌面折叠态是两套状态，勿互相污染；桌面端（>768px）行为保持现状不变。
  - 侧边栏内含三个折叠分组子菜单（账号/采集/数据），确认小屏下默认展开或点击可展开，触控目标≥44px。
- 自查标准：手机上任意页面可开合抽屉、切换页面后抽屉自动收起、桌面端显示与现在完全一致。（自查通过即继续下一步，不等待用户）

1.2 触控与 hover 修复（全站）：
- 排查 `style.css` 及各模板中的 hover 显示逻辑（如 `table.html` 第 497 行附近 `.tbl-row:hover .tbl-action-btn { opacity: 1 }`），在触摸设备（`@media (hover: none)`）下改为常显。
- `style.css` 中 `btn-sm`、`icon-btn` 等小控件在小屏断点下 `min-height/min-width ≥ 40px`。

### 步骤 2：采集三页精调（用户核心场景）

2.1 增量采集 `app/templates/collect.html`（改动最大）：
- 批次历史表（第 117–138 行，11 列）：≤768px 时不渲染宽表格，改为卡片列表。该表由 JS 渲染（`renderBatchHistory`，约第 588 行起的渲染函数），在渲染函数中按 `window.matchMedia('(max-width: 768px)')` 分支输出卡片 HTML（卡片字段：批次号、方案、状态、成功/失败/跳过、时间；点击卡片展开完整信息或保留操作按钮）。退路：若卡片化复杂度超预期，改为精简列（批次/方案/状态/成功/失败/时间）+ 保留 `.table-scroll` 横滑。
- `preset-action-row`（第 33 行，方案下拉 + 开始采集按钮并排）：小屏改纵向堆叠，"开始采集"按钮铺满宽。
- 运行日志 `<pre class="workflow-log">`（约第 627 行）：小屏字号适配、`-webkit-overflow-scrolling: touch`、限制最大高度。
- 运行状态区（进度条、账号明细表第 623 行）：进度条自适应已基本可用；账号明细 4 列小表可保留横滑即可。

2.2 单作品采集 `app/templates/collect_detail.html`（改动最小）：
- `mode-ops-row`（第 39–48 行，解析/一键下载/API请求/下载设置四按钮）：≤768px 改为两行或网格铺满，主按钮（解析/一键下载）保持明显。
- 其余为纵向流式布局，基本不动；确认 `settings-block` 折叠面板与模态框（`#task-modal` 类似物）在小屏可正常滚动。

2.3 采集概览 `app/templates/collect/overview.html`：
- 最近批次表（第 43–60 行，7 列）：小屏精简列（批次/状态/成功/失败/时间）或卡片化，任选实现简单者。
- `page-actions` 两个按钮小屏换行；指标区 `workflow-metrics` 已自适应，验证即可。

### 步骤 3：其他页面巡检修复（不精调）

- 手机视口逐页打开：dashboard、history、accounts、database、duplicates、schedule、settings、status、sync/* 各页。
- 只修"排版崩坏、功能不可用"级别的问题（如固定宽度元素溢出、表格无横滑容器、模态框超出视口），不做视觉精调。
- 巡检方式：可用浏览器开发者工具移动模拟 + 真机抽查；发现问题逐条记录到本文档末尾"巡检记录"节再修。

### 步骤 4：PWA（可选，用户确认后再做）

- 新增 `app/static/manifest.webmanifest`：name=DoukHub、standalone 显示、192/512 图标（可先用现有 `TikTokDownloader/static/images/` 或项目内任意占位图，或生成纯色字标 PNG）。
- `base.html` `<head>` 加 `<link rel="manifest">` 与 apple-touch-icon、theme-color。
- Service Worker 不做（局域网工具，无离线价值）。
- 自查标准：manifest 与图标链接正确加载，无控制台报错。（真机添加主屏由用户终验时操作）

## 2. 硬性约束

1. 禁止改动：`app/main.py`、`app/api/`、`app/core/`、`app/services/`、数据库、任何 `/api/*` 接口行为。
2. 禁止引入新前端框架/构建步骤（不加 npm 包，不加打包工具）；CSS 写入现有 `style.css` 或模板内现有 `<style>` 块，JS 写入模板现有 script 区。
3. 断点统一沿用现有体系：主断点 768px，辅以 420px；新增媒体查询优先用 `@media (max-width: 768px)` 与 `@media (hover: none)`，避免新造断点值。
4. 桌面端（>768px）显示与交互必须与改动前完全一致；每步完成后用桌面浏览器回归验证。
5. 改动 `loadPage` 逻辑需谨慎：不得破坏 `#main-content` / `#page-scripts` 替换与脚本重执行机制。
6. 代码风格与现有保持一致（缩进、中文注释习惯、lucide 图标用法）。

## 3. 验证方法

- 桌面回归：每步完成后 `start.bat` 启动服务，浏览器开 `http://127.0.0.1:2999` 逐页检查。
- 移动模拟：开发者工具 iPhone/Android 视口（375px、420px 宽重点测）。
- 真机：用户手机连同一 Wi-Fi 访问 `http://<电脑IP>:2999` 实测（执行 AI 可用 paw browser-action 截图自查）。
- git：开工前确认工作区干净，每步骤单独提交，commit message 格式 `mobile: 步骤N-简述`。

## 4. 已知风险与退路

| 风险 | 退路 |
|---|---|
| 批次历史卡片化 JS 改动超预期 | 退为精简列+横滑表格（步骤 2.1 已注明） |
| 抽屉状态与桌面折叠状态互相污染 | 抽屉态用独立 class（如 `sidebar-open`）与 data 属性，不共用折叠变量 |
| 国产浏览器 PWA 添加后仍是快捷方式 | 已知限制，不投入适配，建议用户用 Chrome/Safari |

## 5. 巡检记录（步骤 3 执行时填写）

**巡检方式**：移动视口（390×844）逐页打开，检测页面级横向溢出（`scrollWidth > innerWidth`）及排版崩坏。

**巡检范围**：/（dashboard）、/history、/accounts、/database、/table、/duplicates、/schedule、/settings、/status、/sync/overview、/sync/import、/sync/resolve、/sync/account、/sync/refresh。

**结论**：全部 14 个页面在 390px 视口下 `scrollWidth ≤ innerWidth`，无页面级横向溢出；数据表均位于 `.table-scroll` 横滑容器内（table 页为 AG Grid 自带滚动）；常见模态框（`dh-modal-box` 90% 宽、`#task-modal` 90vw、`single-dir/template-modal` 92vw）均不超出视口并内部可滚动。**未发现"排版崩坏 / 功能不可用"级别问题，无需修复。**
