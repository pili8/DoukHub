"""短链接解析模块测试"""
import pytest

from app.core.link_resolver import (
    detect_platform,
    extract_sec_user_id,
    extract_detail_id,
)


class TestLinkResolver:
    """短链接解析测试"""

    def test_detect_platform_douyin(self):
        assert detect_platform("https://www.douyin.com/user/abc") == "抖音"
        assert detect_platform("https://v.douyin.com/xxx") == "抖音"
        assert detect_platform("https://www.iesdouyin.com/share/user/abc") == "抖音"

    def test_detect_platform_tiktok(self):
        assert detect_platform("https://www.tiktok.com/@user") == "TikTok"
        assert detect_platform("https://vm.tiktok.com/xxx") == "TikTok"

    def test_detect_platform_xhs(self):
        assert detect_platform("https://www.xiaohongshu.com/user/123") == "小红书"
        assert detect_platform("https://xhslink.com/abc") == "小红书"
        assert detect_platform("https://www.rednote.com/user/123") == "小红书"

    def test_detect_platform_unknown(self):
        assert detect_platform("https://www.google.com") == ""
        assert detect_platform("not a url") == ""

    def test_extract_sec_user_id_douyin(self):
        url = "https://www.douyin.com/user/MS4wLjABAAAA123456"
        assert extract_sec_user_id(url, "抖音") == "MS4wLjABAAAA123456"

    def test_extract_sec_user_id_iesdouyin(self):
        url = "https://www.iesdouyin.com/share/user/MS4wLjABAAAA789?sec_uid=xxx"
        assert extract_sec_user_id(url, "抖音") == "MS4wLjABAAAA789"

    def test_extract_sec_user_id_xhs(self):
        url = "https://www.xiaohongshu.com/user/profile/abc123"
        assert extract_sec_user_id(url, "小红书") == "abc123"

    def test_extract_sec_user_id_empty(self):
        assert extract_sec_user_id("", "抖音") == ""
        assert extract_sec_user_id("https://www.douyin.com/", "抖音") == ""

    def test_extract_detail_id(self):
        url = "https://www.douyin.com/video/7123456789012345678"
        assert extract_detail_id(url) == "7123456789012345678"

    def test_extract_detail_id_note(self):
        url = "https://www.douyin.com/note/7123456789012345678"
        assert extract_detail_id(url) == "7123456789012345678"

    def test_extract_detail_id_empty(self):
        assert extract_detail_id("") == ""
        assert extract_detail_id("https://www.douyin.com/user/abc") == ""
