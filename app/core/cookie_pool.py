"""Cookie 轮换管理"""
import random
from itertools import cycle


class CookiePool:
    """Cookie 池管理器，支持随机/顺序轮换"""

    def __init__(
        self,
        cookies: list[str],
        mode: str = "random",
        usage_limit: int = 10,
    ):
        self.cookies = [c.strip() for c in cookies if c.strip()]
        self.mode = mode
        self.usage_limit = usage_limit
        self._usage_count: dict[str, int] = {c: 0 for c in self.cookies}
        self._cycle = cycle(self.cookies) if self.cookies else None
        self._current: str | None = None

    @classmethod
    def from_field(cls, cookie_field: str, mode: str = "random", usage_limit: int = 10) -> "CookiePool":
        """从飞书表的 Cookie 字段创建 CookiePool
        支持换行分隔的多个 Cookie
        """
        cookies = [c.strip() for c in cookie_field.split("\n") if c.strip()]
        return cls(cookies, mode, usage_limit)

    def get_cookie(self) -> str:
        """获取一个 Cookie"""
        if not self.cookies:
            return ""

        if self.mode == "random":
            return self._get_random()
        else:
            return self._get_sequential()

    def _get_random(self) -> str:
        """随机模式：随机选择一个 Cookie"""
        # 找到还没达到使用上限的 Cookie
        available = [c for c in self.cookies if self._usage_count.get(c, 0) < self.usage_limit]
        if not available:
            # 全部达到上限，重置计数
            self._usage_count = {c: 0 for c in self.cookies}
            available = self.cookies
        cookie = random.choice(available)
        self._usage_count[cookie] = self._usage_count.get(cookie, 0) + 1
        return cookie

    def _get_sequential(self) -> str:
        """顺序模式：按顺序轮换"""
        if self._cycle is None:
            return ""
        if self._current is None or self._usage_count.get(self._current, 0) >= self.usage_limit:
            self._current = next(self._cycle)
            self._usage_count[self._current] = 0
        self._usage_count[self._current] = self._usage_count.get(self._current, 0) + 1
        return self._current

    def reset(self) -> None:
        """重置所有计数"""
        self._usage_count = {c: 0 for c in self.cookies}

    @property
    def count(self) -> int:
        return len(self.cookies)

    @property
    def has_cookies(self) -> bool:
        return len(self.cookies) > 0
