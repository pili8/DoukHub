# TTD-Backed Collection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build batch account collection on TTD's native terminal downloader and add DoukHub-managed flexible single-work downloads.

**Architecture:** DoukHub owns account selection, incremental dates, batch state, and TTD `accounts_urls` generation. A dedicated TTD terminal child process performs bulk downloads and emits JSON account markers, while the existing TTD Web API remains resident for metadata. Single-work files are downloaded by DoukHub into user-selected directories and never touch TTD's `download_data`.

**Tech Stack:** FastAPI, SQLite, asyncio subprocesses, httpx, Jinja2 templates, pytest.

## Global Constraints

- TTD Web API remains resident; bulk download runs in a separate TTD terminal process.
- At most one TTD bulk download process runs at a time.
- DoukHub only replaces `accounts_urls` or `accounts_urls_tiktok`; all other TTD settings are preserved.
- TTD settings updates are atomic.
- Douyin account URL is `https://www.douyin.com/user/{sec_user_id}`.
- TikTok accounts require an existing profile URL.
- `tab` is always `post` in phase one.
- Concrete `earliest` dates use `YYYY/MM/DD`.
- Numeric `earliest` values mean days before today.
- Normal accounts: first run full, later run from `last_collected_at - 1 day`.
- Accounts with `collect_window_days` always use that fixed window.
- Only confirmed successful items update `last_collected_at`.
- DoukHub never writes, edits, or clears TTD `download_data`.
- Single-work downloads do not participate in TTD archive deduplication.
- Do not build a full work-metadata table.
- Do not add Xiaohongshu to the new batch workflow in this phase.

---

### Task 1: Collection Batch Schema And Repository

**Files:**

- Modify: `app/core/database.py`
- Test: `tests/test_collection_batches.py`

**Interfaces:**

- Consumes: existing `Database._connect()`, `_init_database()`, and `_migrate_legacy_columns()`.
- Produces:
  - `account_cache.last_collected_at: DATETIME`
  - `account_cache.collect_window_days: INTEGER`
  - `Database.create_collection_batch(batch_id, filter_json, platform, log_path, items)`
  - `Database.get_collection_batch(batch_id) -> dict | None`
  - `Database.get_active_collection_batch() -> dict | None`
  - `Database.list_collection_batches(limit=20) -> list[dict]`
  - `Database.get_collection_batch_items(batch_id) -> list[dict]`
  - `Database.update_collection_batch(batch_id, **fields) -> bool`
  - `Database.update_collection_batch_item(item_id, **fields) -> bool`
  - `Database.find_collection_batch_item(batch_id, sec_user_id) -> dict | None`
  - `Database.refresh_collection_batch_counts(batch_id) -> dict`

- [ ] **Step 1: Write the failing schema and repository tests**

Create `tests/test_collection_batches.py`:

```python
import pathlib
import tempfile

import pytest

from app.core.database import Database


@pytest.fixture
def db():
    path = pathlib.Path(tempfile.mkdtemp()) / "doukhub.db"
    return Database(db_path=path)


def test_collection_account_fields_are_added(db):
    names = {c["name"] for c in db.get_table_schema("account_cache")}
    assert "last_collected_at" in names
    assert "collect_window_days" in names


def test_existing_account_database_is_migrated(tmp_path):
    import sqlite3

    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
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
                已获取信息 BOOLEAN DEFAULT 0,
                获取错误 TEXT,
                同步时间 DATETIME,
                created_at DATETIME,
                is_deleted BOOLEAN DEFAULT 0,
                deleted_at DATETIME,
                synced BOOLEAN DEFAULT 0,
                local_updated_at DATETIME
            )
            """
        )
        conn.execute(
            "INSERT INTO account_cache(record_id, sec_user_id) VALUES ('a1', 'sec1')"
        )

    database = Database(db_path=path)
    account = database.get_account_by_id("a1")
    assert account["last_collected_at"] is None
    assert account["collect_window_days"] is None


def test_create_and_query_collection_batch(db):
    items = [
        {
            "account_record_id": "a1",
            "sec_user_id": "sec1",
            "account_name": "一号",
            "platform": "douyin",
            "mark": "一号",
            "url": "https://www.douyin.com/user/sec1",
            "earliest": "",
        },
        {
            "account_record_id": "a2",
            "sec_user_id": "sec2",
            "account_name": "二号",
            "platform": "douyin",
            "mark": "二号",
            "url": "https://www.douyin.com/user/sec2",
            "earliest": "2026/08/01",
        },
    ]
    db.create_collection_batch(
        batch_id="batch1",
        filter_json='{"rating_min":3}',
        platform="douyin",
        log_path="/tmp/batch1.log",
        items=items,
    )

    batch = db.get_collection_batch("batch1")
    assert batch["status"] == "pending"
    assert batch["total_accounts"] == 2
    assert batch["process_pid"] is None

    queried = db.get_collection_batch_items("batch1")
    assert [row["sec_user_id"] for row in queried] == ["sec1", "sec2"]
    assert queried[1]["earliest"] == "2026/08/01"
    assert db.find_collection_batch_item("batch1", "sec2")["account_name"] == "二号"


def test_update_batch_and_refresh_counts(db):
    db.create_collection_batch(
        batch_id="batch1",
        filter_json="{}",
        platform="douyin",
        log_path="/tmp/batch1.log",
        items=[
            {
                "account_record_id": "a1",
                "sec_user_id": "sec1",
                "account_name": "一号",
                "platform": "douyin",
                "mark": "一号",
                "url": "https://www.douyin.com/user/sec1",
                "earliest": "",
            }
        ],
    )
    item = db.find_collection_batch_item("batch1", "sec1")
    assert db.update_collection_batch_item(item["id"], status="success", message="OK")
    assert db.update_collection_batch("batch1", status="running", process_pid=123)

    counts = db.refresh_collection_batch_counts("batch1")
    assert counts == {"success": 1, "failed": 0, "skipped": 0}
    assert db.get_collection_batch("batch1")["success_accounts"] == 1
    assert db.get_active_collection_batch()["id"] == "batch1"


def test_list_batches_orders_newest_first(db):
    for index in range(2):
        db.create_collection_batch(
            batch_id=f"batch{index}",
            filter_json="{}",
            platform="douyin",
            log_path=f"/tmp/batch{index}.log",
            items=[],
        )
    assert [batch["id"] for batch in db.list_collection_batches()] == [
        "batch1",
        "batch0",
    ]
```

- [ ] **Step 2: Run the failing tests**

Run:

```powershell
.\venv\Scripts\python.exe -m pytest tests\test_collection_batches.py -v
```

Expected: all tests fail because the fields, tables, and repository methods do not exist.

- [ ] **Step 3: Add schema and migrations**

In `app/core/database.py`, add these columns to the `CREATE TABLE IF NOT EXISTS account_cache` statement after `local_updated_at`:

```sql
                    last_collected_at DATETIME,
                    collect_window_days INTEGER
```

Add these table definitions in `_init_database()` after `collection_history`:

```python
            conn.execute("""
                CREATE TABLE IF NOT EXISTS collection_batches (
                    id TEXT PRIMARY KEY,
                    status TEXT NOT NULL DEFAULT 'pending',
                    filter_json TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    process_pid INTEGER,
                    log_path TEXT,
                    started_at DATETIME,
                    finished_at DATETIME,
                    total_accounts INTEGER DEFAULT 0,
                    success_accounts INTEGER DEFAULT 0,
                    failed_accounts INTEGER DEFAULT 0,
                    skipped_accounts INTEGER DEFAULT 0,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS collection_batch_items (
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
                )
            """)
```

In `_migrate_legacy_columns()`, extend the existing `account_cache` additions:

```python
            "account_cache": [
                ("启用", "BOOLEAN DEFAULT 1"),
                ("采集类型", "TEXT DEFAULT '发布'"),
                ("获取错误", "TEXT"),
                ("last_collected_at", "DATETIME"),
                ("collect_window_days", "INTEGER"),
            ],
```

Add indexes to the existing index list:

```python
                "CREATE INDEX IF NOT EXISTS idx_collection_batch_status ON collection_batches(status, created_at)",
                "CREATE INDEX IF NOT EXISTS idx_collection_batch_item_batch ON collection_batch_items(batch_id, sec_user_id)",
```

- [ ] **Step 4: Add repository methods**

Add this section after the account cache methods in `Database`:

```python
    # ========== 采集批次操作 ==========

    _BATCH_FIELDS = {
        "status", "process_pid", "log_path", "started_at", "finished_at",
        "total_accounts", "success_accounts", "failed_accounts", "skipped_accounts",
    }
    _BATCH_ITEM_FIELDS = {
        "status", "message", "started_at", "finished_at",
    }

    def create_collection_batch(
        self,
        batch_id: str,
        filter_json: str,
        platform: str,
        log_path: str,
        items: list[dict],
    ) -> None:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO collection_batches
                (id, status, filter_json, platform, log_path, total_accounts, created_at)
                VALUES (?, 'pending', ?, ?, ?, ?, ?)
                """,
                (batch_id, filter_json, platform, log_path, len(items), now),
            )
            conn.executemany(
                """
                INSERT INTO collection_batch_items
                (batch_id, account_record_id, sec_user_id, account_name, platform,
                 mark, url, earliest, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending')
                """,
                [
                    (
                        batch_id,
                        item.get("account_record_id"),
                        item["sec_user_id"],
                        item.get("account_name", ""),
                        item.get("platform", platform),
                        item.get("mark", ""),
                        item.get("url", ""),
                        str(item.get("earliest", "")),
                    )
                    for item in items
                ],
            )
            conn.commit()

    def get_collection_batch(self, batch_id: str) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM collection_batches WHERE id = ?", (batch_id,)
            ).fetchone()
            return dict(row) if row else None

    def get_active_collection_batch(self) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM collection_batches
                WHERE status IN ('pending', 'running', 'cancelling')
                ORDER BY created_at, id
                LIMIT 1
                """
            ).fetchone()
            return dict(row) if row else None

    def list_collection_batches(self, limit: int = 20) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM collection_batches ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(row) for row in rows]

    def get_collection_batch_items(self, batch_id: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM collection_batch_items WHERE batch_id = ? ORDER BY id",
                (batch_id,),
            ).fetchall()
            return [dict(row) for row in rows]

    def find_collection_batch_item(
        self, batch_id: str, sec_user_id: str
    ) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM collection_batch_items
                WHERE batch_id = ? AND sec_user_id = ?
                LIMIT 1
                """,
                (batch_id, sec_user_id),
            ).fetchone()
            return dict(row) if row else None

    def update_collection_batch(self, batch_id: str, **fields) -> bool:
        valid = {key: value for key, value in fields.items() if key in self._BATCH_FIELDS}
        if not valid:
            return False
        assignments = ", ".join(f"{key} = ?" for key in valid)
        with self._connect() as conn:
            cursor = conn.execute(
                f"UPDATE collection_batches SET {assignments} WHERE id = ?",
                list(valid.values()) + [batch_id],
            )
            conn.commit()
            return cursor.rowcount > 0

    def update_collection_batch_item(self, item_id: int, **fields) -> bool:
        valid = {key: value for key, value in fields.items() if key in self._BATCH_ITEM_FIELDS}
        if not valid:
            return False
        assignments = ", ".join(f"{key} = ?" for key in valid)
        with self._connect() as conn:
            cursor = conn.execute(
                f"UPDATE collection_batch_items SET {assignments} WHERE id = ?",
                list(valid.values()) + [item_id],
            )
            conn.commit()
            return cursor.rowcount > 0

    def refresh_collection_batch_counts(self, batch_id: str) -> dict:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT
                    SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END) AS success,
                    SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed,
                    SUM(CASE WHEN status = 'skipped' THEN 1 ELSE 0 END) AS skipped
                FROM collection_batch_items
                WHERE batch_id = ?
                """,
                (batch_id,),
            ).fetchone()
            counts = {
                "success": row["success"] or 0,
                "failed": row["failed"] or 0,
                "skipped": row["skipped"] or 0,
            }
            conn.execute(
                """
                UPDATE collection_batches
                SET success_accounts = ?, failed_accounts = ?, skipped_accounts = ?
                WHERE id = ?
                """,
                (counts["success"], counts["failed"], counts["skipped"], batch_id),
            )
            conn.commit()
            return counts
```

- [ ] **Step 5: Run tests**

```powershell
.\venv\Scripts\python.exe -m pytest tests\test_collection_batches.py -v
```

Expected: 5 tests pass.

- [ ] **Step 6: Commit**

```powershell
git add app\core\database.py tests\test_collection_batches.py
git commit -m "feat: persist collection batches"
```

---

### Task 2: Account Selection And TTD Settings Planner

**Files:**

- Create: `app/core/collection_planner.py`
- Test: `tests/test_collection_planner.py`

**Interfaces:**

- Consumes `Database.get_all_accounts()` rows with `账号名称`, `平台`, `链接`, `sec_user_id`, `等级`, `标签`, `启用`, `last_collected_at`, and `collect_window_days`.
- Produces:
  - `PlannedAccount`
  - `plan_collection(accounts, rating_min=3, tags=None, account_names="", record_ids=None, platform="douyin", mode="incremental", today=None) -> list[PlannedAccount]`
  - `write_ttd_accounts(settings_path, platform, planned) -> list[dict]`

- [ ] **Step 1: Write failing planner tests**

Create `tests/test_collection_planner.py`:

```python
import json
from datetime import date

from app.core.collection_planner import plan_collection, write_ttd_accounts


def account(**overrides):
    data = {
        "record_id": "a1",
        "账号名称": "一号",
        "平台": "抖音",
        "链接": "",
        "sec_user_id": "sec1",
        "等级": 4,
        "标签": "多, 个人",
        "启用": 1,
        "last_collected_at": None,
        "collect_window_days": None,
    }
    data.update(overrides)
    return data


def test_filters_enabled_douyin_accounts_and_sorts_by_rating():
    planned = plan_collection(
        [
            account(record_id="a2", 账号名称="二号", sec_user_id="sec2", 等级=3),
            account(record_id="a1", 等级=4),
            account(record_id="a3", 账号名称="三号", sec_user_id="sec3", 等级=5, 启用=0),
            account(record_id="a4", 账号名称="四号", sec_user_id="", 等级=5),
        ],
        rating_min=3,
    )
    assert [item.sec_user_id for item in planned] == ["sec1", "sec2"]
    assert all(item.status == "pending" for item in planned)


def test_tag_and_name_filters():
    planned = plan_collection(
        [
            account(sec_user_id="sec1", 账号名称="一号", 标签="多"),
            account(record_id="a2", 账号名称="二号", sec_user_id="sec2", 标签="个人"),
        ],
        tags=["多"],
    )
    assert [item.sec_user_id for item in planned] == ["sec1"]

    planned = plan_collection(
        [
            account(sec_user_id="sec1", 账号名称="一号"),
            account(record_id="a2", 账号名称="二号", sec_user_id="sec2"),
        ],
        account_names="二号",
    )
    assert [item.sec_user_id for item in planned] == ["sec2"]


def test_first_collection_is_full_and_next_is_incremental_with_overlap():
    today = date(2026, 8, 15)
    first = plan_collection([account()], mode="incremental", today=today)
    assert first[0].earliest == ""

    second = plan_collection(
        [account(last_collected_at="2026-08-15 10:00:00")],
        mode="incremental",
        today=today,
    )
    assert second[0].earliest == "2026/08/14"


def test_fixed_window_takes_precedence_and_full_mode_can_force_full():
    fixed = plan_collection(
        [account(last_collected_at="2026-08-15 10:00:00", collect_window_days=200)],
        mode="incremental",
    )
    assert fixed[0].earliest == 200

    forced_full = plan_collection(
        [account(last_collected_at="2026-08-15 10:00:00")],
        mode="full",
    )
    assert forced_full[0].earliest == ""


def test_tiktok_requires_profile_link_and_douyin_url_is_generated():
    planned = plan_collection(
        [
            account(platform="TikTok", sec_user_id="tiksec", 链接=""),
            account(
                record_id="a2",
                账号名称="二号",
                sec_user_id="tiksec2",
                platform="TikTok",
                链接="https://www.tiktok.com/@two",
            ),
        ],
        platform="tiktok",
    )
    by_id = {item.sec_user_id: item for item in planned}
    assert by_id["tiksec"].status == "skipped"
    assert "主页链接缺失" in by_id["tiksec"].message
    assert by_id["tiksec2"].url == "https://www.tiktok.com/@two"


def test_write_ttd_accounts_preserves_unrelated_settings(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text(
        json.dumps(
            {
                "cookie": "preserve",
                "accounts_urls": [{"mark": "old", "url": "old"}],
                "root": "D:/Media",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    planned = plan_collection([account()])
    entries = write_ttd_accounts(settings, "douyin", planned)

    saved = json.loads(settings.read_text(encoding="utf-8"))
    assert saved["cookie"] == "preserve"
    assert saved["root"] == "D:/Media"
    assert entries == saved["accounts_urls"]
    assert entries[0] == {
        "mark": "一号",
        "url": "https://www.douyin.com/user/sec1",
        "tab": "post",
        "earliest": "",
        "latest": "",
        "enable": True,
    }
    assert not list(tmp_path.glob("*.tmp"))
```

- [ ] **Step 2: Run tests to verify failure**

```powershell
.\venv\Scripts\python.exe -m pytest tests\test_collection_planner.py -v
```

Expected: import error for `app.core.collection_planner`.

- [ ] **Step 3: Implement planner**

Create `app/core/collection_planner.py`:

```python
"""Pure planning and TTD account-list generation for collection batches."""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path


@dataclass
class PlannedAccount:
    record_id: str
    sec_user_id: str
    account_name: str
    platform: str
    mark: str
    url: str
    earliest: str | int
    status: str = "pending"
    message: str = ""


def _tags(value) -> set[str]:
    if isinstance(value, list):
        return {str(item).strip() for item in value if str(item).strip()}
    if not value:
        return set()
    return {
        tag.strip()
        for tag in re.split(r"[,，、\s]+", str(value))
        if tag.strip()
    }


def _last_date(value) -> date | None:
    if not value:
        return None
    normalized = str(value)[:10].replace("/", "-")
    try:
        return datetime.strptime(normalized, "%Y-%m-%d").date()
    except ValueError:
        return None


def _earliest_for(row: dict, mode: str) -> str | int:
    window = row.get("collect_window_days")
    if window not in (None, ""):
        try:
            return int(window)
        except (TypeError, ValueError):
            pass
    if mode == "full":
        return ""
    last = _last_date(row.get("last_collected_at"))
    if last is None:
        return ""
    return (last - timedelta(days=1)).strftime("%Y/%m/%d")


def plan_collection(
    accounts: list[dict],
    rating_min: int = 3,
    tags: list[str] | None = None,
    account_names: str = "",
    record_ids: list[str] | None = None,
    platform: str = "douyin",
    mode: str = "incremental",
) -> list[PlannedAccount]:
    wanted_tags = set(tags or [])
    names = {
        name.strip()
        for name in re.split(r"[,，\n]+", account_names or "")
        if name.strip()
    }
    ids = set(record_ids or [])
    expected_platform = "抖音" if platform == "douyin" else "TikTok"
    result: list[PlannedAccount] = []

    candidates = [
        row
        for row in accounts
        if row.get("平台") == expected_platform
        and row.get("启用")
        and str(row.get("sec_user_id") or "").strip()
    ]
    if names:
        candidates = [row for row in candidates if row.get("账号名称") in names]
    if ids:
        candidates = [row for row in candidates if row.get("record_id") in ids]
    if wanted_tags:
        candidates = [row for row in candidates if _tags(row.get("标签")) & wanted_tags]
    candidates = [
        row for row in candidates if int(row.get("等级") or 0) >= rating_min
    ]
    candidates.sort(
        key=lambda row: (-int(row.get("等级") or 0), str(row.get("账号名称") or ""))
    )

    for row in candidates:
        sec_user_id = str(row["sec_user_id"]).strip()
        name = str(row.get("账号名称") or sec_user_id)
        url = str(row.get("链接") or "").strip()
        status = "pending"
        message = ""
        if platform == "douyin":
            url = f"https://www.douyin.com/user/{sec_user_id}"
        elif "tiktok.com/" not in url:
            status = "skipped"
            message = "TikTok 主页链接缺失"
            url = ""
        result.append(
            PlannedAccount(
                record_id=str(row.get("record_id") or ""),
                sec_user_id=sec_user_id,
                account_name=name,
                platform=platform,
                mark=name,
                url=url,
                earliest=_earliest_for(row, mode),
                status=status,
                message=message,
            )
        )
    return result


def write_ttd_accounts(
    settings_path: Path,
    platform: str,
    planned: list[PlannedAccount],
) -> list[dict]:
    key = "accounts_urls" if platform == "douyin" else "accounts_urls_tiktok"
    entries = [
        {
            "mark": item.mark,
            "url": item.url,
            "tab": "post",
            "earliest": item.earliest,
            "latest": "",
            "enable": True,
        }
        for item in planned
        if item.status == "pending"
    ]
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    if settings_path.exists():
        with settings_path.open("r", encoding="utf-8") as file:
            settings = json.load(file)
    else:
        settings = {}
    settings[key] = entries

    temporary = settings_path.with_name(f"{settings_path.name}.doukhub.tmp")
    with temporary.open("w", encoding="utf-8") as file:
        json.dump(settings, file, ensure_ascii=False, indent=4)
        file.flush()
        os.fsync(file.fileno())
    os.replace(temporary, settings_path)
    return entries
```

- [ ] **Step 4: Run planner tests**

```powershell
.\venv\Scripts\python.exe -m pytest tests\test_collection_planner.py -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```powershell
git add app\core\collection_planner.py tests\test_collection_planner.py
git commit -m "feat: plan TTD collection batches"
```

---

### Task 3: Noninteractive TTD Batch Runner

**Files:**

- Create: `app/core/ttd_batch_runner.py`
- Test: `tests/test_ttd_batch_runner.py`

**Interfaces:**

- Consumes TTD classes `src.application.TikTokDownloader` and `src.application.main_terminal.TikTok`, plus TTD settings key `accounts_urls` / `accounts_urls_tiktok`.
- Produces:
  - CLI: `python app/core/ttd_batch_runner.py --platform douyin|tiktok`
  - Marker prefix `__DOUKHUB__`
  - Marker objects:
  - `{"type":"account_start","index":1,"total":1,"sec_user_id":"","url":"...","account_name":"..."}`
    - `{"type":"account_result","index":1,"total":1,"sec_user_id":"...","account_name":"...","status":"success|failed","message":"..."}`
    - `{"type":"summary","total":1,"success":1,"failed":0}`

- [ ] **Step 1: Write compile and marker tests**

Create `tests/test_ttd_batch_runner.py`:

```python
import py_compile

from app.core.ttd_batch_runner import emit_marker, marker_line


def test_runner_compiles_without_importing_ttd():
    py_compile.compile(
        "app/core/ttd_batch_runner.py",
        doraise=True,
    )


def test_marker_line_is_stable_json(capsys):
    emit_marker(
        {
            "type": "account_result",
            "index": 2,
            "total": 10,
            "sec_user_id": "sec1",
            "account_name": "一号",
            "status": "success",
            "message": "OK",
        }
    )
    line = capsys.readouterr().out.strip()
    assert line.startswith("__DOUKHUB__")
    parsed = marker_line(line)
    assert parsed["type"] == "account_result"
    assert parsed["account_name"] == "一号"
```

- [ ] **Step 2: Run tests to verify failure**

```powershell
.\venv\Scripts\python.exe -m pytest tests\test_ttd_batch_runner.py -v
```

Expected: import error for the runner module.

- [ ] **Step 3: Implement runner**

Create `app/core/ttd_batch_runner.py`:

```python
"""Run one TTD terminal account batch without interactive menu input.

This script is launched with TTD's repository as the current working directory.
It must not import TTD at module import time so DoukHub can compile and test it
even when TTD dependencies are absent.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace


MARKER_PREFIX = "__DOUKHUB__"


def marker_line(line: str) -> dict | None:
    if MARKER_PREFIX not in line:
        return None
    payload = line[line.index(MARKER_PREFIX) + len(MARKER_PREFIX):].strip()
    try:
        value = json.loads(payload)
        return value if isinstance(value, dict) else None
    except json.JSONDecodeError:
        return None


def emit_marker(payload: dict) -> None:
    print(f"{MARKER_PREFIX}{json.dumps(payload, ensure_ascii=False)}", flush=True)


def init_ttd_database(root: Path) -> None:
    database = root / "DouK-Downloader.db"
    with sqlite3.connect(database) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS config_data (
                NAME TEXT PRIMARY KEY,
                VALUE INTEGER NOT NULL CHECK(VALUE IN (0, 1))
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS option_data (
                NAME TEXT PRIMARY KEY,
                VALUE TEXT NOT NULL
            )
            """
        )
        conn.executemany(
            "INSERT OR REPLACE INTO config_data(NAME, VALUE) VALUES (?, ?)",
            [("Disclaimer", 1), ("Record", 1), ("Logger", 0)],
        )
        conn.execute(
            "INSERT OR REPLACE INTO option_data(NAME, VALUE) VALUES ('Language', 'zh_CN')"
        )
        conn.commit()


async def run_platform(platform: str) -> int:
    root = Path.cwd().resolve()
    sys.path.insert(0, str(root))
    init_ttd_database(root)

    try:
        import rich.console as rich_console
        rich_console.detect_legacy_windows = lambda: False
    except Exception:
        pass

    from src.application import TikTokDownloader
    from src.application.main_terminal import TikTok
    from src.custom import suspend

    with (root / "Volume" / "settings.json").open("r", encoding="utf-8") as file:
        settings = json.load(file)
    key = "accounts_urls" if platform == "douyin" else "accounts_urls_tiktok"
    accounts = [
        SimpleNamespace(**item)
        for item in settings.get(key, [])
        if item.get("enable", True)
    ]
    if not accounts:
        emit_marker({"type": "summary", "total": 0, "success": 0, "failed": 0})
        return 0

    async with TikTokDownloader() as downloader:
        downloader.check_config()
        await downloader.check_settings(False)
        terminal = TikTok(downloader.parameter, downloader.database)
        tiktok = platform == "tiktok"
        success = 0
        failed = 0
        total = len(accounts)

        for index, item in enumerate(accounts, start=1):
            name = item.mark or getattr(item, "url", "")
            emit_marker(
                {
                    "type": "account_start",
                    "index": index,
                    "total": total,
                    "sec_user_id": "",
                    "url": item.url,
                    "account_name": name,
                }
            )
            result = False
            resolved = ""
            message = ""
            try:
                resolved = await terminal.check_sec_user_id(item.url, tiktok)
                if not resolved:
                    raise RuntimeError("无法从账号链接提取 sec_user_id")
                result = bool(
                    await terminal.deal_account_detail(
                        index,
                        resolved,
                        mark=item.mark,
                        tab=getattr(item, "tab", "post") or "post",
                        earliest=getattr(item, "earliest", "") or "",
                        latest=getattr(item, "latest", "") or "",
                        tiktok=tiktok,
                    )
                )
                if result:
                    success += 1
                    message = "下载完成"
                else:
                    failed += 1
                    message = "TTD 返回账号处理失败"
            except Exception as error:
                failed += 1
                message = str(error)

            emit_marker(
                {
                    "type": "account_result",
                    "index": index,
                    "total": total,
                    "sec_user_id": resolved,
                    "account_name": name,
                    "status": "success" if result else "failed",
                    "message": message,
                }
            )
            if index != total and result:
                await suspend(index, terminal.console)

        emit_marker(
            {"type": "summary", "total": total, "success": success, "failed": failed}
        )
        return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--platform", choices=("douyin", "tiktok"), required=True)
    args = parser.parse_args()
    try:
        return asyncio.run(run_platform(args.platform))
    except Exception as error:
        emit_marker(
            {
                "type": "summary",
                "total": 0,
                "success": 0,
                "failed": 1,
                "message": str(error),
            }
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run runner tests**

```powershell
.\venv\Scripts\python.exe -m pytest tests\test_ttd_batch_runner.py -v
```

Expected: 2 tests pass.

- [ ] **Step 5: Commit**

```powershell
git add app\core\ttd_batch_runner.py tests\test_ttd_batch_runner.py
git commit -m "feat: add noninteractive TTD batch runner"
```

---

### Task 4: Batch Process Manager

**Files:**

- Create: `app/core/collection_batch_manager.py`
- Modify: `app/core/database.py`
- Test: `tests/test_collection_batch_manager.py`

**Interfaces:**

- Consumes Task 1 repository methods, Task 2 planner functions, and Task 3 `marker_line()`.
- Produces:
  - `CollectionBatchManager.start(accounts, rating_min=3, tags=None, account_names="", record_ids=None, platforms=("douyin",), mode="incremental") -> list[dict]`
  - `CollectionBatchManager.cancel(batch_id) -> bool`
  - `CollectionBatchManager.read_log(batch_id, max_lines=200) -> list[str]`
  - `CollectionBatchManager.recover_interrupted_batches() -> None`
  - `CollectionBatchManager.shutdown() -> None`

- [ ] **Step 1: Write failing manager tests**

Create `tests/test_collection_batch_manager.py`:

```python
import asyncio

import pytest

from app.core.collection_batch_manager import CollectionBatchManager
from app.core.database import Database


class FakeStream:
    def __init__(self, lines):
        self.lines = [line.encode("utf-8") for line in lines]

    async def readline(self):
        if not self.lines:
            return b""
        return self.lines.pop(0)


class FakeProcess:
    def __init__(self, lines, returncode=0):
        self.stdout = FakeStream(lines)
        self.returncode = None
        self._returncode = returncode
        self.pid = 12345
        self.terminated = False

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    async def wait(self):
        if self.returncode is None:
            self.returncode = self._returncode
        return self.returncode


@pytest.fixture
def db(tmp_path):
    return Database(db_path=tmp_path / "doukhub.db")


@pytest.fixture
def manager(db, tmp_path, monkeypatch):
    instance = CollectionBatchManager(
        database=db,
        ttd_path=tmp_path / "TikTokDownloader",
        log_dir=tmp_path / "logs",
        ttd_url="http://127.0.0.1:5555",
    )
    monkeypatch.setattr(instance, "_check_ttd_api", lambda: asyncio.sleep(0))
    try:
        yield instance
    finally:
        if instance._worker:
            instance._worker.cancel()


def insert_douyin_account(db):
    db.insert_account(
        {
            "record_id": "a1",
            "sec_user_id": "sec1",
            "账号名称": "一号",
            "平台": "抖音",
            "等级": 4,
            "启用": 1,
        }
    )


def test_marker_updates_item_account_and_counts(db, manager):
    insert_douyin_account(db)
    batches = asyncio.run(
        manager.start(
            db.get_all_accounts(),
            rating_min=3,
            platforms=("douyin",),
            mode="incremental",
        )
    )
    batch_id = batches[0]["id"]
    item = db.find_collection_batch_item(batch_id, "sec1")

    marker = {
        "type": "account_result",
        "sec_user_id": "sec1",
        "status": "success",
        "message": "OK",
    }
    assert manager._apply_marker(batch_id, marker)
    assert db.get_collection_batch_item_by_id(item["id"])["status"] == "success"
    assert db.get_account_by_id("a1")["last_collected_at"] is not None

    counts = manager._finalize(batch_id, "completed", 0)
    assert counts["success"] == 1
    assert db.get_collection_batch(batch_id)["status"] == "completed"


def test_run_batch_uses_ttd_process_and_persists_log(db, manager, monkeypatch):
    insert_douyin_account(db)
    batches = asyncio.run(
        manager.start(
            db.get_all_accounts(),
            rating_min=3,
            platforms=("douyin",),
            mode="incremental",
        )
    )
    batch_id = batches[0]["id"]

    async def fake_launch(command, cwd):
        return FakeProcess(
            [
                "TTD raw output",
                '__DOUKHUB__{"type":"account_result","sec_user_id":"sec1","status":"success","message":"OK"}',
            ],
            returncode=0,
        )

    monkeypatch.setattr(manager, "_launch_process", fake_launch)
    result = asyncio.run(manager._run_batch(batch_id))
    assert result == "completed"
    assert "TTD raw output" in manager.read_log(batch_id)
    assert db.get_collection_batch(batch_id)["process_pid"] == 12345


def test_interrupted_batches_are_recovered(db, manager):
    db.create_collection_batch(
        batch_id="old",
        filter_json="{}",
        platform="douyin",
        log_path="",
        items=[
            {
                "account_record_id": "a1",
                "sec_user_id": "sec1",
                "account_name": "一号",
                "platform": "douyin",
                "mark": "一号",
                "url": "https://www.douyin.com/user/sec1",
                "earliest": "",
            }
        ],
    )
    db.update_collection_batch("old", status="running", process_pid=999)
    manager.recover_interrupted_batches()

    assert db.get_collection_batch("old")["status"] == "failed"
    assert db.get_collection_batch_items("old")[0]["status"] == "failed"
```

- [ ] **Step 2: Run tests to verify failure**

```powershell
.\venv\Scripts\python.exe -m pytest tests\test_collection_batch_manager.py -v
```

Expected: import error for `app.core.collection_batch_manager`.

- [ ] **Step 3: Add item-by-id repository helper**

In `app/core/database.py`, add beside batch item methods:

```python
    def get_collection_batch_item_by_id(self, item_id: int) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM collection_batch_items WHERE id = ?", (item_id,)
            ).fetchone()
            return dict(row) if row else None
```

- [ ] **Step 4: Implement manager**

Create `app/core/collection_batch_manager.py`:

```python
"""Manage persistent collection batches and one TTD terminal process at a time."""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

import httpx

from .collection_planner import PlannedAccount, plan_collection, write_ttd_accounts
from .database import Database
from .ttd_batch_runner import marker_line


class CollectionBatchManager:
    def __init__(
        self,
        database: Database,
        ttd_path: Path,
        log_dir: Path,
        ttd_url: str,
    ):
        self.db = database
        self.ttd_path = Path(ttd_path).resolve()
        self.log_dir = Path(log_dir).resolve()
        self.ttd_url = ttd_url.rstrip("/")
        self.runner_path = Path(__file__).with_name("ttd_batch_runner.py").resolve()
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._worker: Optional[asyncio.Task] = None
        self._active_batch_id: Optional[str] = None
        self._active_process: Optional[asyncio.subprocess.Process] = None
        self._cancel_requested = False
        self._closing = False

    async def start(
        self,
        accounts: list[dict],
        rating_min: int = 3,
        tags: list[str] | None = None,
        account_names: str = "",
        record_ids: list[str] | None = None,
        platforms: tuple[str, ...] = ("douyin",),
        mode: str = "incremental",
    ) -> list[dict]:
        if self.db.get_active_collection_batch():
            raise RuntimeError("已有采集批次正在执行或等待执行")

        created: list[dict] = []
        stamp = datetime.now().strftime("%Y%m%d%H%M%S")
        self.log_dir.mkdir(parents=True, exist_ok=True)
        for platform in platforms:
            planned = plan_collection(
                accounts=accounts,
                rating_min=rating_min,
                tags=tags,
                account_names=account_names,
                record_ids=record_ids,
                platform=platform,
                mode=mode,
            )
            if not planned:
                continue
            batch_id = f"{stamp}-{platform}-{uuid.uuid4().hex[:6]}"
            log_path = self.log_dir / f"{batch_id}.log"
            snapshot = {
                "rating_min": rating_min,
                "tags": tags or [],
                "account_names": account_names,
                "record_ids": record_ids or [],
                "platform": platform,
                "mode": mode,
            }
            self.db.create_collection_batch(
                batch_id=batch_id,
                filter_json=json.dumps(snapshot, ensure_ascii=False),
                platform=platform,
                log_path=str(log_path),
                items=[vars(item) for item in planned],
            )
            created.append(
                {
                    "id": batch_id,
                    "platform": platform,
                    "total_accounts": len(planned),
                    "status": "pending",
                }
            )
            self._queue.put_nowait(batch_id)

        if not created:
            raise ValueError("没有符合条件的账号")
        self._ensure_worker()
        return created

    def cancel(self, batch_id: str) -> bool:
        batch = self.db.get_collection_batch(batch_id)
        if not batch or batch["status"] not in ("pending", "running"):
            return False
        self.db.update_collection_batch(batch_id, status="cancelling")
        if batch_id == self._active_batch_id:
            self._cancel_requested = True
            if self._active_process and self._active_process.returncode is None:
                self._active_process.terminate()
        return True

    def read_log(self, batch_id: str, max_lines: int = 200) -> list[str]:
        batch = self.db.get_collection_batch(batch_id)
        if not batch or not batch.get("log_path"):
            return []
        path = Path(batch["log_path"])
        if not path.exists():
            return []
        with path.open("r", encoding="utf-8", errors="replace") as file:
            return [line.rstrip("\r\n") for line in file][-max_lines:]

    def recover_interrupted_batches(self) -> None:
        for batch in self.db.list_collection_batches(limit=100):
            if batch["status"] not in ("pending", "running", "cancelling"):
                continue
            self._finalize(
                batch["id"],
                "failed" if batch["status"] == "running" else "cancelled",
                -1,
                "DoukHub 重启，批次中断",
            )

    async def shutdown(self) -> None:
        self._closing = True
        if self._active_process and self._active_process.returncode is None:
            self._active_process.terminate()
            await self._active_process.wait()
        if self._active_batch_id:
            self._finalize(
                self._active_batch_id,
                "cancelled",
                self._active_process.returncode if self._active_process else -1,
                "DoukHub 关闭，批次已取消",
            )
        if self._worker:
            self._worker.cancel()
            try:
                await self._worker
            except asyncio.CancelledError:
                pass

    def _ensure_worker(self) -> None:
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._worker_loop())

    async def _worker_loop(self) -> None:
        while True:
            if self._closing and self._queue.empty():
                return
            batch_id = await self._queue.get()
            try:
                batch = self.db.get_collection_batch(batch_id)
                if not batch or batch["status"] == "cancelling":
                    self._finalize(batch_id, "cancelled", -1, "批次已取消")
                    continue
                await self._run_batch(batch_id)
            finally:
                self._queue.task_done()

    async def _check_ttd_api(self) -> None:
        async with httpx.AsyncClient(timeout=3) as client:
            try:
                response = await client.get(f"{self.ttd_url}/")
            except Exception as error:
                raise RuntimeError(f"TTD API 未运行: {error}") from error
        if response.status_code not in (200, 307, 404):
            raise RuntimeError("TTD API 未运行")

    async def _launch_process(
        self, command: list[str], cwd: Path
    ) -> asyncio.subprocess.Process:
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        return await asyncio.create_subprocess_exec(
            *command,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            creationflags=creationflags,
        )

    async def _run_batch(self, batch_id: str) -> str:
        self._active_batch_id = batch_id
        self._cancel_requested = False
        batch = self.db.get_collection_batch(batch_id)
        items = self.db.get_collection_batch_items(batch_id)
        pending = [item for item in items if item["status"] == "pending"]
        started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        if not pending:
            return self._finalize(batch_id, "completed", 0)

        try:
            await self._check_ttd_api()
            planned_items = [
                PlannedAccount(
                    record_id=str(item.get("account_record_id") or ""),
                    sec_user_id=item["sec_user_id"],
                    account_name=item["account_name"],
                    platform=item["platform"],
                    mark=item["mark"],
                    url=item["url"],
                    earliest=item["earliest"],
                )
                for item in pending
            ]
            write_ttd_accounts(
                self.ttd_path / "Volume" / "settings.json",
                batch["platform"],
                planned_items,
            )
        except Exception as error:
            for item in pending:
                self.db.update_collection_batch_item(
                    item["id"], status="failed", message=str(error)
                )
            return self._finalize(batch_id, "failed", -1, str(error))

        self.db.update_collection_batch(
            batch_id, status="running", started_at=started_at
        )
        command = [
            sys.executable,
            str(self.runner_path),
            "--platform",
            batch["platform"],
        ]
        process = await self._launch_process(command, self.ttd_path)
        self._active_process = process
        self.db.update_collection_batch(batch_id, process_pid=process.pid)
        log_path = Path(batch["log_path"])
        log_path.parent.mkdir(parents=True, exist_ok=True)

        with log_path.open("w", encoding="utf-8", errors="replace") as log_file:
            while True:
                raw = await process.stdout.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                log_file.write(line + "\n")
                log_file.flush()
                marker = marker_line(line)
                if marker:
                    self._apply_marker(batch_id, marker)

        return_code = await process.wait()
        current_status = self.db.get_collection_batch(batch_id)["status"]
        if self._cancel_requested or current_status == "cancelling":
            final_status = "cancelled"
            message = "批次已取消"
        else:
            final_status = "completed" if return_code == 0 else "failed"
            message = "" if return_code == 0 else f"TTD 进程退出码: {return_code}"

        self._active_process = None
        self._active_batch_id = None
        return self._finalize(batch_id, final_status, return_code, message)

    def _apply_marker(self, batch_id: str, marker: dict) -> bool:
        marker_type = marker.get("type")
        if marker_type not in ("account_start", "account_result"):
            return False
        sec_user_id = str(marker.get("sec_user_id") or "")
        item = self.db.find_collection_batch_item(batch_id, sec_user_id)
        if not item and marker.get("url"):
            item = next(
                (
                    current
                    for current in self.db.get_collection_batch_items(batch_id)
                    if current.get("url") == marker.get("url")
                ),
                None,
            )
        if not item:
            return False
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if marker_type == "account_start":
            self.db.update_collection_batch_item(
                item["id"], status="running", started_at=now
            )
            self.db.refresh_collection_batch_counts(batch_id)
            return True

        status = "success" if marker.get("status") == "success" else "failed"
        self.db.update_collection_batch_item(
            item["id"],
            status=status,
            message=str(marker.get("message") or ""),
            finished_at=now,
        )
        if status == "success" and item.get("account_record_id"):
            self.db.update_account(
                item["account_record_id"], {"last_collected_at": now[:10]}
            )
        self.db.refresh_collection_batch_counts(batch_id)
        return True

    def _finalize(
        self,
        batch_id: str,
        status: str,
        return_code: int,
        message: str = "",
    ) -> dict:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        terminal_status = "cancelled" if status == "cancelled" else "failed"
        with self.db._connect() as conn:
            conn.execute(
                """
                UPDATE collection_batch_items
                SET status = ?, message = ?, finished_at = ?
                WHERE batch_id = ? AND status IN ('pending', 'running')
                """,
                (terminal_status, message or "批次结束前未收到账号结果", now, batch_id),
            )
            conn.commit()
        self.db.update_collection_batch(batch_id, status=status, finished_at=now)
        return self.db.refresh_collection_batch_counts(batch_id)
```

- [ ] **Step 5: Run manager tests**

```powershell
.\venv\Scripts\python.exe -m pytest tests\test_collection_batch_manager.py -v
```

Expected: 3 tests pass.

- [ ] **Step 6: Commit**

```powershell
git add app\core\collection_batch_manager.py app\core\database.py tests\test_collection_batch_manager.py
git commit -m "feat: manage TTD collection batch process"
```

---

### Task 5: Collection Batch API And App Lifecycle

**Files:**

- Modify: `app/main.py`
- Modify: `tests/test_api.py`
- Test: `tests/test_collection_api.py`

**Interfaces:**

- Consumes `get_database()`, config paths, and `CollectionBatchManager`.
- Produces:
  - `POST /api/collection/batches`
  - `GET /api/collection/batches`
  - `GET /api/collection/batches/{batch_id}`
  - `POST /api/collection/batches/{batch_id}/cancel`
  - `POST /api/collection/batches/{batch_id}/retry`
  - `get_collection_batch_manager()`

- [ ] **Step 1: Write failing API tests**

Create `tests/test_collection_api.py`:

```python
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

import app.main as app_main


@pytest.fixture
def batch_client():
    database = MagicMock()
    manager = MagicMock()
    manager.start = AsyncMock(
        return_value=[{"id": "b1", "platform": "douyin", "status": "pending"}]
    )
    manager.cancel.return_value = True
    manager.read_log.return_value = ["raw log"]
    database.get_all_accounts.return_value = []
    database.list_collection_batches.return_value = [
        {"id": "b1", "platform": "douyin", "status": "pending"}
    ]
    database.get_collection_batch.return_value = {
        "id": "b1",
        "platform": "douyin",
        "status": "pending",
    }
    database.get_collection_batch_items.return_value = [
        {
            "id": 1,
            "sec_user_id": "sec1",
            "account_name": "一号",
            "status": "pending",
            "message": "",
        }
    ]

    saved = (
        app_main.config,
        app_main.database,
        app_main.collection_batch_manager,
    )
    app_main.config = MagicMock()
    app_main.database = database
    app_main.collection_batch_manager = manager
    try:
        yield TestClient(app_main.app), database, manager
    finally:
        app_main.config, app_main.database, app_main.collection_batch_manager = saved


def test_start_batch(batch_client):
    client, _, manager = batch_client
    response = client.post(
        "/api/collection/batches",
        json={"rating_min": 3, "platform": "douyin", "mode": "incremental"},
    )
    assert response.status_code == 200
    assert response.json()["batches"][0]["id"] == "b1"
    assert manager.start.await_args.kwargs["rating_min"] == 3


def test_start_batch_rejects_empty_selection(batch_client):
    client, _, manager = batch_client
    manager.start = AsyncMock(side_effect=ValueError("没有符合条件的账号"))
    response = client.post("/api/collection/batches", json={})
    assert response.status_code == 400
    assert "没有符合条件的账号" in response.json()["message"]


def test_batch_detail_contains_items_and_log(batch_client):
    client, _, _ = batch_client
    response = client.get("/api/collection/batches/b1")
    assert response.status_code == 200
    data = response.json()
    assert data["batch"]["id"] == "b1"
    assert data["items"][0]["sec_user_id"] == "sec1"
    assert data["log"] == ["raw log"]


def test_cancel_batch(batch_client):
    client, _, manager = batch_client
    response = client.post("/api/collection/batches/b1/cancel")
    assert response.status_code == 200
    assert response.json()["success"] is True
    manager.cancel.assert_called_once_with("b1")


def test_retry_failed_items_creates_new_batch(batch_client):
    client, database, manager = batch_client
    database.get_collection_batch_items.return_value = [
        {
            "account_record_id": "a1",
            "sec_user_id": "sec1",
            "account_name": "一号",
            "status": "failed",
        },
        {
            "account_record_id": "a2",
            "sec_user_id": "sec2",
            "account_name": "二号",
            "status": "success",
        },
    ]
    response = client.post(
        "/api/collection/batches/b1/retry", json={"mode": "full"}
    )
    assert response.status_code == 200
    assert manager.start.await_args.kwargs["record_ids"] == ["a1"]
    assert manager.start.await_args.kwargs["mode"] == "full"
```

- [ ] **Step 2: Run tests to verify failure**

```powershell
.\venv\Scripts\python.exe -m pytest tests\test_collection_api.py -v
```

Expected: failures because globals and routes do not exist.

- [ ] **Step 3: Add manager global and lifespan hooks**

In `app/main.py`:

```python
from .core.collection_batch_manager import CollectionBatchManager
```

Add to globals:

```python
collection_batch_manager: CollectionBatchManager | None = None
```

Add getter:

```python
def get_collection_batch_manager() -> CollectionBatchManager:
    global collection_batch_manager
    if collection_batch_manager is None:
        collection_batch_manager = CollectionBatchManager(
            database=get_database(),
            ttd_path=Path(config.ttd_path),
            log_dir=config.data_dir / "collection_logs",
            ttd_url=f"http://127.0.0.1:{config.ttd_port}",
        )
    return collection_batch_manager
```

Before `yield` in `lifespan()`:

```python
    get_collection_batch_manager().recover_interrupted_batches()
```

After `yield`, before `svc.close()`:

```python
    await get_collection_batch_manager().shutdown()
```

- [ ] **Step 4: Add request models**

Near existing Pydantic models or immediately before the routes:

```python
class CollectionBatchRequest(BaseModel):
    rating_min: int = 3
    tags: list[str] = []
    account_names: str = ""
    mode: Literal["incremental", "full"] = "incremental"
    platform: Literal["douyin", "tiktok", "all"] = "douyin"


class CollectionRetryRequest(BaseModel):
    mode: Literal["incremental", "full"] = "incremental"
```

Ensure `Literal` is imported from `typing`.

- [ ] **Step 5: Add routes**

```python
# ========== 采集批次 ==========

@app.post("/api/collection/batches")
async def api_start_collection_batch(request: CollectionBatchRequest):
    db = get_database()
    manager = get_collection_batch_manager()
    platforms = (
        ("douyin", "tiktok") if request.platform == "all" else (request.platform,)
    )
    try:
        batches = await manager.start(
            accounts=db.get_all_accounts(),
            rating_min=request.rating_min,
            tags=request.tags,
            account_names=request.account_names,
            platforms=platforms,
            mode=request.mode,
        )
        return {"success": True, "batches": batches}
    except ValueError as error:
        return JSONResponse({"success": False, "message": str(error)}, status_code=400)
    except RuntimeError as error:
        return JSONResponse({"success": False, "message": str(error)}, status_code=409)


@app.get("/api/collection/batches")
async def api_list_collection_batches():
    return {"batches": get_database().list_collection_batches()}


@app.get("/api/collection/batches/{batch_id}")
async def api_collection_batch_detail(batch_id: str):
    db = get_database()
    batch = db.get_collection_batch(batch_id)
    if not batch:
        return JSONResponse({"success": False, "message": "批次不存在"}, status_code=404)
    items = db.get_collection_batch_items(batch_id)
    accounts = {
        row["record_id"]: row
        for row in db.get_all_accounts()
        if row.get("record_id")
    }
    for item in items:
        account = accounts.get(item.get("account_record_id"))
        item["last_collected_at"] = account.get("last_collected_at") if account else None
    return {
        "batch": batch,
        "items": items,
        "log": get_collection_batch_manager().read_log(batch_id),
    }


@app.post("/api/collection/batches/{batch_id}/cancel")
async def api_cancel_collection_batch(batch_id: str):
    ok = get_collection_batch_manager().cancel(batch_id)
    return {
        "success": ok,
        "message": "已请求取消" if ok else "批次不存在或已结束",
    }


@app.post("/api/collection/batches/{batch_id}/retry")
async def api_retry_collection_batch(batch_id: str, request: CollectionRetryRequest):
    db = get_database()
    source = db.get_collection_batch_items(batch_id)
    record_ids = [
        item["account_record_id"]
        for item in source
        if item.get("status") in ("failed", "cancelled")
        and item.get("account_record_id")
    ]
    if not record_ids:
        return JSONResponse(
            {"success": False, "message": "没有可重试的账号"},
            status_code=400,
        )
    try:
        batches = await get_collection_batch_manager().start(
            accounts=db.get_all_accounts(),
            rating_min=1,
            record_ids=record_ids,
            platforms=(db.get_collection_batch(batch_id)["platform"],),
            mode=request.mode,
        )
        return {"success": True, "batches": batches}
    except RuntimeError as error:
        return JSONResponse({"success": False, "message": str(error)}, status_code=409)
```

- [ ] **Step 6: Keep the existing API fixture isolated**

In `tests/test_api.py`, save and replace `app_main.collection_batch_manager` exactly like the existing globals:

```python
        "collection_batch_manager": app_main.collection_batch_manager,
```

and:

```python
    app_main.collection_batch_manager = MagicMock()
```

- [ ] **Step 7: Run API tests**

```powershell
.\venv\Scripts\python.exe -m pytest tests\test_collection_api.py tests\test_api.py -v
```

Expected: all tests pass.

- [ ] **Step 8: Commit**

```powershell
git add app\main.py tests\test_collection_api.py tests\test_api.py
git commit -m "feat: expose collection batch API"
```

---

### Task 6: Batch Collection UI And Account Window Field

**Files:**

- Modify: `app/templates/collect.html`
- Modify: `app/templates/table.html`
- Modify: `tests/test_api.py`

**Interfaces:**

- Consumes collection batch APIs from Task 5.
- Produces batch form controls, batch table, batch detail, cancel/retry actions, and editable `collect_window_days`.

- [ ] **Step 1: Add a failing page assertion**

In `tests/test_api.py::TestPageRoutes`, add:

```python
    def test_collect_page_contains_batch_controls(self, app_env):
        client, *_ = app_env
        r = client.get("/collect")
        assert "采集模式" in r.text
        assert "批次记录" in r.text
        assert "失败重试" in r.text
```

Run:

```powershell
.\venv\Scripts\python.exe -m pytest tests\test_api.py::TestPageRoutes::test_collect_page_contains_batch_controls -v
```

Expected: fail because the page still uses the old SSE form.

- [ ] **Step 2: Replace the account form**

Replace the contents of `<div class="card" id="account-form-card">` in `app/templates/collect.html` with:

```html
<h3><i class="ph ph-download"></i> 整号采集</h3>
<form id="account-form" onsubmit="startCollectionBatch(event)">
    <div class="form-group">
        <label>等级筛选（只采集选中等级以上）</label>
        <div class="collect-rating-group" style="display:flex;gap:16px;flex-wrap:wrap;">
            <label style="display:flex;align-items:center;gap:4px;font-size:14px;font-weight:normal;"><input type="checkbox" name="rating" value="4" checked> 4星</label>
            <label style="display:flex;align-items:center;gap:4px;font-size:14px;font-weight:normal;"><input type="checkbox" name="rating" value="3" checked> 3星</label>
            <label style="display:flex;align-items:center;gap:4px;font-size:14px;font-weight:normal;"><input type="checkbox" name="rating" value="2"> 2星</label>
            <label style="display:flex;align-items:center;gap:4px;font-size:14px;font-weight:normal;"><input type="checkbox" name="rating" value="1"> 1星</label>
        </div>
    </div>
    <div class="form-group">
        <label>标签筛选（逗号分隔，留空为全部）</label>
        <input type="text" name="tags" placeholder="多, 个人">
    </div>
    <div class="form-group">
        <label>指定账号（留空为符合条件的全部账号）</label>
        <input type="text" name="account_names" placeholder="账号名称, 多个用逗号分隔">
    </div>
    <div class="form-group">
        <label>平台</label>
        <select name="platform">
            <option value="douyin">抖音</option>
            <option value="tiktok">TikTok</option>
            <option value="all">全部</option>
        </select>
    </div>
    <div class="form-group">
        <label>采集模式</label>
        <select name="mode">
            <option value="incremental">首次全量，后续增量</option>
            <option value="full">重新全量</option>
        </select>
    </div>
    <button type="submit" class="btn btn-primary" id="account-submit">
        <i class="ph ph-download-simple"></i> 开始整号采集
    </button>
</form>
```

- [ ] **Step 3: Replace progress card with batch cards**

Replace the existing `<div class="card" id="progress-card">...</div>` with:

```html
<div class="card" id="batch-card" style="display:none;">
    <h3><i class="ph ph-list-checks"></i> 批量批次</h3>
    <div id="batch-detail"></div>
</div>

<div class="card">
    <h3><i class="ph ph-clock-counter-clockwise"></i> 批次记录</h3>
    <div class="table-scroll">
        <table>
            <thead>
                <tr>
                    <th>批次</th>
                    <th>平台</th>
                    <th>状态</th>
                    <th>账号</th>
                    <th>成功</th>
                    <th>失败</th>
                    <th>跳过</th>
                    <th>开始时间</th>
                    <th>操作</th>
                </tr>
            </thead>
            <tbody id="batch-table-body">
                <tr><td colspan="9" class="text-muted">暂无批次</td></tr>
            </tbody>
        </table>
    </div>
</div>
```

Remove the now-unused `collect-stats-grid` and `progress-step` markup if no other block depends on them.

- [ ] **Step 4: Replace old SSE JavaScript**

Delete `submitAccountCollect()`, `showProgress()`, `updateStep()`, and SSE reader logic. Add:

```javascript
let collectionPollTimer = null;

async function startCollectionBatch(e) {
    e.preventDefault();
    const form = new FormData(e.target);
    const ratings = form.getAll('rating').map(Number);
    const payload = {
        rating_min: ratings.length ? Math.min(...ratings) : 3,
        tags: String(form.get('tags') || '')
            .split(/[,，]/).map(value => value.trim()).filter(Boolean),
        account_names: form.get('account_names') || '',
        platform: form.get('platform') || 'douyin',
        mode: form.get('mode') || 'incremental',
    };
    const button = document.getElementById('account-submit');
    button.disabled = true;
    button.innerHTML = '<i class="ph ph-spinner"></i> 正在创建...';
    try {
        const data = await apiCall('/api/collection/batches', 'POST', payload);
        showToast(`已创建 ${data.batches.length} 个批次`, 'success');
        document.getElementById('batch-card').style.display = 'block';
        await refreshCollectionBatches();
    } catch (error) {
        showToast(error.message || '创建批次失败', 'error');
    } finally {
        button.disabled = false;
        button.innerHTML = '<i class="ph ph-download-simple"></i> 开始整号采集';
    }
}

async function refreshCollectionBatches() {
    const data = await apiCall('/api/collection/batches', 'GET');
    const body = document.getElementById('batch-table-body');
    if (!data.batches?.length) {
        body.innerHTML = '<tr><td colspan="9" class="text-muted">暂无批次</td></tr>';
        return;
    }
    body.innerHTML = data.batches.map(batch => `
        <tr>
            <td title="${escapeHtml(batch.id)}">${escapeHtml(batch.id.slice(0, 17))}</td>
            <td>${formatPlatform(batch.platform)}</td>
            <td>${formatBatchStatus(batch.status)}</td>
            <td>${batch.total_accounts || 0}</td>
            <td>${batch.success_accounts || 0}</td>
            <td>${batch.failed_accounts || 0}</td>
            <td>${batch.skipped_accounts || 0}</td>
            <td>${formatDateTime(batch.started_at)}</td>
            <td>
                <button class="btn btn-secondary" onclick="showBatchDetail('${batch.id}')">详情</button>
                ${['pending', 'running', 'cancelling'].includes(batch.status) ? `<button class="btn btn-danger" onclick="cancelCollectionBatch('${batch.id}')">取消</button>` : ''}
                ${(batch.failed_accounts || 0) > 0 ? `<button class="btn btn-primary" onclick="retryCollectionBatch('${batch.id}')">失败重试</button>` : ''}
            </td>
        </tr>
    `).join('');

    const active = data.batches.find(batch =>
        ['pending', 'running', 'cancelling'].includes(batch.status)
    );
    if (active) await showBatchDetail(active.id, true);
}

async function showBatchDetail(batchId, silent = false) {
    const data = await apiCall(`/api/collection/batches/${batchId}`, 'GET');
    document.getElementById('batch-card').style.display = 'block';
    const rows = data.items.map(item => `
        <tr>
            <td>${escapeHtml(item.account_name || item.sec_user_id)}</td>
            <td>${formatBatchStatus(item.status)}</td>
            <td>${formatDateTime(item.last_collected_at)}</td>
            <td>${escapeHtml(item.message || '')}</td>
        </tr>
    `).join('');
    document.getElementById('batch-detail').innerHTML = `
        <div class="table-scroll" style="margin-bottom:12px;">
            <table>
                <thead><tr><th>账号</th><th>状态</th><th>上次采集</th><th>信息</th></tr></thead>
                <tbody>${rows}</tbody>
            </table>
        </div>
        <details>
            <summary class="text-muted">批次日志</summary>
            <pre style="max-height:240px;overflow:auto;background:var(--bg-muted);padding:12px;border-radius:var(--radius-sm);font-size:12px;">${escapeHtml((data.log || []).join('\n'))}</pre>
        </details>
    `;
    if (!silent) await refreshCollectionBatches();
}

async function cancelCollectionBatch(batchId) {
    const ok = await confirmDialog(
        '将停止当前 TTD 批量进程，未完成账号会标记为取消。',
        {title: '取消批次', confirmText: '取消批次'}
    );
    if (!ok) return;
    await apiCall(`/api/collection/batches/${batchId}/cancel`, 'POST');
    showToast('已请求取消');
    await refreshCollectionBatches();
}

async function retryCollectionBatch(batchId) {
    const data = await apiCall(`/api/collection/batches/${batchId}/retry`, 'POST', {
        mode: 'incremental',
    });
    showToast(`已创建 ${data.batches.length} 个重试批次`, 'success');
    await refreshCollectionBatches();
}

function formatPlatform(value) {
    return {douyin: '抖音', tiktok: 'TikTok'}[value] || value || '-';
}

function formatBatchStatus(value) {
    return {
        pending: '等待中', running: '运行中', cancelling: '取消中',
        completed: '已完成', failed: '失败', cancelled: '已取消',
        success: '成功', skipped: '跳过',
    }[value] || value || '-';
}

function formatDateTime(value) {
    return value ? String(value).replace('T', ' ').slice(0, 19) : '-';
}

function escapeHtml(value) {
    return String(value ?? '')
        .replaceAll('&', '&amp;').replaceAll('<', '&lt;')
        .replaceAll('>', '&gt;').replaceAll('"', '&quot;')
        .replaceAll("'", '&#039;');
}

if (collectionPollTimer) clearInterval(collectionPollTimer);
collectionPollTimer = setInterval(refreshCollectionBatches, 3000);
refreshCollectionBatches();
```

- [ ] **Step 5: Expose the fixed window field**

In `app/templates/table.html`:

Add to `FIELD_EDITORS`:

```javascript
        'collect_window_days': { editor: 'agNumberCellEditor', params: { min: 0 } },
```

Add `collect_window_days` to `TABLE_EDITABLE_FIELDS.account_cache`.

Add widths:

```javascript
        'last_collected_at': 140,
        'collect_window_days': 110,
```

Place these fields in `COLUMN_ORDER.account_cache` after `采集类型`:

```javascript
            '采集类型', 'collect_window_days', 'last_collected_at', '启用',
```

Add to `FIELD_CATEGORIES.account_cache`:

```javascript
            'collect_window_days': 'control',
            'last_collected_at': 'system',
```

- [ ] **Step 6: Run API and page tests**

```powershell
.\venv\Scripts\python.exe -m pytest tests\test_api.py -v
```

Expected: all tests pass.

- [ ] **Step 7: Commit**

```powershell
git add app\templates\collect.html app\templates\table.html tests\test_api.py
git commit -m "feat: add collection batch UI"
```

---

### Task 7: Single-Work Metadata And Flexible Downloader

**Files:**

- Create: `app/core/single_work.py`
- Test: `tests/test_single_work.py`

**Interfaces:**

- Consumes TTD endpoints `/douyin/share`, `/tiktok/share`, `/douyin/detail`, and `/tiktok/detail`.
- Produces:
  - `detect_single_platform(link) -> str`
  - `extract_detail_id(link) -> str`
  - `normalize_work(raw, platform) -> dict`
  - `sanitize_filename_part(value, max_length=80) -> str`
  - `build_filename(work, template="{create_time} {author} {title}", index=0) -> str`
  - `async fetch_work(client, ttd_url, link, platform) -> dict`
  - `async download_work(client, work, target_dir, template="{create_time} {author} {title}") -> list[Path]`

- [ ] **Step 1: Write failing tests**

Create `tests/test_single_work.py`:

```python
import asyncio

import httpx

from app.core.single_work import (
    build_filename,
    detect_single_platform,
    download_work,
    extract_detail_id,
    fetch_work,
    normalize_work,
    sanitize_filename_part,
)


def test_platform_and_id_detection():
    assert detect_single_platform(
        "https://www.douyin.com/video/1234567890123456789"
    ) == "douyin"
    assert detect_single_platform(
        "https://www.tiktok.com/@user/video/1234567890123456789"
    ) == "tiktok"
    assert (
        extract_detail_id(
            "abc https://www.douyin.com/video/1234567890123456789?x=1"
        )
        == "1234567890123456789"
    )


def test_normalize_work_uses_ttd_extracted_fields():
    work = normalize_work(
        {
            "id": "1234567890123456789",
            "desc": "标题",
            "nickname": "作者",
            "mark": "作者",
            "create_time": "2026-08-15 10:00:00",
            "type": "视频",
            "downloads": ["https://example.com/video"],
            "share_url": "https://www.douyin.com/video/1234567890123456789",
        },
        "douyin",
    )
    assert work["title"] == "标题"
    assert work["author"] == "作者"
    assert work["create_time"] == "2026-08-15 10-00-00"
    assert work["platform"] == "douyin"


def test_filename_cleanup_and_image_suffix():
    assert sanitize_filename_part("a/b:c*d?", 5) == "abcd"
    work = {
        "id": "1234567890123456789",
        "title": "标题",
        "author": "作者",
        "create_time": "2026-08-15 10-00-00",
        "type": "图集",
        "downloads": ["one", "two"],
    }
    assert build_filename(work, "{author} {title}", 1) == "作者 标题_1"


def test_filename_total_length_is_capped():
    work = {
        "id": "1234567890123456789",
        "title": "长" * 200,
        "author": "作者" * 100,
        "create_time": "2026-08-15 10-00-00",
    }
    assert len(build_filename(work, "{title} {author} {title}")) <= 160


def test_fetch_and_download_work(tmp_path):
    async def handler(request):
        if request.url.path == "/douyin/detail":
            return httpx.Response(
                200,
                json={
                    "data": {
                        "id": "1234567890123456789",
                        "desc": "标题",
                        "nickname": "作者",
                        "create_time": "2026-08-15 10:00:00",
                        "type": "图集",
                        "downloads": [
                            "https://cdn.example/a.jpg",
                            "https://cdn.example/b.jpg",
                        ],
                    }
                },
            )
        if request.url.host == "cdn.example":
            return httpx.Response(200, content=b"image")
        return httpx.Response(404)

    async def run():
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(
            transport=transport, follow_redirects=True
        ) as client:
            work = await fetch_work(
                client,
                "http://ttd.local",
                "https://www.douyin.com/video/1234567890123456789",
                "douyin",
            )
            return await download_work(client, work, tmp_path, "{author} {title}")

    paths = asyncio.run(run())
    assert [path.name for path in paths] == ["作者 标题_1.jpg", "作者 标题_2.jpg"]
    assert all(path.read_bytes() == b"image" for path in paths)
    assert not list(tmp_path.glob("*.part"))
```

- [ ] **Step 2: Run tests to verify failure**

```powershell
.\venv\Scripts\python.exe -m pytest tests\test_single_work.py -v
```

Expected: import error for `app.core.single_work`.

- [ ] **Step 3: Implement single-work service**

Create `app/core/single_work.py`:

```python
"""Fetch TTD single-work metadata and download files outside TTD's archive."""
from __future__ import annotations

import re
from pathlib import Path

import httpx


DETAIL_ID = re.compile(r"\b(\d{19})\b")
URL = re.compile(r"https?://[^\s\"'<>]+")
INVALID_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
MAX_FILENAME_STEM = 160


def detect_single_platform(link: str) -> str:
    if "douyin.com" in link or "iesdouyin.com" in link:
        return "douyin"
    if "tiktok.com" in link:
        return "tiktok"
    return ""


def extract_detail_id(link: str) -> str:
    match = DETAIL_ID.search(link)
    return match.group(1) if match else ""


def normalize_work(raw: dict, platform: str) -> dict:
    work_id = str(raw.get("id") or "")
    return {
        "id": work_id,
        "title": str(raw.get("desc") or work_id),
        "author": str(raw.get("mark") or raw.get("nickname") or ""),
        "create_time": str(raw.get("create_time") or "").replace(":", "-"),
        "type": str(raw.get("type") or ""),
        "downloads": [url for url in raw.get("downloads") or [] if url],
        "share_url": str(raw.get("share_url") or ""),
        "platform": platform,
    }


def sanitize_filename_part(value, max_length: int = 80) -> str:
    cleaned = INVALID_FILENAME.sub("", str(value or ""))
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return cleaned[:max_length]


def build_filename(
    work: dict,
    template: str = "{create_time} {author} {title}",
    index: int = 0,
) -> str:
    stem = template.format(
        create_time=sanitize_filename_part(work.get("create_time"), 24),
        author=sanitize_filename_part(work.get("author")),
        title=sanitize_filename_part(work.get("title")),
        id=sanitize_filename_part(work.get("id"), 24),
    ).strip()
    if index:
        stem = f"{stem}_{index}"
    stem = stem[:MAX_FILENAME_STEM].rstrip(" .")
    return stem or sanitize_filename_part(work.get("id"), 24)


async def _resolve_share_link(
    client: httpx.AsyncClient, ttd_url: str, link: str, platform: str
) -> str:
    response = await client.post(f"{ttd_url}/{platform}/share", json={"text": link})
    response.raise_for_status()
    return str(response.json().get("url") or "")


async def fetch_work(
    client: httpx.AsyncClient, ttd_url: str, link: str, platform: str
) -> dict:
    detail_id = extract_detail_id(link)
    if not detail_id:
        resolved = await _resolve_share_link(client, ttd_url, link, platform)
        detail_id = extract_detail_id(resolved)
    if not detail_id:
        raise ValueError("无法从链接提取作品 ID")

    response = await client.post(
        f"{ttd_url}/{platform}/detail",
        json={"detail_id": detail_id, "source": False},
    )
    response.raise_for_status()
    payload = response.json()
    raw = payload.get("data")
    if not raw:
        raise RuntimeError(payload.get("message") or "TTD 未返回作品数据")
    if isinstance(raw, list):
        raw = raw[0]
    return normalize_work(raw, platform)


def _extension(response: httpx.Response, work_type: str) -> str:
    content_type = (
        response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    )
    mapping = {
        "video/mp4": ".mp4",
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
    }
    if content_type in mapping:
        return mapping[content_type]
    return ".mp4" if "视频" in work_type else ".jpg"


def _unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    counter = 2
    while True:
        candidate = path.parent / f"{path.stem} ({counter}){path.suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


async def download_work(
    client: httpx.AsyncClient,
    work: dict,
    target_dir: Path,
    template: str = "{create_time} {author} {title}",
) -> list[Path]:
    if not work.get("downloads"):
        raise ValueError("作品没有可用下载地址")
    target_dir = Path(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    saved: list[Path] = []
    multiple = len(work["downloads"]) > 1

    for index, url in enumerate(work["downloads"], start=1):
        async with client.stream("GET", url) as response:
            response.raise_for_status()
            extension = _extension(response, work.get("type", ""))
            stem = build_filename(work, template, index if multiple else 0)
            final_path = _unique_path(target_dir / f"{stem}{extension}")
            temporary = final_path.with_suffix(f"{final_path.suffix}.part")
            try:
                with temporary.open("wb") as file:
                    async for chunk in response.aiter_bytes():
                        file.write(chunk)
                temporary.replace(final_path)
            finally:
                if temporary.exists():
                    temporary.unlink()
            saved.append(final_path)
    return saved
```

- [ ] **Step 4: Run tests**

```powershell
.\venv\Scripts\python.exe -m pytest tests\test_single_work.py -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```powershell
git add app\core\single_work.py tests\test_single_work.py
git commit -m "feat: add flexible single-work downloader"
```

---

### Task 8: Single-Work API And UI

**Files:**

- Modify: `app/main.py`
- Modify: `app/templates/collect.html`
- Test: `tests/test_collection_api.py`

**Interfaces:**

- Consumes Task 7 functions and existing `GET /api/browse-dir`.
- Produces:
  - `POST /api/collection/works/resolve`
  - `POST /api/collection/works/download`
  - `get_single_work_client()`
  - single-work parse, directory selection, and download UI.

- [ ] **Step 1: Add failing API tests**

Append to `tests/test_collection_api.py`:

```python
@pytest.fixture
def single_client(monkeypatch):
    saved = app_main.single_work_client
    app_main.single_work_client = MagicMock()
    try:
        yield TestClient(app_main.app)
    finally:
        app_main.single_work_client = saved


def test_resolve_single_works(single_client, monkeypatch):
    from app.core import single_work

    async def fake_fetch(client, ttd_url, link, platform):
        return {
            "id": "1234567890123456789",
            "title": "标题",
            "author": "作者",
            "create_time": "2026-08-15 10-00-00",
            "type": "视频",
            "downloads": ["https://example.com/video"],
            "share_url": link,
            "platform": platform,
        }

    monkeypatch.setattr(single_work, "fetch_work", fake_fetch)
    link = "https://www.douyin.com/video/1234567890123456789"
    monkeypatch.setattr(app_main, "_extract_single_work_links", lambda text: [(link, "douyin")])
    response = single_client.post("/api/collection/works/resolve", json={"links": link})
    assert response.status_code == 200
    assert response.json()["works"][0]["title"] == "标题"


def test_download_single_works(single_client, tmp_path, monkeypatch):
    from app.core import single_work

    async def fake_fetch(client, ttd_url, link, platform):
        return {"id": "1", "title": "标题", "downloads": ["https://example.com/a"]}

    async def fake_download(client, work, target_dir, template):
        path = target_dir / "saved.mp4"
        path.write_bytes(b"data")
        return [path]

    monkeypatch.setattr(single_work, "fetch_work", fake_fetch)
    monkeypatch.setattr(single_work, "download_work", fake_download)
    link = "https://www.douyin.com/video/1234567890123456789"
    monkeypatch.setattr(app_main, "_extract_single_work_links", lambda text: [(link, "douyin")])
    response = single_client.post(
        "/api/collection/works/download",
        json={
            "links": link,
            "target_dir": str(tmp_path),
            "filename_template": "{author} {title}",
        },
    )
    assert response.status_code == 200
    assert response.json()["results"][0]["status"] == "success"
```

- [ ] **Step 2: Run tests to verify failure**

```powershell
.\venv\Scripts\python.exe -m pytest tests\test_collection_api.py -v
```

Expected: new routes return 404.

- [ ] **Step 3: Add globals, models, helper, and routes**

In `app/main.py`:

```python
from .core import single_work
```

Globals:

```python
single_work_client: httpx.AsyncClient | None = None
```

Getter:

```python
def get_single_work_client() -> httpx.AsyncClient:
    global single_work_client
    if single_work_client is None:
        single_work_client = httpx.AsyncClient(
            timeout=300,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0"},
        )
    return single_work_client
```

Cleanup after `yield`:

```python
    if single_work_client:
        await single_work_client.aclose()
        single_work_client = None
```

Models:

```python
class SingleWorkResolveRequest(BaseModel):
    links: str


class SingleWorkDownloadRequest(SingleWorkResolveRequest):
    target_dir: str
    filename_template: str = "{create_time} {author} {title}"
```

Helper and routes:

```python
def _extract_single_work_links(text: str) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    for link in single_work.URL.findall(text or ""):
        platform = single_work.detect_single_platform(link)
        if platform:
            result.append((link, platform))
    return result


@app.post("/api/collection/works/resolve")
async def api_resolve_single_works(request: SingleWorkResolveRequest):
    links = _extract_single_work_links(request.links)
    if not links:
        return JSONResponse(
            {"success": False, "message": "未识别到抖音或 TikTok 作品链接"},
            status_code=400,
        )
    client = get_single_work_client()
    ttd_url = f"http://127.0.0.1:{config.ttd_port}"
    works = []
    errors = []
    for link, platform in links:
        try:
            works.append(
                await single_work.fetch_work(client, ttd_url, link, platform)
            )
        except Exception as error:
            errors.append({"link": link, "message": str(error)})
    return {"success": bool(works), "works": works, "errors": errors}


@app.post("/api/collection/works/download")
async def api_download_single_works(request: SingleWorkDownloadRequest):
    links = _extract_single_work_links(request.links)
    if not links:
        return JSONResponse(
            {"success": False, "message": "未识别到抖音或 TikTok 作品链接"},
            status_code=400,
        )
    target = Path(request.target_dir).expanduser()
    if not target.exists() or not target.is_dir():
        return JSONResponse(
            {"success": False, "message": "保存目录不存在"},
            status_code=400,
        )

    client = get_single_work_client()
    ttd_url = f"http://127.0.0.1:{config.ttd_port}"
    results = []
    for link, platform in links:
        try:
            work = await single_work.fetch_work(client, ttd_url, link, platform)
            paths = await single_work.download_work(
                client,
                work,
                target,
                request.filename_template,
            )
            results.append(
                {
                    "link": link,
                    "status": "success",
                    "title": work["title"],
                    "files": [str(path) for path in paths],
                }
            )
        except Exception as error:
            results.append(
                {"link": link, "status": "failed", "message": str(error)}
            )
    return {
        "success": any(item["status"] == "success" for item in results),
        "results": results,
    }
```

- [ ] **Step 4: Update the `/collect` context**

In `page_collect()`, add:

```python
        "download_path": str(config.download_path),
```

- [ ] **Step 5: Replace the single-work form**

Replace the contents of `<div class="card" id="detail-form-card">`:

```html
<h3><i class="ph ph-link"></i> 单品采集</h3>
<form id="detail-form" onsubmit="resolveSingleWorks(event)">
    <div class="form-group">
        <label>粘贴作品链接（一行一个，支持抖音 / TikTok）</label>
        <textarea name="links" rows="6" placeholder="https://www.douyin.com/video/...&#10;https://www.tiktok.com/@user/video/..."></textarea>
    </div>
    <div class="form-group">
        <label>保存目录</label>
        <div style="display:flex;gap:8px;">
            <input type="text" name="target_dir" id="single-target-dir" value="{{ download_path or '' }}" style="flex:1;">
            <button type="button" class="btn btn-secondary" onclick="openSingleDirDialog()" title="选择目录">
                <i class="ph ph-folder-open"></i>
            </button>
        </div>
    </div>
    <div class="form-group">
        <label>命名模板</label>
        <input type="text" name="filename_template" value="{create_time} {author} {title}">
    </div>
    <div class="btn-group">
        <button type="submit" class="btn btn-secondary" id="detail-resolve">
            <i class="ph ph-magnifying-glass"></i> 解析作品
        </button>
        <button type="button" class="btn btn-primary" id="detail-submit" onclick="downloadSingleWorks()" disabled>
            <i class="ph ph-download-simple"></i> 下载作品
        </button>
    </div>
</form>
<div id="single-work-list" style="margin-top:16px;"></div>

<div id="single-dir-modal" style="display:none;position:fixed;inset:0;z-index:1000;align-items:center;justify-content:center;background:rgba(0,0,0,.45);">
    <div class="card" style="width:min(560px,92vw);max-height:80vh;display:flex;flex-direction:column;">
        <h3>选择目录</h3>
        <div class="form-group">
            <label>当前目录</label>
            <input type="text" id="single-dir-current" readonly>
        </div>
        <div id="single-dir-list" style="overflow:auto;flex:1;min-height:160px;"></div>
        <div class="btn-group" style="justify-content:flex-end;margin-top:12px;">
            <button type="button" class="btn btn-secondary" onclick="goSingleDirParent()">上一级</button>
            <button type="button" class="btn btn-secondary" onclick="closeSingleDirDialog()">取消</button>
            <button type="button" class="btn btn-primary" onclick="chooseSingleDir()">使用此目录</button>
        </div>
    </div>
</div>
```

- [ ] **Step 6: Add single-work JavaScript**

Delete `submitDetailCollect()` and `showCollectStats()` if they are no longer referenced. Add:

```javascript
let resolvedSingleLinks = [];

async function resolveSingleWorks(event) {
    event.preventDefault();
    const form = new FormData(event.target);
    const button = document.getElementById('detail-resolve');
    button.disabled = true;
    button.innerHTML = '<i class="ph ph-spinner"></i> 解析中...';
    try {
        const data = await apiCall('/api/collection/works/resolve', 'POST', {
            links: form.get('links') || '',
        });
        if (!data.works?.length) {
            throw new Error(
                data.message || data.errors?.[0]?.message || '解析失败'
            );
        }
        resolvedSingleLinks = data.works
            .map(work => work.share_url)
            .filter(Boolean);
        renderSingleWorks(data.works);
        document.getElementById('detail-submit').disabled = false;
        showToast(`解析到 ${data.works.length} 个作品`, 'success');
    } catch (error) {
        showToast(error.message || '解析作品失败', 'error');
    } finally {
        button.disabled = false;
        button.innerHTML = '<i class="ph ph-magnifying-glass"></i> 解析作品';
    }
}

async function downloadSingleWorks() {
    const form = new FormData(document.getElementById('detail-form'));
    const originalLinks = String(form.get('links') || '');
    const links = resolvedSingleLinks.length
        ? resolvedSingleLinks.join('\n')
        : originalLinks;
    const button = document.getElementById('detail-submit');
    button.disabled = true;
    button.innerHTML = '<i class="ph ph-spinner"></i> 下载中...';
    try {
        const data = await apiCall('/api/collection/works/download', 'POST', {
            links,
            target_dir: form.get('target_dir') || '',
            filename_template: form.get('filename_template') ||
                '{create_time} {author} {title}',
        });
        renderSingleResults(data.results || []);
        showToast(
            data.success ? '下载完成' : '部分作品下载失败',
            data.success ? 'success' : 'error'
        );
    } catch (error) {
        showToast(error.message || '下载作品失败', 'error');
    } finally {
        button.disabled = false;
        button.innerHTML = '<i class="ph ph-download-simple"></i> 下载作品';
    }
}

function renderSingleWorks(works) {
    document.getElementById('single-work-list').innerHTML = `
        <div class="table-scroll">
            <table>
                <thead><tr><th>作品</th><th>作者</th><th>发布时间</th><th>类型</th><th>文件</th></tr></thead>
                <tbody>
                    ${works.map(work => `
                        <tr>
                            <td>${escapeHtml(work.title)}</td>
                            <td>${escapeHtml(work.author)}</td>
                            <td>${formatDateTime(work.create_time)}</td>
                            <td>${escapeHtml(work.type)}</td>
                            <td>${work.downloads?.length || 0}</td>
                        </tr>
                    `).join('')}
                </tbody>
            </table>
        </div>
    `;
}

function renderSingleResults(results) {
    document.getElementById('single-work-list').innerHTML = `
        <div class="table-scroll">
            <table>
                <thead><tr><th>作品</th><th>状态</th><th>文件 / 错误</th></tr></thead>
                <tbody>
                    ${results.map(result => `
                        <tr>
                            <td>${escapeHtml(result.title || result.link)}</td>
                            <td>${result.status === 'success' ? '成功' : '失败'}</td>
                            <td>${escapeHtml(result.files ? result.files.join('\\n') : result.message || '')}</td>
                        </tr>
                    `).join('')}
                </tbody>
            </table>
        </div>
    `;
}

let singleDirCurrent = '';
let singleDirEntries = [];

async function openSingleDirDialog() {
    const input = document.getElementById('single-target-dir');
    await loadSingleDirs(input.value || '');
    document.getElementById('single-dir-modal').style.display = 'flex';
}

async function loadSingleDirs(path) {
    const data = await apiCall(
        `/api/browse-dir?path=${encodeURIComponent(path || '')}`,
        'GET'
    );
    singleDirCurrent = data.current || '';
    singleDirEntries = data.dirs || [];
    document.getElementById('single-dir-current').value = singleDirCurrent || '此电脑';
    renderSingleDirs();
}

function renderSingleDirs() {
    const list = document.getElementById('single-dir-list');
    if (!singleDirEntries.length) {
        list.innerHTML = '<div class="text-muted" style="padding:12px;">没有子目录</div>';
        return;
    }
    list.innerHTML = singleDirEntries.map(dir => {
        const name = dir.split(/[\\/]/).filter(Boolean).pop() || dir;
        return `
            <div style="padding:8px 10px;border-bottom:1px solid var(--border-default);cursor:pointer;">
                <a href="javascript:void(0)" onclick="loadSingleDirs('${dir.replace(/\\/g, '\\\\').replace(/'/g, "\\'")}')">
                    <i class="ph ph-folder"></i> ${escapeHtml(name)}
                </a>
            </div>
        `;
    }).join('');
}

async function goSingleDirParent() {
    if (!singleDirCurrent) return;
    const parent = singleDirCurrent.replace(/[\\/][^\\/]+[\\/]?$/, '');
    await loadSingleDirs(parent || singleDirCurrent);
}

function closeSingleDirDialog() {
    document.getElementById('single-dir-modal').style.display = 'none';
}

function chooseSingleDir() {
    if (singleDirCurrent) {
        document.getElementById('single-target-dir').value = singleDirCurrent;
    }
    closeSingleDirDialog();
}
```

- [ ] **Step 7: Run API and page tests**

```powershell
.\venv\Scripts\python.exe -m pytest tests\test_collection_api.py tests\test_api.py -v
```

Expected: all tests pass.

- [ ] **Step 8: Commit**

```powershell
git add app\main.py app\templates\collect.html tests\test_collection_api.py
git commit -m "feat: add single-work collection workflow"
```

---

### Task 9: Regression Verification And Manual Smoke Checklist

**Files:**

- Modify: `DEVELOPMENT.md`

**Interfaces:**

- Consumes all previous tasks.
- Produces verified tests and a short operator note about the TTD/DoukHub boundary.

- [ ] **Step 1: Run focused tests**

```powershell
.\venv\Scripts\python.exe -m pytest tests\test_collection_batches.py tests\test_collection_planner.py tests\test_ttd_batch_runner.py tests\test_collection_batch_manager.py tests\test_collection_api.py tests\test_single_work.py -v
```

Expected: all pass.

- [ ] **Step 2: Run the full suite**

```powershell
.\venv\Scripts\python.exe -m pytest -v
```

Expected: full suite passes. If unrelated pre-existing tests fail, capture exact test names and errors in the handoff and do not edit unrelated code.

- [ ] **Step 3: Compile changed Python files**

```powershell
.\venv\Scripts\python.exe -m py_compile app\core\database.py app\core\collection_planner.py app\core\ttd_batch_runner.py app\core\collection_batch_manager.py app\core\single_work.py app\main.py
```

Expected: no output and exit code 0.

- [ ] **Step 4: Start the app**

```powershell
.\venv\Scripts\python.exe main.py
```

Expected:

- The app opens on its configured local port.
- TTD API starts.
- `/collect` loads without JavaScript console errors.
- The batch table polls every three seconds.

- [ ] **Step 5: Manual batch smoke test**

Use one small Douyin account:

1. Select mode `incremental` and start the batch.
2. Confirm `TikTokDownloader/Volume/settings.json` contains only that account in `accounts_urls`.
3. Confirm unrelated settings such as `cookie`, `root`, and `name_format` remain unchanged.
4. Confirm batch status changes pending → running → completed.
5. Confirm the account has `last_collected_at`.
6. Run the same account again.
7. Confirm `earliest` is the previous date minus one day in `YYYY/MM/DD`.
8. Confirm TTD skips already downloaded files.
9. Refresh `/collect` during the run and confirm state restores.
10. Cancel a run and confirm unfinished items become cancelled.
11. Stop TTD before a retry and confirm the failure is visible and retryable.

- [ ] **Step 6: Manual single-work smoke test**

1. Paste one Douyin video link and resolve.
2. Choose a directory outside TTD's download root.
3. Download once.
4. Confirm the file name follows the selected template.
5. Confirm this SQL returns the same count before and after:

```sql
SELECT COUNT(*) FROM download_data;
```

Run it against `TikTokDownloader/Volume/DouK-Downloader.db`.

6. Paste one image-work link and confirm numbered files.
7. Download the same work again and confirm the duplicate uses `(2)` rather than overwriting.

- [ ] **Step 7: Document the workflow**

Add this section to `DEVELOPMENT.md`:

```markdown
## 采集功能验证

批量采集使用 TTD 终端模式执行，DoukHub 只改写 `TikTokDownloader/Volume/settings.json` 的 `accounts_urls` / `accounts_urls_tiktok`。验证增量时注意 TTD 要求具体日期格式为 `YYYY/MM/DD`。

单作品下载不会写入 TTD 的 `download_data`，因此同一作品后续整号归档仍可能再次下载。这是有意设计：单作品是灵活取件，整号批量是 TTD 管理的档案库。
```

- [ ] **Step 8: Commit**

```powershell
git add DEVELOPMENT.md
git commit -m "docs: verify TTD collection workflow"
```

---

## Plan Self-Review

- Spec coverage:
  - Account selection and stable Douyin URL: Task 2.
  - Atomic TTD settings update and unrelated field preservation: Task 2.
  - Increment and fixed-window behavior: Tasks 1, 2, and 4.
  - Resident API plus dedicated terminal process: Tasks 3 and 4.
  - Persistent batch and item state: Tasks 1 and 4.
  - Cancel, retry, and page-refresh continuity: Tasks 4, 5, and 6.
  - Single-work metadata and DoukHub-managed files: Tasks 7 and 8.
  - No full work-metadata table and no `download_data` mutation: Tasks 7 and 9.
- Placeholder scan: no task depends on unspecified implementation work; code changes include concrete code or exact replacement markup.
- Type consistency:
  - Planner output fields match database item creation.
  - Runner `account_result.sec_user_id` matches batch item lookup.
  - Manager `start()` keyword arguments match API calls.
  - Single-work function signatures match API wrappers and tests.
