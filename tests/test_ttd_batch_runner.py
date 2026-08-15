import py_compile
import asyncio
import json
import sqlite3
import sys
from types import ModuleType

from app.core.ttd_batch_runner import emit_marker, init_ttd_database, marker_line, run_platform


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


def test_database_init_preserves_existing_preferences_and_adds_defaults(tmp_path):
    with sqlite3.connect(tmp_path / "DouK-Downloader.db") as conn:
        conn.execute(
            "CREATE TABLE config_data (NAME TEXT PRIMARY KEY, VALUE INTEGER)"
        )
        conn.execute("CREATE TABLE option_data (NAME TEXT PRIMARY KEY, VALUE TEXT)")
        conn.executemany(
            "INSERT INTO config_data(NAME, VALUE) VALUES (?, ?)",
            [("Disclaimer", 0), ("Record", 0), ("Logger", 1)],
        )
        conn.execute(
            "INSERT INTO option_data(NAME, VALUE) VALUES ('Language', 'en_US')"
        )

    init_ttd_database(tmp_path)

    with sqlite3.connect(tmp_path / "DouK-Downloader.db") as conn:
        config = dict(conn.execute("SELECT NAME, VALUE FROM config_data"))
        language = conn.execute(
            "SELECT VALUE FROM option_data WHERE NAME = 'Language'"
        ).fetchone()[0]
    assert config == {"Disclaimer": 0, "Record": 0, "Logger": 1}
    assert language == "en_US"

    with sqlite3.connect(tmp_path / "DouK-Downloader.db") as conn:
        conn.execute("DELETE FROM config_data WHERE NAME = 'Logger'")
        conn.execute("DELETE FROM option_data WHERE NAME = 'Language'")
        conn.commit()

    init_ttd_database(tmp_path)
    with sqlite3.connect(tmp_path / "DouK-Downloader.db") as conn:
        config = dict(conn.execute("SELECT NAME, VALUE FROM config_data"))
        language = conn.execute(
            "SELECT VALUE FROM option_data WHERE NAME = 'Language'"
        ).fetchone()[0]
    assert config == {"Disclaimer": 0, "Record": 0, "Logger": 0}
    assert language == "zh_CN"


def test_account_processing_forces_post_tab(tmp_path, monkeypatch):
    settings_dir = tmp_path / "Volume"
    settings_dir.mkdir()
    (settings_dir / "settings.json").write_text(
        json.dumps(
            {
                "accounts_urls": [
                    {
                        "mark": "一号",
                        "url": "https://www.douyin.com/user/sec1",
                        "tab": "favorite",
                        "earliest": "",
                        "latest": "",
                        "enable": True,
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    calls = []

    class FakeTerminal:
        console = object()

        def __init__(self, parameter, database):
            self.parameter = parameter
            self.database = database

        async def check_sec_user_id(self, url, tiktok):
            return "sec1"

        async def deal_account_detail(self, index, sec_user_id, **kwargs):
            calls.append((index, sec_user_id, kwargs))
            return True

    class FakeDownloader:
        parameter = object()
        database = object()

        def check_config(self):
            return None

        async def check_settings(self, update):
            return None

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    async def fake_suspend(index, console):
        return None

    src_module = ModuleType("src")
    application_module = ModuleType("src.application")
    main_terminal_module = ModuleType("src.application.main_terminal")
    custom_module = ModuleType("src.custom")
    application_module.TikTokDownloader = FakeDownloader
    main_terminal_module.TikTok = FakeTerminal
    custom_module.suspend = fake_suspend
    src_module.application = application_module
    src_module.custom = custom_module
    monkeypatch.setitem(sys.modules, "src", src_module)
    monkeypatch.setitem(sys.modules, "src.application", application_module)
    monkeypatch.setitem(sys.modules, "src.application.main_terminal", main_terminal_module)
    monkeypatch.setitem(sys.modules, "src.custom", custom_module)
    monkeypatch.chdir(tmp_path)

    old_path = sys.path[:]
    try:
        result = asyncio.run(run_platform("douyin"))
    finally:
        sys.path[:] = old_path

    assert result == 0
    assert calls == [
        (
            1,
            "sec1",
            {
                "mark": "一号",
                "tab": "post",
                "earliest": "",
                "latest": "",
                "tiktok": False,
            },
        )
    ]
