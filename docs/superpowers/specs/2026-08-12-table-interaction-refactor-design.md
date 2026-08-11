# 表浏览页面交互重构设计文档

- 日期: 2026-08-12
- 状态: 待审阅
- 范围: `app/templates/table.html` + 4 套主题 CSS + 后端筛选支持

## 1. 背景与问题

表浏览页面(`app/templates/table.html`,基于 AG Grid 31)存在以下问题:

1. **排序易误触**:所有列默认 `sortable: true`,点击表头单元格直接触发排序。排序/筛选是低频操作,用户点表头往往是想选中、拖动或看列宽,误触率高。
2. **列菜单不规整**:AG Grid 默认列菜单(三个竖点触发)样式与 4 套自定义主题不协调,且 `table.html` 中直接引用 `var(--md-*)` 变量(50+ 处)仅在 obsidian/material 主题下存在,glass/slate 主题下菜单样式失效。
3. **筛选不便**:筛选藏在默认列菜单中,需多次点击;且后端不支持列级筛选(筛选仅作用于已加载页,配合服务端分页会不正确)。

## 2. 设计目标

- 点击表头**不触发排序**;排序/筛选统一通过表头右侧的竖点按钮弹出的**自写扁平菜单**完成。
- 菜单样式**只做一遍**,通过统一 CSS 变量适配全部 4 套主题(obsidian/material/glass/slate)。
- 筛选**服务端化**,作用于全量数据,与现有服务端分页正确配合。
- 遵循 YAGNI:本次不做"隐藏/显示列"、不做复杂筛选操作符。

## 3. 交互设计

### 3.1 表头交互

- 所有数据列 `sortable: false`(关闭点击排序)。
- 表头右侧保留竖点按钮(AG Grid 默认菜单按钮外观),点击后弹出**自写扁平菜单**。
- 当前排序列在菜单中高亮显示,表头上显示排序状态指示(小箭头)。

### 3.2 自写扁平菜单内容

点击竖点按钮弹出菜单,包含:

| 菜单项 | 行为 |
|--------|------|
| 升序 | 服务端按该列升序排序 |
| 降序 | 服务端按该列降序排序 |
| 清除排序 | 恢复默认排序(若当前已排序) |
| 分隔线 | - |
| 筛选… | 展开筛选输入区(输入框 + 操作符选择) |

筛选输入区包含:
- 操作符:包含(contains)/ 等于(equals)
- 值输入框
- 应用 / 清除 按钮

### 3.3 排序/筛选状态

- 前端维护 `currentSortField`、`currentSortOrder`、`currentFilterField`、`currentFilterOp`、`currentFilterValue` 状态。
- `showTable()` 请求时携带这些参数。
- 变更排序/筛选后重置到第 1 页。
- 表头显示排序指示(当前排序字段)。

## 4. 技术方案

### 4.1 前端:自定义表头组件

用 AG Grid 自定义 header component 替换默认表头:

- `headerComponent` 渲染:列名 + 排序指示 + 竖点按钮。
- 竖点按钮点击 → 弹出自写菜单 DOM(绝对定位,floating 容器)。
- 菜单点击外部区域关闭。
- 菜单项动作:
  - 排序:调 `showTable()` 带 `sort_field`/`sort_order` 参数(服务端排序)。
  - 筛选:展开输入区,应用后调 `showTable()` 带 `filter_*` 参数。

替代方案:不启用 AG Grid 自带 menu(`suppressMenu: false` 不动),通过 `getMainMenuItems` 定制菜单项。若定制能力不足,则完全自写菜单 DOM(推荐,样式可控)。

### 4.2 前端:主题通用化

- `table.html` 中所有 `var(--md-*)` 引用改为通用变量。
- 需要新增的通用变量(在 4 套主题中补齐):

| 变量 | 用途 | obsidian/material 现有来源 | glass/slate 需补充 |
|------|------|---------------------------|-------------------|
| `--surface-overlay` | 菜单/浮层背景 | `--md-surface-container-high` | 补齐 |
| `--surface-raised` | 悬浮卡片背景 | `--md-surface-container` | 补齐 |
| `--menu-hover` | 菜单项 hover | `--md-primary-container` | 补齐 |
| `--text-tertiary` | 次要文字 | `--md-on-surface-variant` | 补齐 |

- 每套主题各补 3~5 行变量,保持既有设计语义。
- 校验:4 套主题下菜单观感一致。

### 4.3 后端:筛选支持

`app/core/database.py` 的 `query_table()` 扩展:

- 新增参数 `filter_field: Optional[str]`、`filter_value: Optional[str]`、`filter_op: Optional[str]`。
- `filter_op` 支持 `contains` / `equals`:
  - contains: `CAST("field" AS TEXT) LIKE '%value%'`
  - equals: `CAST("field" AS TEXT) = value`
- 与现有 `search` 参数组合(AND 关系)。
- 字段不存在时忽略该筛选(与现有 `sort_field` 行为一致)。

`app/main.py` 的 `GET /api/database/table/{table_name}` 透传新参数。

### 4.4 前端:数据加载

`showTable()` 的 URL 构造增加:

```
&sort_field=<field>&sort_order=<asc|desc>
&filter_field=<field>&filter_value=<value>&filter_op=<contains|equals>
```

## 5. 数据结构与数据流

```
用户点击竖点按钮
  → 渲染自写菜单(浮层)
  → 用户选择排序 / 输入筛选
  → 更新前端状态变量
  → showTable(currentTable, 1)
  → GET /api/database/table/{table}?limit&offset&sort_*&filter_*
  → 后端 query_table 构造 WHERE/ORDER BY
  → 返回 records + total
  → 渲染表格 + 分页 + 表头排序指示
```

## 6. 错误处理

- 筛选字段不存在:后端忽略该筛选,返回全部数据(与 sort_field 行为一致)。
- API 失败:沿用现有 `showToast` 错误提示,表格显示空态。
- 菜单点击外部区域 / Esc:关闭菜单。

## 7. 测试计划

- 后端:`tests/` 中新增 `query_table` 筛选的单元测试(contains/equals、与 search 组合、字段不存在)。
- 前端:手动验证
  - 点表头单元格不排序。
  - 竖点菜单弹开/关闭、外部点击关闭。
  - 排序升序/降序/清除,服务端生效,翻页保持。
  - 筛选 contains/equals,作用于全量数据。
  - 4 套主题下菜单观感一致。

## 8. 范围外(YAGNI)

- 隐藏/显示列管理。
- 复杂筛选操作符(>、<、between、多列组合筛选)。
- 列拖拽排序、列宽持久化。