import pathlib
import sqlite3
import tempfile

import pytest

from app.core.database import Database


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
