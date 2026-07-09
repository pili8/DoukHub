"""URL 解析工具 — 纯正则解析，不做 HTTP 请求
短链接解析统一走 TikTokDownloader API，避免 DoukHub 直接请求平台
"""
import re

# 正则：从短链文本中提取 URL
URL_PATTERN = re.compile(r"(https?://[^\s\"<>]+)")

# 正则：从解析后的完整 URL 中提取 sec_user_id
DOUYIN_USER_PATTERNS = [
    re.compile(r"douyin\.com/user/([A-Za-z0-9_-]+)"),
    re.compile(r"iesdouyin\.com/share/user/([A-Za-z0-9_-]+)"),
]

# 正则：从 URL 中提取视频 ID
DOUYIN_DETAIL_PATTERNS = [
    re.compile(r"douyin\.com/(?:video|note|slides)/(\d{19})"),
    re.compile(r"iesdouyin\.com/share/(?:video|note|slides)/(\d{19})"),
]

# 正则：小红书用户
XHS_USER_PATTERNS = [
    re.compile(r"xiaohongshu\.com/user/profile/([A-Za-z0-9_-]+)"),
]


def detect_platform(url: str) -> str:
    """根据 URL 判断平台"""
    if "douyin.com" in url or "iesdouyin.com" in url:
        return "抖音"
    elif "tiktok.com" in url:
        return "TikTok"
    elif "xiaohongshu.com" in url or "xhslink.com" in url or "rednote.com" in url:
        return "小红书"
    return ""


def extract_url_from_text(text: str) -> str:
    """从文本中提取 URL"""
    match = URL_PATTERN.search(text)
    return match.group() if match else text


def extract_sec_user_id(resolved_url: str, platform: str = "") -> str:
    """从解析后的完整 URL 中提取 sec_user_id（纯正则，无 HTTP 请求）"""
    if not resolved_url:
        return ""
    if not platform:
        platform = detect_platform(resolved_url)

    if platform == "抖音":
        for pattern in DOUYIN_USER_PATTERNS:
            m = pattern.search(resolved_url)
            if m:
                return m.group(1)
    elif platform == "小红书":
        for pattern in XHS_USER_PATTERNS:
            m = pattern.search(resolved_url)
            if m:
                return m.group(1)

    return ""


def extract_detail_id(resolved_url: str) -> str:
    """从解析后的 URL 中提取视频 ID（纯正则）"""
    if not resolved_url:
        return ""
    for pattern in DOUYIN_DETAIL_PATTERNS:
        m = pattern.search(resolved_url)
        if m:
            return m.group(1)
    return ""
