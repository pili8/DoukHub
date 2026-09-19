"""采集调度器 — 调用 Downloader API 执行采集任务"""
import asyncio
import re
import time
from dataclasses import dataclass, field
from typing import Any

import logging

import httpx

from .cookie_pool import CookiePool
from .link_resolver import classify_douyin_url, extract_sec_user_id

_logger = logging.getLogger("doukhub.collector")


def _pick_avatar(d: dict) -> str:
    """从抖音 API 响应中提取头像 URL。"""
    avatar_field = d.get("avatar_larger") or d.get("avatar_300x300") or d.get("avatar_thumb")
    if isinstance(avatar_field, dict):
        url_list = avatar_field.get("url_list", [])
        if url_list:
            return url_list[0]
    return ""

# 短码长度下限：抖音/TikTok 短码是 base62（如 iMJV5Ajw），低于此值必然是截断垃圾
MIN_SHARE_CODE_LEN = 6
SHARE_CODE_PATTERN = re.compile(r"^[A-Za-z0-9_.\-]+$")
# 判「链接失效」前再确认一次的间隔。
# 曾把「同一短码时而成功时而失败」解释为 TTD 抖动，实测真因是 httpx 继承了系统代理：
# 同一条短码「走代理 404 / 不走代理 200」（见 __init__ 的 trust_env=False）。
# 复认挡不住代理类故障（同一环境两次都会被拦），这里只作为对偶发抖动的廉价保险。
BAD_LINK_RECHECK_DELAY_SECONDS = 1.5


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


@dataclass
class ResolveOutcome:
    """短链解析结果：把笼统的「失败」拆成可判定的类型。

    上层据此决定**跳过**还是**退避重试**（见 docs/账号解析健壮性修复方案.md）：
        ok          拿到用户主页链接，可提取 sec_user_id
        bad_link    链接永久失效（分享码残缺 / TTD 静默跳首页）→ 判终态，跳过，不退避
        not_profile 链接有效但指向作品、合集或直播 → 判终态，跳过，不退避
        unsupported 平台暂不支持（如小红书）→ 判终态，跳过，不退避
        ttd_down    TTD 连不上、超时 → 服务层故障，退避后重试
        ttd_http    TTD 返回 4xx/5xx、空 url → 服务层故障，退避后重试
        bad_json    TTD 返回非 JSON → 服务层故障，退避后重试
    """
    url: str = ""
    kind: str = "ok"
    detail: str = ""

    @property
    def is_ok(self) -> bool:
        return self.kind == "ok"

    @property
    def is_terminal(self) -> bool:
        """True = 这条记录可以判死了，不该再被重试。"""
        return self.kind in ("bad_link", "not_profile", "unsupported")

    @property
    def is_service_failure(self) -> bool:
        """True = 是 TTD 这边的毛病，值得退避后重试。"""
        return self.kind in ("ttd_down", "ttd_http", "bad_json")


def is_cookie_failure(info: dict) -> bool:
    """资料获取失败是否指向 Cookie 失效（决定要不要换 Cookie）。

    TTD 拿不到 data 时基本只有两种可能：Cookie 过期，或封控；
    两种都该换个 Cookie 再试，所以 no_data 一并归为 Cookie 类故障。
    """
    if not info:
        return False
    return info.get("_kind") in ("cookie", "no_data")


def _looks_like_cookie_error(data: Any) -> bool:
    """TTD 返回体里是否直接点明 Cookie 有问题。"""
    blob = str(data).lower()
    return "cookie" in blob or "登录" in blob or "验证" in blob


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
        # trust_env=False：本机回环请求绝不能被系统代理接管。
        # 实测（TTD access log）：一旦走代理，请求行会从 `POST /douyin/share`
        # 变成 `POST http%3A//127.0.0.1%3A5555/douyin/share`，TTD 路由匹配不上直接 404。
        # 上层会把 404 当成"服务故障"去退避重试，看起来就像链接失效、且"有时行有时不行"。
        self._client = httpx.AsyncClient(timeout=300, trust_env=False)

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
            # source=True：返回原始 API 数据，不经 TTD 的文件处理流程
            # （新版 TTD 的 source=False 需要 Volume 目录等配置，会 500）
            "source": True,
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
                payload = {"detail_id": match.group(1), "source": True}
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

    @staticmethod
    def is_valid_share_code(code: str) -> bool:
        """本地格式预校验：明显残缺的短码直接判死链，连一次请求都不用发。

        库里那些 `If` / `3` / `U` / `xo` 属于历史脏数据，永远解析不出来；
        完整 URL 则放行，交给 TTD 判定。
        """
        code = (code or "").strip()
        if not code:
            return False
        if code.startswith("http"):
            return True
        return len(code) >= MIN_SHARE_CODE_LEN and bool(SHARE_CODE_PATTERN.match(code))

    async def resolve_short_url(self, url: str, platform: str = "douyin", proxy: str = "") -> str:
        """调用 TTD API 解析短链接（兼容旧调用方，只返回 URL 字符串）。"""
        outcome = await self.resolve_short_url_ex(url, platform, proxy)
        return outcome.url

    async def resolve_short_url_ex(self, url: str, platform: str = "douyin", proxy: str = "") -> ResolveOutcome:
        """解析短链接，并给出可判定的失败类型。

        快路径：直连 HTTP 重定向（~0.4s），不经 TTD。
        回退路径：TTD /share 端点（~6s，含 TTD 内置 wait）。

        TTD 对失效链接**没有业务错误码**，表现为「200 但 url 跳到平台首页」；
        所以死链只能靠解析出来的 URL 反推，这也是本方法存在的理由。
        """
        if platform not in ("douyin", "tiktok"):
            return ResolveOutcome(kind="unsupported", detail=f"暂不支持的平台: {platform or '未知'}")

        code = (url or "").strip()
        if not code:
            return ResolveOutcome(kind="bad_link", detail="分享码为空")
        if not self.is_valid_share_code(code):
            return ResolveOutcome(kind="bad_link", detail=f"分享码残缺({len(code)} 字符): {code}")

        is_short_code = not code.startswith("http")
        # 直接粘贴的完整作品/直播链接：本地正则就能判，不必发给 TTD
        if not is_short_code and platform == "douyin" and classify_douyin_url(code) == "content":
            return ResolveOutcome(kind="not_profile", detail=f"本身就是作品或直播链接: {code[:80]}")
        # 补全短链接前缀
        if is_short_code:
            code = f"https://v.douyin.com/{code}/" if platform == "douyin" else f"https://vm.tiktok.com/{code}"

        # ── 快路径：直连 HTTP 重定向（~0.4s），不经 TTD ──
        direct_resolved = await self._direct_redirect(code, proxy)
        if direct_resolved:
            outcome = self._evaluate_resolved(direct_resolved, platform, code)
            if outcome.kind != "bad_link":
                return outcome
            # 直连也跳首页 → 短码失效，不必再请求 TTD
            if outcome.kind == "not_profile":
                return outcome

        # ── 回退路径：TTD /share 端点 ──
        endpoint = f"{self.ttd_url}/douyin/share" if platform == "douyin" else f"{self.ttd_url}/tiktok/share"
        payload: dict[str, Any] = {"text": code}
        if proxy:
            payload["proxy"] = proxy

        # 死链复认：短码在判死前多确认一次，避免把有效链接误杀成不可逆的终态。
        # 局限：代理类故障下两次请求都会被拦，复认无效——那类问题的根治在 trust_env=False。
        for attempt in range(2 if is_short_code else 1):
            if attempt:
                await asyncio.sleep(BAD_LINK_RECHECK_DELAY_SECONDS)
            outcome = await self._request_share(endpoint, payload, platform)
            if outcome.kind != "bad_link":
                return outcome
        return outcome

    async def _direct_redirect(self, url: str, proxy: str = "") -> str:
        """直连 HTTP 重定向解析短链接（~0.4s），不经 TTD。

        与 single_work._resolve_share_link 同理：抖音短链接会 302 到完整 URL。
        返回空字符串表示直连失败，调用方应回退 TTD。
        """
        try:
            redirect_client = httpx.AsyncClient(
                timeout=10,
                follow_redirects=True,
                trust_env=False,
            )
            if proxy:
                redirect_client = httpx.AsyncClient(
                    timeout=10,
                    follow_redirects=True,
                    trust_env=False,
                    proxy=proxy,
                )
            async with redirect_client as rc:
                resp = await rc.get(url)
            resolved = str(resp.url)
            # 如果跳到了平台首页，说明短码失效
            if resolved and resolved != url:
                return resolved
        except httpx.HTTPError:
            pass
        except Exception:
            pass
        return ""

    def _evaluate_resolved(self, resolved: str, platform: str, original_code: str) -> ResolveOutcome:
        """评估重定向后的 URL，判定链接类型（与 _request_share 中的逻辑一致）。"""
        if not extract_sec_user_id(resolved, platform):
            if platform == "douyin" and classify_douyin_url(resolved) == "content":
                return ResolveOutcome(kind="not_profile", detail=f"指向作品或直播: {resolved[:80]}")
            return ResolveOutcome(kind="bad_link", detail=f"未跳到用户页: {resolved[:80]}")
        _logger.info(f"直连解析成功: {original_code[:60]} → {resolved[:80]}")
        return ResolveOutcome(url=resolved, kind="ok")

    async def _request_share(self, endpoint: str, payload: dict, platform: str) -> ResolveOutcome:
        """发一次短链解析请求，并按 TTD 的返回内容判定类型。"""
        try:
            resp = await self._client.post(endpoint, json=payload, timeout=30)
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
            return ResolveOutcome(kind="ttd_down", detail=f"TTD 连接失败: {e}")
        except httpx.TimeoutException as e:
            return ResolveOutcome(kind="ttd_down", detail=f"TTD 超时: {e}")
        except Exception as e:
            return ResolveOutcome(kind="ttd_http", detail=f"{type(e).__name__}: {e}")

        if resp.status_code >= 400:
            return ResolveOutcome(kind="ttd_http", detail=f"TTD 返回 HTTP {resp.status_code}")
        try:
            data = resp.json()
        except Exception:
            return ResolveOutcome(kind="bad_json", detail=f"响应非 JSON: {resp.text[:120]}")

        resolved = (data.get("url") or "").strip()
        if not resolved:
            # 空 url 更像 TTD 半死不活，而不是链接死了 → 归服务层，退避后还会再试
            return ResolveOutcome(kind="ttd_http", detail="TTD 返回空 url")
        platform_hint = payload.get("platform_hint", "") or self._platform_of(endpoint)
        if not extract_sec_user_id(resolved, platform_hint):
            if platform_hint == "douyin" and classify_douyin_url(resolved) == "content":
                # 链接有效，只是指向作品/直播 → 提不出 sec_user_id，判终态
                return ResolveOutcome(kind="not_profile", detail=f"指向作品或直播: {resolved[:80]}")
            # 死链的真身：TTD 静默跳到平台首页，URL 里没有用户主页特征
            return ResolveOutcome(kind="bad_link", detail=f"未跳到用户页: {resolved[:80]}")

        _logger.info(f"短链解析成功: {payload.get('text', '')[:60]} → {resolved[:80]}")
        return ResolveOutcome(url=resolved, kind="ok")

    @staticmethod
    def _platform_of(endpoint: str) -> str:
        """从接口路径反推平台（douyin / tiktok）。"""
        return "tiktok" if "/tiktok/" in endpoint else "douyin"

    async def get_account_info(self, sec_user_id: str, platform: str = "douyin", cookie: str = "") -> dict:
        """通过 sec_user_id 获取账号资料（账号名称、粉丝数、作品数等）。

        快路径：直连抖音 API（ABogus 签名，~1s），不经 TTD。
        回退路径：TTD /douyin/account（~6s，含 TTD 内置 wait）。
        """
        if not sec_user_id:
            return {}

        # ── 快路径：直连抖音 API（仅抖音 + 有 Cookie） ──
        if platform == "douyin" and cookie:
            try:
                from app.core.douyin_api import fetch_user_profile_direct
                info = await fetch_user_profile_direct(self._client, sec_user_id, cookie)
                if info and info.get("nickname"):
                    return info
                # 直连返回但没拿到 nickname → 可能是 Cookie 过期
                return {
                    "sec_user_id": sec_user_id,
                    "_kind": "cookie",
                    "_error": "直连 API 未返回用户数据，Cookie 可能已过期",
                }
            except RuntimeError as e:
                msg = str(e)
                # 账号本身不存在/已注销 → 不必再请求 TTD
                if any(kw in msg for kw in ("账号不存在", "用户不存在", "已被注销", "已被封禁")):
                    return {"sec_user_id": sec_user_id, "_kind": "no_data", "_error": msg}
                _logger.warning(f"直连 API 获取账号资料失败，回退 TTD: {msg}")
            except Exception as e:
                _logger.warning(f"直连 API 异常，回退 TTD: {type(e).__name__}: {e}")

        # ── 回退路径：TTD API ──
        try:
            if platform == "douyin":
                endpoint = f"{self.ttd_url}/douyin/account"
            elif platform == "tiktok":
                endpoint = f"{self.ttd_url}/tiktok/account"
            else:
                return {}

            payload = {
                "sec_user_id": sec_user_id,
                "source": True,
                "count": 1,
            }
            if cookie:
                payload["cookie"] = cookie

            resp = await self._client.post(endpoint, json=payload, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            if data.get("data"):
                d = data["data"]
                # TTD /douyin/account 返回的是作品列表，第一个作品的 author 里有账号资料
                if isinstance(d, list) and d:
                    author = d[0].get("author", {})
                    if author:
                        return {
                            "sec_user_id": sec_user_id,
                            "nickname": author.get("nickname", ""),
                            "signature": author.get("signature", ""),
                            "follower_count": author.get("follower_count", 0),
                            "aweme_count": author.get("aweme_count", 0),
                            "following_count": author.get("following_count", 0),
                            "total_favorited": author.get("total_favorited", 0),
                            "avatar": _pick_avatar(author),
                            "uid": author.get("uid", ""),
                            "unique_id": author.get("unique_id", ""),
                        }
                # 非列表格式（旧接口可能返回直接对象）
                if isinstance(d, dict):
                    return {
                        "sec_user_id": sec_user_id,
                        "nickname": d.get("nickname", ""),
                        "signature": d.get("signature", ""),
                        "follower_count": d.get("follower_count", 0),
                        "aweme_count": d.get("aweme_count", 0),
                        "following_count": d.get("following_count", 0),
                        "total_favorited": d.get("total_favorited", 0),
                        "avatar": _pick_avatar(d),
                        "uid": d.get("uid", ""),
                        "unique_id": d.get("unique_id", ""),
                    }
            # TTD 返回了但 data 为空：绝大多数是 Cookie 失效/封控
            return {
                "sec_user_id": sec_user_id,
                "_kind": "cookie" if _looks_like_cookie_error(data) else "no_data",
                "_error": f"TTD 返回无 data 字段: {str(data)[:200]}",
            }

        except httpx.ReadTimeout:
            return {"sec_user_id": sec_user_id, "_kind": "ttd_down", "_error": "TTD 接口超时(30s)，可能服务负载高或网络慢"}
        except httpx.ConnectError as e:
            return {"sec_user_id": sec_user_id, "_kind": "ttd_down", "_error": f"TTD 连接失败: {e}"}
        except httpx.HTTPStatusError as e:
            return {"sec_user_id": sec_user_id, "_kind": "ttd_http", "_error": f"TTD 返回 HTTP {e.response.status_code}"}
        except Exception as e:
            return {"sec_user_id": sec_user_id, "_kind": "ttd_http", "_error": f"TTD 请求异常: {type(e).__name__}: {e}"}

    async def validate_cookie(self, cookie: str, platform: str = "douyin") -> dict:
        """验证 Cookie 是否有效，返回详细状态。

        优先使用 Dok 直连 API（ABogus 签名）验证，速度快且不依赖 TTD Server。
        直连失败时回退 TTD Server。

        返回值:
            {"status": "valid", "message": "...", "nickname": "..."}
            {"status": "invalid", "message": "..."}
            {"status": "ttd_error", "message": "..."}
        """
        if not cookie or not cookie.strip():
            return {"status": "invalid", "message": "Cookie 为空"}

        if platform != "douyin":
            # TikTok Cookie 暂不支持直连验证，回退 TTD
            return await self._validate_cookie_ttd(cookie, platform)

        # ── 优先：Dok 直连 API 验证 ──
        test_sec = "MS4wLjABAAAAzDqoM18FSDjaF9sNew0tqW6SfduLomZWPPhOrBkDm3IzPjbBWhw31ec8O6wfn1ps"
        try:
            from app.core.douyin_api import fetch_user_profile_direct
            result = await fetch_user_profile_direct(self._client, test_sec, cookie)
            nickname = result.get("nickname", "")
            return {
                "status": "valid",
                "message": f"有效 ({nickname})" if nickname else "有效",
                "nickname": nickname,
            }
        except RuntimeError as e:
            # 直连 API 明确返回"未登录/过期"→ Cookie 无效
            msg = str(e)
            if any(kw in msg for kw in ("登录", "过期", "未登录", "不存在", "封")):
                return {"status": "invalid", "message": msg[:80]}
            # 其他错误 → 回退 TTD
            logger.debug(f"直连验证失败，回退 TTD: {msg}")
        except Exception as e:
            logger.debug(f"直连验证异常，回退 TTD: {e}")

        # ── 回退：TTD Server 验证 ──
        return await self._validate_cookie_ttd(cookie, platform)

    async def _validate_cookie_ttd(self, cookie: str, platform: str = "douyin") -> dict:
        """通过 TTD Server 验证 Cookie（回退方案）。"""
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
