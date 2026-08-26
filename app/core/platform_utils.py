"""平台标识统一：douyin / tiktok / xhs（全小写），兼容中文与大小写输入。

业务表（cookie_cache / account_cache / share_cache）的「平台」字段历史上有
中文值（抖音 / TikTok / 小红书），现统一为英文小写规范值，同时保留对中文
与任意大小写写法的识别能力。
"""

# 规范值 -> 别名集合（含中文原名；匹配时统一 lower 后比较，中文不受 lower 影响）
PLATFORM_ALIASES = {
    "douyin": {"douyin", "抖音", "dy", "抖"},
    "tiktok": {"tiktok", "tik tok", "tk"},
    "xhs": {"xhs", "小红书", "xiaohongshu", "rednote"},
}

# 前端下拉等场景使用的规范值列表
PLATFORM_VALUES = ("douyin", "tiktok", "xhs")


def normalize_platform(value):
    """把任意平台写法归一为 douyin / tiktok / xhs（小写英文）。

    - 中文：抖音 -> douyin，小红书 -> xhs
    - 大小写不敏感：DOUYIN / Douyin -> douyin，XHS / Xhs -> xhs
    - 未识别值：英文转小写原样返回，中文原样返回（保留未知平台写法）
    """
    if value is None:
        return ""
    s = str(value).strip()
    if not s:
        return ""
    low = s.lower()
    for canon, aliases in PLATFORM_ALIASES.items():
        if low in {a.lower() for a in aliases}:
            return canon
    return low if s.isascii() else s
