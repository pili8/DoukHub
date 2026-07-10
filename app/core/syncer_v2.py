"""DoukHub 同步引擎 - 使用本地数据库"""
import json
import re
from datetime import datetime
from typing import Optional
import logging

from .database import Database
from .collector import Collector
from .feishu import FeishuClient

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
        """合并标签（去重，大小写不敏感）"""
        tag_set = set()
        for tag in existing_tags + new_tags:
            tag_set.add(tag.lower())
        return list(tag_set)

    def merge_level(self, existing_level: int, new_level: int) -> int:
        """合并等级（取高的）"""
        return max(existing_level or 0, new_level or 0)

    # ========== 步骤1：导入采集表 ==========

    def import_to_collection(self, text: str) -> SyncResult:
        """导入文本到采集表"""
        result = SyncResult()

        # 解析文本（这里简化为按行分割，实际应该用正则提取）
        lines = [line.strip() for line in text.split("\n") if line.strip()]

        for line in lines:
            result.total += 1
            try:
                # 这里应该用正则提取 share、等级、标签等
                # 简化处理：假设格式为 "share 等级 标签"
                parts = line.split()
                if len(parts) < 2:
                    result.skipped += 1
                    continue

                share = self.normalize_share(parts[0])
                level = int(parts[1]) if parts[1].isdigit() else 3
                tags = parts[2:] if len(parts) > 2 else []

                # 检查是否已存在
                existing = self.db.get_collection_by_share(share)
                if existing:
                    # 去重：等级取高的，标签合并
                    new_level = self.merge_level(existing.get("等级"), level)
                    existing_tags = json.loads(existing.get("标签", "[]")) if existing.get("标签") else []
                    new_tags = self.merge_tags(existing_tags, tags)

                    self.db.update_collection(existing["记录ID"], {
                        "等级": new_level,
                        "标签": json.dumps(new_tags),
                    })
                    result.success += 1
                else:
                    # 新增
                    record_id = f"rec_{datetime.now().strftime('%Y%m%d%H%M%S')}_{result.total}"
                    self.db.insert_collection({
                        "记录ID": record_id,
                        "分享码": share,
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
        to_process = [c for c in collections if not c.get("账号标识")]

        result.total = len(to_process)

        for collection in to_process:
            try:
                share = collection["分享码"]
                platform = collection.get("平台", "抖音")

                # 调用 TTD API 解析短链接
                resolved_url = await self.collector.resolve_short_url(share, platform)
                sec_user_id = self._extract_sec_user_id(resolved_url, platform)

                if not sec_user_id:
                    result.failed += 1
                    result.errors.append(f"{share}: 无法提取 sec_user_id")
                    self.db.update_collection(collection["记录ID"], {
                        "同步错误": "无法提取 sec_user_id",
                    })
                    continue

                # 检查是否已存在
                existing = self.db.get_collection_by_sec_user_id(sec_user_id)
                if existing and existing["记录ID"] != collection["记录ID"]:
                    # 去重：等级取高的，标签合并
                    new_level = self.merge_level(existing.get("等级"), collection.get("等级"))
                    existing_tags = json.loads(existing.get("标签", "[]")) if existing.get("标签") else []
                    new_tags = json.loads(collection.get("标签", "[]")) if collection.get("标签") else []
                    merged_tags = self.merge_tags(existing_tags, new_tags)

                    self.db.update_collection(existing["记录ID"], {
                        "等级": new_level,
                        "标签": json.dumps(merged_tags),
                    })
                    # 删除重复记录
                    self.db.delete_collection(collection["记录ID"])
                    result.success += 1
                else:
                    # 更新 sec_user_id
                    self.db.update_collection(collection["记录ID"], {
                        "账号标识": sec_user_id,
                        "已同步": True,
                        "同步错误": None,
                    })
                    result.success += 1

            except Exception as e:
                result.failed += 1
                result.errors.append(f"{collection.get('分享码')}: {str(e)}")
                self.db.update_collection(collection["记录ID"], {
                    "同步错误": str(e),
                })

        return result

    def _extract_sec_user_id(self, url: str, platform: str) -> Optional[str]:
        """从 URL 提取 sec_user_id"""
        if "douyin.com/user/" in url:
            match = re.search(r"douyin\.com/user/([A-Za-z0-9_-]+)", url)
            if match:
                return match.group(1)
        return None

    # ========== 步骤3：同步账号表 ==========

    async def sync_to_account(self) -> SyncResult:
        """同步到账号表"""
        result = SyncResult()

        # 获取所有已同步但账号表未更新的记录
        collections = self.db.get_all_collections()
        to_process = [c for c in collections if c.get("已同步") and c.get("账号标识")]

        result.total = len(to_process)

        for collection in to_process:
            try:
                sec_user_id = collection["账号标识"]
                platform = collection.get("平台", "抖音")

                # 检查账号表是否已存在
                existing_account = self.db.get_account_by_sec_user_id(sec_user_id)

                if existing_account:
                    # 去重：等级取高的，标签合并
                    new_level = self.merge_level(existing_account.get("等级"), collection.get("等级"))
                    existing_tags = json.loads(existing_account.get("标签", "[]")) if existing_account.get("标签") else []
                    new_tags = json.loads(collection.get("标签", "[]")) if collection.get("标签") else []
                    merged_tags = self.merge_tags(existing_tags, new_tags)

                    # 更新账号表
                    self.db.update_account(existing_account["记录ID"], {
                        "等级": new_level,
                        "标签": json.dumps(merged_tags),
                        "已更新": True,
                    })
                else:
                    # 调用 API 获取账号信息
                    info = await self.collector.get_account_info(sec_user_id, platform)
                    if not info:
                        result.failed += 1
                        result.errors.append(f"{sec_user_id}: 无法获取账号信息")
                        continue

                    # 创建账号记录
                    record_id = f"acc_{datetime.now().strftime('%Y%m%d%H%M%S')}_{result.total}"
                    self.db.insert_account({
                        "记录ID": record_id,
                        "账号名称": info.get("nickname", ""),
                        "平台": platform,
                        "链接": f"https://www.douyin.com/user/{sec_user_id}",
                        "账号标识": sec_user_id,
                        "等级": collection.get("等级"),
                        "标签": collection.get("标签"),
                        "昵称": info.get("nickname", ""),
                        "粉丝数": info.get("follower_count", 0),
                        "作品数": info.get("aweme_count", 0),
                        "签名": info.get("signature", ""),
                        "头像": info.get("avatar", ""),
                        "已更新": True,
                    })

                result.success += 1

            except Exception as e:
                result.failed += 1
                result.errors.append(f"{collection.get('账号标识')}: {str(e)}")

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
