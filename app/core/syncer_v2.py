"""DoukHub 同步引擎 - 使用本地数据库"""
import json
import re
from datetime import datetime
from typing import Optional
import logging

from .database import Database
from .collector import Collector
from .link_resolver import build_profile_url
from .feishu import FeishuClient
from .feishu_sync import FeishuSyncer

logger = logging.getLogger("doukhub.syncer")


class SyncResult:
    """同步结果"""
    def __init__(self):
        self.total = 0
        self.success = 0
        self.failed = 0
        self.skipped = 0
        self.errors = []

    def to_dict(self):
        return {
            "total": self.total,
            "success": self.success,
            "failed": self.failed,
            "skipped": self.skipped,
            "errors": self.errors,
        }


class Syncer:
    """同步引擎"""

    def __init__(self, feishu: FeishuClient, collector: Collector, config: dict):
        self.feishu = feishu
        self.collector = collector
        self.config = config
        self.db = Database()

        # 飞书表 ID
        self.collection_table_id = config.get("collection_table_id", "")
        self.account_table_id = config.get("account_table_id", "")
        self.cookie_table_id = config.get("cookie_table_id", "")
        self.app_token = config.get("app_token", "")

        # 飞书双向同步器（步骤1自动拉取、步骤3自动回写）
        if feishu and self.app_token:
            self.feishu_syncer = FeishuSyncer(feishu, config)
        else:
            self.feishu_syncer = None

    def normalize_share(self, share: str) -> str:
        """标准化分享码"""
        # 去掉前缀
        share = re.sub(r"https?://v\.douyin\.com/", "", share)
        share = re.sub(r"https?://www\.iesdouyin\.com/share/user/", "", share)
        # 去掉参数
        share = share.split("?")[0]
        # 去掉尾部斜杠
        share = share.rstrip("/")
        return share.strip()

    def merge_tags(self, existing_tags: list, new_tags: list) -> list:
        """合并标签（去重，大小写不敏感，保留原样）"""
        merged = {}
        for tag in existing_tags + new_tags:
            key = tag.strip().lower()
            if key and key not in merged:
                merged[key] = tag.strip()
        return list(merged.values())

    def merge_level(self, existing_level: int, new_level: int) -> int:
        """合并等级（取高的）"""
        return max(existing_level or 0, new_level or 0)

    # ========== 步骤1：导入采集表 ==========

    def import_to_collection(self, text: str) -> SyncResult:
        """导入文本到采集表，支持多种格式"""
        result = SyncResult()

        lines = [line.strip() for line in text.split("\n") if line.strip()]

        for line in lines:
            result.total += 1
            try:
                share = ""
                level = 3
                tags = []

                if line.startswith("{"):
                    # JSON 格式: {"地址":"xxx","等级":"个2","用户":"name"}
                    try:
                        data = json.loads(line.split("|")[0].strip())
                    except json.JSONDecodeError:
                        result.skipped += 1
                        continue
                    share = data.get("地址", "") or data.get("Share", "")
                    if not share:
                        result.skipped += 1
                        continue
                    grade = data.get("等级", "") or data.get("等級", "")
                    parts = re.split(r"[,\uff0c]", grade) if grade else []
                    for part in parts:
                        part = part.strip()
                        num_m = re.search(r"(\d+)$", part)
                        if num_m:
                            level = max(1, min(4, int(num_m.group(1))))
                            tag_p = part[:num_m.start()].strip()
                            if tag_p:
                                tags.append(tag_p)
                        elif part:
                            tags.append(part)
                elif "@" in line:
                    # 简单格式: 标签+等级@分享码，如 "个2@abc123" 或 "个2，图3@abc123"
                    at_idx = line.rfind("@")
                    prefix = line[:at_idx].strip()
                    share = line[at_idx + 1:].strip()
                    if not share:
                        result.skipped += 1
                        continue
                    parts = re.split(r"[,，]", prefix)
                    for part in parts:
                        part = part.strip()
                        if not part:
                            continue
                        num_m = re.search(r"(\d+)$", part)
                        if num_m:
                            level = max(1, min(4, int(num_m.group(1))))
                            tag_p = part[:num_m.start()].strip()
                            if tag_p:
                                tags.append(tag_p)
                        elif part.isdigit():
                            level = max(1, min(4, int(part)))
                        else:
                            tags.append(part)
                else:
                    # 兼容旧格式: 分享码 等级 标签（空格分隔）
                    parts = line.split()
                    if len(parts) < 1:
                        result.skipped += 1
                        continue
                    share = parts[0]
                    if len(parts) > 1 and parts[1].isdigit():
                        level = int(parts[1])
                    tags = parts[2:] if len(parts) > 2 else []

                if not share:
                    result.skipped += 1
                    continue

                share = self.normalize_share(share)

                # 检查是否已存在
                existing = self.db.get_collection_by_share(share)
                if existing:
                    # 去重：等级取高的，标签合并
                    new_level = self.merge_level(existing.get("等级"), level)
                    existing_tags = json.loads(existing.get("标签", "[]")) if existing.get("标签") else []
                    new_tags = self.merge_tags(existing_tags, tags)

                    self.db.update_collection(existing["record_id"], {
                        "等级": new_level,
                        "标签": json.dumps(new_tags),
                    })
                    result.success += 1
                else:
                    # 新增
                    record_id = f"rec_{datetime.now().strftime('%Y%m%d%H%M%S')}_{result.total}"
                    self.db.insert_collection({
                        "record_id": record_id,
                        "share_code": share,
                        "平台": "抖音",  # 默认
                        "等级": level,
                        "标签": json.dumps(tags),
                        "已同步": False,
                    })
                    result.success += 1

            except Exception as e:
                result.failed += 1
                result.errors.append(f"{line}: {str(e)}")

        return result

    # ========== 步骤2：更新采集表（获取 sec_user_id）==========

    async def update_collection(self) -> SyncResult:
        """更新采集表，获取 sec_user_id"""
        result = SyncResult()

        # 获取所有未获取 sec_user_id 的记录
        collections = self.db.get_all_collections()
        to_process = [c for c in collections if not c.get("sec_user_id")]

        result.total = len(to_process)

        for collection in to_process:
            try:
                share = collection["share_code"]
                platform = collection.get("平台") or "抖音"

                # 调用 TTD API 解析短链接
                resolved_url = await self.collector.resolve_short_url(share, platform)
                sec_user_id = self._extract_sec_user_id(resolved_url, platform)

                if not sec_user_id:
                    result.failed += 1
                    result.errors.append(f"{share}: 无法提取 sec_user_id")
                    self.db.update_collection(collection["record_id"], {
                        "同步错误": "无法提取 sec_user_id",
                    })
                    continue

                # 检查是否已存在
                existing = self.db.get_collection_by_sec_user_id(sec_user_id)
                if existing and existing["record_id"] != collection["record_id"]:
                    # 去重：等级取高的，标签合并
                    new_level = self.merge_level(existing.get("等级"), collection.get("等级"))
                    existing_tags = json.loads(existing.get("标签", "[]")) if existing.get("标签") else []
                    new_tags = json.loads(collection.get("标签", "[]")) if collection.get("标签") else []
                    merged_tags = self.merge_tags(existing_tags, new_tags)

                    self.db.update_collection(existing["record_id"], {
                        "等级": new_level,
                        "标签": json.dumps(merged_tags),
                    })
                    # 删除重复记录
                    self.db.delete_collection(collection["record_id"])
                    result.success += 1
                else:
                    # 更新 sec_user_id
                    self.db.update_collection(collection["record_id"], {
                        "sec_user_id": sec_user_id,
                        "已同步": True,
                        "同步错误": None,
                    })
                    result.success += 1

            except Exception as e:
                result.failed += 1
                result.errors.append(f"{collection.get('share_code')}: {str(e)}")
                self.db.update_collection(collection["record_id"], {
                    "同步错误": str(e),
                })

        return result

    def _extract_sec_user_id(self, url: str, platform: str) -> Optional[str]:
        """从 URL 提取 sec_user_id"""
        match = re.search(r"(?:iesdouyin|douyin)\.com/(?:share/)?user/([A-Za-z0-9_-]+)", url)
        if match:
            return match.group(1)
        return None

    # ========== 步骤3：同步账号表 ==========

    async def sync_to_account(self) -> SyncResult:
        """同步到账号表"""
        result = SyncResult()

        # 获取所有已同步但账号表未更新的记录
        collections = self.db.get_all_collections()
        to_process = [c for c in collections if c.get("已同步") and c.get("sec_user_id")]

        result.total = len(to_process)

        # 预检 TTD 服务是否可用（避免逐条等 15s 超时）
        ttd_available = False
        try:
            import httpx
            async with httpx.AsyncClient(timeout=3) as client:
                resp = await client.get(f"{self.collector.ttd_url}/")
                if resp.status_code in (200, 307, 404):
                    ttd_available = True
        except Exception:
            ttd_available = False

        for collection in to_process:
            try:
                sec_user_id = collection["sec_user_id"]
                platform = collection.get("平台") or "\u6296\u97f3"

                # 检查账号表是否已存在
                existing_account = self.db.get_account_by_sec_user_id(sec_user_id)

                if existing_account:
                    # 去重：等级取高的，标签合并
                    new_level = self.merge_level(existing_account.get("等级"), collection.get("等级"))
                    existing_tags = json.loads(existing_account.get("标签", "[]")) if existing_account.get("标签") else []
                    new_tags = json.loads(collection.get("标签", "[]")) if collection.get("标签") else []
                    merged_tags = self.merge_tags(existing_tags, new_tags)

                    # 先更新等级/标签（不碰 已获取信息）
                    self.db.update_account(existing_account["record_id"], {
                        "等级": new_level,
                        "标签": json.dumps(merged_tags),
                    })
                    account_id = existing_account["record_id"]
                    need_fetch = not existing_account.get("已获取信息")
                else:
                    # 创建账号记录（已获取信息=False）
                    record_id = f"acc_{datetime.now().strftime('%Y%m%d%H%M%S')}_{result.total}"
                    self.db.insert_account({
                        "record_id": record_id,
                        "账号名称": "",
                        "平台": platform,
                        "链接": build_profile_url(sec_user_id, platform),
                        "sec_user_id": sec_user_id,
                        "等级": collection.get("等级"),
                        "标签": collection.get("标签"),
                        "已获取信息": False,
                    })
                    account_id = record_id
                    need_fetch = True

                # 未获取信息的，调 API 补全
                if need_fetch:
                    if not ttd_available:
                        result.failed += 1
                        result.errors.append(f"{sec_user_id}: TTD 服务未运行，跳过获取详情")
                        continue
                    cookies = self.db.get_enabled_cookies()
                    cookie = cookies[0].get("Cookie", "") if cookies else ""
                    info = await self.collector.get_account_info(sec_user_id, platform, cookie)
                    if info and info.get("nickname"):
                        self.db.update_account(account_id, {
                            "账号名称": info.get("nickname", ""),
                            "粉丝数": info.get("follower_count", 0),
                            "作品数": info.get("aweme_count", 0),
                            "签名": info.get("signature", ""),
                            "头像": info.get("avatar", ""),
                            "已获取信息": True,
                        })
                    else:
                        result.failed += 1
                        result.errors.append(f"{sec_user_id}: 无法获取账号信息")
                        continue

                result.success += 1

            except Exception as e:
                result.failed += 1
                result.errors.append(f"{collection.get('sec_user_id')}: {str(e)}")

        return result

    # ========== 一键同步 ==========

    async def sync_all(self, text: Optional[str] = None) -> dict:
        """一键同步所有步骤"""
        results = {}

        # 步骤1：导入
        if text:
            results["step1"] = self.import_to_collection(text).to_dict()
        else:
            results["step1"] = {"total": 0, "success": 0, "failed": 0, "skipped": 0, "errors": []}

        # 步骤2：更新采集表
        results["step2"] = (await self.update_collection()).to_dict()

        # 步骤3：同步账号表
        results["step3"] = (await self.sync_to_account()).to_dict()

        return results
