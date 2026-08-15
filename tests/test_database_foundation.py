import ast
import pathlib
import textwrap
import sqlite3
import tempfile
import asyncio
from types import SimpleNamespace

import pytest

from app.core.database import Database
from app.core import history as history_module
from app.core import ttd_batch_runner as ttd_runner_module
from app.services import downloader as downloader_module


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "foundation.db"


def test_connection_enables_sqlite_foundations(db_path):
    database = Database(db_path=db_path)
    with database._connect() as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000


def test_schema_version_is_persisted_once(db_path):
    database = Database(db_path=db_path)
    with database._connect() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1

    Database(db_path=db_path)
    with database._connect() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1


def test_future_version_is_not_downgraded(db_path, monkeypatch):
    database = Database(db_path=db_path)
    with database._connect() as conn:
        conn.execute("PRAGMA user_version = 2")
        conn.commit()

    monkeypatch.setattr(Database, "SCHEMA_VERSION", 1)
    Database(db_path=db_path)
    with database._connect() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2


def test_history_connections_use_standard_sqlite_foundations(tmp_path):
    history = history_module.HistoryDB(tmp_path)
    with history._connect() as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000

    source = pathlib.Path(history_module.__file__).read_text(encoding="utf-8")
    assert source.count("sqlite3.connect(") == 1


def test_ttd_database_connection_uses_standard_sqlite_foundations(tmp_path, monkeypatch):
    calls = []
    original_connect = ttd_runner_module.sqlite3.connect

    class RecordingConnection:
        def __init__(self, conn):
            self.conn = conn
            self.pragma_calls = []

        def execute(self, sql, *args, **kwargs):
            if sql.startswith("PRAGMA "):
                self.pragma_calls.append(sql)
            return self.conn.execute(sql, *args, **kwargs)

        def executemany(self, *args, **kwargs):
            return self.conn.executemany(*args, **kwargs)

        def commit(self):
            return self.conn.commit()

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            self.conn.close()
            return False

    connections = []

    def connect(path, timeout):
        calls.append((path, timeout))
        connection = RecordingConnection(original_connect(path, timeout=timeout))
        connections.append(connection)
        return connection

    with monkeypatch.context() as patcher:
        patcher.setattr(ttd_runner_module.sqlite3, "connect", connect)
        ttd_runner_module.init_ttd_database(tmp_path)

    with sqlite3.connect(tmp_path / "DouK-Downloader.db") as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert calls == [(tmp_path / "DouK-Downloader.db", 5.0)]
    assert connections[0].pragma_calls == [
        "PRAGMA journal_mode=WAL",
        "PRAGMA foreign_keys=ON",
        "PRAGMA busy_timeout=5000",
    ]


def test_generated_ttd_launcher_uses_standard_sqlite_foundations(tmp_path):
    source = pathlib.Path(downloader_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    launcher_source = None
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "write_text":
            launcher_source = node.args[0].value
            break
    assert launcher_source is not None

    init_source = launcher_source.split("async def init_db():", 1)[1]
    init_source = init_source.split("async def main():", 1)[0]
    init_source = textwrap.dedent("async def init_db():" + init_source)

    connect_calls = []
    statements = []

    class FakeConnection:
        async def execute(self, sql, *args):
            statements.append(sql)
            return None

        async def commit(self):
            return None

    class FakeConnect:
        async def __aenter__(self):
            return FakeConnection()

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    def connect(db_file, **kwargs):
        connect_calls.append((db_file, kwargs))
        return FakeConnect()

    namespace = {"PROJECT_ROOT": tmp_path, "aiosqlite": SimpleNamespace(connect=connect)}
    exec(compile(init_source, "<generated launcher>", "exec"), namespace)
    asyncio.run(namespace["init_db"]())

    database = tmp_path / "DouK-Downloader.db"
    assert connect_calls == [(database, {"timeout": 5.0})]
    assert statements[:3] == [
        "PRAGMA journal_mode=WAL",
        "PRAGMA foreign_keys=ON",
        "PRAGMA busy_timeout=5000",
    ]
    assert "CREATE TABLE IF NOT EXISTS config_data" in statements[3]
