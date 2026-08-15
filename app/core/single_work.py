"""Fetch TTD single-work metadata and download files outside TTD's archive."""
from __future__ import annotations

import re
from pathlib import Path

import httpx


DETAIL_ID = re.compile(r"\b(\d{19})\b")
URL = re.compile(r"https?://[^\s\"'<>]+")
INVALID_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
MAX_FILENAME_STEM = 160


def detect_single_platform(link: str) -> str:
    if "douyin.com" in link or "iesdouyin.com" in link:
        return "douyin"
    if "tiktok.com" in link:
        return "tiktok"
    return ""


def extract_detail_id(link: str) -> str:
    match = DETAIL_ID.search(link)
    return match.group(1) if match else ""


def normalize_work(raw: dict, platform: str) -> dict:
    work_id = str(raw.get("id") or "")
    return {
        "id": work_id,
        "title": str(raw.get("desc") or work_id),
        "author": str(raw.get("mark") or raw.get("nickname") or ""),
        "create_time": str(raw.get("create_time") or "").replace(":", "-"),
        "type": str(raw.get("type") or ""),
        "downloads": [url for url in raw.get("downloads") or [] if url],
        "share_url": str(raw.get("share_url") or ""),
        "platform": platform,
    }


def sanitize_filename_part(value, max_length: int = 80) -> str:
    cleaned = INVALID_FILENAME.sub("", str(value or ""))
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return cleaned[:max_length]


def build_filename(
    work: dict,
    template: str = "{create_time} {author} {title}",
    index: int = 0,
) -> str:
    stem = template.format(
        create_time=sanitize_filename_part(work.get("create_time"), 24),
        author=sanitize_filename_part(work.get("author")),
        title=sanitize_filename_part(work.get("title")),
        id=sanitize_filename_part(work.get("id"), 24),
    ).strip()
    if index:
        stem = f"{stem}_{index}"
    stem = stem[:MAX_FILENAME_STEM].rstrip(" .")
    return stem or sanitize_filename_part(work.get("id"), 24)


async def _resolve_share_link(
    client: httpx.AsyncClient, ttd_url: str, link: str, platform: str
) -> str:
    response = await client.post(f"{ttd_url}/{platform}/share", json={"text": link})
    response.raise_for_status()
    return str(response.json().get("url") or "")


async def fetch_work(
    client: httpx.AsyncClient, ttd_url: str, link: str, platform: str
) -> dict:
    detail_id = extract_detail_id(link)
    if not detail_id:
        resolved = await _resolve_share_link(client, ttd_url, link, platform)
        detail_id = extract_detail_id(resolved)
    if not detail_id:
        raise ValueError("无法从链接提取作品 ID")

    response = await client.post(
        f"{ttd_url}/{platform}/detail",
        json={"detail_id": detail_id, "source": False},
    )
    response.raise_for_status()
    payload = response.json()
    raw = payload.get("data")
    if not raw:
        raise RuntimeError(payload.get("message") or "TTD 未返回作品数据")
    if isinstance(raw, list):
        raw = raw[0]
    return normalize_work(raw, platform)


def _extension(response: httpx.Response, work_type: str) -> str:
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
    return ".mp4" if "视频" in work_type else ".jpg"


def _unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    counter = 2
    while True:
        candidate = path.parent / f"{path.stem} ({counter}){path.suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


async def download_work(
    client: httpx.AsyncClient,
    work: dict,
    target_dir: Path,
    template: str = "{create_time} {author} {title}",
) -> list[Path]:
    if not work.get("downloads"):
        raise ValueError("作品没有可用下载地址")
    target_dir = Path(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    saved: list[Path] = []
    multiple = len(work["downloads"]) > 1

    for index, url in enumerate(work["downloads"], start=1):
        async with client.stream("GET", url) as response:
            response.raise_for_status()
            extension = _extension(response, work.get("type", ""))
            stem = build_filename(work, template, index if multiple else 0)
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
