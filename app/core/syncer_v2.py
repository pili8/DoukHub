"""DoukHub 同步引擎 - 使用本地数据库"""
import json
import re
from datetime import datetime
from typing import Optional
import logging

from .database import Database
from .collector import Collector
from .link_resolver import build_profile_url, extract_sec_user_id
from typing import Optional
from .feishu import FeishuClient
from .feishu_sync import FeishuSyncer

logger = logging.getLogger("doukhub.syncer")


class SyncResult:
    """同步结果"""
    def __init__(self):
        self.total = 0
        self.success = 0
        self.created = 0
        self.updated = 0
        self.revived = 0
        self.failed = 0
        self.skipped = 0
        self.duplicates = 0
        self.errors = []
        self.warnings = []

    def to_dict(self):
        return {
            "total": self.total,
            "success": self.success,
            "created": self.created,
            "updated": self.updated,
            "revived": self.revived,
            "failed": self.failed,
            "skipped": self.skipped,
            "duplicates": self.duplicates,
            "errors": self.errors,
            "warnings": self.warnings,
        }


class Syncer:
    """同步引擎"""

    def __init__(self, feishu: Optional[FeishuClient], collector: Collector, config: dict, tags_mapping: dict = None):
        self.feishu = feishu
        self.collector = collector
        self.config = config
        self.tags_mapping = tags_mapping or {}
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

    @staticmethod
    def parse_count(val) -> int:
        """解析粉丝数/作品数，支持 '1.2万'/'3500' 等格式"""
        if isinstance(val, (int, float)):
            return int(val)
        s = str(val).strip()
        if not s:
            return 0
        try:
            if "万" in s:
                return int(float(s.replace("万", "")) * 10000)
            if "亿" in s:
                return int(float(s.replace("亿", "")) * 100000000)
            return int(float(s))
        except (ValueError, TypeError):
            return 0

    @staticmethod
    def detect_platform(share: str) -> str:
        """根据链接内容自动判断平台"""
        s = share.lower()
        if "tiktok.com" in s:
            return "tiktok"
        if "xiaohongshu.com" in s or "xhslink.com" in s or "rednote.com" in s:
            return "xhs"
        return "douyin"

    def map_tags(self, tags: list) -> list:
        """Apply tag mapping from config, ignoring case (e.g. '个' -> '个人')"""
        mapping = self.tags_mapping or {}
        folded = {key.lower(): value for key, value in mapping.items()}
        result = []
        for tag in tags:
            t = tag.strip()
            if not t:
                continue
            result.append(folded.get(t.lower(), t))
        return result

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

    @staticmethod
    def is_ready_for_account(collection: dict) -> bool:
        """只有「已就绪」状态的记录才参与生成账号表。"""
        return collection.get("解析状态") == "已就绪"

    # ========== 步骤1：导入分享表 ==========

    def import_to_collection(self, text: str) -> SyncResult:
        """导入文本到分享表，支持多种格式"""
        result = SyncResult()

        def skip(reason: str):
            result.skipped += 1
            result.warnings.append(f"{line}: {reason}")

        lines = [line.strip() for line in text.split("\n") if line.strip()]
        seen_shares = set()

        for line in lines:
            result.total += 1
            try:
                share = ""
                level = 1
                tags = []
                import_name = ""
                import_fans = 0
                import_works = 0

                if line.startswith("{"):
                    # JSON 格式: {"地址":"xxx","等级":"个2","用户":"name"}
                    try:
                        data = json.loads(line.split("|")[0].strip())
                    except json.JSONDecodeError:
                        skip("JSON 格式错误")
                        continue
                    share = data.get("地址", "") or data.get("Share", "")
                    if not share:
                        skip("缺少地址")
                        continue
                    import_name = data.get("用户", "") or data.get("名称", "") or ""
                    import_fans = self.parse_count(data.get("粉丝", 0) or data.get("粉丝数", 0))
                    import_works = self.parse_count(data.get("作品", 0) or data.get("作品数", 0))
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
                        skip("缺少地址")
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
                        skip("空行")
                        continue
                    share = parts[0]
                    if len(parts) > 1 and parts[1].isdigit():
                        level = int(parts[1])
                    tags = parts[2:] if len(parts) > 2 else []

                if not share:
                    skip("缺少地址")
                    continue

                # 如果是完整用户主页链接，直接提取 sec_user_id，跳过第二步
                direct_sec_user_id = extract_sec_user_id(share, "")
                if direct_sec_user_id:
                    share = direct_sec_user_id
                else:
                    share = self.normalize_share(share)

                if share in seen_shares:
                    existing = self.db.get_collection_by_share(share)
                    if existing:
                        existing_tags = json.loads(existing.get("标签", "[]")) if existing.get("标签") else []
                        updates = {
                            "等级": self.merge_level(existing.get("等级"), level),
                            "标签": json.dumps(self.merge_tags(existing_tags, self.map_tags(tags)), ensure_ascii=False),
                        }
                        if import_name:
                            updates["账号名称"] = import_name
                        if import_fans:
                            updates["粉丝数"] = import_fans
                        if import_works:
                            updates["作品数"] = import_works
                        if direct_sec_user_id and not existing.get("sec_user_id"):
                            updates.update({"sec_user_id": direct_sec_user_id, "解析状态": "已就绪"})
                        self.db.update_collection(existing["record_id"], updates)
                    result.duplicates += 1
                    continue
                seen_shares.add(share)

                # 检查是否已存在
                existing = self.db.get_collection_by_share(share)
                if not existing and direct_sec_user_id:
                    existing = self.db.get_collection_by_sec_user_id(direct_sec_user_id)
                if existing:
                    # 去重：等级取高的，标签合并
                    new_level = self.merge_level(existing.get("等级"), level)
                    existing_tags = json.loads(existing.get("标签", "[]")) if existing.get("标签") else []
                    new_tags = self.merge_tags(existing_tags, self.map_tags(tags))
                    updates = {
                        "等级": new_level,
                        "标签": json.dumps(new_tags, ensure_ascii=False),
                    }
                    if import_name:
                        updates["账号名称"] = import_name
                    if import_fans:
                        updates["粉丝数"] = import_fans
                    if import_works:
                        updates["作品数"] = import_works
                    # 如果新导入的直接提取了 sec_user_id，补上
                    if direct_sec_user_id and not existing.get("sec_user_id"):
                        updates["sec_user_id"] = direct_sec_user_id
                        updates["解析状态"] = "已就绪"
                    self.db.update_collection(existing["record_id"], updates)
                    result.success += 1
                    result.updated += 1
                else:
                    revived_id = self.db.revive_collection_if_deleted(share)
                    if revived_id:
                        revived_data = {
                            "平台": self.detect_platform(share),
                            "等级": level,
                            "标签": json.dumps(self.map_tags(tags), ensure_ascii=False),
                            "解析状态": "待解析",
                        }
                        if import_name:
                            revived_data["账号名称"] = import_name
                        if import_fans:
                            revived_data["粉丝数"] = import_fans
                        if import_works:
                            revived_data["作品数"] = import_works
                        if direct_sec_user_id:
                            revived_data.update({"sec_user_id": direct_sec_user_id, "解析状态": "已就绪"})
                        self.db.update_collection(revived_id, revived_data)
                        result.success += 1
                        result.revived += 1
                        continue

                    # 新增
                    record_id = f"rec_{datetime.now().strftime('%Y%m%d%H%M%S')}_{result.total}"
                    insert_data = {
                        "record_id": record_id,
                        "share_code": share,
                        "平台": self.detect_platform(share),
                        "等级": level,
                        "标签": json.dumps(self.map_tags(tags), ensure_ascii=False),
                        "解析状态": "待解析",
                    }
                    if import_name:
                        insert_data["账号名称"] = import_name
                    if import_fans:
                        insert_data["粉丝数"] = import_fans
                    if import_works:
                        insert_data["作品数"] = import_works

                    # 如果直接提取了 sec_user_id，标记为已就绪
                    if direct_sec_user_id:
                        insert_data["sec_user_id"] = direct_sec_user_id
                        insert_data["解析状态"] = "已就绪"
                    self.db.insert_collection(insert_data)
                    result.success += 1
                    result.created += 1

            except Exception as e:
                result.failed += 1
                result.errors.append(f"{line}: {str(e)}")

        return result

    # ========== 步骤2：更新分享表（获取 sec_user_id）==========

    async def update_collection(self) -> SyncResult:
        """更新分享表，获取 sec_user_id"""
        result = SyncResult()

        # 获取待解析和解析失败的记录
        collections = self.db.get_all_collections()
        to_process = [c for c in collections if c.get("解析状态") in ("待解析", "解析失败") and str(c.get("share_code", "")).strip()]

        result.total = len(to_process)

        for collection in to_process:
            try:
                share = collection["share_code"]
                platform = collection.get("平台") or "douyin"

                # 调用 TTD API 解析短链接
                resolved_url = await self.collector.resolve_short_url(share, platform)
                sec_user_id = self._extract_sec_user_id(resolved_url, platform)

                if not sec_user_id:
                    result.failed += 1
                    result.errors.append(f"{share}: 无法提取 sec_user_id")
                    self.db.update_collection(collection["record_id"], {
                        "解析状态": "解析失败",
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
                        "标签": json.dumps(merged_tags, ensure_ascii=False),
                    })
                    # 删除重复记录
                    self.db.delete_collection(collection["record_id"])
                    result.success += 1
                else:
                    # 更新 sec_user_id
                    self.db.update_collection(collection["record_id"], {
                        "sec_user_id": sec_user_id,
                        "解析状态": "已就绪",
                    })
                    result.success += 1

            except Exception as e:
                result.failed += 1
                result.errors.append(f"{collection.get('share_code')}: {str(e)}")
                self.db.update_collection(collection["record_id"], {
                    "解析状态": "解析失败",
                })

        return result

    def _extract_sec_user_id(self, url: str, platform: str) -> Optional[str]:
        """从 URL 提取 sec_user_id"""
        match = re.search(r"(?:iesdouyin|douyin)\.com/(?:share/)?user/([A-Za-z0-9_-]+)", url)
        if match:
            return match.group(1)
        return None

    # ========== 步骤3：生成账号表 ==========

    async def sync_to_account(self) -> SyncResult:
        """同步到账号表"""
        result = SyncResult()

        # 获取「已就绪」状态的记录
        collections = self.db.get_all_collections()
        to_process = [c for c in collections if self.is_ready_for_account(c)]

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
                platform = collection.get("平台") or "douyin"

                # 检查账号表是否已存在
                existing_account = self.db.get_account_by_sec_user_id(sec_user_id)

                if existing_account:
                    # 去重：等级取高的，标签合并
                    new_level = self.merge_level(existing_account.get("等级"), collection.get("等级"))
                    existing_tags = json.loads(existing_account.get("标签", "[]")) if existing_account.get("标签") else []
                    new_tags = json.loads(collection.get("标签", "[]")) if collection.get("标签") else []
                    merged_tags = self.merge_tags(existing_tags, new_tags)

                    # 先更新等级/标签（不碰 获取状态）
                    self.db.update_account(existing_account["record_id"], {
                        "等级": new_level,
                        "标签": json.dumps(merged_tags, ensure_ascii=False),
                    })
                    account_id = existing_account["record_id"]
                    need_fetch = existing_account.get("获取状态") != "已获取"
                else:
                    # 创建账号记录（获取状态=待获取）
                    record_id = f"acc_{datetime.now().strftime('%Y%m%d%H%M%S')}_{result.total}"
                    self.db.insert_account({
                        "record_id": record_id,
                        "账号名称": "",
                        "平台": platform,
                        "链接": build_profile_url(sec_user_id, platform),
                        "sec_user_id": sec_user_id,
                        "等级": collection.get("等级"),
                        "标签": collection.get("标签"),
                        "获取状态": "待获取",
                    })
                    account_id = record_id
                    need_fetch = True

                # 标记分享表为已生成
                self.db.update_collection(collection["record_id"], {"解析状态": "已生成"})

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
                            "获取状态": "已获取",
                        })
                    else:
                        self.db.update_account(account_id, {"获取状态": "获取失败"})
                        result.failed += 1
                        result.errors.append(f"{sec_user_id}: 无法获取账号信息")
                        continue

                result.success += 1

            except Exception as e:
                result.failed += 1
                result.errors.append(f"{collection.get('sec_user_id')}: {str(e)}")

        return result

    # ========== 处理账号数据 ==========

    async def sync_all(self, text: Optional[str] = None) -> dict:
        """处理账号数据所有步骤"""
        results = {}

        # 步骤1：导入
        if text:
            results["step1"] = self.import_to_collection(text).to_dict()
        else:
            results["step1"] = {"total": 0, "success": 0, "failed": 0, "skipped": 0, "errors": []}

        # 步骤2：更新分享表
        results["step2"] = (await self.update_collection()).to_dict()

        # 步骤3：生成账号表
        results["step3"] = (await self.sync_to_account()).to_dict()

        return results
