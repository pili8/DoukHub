# -*- coding: utf-8 -*-
"""飞书双向同步 - 本地数据库 <-> 飞书表"""
import json
import logging
import sqlite3
import time as _time
from datetime import datetime

from .database import Database
from .feishu import FeishuClient

logger = logging.getLogger("doukhub.feishu_sync")


class FeishuSyncer:
    """飞书双向同步器"""

    # 飞书表 → 本地数据库表 的映射（用于删除同步）
    _TABLE_MAP = {
        "collection": "collection_cache",
        "account": "account_cache",
        "cookie": "cookie_cache",
    }

    def __init__(self, feishu: FeishuClient, config: dict):
        self.feishu = feishu
        self.config = config
        self.db = Database()
        self.collection_table_id = config.get("collection_table_id", "")
        self.account_table_id = config.get("account_table_id", "")
        self.cookie_table_id = config.get("cookie_table_id", "")
        self.app_token = config.get("app_token", "")

    # ========== 辅助方法 ==========

    @staticmethod
    def _parse_text_value(value) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        if isinstance(value, (int, float)):
            return str(value)
        if isinstance(value, list):
            return "".join(
                s.get("text", str(s)) if isinstance(s, dict) else str(s)
                for s in value
            )
        return str(value)

    @staticmethod
    def _parse_local_time(time_val) -> int:
        if not time_val:
            return 0
        try:
            if isinstance(time_val, (int, float)):
                return int(time_val)
            dt = datetime.strptime(str(time_val), "%Y-%m-%d %H:%M:%S")
            return int(dt.timestamp() * 1000)
        except Exception:
            return 0

    def _safe_int(self, value, default=0) -> int:
        try:
            return int(value) if value else default
        except (TypeError, ValueError):
            return default

    def _feishu_to_db_synced(self, value) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value in ("\u5df2\u540c\u6b65", "true", "True", "1")
        return False

    def _db_to_feishu_synced(self, value):
        return "\u5df2\u540c\u6b65" if value else "\u5f85\u540c\u6b65"

    # ========== 字段构建 ==========

    def _build_collection_fields(self, record: dict) -> dict:
        fields = {
            "Share": record.get("分享码", ""),
            "平台": record.get("平台", ""),
            "等级": record.get("等级", 3),
            "同步状态": self._db_to_feishu_synced(record.get("已同步", False)),
        }
        tags = record.get("标签")
        if tags:
            try:
                if isinstance(tags, str):
                    tags = json.loads(tags)
                fields["标签"] = tags
            except Exception:
                pass
        if record.get("sec_user_id"):
            fields["sec_user_id"] = record["sec_user_id"]
        if record.get("同步错误"):
            fields["备注"] = record["同步错误"]
        elif record.get("备注"):
            fields["备注"] = record["备注"]
        if record.get("昵称"):
            fields["昵称"] = record["昵称"]
        if record.get("粉丝数") is not None:
            fields["粉丝数"] = record["粉丝数"]
        if record.get("作品数") is not None:
            fields["作品数"] = record["作品数"]
        if record.get("账号名称"):
            fields["账号名称"] = record["账号名称"]
        if record.get("签名"):
            fields["签名"] = record["签名"]
        if record.get("头像"):
            # URL 字段需要对象格式
            fields["头像"] = {"link": record["头像"], "text": "头像"}
        fields["同步时间"] = int(_time.time() * 1000)
        return fields

    def _build_account_fields(self, record: dict) -> dict:
        """本地账号 → 飞书字段。字段名现已与飞书对齐，直接传递。"""
        fields = {
            "账号名称": record.get("账号名称", ""),
            "平台": record.get("平台", ""),
            "sec_user_id": record.get("sec_user_id", ""),
            "等级": record.get("等级", 3),
            "已获取信息": bool(record.get("已获取信息", False)),
        }
        if record.get("链接"):
            # URL 字段需要对象格式
            fields["链接"] = {"link": record["链接"], "text": "链接"}
        tags = record.get("标签")
        if tags:
            try:
                if isinstance(tags, str):
                    tags = json.loads(tags)
                fields["标签"] = tags
            except Exception:
                pass
        if record.get("昵称"):
            fields["昵称"] = record["昵称"]
        if record.get("粉丝数") is not None:
            fields["粉丝数"] = record["粉丝数"]
        if record.get("作品数") is not None:
            fields["作品数"] = record["作品数"]
        if record.get("签名"):
            fields["签名"] = record["签名"]
        if record.get("头像"):
            # URL 字段需要对象格式
            fields["头像"] = {"link": record["头像"], "text": "头像"}
        # 备注（本地"备注"对应飞书"备注"）
        if record.get("备注"):
            fields["备注"] = record["备注"]
        # 启用
        enabled = record.get("启用")
        if enabled is not None:
            fields["启用"] = bool(enabled)
        # 采集类型
        ct = record.get("采集类型")
        if ct:
            fields["采集类型"] = ct
        fields["同步时间"] = int(_time.time() * 1000)
        return fields

    def _build_cookie_fields(self, cookie: dict) -> dict:
        fields = {}
        cookie_value = cookie.get("Cookie", "")
        if cookie_value:
            fields["Cookie"] = cookie_value
        platform = cookie.get("\u5e73\u53f0", "")
        if platform:
            fields["\u5e73\u53f0"] = platform
        status = cookie.get("\u72b6\u6001", "")
        if status:
            fields["\u72b6\u6001"] = status
        enabled = cookie.get("\u542f\u7528")
        if enabled is not None:
            fields["\u542f\u7528"] = bool(enabled)
        remark = cookie.get("\u5907\u6ce8", "")
        if remark:
            fields["\u5907\u6ce8"] = remark
        verify_time = cookie.get("\u9a8c\u8bc1\u65f6\u95f4", "")
        if verify_time:
            try:
                ts = int(datetime.strptime(str(verify_time), "%Y-%m-%d %H:%M:%S").timestamp() * 1000)
                fields["\u6700\u540e\u9a8c\u8bc1\u65f6\u95f4"] = ts
            except Exception:
                pass
        fields["\u540c\u6b65\u65f6\u95f4"] = int(_time.time() * 1000)
        return fields

    # ========== 飞书记录转本地 ==========

    def _feishu_record_to_local_collection(self, record):
        fields = record.get("fields", {})
        share = self._parse_text_value(fields.get("Share", ""))
        if not share.strip():
            return None
        data = {
            "\u5206\u4eab\u7801": share,
            "\u5e73\u53f0": self._parse_text_value(fields.get("\u5e73\u53f0", "")),
            "\u7b49\u7ea7": self._safe_int(fields.get("\u7b49\u7ea7", 3), 3),
            "\u5df2\u540c\u6b65": self._feishu_to_db_synced(fields.get("\u540c\u6b65\u72b6\u6001")),
        }
        if fields.get("sec_user_id"):
            data["sec_user_id"] = fields["sec_user_id"]
        tags = fields.get("\u6807\u7b7e")
        if tags:
            data["\u6807\u7b7e"] = json.dumps(tags if isinstance(tags, list) else [str(tags)])
        for k, fk in [("\u6635\u79f0", "\u6635\u79f0"), ("\u7c89\u4e1d\u6570", "\u7c89\u4e1d\u6570"), ("\u4f5c\u54c1\u6570", "\u4f5c\u54c1\u6570")]:
            v = fields.get(fk)
            if v is not None:
                data[k] = self._safe_int(v) if k in ("\u7c89\u4e1d\u6570", "\u4f5c\u54c1\u6570") else self._parse_text_value(v)
        return data

    def _feishu_record_to_local_account(self, record):
        """飞书记录 → 本地账号。字段名现已与飞书对齐，直接读取。"""
        fields = record.get("fields", {})
        sec = self._parse_text_value(fields.get("sec_user_id", ""))
        if not sec.strip():
            return None
        data = {
            "sec_user_id": sec,
            "账号名称": self._parse_text_value(fields.get("账号名称", "")),
            "平台": self._parse_text_value(fields.get("平台", "")),
            "等级": self._safe_int(fields.get("等级", 3), 3),
        }
        tags = fields.get("标签")
        if tags:
            data["标签"] = json.dumps(tags if isinstance(tags, list) else [str(tags)])
        for k, fk in [("昵称", "昵称"), ("粉丝数", "粉丝数"), ("作品数", "作品数"), ("签名", "签名"), ("头像", "头像"), ("链接", "链接")]:
            v = fields.get(fk)
            if v is not None:
                data[k] = self._safe_int(v) if k in ("粉丝数", "作品数") else self._parse_text_value(v)
        # 备注
        remark = self._parse_text_value(fields.get("备注", ""))
        if remark:
            data["备注"] = remark
        # 启用
        if "启用" in fields:
            data["启用"] = bool(fields.get("启用"))
        # 采集类型
        ct = self._parse_text_value(fields.get("采集类型", ""))
        if ct:
            data["采集类型"] = ct
        # 已获取信息
        if "已获取信息" in fields:
            data["已获取信息"] = bool(fields.get("已获取信息"))
        return data

    def _feishu_record_to_local_cookie(self, record):
        fields = record.get("fields", {})
        cookie_value = self._parse_text_value(fields.get("Cookie", ""))
        if not cookie_value.strip():
            return None
        return {
            "Cookie": cookie_value,
            "\u5e73\u53f0": self._parse_text_value(fields.get("\u5e73\u53f0", "")),
            "\u72b6\u6001": self._parse_text_value(fields.get("\u72b6\u6001", "")) or "\u6b63\u5e38",
            "\u5907\u6ce8": self._parse_text_value(fields.get("\u5907\u6ce8", "")),
        }

    # ========== 批量 -> 飞书 ==========

    def _batch_to_feishu(self, table_id, local_records, build_fn, db_update_fn, incremental=False, batch_size=500):
        # skipped_uptodate: 飞书已是最新的（增量同步正常情况）
        # skipped_invalid: 本地数据缺记录ID或无法构造字段（异常）
        result = {"created": 0, "updated": 0, "skipped_uptodate": 0, "skipped_invalid": 0, "failed": 0, "errors": []}
        if not table_id:
            return result
        try:
            feishu_records = self.feishu.get_all_records(self.app_token, table_id)
            feishu_index = {r["record_id"]: r for r in feishu_records}
            to_create, to_update, create_locals = [], [], []
            for local in local_records:
                rid = local.get("\u8bb0\u5f55ID", "")
                if not rid:
                    result["skipped_invalid"] += 1
                    continue
                if rid and rid in feishu_index:
                    if incremental:
                        # 比较「最后更新时间」
                        fs_update_time = self._safe_int(feishu_index[rid].get("fields", {}).get("最后更新时间", 0))
                        local_update_time = self._parse_local_time(local.get("最后更新时间", ""))
                        # 如果飞书的最后更新时间 >= 本地的最后更新时间，说明飞书已是最新
                        if fs_update_time and local_update_time and fs_update_time >= local_update_time - 5000:
                            result["skipped_uptodate"] += 1
                            continue
                    fields = build_fn(local)
                    if fields:
                        to_update.append({"record_id": rid, "fields": fields})
                        # 更新本地的最后更新时间为当前时间
                        if db_update_fn:
                            try:
                                db_update_fn(rid, {"最后更新时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
                            except Exception:
                                pass
                else:
                    fields = build_fn(local)
                    if fields:
                        to_create.append({"fields": fields})
                        create_locals.append(local)
            for i in range(0, len(to_create), batch_size):
                batch, bl = to_create[i:i+batch_size], create_locals[i:i+batch_size]
                try:
                    resp = self.feishu.batch_create_records(self.app_token, table_id, batch)
                    if resp.get("code") == 0:
                        result["created"] += len(batch)
                        recs = resp.get("data", {}).get("records", [])
                        for j, rec in enumerate(recs):
                            if j < len(bl):
                                nid, oid = rec.get("record_id", ""), bl[j].get("\u8bb0\u5f55ID", "")
                                if nid and oid and db_update_fn:
                                    try:
                                        update_data = {"synced": True}
                                        if nid != oid:
                                            update_data["\u8bb0\u5f55ID"] = nid
                                        db_update_fn(oid, update_data)
                                    except Exception:
                                        pass
                except Exception as e:
                    result["failed"] += len(batch)
                    result["errors"].append("create err: " + str(e))
            for i in range(0, len(to_update), batch_size):
                batch = to_update[i:i+batch_size]
                try:
                    resp = self.feishu.batch_update_records(self.app_token, table_id, batch)
                    if resp.get("code") == 0:
                        result["updated"] += len(batch)
                    else:
                        result["failed"] += len(batch)
                        result["errors"].append("update: " + resp.get("msg", ""))
                except Exception as e:
                    result["failed"] += len(batch)
                    result["errors"].append("update err: " + str(e))
        except Exception as e:
            result["errors"].append(str(e))
        return result

    # ========== 飞书 -> 本地（全盘） ==========

    def _from_feishu_full(self, table_id, convert_fn, db_update_fn, db_insert_fn, get_by_id_fn):
        # skipped_duplicate: 飞书数据 UNIQUE 字段与本地已有冲突（如同一分享码录了多次）
        # skipped_invalid: 转换函数返回 None（数据本身缺关键字段）
        result = {"created": 0, "updated": 0, "skipped_duplicate": 0, "skipped_invalid": 0, "failed": 0, "errors": []}
        if not table_id:
            return result
        try:
            for record in self.feishu.get_all_records(self.app_token, table_id):
                rid = record.get("record_id", "")
                local_data = convert_fn(record)
                if not local_data:
                    result["skipped_invalid"] += 1
                    continue
                local_data["\u8bb0\u5f55ID"] = rid
                local_data["synced"] = True
                existing = get_by_id_fn(rid)
                if existing:
                    try:
                        db_update_fn(rid, local_data)
                        result["updated"] += 1
                    except Exception as e:
                        result["failed"] += 1
                        result["errors"].append(f"{rid}: {e}")
                else:
                    try:
                        db_insert_fn(local_data)
                        result["created"] += 1
                    except sqlite3.IntegrityError:
                        result["skipped_duplicate"] += 1
                    except Exception as e:
                        result["failed"] += 1
                        result["errors"].append(f"{rid}: {e}")
        except Exception as e:
            result["errors"].append(str(e))
        return result

    # ========== 飞书 -> 本地（增量） ==========

    def _from_feishu_incremental(self, table_id, db_get_by_id, convert_fn, db_update_fn, db_insert_fn):
        # 飞书→本地方向：比较「最后更新时间」，只更新有变化的记录
        # skipped_uptodate: 本地已是最新的（最后更新时间相同或更新）
        # skipped_duplicate: 飞书数据 UNIQUE 字段与本地已有冲突（如同一分享码录了多次）
        # skipped_invalid: 转换函数返回 None（数据本身缺关键字段）
        result = {"created": 0, "updated": 0, "skipped_uptodate": 0, "skipped_duplicate": 0, "skipped_invalid": 0, "failed": 0, "errors": []}
        if not table_id:
            return result
        try:
            for record in self.feishu.get_all_records(self.app_token, table_id):
                rid = record.get("record_id", "")
                fields = record.get("fields", {})
                existing = db_get_by_id(rid)
                if existing:
                    # 比较「最后更新时间」
                    feishu_update_time = self._safe_int(fields.get("最后更新时间", 0))
                    local_update_time = self._parse_local_time(existing.get("最后更新时间", ""))
                    
                    # 如果飞书的最后更新时间 <= 本地的最后更新时间，说明本地已是最新
                    if feishu_update_time and local_update_time and feishu_update_time <= local_update_time + 5000:
                        result["skipped_uptodate"] += 1
                        continue
                    
                    # 飞书有更新，同步到本地
                    try:
                        local_data = convert_fn(record)
                        if local_data:
                            local_data["synced"] = True
                            # 保存飞书的最后更新时间到本地
                            if feishu_update_time:
                                local_data["最后更新时间"] = datetime.fromtimestamp(feishu_update_time / 1000).strftime("%Y-%m-%d %H:%M:%S")
                            db_update_fn(rid, local_data)
                            result["updated"] += 1
                        else:
                            result["skipped_invalid"] += 1
                    except Exception as e:
                        result["failed"] += 1
                        result["errors"].append(f"{rid}: {e}")
                    continue
                # 新记录，插入本地
                local_data = convert_fn(record)
                if not local_data:
                    result["skipped_invalid"] += 1
                    continue
                local_data["记录ID"] = rid
                local_data["synced"] = True
                feishu_update_time = self._safe_int(fields.get("最后更新时间", 0))
                if feishu_update_time:
                    local_data["最后更新时间"] = datetime.fromtimestamp(feishu_update_time / 1000).strftime("%Y-%m-%d %H:%M:%S")
                try:
                    db_insert_fn(local_data)
                    result["created"] += 1
                except sqlite3.IntegrityError:
                    result["skipped_duplicate"] += 1
                except Exception as e:
                    result["failed"] += 1
                    result["errors"].append(f"{rid}: {e}")
        except Exception as e:
            result["errors"].append(str(e))
        return result

    # ========== 全盘同步（3表 x 2方向） ==========

    def sync_collection_to_feishu(self):
        result = self._sync_deletions_from_feishu(self.collection_table_id, "collection_cache")
        result.update(self._batch_to_feishu(self.collection_table_id, self.db.get_all_collections(), self._build_collection_fields, self.db.update_collection))
        result.update(self._sync_deletions_to_feishu(self.collection_table_id, "collection_cache"))
        return result

    def sync_account_to_feishu(self):
        result = self._sync_deletions_from_feishu(self.account_table_id, "account_cache")
        result.update(self._batch_to_feishu(self.account_table_id, self.db.get_all_accounts(), self._build_account_fields, self.db.update_account))
        result.update(self._sync_deletions_to_feishu(self.account_table_id, "account_cache"))
        return result

    def sync_cookie_to_feishu(self):
        result = self._sync_deletions_from_feishu(self.cookie_table_id, "cookie_cache")
        result.update(self._batch_to_feishu(self.cookie_table_id, self.db.get_all_cookies(), self._build_cookie_fields, self.db.update_cookie))
        result.update(self._sync_deletions_to_feishu(self.cookie_table_id, "cookie_cache"))
        return result

    def sync_collection_from_feishu(self):
        return self._from_feishu_full(self.collection_table_id, self._feishu_record_to_local_collection, self.db.update_collection, self.db.insert_collection, self.db.get_collection_by_id)

    def sync_account_from_feishu(self):
        return self._from_feishu_full(self.account_table_id, self._feishu_record_to_local_account, self.db.update_account, self.db.insert_account, self.db.get_account_by_id)

    def sync_cookie_from_feishu(self):
        return self._from_feishu_full(self.cookie_table_id, self._feishu_record_to_local_cookie, self.db.update_cookie, self.db.insert_cookie, self.db.get_cookie_by_id)

    # ========== 删除同步 ==========

    def _sync_deletions_to_feishu(self, table_id: str, db_table: str) -> dict:
        """本地墓碑 → 删飞书：把本地标了删除的记录从飞书表中删掉"""
        result = {"deleted": 0, "skipped": 0, "failed": 0, "errors": []}
        if not table_id:
            return result
        try:
            tombstone_ids = self.db.get_deleted_ids(db_table)
            if not tombstone_ids:
                return result
            feishu_records = self.feishu.get_all_records(self.app_token, table_id)
            feishu_ids = {r["record_id"] for r in feishu_records}
            to_delete = [rid for rid in tombstone_ids if rid in feishu_ids]
            if not to_delete:
                # 飞书侧已经没有这些记录了，直接清墓碑
                for rid in tombstone_ids:
                    self.db.purge_tombstone(db_table, rid)
                return result
            for i in range(0, len(to_delete), 500):
                batch = to_delete[i:i + 500]
                resp = self.feishu.batch_delete_records(self.app_token, table_id, batch)
                if resp.get("code") == 0:
                    result["deleted"] += len(batch)
                    for rid in batch:
                        self.db.purge_tombstone(db_table, rid)
                else:
                    result["failed"] += len(batch)
                    result["errors"].append("delete: " + resp.get("msg", ""))
        except Exception as e:
            result["errors"].append(str(e))
        return result

    def _sync_deletions_from_feishu(self, table_id: str, db_table: str) -> dict:
        """飞书删除 → 删本地：飞书有、本地没有的记录说明飞书那边删了"""
        result = {"deleted": 0, "skipped": 0, "failed": 0, "errors": []}
        if not table_id:
            return result
        try:
            feishu_records = self.feishu.get_all_records(self.app_token, table_id)
            # 保险：飞书返回空可能是 API 异常，跳过删除避免误清空
            if not feishu_records:
                result["skipped"] = 1
                return result
            feishu_ids = {r["record_id"] for r in feishu_records}
            local_ids = self.db.get_synced_active_ids(db_table)
            orphan_ids = [rid for rid in local_ids if rid not in feishu_ids]
            for rid in orphan_ids:
                self.db.hard_delete(db_table, rid)
                result["deleted"] += 1
        except Exception as e:
            result["errors"].append(str(e))
        return result

    # ========== 增量同步（6步拆分） ==========

    def _incremental_collection_to_feishu(self):
        result = self._sync_deletions_from_feishu(self.collection_table_id, "collection_cache")
        result.update(self._batch_to_feishu(self.collection_table_id, self.db.get_all_collections(), self._build_collection_fields, self.db.update_collection, incremental=True))
        result.update(self._sync_deletions_to_feishu(self.collection_table_id, "collection_cache"))
        return result

    def _incremental_account_to_feishu(self):
        result = self._sync_deletions_from_feishu(self.account_table_id, "account_cache")
        result.update(self._batch_to_feishu(self.account_table_id, self.db.get_all_accounts(), self._build_account_fields, self.db.update_account, incremental=True))
        result.update(self._sync_deletions_to_feishu(self.account_table_id, "account_cache"))
        return result

    def _incremental_cookie_to_feishu(self):
        result = self._sync_deletions_from_feishu(self.cookie_table_id, "cookie_cache")
        result.update(self._batch_to_feishu(self.cookie_table_id, self.db.get_all_cookies(), self._build_cookie_fields, self.db.update_cookie, incremental=True))
        result.update(self._sync_deletions_to_feishu(self.cookie_table_id, "cookie_cache"))
        return result

    def _incremental_collection_from_feishu(self):
        return self._from_feishu_incremental(self.collection_table_id, self.db.get_collection_by_id, self._feishu_record_to_local_collection, self.db.update_collection, self.db.insert_collection)

    def _incremental_account_from_feishu(self):
        return self._from_feishu_incremental(self.account_table_id, self.db.get_account_by_id, self._feishu_record_to_local_account, self.db.update_account, self.db.insert_account)

    def _incremental_cookie_from_feishu(self):
        return self._from_feishu_incremental(self.cookie_table_id, self.db.get_cookie_by_id, self._feishu_record_to_local_cookie, self.db.update_cookie, self.db.insert_cookie)
