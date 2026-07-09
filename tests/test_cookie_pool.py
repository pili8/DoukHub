"""CookiePool 模块测试"""
import pytest

from app.core.cookie_pool import CookiePool


class TestCookiePool:
    """Cookie 池管理测试"""

    def test_from_field_single_cookie(self):
        """单个 Cookie 解析"""
        pool = CookiePool.from_field("cookie_value_1")
        assert pool.count == 1
        assert pool.has_cookies is True
        assert pool.get_cookie() == "cookie_value_1"

    def test_from_field_multiple_cookies(self):
        """多个 Cookie 换行分隔"""
        pool = CookiePool.from_field("cookie_1\ncookie_2\ncookie_3")
        assert pool.count == 3

    def test_from_field_strips_whitespace(self):
        """空白行和首尾空格被过滤"""
        pool = CookiePool.from_field("  cookie_1  \n\n  cookie_2  \n")
        assert pool.count == 2

    def test_from_field_empty(self):
        """空字符串"""
        pool = CookiePool.from_field("")
        assert pool.count == 0
        assert pool.has_cookies is False
        assert pool.get_cookie() == ""

    def test_random_mode_returns_valid_cookie(self):
        """随机模式返回有效 Cookie"""
        cookies = ["a", "b", "c"]
        pool = CookiePool(cookies, mode="random", usage_limit=100)
        for _ in range(50):
            assert pool.get_cookie() in cookies

    def test_sequential_mode_rotates(self):
        """顺序模式按序轮换"""
        cookies = ["a", "b", "c"]
        pool = CookiePool(cookies, mode="sequential", usage_limit=1)
        results = [pool.get_cookie() for _ in range(6)]
        # 每个 cookie 用 1 次后切换，所以顺序为 a, b, c, a, b, c
        assert results == ["a", "b", "c", "a", "b", "c"]

    def test_usage_limit_triggers_rotation(self):
        """达到使用上限后自动切换"""
        cookies = ["a", "b"]
        pool = CookiePool(cookies, mode="sequential", usage_limit=2)
        results = [pool.get_cookie() for _ in range(4)]
        # a 用 2 次，b 用 2 次
        assert results == ["a", "a", "b", "b"]

    def test_random_mode_resets_when_all_exhausted(self):
        """随机模式全部达到上限后重置计数"""
        cookies = ["a", "b"]
        pool = CookiePool(cookies, mode="random", usage_limit=1)
        # 取 3 次，第 3 次应重置后仍可取到
        results = [pool.get_cookie() for _ in range(4)]
        assert all(r in cookies for r in results)

    def test_reset(self):
        """手动重置计数"""
        pool = CookiePool(["a", "b"], mode="random", usage_limit=1)
        pool.get_cookie()
        pool.reset()
        assert pool._usage_count == {"a": 0, "b": 0}
