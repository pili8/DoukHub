"""Fetch TTD single-work metadata and download files outside TTD's archive."""
from __future__ import annotations

import re
from pathlib import Path

import httpx


DETAIL_ID = re.compile(r"\b(\d{19})\b")
# 匹配带 http(s) 前缀的完整 URL
_FULL_URL = re.compile(r"https?://[^\s\"'<>]+")
# 匹配不带 http 前缀的抖音/TikTok 短链接域名
# 抖音: v.douyin.com/xxx, t-a.cn/d-xxx, iesdouyin.com/share/xxx
# TikTok: vm.tiktok.com/xxx, vt.tiktok.com/xxx
_SHORT_DOMAINS = (
    r"(?:v\.douyin\.com|t-a\.cn|iesdouyin\.com|vm\.tiktok\.com|vt\.tiktok\.com)"
    r"/[^\s\"'<>]+"
)
_SHORT_URL = re.compile(_SHORT_DOMAINS)
# 合并:优先匹配完整 URL,再匹配短链接
URL = re.compile(r"https?://[^\s\"'<>]+|" + _SHORT_DOMAINS)

INVALID_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
MAX_FILENAME_STEM = 160

PRIMARY_ASSET_KINDS = {"video", "image", "live_photo"}


def detect_single_platform(link: str) -> str:
    """根据链接识别平台，支持完整 URL 和不带 http 前缀的短链接"""
    if "douyin.com" in link or "iesdouyin.com" in link or "t-a.cn" in link:
        return "douyin"
    if "tiktok.com" in link:
        return "tiktok"
    return ""


def extract_detail_id(link: str) -> str:
    match = DETAIL_ID.search(link)
    return match.group(1) if match else ""


def _asset(kind: str, index: int, url: str, cover_url: str = "") -> dict:
    return {"kind": kind, "index": index, "url": str(url or ""), "cover_url": str(cover_url or "")}


def normalize_assets(
    work_type: str,
    downloads: list,
    music_url: str = "",
    static_cover: str = "",
    dynamic_cover: str = "",
) -> list[dict]:
    work_type = str(work_type or "")

    # Determine default primary kind for this work type
    if "实况" in work_type:
        default_kind = "live_photo"
    elif any(word in work_type for word in ("视频", "动图")):
        default_kind = "video"
    else:
        default_kind = "image"

    assets = []
    index = 1
    for item in downloads:
        # Support both structured items (dict) and plain URL strings
        if isinstance(item, dict):
            url = str(item.get("url") or "")
            kind = str(item.get("kind") or default_kind)
            cover = str(item.get("cover_url") or "")
        else:
            url = str(item or "")
            kind = default_kind
            cover = ""
        if url:
            assets.append(_asset(kind, index, url, cover))
            index += 1
    for kind, url in (
        ("music", music_url),
        ("static_cover", static_cover),
        ("dynamic_cover", dynamic_cover),
    ):
        if url:
            assets.append(_asset(kind, index, url, url if kind in ("static_cover", "dynamic_cover") else ""))
            index += 1
    return assets


def normalize_work(raw: dict, platform: str) -> dict:
    work_id = str(raw.get("id") or "")
    raw_downloads = raw.get("downloads") or []
    if isinstance(raw_downloads, str):
        raw_downloads = [raw_downloads]
    # Keep both plain URL strings and structured dict items
    downloads = [d for d in raw_downloads if d and (isinstance(d, str) or isinstance(d, dict))]
    work_type = str(raw.get("type") or "")
    create_time = str(raw.get("create_time") or "").replace(":", "-")
    create_timestamp = raw.get("create_timestamp") or 0
    # 互动数据
    stats = {
        "digg_count": raw.get("digg_count", 0),
        "comment_count": raw.get("comment_count", 0),
        "collect_count": raw.get("collect_count", 0),
        "share_count": raw.get("share_count", 0),
        "play_count": raw.get("play_count", 0),
    }
    # 音乐信息
    music = {
        "author": str(raw.get("music_author") or ""),
        "title": str(raw.get("music_title") or ""),
        "url": str(raw.get("music_url") or ""),
    }
    # 标签
    hashtags = raw.get("text_extra") or []
    video_tags = raw.get("tag") or []
    # 作者信息
    author_info = {
        "nickname": str(raw.get("nickname") or ""),
        "mark": str(raw.get("mark") or ""),
        "uid": str(raw.get("uid") or ""),
        "sec_uid": str(raw.get("sec_uid") or ""),
        "signature": str(raw.get("signature") or ""),
        "user_age": raw.get("user_age", -1),
    }
    # 媒体属性
    media = {
        "duration": str(raw.get("duration") or ""),
        "height": raw.get("height", -1),
        "width": raw.get("width", -1),
        "uri": str(raw.get("uri") or ""),
    }
    return {
        "id": work_id,
        "title": str(raw.get("desc") or work_id),
        "desc": str(raw.get("desc") or ""),
        "author": str(raw.get("mark") or raw.get("nickname") or ""),
        "author_info": author_info,
        "create_time": create_time,
        "create_timestamp": create_timestamp,
        "type": work_type,
        "downloads": downloads,
        "assets": normalize_assets(
            work_type,
            downloads,
            music["url"],
            raw.get("static_cover") or "",
            raw.get("dynamic_cover") or "",
        ),
        "share_url": str(raw.get("share_url") or ""),
        "platform": platform,
        "stats": stats,
        "music": music,
        "hashtags": hashtags,
        "video_tags": video_tags,
        "media": media,
        "static_cover": str(raw.get("static_cover") or ""),
        "dynamic_cover": str(raw.get("dynamic_cover") or ""),
    }


def sanitize_filename_part(value, max_length: int = 80) -> str:
    cleaned = INVALID_FILENAME.sub("", str(value or ""))
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return cleaned[:max_length]


def build_filename(
    work: dict,
    template: str = "{create_time} {author} {title}",
    index: int = 0,
    override: str = "",
) -> str:
    if override:
        stem = sanitize_filename_part(override, MAX_FILENAME_STEM)
    else:
        stem = template.format(
            create_time=sanitize_filename_part(work.get("create_time"), 24),
            author=sanitize_filename_part(work.get("author")),
            title=sanitize_filename_part(work.get("title")),
            id=sanitize_filename_part(work.get("id"), 24),
            type=sanitize_filename_part(work.get("type")),
            platform=sanitize_filename_part(work.get("platform")),
        ).strip()
    if index:
        stem = f"{stem}_{index}"
    stem = stem[:MAX_FILENAME_STEM].rstrip(" .")
    return stem or sanitize_filename_part(work.get("id"), 24)


async def _resolve_share_link(
    client: httpx.AsyncClient, ttd_url: str, link: str, platform: str, cookie: str = ""
) -> str:
    """Resolve short link to full URL.

    Uses direct HTTP redirect (~0.4s) instead of TTD's /share endpoint (~6s).
    Falls back to TTD if direct redirect fails.
    """
    if link and not link.startswith("http"):
        link = f"https://{link}"
    # Fast path: direct redirect, no TTD needed
    try:
        response = await client.get(link)
        if str(response.url) != link:
            return str(response.url)
    except httpx.HTTPError:
        pass
    # Fallback: TTD share endpoint
    try:
        payload = {"text": link}
        if cookie:
            payload["cookie"] = cookie
        response = await client.post(f"{ttd_url}/{platform}/share", json=payload)
        response.raise_for_status()
    except httpx.ConnectError:
        raise RuntimeError("无法连接 TTD 服务，请确认 TikTokDownloader 已启动")
    except httpx.HTTPStatusError as e:
        raise RuntimeError(f"TTD 解析短链接失败 (HTTP {e.response.status_code})")
    return str(response.json().get("url") or "")


async def fetch_work(
    client: httpx.AsyncClient,
    ttd_url: str,
    link: str,
    platform: str,
    cookie: str = "",
    mode: str = "auto",
    on_stage=None,
) -> dict:
    """Fetch work metadata.

    Args:
        mode: "auto" (API first, TTD fallback), "api" (direct API only),
              "ttd" (TTD only).
        on_stage: Optional async callback(stage: str, message: str) for
              progress reporting.
    """
    async def _stage(stage: str, message: str):
        if on_stage:
            await on_stage(stage, message)

    detail_id = extract_detail_id(link)
    if not detail_id:
        await _stage("redirect", "解析短链接...")
        resolved = await _resolve_share_link(client, ttd_url, link, platform, cookie)
        detail_id = extract_detail_id(resolved)
    if not detail_id:
        raise ValueError("无法从链接提取作品 ID")

    use_api = mode in ("auto", "api") and platform == "douyin" and cookie
    use_ttd = mode in ("auto", "ttd")

    # Fast path: direct Douyin API with ABogus signing (~1s for douyin)
    if use_api:
        await _stage("api", "直接 API 解析中...")
        try:
            import time as _time
            _t0 = _time.time()
            from app.core.douyin_api import fetch_detail_direct
            result = await fetch_detail_direct(client, detail_id, cookie)
            _elapsed = _time.time() - _t0
            import logging
            logging.getLogger("doukhub").info(f"Direct API OK in {_elapsed:.2f}s for {detail_id}")
            await _stage("api_done", f"API 解析成功 ({_elapsed:.1f}s)")
            return result
        except RuntimeError as e:
            import logging
            msg = str(e)
            # If the work is genuinely unavailable (deleted/private/etc),
            # don't waste time retrying with TTD — it will fail too
            if any(kw in msg for kw in ("作品不存在", "无法观看", "作品权限", "已被删除", "抱歉")):
                raise
            if mode == "api":
                raise  # api-only mode: no fallback
            logging.getLogger("doukhub").warning(f"Direct Douyin API failed, falling back to TTD: {msg}")
            await _stage("ttd_fallback", f"API 失败，回退 TTD: {msg[:60]}")
        except Exception as e:
            if mode == "api":
                raise
            import logging
            logging.getLogger("doukhub").warning(f"Direct Douyin API failed, falling back to TTD: {e}")
            await _stage("ttd_fallback", f"API 异常，回退 TTD: {str(e)[:60]}")

    if not use_ttd:
        raise RuntimeError("解析模式不允许使用 TTD，且直接 API 未执行或失败")

    # Fallback / TTD-only path
    await _stage("ttd", "TTD 解析中...")
    try:
        payload = {"detail_id": detail_id, "source": False}
        if cookie:
            payload["cookie"] = cookie
        response = await client.post(
            f"{ttd_url}/{platform}/detail",
            json=payload,
        )
        response.raise_for_status()
    except httpx.ConnectError:
        raise RuntimeError("无法连接 TTD 服务，请确认 TikTokDownloader 已启动")
    except httpx.HTTPStatusError as e:
        raise RuntimeError(f"TTD 获取作品详情失败 (HTTP {e.response.status_code})")
    payload = response.json()
    raw = payload.get("data")
    if not raw:
        msg = payload.get("message") or "TTD 未返回作品数据"
        raise RuntimeError(f"TTD 返回错误: {msg}")
    if isinstance(raw, list):
        raw = raw[0]
    await _stage("ttd_done", "TTD 解析成功")
    return normalize_work(raw, platform)


def _extension(response: httpx.Response, asset_kind: str) -> str:
    content_type = (
        response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    )
    mapping = {
        "video/mp4": ".mp4",
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
    }
    if content_type in mapping:
        return mapping[content_type]
    if asset_kind in ("video", "live_photo"):
        return ".mp4"
    if asset_kind == "music":
        return ".mp3"
    return ".jpg"


def _unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    counter = 2
    while True:
        candidate = path.parent / f"{path.stem} ({counter}){path.suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


_DOWNLOAD_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/139.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.douyin.com/",
    "Accept": "*/*",
    "Accept-Encoding": "identity",
}


async def download_work(
    client: httpx.AsyncClient,
    work: dict,
    target_dir: Path,
    template: str = "{create_time} {author} {title}",
    filename_override: str = "",
    asset_indexes=None,
    include_music: bool = False,
    include_static_cover: bool = False,
    include_dynamic_cover: bool = False,
) -> list[Path]:
    assets = work.get("assets") or []
    if not assets and not work.get("downloads"):
        raise ValueError("作品没有可用下载地址")
    if asset_indexes:
        wanted = set(asset_indexes)
        selected = [a for a in assets if a["index"] in wanted]
        if not selected:
            selected = [a for a in assets if a["kind"] in PRIMARY_ASSET_KINDS]
            selected.extend(a for a in assets if (
                (a["kind"] == "music" and include_music)
                or (a["kind"] == "static_cover" and include_static_cover)
                or (a["kind"] == "dynamic_cover" and include_dynamic_cover)
            ))
    else:
        selected = [a for a in assets if a["kind"] in PRIMARY_ASSET_KINDS]
        selected.extend(a for a in assets if (
            (a["kind"] == "music" and include_music)
            or (a["kind"] == "static_cover" and include_static_cover)
            or (a["kind"] == "dynamic_cover" and include_dynamic_cover)
        ))
    if not selected:
        raise ValueError("作品没有可用下载地址")
    target_dir = Path(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    saved: list[Path] = []
    multiple = len(selected) > 1

    # Use a standalone client with proper headers for CDN downloads
    # (the shared single_work_client lacks Referer/UA needed by Douyin CDN)
    async with httpx.AsyncClient(
        timeout=300,
        follow_redirects=True,
        headers=_DOWNLOAD_HEADERS,
    ) as dl_client:
        for offset, asset in enumerate(selected, start=1):
            url = asset["url"]
            async with dl_client.stream("GET", url) as response:
                response.raise_for_status()
                extension = _extension(response, asset["kind"])
                stem = build_filename(
                    work, template, offset if multiple else 0, filename_override
                )
                final_path = _unique_path(target_dir / f"{stem}{extension}")
                temporary = final_path.with_suffix(f"{final_path.suffix}.part")
                try:
                    with temporary.open("wb") as file:
                        async for chunk in response.aiter_bytes():
                            file.write(chunk)
                    temporary.replace(final_path)
                finally:
                    if temporary.exists():
                        temporary.unlink()
                saved.append(final_path)
    return saved
