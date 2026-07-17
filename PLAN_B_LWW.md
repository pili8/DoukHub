# 方案 B 实施文档：LWW 真双向同步

> **文档目的**：让 AI 或开发者按照本文档实施「方案 B」，不需要查看对话历史。
>
> **当前分支**：`feature/sync-v3-lww`（已基于 `main` 分支的方案 A 稳定版）
>
> **方案 A 稳定版 Tag**：`v2.1.0-sync-a`（回退点）

---

## 📌 一、背景

### 当前状态（方案 A）

DoukHub 的飞书同步目前采用「字段级单向同步」（方案 A）：

- 每个字段固定归属一个「权威源」
- **☁️ 飞书赢**（人工字段）：飞书 → 本地 单向
- **💻 本地赢**（API 字段）：本地 → 飞书 单向
- **🔒 immutable**：创建后不变

### 方案 A 的问题

不是真正的双向同步。用户在「错的端」修改会被覆盖：

| 场景 | 方案 A 行为 | 用户期望 |
|---|---|---|
| 飞书改等级 | ✅ 同步到本地 | 同步到本地 |
| 本地改等级 | ❌ 被飞书覆盖 | 同步到飞书 |
| 本地改粉丝数（API） | ✅ 同步到飞书 | 同步到飞书 |
| 飞书改粉丝数 | ❌ 被本地覆盖 | 同步到本地 |

### 方案 B 目标

让真正需要双向的字段支持「两端都能改，谁后改谁赢」（LWW = Last Write Wins）。

---

## 🎯 二、方案 B 核心设计

### LWW 字段清单（6 个）

只对以下字段改为 LWW，其他字段保持单向：

| 字段 | 出现的表 | 原归属（方案 A） | 新归属（方案 B） |
|---|---|---|---|
| 等级 | 采集表 + 账号表 | feishu_wins | **lww** |
| 标签 | 采集表 + 账号表 | feishu_wins | **lww** |
| 备注 | 采集表 + 账号表 + Cookie表 | feishu_wins | **lww** |
| 启用 | 账号表 + Cookie表 | feishu_wins | **lww** |
| 采集类型 | 账号表 | feishu_wins | **lww** |
| 状态 | Cookie表 | local_wins | **lww** |

**字段归属规则（方案 B）：**
- **🔄 lww**：比较两端最后修改时间，谁新谁赢
- **💻 local_wins**：本地总是推送（不变）
- **🔒 immutable**：创建后不变（不变）
- **⚙️ sync_generated**：同步动作产生（不变）

**注意**：方案 B 取消了「☁️ feishu_wins」类型，原 feishu_wins 字段全部改为 lww。

### 时间戳比较规则

每条记录维护两个时间戳：

| 时间戳 | 位置 | 类型 | 维护方 |
|---|---|---|---|
| `local_updated_at` | 本地数据库 | DATETIME（秒级） | 每次 update_* 自动写入 |
| 「最后更新时间」 | 飞书表 | 飞书字段类型 1001（毫秒级） | 飞书系统自动维护 |

**比较逻辑**：
```
if local_updated_at > feishu_last_modified:
    # 本地后改，本地赢
    use local values for lww fields
else:
    # 飞书后改（或同时改），飞书赢
    use feishu values for lww fields
```

### 时钟漂移处理

- 飞书时间戳精度：毫秒
- 本地时间戳精度：秒（datetime.now()）
- 比较时统一转换为毫秒
- **不需要容差**：因为「人工字段」用户不会在 1 秒内连改两次
- **同时修改的边缘 case**（时间戳相等）：默认飞书赢（保守选择）

### 不精确场景（已知限制）

**场景**：用户在本地改等级，DoukHub 同时通过 API 改粉丝数

```
T0：初始状态
T1：用户在本地改等级=5 → 本地 local_updated_at=T1
T2：API 推送粉丝数 → 本地 local_updated_at=T2
同步等级：local_updated_at=T2 vs 飞书最后更新时间=T0
T2 > T0 → 本地赢，飞书等级变 5 ✓
```

这个场景下结果是对的。

**不精确的场景**：用户在飞书改等级 + DoukHub 同时推粉丝数到飞书

```
T0：初始状态
T1：用户在飞书改等级=4 → 飞书最后更新时间=T1
T2：DoukHub 推粉丝数 → 飞书最后更新时间=T2（更新）
T3：本地 local_updated_at=T3（推送后更新）
同步等级：本地 T3 vs 飞书 T2
T3 > T2 → 本地赢，但本地等级还是旧值 3
飞书等级被覆盖回 3 ❌（用户改的 4 丢了）
```

**这是方案 B 的已知限制**。原因是飞书的「最后更新时间」是记录级的，DoukHub 推送 API 字段也会更新它，污染了 LWW 字段的判断。

**缓解方案**（可选，复杂度高）：
- 本地维护字段级时间戳（每个 LWW 字段一个时间戳，存 JSON）
- 只在 LWW 字段被修改时更新对应的字段时间戳
- API 字段的修改不触发 LWW 字段时间戳更新

**建议**：先用记录级 LWW（基础版），如果有问题再升级到字段级。大多数实际场景下，用户不会在飞书改 LWW 字段的同时让 DoukHub 推送 API 字段。

---

## 📁 三、需要修改的文件

| 文件 | 改动类型 | 说明 |
|---|---|---|
| `app/core/database.py` | schema + 迁移 + CRUD | 加 `local_updated_at` 字段 |
| `app/core/feishu.py` | 字段定义 | 飞书表加「最后更新时间」字段 |
| `app/core/feishu_sync.py` | 核心逻辑 | 重写 `_compute_field_updates` |
| `app/templates/sync.html` | UI | 去掉字段归属警告，改为 LWW 说明 |
| `app/templates/database.html` | UI | 字段名旁标注类型（可选） |
| `tests/test_feishu_sync.py` | 测试 | 新增 LWW 测试用例 |

---

## 🔧 四、详细实施步骤

### 步骤 1：数据库 schema 变更

**文件**：`app/core/database.py`

**1.1 修改 `_init_database` 方法（约第 19 行）**

在三张同步表的 CREATE TABLE 里加 `local_updated_at` 字段：

```sql
-- collection_cache（约第 23 行）
CREATE TABLE IF NOT EXISTS collection_cache (
    record_id TEXT PRIMARY KEY,
    分享码 TEXT UNIQUE NOT NULL,
    -- ... 其他字段不变 ...
    synced BOOLEAN DEFAULT 0,
    local_updated_at DATETIME  -- 新增
)

-- account_cache（约第 46 行）
CREATE TABLE IF NOT EXISTS account_cache (
    record_id TEXT PRIMARY KEY,
    -- ... 其他字段不变 ...
    synced BOOLEAN DEFAULT 0,
    local_updated_at DATETIME  -- 新增
)

-- cookie_cache（约第 72 行）
CREATE TABLE IF NOT EXISTS cookie_cache (
    record_id TEXT PRIMARY KEY,
    -- ... 其他字段不变 ...
    synced BOOLEAN DEFAULT 0,
    local_updated_at DATETIME  -- 新增
)
```

**1.2 修改 `_migrate_legacy_columns` 方法（约第 151 行）**

在 `add_columns` 字典里为三张表添加新字段（旧库自动迁移）：

```python
# 在现有的 add_columns 定义后追加
for _tbl in ("collection_cache", "account_cache", "cookie_cache"):
    add_columns.setdefault(_tbl, []).append(
        ("local_updated_at", "DATETIME"),
    )
```

**1.3 修改 `update_collection` 方法（约第 290 行）**

```python
def update_collection(self, record_id: str, data: dict) -> bool:
    """更新采集表记录"""
    with self._connect() as conn:
        # 自动维护 local_updated_at（方案 B：用于 LWW 比较）
        if "local_updated_at" not in data:
            data["local_updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        set_clause = ", ".join([f"{k} = ?" for k in data.keys()])
        conn.execute(f"UPDATE collection_cache SET {set_clause} WHERE record_id = ?", list(data.values()) + [record_id])
        conn.commit()
        return True
```

**1.4 同样修改 `update_account`（约第 344 行）和 `update_cookie`（约第 395 行）**

加相同的 `local_updated_at` 自动维护逻辑。

**1.5 修改 `update_record_field`（约第 588 行，通用单字段更新）**

```python
def update_record_field(self, table: str, record_id: str, field: str, value: Any) -> bool:
    # ... 前面不变 ...
    with self._connect() as conn:
        set_clause = f'"{field}" = ?'
        params: list[Any] = [value]
        if "同步时间" in col_names and field != "同步时间":
            set_clause += ', "同步时间" = ?'
            params.append(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        # 新增：同步表自动维护 local_updated_at
        if "local_updated_at" in col_names and field != "local_updated_at":
            set_clause += ', "local_updated_at" = ?'
            params.append(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        # ... 后面不变 ...
```

### 步骤 2：飞书表加「最后更新时间」字段

**文件**：`app/core/feishu.py`

**2.1 修改 `_get_required_fields` 方法（约第 234 行）**

在三张表的字段定义里都加上「最后更新时间」：

```python
@staticmethod
def _get_required_fields(table_type: str) -> list[tuple]:
    """获取不同表类型的必需字段定义

    field_type: 1=文本, 2=数字, 3=单选, 4=多选, 5=日期, 7=复选框, 15=URL, 1001=最后更新时间
    """
    common_lww_field = ("最后更新时间", 1001, None)  # 飞书自动维护的系统字段

    if table_type == "collection":
        return [
            ("分享码", 1, None),
            # ... 其他字段不变 ...
            ("同步时间", 5, None),
            common_lww_field,  # 新增
        ]
    elif table_type == "cookie":
        return [
            ("Cookie", 1, None),
            # ... 其他字段不变 ...
            ("同步时间", 5, None),
            common_lww_field,  # 新增
        ]
    else:  # account
        return [
            # ... 其他字段不变 ...
            ("同步时间", 5, None),
            common_lww_field,  # 新增
        ]
```

**⚠️ 重要测试**：飞书 API 是否支持通过 `create_field` 创建类型 1001 的字段需要测试。如果不支持：
- 方案 1：在 `ensure_fields` 里捕获异常，提示用户手动在飞书表里添加「最后更新时间」字段
- 方案 2：跳过创建，依赖用户预先添加

**测试代码**：
```python
# 在 Python REPL 里测试
from app.core.feishu import FeishuClient
client = FeishuClient(app_id, app_secret)
result = client.create_field(app_token, table_id, "最后更新时间", 1001)
print(result)
# 如果返回 code=0，说明 API 支持创建
# 如果返回错误，需要用户手动添加
```

**2.2 飞书「最后更新时间」字段的读取**

飞书的「最后更新时间」字段值是毫秒时间戳（int），通过 `list_records` API 返回的 `fields` 里直接可读：

```python
feishu_record["fields"]["最后更新时间"]  # 例如 1721234567890
```

### 步骤 3：重写同步核心逻辑

**文件**：`app/core/feishu_sync.py`

**3.1 修改 `FIELD_OWNERSHIP`（约第 52 行）**

```python
FIELD_OWNERSHIP = {
    "collection_cache": {
        # LWW 字段：比较时间戳，谁新谁赢
        "lww": ["等级", "标签", "备注", "账号名称", "昵称", "粉丝数", "作品数", "签名", "头像"],
        # 本地赢字段：DoukHub 是权威源
        "local_wins": ["sec_user_id", "已同步"],
        # 元数据
        "immutable": ["分享码", "平台"],
        # 同步产生
        "sync_generated": ["同步错误", "同步时间"],
    },
    "account_cache": {
        "lww": ["等级", "标签", "备注", "启用", "采集类型", "账号名称"],
        "local_wins": ["sec_user_id", "昵称", "粉丝数", "作品数", "签名", "头像", "链接", "已获取信息"],
        "immutable": ["平台"],
        "sync_generated": ["同步时间"],
    },
    "cookie_cache": {
        "lww": ["启用", "备注", "状态"],
        "local_wins": ["验证时间"],
        "immutable": ["Cookie", "平台"],
        "sync_generated": ["同步时间"],
    },
}
```

**注意**：取消 `feishu_wins` 键，改为 `lww`。原 `feishu_wins` 字段全部移到 `lww`。

**3.2 新增 `_parse_local_timestamp` 辅助方法**

```python
@staticmethod
def _parse_local_timestamp(ts) -> int:
    """本地时间戳（字符串）转毫秒 int"""
    if not ts:
        return 0
    try:
        if isinstance(ts, (int, float)):
            return int(ts)
        dt = datetime.strptime(str(ts), "%Y-%m-%d %H:%M:%S")
        return int(dt.timestamp() * 1000)
    except Exception:
        return 0
```

**3.3 新增 `_get_feishu_timestamp` 辅助方法**

```python
def _get_feishu_timestamp(self, feishu_record: dict) -> int:
    """从飞书记录提取「最后更新时间」（毫秒）
    
    注意：飞书可能没建此字段（首次部署），返回 0
    """
    fields = feishu_record.get("fields", {})
    ts = fields.get("最后更新时间", 0)
    try:
        return int(ts) if ts else 0
    except (TypeError, ValueError):
        return 0
```

**3.4 重写 `_compute_field_updates`（约第 459 行）**

```python
def _compute_field_updates(self, db_table: str, local_record: dict, feishu_record: dict) -> tuple[dict, dict]:
    """按字段归属计算需要更新到两端的数据（方案 B：LWW）

    返回 (to_feishu_updates, to_local_updates)
    - to_feishu_updates: 需要推送到飞书的字段
    - to_local_updates: 需要更新到本地的字段
    """
    ownership = self.FIELD_OWNERSHIP.get(db_table, {})
    feishu_fields = feishu_record.get("fields", {})

    to_feishu = {}
    to_local = {}

    # === Step 1: 本地赢字段（不变，总是推送） ===
    for field in ownership.get("local_wins", []):
        local_val = local_record.get(field)
        feishu_val = feishu_fields.get(field)
        if not self._values_equal(field, local_val, feishu_val):
            if local_val not in (None, "", 0) or feishu_val in (None, "", 0):
                to_feishu[field] = local_val

    # === Step 2: LWW 字段（新增，按时间戳判断） ===
    local_ts = self._parse_local_timestamp(local_record.get("local_updated_at"))
    feishu_ts = self._get_feishu_timestamp(feishu_record)

    for field in ownership.get("lww", []):
        local_val = local_record.get(field)
        feishu_val = feishu_fields.get(field)
        if self._values_equal(field, local_val, feishu_val):
            continue  # 值相同，跳过

        # 时间戳缺失时的兜底处理
        if local_ts == 0 and feishu_ts == 0:
            # 两端都没时间戳（首次部署或字段未建），默认飞书赢
            winner = "feishu"
        elif local_ts == 0:
            winner = "feishu"
        elif feishu_ts == 0:
            winner = "local"
        elif local_ts > feishu_ts:
            winner = "local"
        else:
            winner = "feishu"  # 包括相等的情况

        if winner == "local":
            # 本地赢，推送本地值到飞书
            if local_val not in (None, "", 0):
                to_feishu[field] = local_val
        else:
            # 飞书赢，更新本地
            parsed = self._extract_field_value(field, feishu_val)
            if parsed is not None:
                to_local[field] = parsed

    return to_feishu, to_local
```

**3.5 新增 `_extract_field_value` 辅助方法**

```python
def _extract_field_value(self, field: str, feishu_val):
    """从飞书值提取并转换为本地格式"""
    if feishu_val is None:
        return None
    if field == "标签":
        parsed = self._normalize_tags(feishu_val)
        return json.dumps(parsed, ensure_ascii=False) if parsed else None
    if field in ("等级", "粉丝数", "作品数"):
        v = self._safe_int(feishu_val)
        return v if v > 0 else None
    if field in ("已同步", "已获取信息", "启用"):
        return self._safe_bool(feishu_val)
    if field == "状态":
        v = self._parse_text_value(feishu_val)
        return v if v else None
    # 默认文本
    v = self._parse_text_value(feishu_val)
    return v if v else None
```

**3.6 修改 `_sync_to_feishu` 和 `_sync_from_feishu` 里的字段推送**

更新飞书成功后，需要更新本地的 `local_updated_at` 吗？

**答案：不需要**。`local_updated_at` 只在本地修改时更新，不在同步推送时更新。否则会导致「飞书→本地」同步也更新 `local_updated_at`，让下次「本地→飞书」误判为"本地后改"。

**但是**：「本地→飞书」推送 LWW 字段成功后，飞书的「最后更新时间」会变。下次「飞书→本地」时，飞书时间戳更新了，但本地的 LWW 字段值和 `local_updated_at` 都没变。这是对的。

**关键**：`update_*` 方法只在「本地主动修改」时被调用，不在同步流程中被调用时更新 `local_updated_at`。

**问题**：当前 `_sync_from_feishu` 里会调用 `update_fn`（即 `update_collection` 等），如果 `update_*` 自动维护 `local_updated_at`，那「飞书→本地」同步也会更新 `local_updated_at`，导致下次「本地→飞书」误判。

**解决方案**：同步流程里更新本地时，**显式传入 `local_updated_at`**，使用飞书的时间戳：

```python
# 在 _sync_from_feishu 里（约第 762 行）
_, to_local_updates = self._compute_field_updates(db_table, existing, record)
if to_local_updates:
    # 用飞书的最后更新时间作为本地的 local_updated_at
    feishu_ts = self._get_feishu_timestamp(record)
    if feishu_ts:
        to_local_updates["local_updated_at"] = datetime.fromtimestamp(feishu_ts / 1000).strftime("%Y-%m-%d %H:%M:%S")
    to_local_updates["synced"] = True
    update_fn(rid or existing["record_id"], to_local_updates)
```

但 `update_fn` 会再次自动维护 `local_updated_at`（覆盖传入的值）。需要修改 `update_*` 方法：

**修改方案**：`update_*` 方法只在 `local_updated_at` 不在 data 里时自动维护：

```python
def update_collection(self, record_id: str, data: dict) -> bool:
    with self._connect() as conn:
        if "local_updated_at" not in data:
            data["local_updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        # ... 后续不变
```

这样：
- 本地主动修改：`update_collection(rid, {"等级": 4})` → 自动设置 `local_updated_at = now()`
- 同步流程更新：`update_collection(rid, {"等级": 4, "local_updated_at": feishu_ts})` → 用传入的值

### 步骤 4：UI 改进

**文件**：`app/templates/sync.html`

**4.1 修改字段归属说明（约第 145-167 行）**

把方案 A 的「字段归属规则」折叠卡片替换为方案 B 的说明：

```html
<div style="font-size:12px;color:#888;margin-bottom:12px;">
    双向同步采集表、账号表、Cookie表。<strong>方案 B（LWW 真双向）</strong>：
    人工字段（等级/标签/备注/启用/采集类型/状态）两端都能改，谁后改谁赢；
    API 字段（粉丝数/作品数等）以本地采集为准。
</div>
<details style="margin-bottom:12px;font-size:12px;color:#666;">
    <summary style="cursor:pointer;color:#3498db;">📋 字段同步规则（点击展开）</summary>
    <div style="margin-top:8px;padding:12px;background:#f8f9fa;border-radius:6px;font-size:11px;line-height:1.7;">
        <div style="margin-bottom:8px;"><strong>🔄 双向 LWW 字段（两端都能改）：</strong></div>
        <div style="margin-left:12px;margin-bottom:8px;">
            等级、标签、备注、启用、采集类型、状态<br>
            <span style="color:#888;">比较两端最后修改时间，谁新谁赢</span>
        </div>
        <div style="margin-bottom:8px;"><strong>💻 本地优先字段（DoukHub 采集为准）：</strong></div>
        <div style="margin-left:12px;">
            sec_user_id、链接、昵称、粉丝数、作品数、签名、头像、已获取信息、验证时间<br>
            <span style="color:#888;">在飞书改这些字段无效，会被本地覆盖</span>
        </div>
    </div>
</details>
```

**4.2 修改进度区域文案**（如有必要）

无需大改，进度展示逻辑不变。

### 步骤 5：测试

**文件**：`tests/test_feishu_sync.py`

**5.1 新增 LWW 时间戳测试**

```python
def test_lww_local_newer_wins(syncer, db):
    """LWW：本地时间戳更新 → 本地赢"""
    # 本地最近改过（local_updated_at 较新）
    db.insert_account({
        "record_id": "r1", "sec_user_id": "sec1",
        "等级": 5, "synced": True,
        "local_updated_at": "2026-07-17 12:00:00",  # 较新
    })
    local = db.get_account_by_id("r1")
    # 飞书的等级=3，最后更新时间较早
    feishu_record = {
        "record_id": "r1",
        "fields": {
            "sec_user_id": "sec1", "等级": 3,
            "最后更新时间": int(datetime(2026, 7, 17, 10, 0, 0).timestamp() * 1000),  # 较早
        },
    }
    to_feishu, to_local = syncer._compute_field_updates("account_cache", local, feishu_record)
    # 本地赢 → 推送等级=5 到飞书
    assert "等级" in to_feishu
    assert to_feishu["等级"] == 5
    # 不应该更新本地
    assert "等级" not in to_local


def test_lww_feishu_newer_wins(syncer, db):
    """LWW：飞书时间戳更新 → 飞书赢"""
    db.insert_account({
        "record_id": "r1", "sec_user_id": "sec1",
        "等级": 3, "synced": True,
        "local_updated_at": "2026-07-17 10:00:00",  # 较早
    })
    local = db.get_account_by_id("r1")
    feishu_record = {
        "record_id": "r1",
        "fields": {
            "sec_user_id": "sec1", "等级": 5,
            "最后更新时间": int(datetime(2026, 7, 17, 12, 0, 0).timestamp() * 1000),  # 较新
        },
    }
    to_feishu, to_local = syncer._compute_field_updates("account_cache", local, feishu_record)
    # 飞书赢 → 更新本地等级=5
    assert "等级" in to_local
    assert to_local["等级"] == 5
    # 不应该推送飞书
    assert "等级" not in to_feishu


def test_lww_equal_timestamps_feishu_wins(syncer, db):
    """LWW：时间戳相等 → 默认飞书赢（保守）"""
    ts_str = "2026-07-17 12:00:00"
    ts_ms = int(datetime(2026, 7, 17, 12, 0, 0).timestamp() * 1000)
    db.insert_account({
        "record_id": "r1", "sec_user_id": "sec1",
        "等级": 3, "synced": True,
        "local_updated_at": ts_str,
    })
    local = db.get_account_by_id("r1")
    feishu_record = {
        "record_id": "r1",
        "fields": {"sec_user_id": "sec1", "等级": 5, "最后更新时间": ts_ms},
    }
    to_feishu, to_local = syncer._compute_field_updates("account_cache", local, feishu_record)
    # 飞书赢
    assert "等级" in to_local
    assert to_local["等级"] == 5


def test_lww_missing_feishu_timestamp(syncer, db):
    """LWW：飞书没有时间戳字段（首次部署）→ 飞书赢（兜底）"""
    db.insert_account({
        "record_id": "r1", "sec_user_id": "sec1",
        "等级": 3, "synced": True,
        "local_updated_at": "2026-07-17 12:00:00",
    })
    local = db.get_account_by_id("r1")
    # 飞书没有「最后更新时间」字段
    feishu_record = {
        "record_id": "r1",
        "fields": {"sec_user_id": "sec1", "等级": 5},  # 没有时间戳
    }
    to_feishu, to_local = syncer._compute_field_updates("account_cache", local, feishu_record)
    # 默认飞书赢
    assert "等级" in to_local


def test_lww_local_wins_field_still_pushed(syncer, db):
    """LWW 字段如果本地赢，仍然推送到飞书"""
    db.insert_collection({
        "record_id": "r1", "分享码": "abc", "等级": 5,
        "synced": True,
        "local_updated_at": "2026-07-17 12:00:00",
    })
    local = db.get_collection_by_id("r1")
    feishu_record = {
        "record_id": "r1",
        "fields": {
            "分享码": "abc", "等级": 3,
            "最后更新时间": int(datetime(2026, 7, 17, 10, 0, 0).timestamp() * 1000),
        },
    }
    to_feishu, to_local = syncer._compute_field_updates("collection_cache", local, feishu_record)
    assert "等级" in to_feishu
    assert to_feishu["等级"] == 5


def test_lww_local_wins_fields_not_pushed_when_local_wins_type(syncer, db):
    """local_wins 字段不受 LWW 影响，总是推送"""
    db.insert_account({
        "record_id": "r1", "sec_user_id": "sec1",
        "粉丝数": 200, "synced": True,
        "local_updated_at": "2026-07-17 10:00:00",  # 较早
    })
    local = db.get_account_by_id("r1")
    feishu_record = {
        "record_id": "r1",
        "fields": {
            "sec_user_id": "sec1", "粉丝数": 100,
            "最后更新时间": int(datetime(2026, 7, 17, 12, 0, 0).timestamp() * 1000),  # 较新
        },
    }
    to_feishu, to_local = syncer._compute_field_updates("account_cache", local, feishu_record)
    # 粉丝数是 local_wins，不参与 LWW，总是推送
    assert "粉丝数" in to_feishu
    assert to_feishu["粉丝数"] == 200
```

**5.2 更新现有测试**

把现有测试里所有 `feishu_wins` 的引用改为 `lww`：

```python
# 把这类断言
assert "等级" in ownership["feishu_wins"]
# 改为
assert "等级" in ownership["lww"]
```

**5.3 端到端测试：飞书改等级同步到本地**

```python
def test_e2e_feishu_modify_level_syncs_to_local(syncer, db):
    """端到端：飞书改等级 → 同步 → 本地更新"""
    db.insert_account({
        "record_id": "r1", "sec_user_id": "sec1", "等级": 3,
        "synced": True,
        "local_updated_at": "2026-07-17 10:00:00",
    })
    # 飞书改了等级=5，时间戳较新
    syncer.feishu.get_all_records.return_value = [{
        "record_id": "r1",
        "fields": {
            "sec_user_id": "sec1", "等级": 5,
            "最后更新时间": int(datetime(2026, 7, 17, 12, 0, 0).timestamp() * 1000),
        },
    }]
    syncer.feishu.batch_create_records.return_value = {"code": 0, "data": {"records": []}}
    syncer.feishu.batch_update_records.return_value = {"code": 0}
    syncer.feishu.batch_delete_records.return_value = {"code": 0}

    syncer.sync_incremental()

    acc = db.get_account_by_id("r1")
    assert acc["等级"] == 5


def test_e2e_local_modify_level_syncs_to_feishu(syncer, db):
    """端到端：本地改等级 → 同步 → 飞书更新"""
    db.insert_account({
        "record_id": "r1", "sec_user_id": "sec1", "等级": 3,
        "synced": True,
        "local_updated_at": "2026-07-17 10:00:00",
    })
    # 本地改等级=5
    db.update_account("r1", {"等级": 5})  # 这会自动更新 local_updated_at

    # 飞书还是等级=3，时间戳较早
    syncer.feishu.get_all_records.return_value = [{
        "record_id": "r1",
        "fields": {
            "sec_user_id": "sec1", "等级": 3,
            "最后更新时间": int(datetime(2026, 7, 17, 10, 0, 0).timestamp() * 1000),
        },
    }]
    syncer.feishu.batch_create_records.return_value = {"code": 0, "data": {"records": []}}
    syncer.feishu.batch_update_records.return_value = {"code": 0}
    syncer.feishu.batch_delete_records.return_value = {"code": 0}

    syncer.sync_incremental()

    # 应该调用 batch_update_records 把等级=5 推送到飞书
    syncer.feishu.batch_update_records.assert_called()
```

---

## 🔄 五、实施顺序

建议按以下顺序实施，每步完成后跑测试：

1. **步骤 1**：数据库 schema 变更（加 `local_updated_at`）
   - 跑 `pytest tests/`，确保现有测试不破坏
2. **步骤 2**：飞书表字段定义（加「最后更新时间」）
   - 测试飞书 API 是否支持创建类型 1001 字段
3. **步骤 3**：重写 `_compute_field_updates`（核心）
   - 更新 FIELD_OWNERSHIP
   - 加辅助方法
   - 跑新增的 LWW 测试
4. **步骤 4**：修改 `_sync_from_feishu`（同步流程传入时间戳）
5. **步骤 5**：UI 改进
6. **步骤 6**：完整测试（`pytest tests/ -v`）
7. **步骤 7**：手动验证（启动 app，测试双向同步）

---

## ⚠️ 六、边界场景处理

### 场景 1：首次部署（飞书表没「最后更新时间」字段）

- `ensure_fields` 尝试创建，如果失败提示用户手动添加
- 同步逻辑兜底：飞书时间戳缺失时默认飞书赢（保守）

### 场景 2：本地数据库迁移（旧库没 `local_updated_at`）

- `_migrate_legacy_columns` 自动添加字段
- 旧记录的 `local_updated_at` 为 NULL
- LWW 比较：本地时间戳为 0 → 飞书赢（合理，因为旧数据应被飞书覆盖）

### 场景 3：同时修改（时钟漂移）

- 飞书时间戳精度毫秒，本地精度秒
- 同一秒内的修改可能误判
- **不处理**：人工字段用户不会 1 秒内连改

### 场景 4：飞书 API 推送污染时间戳

- DoukHub 推送 API 字段时，飞书的「最后更新时间」会更新
- 这会让下次同步时所有 LWW 字段都比较「飞书最新时间」
- 大多数场景没问题（如果飞书的 LWW 字段值没变，即使时间戳新也不会触发更新）
- 极端场景见上文「不精确场景」

### 场景 5：LWW 字段值两端相同

- `_values_equal` 返回 True → 跳过，不比较时间戳
- 性能优化，避免不必要的时间戳判断

---

## 🧪 七、验证清单

实施完成后，逐项验证：

### 自动化测试
- [ ] `pytest tests/` 全部通过
- [ ] 新增的 LWW 测试全部通过
- [ ] 现有测试不破坏（如有破坏，更新断言）

### 手动测试
- [ ] 启动 app，启动时自动同步不报错
- [ ] 飞书表自动出现「最后更新时间」字段
- [ ] 在飞书改等级 → 增量同步 → 本地等级更新
- [ ] 在本地数据管理页改等级 → 增量同步 → 飞书等级更新
- [ ] 在飞书改备注 → 同步 → 本地备注更新
- [ ] 在本地改备注 → 同步 → 飞书备注更新
- [ ] 在飞书改粉丝数 → 同步 → 本地粉丝数**不变**（API 字段本地赢）
- [ ] 删除测试（飞书删 / 本地删）仍正常工作
- [ ] 全盘同步仍正常工作

### 日志检查
- [ ] 同步日志显示 6 步进度
- [ ] LWW 字段冲突时有合理的日志（哪个端赢）

---

## ↩️ 八、回退方案

如果方案 B 有严重问题，回退到方案 A：

```bash
# 切回 main 分支（方案 A 稳定版）
git checkout main

# 或切到具体 tag
git checkout v2.1.0-sync-a

# 如果需要把 main 分支重置到稳定版（慎用）
git reset --hard v2.1.0-sync-a
```

**注意**：方案 B 添加的 `local_updated_at` 字段和飞书的「最后更新时间」字段都是向后兼容的，回退到方案 A 不会破坏数据。

---

## 📚 九、相关代码位置速查

| 改动点 | 文件 | 大约行号 |
|---|---|---|
| FIELD_OWNERSHIP | `app/core/feishu_sync.py` | 52 |
| `_compute_field_updates` | `app/core/feishu_sync.py` | 459 |
| `_sync_to_feishu` | `app/core/feishu_sync.py` | 506 |
| `_sync_from_feishu` | `app/core/feishu_sync.py` | 687 |
| `_init_database` (schema) | `app/core/database.py` | 19 |
| `_migrate_legacy_columns` | `app/core/database.py` | 151 |
| `update_collection` | `app/core/database.py` | 290 |
| `update_account` | `app/core/database.py` | 344 |
| `update_cookie` | `app/core/database.py` | 395 |
| `update_record_field` | `app/core/database.py` | 588 |
| `_get_required_fields` | `app/core/feishu.py` | 234 |
| 字段归属 UI | `app/templates/sync.html` | 145 |

---

## 💡 十、设计决策记录

### 为什么不用字段级时间戳？

- 实现复杂度高（每个 LWW 字段一个时间戳，或一个 JSON 字段）
- 飞书端不支持字段级时间戳
- 记录级 LWW 在 99% 场景下正确
- 极端不精确场景（DoukHub 推 API 字段污染飞书时间戳）实际很少发生

### 为什么 LWW 字段默认飞书赢（时间戳相等时）？

- 用户主要在飞书端修改（管理窗口）
- 飞书是「人工字段」的主要编辑端
- 保守选择，避免误覆盖用户在飞书的修改

### 为什么保留 `local_wins` 字段类型？

- API 字段（粉丝数/作品数等）由 DoukHub 通过 TTD API 获取
- 用户在飞书改这些字段没意义（数据会被下次 API 采集覆盖）
- 保留单向简化了逻辑

### 为什么取消 `feishu_wins` 字段类型？

- 方案 A 的 `feishu_wins` 实质是「飞书→本地单向」
- 方案 B 改为 LWW，原 `feishu_wins` 字段都改为 `lww`
- 这样用户在两端修改 LWW 字段都有效

---

## 🎯 总结

方案 B 的核心改动：
1. **数据库**：加 `local_updated_at` 字段
2. **飞书表**：加「最后更新时间」字段
3. **同步逻辑**：`_compute_field_updates` 重写，LWW 字段按时间戳比较
4. **字段归属**：`feishu_wins` → `lww`，其他不变
5. **UI**：更新字段归属说明

预计实施工作量：4-6 小时（含测试）。

实施完成后，用户在两端修改 LWW 字段都能正确同步，体验从「字段级单向」升级为「真双向」。
