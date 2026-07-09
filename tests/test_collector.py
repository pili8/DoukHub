"""Collector 模块测试"""
import pytest

from app.core.collector import Collector, Account, TAB_MAP


class TestCollector:
    """采集器测试"""

    def test_account_dataclass_defaults(self):
        """Account 默认值"""
        acc = Account()
        assert acc.name == ""
        assert acc.platform == ""
        assert acc.collection_type == "发布"
        assert acc.enabled is True
        assert acc.rating == 3
        assert acc.tags == []
        assert acc.sec_user_id == ""

    def test_account_dataclass_custom(self):
        """Account 自定义值"""
        acc = Account(
            name="测试",
            platform="抖音",
            rating=5,
            tags=["a", "b"],
        )
        assert acc.name == "测试"
        assert acc.rating == 5
        assert acc.tags == ["a", "b"]

    def test_tab_map(self):
        """采集类型映射"""
        assert TAB_MAP["发布"] == "post"
        assert TAB_MAP["喜欢"] == "favorite"
        assert TAB_MAP["收藏"] == "collection"

    def test_detect_platform_douyin(self):
        """抖音链接识别"""
        c = Collector()
        assert c.detect_platform("https://www.douyin.com/user/abc") == "抖音"
        assert c.detect_platform("https://www.iesdouyin.com/share/user/abc") == "抖音"

    def test_detect_platform_tiktok(self):
        """TikTok 链接识别"""
        c = Collector()
        assert c.detect_platform("https://www.tiktok.com/@user") == "TikTok"

    def test_detect_platform_xhs(self):
        """小红书链接识别"""
        c = Collector()
        assert c.detect_platform("https://www.xiaohongshu.com/user/123") == "小红书"
        assert c.detect_platform("https://xhslink.com/abc") == "小红书"
        assert c.detect_platform("https://www.rednote.com/user/123") == "小红书"

    def test_detect_platform_unknown(self):
        """未知平台"""
        c = Collector()
        assert c.detect_platform("https://www.google.com") == ""
        assert c.detect_platform("not a url") == ""

    def test_collector_init_defaults(self):
        """Collector 默认初始化"""
        c = Collector()
        assert c.ttd_url == "http://127.0.0.1:5555"
        assert c.xhs_url == "http://127.0.0.1:5556"
        assert c.cookie_mode == "random"
        assert c.cookie_usage_limit == 10

    def test_collector_init_custom(self):
        """Collector 自定义初始化"""
        c = Collector(
            ttd_url="http://localhost:8000/",
            xhs_url="http://localhost:8001/",
            cookie_mode="sequential",
            cookie_usage_limit=20,
        )
        assert c.ttd_url == "http://localhost:8000"
        assert c.xhs_url == "http://localhost:8001"
        assert c.cookie_mode == "sequential"
        assert c.cookie_usage_limit == 20
