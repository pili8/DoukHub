"""Direct Douyin API client with ABogus signing — bypasses TTD for ~1s parsing."""
from __future__ import annotations

import sys
import time
from pathlib import Path
from urllib.parse import urlencode

import httpx

# curl_cffi 用于模拟浏览器 TLS 指纹，绕过抖音的 403 风控
try:
    from curl_cffi.requests import AsyncSession as _CffiSession
except ImportError:
    _CffiSession = None

# TTD root path for ABogus module (imports as src.encrypt.aBogus)
_TTD_ROOT = Path(__file__).resolve().parent.parent.parent / "TikTokDownloader"
if str(_TTD_ROOT) not in sys.path:
    sys.path.insert(0, str(_TTD_ROOT))

_abogus_instance = None


def _get_abogus():
    global _abogus_instance
    if _abogus_instance is None:
        from src.encrypt.aBogus import ABogus
        # 新版 ABogus 构造函数需要 user_agent 参数
        _abogus_instance = ABogus(_UA)
    return _abogus_instance


# Fixed query params as dict — exactly matching TTD's API class
# (includes uifid and msToken empty strings, which affect ABogus signing)
_BASE_PARAMS_DICT = {
    "device_platform": "webapp",
    "aid": "6383",
    "channel": "channel_pc_web",
    "update_version_code": "170400",
    "pc_client_type": "1",
    "pc_libra_divert": "Windows",
    "support_h265": "1",
    "support_dash": "1",
    "version_code": "290100",
    "version_name": "29.1.0",
    "cookie_enabled": "true",
    "screen_width": "1536",
    "screen_height": "864",
    "browser_language": "zh-CN",
    "browser_platform": "Win32",
    "browser_name": "Chrome",
    "browser_version": "139.0.0.0",
    "browser_online": "true",
    "engine_name": "Blink",
    "engine_version": "139.0.0.0",
    "os_name": "Windows",
    "os_version": "10",
    "cpu_core_num": "16",
    "device_memory": "8",
    "platform": "PC",
    "downlink": "10",
    "effective_type": "4g",
    "round_trip_time": "200",
    "uifid": "",
    "msToken": "",
}

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/139.0.0.0 Safari/537.36"
)

_API_URL = "https://www.douyin.com/aweme/v1/web/aweme/detail/"
_PROFILE_API_URL = "https://www.douyin.com/aweme/v1/web/user/profile/other/"
_POST_API_URL = "https://www.douyin.com/aweme/v1/web/aweme/post/"

# curl_cffi 模拟的浏览器类型，与 TTD 的 IMPERSONATE 一致
_IMPERSONATE = "chrome146"

# Headers matching TTD's DATA_HEADERS exactly
_HEADERS = {
    "Accept": "*/*",
    "Accept-Encoding": "*/*",
    "Referer": "https://www.douyin.com/?recommend=1",
    "User-Agent": _UA,
    "x-tt-argus": "1",
}


def _extract_uifid(cookie: str) -> str:
    """从 Cookie 字符串中提取 UIFID 值（不区分大小写）。

    抖音的 Argus 安全插件要求 URL 参数和 Header 中都携带 uifid，
    否则返回 403 "Blocked by ArgusSecurityPlugin Uifid Not Found"。
    """
    if not cookie:
        return ""
    for part in cookie.split(";"):
        part = part.strip()
        if "=" in part:
            key, _, value = part.partition("=")
            if key.strip().lower() == "uifid" and value.strip():
                return value.strip()
    return ""


async def _cffi_get(url: str, headers: dict, timeout: int = 10) -> dict:
    """用 curl_cffi 发 GET 请求，模拟 Chrome TLS 指纹绕过抖音 403 风控。

    curl_cffi 不可用时回退到 httpx（可能被 403）。
    """
    if _CffiSession:
        async with _CffiSession(
            timeout=timeout,
            allow_redirects=False,
            verify=False,
            impersonate=_IMPERSONATE,
        ) as session:
            response = await session.get(url, headers=headers)
    else:
        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=False,
            headers={"User-Agent": _UA},
        ) as req_client:
            response = await req_client.get(url, headers=headers)
    response.raise_for_status()
    return response.json()

# In-memory LRU cache: detail_id -> (timestamp, work_dict)
_cache: dict[str, tuple[float, dict]] = {}
_CACHE_TTL = 300  # 5 minutes


def _is_cached(detail_id: str) -> dict | None:
    entry = _cache.get(detail_id)
    if entry and (time.time() - entry[0]) < _CACHE_TTL:
        return entry[1]
    if entry:
        _cache.pop(detail_id, None)
    return None


def _set_cache(detail_id: str, work: dict) -> None:
    _cache[detail_id] = (time.time(), work)
    if len(_cache) > 200:
        oldest = min(_cache, key=lambda k: _cache[k][0])
        _cache.pop(oldest, None)


def _pick_url(url_list: list | None) -> str:
    if not url_list:
        return ""
    for url in url_list:
        if url and "douyin.com/aweme/v1/play" not in url:
            return url
    return str(url_list[0] or "")


def _detect_type(detail: dict) -> str:
    images = detail.get("images") or []
    if images:
        video = detail.get("video") or {}
        play_addr = video.get("play_addr") or {}
        if play_addr.get("url_list"):
            return "实况"
        return "图集"
    if detail.get("video"):
        return "视频"
    return "图文"


def _parse_raw_detail(detail: dict, cookie: str = "") -> dict:
    """Convert raw Douyin API aweme_detail to DoukHub normalize_work format."""
    aweme_id = str(detail.get("aweme_id") or "")
    desc = str(detail.get("desc") or "")
    create_ts = detail.get("create_time") or 0

    from datetime import datetime
    create_time = ""
    if create_ts:
        try:
            create_time = datetime.fromtimestamp(create_ts).strftime("%Y-%m-%d %H-%M-%S")
        except (ValueError, OSError):
            pass

    work_type = _detect_type(detail)
    images = detail.get("images") or []
    video = detail.get("video") or {}

    # Covers (computed early — needed for download cover_url)
    static_cover = _pick_url((video.get("cover") or {}).get("url_list"))
    dynamic_cover = _pick_url((video.get("dynamic_cover") or {}).get("url_list"))

    # Build downloads list — structured items with kind + cover_url + media metadata
    downloads: list[dict] = []
    if "图集" in work_type or "实况" in work_type:
        for img in images:
            url = _pick_url(img.get("url_list"))
            if url:
                img_width = int(img.get("width") or 0)
                img_height = int(img.get("height") or 0)
                # For live photos, each image may have a video (motion);
                # otherwise it's just a static image
                img_video = img.get("video") or {}
                if "实况" in work_type and img_video:
                    play_addr = img_video.get("play_addr") or {}
                    motion_url = _pick_url(play_addr.get("url_list"))
                    if motion_url:
                        downloads.append({
                            "url": motion_url, "kind": "live_photo", "cover_url": url,
                            "duration": str(play_addr.get("duration") or ""),
                            "width": img_width, "height": img_height,
                        })
                        continue
                downloads.append({
                    "url": url, "kind": "image", "cover_url": url,
                    "width": img_width, "height": img_height,
                })
        # Fallback: if live photo but no per-image video, use the main video play_addr
        if "实况" in work_type and not any(d["kind"] == "live_photo" for d in downloads):
            play_addr = video.get("play_addr") or {}
            vurl = _pick_url(play_addr.get("url_list"))
            if vurl:
                cover = _pick_url((video.get("cover") or {}).get("url_list")) or static_cover
                downloads.append({
                    "url": vurl, "kind": "live_photo", "cover_url": cover or vurl,
                    "duration": str(play_addr.get("duration") or ""),
                    "width": int(video.get("width") or 0), "height": int(video.get("height") or 0),
                })
    elif detail.get("video"):
        play_addr = video.get("play_addr") or {}
        vurl = _pick_url(play_addr.get("url_list"))
        if vurl:
            downloads.append({
                "url": vurl, "kind": "video", "cover_url": static_cover or vurl,
                "duration": str(play_addr.get("duration") or video.get("duration") or ""),
                "width": int(video.get("width") or 0), "height": int(video.get("height") or 0),
                "size": str(play_addr.get("data_size") or ""),
            })

    # Music
    music_obj = detail.get("music") or {}
    music_url = _pick_url((music_obj.get("play_url") or {}).get("url_list"))

    # Author
    author_obj = detail.get("author") or {}

    # Stats
    stats_obj = detail.get("statistics") or {}

    # Hashtags
    text_extra = detail.get("text_extra") or []
    hashtags = [
        {"hashtag_name": str(te.get("hashtag_name") or ""), "hashtag_id": str(te.get("hashtag_id") or "")}
        for te in text_extra if te.get("hashtag_name")
    ]

    raw = {
        "id": aweme_id,
        "desc": desc,
        "type": work_type,
        "create_time": create_time,
        "create_timestamp": create_ts,
        "downloads": downloads,
        "static_cover": static_cover,
        "dynamic_cover": dynamic_cover,
        "music_url": music_url,
        "music_title": str(music_obj.get("title") or ""),
        "music_author": str(music_obj.get("author") or ""),
        "nickname": str(author_obj.get("nickname") or ""),
        "mark": str(author_obj.get("nickname") or ""),
        "uid": str(author_obj.get("uid") or ""),
        "sec_uid": str(author_obj.get("sec_uid") or ""),
        "signature": str(author_obj.get("signature") or ""),
        "digg_count": stats_obj.get("digg_count", 0),
        "comment_count": stats_obj.get("comment_count", 0),
        "share_count": stats_obj.get("share_count", 0),
        "collect_count": stats_obj.get("collect_count", 0),
        "play_count": stats_obj.get("play_count", 0),
        "text_extra": hashtags,
        "tag": [],
        "duration": str(video.get("duration", "")),
        "height": (video.get("height") or -1),
        "width": (video.get("width") or -1),
        "uri": str(video.get("uri") or ""),
        "share_url": f"https://www.douyin.com/note/{aweme_id}" if "图集" in work_type or "实况" in work_type else f"https://www.douyin.com/video/{aweme_id}",
    }
    return raw


async def fetch_detail_direct(
    client: httpx.AsyncClient,
    detail_id: str,
    cookie: str = "",
) -> dict:
    """Fetch work detail directly from Douyin API with ABogus signing.

    Returns normalized work dict compatible with normalize_work().
    Typically completes in ~1s (sign 5ms + HTTP 700ms).
    """
    # Check cache
    cached = _is_cached(detail_id)
    if cached:
        return cached

    ab = _get_abogus()

    # Build params dict exactly like TTD (including uifid and msToken)
    uifid = _extract_uifid(cookie)
    params_dict = _BASE_PARAMS_DICT | {"aweme_id": detail_id}
    if uifid:
        params_dict["uifid"] = uifid
    # Use urlencode with safe="=" and quote_via=quote — exactly like TTD
    from urllib.parse import quote
    params_str = urlencode(params_dict, safe="=", quote_via=quote)

    headers = _HEADERS | {"Cookie": cookie}
    if uifid:
        headers["uifid"] = uifid

    # Retry up to 3 times (ABogus has random component)
    last_error = None
    detail = None
    for _attempt in range(3):
        a_bogus = ab.get_value(params_str)
        url = f"{_API_URL}?{params_str}&a_bogus={a_bogus}"
        try:
            data = await _cffi_get(url, headers, timeout=10)
            detail = data.get("aweme_detail")
            if detail:
                break
            # Check if work was deleted / made private
            filter_info = data.get("filter_detail") or {}
            if filter_info:
                notice = filter_info.get("notice") or filter_info.get("detail_msg") or "作品不存在"
                # Don't retry — the work is genuinely unavailable
                raise RuntimeError(notice)
            last_error = data.get("status_msg") or "抖音 API 未返回作品数据"
        except Exception as e:
            last_error = str(e)
    else:
        raise RuntimeError(last_error or "抖音 API 多次重试后仍未返回作品数据")

    raw = _parse_raw_detail(detail, cookie)
    from app.core.single_work import normalize_work
    work = normalize_work(raw, "douyin")
    _set_cache(detail_id, work)
    return work


# ── 账号资料直连 API ──

# Profile API 需要额外参数（来自 TTD User.generate_params）
_PROFILE_EXTRA_PARAMS = {
    "publish_video_strategy_type": "2",
    "personal_center_strategy": "1",
    "profile_other_record_enable": "1",
    "land_to": "1",
}

# 缓存：sec_user_id -> (timestamp, info_dict)
_profile_cache: dict[str, tuple[float, dict]] = {}
_PROFILE_CACHE_TTL = 300  # 5 minutes


def _is_profile_cached(sec_user_id: str) -> dict | None:
    entry = _profile_cache.get(sec_user_id)
    if entry and (time.time() - entry[0]) < _PROFILE_CACHE_TTL:
        return entry[1]
    if entry:
        _profile_cache.pop(sec_user_id, None)
    return None


def _set_profile_cache(sec_user_id: str, info: dict) -> None:
    _profile_cache[sec_user_id] = (time.time(), info)
    if len(_profile_cache) > 200:
        oldest = min(_profile_cache, key=lambda k: _profile_cache[k][0])
        _profile_cache.pop(oldest, None)


def _parse_profile(user: dict) -> dict:
    """将抖音 user/profile/other 返回的 user 对象转为 collector.get_account_info 格式。"""
    avatar = ""
    avatar_field = (
        user.get("avatar_larger")
        or user.get("avatar_300x300")
        or user.get("avatar_thumb")
    )
    if isinstance(avatar_field, dict):
        url_list = avatar_field.get("url_list", [])
        if url_list:
            avatar = url_list[0]
    return {
        "nickname": str(user.get("nickname") or ""),
        "signature": str(user.get("signature") or ""),
        "follower_count": int(user.get("follower_count") or 0),
        "aweme_count": int(user.get("aweme_count") or 0),
        "following_count": int(user.get("following_count") or 0),
        "total_favorited": int(user.get("total_favorited") or 0),
        "avatar": avatar,
        "uid": str(user.get("uid") or ""),
        "unique_id": str(user.get("unique_id") or ""),
    }


async def fetch_user_profile_direct(
    client: httpx.AsyncClient,
    sec_user_id: str,
    cookie: str = "",
) -> dict:
    """直连抖音 API 获取账号资料（ABogus 签名），绕过 TTD。

    返回格式与 collector.get_account_info 一致（含 sec_user_id/nickname 等）。
    失败时抛 RuntimeError，调用方应回退 TTD。
    """
    cached = _is_profile_cached(sec_user_id)
    if cached:
        return cached

    ab = _get_abogus()

    uifid = _extract_uifid(cookie)
    params_dict = _BASE_PARAMS_DICT | _PROFILE_EXTRA_PARAMS | {"sec_user_id": sec_user_id}
    if uifid:
        params_dict["uifid"] = uifid
    from urllib.parse import quote
    params_str = urlencode(params_dict, safe="=", quote_via=quote)

    headers = _HEADERS | {"Cookie": cookie}
    if uifid:
        headers["uifid"] = uifid

    last_error = None
    for _attempt in range(3):
        a_bogus = ab.get_value(params_str)
        url = f"{_PROFILE_API_URL}?{params_str}&a_bogus={a_bogus}"
        try:
            data = await _cffi_get(url, headers, timeout=10)
            user = data.get("user")
            if user:
                info = {"sec_user_id": sec_user_id, **_parse_profile(user)}
                _set_profile_cache(sec_user_id, info)
                return info
            # Cookie 失效或封控：返回体里没有 user 字段
            filter_info = data.get("filter_detail") or {}
            if filter_info:
                notice = filter_info.get("notice") or filter_info.get("detail_msg") or "账号不存在"
                raise RuntimeError(notice)
            last_error = data.get("status_msg") or "抖音 API 未返回用户数据"
        except Exception as e:
            last_error = str(e)
    else:
        raise RuntimeError(last_error or "抖音 API 多次重试后仍未返回用户数据")


# Account works (post list) API — same endpoint TTD uses for batch collection
_POST_EXTRA_PARAMS = {
    "max_cursor": "0",
    "locate_query": "false",
    "show_live_replay_strategy": "1",
    "need_time_list": "1",
    "time_list_query": "0",
    "whale_cut_token": "",
    "cut_version": "1",
    "count": "18",
    "publish_video_strategy_type": "2",
}


async def fetch_account_works_direct(
    client: httpx.AsyncClient,
    sec_user_id: str,
    cookie: str = "",
    max_cursor: int = 0,
    count: int = 18,
) -> dict:
    """直连抖音 API 获取账号作品列表（ABogus 签名），绕过 TTD。

    返回原始 API 响应 dict。
    失败时抛 RuntimeError。
    """
    ab = _get_abogus()

    uifid = _extract_uifid(cookie)
    params_dict = _BASE_PARAMS_DICT | _POST_EXTRA_PARAMS | {
        "sec_user_id": sec_user_id,
        "max_cursor": str(max_cursor),
        "count": str(count),
    }
    if uifid:
        params_dict["uifid"] = uifid
    from urllib.parse import quote
    params_str = urlencode(params_dict, safe="=", quote_via=quote)

    headers = _HEADERS | {
        "Cookie": cookie,
        "Referer": f"https://www.douyin.com/user/{sec_user_id}",
    }
    if uifid:
        headers["uifid"] = uifid

    last_error = None
    for _attempt in range(3):
        a_bogus = ab.get_value(params_str)
        url = f"{_POST_API_URL}?{params_str}&a_bogus={a_bogus}"
        try:
            data = await _cffi_get(url, headers, timeout=10)
            return data
        except Exception as e:
            last_error = str(e)
    else:
        raise RuntimeError(last_error or "作品列表 API 多次重试后仍未返回数据")
