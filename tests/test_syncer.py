"""Syncer 解析逻辑测试"""
import pytest

from app.core.syncer import (
    _parse_rating, _parse_tags, _parse_enabled,
    _parse_collection_record, _parse_cookie_record,
)
from app.core.collector import Account


class TestSyncerParsing:
    """飞书数据解析测试"""

    # --- 评级解析 ---

    def test_parse_rating_number(self):
        """纯数字"""
        assert _parse_rating(1) == 1
        assert _parse_rating(3) == 3
        assert _parse_rating(4) == 4

    def test_parse_rating_numeric_string(self):
        """数字字符串"""
        assert _parse_rating("2") == 2
        assert _parse_rating("4") == 4

    def test_parse_rating_mixed_text(self):
        """文本混合格式（如 '个3'、'街拍2'）"""
        assert _parse_rating("个3") == 3
        assert _parse_rating("街拍2") == 2
        assert _parse_rating("酒吧3，多") == 3
        assert _parse_rating("模特3") == 3

    def test_parse_rating_clamped(self):
        """超出范围时限制在 1-4"""
        assert _parse_rating(0) == 1
        assert _parse_rating(5) == 4
        assert _parse_rating(100) == 4

    def test_parse_rating_default(self):
        """无法解析时默认 3"""
        assert _parse_rating("unknown") == 3
        assert _parse_rating(None) == 3
        assert _parse_rating("") == 3

    # --- 标签解析 ---

    def test_parse_tags_list(self):
        """列表输入"""
        assert _parse_tags(["美食", "旅行"]) == ["美食", "旅行"]

    def test_parse_tags_comma_string(self):
        """逗号分隔字符串"""
        assert _parse_tags("美食, 旅行, 科技") == ["美食", "旅行", "科技"]

    def test_parse_tags_empty(self):
        """空值"""
        assert _parse_tags(None) == []
        assert _parse_tags("") == []
        assert _parse_tags(123) == []

    # --- 启用状态解析 ---

    def test_parse_enabled_bool(self):
        assert _parse_enabled(True) is True
        assert _parse_enabled(False) is False

    def test_parse_enabled_string(self):
        assert _parse_enabled("true") is True
        assert _parse_enabled("是") is True
        assert _parse_enabled("1") is True
        assert _parse_enabled("false") is False
        assert _parse_enabled("否") is False

    def test_parse_enabled_number(self):
        assert _parse_enabled(1) is True
        assert _parse_enabled(0) is False
        assert _parse_enabled(1.0) is True

    # --- 分享表记录解析 ---

    def test_parse_collection_record_basic(self):
        """分享表基本记录解析"""
        record = {
            "record_id": "rec001",
            "fields": {
                "地址": "https://v.douyin.com/abc123",
                "等级": "个3",
                "标签": ["美食", "探店"],
                "账号名称": "测试账号",
                "平台": "抖音",
                "备注": "重要账号",
            },
        }
        data = _parse_collection_record(record)
        assert data["record_id"] == "rec001"
        assert data["link"] == "https://v.douyin.com/abc123"
        assert data["rating"] == 3
        assert data["tags"] == ["美食", "探店"]
        assert data["name"] == "测试账号"
        assert data["platform"] == "抖音"
        assert data["note"] == "重要账号"

    def test_parse_collection_record_url_as_dict(self):
        """URL 字段为 dict 的情况"""
        record = {
            "record_id": "rec002",
            "fields": {
                "地址": {"link": "https://v.douyin.com/xxx", "text": "链接"},
                "等级": 2,
                "标签": "街拍",
            },
        }
        data = _parse_collection_record(record)
        assert data["link"] == "https://v.douyin.com/xxx"
        assert data["rating"] == 2
        assert data["tags"] == ["街拍"]

    def test_parse_collection_record_empty(self):
        """空记录"""
        record = {"record_id": "rec003", "fields": {}}
        data = _parse_collection_record(record)
        assert data.get("link", "") == ""
        assert data.get("rating", 3) == 3  # 默认

    # --- Cookie 表记录解析 ---

    def test_parse_cookie_record(self):
        """Cookie 表记录解析"""
        record = {
            "record_id": "cookie001",
            "fields": {
                "Cookie": "session_id=xxx; token=yyy",
                "平台": "抖音",
                "状态": "正常",
                "启用": True,
                "备注": "小号A",
            },
        }
        data = _parse_cookie_record(record)
        assert data["record_id"] == "cookie001"
        assert data["cookie"] == "session_id=xxx; token=yyy"
        assert data["platform"] == "抖音"
        assert data["status"] == "正常"
        assert data["enabled"] is True
        assert data["note"] == "小号A"
