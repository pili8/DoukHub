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
