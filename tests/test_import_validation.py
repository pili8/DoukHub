"""导入校验和数字分隔符解析测试。"""
import json

import pytest

from app.core import syncer_v2
from app.core.database import Database
from app.core.syncer_v2 import Syncer


def make_syncer(tmp_path, monkeypatch):
    db = Database(tmp_path / "test.db")
    monkeypatch.setattr(syncer_v2, "Database", lambda: db)
    return Syncer(None, None, {}, {"个": "个人"})


class TestParseGradeTags:
    def test_simple_number_tag(self):
        level, tags = Syncer.parse_grade_tags("2多")
        assert level == 2
        assert tags == ["多"]

    def test_tag_number_tag(self):
        """酒吧3多 → 等级3，标签酒吧和多。"""
        level, tags = Syncer.parse_grade_tags("酒吧3多")
        assert level == 3
        assert tags == ["酒吧", "多"]

    def test_number_first_tag(self):
        level, tags = Syncer.parse_grade_tags("3个")
        assert level == 3
        assert tags == ["个"]

    def test_tag_number_only(self):
        level, tags = Syncer.parse_grade_tags("COS2")
        assert level == 2
        assert tags == ["COS"]

    def test_comma_separated(self):
        level, tags = Syncer.parse_grade_tags("个3，多")
        assert level == 3
        assert tags == ["个", "多"]

    def test_multiple_numbers_takes_highest(self):
        level, tags = Syncer.parse_grade_tags("2图3多")
        assert level == 3
        assert set(tags) == {"图", "多"}

    def test_no_number_defaults_level_1(self):
        level, tags = Syncer.parse_grade_tags("图")
        assert level == 1
        assert tags == ["图"]


class TestValidateShareCode:
    def test_valid_code(self):
        ok, reason = Syncer.validate_share_code("Wfdc1A6ewbg")
        assert ok is True
        assert reason == ""

    def test_empty_code(self):
        ok, reason = Syncer.validate_share_code("")
        assert ok is False
        assert "缺少" in reason

    def test_pure_digit_is_uid(self):
        ok, reason = Syncer.validate_share_code("64747796680")
        assert ok is False
        assert "UID" in reason

    def test_short_code_blocked(self):
        ok, reason = Syncer.validate_share_code("abc")
        assert ok is False
        assert "过短" in reason

    def test_illegal_chars_blocked(self):
        ok, reason = Syncer.validate_share_code("abc·123")
        assert ok is False
        assert "非法字符" in reason


class TestImportValidation:
    def test_intercept_missing_code(self, tmp_path, monkeypatch):
        syncer = make_syncer(tmp_path, monkeypatch)
        result = syncer.import_to_collection("酒吧2@")
        assert result.skipped == 1
        assert result.success == 0
        assert "缺少地址" in result.warnings[0]

    def test_intercept_short_code(self, tmp_path, monkeypatch):
        syncer = make_syncer(tmp_path, monkeypatch)
        result = syncer.import_to_collection("图2@abc")
        assert result.skipped == 1
        assert result.success == 0
        assert "过短" in result.warnings[0]

    def test_intercept_pure_digit(self, tmp_path, monkeypatch):
        syncer = make_syncer(tmp_path, monkeypatch)
        result = syncer.import_to_collection("图2@64747796680")
        assert result.skipped == 1
        assert "UID" in result.warnings[0]

    def test_import_full_profile_link(self, tmp_path, monkeypatch):
        syncer = make_syncer(tmp_path, monkeypatch)
        result = syncer.import_to_collection(
            "图3@https://www.douyin.com/user/MS4wLjABAAAAEtfK7xIVdVCD6zsxc0kqbb8qXZSO2H6UNWcxJuBQqUg"
        )
        assert result.success == 1
        rows = syncer.db.get_all_collections()
        assert len(rows) == 1
        assert rows[0]["解析状态"] == "已就绪"

    def test_number_delimiter_at_import(self, tmp_path, monkeypatch):
        syncer = make_syncer(tmp_path, monkeypatch)
        result = syncer.import_to_collection("酒吧3多@Wfdc1A6ewbg")
        assert result.success == 1
        rows = syncer.db.get_all_collections()
        assert rows[0]["等级"] == 3
        assert json.loads(rows[0]["标签"]) == ["酒吧", "多"]

    def test_number_delimiter_json(self, tmp_path, monkeypatch):
        syncer = make_syncer(tmp_path, monkeypatch)
        data = json.dumps({"地址": "Wfdc1A6ewbg", "等级": "酒吧3多"})
        result = syncer.import_to_collection(data)
        assert result.success == 1
        rows = syncer.db.get_all_collections()
        assert rows[0]["等级"] == 3
        assert json.loads(rows[0]["标签"]) == ["酒吧", "多"]

    def test_valid_and_blocked_mixed(self, tmp_path, monkeypatch):
        """合格行入库，拦截行不入库。"""
        syncer = make_syncer(tmp_path, monkeypatch)
        text = "图3@Wfdc1A6ewbg\n图2@abc\n图3@"
        result = syncer.import_to_collection(text)
        assert result.total == 3
        assert result.success == 1
        assert result.skipped == 2
        assert len(syncer.db.get_all_collections()) == 1
