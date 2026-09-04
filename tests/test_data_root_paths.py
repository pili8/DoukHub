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
