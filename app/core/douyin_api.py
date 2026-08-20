"""Direct Douyin API client with ABogus signing — bypasses TTD for ~1s parsing."""
from __future__ import annotations

import sys
import time
from pathlib import Path
from urllib.parse import urlencode

import httpx

# TTD root path for ABogus module (imports as src.encrypt.aBogus)
_TTD_ROOT = Path(__file__).resolve().parent.parent.parent / "TikTokDownloader"
if str(_TTD_ROOT) not in sys.path:
    sys.path.insert(0, str(_TTD_ROOT))

_abogus_instance = None


def _get_abogus():
    global _abogus_instance
    if _abogus_instance is None:
        from src.encrypt.aBogus import ABogus
        _abogus_instance = ABogus()
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
    "version_code": "190500",
    "version_name": "19.5.0",
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

# Headers matching TTD's DATA_HEADERS exactly
_HEADERS = {
    "Accept": "*/*",
    "Accept-Encoding": "*/*",
    "Referer": "https://www.douyin.com/?recommend=1",
    "User-Agent": _UA,
}

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
    params_dict = _BASE_PARAMS_DICT | {"aweme_id": detail_id}
    # Use urlencode with safe="=" and quote_via=quote — exactly like TTD
    from urllib.parse import quote
    params_str = urlencode(params_dict, safe="=", quote_via=quote)

    headers = _HEADERS | {"Cookie": cookie}

    # Retry up to 3 times (ABogus has random component)
    last_error = None
    detail = None
    for _attempt in range(3):
        a_bogus = ab.get_value(params_str, {}, "GET", user_agent=_UA)
        url = f"{_API_URL}?{params_str}&a_bogus={a_bogus}"
        try:
            async with httpx.AsyncClient(
                timeout=10,
                follow_redirects=False,
                headers={"User-Agent": _UA},
            ) as req_client:
                response = await req_client.get(url, headers=headers)
            response.raise_for_status()
            data = response.json()
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
        except httpx.HTTPError as e:
            last_error = str(e)
    else:
        raise RuntimeError(last_error or "抖音 API 多次重试后仍未返回作品数据")

    raw = _parse_raw_detail(detail, cookie)
    from app.core.single_work import normalize_work
    work = normalize_work(raw, "douyin")
    _set_cache(detail_id, work)
    return work
