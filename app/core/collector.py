"""采集调度器 — 调用 Downloader API 执行采集任务"""
import asyncio
import re
import time
from dataclasses import dataclass, field
from typing import Any

import logging

import httpx

from .cookie_pool import CookiePool

_logger = logging.getLogger("doukhub.collector")


@dataclass
class Account:
    """账号数据结构"""
    record_id: str = ""           # 飞书记录 ID
    name: str = ""                # 账号名称
    platform: str = ""            # 抖音 / TikTok / 小红书
    link: str = ""                # 账号链接
    collection_type: str = "发布"  # 发布/喜欢/收藏
    proxy: str = ""               # 代理
    enabled: bool = True          # 是否启用
    rating: int = 3               # 评级 (1-4)
    tags: list[str] = field(default_factory=list)
    note: str = ""                # 备注
    # 自动获取的字段
    sec_user_id: str = ""
    nickname: str = ""
    follower_count: int = 0
    aweme_count: int = 0
    signature: str = ""
    avatar: str = ""
    synced_at: str = ""
    info_fetched: bool = False    # 是否已获取账号基本信息


@dataclass
class CollectResult:
    """采集结果"""
    account_name: str = ""
    platform: str = ""
    status: str = "pending"       # pending / running / success / failed
    works_count: int = 0
    message: str = ""
    started_at: float = 0
    finished_at: float = 0

    @property
    def duration(self) -> float:
        if self.started_at and self.finished_at:
            return self.finished_at - self.started_at
        return 0


TAB_MAP = {
    "发布": "post",
    "喜欢": "favorite",
    "收藏": "collection",
}


class Collector:
    """采集器 — 调用 Downloader HTTP API"""

    def __init__(
        self,
        ttd_url: str = "http://127.0.0.1:5555",
        xhs_url: str = "http://127.0.0.1:5556",
        cookie_mode: str = "random",
        cookie_usage_limit: int = 10,
    ):
        self.ttd_url = ttd_url.rstrip("/")
        self.xhs_url = xhs_url.rstrip("/")
        self.cookie_mode = cookie_mode
        self.cookie_usage_limit = cookie_usage_limit
        self._client = httpx.AsyncClient(timeout=300)

    async def collect_account(self, account: Account, cookie: str = "") -> CollectResult:
        """采集单个账号的所有作品"""
        result = CollectResult(
            account_name=account.name,
            platform=account.platform,
            status="running",
            started_at=time.time(),
        )

        try:
            if account.platform in ("douyin", "tiktok"):
                data = await self._collect_ttd(account, cookie)
            elif account.platform == "xhs":
                data = await self._collect_xhs(account, cookie)
            else:
                result.status = "failed"
                result.message = f"不支持的平台: {account.platform}"
                return result

            result.works_count = len(data) if isinstance(data, list) else 0
            result.status = "success"
            result.message = f"成功采集 {result.works_count} 个作品"

        except Exception as e:
            result.status = "failed"
            result.message = str(e)

        finally:
            result.finished_at = time.time()

        return result

    async def _collect_ttd(self, account: Account, cookie: str) -> Any:
        """调用 TikTokDownloader API 采集抖音/TikTok 账号"""
        if account.platform == "douyin":
            endpoint = f"{self.ttd_url}/douyin/account"
        else:
            endpoint = f"{self.ttd_url}/tiktok/account"

        payload = {
            "sec_user_id": account.sec_user_id,
            "cookie": cookie,
            "tab": TAB_MAP.get(account.collection_type, "post"),
            "source": False,
        }
        if account.proxy:
            payload["proxy"] = account.proxy

        resp = await self._client.post(endpoint, json=payload)
        resp.raise_for_status()
        result = resp.json()
        if result.get("data"):
            return result["data"]
        raise RuntimeError(result.get("message", "获取数据失败"))

    async def _collect_xhs(self, account: Account, cookie: str) -> Any:
        """调用 XHS-Downloader API 采集小红书账号"""
        endpoint = f"{self.xhs_url}/xhs/detail"
        payload = {
            "url": account.link,
            "download": True,
        }
        if cookie:
            payload["cookie"] = cookie
        if account.proxy:
            payload["proxy"] = account.proxy

        resp = await self._client.post(endpoint, json=payload)
        resp.raise_for_status()
        result = resp.json()
        if result.get("data"):
            return [result["data"]]
        raise RuntimeError(result.get("message", "获取数据失败"))

    async def collect_single_detail(
        self,
        link: str,
        platform: str,
        cookie: str = "",
    ) -> CollectResult:
        """采集单个作品"""
        result = CollectResult(
            account_name="单品采集",
            platform=platform,
            status="running",
            started_at=time.time(),
        )
        try:
            if platform in ("douyin", "tiktok"):
                endpoint = f"{self.ttd_url}/douyin/detail"
                # 从链接中提取 detail_id
                match = re.search(r"\b(\d{19})\b", link)
                if not match:
                    result.status = "failed"
                    result.message = "无法从链接中提取作品ID"
                    return result
                payload = {"detail_id": match.group(1), "source": False}
                if cookie:
                    payload["cookie"] = cookie
            elif platform == "xhs":
                endpoint = f"{self.xhs_url}/xhs/detail"
                payload = {"url": link, "download": True}
                if cookie:
                    payload["cookie"] = cookie
            else:
                result.status = "failed"
                result.message = f"不支持的平台: {platform}"
                return result

            resp = await self._client.post(endpoint, json=payload)
            resp.raise_for_status()
            data = resp.json()
            if data.get("data"):
                result.status = "success"
                result.works_count = 1
                result.message = "采集成功"
            else:
                result.status = "failed"
                result.message = data.get("message", "采集失败")

        except Exception as e:
            result.status = "failed"
            result.message = str(e)
        finally:
            result.finished_at = time.time()
        return result

    async def collect_batch(
        self,
        accounts: list[Account],
        cookies: list[str] | None = None,
        concurrency: int = 3,
        progress_callback=None,
    ) -> list[CollectResult]:
        """批量采集多个账号，支持并发控制和 Cookie 轮换"""
        results: list[CollectResult] = []
        sem = asyncio.Semaphore(concurrency)

        # Cookie 轮换
        cookie_pool = CookiePool(cookies or [], self.cookie_mode, self.cookie_usage_limit)

        async def _collect_one(acc: Account, index: int):
            async with sem:
                cookie = cookie_pool.get_cookie() if cookie_pool.has_cookies else ""
                r = await self.collect_account(acc, cookie)
                results.append(r)
                if progress_callback:
                    await progress_callback(index, len(accounts), r)

        tasks = [_collect_one(acc, i) for i, acc in enumerate(accounts)]
        await asyncio.gather(*tasks)
        return results

    async def resolve_short_url(self, url: str, platform: str = "douyin", proxy: str = "") -> str:
        """调用 TTD API 解析短链接"""
        if platform == "douyin":
            endpoint = f"{self.ttd_url}/douyin/share"
        elif platform == "tiktok":
            endpoint = f"{self.ttd_url}/tiktok/share"
        else:
            return ""

        # 补全短链接前缀（TTD API 需要完整 URL）
        if url and not url.startswith("http"):
            if platform == "douyin":
                url = f"https://v.douyin.com/{url}/"
            elif platform == "tiktok":
                url = f"https://vm.tiktok.com/{url}"

        payload = {"text": url}
        if proxy:
            payload["proxy"] = proxy

        try:
            # 使用较短的超时时间（30秒）
            resp = await self._client.post(endpoint, json=payload, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            return data.get("url", "")
        except Exception as e:
            _logger.warning(f"短链接解析失败: {url} - {e}")
            return ""

    async def get_account_info(self, sec_user_id: str, platform: str = "douyin", cookie: str = "") -> dict:
        """通过 sec_user_id 获取账号资料（账号名称、粉丝数、作品数等）"""
        if not sec_user_id:
            return {}

        try:
            if platform == "douyin":
                endpoint = f"{self.ttd_url}/douyin/user/profile"
            elif platform == "tiktok":
                endpoint = f"{self.ttd_url}/tiktok/account"
            else:
                return {}

            payload = {
                "sec_user_id": sec_user_id,
            }
            if cookie:
                payload["cookie"] = cookie

            resp = await self._client.post(endpoint, json=payload, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            if data.get("data"):
                d = data["data"]
                avatar = ""
                avatar_field = d.get("avatar_larger") or d.get("avatar_300x300") or d.get("avatar_thumb")
                if isinstance(avatar_field, dict):
                    url_list = avatar_field.get("url_list", [])
                    if url_list:
                        avatar = url_list[0]
                return {
                    "sec_user_id": sec_user_id,
                    "nickname": d.get("nickname", ""),
                    "signature": d.get("signature", ""),
                    "follower_count": d.get("follower_count", 0),
                    "aweme_count": d.get("aweme_count", 0),
                    "following_count": d.get("following_count", 0),
                    "total_favorited": d.get("total_favorited", 0),
                    "avatar": avatar,
                    "uid": d.get("uid", ""),
                    "unique_id": d.get("unique_id", ""),
                }
            # TTD 返回了但 data 为空
            return {"sec_user_id": sec_user_id, "_error": f"TTD 返回无 data 字段: {str(data)[:200]}"}

        except httpx.ReadTimeout:
            return {"sec_user_id": sec_user_id, "_error": "TTD 接口超时(30s)，可能服务负载高或网络慢"}
        except httpx.ConnectError as e:
            return {"sec_user_id": sec_user_id, "_error": f"TTD 连接失败: {e}"}
        except Exception as e:
            return {"sec_user_id": sec_user_id, "_error": f"TTD 请求异常: {type(e).__name__}: {e}"}

    async def validate_cookie(self, cookie: str, platform: str = "douyin") -> dict:
        """验证 Cookie 是否有效，返回详细状态。

        返回值:
            {"status": "valid", "message": "...", "nickname": "..."}
            {"status": "invalid", "message": "..."}
            {"status": "ttd_error", "message": "..."}
        """
        if not cookie or not cookie.strip():
            return {"status": "invalid", "message": "Cookie 为空"}

        test_sec = "MS4wLjABAAAAzDqoM18FSDjaF9sNew0tqW6SfduLomZWPPhOrBkDm3IzPjbBWhw31ec8O6wfn1ps"

        if platform == "douyin":
            endpoint = f"{self.ttd_url}/douyin/account"
        elif platform == "tiktok":
            endpoint = f"{self.ttd_url}/tiktok/account"
        else:
            return {"status": "invalid", "message": f"不支持的平台: {platform}"}

        try:
            import httpx as _httpx
            payload = {
                "sec_user_id": test_sec,
                "source": True,
                "pages": 1,
                "count": 1,
                "cookie": cookie,
            }
            resp = await self._client.post(endpoint, json=payload, timeout=20)

            content_type = resp.headers.get("content-type", "")
            if "application/json" not in content_type:
                return {"status": "ttd_error", "message": f"TTD 返回非 JSON (HTTP {resp.status_code})，服务可能异常"}

            if resp.status_code != 200:
                return {"status": "ttd_error", "message": f"TTD 返回 HTTP {resp.status_code}"}

            data = resp.json()
            api_code = data.get("code")
            api_msg = data.get("message", "")

            d = data.get("data")
            if not d:
                if api_code and api_code != 0:
                    return {"status": "ttd_error", "message": f"TTD: {api_msg} (code={api_code})"}
                return {"status": "invalid", "message": "Cookie 可能已过期"}

            if isinstance(d, list) and d:
                author = d[0].get("author", {})
                nickname = author.get("nickname", "")
                followers = author.get("follower_count", 0)
                return {
                    "status": "valid",
                    "message": f"有效 ({nickname}, {followers}粉丝)" if nickname else "有效",
                    "nickname": nickname,
                    "follower_count": followers,
                }

            return {"status": "invalid", "message": "数据格式异常"}

        except _httpx.ConnectError:
            return {"status": "ttd_error", "message": "TTD 服务未启动"}
        except _httpx.ReadTimeout:
            return {"status": "ttd_error", "message": "TTD 响应超时"}
        except Exception as e:
            return {"status": "ttd_error", "message": f"异常: {str(e)}"}

    def detect_platform(self, link: str) -> str:
        """根据链接自动识别平台"""
        if "douyin.com" in link or "iesdouyin.com" in link:
            return "douyin"
        elif "tiktok.com" in link:
            return "tiktok"
        elif "xiaohongshu.com" in link or "xhslink.com" in link or "rednote.com" in link:
            return "xhs"
        return ""

    async def close(self):
        await self._client.aclose()
