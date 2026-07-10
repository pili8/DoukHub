"""飞书双向同步 - 本地数据库 ↔ 飞书表"""
import json
import logging
from typing import Optional
from datetime import datetime

from .database import Database
from .feishu import FeishuClient

logger = logging.getLogger("doukhub.feishu_sync")


class FeishuSyncResult:
    """飞书同步结果"""
    def __init__(self):
        self.collection_to_feishu = {"created": 0, "updated": 0, "failed": 0}
        self.collection_from_feishu = {"created": 0, "updated": 0, "failed": 0}
        self.account_to_feishu = {"created": 0, "updated": 0, "failed": 0}
        self.account_from_feishu = {"created": 0, "updated": 0, "failed": 0}
        self.errors = []

    def to_dict(self):
        return {
            "collection_to_feishu": self.collection_to_feishu,
            "collection_from_feishu": self.collection_from_feishu,
            "account_to_feishu": self.account_to_feishu,
            "account_from_feishu": self.account_from_feishu,
            "errors": self.errors[:10],
        }


class FeishuSyncer:
    """飞书双向同步器"""

    def __init__(self, feishu: FeishuClient, config: dict):
        self.feishu = feishu
        self.config = config
        self.db = Database()

        # 飞书表 ID
        self.collection_table_id = config.get("collection_table_id", "")
        self.account_table_id = config.get("account_table_id", "")
        self.cookie_table_id = config.get("cookie_table_id", "")
        self.app_token = config.get("app_token", "")

    # ========== 本地 → 飞书 ==========

    def sync_collection_to_feishu(self) -> dict:
        """同步采集表缓存到飞书"""
        result = {"created": 0, "updated": 0, "failed": 0, "errors": []}

        if not self.collection_table_id:
            result["errors"].append("未配置采集表 Table ID")
            return result

        try:
            # 获取本地采集表缓存
            local_records = self.db.get_all_collections()

            # 获取飞书采集表记录
            feishu_records = self.feishu.get_all_records(
                self.app_token,
                self.collection_table_id
            )

            # 建立飞书记录索引（基于记录ID）
            feishu_index = {r["record_id"]: r for r in feishu_records}

            for local in local_records:
                record_id = local.get("记录ID")

                if record_id and record_id in feishu_index:
                    # 更新已有记录
                    try:
                        fields = self._build_collection_fields(local)
                        self.feishu.update_record(
                            self.app_token,
                            self.collection_table_id,
                            record_id,
                            fields
                        )
                        result["updated"] += 1
                    except Exception as e:
                        result["failed"] += 1
                        result["errors"].append(f"更新 {record_id} 失败: {e}")
                else:
                    # 创建新记录
                    try:
                        fields = self._build_collection_fields(local)
                        response = self.feishu.create_record(
                            self.app_token,
                            self.collection_table_id,
                            fields
                        )
                        new_record_id = response.get("record", {}).get("record_id")
                        if new_record_id:
                            # 更新本地记录ID
                            self.db.update_collection(local["记录ID"], {
                                "记录ID": new_record_id
                            })
                        result["created"] += 1
                    except Exception as e:
                        result["failed"] += 1
                        result["errors"].append(f"创建记录失败: {e}")

        except Exception as e:
            result["errors"].append(f"同步采集表失败: {e}")

        return result

    def sync_account_to_feishu(self) -> dict:
        """同步账号表缓存到飞书"""
        result = {"created": 0, "updated": 0, "failed": 0, "errors": []}

        if not self.account_table_id:
            result["errors"].append("未配置账号表 Table ID")
            return result

        try:
            # 获取本地账号表缓存
            local_records = self.db.get_all_accounts()

            # 获取飞书账号表记录
            feishu_records = self.feishu.get_all_records(
                self.app_token,
                self.account_table_id
            )

            # 建立飞书记录索引（基于记录ID）
            feishu_index = {r["record_id"]: r for r in feishu_records}

            for local in local_records:
                record_id = local.get("记录ID")

                if record_id and record_id in feishu_index:
                    # 更新已有记录
                    try:
                        fields = self._build_account_fields(local)
                        self.feishu.update_record(
                            self.app_token,
                            self.account_table_id,
                            record_id,
                            fields
                        )
                        result["updated"] += 1
                    except Exception as e:
                        result["failed"] += 1
                        result["errors"].append(f"更新 {record_id} 失败: {e}")
                else:
                    # 创建新记录
                    try:
                        fields = self._build_account_fields(local)
                        response = self.feishu.create_record(
                            self.app_token,
                            self.account_table_id,
                            fields
                        )
                        new_record_id = response.get("record", {}).get("record_id")
                        if new_record_id:
                            # 更新本地记录ID
                            self.db.update_account(local["记录ID"], {
                                "记录ID": new_record_id
                            })
                        result["created"] += 1
                    except Exception as e:
                        result["failed"] += 1
                        result["errors"].append(f"创建记录失败: {e}")

        except Exception as e:
            result["errors"].append(f"同步账号表失败: {e}")

        return result

    def _build_collection_fields(self, record: dict) -> dict:
        """构建采集表飞书字段"""
        fields = {
            "分享码": record.get("分享码", ""),
            "平台": record.get("平台", ""),
            "等级": record.get("等级", 3),
            "已同步": bool(record.get("已同步", False)),
        }

        # 标签（JSON数组 → 飞书多选）
        tags = record.get("标签")
        if tags:
            try:
                if isinstance(tags, str):
                    tags = json.loads(tags)
                fields["标签"] = tags
            except:
                pass

        # 账号标识
        if record.get("账号标识"):
            fields["账号标识"] = record["账号标识"]

        # 同步错误
        if record.get("同步错误"):
            fields["同步错误"] = record["同步错误"]

        # 备注
        if record.get("备注"):
            fields["备注"] = record["备注"]

        # 次要字段
        if record.get("昵称"):
            fields["昵称"] = record["昵称"]
        if record.get("粉丝数"):
            fields["粉丝数"] = record["粉丝数"]
        if record.get("作品数"):
            fields["作品数"] = record["作品数"]

        return fields

    def _build_account_fields(self, record: dict) -> dict:
        """构建账号表飞书字段"""
        fields = {
            "账号名称": record.get("账号名称", ""),
            "平台": record.get("平台", ""),
            "链接": record.get("链接", ""),
            "账号标识": record.get("账号标识", ""),
            "等级": record.get("等级", 3),
            "已更新": bool(record.get("已更新", False)),
        }

        # 标签（JSON数组 → 飞书多选）
        tags = record.get("标签")
        if tags:
            try:
                if isinstance(tags, str):
                    tags = json.loads(tags)
                fields["标签"] = tags
            except:
                pass

        # 详细信息
        if record.get("昵称"):
            fields["昵称"] = record["昵称"]
        if record.get("粉丝数"):
            fields["粉丝数"] = record["粉丝数"]
        if record.get("作品数"):
            fields["作品数"] = record["作品数"]
        if record.get("签名"):
            fields["签名"] = record["签名"]
        if record.get("头像"):
            fields["头像"] = record["头像"]

        # 更新错误
        if record.get("更新错误"):
            fields["更新错误"] = record["更新错误"]

        return fields

    # ========== 飞书 → 本地 ==========

    def sync_collection_from_feishu(self) -> dict:
        """从飞书同步采集表到本地"""
        result = {"created": 0, "updated": 0, "failed": 0, "errors": []}

        if not self.collection_table_id:
            result["errors"].append("未配置采集表 Table ID")
            return result

        try:
            # 获取飞书采集表记录
            feishu_records = self.feishu.get_all_records(
                self.app_token,
                self.collection_table_id
            )

            for record in feishu_records:
                record_id = record.get("record_id")
                fields = record.get("fields", {})

                # 解析字段
                local_data = {
                    "记录ID": record_id,
                    "分享码": fields.get("分享码", ""),
                    "平台": fields.get("平台", ""),
                    "等级": fields.get("等级", 3),
                    "已同步": fields.get("已同步", False),
                    "更新时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                }

                # 标签（飞书多选 → JSON数组）
                tags = fields.get("标签")
                if tags:
                    if isinstance(tags, list):
                        local_data["标签"] = json.dumps(tags)
                    else:
                        local_data["标签"] = json.dumps([str(tags)])

                # 可选字段
                if fields.get("账号标识"):
                    local_data["账号标识"] = fields["账号标识"]
                if fields.get("同步错误"):
                    local_data["同步错误"] = fields["同步错误"]
                if fields.get("备注"):
                    local_data["备注"] = fields["备注"]
                if fields.get("昵称"):
                    local_data["昵称"] = fields["昵称"]
                if fields.get("粉丝数") is not None:
                    local_data["粉丝数"] = fields["粉丝数"]
                if fields.get("作品数") is not None:
                    local_data["作品数"] = fields["作品数"]

                # 检查本地是否已有
                existing = self.db.get_collection_by_id(record_id)
                if existing:
                    # 更新
                    try:
                        self.db.update_collection(record_id, local_data)
                        result["updated"] += 1
                    except Exception as e:
                        result["failed"] += 1
                        result["errors"].append(f"更新 {record_id} 失败: {e}")
                else:
                    # 创建
                    try:
                        local_data["创建时间"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        self.db.insert_collection(local_data)
                        result["created"] += 1
                    except Exception as e:
                        result["failed"] += 1
                        result["errors"].append(f"创建记录失败: {e}")

        except Exception as e:
            result["errors"].append(f"从飞书同步采集表失败: {e}")

        return result

    def sync_account_from_feishu(self) -> dict:
        """从飞书同步账号表到本地"""
        result = {"created": 0, "updated": 0, "failed": 0, "errors": []}

        if not self.account_table_id:
            result["errors"].append("未配置账号表 Table ID")
            return result

        try:
            # 获取飞书账号表记录
            feishu_records = self.feishu.get_all_records(
                self.app_token,
                self.account_table_id
            )

            for record in feishu_records:
                record_id = record.get("record_id")
                fields = record.get("fields", {})

                # 解析字段
                local_data = {
                    "记录ID": record_id,
                    "账号名称": fields.get("账号名称", ""),
                    "平台": fields.get("平台", ""),
                    "链接": fields.get("链接", ""),
                    "账号标识": fields.get("账号标识", ""),
                    "等级": fields.get("等级", 3),
                    "已更新": fields.get("已更新", False),
                    "更新时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                }

                # 标签（飞书多选 → JSON数组）
                tags = fields.get("标签")
                if tags:
                    if isinstance(tags, list):
                        local_data["标签"] = json.dumps(tags)
                    else:
                        local_data["标签"] = json.dumps([str(tags)])

                # 可选字段
                if fields.get("昵称"):
                    local_data["昵称"] = fields["昵称"]
                if fields.get("粉丝数") is not None:
                    local_data["粉丝数"] = fields["粉丝数"]
                if fields.get("作品数") is not None:
                    local_data["作品数"] = fields["作品数"]
                if fields.get("签名"):
                    local_data["签名"] = fields["签名"]
                if fields.get("头像"):
                    local_data["头像"] = fields["头像"]
                if fields.get("更新错误"):
                    local_data["更新错误"] = fields["更新错误"]

                # 检查本地是否已有
                existing = self.db.get_account_by_id(record_id)
                if existing:
                    # 更新
                    try:
                        self.db.update_account(record_id, local_data)
                        result["updated"] += 1
                    except Exception as e:
                        result["failed"] += 1
                        result["errors"].append(f"更新 {record_id} 失败: {e}")
                else:
                    # 创建
                    try:
                        local_data["创建时间"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        self.db.insert_account(local_data)
                        result["created"] += 1
                    except Exception as e:
                        result["failed"] += 1
                        result["errors"].append(f"创建记录失败: {e}")

        except Exception as e:
            result["errors"].append(f"从飞书同步账号表失败: {e}")

        return result

    # ========== 双向同步 ==========

    def sync_all(self) -> FeishuSyncResult:
        """执行完整的双向同步"""
        result = FeishuSyncResult()

        # 本地 → 飞书
        logger.info("同步采集表: 本地 → 飞书")
        coll_to = self.sync_collection_to_feishu()
        result.collection_to_feishu = {
            "created": coll_to["created"],
            "updated": coll_to["updated"],
            "failed": coll_to["failed"],
        }
        result.errors.extend(coll_to.get("errors", []))

        logger.info("同步账号表: 本地 → 飞书")
        acc_to = self.sync_account_to_feishu()
        result.account_to_feishu = {
            "created": acc_to["created"],
            "updated": acc_to["updated"],
            "failed": acc_to["failed"],
        }
        result.errors.extend(acc_to.get("errors", []))

        # 飞书 → 本地
        logger.info("同步采集表: 飞书 → 本地")
        coll_from = self.sync_collection_from_feishu()
        result.collection_from_feishu = {
            "created": coll_from["created"],
            "updated": coll_from["updated"],
            "failed": coll_from["failed"],
        }
        result.errors.extend(coll_from.get("errors", []))

        logger.info("同步账号表: 飞书 → 本地")
        acc_from = self.sync_account_from_feishu()
        result.account_from_feishu = {
            "created": acc_from["created"],
            "updated": acc_from["updated"],
            "failed": acc_from["failed"],
        }
        result.errors.extend(acc_from.get("errors", []))

        return result
