"""备份模块测试：验证「主库 + TTD 台账 + TTD 配置」三合一备份。

覆盖点：
- create_backup 一次产出三类文件，且 TTD 失败不影响主库成功
- list_backups 能给每条打上 kind 标记（界面据此决定是否显示「恢复」）
- cleanup_old_backups 三类各自独立保留 N 份
"""
import sqlite3
from pathlib import Path

import pytest

from app.core import backup


def _make_sqlite(path: Path) -> None:
    """造一个合法的 SQLite 库（VACUUM INTO 需要真实可读的库文件）。"""
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE IF NOT EXISTS t (id INTEGER PRIMARY KEY, v TEXT)")
    conn.commit()
    conn.close()


@pytest.fixture
def env(tmp_path, monkeypatch):
    """把「主库」「备份目录」「TTD Volume」三者都指到临时目录，与真实环境隔离。"""
    data_root = tmp_path / "data"
    data_root.mkdir()
    bdir = tmp_path / "backups"
    bdir.mkdir()
    volume = tmp_path / "Volume"
    volume.mkdir()

    _make_sqlite(data_root / "doukhub.db")
    _make_sqlite(volume / "DouK-Downloader.db")
    (volume / "settings.json").write_text('{"cookie": "demo"}', encoding="utf-8")

    monkeypatch.setattr(backup, "db_path", lambda: data_root / "doukhub.db")
    monkeypatch.setattr(backup, "get_backup_dir", lambda: bdir)
    monkeypatch.setattr(backup, "ttd_volume_dir", lambda: volume)
    return bdir, volume


def test_create_backup_makes_three_kinds(env):
    """一次备份应产出：主库 .db + TTD 台账 .db + TTD 配置 .json。"""
    bdir, _ = env
    result = backup.create_backup(reason="测试")

    assert result["success"] is True
    assert result["ttd_error"] is None

    names = sorted(p.name for p in bdir.iterdir())
    assert len(names) == 3
    assert any(n.startswith("doukhub_") and n.endswith(".db") for n in names)
    assert any(n.startswith("ttd-ledger_") and n.endswith(".db") for n in names)
    assert any(n.startswith("ttd-settings_") and n.endswith(".json") for n in names)


def test_backup_filenames_share_timestamp(env):
    """三类文件共用同一个时间戳，便于按时间配对还原。"""
    bdir, _ = env
    backup.create_backup()

    stamps = set()
    for p in bdir.iterdir():
        prefix = p.name.split("_", 1)[0] + "_"
        stamps.add(p.name[len(prefix):].rsplit(".", 1)[0])
    assert len(stamps) == 1


def test_list_backups_marks_kind(env):
    """列表应能区分主库与 TTD 存档；按 kind 过滤只返回该类的条目。"""
    env[0]  # 触发 fixture
    backup.create_backup()

    kinds = sorted(item["kind"] for item in backup.list_backups())
    assert kinds == ["db", "ttd-db", "ttd-settings"]

    assert len(backup.list_backups("db")) == 1
    assert backup.list_backups("db")[0]["filename"].startswith("doukhub_")


def test_cleanup_keeps_each_kind_independently(env):
    """保留策略按类型独立计算：三类各留 1 份，共删 6 份。"""
    bdir, _ = env
    for day in range(3):
        ts = f"2026-09-{20 + day}_10-00-00"
        (bdir / f"doukhub_{ts}.db").write_bytes(b"x")
        (bdir / f"ttd-ledger_{ts}.db").write_bytes(b"x")
        (bdir / f"ttd-settings_{ts}.json").write_bytes(b"x")

    result = backup.cleanup_old_backups(keep_count=1)
    assert result["deleted"] == 6
    assert len(backup.list_backups("db")) == 1
    assert len(backup.list_backups("ttd-db")) == 1
    assert len(backup.list_backups("ttd-settings")) == 1

    # 留下的是最新的那一份（09-22）
    assert backup.list_backups("db")[0]["filename"] == "doukhub_2026-09-22_10-00-00.db"


def test_ttd_missing_reported_without_breaking_db_backup(env):
    """TTD 目录缺失时：主库备份照旧成功，但 ttd_error 要如实报告。"""
    bdir, volume = env
    for f in volume.iterdir():
        f.unlink()

    result = backup.create_backup()
    assert result["success"] is True
    assert result["ttd_error"]
    # 主库那一份仍然产出
    assert [p for p in bdir.iterdir() if p.name.startswith("doukhub_")]


def test_check_daily_backup_only_looks_at_db(env):
    """每日备份判定只看主库：只有 TTD 存档没有主库时，应当补一份主库备份。"""
    bdir, _ = env
    (bdir / "ttd-ledger_2026-09-01_10-00-00.db").write_bytes(b"x")
    (bdir / "ttd-settings_2026-09-01_10-00-00.json").write_bytes(b"x")

    result = backup.check_daily_backup()
    assert result["success"] is True
    assert result["filename"].startswith("doukhub_")
