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
