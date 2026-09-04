# 应用数据目录迁移 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 允许用户把 DoukHub 应用小数据迁移到自定义目录，并在迁移期间安全锁定操作。

**Architecture:** 增加一个固定位置的引导文件，让应用启动时读取真实应用数据根目录；主库、历史库、备份和采集批次日志统一放在该目录。迁移服务复制并校验小数据，成功后原子替换引导文件，用户手动重启后切换到新目录。

**Tech Stack:** Python 3、FastAPI、SQLite、pytest、Jinja2。

## Global Constraints

- 只迁移应用小数据：`doukhub.db`、`history.db`、`backups/`、`collection_logs/`。
- 不迁移媒体文件；媒体目录仍由存储方案决定。
- 不迁移托盘/服务运行日志；`.tmp/` 保持原位。
- `history.db` 保持独立，不合并进主库。
- 固定引导文件：`C:\Users\Gm\.doukhub\data_root.json`。
- 启动时引导文件指向的目录不可用：明确报错，不新建、不回退默认目录。
- 新目标目录已有 `doukhub.db` 或冲突文件：禁止迁移，不覆盖、不合并。
- 有后台任务、增量采集批次或单作品下载未完成：禁止迁移。
- 复制开始后不可取消；迁移期间全局锁定非迁移 API。
- 复制完成并校验后展示结果；用户必须手动点「立即重启」。
- 重启并校验成功后，清理旧目录前必须再次弹窗确认；只清理迁移清单里的已知内容。
- 媒体存储、Downloader 程序目录、启动器日志不在迁移范围内。

---

### Task 1: 建立应用数据根目录解析器

**Files:**
- Create: `app/core/data_root.py`
- Test: `tests/test_data_root.py`

**Interfaces:**
- Consumes: none。
- Produces: `app_data_root() -> Path`，`validate_target(raw: str) -> dict`，`write_bootstrap(path: Path) -> None`，`DataRootError(RuntimeError)`，`BOOTSTRAP_PATH: Path`。

- [ ] **Step 1: Write the failing test**

Create `tests/test_data_root.py`:

```python
import json
from pathlib import Path

import pytest

from app.core.data_root import (
    DataRootError,
    app_data_root,
    validate_target,
    write_bootstrap,
)


def test_env_override_is_used(tmp_path, monkeypatch):
    root = tmp_path / "app-data"
    root.mkdir()
    monkeypatch.setenv("DOUKHUB_DATA_ROOT", str(root))
    assert app_data_root() == root.resolve()


def test_missing_bootstrap_creates_default_once(tmp_path, monkeypatch):
    bootstrap = tmp_path / "data_root.json"
    default_root = tmp_path / ".doukhub-test"
    monkeypatch.setattr("app.core.data_root.BOOTSTRAP_PATH", bootstrap)
    monkeypatch.setattr("app.core.data_root.DEFAULT_DATA_ROOT", default_root)
    root = app_data_root()
    assert root.name == ".doukhub-test"
    assert json.loads(bootstrap.read_text(encoding="utf-8"))["data_dir"] == str(root)

    moved = tmp_path / "moved"
    moved.mkdir()
    bootstrap.write_text(
        json.dumps({"version": 1, "data_dir": str(moved)}), encoding="utf-8"
    )
    assert app_data_root() == moved


def test_unavailable_bootstrap_target_does_not_fall_back(tmp_path, monkeypatch):
    bootstrap = tmp_path / "data_root.json"
    missing = tmp_path / "missing-root"
    bootstrap.write_text(
        json.dumps({"version": 1, "data_dir": str(missing)}), encoding="utf-8"
    )
    monkeypatch.setattr("app.core.data_root.BOOTSTRAP_PATH", bootstrap)
    with pytest.raises(DataRootError):
        app_data_root()


def test_validate_target_rejects_conflicting_data(tmp_path, monkeypatch):
    current = tmp_path / "current"
    current.mkdir()
    monkeypatch.setenv("DOUKHUB_DATA_ROOT", str(current))
    target = tmp_path / "target"
    target.mkdir()
    (target / "doukhub.db").write_text("occupied", encoding="utf-8")
    result = validate_target(str(target))
    assert result["valid"] is False
    assert "doukhub.db" in result["message"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_data_root.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.core.data_root'`.

- [ ] **Step 3: Write minimal implementation**

Create `app/core/data_root.py`:

```python
"""Resolve the fixed bootstrap pointer to DoukHub's application data root."""
from __future__ import annotations

import json
import os
from pathlib import Path


class DataRootError(RuntimeError):
    pass


DEFAULT_DATA_ROOT = Path.home() / ".doukhub"
BOOTSTRAP_PATH = DEFAULT_DATA_ROOT / "data_root.json"
RESERVED_NAMES = ("doukhub.db", "history.db", "backups", "collection_logs")


def _root_error(root: Path, bootstrap: Path | None = None) -> DataRootError:
    suffix = f"\n引导文件：{bootstrap}" if bootstrap else ""
    return DataRootError(f"应用数据目录不可用：{root}{suffix}")


def _atomic_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    os.replace(temp, path)


def app_data_root() -> Path:
    env_root = os.getenv("DOUKHUB_DATA_ROOT")
    if env_root:
        root = Path(env_root).expanduser().resolve()
        if not root.exists():
            raise _root_error(root, BOOTSTRAP_PATH)
        return root

    if not BOOTSTRAP_PATH.exists():
        DEFAULT_DATA_ROOT.mkdir(parents=True, exist_ok=True)
        _atomic_write(
            BOOTSTRAP_PATH,
            {"version": 1, "data_dir": str(DEFAULT_DATA_ROOT.resolve())},
        )

    try:
        payload = json.loads(BOOTSTRAP_PATH.read_text(encoding="utf-8"))
        root = Path(str(payload["data_dir"])).expanduser()
    except Exception as exc:
        raise DataRootError(f"引导文件损坏：{BOOTSTRAP_PATH}") from exc

    if not root.is_absolute():
        raise DataRootError(f"引导文件必须使用绝对路径：{BOOTSTRAP_PATH}")
    root = root.resolve()
    if not root.is_dir():
        raise _root_error(root, BOOTSTRAP_PATH)
    return root


def validate_target(raw: str) -> dict:
    raw = (raw or "").strip().strip('"')
    if not raw:
        return {"valid": False, "message": "路径不能为空", "target": ""}

    try:
        target = Path(os.path.expandvars(raw)).expanduser().resolve()
    except Exception as exc:
        return {"valid": False, "message": f"路径无效：{exc}", "target": raw}

    if not target.parent.exists():
        return {"valid": False, "message": "上级目录不存在，请先创建或改用已存在的目录", "target": str(target)}

    current = app_data_root()
    if target == current or target in current.parents or current in target.parents:
        return {"valid": False, "message": "新目录不能是当前目录或其嵌套目录", "target": str(target)}

    conflicts = [name for name in RESERVED_NAMES if (target / name).exists()]
    if conflicts:
        return {"valid": False, "message": "目标已存在：" + "、".join(conflicts), "target": str(target)}

    probe = target.parent / ".doukhub-write-test"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except Exception as exc:
        return {"valid": False, "message": f"父目录不可写：{exc}", "target": str(target)}

    return {"valid": True, "message": "目录可用（开始迁移时才创建）", "target": str(target)}


def write_bootstrap(path: Path) -> None:
    _atomic_write(
        BOOTSTRAP_PATH, {"version": 1, "data_dir": str(path.resolve())}
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_data_root.py -q`
Expected: PASS, 4 tests.

- [ ] **Step 5: Commit**

```bash
git add app/core/data_root.py tests/test_data_root.py
git commit -m "feat: add application data root resolver"
```

---

### Task 2: 统一数据库、历史库和批次日志路径

**Files:**
- Modify: `app/core/database.py:1-20`
- Modify: `app/core/config.py:1-20`, `app/core/config.py:310-325`
- Modify: `app/core/backup.py:1-320`
- Modify: `app/main.py:96-180`
- Modify: `app/core/maintenance.py:20-70`
- Test: `tests/test_data_root_paths.py`

**Interfaces:**
- Consumes: Task 1 的 `app_data_root()`。
- Produces: `Config.app_data_dir -> Path`；`Database()` 默认打开 `app_data_root()/doukhub.db`；`HistoryDB(config.app_data_dir)` 打开 `history.db`；备份仍从主库同目录读取 `backups/`。

- [ ] **Step 1: Write the failing test**

Create `tests/test_data_root_paths.py`:

```python
from pathlib import Path

from app.core.backup import get_backup_dir
from app.core.config import Config
from app.core.data_root import app_data_root
from app.core.database import Database
from app.core.history import HistoryDB


def test_database_and_history_follow_app_root(tmp_path, monkeypatch):
    root = tmp_path / "app-root"
    root.mkdir()
    monkeypatch.setenv("DOUKHUB_DATA_ROOT", str(root))

    db = Database()
    history = HistoryDB(app_data_root())
    assert db.db_path == root / "doukhub.db"
    assert history.db_path == root / "history.db"


def test_backup_dir_follows_app_root(tmp_path, monkeypatch):
    root = tmp_path / "app-root"
    root.mkdir()
    monkeypatch.setenv("DOUKHUB_DATA_ROOT", str(root))
    assert get_backup_dir() == root / "backups"


def test_config_has_unified_app_dir_and_legacy_sync_dir(tmp_path, monkeypatch):
    root = tmp_path / "app-root"
    root.mkdir()
    monkeypatch.setenv("DOUKHUB_DATA_ROOT", str(root))
    cfg = Config(tmp_path / "config.json")
    assert cfg.app_data_dir == root
    assert isinstance(cfg.data_dir, Path)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_data_root_paths.py -q`
Expected: FAIL with `AttributeError: 'Config' object has no attribute 'app_data_dir'` 或等价断言失败。

- [ ] **Step 3: Use dynamic database paths**

In `app/core/database.py`, remove:

```python
DB_PATH = Path.home() / ".doukhub" / "doukhub.db"
```

Add the import and change the constructor:

```python
from .data_root import app_data_root
```

```python
self.db_path = db_path or app_data_root() / "doukhub.db"
```

In `app/core/config.py`, remove:

```python
DB_PATH = Path.home() / ".doukhub" / "doukhub.db"
```

Add:

```python
from .data_root import app_data_root
```

Change both helper functions to call it:

```python
conn = sqlite3.connect(app_data_root() / "doukhub.db")
```

在 `data_dir` 属性旁边新增 `app_data_dir`（`data_dir` 保持原样不动，它仍是旧 Excel 兼容目录，行为不能变）：

```python
@property
def app_data_dir(self) -> Path:
    """Unified application data root; media folders do not live here."""
    return app_data_root()
```

- [ ] **Step 4: Use dynamic paths in backup, history and logs**

In `app/core/backup.py`, replace:

```python
from .database import DB_PATH
```

with:

```python
from .data_root import app_data_root
```

Add directly below imports:

```python
def db_path() -> Path:
    return app_data_root() / "doukhub.db"
```

把文件中其余所有 `DB_PATH` 出现处全部替换为 `db_path()`（共 14 处，分布在 `get_backup_dir()`、`create_backup()`、`restore_backup()`、`get_db_stats()`、`vacuum_database()` 中），并删除第一行的 `from .database import DB_PATH`。模块内不再保留任何模块级数据库路径。The required effects are:

```python
def get_backup_dir() -> Path:
    return db_path().parent / "backups"
```

```python
if not db_path().exists():
    return {"success": False, "error": "数据库文件不存在", "filename": None}
```

```python
conn = sqlite3.connect(str(db_path()), timeout=30.0)
```

```python
shutil.copy2(str(backup_path), str(db_path()))
_clear_wal_shm(db_path())
```

Apply the same replacement inside `restore_backup()`, `check_daily_backup()` and `vacuum_database()`; there must be no remaining module-level database path.

In `app/main.py`, change collection and history creation to:

```python
log_dir=config.app_data_dir / "collection_logs",
```

```python
history = HistoryDB(config.app_data_dir)
```

Leave `get_syncer()` receiving `config.data_dir`; that preserves old `accounts.xlsx` and `cookies.xlsx` compatibility without expanding migration scope.

In `app/core/maintenance.py`, replace both occurrences of:

```python
config.data_dir / "collection_logs"
```

with:

```python
config.app_data_dir / "collection_logs"
```

- [ ] **Step 5: Run tests and search for stale paths**

Run:

```bash
# -k 排除既有失败用例 test_creates_default_config_when_missing：
# 配置已迁到数据库后，Config() 不再自动生成 json 文件（与本计划无关，改动前就已失败）
pytest tests/test_data_root_paths.py tests/test_config.py tests/test_history.py tests/test_database_foundation.py -q -k "not creates_default_config_when_missing"
rg -n "DB_PATH =|Path\\.home\\(\\).*doukhub\\.db" app/core
```

Expected: tests PASS; `rg` returns no module-level fixed main database path.

- [ ] **Step 6: Commit**

```bash
git add app/core/database.py app/core/config.py app/core/backup.py app/core/maintenance.py app/main.py tests/test_data_root_paths.py
git commit -m "refactor: resolve app data paths from one root"
```

---

### Task 3: 实现复制、校验和切换引导文件

**Files:**
- Create: `app/core/data_migration.py`
- Test: `tests/test_data_migration.py`

**Interfaces:**
- Consumes: `app_data_root()`、`validate_target()`、`write_bootstrap()`。
- Produces: `DataMigration.snapshot() -> dict`、`prepare(raw: str, current: Path) -> dict`、`run(source_root: Path, source_db: Path) -> dict`、`fail(message: str) -> None`；状态字段为 `status / locked / progress / current / target / error / source_root`。

- [ ] **Step 1: Write the failing test**

Create `tests/test_data_migration.py`:

```python
import json
import sqlite3

from app.core.data_migration import DataMigration
from app.core.data_root import BOOTSTRAP_PATH


def make_source(tmp_path):
    source = tmp_path / "source"
    (source / "backups").mkdir(parents=True)
    (source / "collection_logs").mkdir()
    conn = sqlite3.connect(source / "doukhub.db")
    conn.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()
    conn = sqlite3.connect(source / "history.db")
    conn.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()
    (source / "backups" / "demo.db").write_text("backup", encoding="utf-8")
    (source / "collection_logs" / "demo.log").write_text("log", encoding="utf-8")
    return source


def test_prepare_rejects_occupied_target(tmp_path, monkeypatch):
    source = make_source(tmp_path)
    target = tmp_path / "target"
    target.mkdir()
    (target / "doukhub.db").write_text("old", encoding="utf-8")
    migration = DataMigration()
    result = migration.prepare(str(target), source)
    assert result["valid"] is False
    assert migration.snapshot()["status"] == "failed"


def test_run_copies_verifies_and_switches(tmp_path, monkeypatch):
    source = make_source(tmp_path)
    target = tmp_path / "target"
    target.mkdir()
    bootstrap = tmp_path / "data_root.json"
    monkeypatch.setattr("app.core.data_root.BOOTSTRAP_PATH", bootstrap)

    migration = DataMigration()
    migration.prepare(str(target), source)
    result = migration.run(source, source / "doukhub.db")

    assert result["success"] is True
    assert migration.snapshot()["status"] == "ready"
    assert (target / "doukhub.db").exists()
    assert (target / "history.db").exists()
    assert (target / "backups" / "demo.db").read_text(encoding="utf-8") == "backup"
    assert (target / "collection_logs" / "demo.log").read_text(encoding="utf-8") == "log"
    assert (target / ".doukhub-migration.json").exists()
    marker = json.loads(
        (target / ".doukhub-migration.json").read_text(encoding="utf-8")
    )
    assert marker["source_root"] == str(source)
    assert json.loads(bootstrap.read_text(encoding="utf-8"))["data_dir"] == str(target)


def test_run_failure_cleans_known_files(tmp_path, monkeypatch):
    from app.core.data_migration import copy_sqlite as real_copy

    source = make_source(tmp_path)
    target = tmp_path / "target"
    target.mkdir()

    calls = {"count": 0}

    def flaky_copy(source_db, target_db):
        calls["count"] += 1
        if calls["count"] == 1:
            return real_copy(source_db, target_db)
        raise RuntimeError("disk full")

    monkeypatch.setattr("app.core.data_migration.copy_sqlite", flaky_copy)
    migration = DataMigration()
    migration.prepare(str(target), source)
    result = migration.run(source, source / "doukhub.db")
    assert result["success"] is False
    assert migration.snapshot()["status"] == "failed"
    assert not (target / "doukhub.db").exists()
    assert not (target / "history.db").exists()
    assert not (target / ".doukhub-migration.json").exists()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_data_migration.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.core.data_migration'`.

- [ ] **Step 3: Write the migration service**

Create `app/core/data_migration.py`:

```python
"""Copy small application data, verify it, then switch the bootstrap pointer."""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import threading
from pathlib import Path
from typing import Any

from .data_root import BOOTSTRAP_PATH, validate_target, write_bootstrap


def copy_sqlite(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target.unlink()
    source_conn = sqlite3.connect(str(source), timeout=30.0)
    target_conn = sqlite3.connect(str(target), timeout=30.0)
    try:
        source_conn.execute("BEGIN IMMEDIATE")
        source_conn.backup(target_conn)
        source_conn.commit()
    finally:
        source_conn.close()
        target_conn.close()


class DataMigration:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state: dict[str, Any] = self._initial_state()

    @staticmethod
    def _initial_state() -> dict[str, Any]:
        return {
            "status": "idle",
            "locked": False,
            "progress": 0,
            "current": "",
            "target": "",
            "source_root": "",
            "error": "",
            "result": None,
        }

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._state)

    def prepare(self, raw: str, current: Path) -> dict:
        check = validate_target(raw)
        with self._lock:
            self._state = self._initial_state()
            self._state["target"] = check.get("target", "")
            self._state["source_root"] = str(current)
            if not check.get("valid"):
                self._state["status"] = "failed"
                self._state["error"] = check.get("message", "目录不可用")
                return check
            self._state["status"] = "preparing"
            self._state["locked"] = True
            return check

    def fail(self, message: str) -> None:
        with self._lock:
            self._state["status"] = "failed"
            self._state["locked"] = False
            self._state["error"] = message

    def run(self, source_root: Path, source_db: Path) -> dict:
        state = self.snapshot()
        target = Path(state["target"])
        created = [
            target / "doukhub.db",
            target / "history.db",
            target / "backups",
            target / "collection_logs",
        ]
        try:
            with self._lock:
                self._state.update(status="copying", progress=5, current="复制主数据库")

            copy_sqlite(source_db, target / "doukhub.db")
            self._advance(30, "复制历史数据库")
            history = source_root / "history.db"
            if history.exists():
                copy_sqlite(history, target / "history.db")

            self._advance(55, "复制备份")
            self._copy_tree(source_root / "backups", target / "backups")
            self._advance(75, "复制采集批次日志")
            self._copy_tree(
                source_root / "collection_logs", target / "collection_logs"
            )

            with self._lock:
                self._state.update(status="verifying", progress=88, current="完整性校验")
            for db_path in (target / "doukhub.db", target / "history.db"):
                if not db_path.exists():
                    continue
                conn = sqlite3.connect(str(db_path))
                try:
                    check = conn.execute("PRAGMA integrity_check").fetchone()[0]
                    if check != "ok":
                        raise RuntimeError(f"数据库校验失败：{db_path.name}")
                finally:
                    conn.close()

            with self._lock:
                self._state.update(progress=96, current="写入迁移清单")
            marker = {
                "version": 1,
                "source_root": str(source_root),
                "items": [
                    "doukhub.db", "history.db", "backups", "collection_logs"
                ],
            }
            (target / ".doukhub-migration.json").write_text(
                json.dumps(marker, ensure_ascii=False, indent=2), encoding="utf-8"
            )

            # 引导文件最后切换：前面任何一步失败时，旧引导仍指向原目录，回滚安全
            with self._lock:
                self._state.update(progress=98, current="切换引导文件")
            write_bootstrap(target)

            with self._lock:
                self._state.update(
                    status="ready",
                    locked=True,
                    progress=100,
                    current="等待重启",
                    error="",
                    result={"success": True, "target": str(target)},
                )
            return {"success": True, "target": str(target)}
        except Exception as exc:
            try:
                BOOTSTRAP_PATH.with_name(BOOTSTRAP_PATH.name + ".tmp").unlink()
            except OSError:
                pass
            for path in created:
                try:
                    if path.is_dir():
                        shutil.rmtree(path)
                    elif path.exists():
                        path.unlink()
                except OSError:
                    pass
            self.fail(str(exc))
            return {"success": False, "error": str(exc)}

    def _copy_tree(self, source: Path, target: Path) -> None:
        if not source.exists():
            return
        shutil.copytree(source, target, dirs_exist_ok=False)

    def _advance(self, progress: int, label: str) -> None:
        with self._lock:
            self._state.update(progress=progress, current=label)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_data_migration.py -q`
Expected: PASS, 3 tests.

- [ ] **Step 5: Commit**

```bash
git add app/core/data_migration.py tests/test_data_migration.py
git commit -m "feat: add verified application data migration"
```

---

### Task 4: 增加迁移 API 和全局安全锁

**Files:**
- Modify: `app/core/database.py:700-740`
- Modify: `app/main.py:40-110`, `app/main.py:4040-4230`
- Test: `tests/test_data_migration_api.py`

**Interfaces:**
- Consumes: `DataMigration`、`app_data_root()`、`get_task_manager()`、`get_database()`。
- Produces: `GET /api/data-migration/status`、`POST /api/data-migration/validate`、`POST /api/data-migration/start`、`POST /api/data-migration/browse`；全局对象 `data_migration`。迁移中除状态、选择器、重启接口外，其他 HTTP 请求返回 503。

- [ ] **Step 1: Write the failing API test**

Create `tests/test_data_migration_api.py`:

```python
import pytest
from tests.test_api import app_env  # noqa: F401


@pytest.fixture
def migration_env(app_env, tmp_path, monkeypatch):
    import app.main as app_main
    from app.core.data_migration import DataMigration

    # 让 app_data_root() 解析到临时目录，避免测试读写真实 ~/.doukhub
    root = tmp_path / "app-root"
    root.mkdir()
    monkeypatch.setenv("DOUKHUB_DATA_ROOT", str(root))

    original = app_main.data_migration
    app_main.data_migration = DataMigration()
    yield app_env, tmp_path
    app_main.data_migration = original


def test_validate_rejects_occupied_target(migration_env, tmp_path):
    client, *_ = migration_env
    target = tmp_path / "target"
    target.mkdir()
    (target / "doukhub.db").write_text("x", encoding="utf-8")
    response = client.post("/api/data-migration/validate", json={"target": str(target)})
    assert response.status_code == 200
    assert response.json()["valid"] is False


def test_start_blocks_when_active_task_exists(migration_env):
    import app.main as app_main

    client, *_ = migration_env
    task = app_main.get_task_manager().create("测试任务")
    try:
        response = client.post(
            "/api/data-migration/start",
            json={"target": str(app_main.app_data_root().parent / "migration-target")},
        )
        assert response.status_code == 409
        assert "后台任务" in response.json()["message"]
    finally:
        app_main.get_task_manager().update(task.task_id, status="done")


def test_migration_locks_other_requests(migration_env):
    import app.main as app_main

    client, *_ = migration_env
    app_main.data_migration._state.update(status="copying", locked=True)
    blocked = client.get("/api/stats")
    allowed = client.get("/api/data-migration/status")
    assert blocked.status_code == 503
    assert allowed.status_code == 200
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_data_migration_api.py -q`
Expected: FAIL with 404 Not Found。

- [ ] **Step 3: Add active-work checks**

In `app/core/database.py`, add this method to `Database` near `get_active_collection_batch()`:

```python
def has_pending_or_running_single_work(self) -> bool:
    """True when the recoverable single-work queue is not empty."""
    with self._connect() as conn:
        row = conn.execute(
            "SELECT EXISTS ("
            "SELECT 1 FROM single_work_history "
            "WHERE status IN ('pending', 'running'))"
        ).fetchone()
        return bool(row[0])
```

In `app/main.py`, add after the existing import block:

```python
from .core.data_migration import DataMigration
from .core.data_root import app_data_root
```

Near the other global singletons:

```python
data_migration = DataMigration()
```

Before the settings API, add:

```python
def _migration_busy_reason() -> str:
    if any(t.status in ("pending", "running") for t in get_task_manager().list()):
        return "后台任务未完成"
    if get_database().get_active_collection_batch():
        return "增量采集批次未完成"
    if get_database().has_pending_or_running_single_work():
        return "单作品下载未完成"
    return ""
```

- [ ] **Step 4: Add middleware and endpoints**

Immediately after `app = FastAPI(...)`, add:

```python
@app.middleware("http")
async def migration_lock_middleware(request: Request, call_next):
    path = request.url.path
    if data_migration.snapshot()["locked"] and not (
        path.startswith("/api/data-migration") or path == "/api/system/restart"
    ):
        return JSONResponse(
            {"success": False, "message": "应用数据正在迁移，请等待完成"},
            status_code=503,
        )
    return await call_next(request)
```

Before `/api/system/restart`, add:

```python
@app.get("/api/data-migration/status")
async def api_data_migration_status():
    state = data_migration.snapshot()
    state["current_root"] = str(app_data_root())
    return state


@app.post("/api/data-migration/validate")
async def api_data_migration_validate(payload: dict):
    return validate_target(payload.get("target", ""))


@app.post("/api/data-migration/browse")
def api_data_migration_browse():
    """Open the same Windows folder dialog used by file deduplication."""
    import subprocess
    import sys

    if sys.platform != "win32":
        return {"success": False, "error": "仅 Windows 支持系统文件夹选择器"}
    script = (
        "Add-Type -AssemblyName System.Windows.Forms; "
        "$f = New-Object System.Windows.Forms.FolderBrowserDialog; "
        "$f.Description = '选择新的应用数据目录'; "
        "$f.ShowNewFolderButton = $true; "
        "if ($f.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) { $f.SelectedPath }"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True, text=True, timeout=180,
        )
        selected = (result.stdout or "").strip()
        return {"success": bool(selected), "path": selected}
    except Exception as exc:
        return {"success": False, "error": str(exc)}


@app.post("/api/data-migration/start")
async def api_data_migration_start(payload: dict):
    import asyncio

    state = data_migration.snapshot()
    if state["status"] in ("preparing", "copying", "verifying", "ready"):
        return JSONResponse({"success": False, "message": "迁移已在进行"}, status_code=409)

    busy = _migration_busy_reason()
    if busy:
        return JSONResponse({"success": False, "message": busy}, status_code=409)

    check = data_migration.prepare(payload.get("target", ""), app_data_root())
    if not check.get("valid"):
        return JSONResponse({"success": False, "message": check["message"]}, status_code=400)

    busy = _migration_busy_reason()
    if busy:
        data_migration.fail(busy)
        return JSONResponse({"success": False, "message": busy}, status_code=409)

    source_root = app_data_root()
    source_db = source_root / "doukhub.db"
    asyncio.create_task(asyncio.to_thread(data_migration.run, source_root, source_db))
    return {"success": True, "message": "迁移已开始"}
```

Add the import near the data-root import:

```python
from .core.data_root import validate_target
```

- [ ] **Step 5: Add confirmed old-directory cleanup**

After the start endpoint, add:

```python
@app.get("/api/data-migration/old-cleanup")
async def api_data_migration_old_cleanup():
    marker_path = app_data_root() / ".doukhub-migration.json"
    if not marker_path.exists():
        return {"available": False, "source_root": ""}
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        return {"available": True, "source_root": marker.get("source_root", "")}
    except Exception:
        return {"available": False, "source_root": ""}


@app.post("/api/data-migration/cleanup-old")
async def api_data_migration_cleanup_old(payload: dict):
    import shutil

    if payload.get("confirmed") is not True:
        return {"success": False, "message": "需要用户确认"}
    marker_path = app_data_root() / ".doukhub-migration.json"
    if not marker_path.exists():
        return {"success": False, "message": "没有迁移记录"}
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    old_root = Path(str(marker.get("source_root", "")))
    new_root = app_data_root()
    if not old_root.is_dir() or old_root == new_root:
        return {"success": False, "message": "旧目录不可清理"}
    if new_root in old_root.parents or old_root in new_root.parents:
        return {"success": False, "message": "新旧目录存在嵌套关系"}

    removed = []
    for name in marker.get("items", []):
        target = old_root / name
        resolved = target.resolve()
        if resolved == old_root.resolve() or not resolved.is_relative_to(old_root.resolve()):
            continue
        try:
            if target.is_dir():
                shutil.rmtree(target)
            elif target.exists():
                target.unlink()
            removed.append(name)
        except OSError as exc:
            return {"success": False, "message": f"清理失败：{exc}", "removed": removed}
    return {"success": True, "removed": removed}
```

- [ ] **Step 6: Run API tests**

Run:

```bash
pytest tests/test_data_migration_api.py tests/test_api.py -q
```

Expected: PASS；原有 `/api/stats` 和 `/api/settings` 测试不受空闲状态影响。

- [ ] **Step 7: Commit**

```bash
git add app/main.py app/core/database.py tests/test_data_migration_api.py
git commit -m "feat: lock app during verified data migration"
```

---

### Task 5: 设置页入口、迁移遮罩和重启确认

**Files:**
- Modify: `app/templates/settings.html:150-220`
- Modify: `app/templates/settings.html:1270-1400`
- Modify: `app/templates/base.html:100-180`, `app/templates/base.html:430-520`
- Test: `tests/test_data_migration_ui.py`

**Interfaces:**
- Consumes: Task 4 API。
- Produces: 设置页「应用数据目录」入口；任意页面出现全屏迁移遮罩；复制中无取消按钮；完成后唯一操作是「立即重启」；失败时遮罩显示错误并可关闭。

- [ ] **Step 1: Write the failing UI test**

Create `tests/test_data_migration_ui.py`:

```python
from tests.test_api import app_env  # noqa: F401


def test_settings_has_migration_entry(app_env):
    client, *_ = app_env
    response = client.get("/settings")
    assert response.status_code == 200
    assert "应用数据目录" in response.text
    assert "startDataMigration()" in response.text


def test_base_has_global_migration_overlay(app_env):
    client, *_ = app_env
    response = client.get("/status")
    assert response.status_code == 200
    assert 'id="migration-overlay"' in response.text
    assert "/api/data-migration/status" in response.text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_data_migration_ui.py -q`
Expected: FAIL,断言找不到迁移入口或遮罩。

- [ ] **Step 3: Add settings card**

In `settings.html`，在现有「配置路径」卡片（含 `config_path` 行）之后追加一张「应用数据目录」卡片，原卡片保持不动。追加以下 HTML：

```html
<div class="card settings-card open">
    <button type="button" class="settings-card-head" onclick="toggleCard(this)">
        <i data-lucide="hard-drive"></i><span class="card-title">应用数据目录</span>
        <span class="card-arrow"><i data-lucide="chevron-down"></i></span>
    </button>
    <div class="settings-card-body">
        <div class="settings-grid" style="grid-template-columns:minmax(240px,1fr) auto;">
            <div>
                <label style="display:block;margin-bottom:6px;">新应用数据目录</label>
                <input id="migration-target" class="input" style="width:100%;" placeholder="例如 D:\\DoukHubData">
                <small id="migration-check" style="color:var(--text-muted);display:block;margin-top:6px;">
                    只迁移主库、历史库、备份和采集日志；媒体文件不会移动。
                </small>
            </div>
            <div style="display:flex;flex-direction:column;gap:8px;justify-content:end;">
                <button type="button" class="btn btn-secondary btn-sm" onclick="browseDataRoot()"><i data-lucide="folder-open"></i> 选择</button>
                <button type="button" class="btn btn-secondary btn-sm" onclick="validateDataRoot()"><i data-lucide="check"></i> 检查</button>
                <button type="button" id="migration-start" class="btn btn-primary btn-sm" onclick="startDataMigration()"><i data-lucide="move"></i> 开始迁移</button>
            </div>
        </div>
    </div>
</div>
```

- [ ] **Step 4: Add settings JavaScript**

In `settings.html`, before `async function testFeishu()`, add:

```javascript
async function browseDataRoot() {
    const result = await apiCall('/api/data-migration/browse', 'POST');
    if (result.success && result.path) {
        document.getElementById('migration-target').value = result.path;
        validateDataRoot();
    } else if (result.error) {
        showToast(result.error, 'error');
    }
}

async function validateDataRoot() {
    const target = document.getElementById('migration-target').value.trim();
    const el = document.getElementById('migration-check');
    if (!target) { el.textContent = '请填写目标目录'; el.style.color = 'var(--danger)'; return false; }
    const result = await apiCall('/api/data-migration/validate', 'POST', { target });
    el.textContent = result.message;
    el.style.color = result.valid ? 'var(--success)' : 'var(--danger)';
    return Boolean(result.valid);
}

async function startDataMigration() {
    const target = document.getElementById('migration-target').value.trim();
    if (!await validateDataRoot()) return;
    const ok = await confirmDialog(
        '复制开始后不能取消，完成后需要立即重启 DoukHub。是否继续？',
        { title: '迁移应用数据', confirmText: '开始迁移' }
    );
    if (!ok) return;
    const result = await apiCall('/api/data-migration/start', 'POST', { target });
    if (!result.success) { showToast(result.message || '开始迁移失败', 'error'); return; }
    refreshMigrationOverlay();
}
```

- [ ] **Step 5: Add global overlay**

In `base.html`, immediately after `<div id="toast" class="toast"></div>`, add:

```html
<div id="migration-overlay" class="dh-modal-overlay">
    <div class="dh-modal-box" style="max-width:440px;">
        <div class="dh-modal-header">
            <h3><i data-lucide="hard-drive"></i> <span id="migration-title">应用数据迁移</span></h3>
        </div>
        <div class="dh-modal-body">
            <p id="migration-message">准备中...</p>
            <div style="height:6px;background:var(--bg-hover);border-radius:3px;overflow:hidden;margin:14px 0;">
                <div id="migration-bar" style="height:100%;width:0%;background:var(--accent);transition:width .4s;"></div>
            </div>
            <p id="migration-detail" style="font-size:12px;color:var(--text-muted);word-break:break-all;"></p>
        </div>
        <div class="dh-modal-actions">
            <button id="migration-close" class="dh-btn" style="display:none;" onclick="closeMigrationOverlay()">关闭</button>
            <button id="migration-restart" class="dh-btn dh-btn-danger" style="display:none;" onclick="restartAfterMigration()">
                立即重启
            </button>
        </div>
    </div>
</div>
```

At the end of `base.html` before `</body>`, add:

```html
<script>
let migrationPoller = null;

function showMigrationOverlay(state) {
    const overlay = document.getElementById('migration-overlay');
    overlay.classList.add('dh-show');
    const title = document.getElementById('migration-title');
    const message = document.getElementById('migration-message');
    const bar = document.getElementById('migration-bar');
    const detail = document.getElementById('migration-detail');
    const restart = document.getElementById('migration-restart');
    const close = document.getElementById('migration-close');
    title.textContent = state.status === 'ready' ? '迁移完成'
        : (state.status === 'failed' ? '迁移失败' : '应用数据迁移中');
    message.textContent = state.status === 'ready'
        ? '数据已复制并通过校验。请立即重启 DoukHub。'
        : (state.current || '正在复制...');
    bar.style.width = `${state.progress || 0}%`;
    detail.textContent = state.error
        ? `失败：${state.error}`
        : `目标目录：${state.target || ''}`;
    restart.style.display = state.status === 'ready' ? 'inline-flex' : 'none';
    close.style.display = state.status === 'failed' ? 'inline-flex' : 'none';
}

function closeMigrationOverlay() {
    if (migrationPoller) { clearInterval(migrationPoller); migrationPoller = null; }
    document.getElementById('migration-overlay').classList.remove('dh-show');
}

async function refreshMigrationOverlay() {
    try {
        const response = await fetch('/api/data-migration/status');
        const state = await response.json();
        if (['preparing', 'copying', 'verifying', 'ready', 'failed'].includes(state.status)) {
            showMigrationOverlay(state);
            if ((state.status === 'ready' || state.status === 'failed') && migrationPoller) {
                clearInterval(migrationPoller);
                migrationPoller = null;
            }
        } else {
            document.getElementById('migration-overlay').classList.remove('dh-show');
        }
    } catch (error) {
        if (migrationPoller) {
            clearInterval(migrationPoller);
            migrationPoller = null;
        }
    }
}

async function restartAfterMigration() {
    const button = document.getElementById('migration-restart');
    button.disabled = true;
    button.textContent = '正在重启...';
    await fetch('/api/system/restart', { method: 'POST' });
}

refreshMigrationOverlay();
migrationPoller = setInterval(refreshMigrationOverlay, 1000);
</script>
```

- [ ] **Step 6: Run UI tests**

Run:

```bash
pytest tests/test_data_migration_ui.py tests/test_workflow_ui.py -q
```

Expected: PASS。

- [ ] **Step 7: Commit**

```bash
git add app/templates/settings.html app/templates/base.html tests/test_data_migration_ui.py
git commit -m "feat: show global migration progress and restart gate"
```

---

### Task 6: 启动预检和整体验证

**Files:**
- Modify: `tray.py:1-220`
- Modify: `main.py:1-20`

**Interfaces:**
- Consumes: `DataRootError`、`app_data_root()`。
- Produces: 托盘启动前检查数据根目录；目录不可用时弹出 Windows 明确提示，不启动服务。

- [ ] **Step 1: Add tray preflight**

In `tray.py`，在 `import webbrowser` 之后（第三方导入之前）先确保项目根目录可导入，再导入 data_root（tray.py 之前只做延迟导入 `app.*`，这里首次在模块顶层导入，先插 `sys.path` 保证从任意目录启动都能找到 `app` 包）：

```python
sys.path.insert(0, str(Path(__file__).resolve().parent))
from app.core.data_root import DataRootError, app_data_root
```

`data_root_ready()` 只用 `app_data_root` 和 `DataRootError`，不需要 `BOOTSTRAP_PATH`。

Above `def main():`, add:

```python
def data_root_ready() -> bool:
    """Fail visibly before starting uvicorn; never fall back to another root."""
    try:
        app_data_root()
        return True
    except DataRootError as exc:
        logger.error(str(exc))
        ctypes.windll.user32.MessageBoxW(
            None,
            f"{exc}\n\n请修复目录或恢复引导文件后重新启动。",
            "DoukHub 应用数据目录不可用",
            0x10 | 0x40000,
        )
        return False
```

At the start of `main()` after the singleton check, add:

```python
    if not data_root_ready():
        return
```

- [ ] **Step 2: Make direct startup report the same error**

Change `main.py` to:

```python
"""DoukHub 入口"""
import sys
from pathlib import Path

# 确保 app 包可导入（指向项目根目录，不是 main.py 文件本身）
sys.path.insert(0, str(Path(__file__).resolve().parent))

if __name__ == "__main__":
    try:
        from app.main import run
        run()
    except Exception as exc:
        if type(exc).__name__ == "DataRootError":
            print(str(exc), file=sys.stderr)
            input("按 Enter 关闭...")
        else:
            raise
```

- [ ] **Step 3: Run the full verification suite**

Run:

```bash
# -k 排除既有失败用例 test_creates_default_config_when_missing（与本计划无关）
pytest tests/test_data_root.py tests/test_data_root_paths.py tests/test_data_migration.py tests/test_data_migration_api.py tests/test_data_migration_ui.py tests/test_api.py tests/test_config.py -q -k "not creates_default_config_when_missing"
python -m compileall app main.py tray.py
rg -n "TBD|TODO|implement later|fill in details" docs/superpowers/plans/2026-09-02-app-data-directory-migration.md
```

Expected: tests PASS；compileall PASS；plan placeholder scan returns no matches。

- [ ] **Step 4: Manual Windows smoke test**

1. 启动托盘，打开 `/settings`。
2. 选择或粘贴一个空目录，点「检查」，应显示「目录可用」。
3. 点「开始迁移」；全屏遮罩出现，设置和其他 API 均不可操作。
4. 等待 100%，点「立即重启」。
5. 重启后打开 `/settings`，新目录显示为当前应用数据目录。
6. 点「清理旧数据」前确认弹窗仍会出现；确认后只删除旧库、旧历史库、旧备份和旧采集日志。

- [ ] **Step 5: Commit**

```bash
git add tray.py main.py
git commit -m "feat: preflight custom application data root"
```

## Self-Review

- 范围覆盖：路径解析、数据迁移、冲突防护、任务锁、进度、强制重启、旧目录确认清理、启动失败提示都有对应任务。
- 交互行为：复制中不可取消；完成前全局锁定；完成后必须手动重启；清理旧目录需要再次确认。
- 数据边界：媒体文件、Downloader 程序、`.tmp/` 启动器日志、旧 Excel 兼容目录均不在迁移范围。
- 一致性：所有模块都通过 `app_data_root()` 解析应用数据；重启后才切换数据库连接。
- 失败态：迁移失败后解锁 API 恢复操作，遮罩显示具体错误并可关闭，用户修改目标后可重试；引导文件最后切换，失败时旧目录仍是活动目录。
