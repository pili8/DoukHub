# -*- coding: utf-8 -*-
"""飞书双向同步 - 本地数据库 <-> 飞书表"""
import json
import logging
import time as _time
from datetime import datetime

from .database import Database
from .feishu import FeishuClient

logger = logging.getLogger("doukhub.feishu_sync")


class FeishuSyncer:
    """飞书双向同步器"""

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
            "Share": record.get("\u5206\u4eab\u7801", ""),
            "\u5e73\u53f0": record.get("\u5e73\u53f0", ""),
            "\u7b49\u7ea7": record.get("\u7b49\u7ea7", 3),
            "\u540c\u6b65\u72b6\u6001": self._db_to_feishu_synced(record.get("\u5df2\u540c\u6b65", False)),
        }
        tags = record.get("\u6807\u7b7e")
        if tags:
            try:
                if isinstance(tags, str):
                    tags = json.loads(tags)
                fields["\u6807\u7b7e"] = tags
            except Exception:
                pass
        if record.get("\u8d26\u53f7\u6807\u8bc6"):
            fields["sec_user_id"] = record["\u8d26\u53f7\u6807\u8bc6"]
        if record.get("\u540c\u6b65\u9519\u8bef"):
            fields["\u5907\u6ce8"] = record["\u540c\u6b65\u9519\u8bef"]
        elif record.get("\u5907\u6ce8"):
            fields["\u5907\u6ce8"] = record["\u5907\u6ce8"]
        if record.get("\u6635\u79f0"):
            fields["\u6635\u79f0"] = record["\u6635\u79f0"]
        if record.get("\u7c89\u4e1d\u6570") is not None:
            fields["\u7c89\u4e1d\u6570"] = record["\u7c89\u4e1d\u6570"]
        if record.get("\u4f5c\u54c1\u6570") is not None:
            fields["\u4f5c\u54c1\u6570"] = record["\u4f5c\u54c1\u6570"]
        if record.get("\u8d26\u53f7\u540d\u79f0"):
            fields["\u8d26\u53f7\u540d\u79f0"] = record["\u8d26\u53f7\u540d\u79f0"]
        fields["\u540c\u6b65\u65f6\u95f4"] = int(_time.time() * 1000)
        return fields

    def _build_account_fields(self, record: dict) -> dict:
        fields = {
            "\u8d26\u53f7\u540d\u79f0": record.get("\u8d26\u53f7\u540d\u79f0", ""),
            "\u5e73\u53f0": record.get("\u5e73\u53f0", ""),
            "sec_user_id": record.get("\u8d26\u53f7\u6807\u8bc6", ""),
            "\u7b49\u7ea7": record.get("\u7b49\u7ea7", 3),
            "\u5df2\u83b7\u53d6\u4fe1\u606f": bool(record.get("\u5df2\u66f4\u65b0", False)),
        }
        if record.get("\u94fe\u63a5"):
            fields["\u94fe\u63a5"] = record["\u94fe\u63a5"]
        tags = record.get("\u6807\u7b7e")
        if tags:
            try:
                if isinstance(tags, str):
                    tags = json.loads(tags)
                fields["\u6807\u7b7e"] = tags
            except Exception:
                pass
        if record.get("\u6635\u79f0"):
            fields["\u6635\u79f0"] = record["\u6635\u79f0"]
        if record.get("\u7c89\u4e1d\u6570") is not None:
            fields["\u7c89\u4e1d\u6570"] = record["\u7c89\u4e1d\u6570"]
        if record.get("\u4f5c\u54c1\u6570") is not None:
            fields["\u4f5c\u54c1\u6570"] = record["\u4f5c\u54c1\u6570"]
        if record.get("\u7b7e\u540d"):
            fields["\u7b7e\u540d"] = record["\u7b7e\u540d"]
        if record.get("\u5934\u50cf"):
            fields["\u5934\u50cf"] = record["\u5934\u50cf"]
        if record.get("\u66f4\u65b0\u9519\u8bef"):
            fields["\u5907\u6ce8"] = record["\u66f4\u65b0\u9519\u8bef"]
        fields["\u540c\u6b65\u65f6\u95f4"] = int(_time.time() * 1000)
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
            data["\u8d26\u53f7\u6807\u8bc6"] = fields["sec_user_id"]
        tags = fields.get("\u6807\u7b7e")
        if tags:
            data["\u6807\u7b7e"] = json.dumps(tags if isinstance(tags, list) else [str(tags)])
        for k, fk in [("\u6635\u79f0", "\u6635\u79f0"), ("\u7c89\u4e1d\u6570", "\u7c89\u4e1d\u6570"), ("\u4f5c\u54c1\u6570", "\u4f5c\u54c1\u6570")]:
            v = fields.get(fk)
            if v is not None:
                data[k] = self._safe_int(v) if k in ("\u7c89\u4e1d\u6570", "\u4f5c\u54c1\u6570") else self._parse_text_value(v)
        return data

    def _feishu_record_to_local_account(self, record):
        fields = record.get("fields", {})
        sec = self._parse_text_value(fields.get("sec_user_id", ""))
        if not sec.strip():
            return None
        data = {
            "\u8d26\u53f7\u6807\u8bc6": sec,
            "\u8d26\u53f7\u540d\u79f0": self._parse_text_value(fields.get("\u8d26\u53f7\u540d\u79f0", "")),
            "\u5e73\u53f0": self._parse_text_value(fields.get("\u5e73\u53f0", "")),
            "\u7b49\u7ea7": self._safe_int(fields.get("\u7b49\u7ea7", 3), 3),
        }
        tags = fields.get("\u6807\u7b7e")
        if tags:
            data["\u6807\u7b7e"] = json.dumps(tags if isinstance(tags, list) else [str(tags)])
        for k, fk in [("\u6635\u79f0", "\u6635\u79f0"), ("\u7c89\u4e1d\u6570", "\u7c89\u4e1d\u6570"), ("\u4f5c\u54c1\u6570", "\u4f5c\u54c1\u6570"), ("\u7b7e\u540d", "\u7b7e\u540d")]:
            v = fields.get(fk)
            if v is not None:
                data[k] = self._safe_int(v) if k in ("\u7c89\u4e1d\u6570", "\u4f5c\u54c1\u6570") else self._parse_text_value(v)
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
        result = {"created": 0, "updated": 0, "skipped": 0, "failed": 0, "errors": []}
        if not table_id:
            return result
        try:
            feishu_records = self.feishu.get_all_records(self.app_token, table_id)
            feishu_index = {r["record_id"]: r for r in feishu_records}
            to_create, to_update, create_locals = [], [], []
            for local in local_records:
                rid = local.get("\u8bb0\u5f55ID", "")
                if rid and rid in feishu_index:
                    if incremental:
                        fs_time = self._safe_int(feishu_index[rid].get("fields", {}).get("\u540c\u6b65\u65f6\u95f4", 0))
                        local_time = self._parse_local_time(local.get("\u66f4\u65b0\u65f6\u95f4", ""))
                        if fs_time and local_time and fs_time >= local_time - 5000:
                            result["skipped"] += 1
                            continue
                    fields = build_fn(local)
                    if fields:
                        to_update.append({"record_id": rid, "fields": fields})
                        sync_ts = self._safe_int(fields.get("\u540c\u6b65\u65f6\u95f4", 0))
                        if sync_ts and db_update_fn:
                            try:
                                db_update_fn(rid, {"\u66f4\u65b0\u65f6\u95f4": datetime.fromtimestamp(sync_ts / 1000).strftime("%Y-%m-%d %H:%M:%S")})
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
                                if nid and oid and nid != oid and db_update_fn:
                                    try:
                                        db_update_fn(oid, {"\u8bb0\u5f55ID": nid})
                                    except Exception:
                                        pass
                    else:
                        result["failed"] += len(batch)
                        result["errors"].append("create: " + resp.get("msg", ""))
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
        result = {"created": 0, "updated": 0, "failed": 0, "errors": []}
        if not table_id:
            return result
        try:
            for record in self.feishu.get_all_records(self.app_token, table_id):
                rid = record.get("record_id", "")
                local_data = convert_fn(record)
                if not local_data:
                    continue
                local_data["\u8bb0\u5f55ID"] = rid
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
                        if db_insert_fn(local_data):
                            result["created"] += 1
                        else:
                            result["failed"] += 1
                    except Exception as e:
                        result["failed"] += 1
                        result["errors"].append(f"{rid}: {e}")
        except Exception as e:
            result["errors"].append(str(e))
        return result

    # ========== 飞书 -> 本地（增量） ==========

    def _from_feishu_incremental(self, table_id, db_get_by_id, convert_fn, db_update_fn, db_insert_fn):
        result = {"created": 0, "updated": 0, "skipped": 0, "failed": 0, "errors": []}
        if not table_id:
            return result
        try:
            for record in self.feishu.get_all_records(self.app_token, table_id):
                rid = record.get("record_id", "")
                fields = record.get("fields", {})
                existing = db_get_by_id(rid)
                if existing:
                    fs_time = self._safe_int(fields.get("\u540c\u6b65\u65f6\u95f4", 0))
                    local_time = self._parse_local_time(existing.get("\u66f4\u65b0\u65f6\u95f4", ""))
                    if local_time and (not fs_time or fs_time <= local_time + 5000):
                        result["skipped"] += 1
                        continue
                    try:
                        local_data = convert_fn(record)
                        if local_data:
                            ts = self._safe_int(fields.get("\u540c\u6b65\u65f6\u95f4", 0))
                            if ts:
                                local_data["\u66f4\u65b0\u65f6\u95f4"] = datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d %H:%M:%S")
                            db_update_fn(rid, local_data)
                            result["updated"] += 1
                        else:
                            result["skipped"] += 1
                    except Exception as e:
                        result["failed"] += 1
                        result["errors"].append(f"{rid}: {e}")
                    continue
                local_data = convert_fn(record)
                if not local_data:
                    result["skipped"] += 1
                    continue
                local_data["\u8bb0\u5f55ID"] = rid
                ts = self._safe_int(fields.get("\u540c\u6b65\u65f6\u95f4", 0))
                if ts:
                    local_data["\u66f4\u65b0\u65f6\u95f4"] = datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d %H:%M:%S")
                if db_insert_fn(local_data):
                    result["created"] += 1
                else:
                    result["skipped"] += 1
        except Exception as e:
            result["errors"].append(str(e))
        return result

    # ========== 全盘同步（3表 x 2方向） ==========

    def sync_collection_to_feishu(self):
        return self._batch_to_feishu(self.collection_table_id, self.db.get_all_collections(), self._build_collection_fields, self.db.update_collection)

    def sync_account_to_feishu(self):
        return self._batch_to_feishu(self.account_table_id, self.db.get_all_accounts(), self._build_account_fields, self.db.update_account)

    def sync_cookie_to_feishu(self):
        return self._batch_to_feishu(self.cookie_table_id, self.db.get_all_cookies(), self._build_cookie_fields, self.db.update_cookie)

    def sync_collection_from_feishu(self):
        return self._from_feishu_full(self.collection_table_id, self._feishu_record_to_local_collection, self.db.update_collection, self.db.insert_collection, self.db.get_collection_by_id)

    def sync_account_from_feishu(self):
        return self._from_feishu_full(self.account_table_id, self._feishu_record_to_local_account, self.db.update_account, self.db.insert_account, self.db.get_account_by_id)

    def sync_cookie_from_feishu(self):
        return self._from_feishu_full(self.cookie_table_id, self._feishu_record_to_local_cookie, self.db.update_cookie, self.db.insert_cookie, self.db.get_cookie_by_id)

    # ========== 增量同步（6步拆分） ==========

    def _incremental_collection_to_feishu(self):
        return self._batch_to_feishu(self.collection_table_id, self.db.get_all_collections(), self._build_collection_fields, self.db.update_collection, incremental=True)

    def _incremental_account_to_feishu(self):
        return self._batch_to_feishu(self.account_table_id, self.db.get_all_accounts(), self._build_account_fields, self.db.update_account, incremental=True)

    def _incremental_cookie_to_feishu(self):
        return self._batch_to_feishu(self.cookie_table_id, self.db.get_all_cookies(), self._build_cookie_fields, self.db.update_cookie, incremental=True)

    def _incremental_collection_from_feishu(self):
        return self._from_feishu_incremental(self.collection_table_id, self.db.get_collection_by_id, self._feishu_record_to_local_collection, self.db.update_collection, self.db.insert_collection)

    def _incremental_account_from_feishu(self):
        return self._from_feishu_incremental(self.account_table_id, self.db.get_account_by_id, self._feishu_record_to_local_account, self.db.update_account, self.db.insert_account)

    def _incremental_cookie_from_feishu(self):
        return self._from_feishu_incremental(self.cookie_table_id, self.db.get_cookie_by_id, self._feishu_record_to_local_cookie, self.db.update_cookie, self.db.insert_cookie)
