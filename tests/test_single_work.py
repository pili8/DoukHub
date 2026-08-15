import asyncio

import httpx

from app.core.single_work import (
    build_filename,
    detect_single_platform,
    download_work,
    extract_detail_id,
    fetch_work,
    normalize_work,
    sanitize_filename_part,
)


def test_platform_and_id_detection():
    assert detect_single_platform(
        "https://www.douyin.com/video/1234567890123456789"
    ) == "douyin"
    assert detect_single_platform(
        "https://www.tiktok.com/@user/video/1234567890123456789"
    ) == "tiktok"
    assert (
        extract_detail_id(
            "abc https://www.douyin.com/video/1234567890123456789?x=1"
        )
        == "1234567890123456789"
    )


def test_normalize_work_uses_ttd_extracted_fields():
    work = normalize_work(
        {
            "id": "1234567890123456789",
            "desc": "标题",
            "nickname": "作者",
            "mark": "作者",
            "create_time": "2026-08-15 10:00:00",
            "type": "视频",
            "downloads": ["https://example.com/video"],
            "share_url": "https://www.douyin.com/video/1234567890123456789",
        },
        "douyin",
    )
    assert work["title"] == "标题"
    assert work["author"] == "作者"
    assert work["create_time"] == "2026-08-15 10-00-00"
    assert work["platform"] == "douyin"


def test_filename_cleanup_and_image_suffix():
    assert sanitize_filename_part("a/b:c*d?", 5) == "abcd"
    work = {
        "id": "1234567890123456789",
        "title": "标题",
        "author": "作者",
        "create_time": "2026-08-15 10-00-00",
        "type": "图集",
        "downloads": ["one", "two"],
    }
    assert build_filename(work, "{author} {title}", 1) == "作者 标题_1"


def test_filename_total_length_is_capped():
    work = {
        "id": "1234567890123456789",
        "title": "长" * 200,
        "author": "作者" * 100,
        "create_time": "2026-08-15 10-00-00",
    }
    assert len(build_filename(work, "{title} {author} {title}")) <= 160


def test_fetch_and_download_work(tmp_path):
    async def handler(request):
        if request.url.path == "/douyin/detail":
            return httpx.Response(
                200,
                json={
                    "data": {
                        "id": "1234567890123456789",
                        "desc": "标题",
                        "nickname": "作者",
                        "create_time": "2026-08-15 10:00:00",
                        "type": "图集",
                        "downloads": [
                            "https://cdn.example/a.jpg",
                            "https://cdn.example/b.jpg",
                        ],
                    }
                },
            )
        if request.url.host == "cdn.example":
            return httpx.Response(200, content=b"image")
        return httpx.Response(404)

    async def run():
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(
            transport=transport, follow_redirects=True
        ) as client:
            work = await fetch_work(
                client,
                "http://ttd.local",
                "https://www.douyin.com/video/1234567890123456789",
                "douyin",
            )
            return await download_work(client, work, tmp_path, "{author} {title}")

    paths = asyncio.run(run())
    assert [path.name for path in paths] == ["作者 标题_1.jpg", "作者 标题_2.jpg"]
    assert all(path.read_bytes() == b"image" for path in paths)
    assert not list(tmp_path.glob("*.part"))
