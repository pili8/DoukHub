"""飞书双向同步 v3 - 本地数据库 <-> 飞书表

设计原则（方案 B：LWW 真双向同步）：

1. 字段命名：业务字段中文与飞书 100% 一致，系统字段英文（record_id/is_deleted/synced/local_updated_at 等）
2. 冲突解决（LWW）：人工字段（等级/标签/备注/启用/采集类型/状态）两端都能改，比较最后修改时间，谁新谁赢
   API 字段（账号名称/粉丝数/作品数/签名/头像/sec_user_id/链接）仍为本地赢（DoukHub 是权威源）
3. 业务唯一键去重：分享表=share_code，账号表=sec_user_id，Cookie表=Cookie
4. 删除同步：
   - 飞书端直接删，本地通过差集反推感知（依赖 synced=1 标记）
   - 本地端软删除（is_deleted=1），推送墓碑到飞书，飞书删除后清本地墓碑
   - 删除优先：删除冲突时删除胜出
5. 增量同步：LWW 字段比较两端时间戳，其他字段基于值差异比对
6. 全盘同步：以一端为基准清空+重建
7. 安全保护：synced 过滤 + 空结果保护 + 50% 比例保护
"""
import json
import logging
import sqlite3
import time as _time
from datetime import datetime
from typing import Callable, Optional

from .database import Database
from .feishu import FeishuClient

logger = logging.getLogger("doukhub.feishu_sync")


class FeishuSyncer:
    """飞书双向同步器 v2"""

    # 业务唯一键（跨端去重用）
    BUSINESS_KEYS = {
        "share_cache": "share_code",
        "account_cache": "sec_user_id",
        "cookie_cache": "Cookie",
    }

    # 飞书表中的业务键名（与本地不同时用此映射）
    FEISHU_BUSINESS_KEYS = {
        "share_cache": "分享码",
    }

    # 业务字段（与飞书同步的字段，排除系统字段）
    # 按字段归属分组（方案 B：LWW 真双向同步）：
    # - lww：两端都能改，比较最后修改时间，谁新谁赢（原方案 A 的 feishu_wins 字段全部改为此类型）
    # - local_wins：DoukHub 是权威源，总是推送（API 字段、状态字段）
    # - immutable：创建后不变，不参与冲突
    # - sync_generated：同步动作产生的字段（同步时间等）
    #
    # 方案 B 取消了「feishu_wins」类型，原 feishu_wins 字段全部改为 lww。
    # Cookie 表的「状态」字段从 local_wins 改为 lww（两端都可能修改）。
    FIELD_OWNERSHIP = {
        "share_cache": {
            # LWW 字段：两端都能改，比较时间戳谁新谁赢
            "lww": ["等级", "标签", "备注", "账号名称", "粉丝数", "作品数"],
            # 本地赢字段：DoukHub 是权威源
            "local_wins": ["sec_user_id", "解析状态"],
            # 元数据：创建后不变
            "immutable": ["share_code", "平台"],
            # 同步产生
            "sync_generated": ["同步时间"],
        },
        "account_cache": {
            "lww": ["等级", "标签", "备注", "启用", "采集类型", "账号名称"],
            "local_wins": ["sec_user_id", "粉丝数", "作品数", "签名", "头像", "链接", "获取状态"],
            "immutable": ["平台"],
            "sync_generated": ["同步时间"],
        },
        "cookie_cache": {
            "lww": ["启用", "备注", "状态"],
            "local_wins": ["验证时间"],
            "immutable": ["Cookie", "平台"],
            "sync_generated": ["同步时间"],
        },
    }

    # 三张同步表的配置（表名 → 飞书 table_id 字段名 + 业务键 + 转换函数）
    TABLE_CONFIG = {
        "share_cache": {
            "table_id_key": "collection_table_id",
            "label": "分享表",
            "build_fields": "_build_collection_fields",
            "from_feishu": "_feishu_record_to_local_collection",
            "get_all_local": "get_all_collections",
            "get_by_id": "get_collection_by_id",
            "get_by_business_key": "get_collection_by_share",
            "insert": "insert_collection",
            "update": "update_collection",
        },
        "account_cache": {
            "table_id_key": "account_table_id",
            "label": "账号表",
            "build_fields": "_build_account_fields",
            "from_feishu": "_feishu_record_to_local_account",
            "get_all_local": "get_all_accounts",
            "get_by_id": "get_account_by_id",
            "get_by_business_key": "get_account_by_sec_user_id",
            "insert": "insert_account",
            "update": "update_account",
        },
        "cookie_cache": {
            "table_id_key": "cookie_table_id",
            "label": "Cookie表",
            "build_fields": "_build_cookie_fields",
            "from_feishu": "_feishu_record_to_local_cookie",
            "get_all_local": "get_all_cookies",
            "get_by_id": "get_cookie_by_id",
            "get_by_business_key": "_get_cookie_by_value",
            "insert": "insert_cookie",
            "update": "update_cookie",
        },
    }

    # 删除同步的安全阈值：飞书返回数量小于本地的 50% 时跳过删除
    DELETE_SAFETY_RATIO = 0.5

    def __init__(self, feishu: FeishuClient, config: dict):
        self.feishu = feishu
        self.config = config
        self.db = Database()
        self.collection_table_id = config.get("collection_table_id", "")
        self.account_table_id = config.get("account_table_id", "")
        self.cookie_table_id = config.get("cookie_table_id", "")
        self.app_token = config.get("app_token", "")

    def _get_table_id(self, db_table: str) -> str:
        """根据本地表名获取对应的飞书 table_id"""
        cfg = self.TABLE_CONFIG.get(db_table, {})
        attr_name = cfg.get("table_id_key", "")
        if not attr_name:
            return ""
        return getattr(self, attr_name, "")

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
            # 飞书文本字段是 [{"text": "..."}] 形式
            return "".join(
                s.get("text", str(s)) if isinstance(s, dict) else str(s)
                for s in value
            )
        if isinstance(value, dict):
            # URL 字段是 {"link": "...", "text": "..."}
            if "link" in value:
                return value.get("link", "")
            if "text" in value:
                return value.get("text", "")
        return str(value)

    def _safe_int(self, value, default=0) -> int:
        try:
            if value is None or value == "":
                return default
            return int(value)
        except (TypeError, ValueError):
            return default

    def _safe_bool(self, value, default=False) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return value != 0
        if isinstance(value, str):
            return value.lower() in ("true", "1", "是", "yes")
        return default

    @staticmethod
    def _normalize_tags(tags):
        """标签字段标准化：飞书是多选数组，本地是 JSON 字符串

        支持的输入：
        - None / 空 → None
        - JSON 字符串 → 解析为 list
        - 普通字符串 → 单元素 list
        - list → 转换为纯字符串 list
          - 飞书多选返回 [{"text": "标签1"}, {"text": "标签2"}]
          - 普通数组 ["标签1", "标签2"]
        """
        if not tags:
            return None
        if isinstance(tags, str):
            try:
                parsed = json.loads(tags)
                if isinstance(parsed, list):
                    tags = parsed
                else:
                    return [tags]
            except (json.JSONDecodeError, ValueError):
                return [tags]
        if isinstance(tags, list):
            # 飞书多选可能是 [{"text": "标签1"}] 格式
            result = []
            for t in tags:
                if isinstance(t, dict):
                    result.append(t.get("text", str(t)))
                else:
                    result.append(str(t))
            return result if result else None
        if isinstance(tags, dict):
            return [tags.get("text", str(tags))]
        return [str(tags)]

    def _get_cookie_by_value(self, cookie_value: str) -> Optional[dict]:
        """Cookie 表按 Cookie 值查找（业务键查询）"""
        if not cookie_value:
            return None
        for ck in self.db.get_all_cookies():
            if ck.get("Cookie") == cookie_value:
                return ck
        return None

    # ========== 方案 B：LWW 时间戳辅助方法 ==========

    @staticmethod
    def _parse_local_timestamp(ts) -> int:
        """本地时间戳（字符串）转毫秒 int

        用于 LWW 比较：将本地的 local_updated_at（秒级字符串）
        转换为毫秒级 int，与飞书的「最后更新时间」（毫秒）比较。
        """
        if not ts:
            return 0
        try:
            if isinstance(ts, (int, float)):
                return int(ts)
            dt = datetime.strptime(str(ts), "%Y-%m-%d %H:%M:%S")
            return int(dt.timestamp() * 1000)
        except Exception:
            return 0

    def _get_feishu_timestamp(self, feishu_record: dict) -> int:
        """从飞书记录提取「最后更新时间」（毫秒）

        注意：飞书可能没建此字段（首次部署），返回 0。
        """
        fields = feishu_record.get("fields", {})
        ts = fields.get("最后更新时间", 0)
        try:
            return int(ts) if ts else 0
        except (TypeError, ValueError):
            return 0

    def _extract_field_value(self, field: str, feishu_val):
        """从飞书值提取并转换为本地格式

        用于 LWW 飞书赢时，将飞书值解析为本地可存储的格式。
        """
        if feishu_val is None:
            return None
        if field == "标签":
            parsed = self._normalize_tags(feishu_val)
            return json.dumps(parsed, ensure_ascii=False) if parsed else None
        if field in ("等级", "粉丝数", "作品数"):
            v = self._safe_int(feishu_val)
            return v if v > 0 else None
        if field == "解析状态":
            return self._safe_text(feishu_val)
        if field == "获取状态":
            return self._safe_text(feishu_val)
        if field == "启用":
            return self._safe_bool(feishu_val)
        if field == "状态":
            v = self._parse_text_value(feishu_val)
            return v if v else None
        # 默认文本
        v = self._parse_text_value(feishu_val)
        return v if v else None

    @staticmethod
    def _merge_results(*results: dict) -> dict:
        """合并多个同步结果 dict，累加 failed/errors，分别取最大值"""
        merged = {
            "created": 0, "updated": 0, "deleted": 0,
            "skipped_uptodate": 0, "skipped_duplicate": 0, "skipped_invalid": 0,
            "failed": 0, "errors": [],
        }
        for r in results:
            if not r:
                continue
            for k in ("created", "updated", "deleted", "skipped_uptodate",
                      "skipped_duplicate", "skipped_invalid", "failed"):
                merged[k] += r.get(k, 0)
            merged["errors"].extend(r.get("errors", []))
        return merged

    # ========== 字段构建：本地 → 飞书 ==========

    def _build_collection_fields(self, record: dict) -> dict:
        """本地分享记录 → 飞书字段"""
        fields = {
            "分享码": record.get("share_code", ""),
            "平台": record.get("平台", ""),
            "等级": record.get("等级", 3),
            "解析状态": record.get("解析状态") or "待解析",
        }
        tags = self._normalize_tags(record.get("标签"))
        if tags:
            fields["标签"] = tags
        if record.get("sec_user_id"):
            fields["sec_user_id"] = record["sec_user_id"]
        if record.get("备注"):
            fields["备注"] = record["备注"]
        if record.get("账号名称"):
            fields["账号名称"] = record["账号名称"]
        if record.get("粉丝数") is not None:
            fields["粉丝数"] = record["粉丝数"]
        if record.get("作品数") is not None:
            fields["作品数"] = record["作品数"]
        fields["同步时间"] = int(_time.time() * 1000)
        return fields

    def _build_account_fields(self, record: dict) -> dict:
        """本地账号 → 飞书字段。字段名与飞书对齐，直接传递。"""
        fields = {
            "账号名称": record.get("账号名称", ""),
            "平台": record.get("平台", ""),
            "sec_user_id": record.get("sec_user_id", ""),
            "等级": record.get("等级", 3),
            "获取状态": record.get("获取状态") or "待获取",
        }
        if record.get("链接"):
            fields["链接"] = {"link": record["链接"], "text": "链接"}
        tags = self._normalize_tags(record.get("标签"))
        if tags:
            fields["标签"] = tags
        if record.get("备注"):
            fields["备注"] = record["备注"]
        enabled = record.get("启用")
        if enabled is not None:
            fields["启用"] = bool(enabled)
        ct = record.get("采集类型")
        if ct:
            fields["采集类型"] = ct
        if record.get("粉丝数") is not None:
            fields["粉丝数"] = record["粉丝数"]
        if record.get("作品数") is not None:
            fields["作品数"] = record["作品数"]
        if record.get("签名"):
            fields["签名"] = record["签名"]
        if record.get("头像"):
            fields["头像"] = {"link": record["头像"], "text": "头像"}
        fields["同步时间"] = int(_time.time() * 1000)
        return fields

    def _build_cookie_fields(self, cookie: dict) -> dict:
        """本地 Cookie → 飞书字段"""
        fields = {}
        cookie_value = cookie.get("Cookie", "")
        if cookie_value:
            fields["Cookie"] = cookie_value
        platform = cookie.get("平台", "")
        if platform:
            fields["平台"] = platform
        status = cookie.get("状态", "")
        if status:
            fields["状态"] = status
        else:
            fields["状态"] = "正常"
        enabled = cookie.get("启用")
        if enabled is not None:
            fields["启用"] = bool(enabled)
        remark = cookie.get("备注", "")
        if remark:
            fields["备注"] = remark
        verify_time = cookie.get("验证时间", "")
        if verify_time:
            try:
                ts = int(datetime.strptime(str(verify_time), "%Y-%m-%d %H:%M:%S").timestamp() * 1000)
                fields["验证时间"] = ts
            except Exception:
                pass
        fields["同步时间"] = int(_time.time() * 1000)
        return fields

    # ========== 飞书记录 → 本地数据 ==========

    def _feishu_record_to_local_collection(self, record):
        """飞书分享记录 → 本地数据"""
        fields = record.get("fields", {})
        share = self._parse_text_value(fields.get("分享码", ""))
        if not share.strip():
            return None
        data = {
            "share_code": share,
            "平台": self._parse_text_value(fields.get("平台", "")),
            "等级": self._safe_int(fields.get("等级", 3), 3),
            "解析状态": fields.get("解析状态") or "待解析",
        }
        if fields.get("sec_user_id"):
            data["sec_user_id"] = self._parse_text_value(fields.get("sec_user_id"))
        if fields.get("备注"):
            data["备注"] = self._parse_text_value(fields.get("备注"))
        if fields.get("账号名称"):
            data["账号名称"] = self._parse_text_value(fields.get("账号名称"))
        for k in ("粉丝数", "作品数"):
            v = fields.get(k)
            if v is not None:
                data[k] = self._safe_int(v)
        tags = self._normalize_tags(fields.get("标签"))
        if tags:
            data["标签"] = json.dumps(tags, ensure_ascii=False)
        return data

    def _feishu_record_to_local_account(self, record):
        """飞书账号记录 → 本地数据"""
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
        tags = self._normalize_tags(fields.get("标签"))
        if tags:
            data["标签"] = json.dumps(tags, ensure_ascii=False)
        for k in ("签名", "备注"):
            v = fields.get(k)
            if v:
                data[k] = self._parse_text_value(v)
        for k in ("粉丝数", "作品数"):
            v = fields.get(k)
            if v is not None:
                data[k] = self._safe_int(v)
        avatar = fields.get("头像")
        if avatar:
            data["头像"] = self._parse_text_value(avatar)
        link = fields.get("链接")
        if link:
            data["链接"] = self._parse_text_value(link)
        if "启用" in fields:
            data["启用"] = self._safe_bool(fields.get("启用"), True)
        ct = fields.get("采集类型")
        if ct:
            data["采集类型"] = self._parse_text_value(ct)
        if "获取状态" in fields:
            data["获取状态"] = self._safe_text(fields.get("获取状态")) or "待获取"
        return data

    def _feishu_record_to_local_cookie(self, record):
        """飞书 Cookie 记录 → 本地数据"""
        fields = record.get("fields", {})
        cookie_value = self._parse_text_value(fields.get("Cookie", ""))
        if not cookie_value.strip():
            return None
        data = {
            "Cookie": cookie_value,
            "平台": self._parse_text_value(fields.get("平台", "")),
            "状态": self._parse_text_value(fields.get("状态", "")) or "正常",
            "备注": self._parse_text_value(fields.get("备注", "")),
        }
        if "启用" in fields:
            data["启用"] = self._safe_bool(fields.get("启用"), True)
        verify = fields.get("验证时间") or fields.get("最后验证时间")  # 兼容旧字段
        if verify:
            try:
                ts = self._safe_int(verify)
                if ts > 0:
                    data["验证时间"] = datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                pass
        return data

    # ========== 字段差异比对 ==========

    def _values_equal(self, field: str, local_val, feishu_val) -> bool:
        """判断本地值和飞书值是否等价（处理类型差异）"""
        # 标签字段：本地 JSON 字符串 vs 飞书数组
        if field == "标签":
            local_tags = self._normalize_tags(local_val) or []
            feishu_tags = self._normalize_tags(feishu_val) or []
            return set(map(str, local_tags)) == set(map(str, feishu_tags))
        # 数值字段
        if field in ("等级", "粉丝数", "作品数"):
            return self._safe_int(local_val) == self._safe_int(feishu_val)
        # 布尔字段
        if field in ("解析状态", "获取状态"):
            return str(local_val) == str(feishu_val)
        if field == "启用":
            return self._safe_bool(local_val) == self._safe_bool(feishu_val)
        # 文本字段：去空格比较
        local_str = self._parse_text_value(local_val).strip() if local_val is not None else ""
        feishu_str = self._parse_text_value(feishu_val).strip() if feishu_val is not None else ""
        return local_str == feishu_str

    def _compute_field_updates(self, db_table: str, local_record: dict, feishu_record: dict) -> tuple[dict, dict]:
        """按字段归属计算需要更新到两端的数据（方案 B：LWW）

        返回 (to_feishu_updates, to_local_updates)
        - to_feishu_updates: 需要推送到飞书的字段（本地赢 + LWW 本地后改）
        - to_local_updates: 需要更新到本地的字段（LWW 飞书后改）

        LWW 逻辑：
        1. local_wins 字段：总是推送本地值到飞书（不变）
        2. lww 字段：比较两端最后修改时间，谁新谁赢
           - 本地后改 → 推送本地值到飞书
           - 飞书后改 → 更新本地
           - 时间戳都缺失 → 默认飞书赢（保守）
        3. 值相同时跳过（不比较时间戳）
        """
        ownership = self.FIELD_OWNERSHIP.get(db_table, {})
        feishu_fields = feishu_record.get("fields", {})

        to_feishu = {}
        to_local = {}

        # === Step 1: 本地赢字段（不变，总是推送） ===
        for field in ownership.get("local_wins", []):
            local_val = local_record.get(field)
            feishu_val = feishu_fields.get(field)
            if not self._values_equal(field, local_val, feishu_val):
                # 只有本地有值或飞书为空时才推送（避免覆盖飞书的非空值）
                if local_val not in (None, "", 0) or feishu_val in (None, "", 0):
                    to_feishu[field] = local_val

        # === Step 2: LWW 字段（按时间戳判断） ===
        local_ts = self._parse_local_timestamp(local_record.get("local_updated_at"))
        feishu_ts = self._get_feishu_timestamp(feishu_record)

        for field in ownership.get("lww", []):
            local_val = local_record.get(field)
            feishu_val = feishu_fields.get(field)
            if self._values_equal(field, local_val, feishu_val):
                continue  # 值相同，跳过

            # 时间戳缺失时的兜底处理
            # 飞书时间戳缺失时默认飞书赢（保守）：无法确定飞书最后修改时间，
            # 不应覆盖飞书的值（PLAN_B_LWW.md 第六节场景1）
            if local_ts == 0 and feishu_ts == 0:
                # 两端都没时间戳（首次部署或字段未建），默认飞书赢
                winner = "feishu"
            elif local_ts == 0:
                # 本地无时间戳（旧记录），飞书赢
                winner = "feishu"
            elif feishu_ts == 0:
                # 飞书无时间戳（字段未建），保守选择飞书赢
                winner = "feishu"
            elif local_ts > feishu_ts:
                winner = "local"
            else:
                winner = "feishu"  # 包括相等的情况（保守选择）

            if winner == "local":
                # 本地赢，推送本地值到飞书
                if local_val not in (None, "", 0):
                    to_feishu[field] = local_val
            else:
                # 飞书赢，更新本地
                parsed = self._extract_field_value(field, feishu_val)
                if parsed is not None:
                    to_local[field] = parsed

        return to_feishu, to_local

    # ========== 同步核心：本地 → 飞书（增量） ==========

    def _sync_to_feishu(self, db_table: str) -> dict:
        """本地 → 飞书 增量同步（含飞书删除检测 + 墓碑推送 + 字段差异更新）

        关键顺序（防止删除被恢复）：
        1. 推送本地墓碑 → 删飞书（先传播本地删除）
        2. 拉取飞书全表
        3. 检测飞书删除 → 删本地（避免下面把已删记录推回去）
        4. 重新拉取本地记录（删除检测后可能少了记录）
        5. 比对推送（创建/更新）
        """
        result = {"created": 0, "updated": 0, "deleted": 0,
                  "skipped_uptodate": 0, "skipped_duplicate": 0, "skipped_invalid": 0,
                  "failed": 0, "errors": []}
        cfg = self.TABLE_CONFIG.get(db_table)
        if not cfg:
            return result
        table_id = self._get_table_id(db_table)
        if not table_id:
            result["errors"].append(f"{cfg['label']} 未配置 table_id")
            return result

        build_fn = getattr(self, cfg["build_fields"])
        update_fn = getattr(self.db, cfg["update"])

        try:
            # Step 1: 推送本地墓碑 → 删飞书
            tombstone_result = self._push_tombstones_to_feishu(table_id, db_table)
            result = self._merge_results(result, tombstone_result)

            # Step 2: 拉取飞书全表，建立索引（按 record_id 和业务键）
            feishu_records = self.feishu.get_all_records(self.app_token, table_id)

            # Step 3: 检测飞书端删除 → 删本地（关键：防止后续步骤把已删记录推回去）
            deletion_result = self._detect_feishu_deletions(table_id, db_table, feishu_records)
            result = self._merge_results(result, deletion_result)

            # Step 4: 建立飞书索引（按 record_id 和业务键）
            feishu_by_id = {r["record_id"]: r for r in feishu_records}
            feishu_ids_set = set(feishu_by_id.keys())  # 用于检测飞书端重复业务键
            business_key = self.BUSINESS_KEYS.get(db_table)
            feishu_bk = self.FEISHU_BUSINESS_KEYS.get(db_table, business_key)
            feishu_by_key = {}
            for r in feishu_records:
                key_val = self._parse_text_value(r.get("fields", {}).get(feishu_bk, ""))
                if key_val:
                    feishu_by_key[key_val] = r

            # Step 5: 遍历本地记录（删除检测后重新拉取），比对并推送
            get_all_fn = getattr(self.db, cfg["get_all_local"])
            local_records = get_all_fn()

            to_create = []  # [{fields, local_record}]
            to_update = []  # [{record_id, fields, local_record}]

            for local in local_records:
                rid = local.get("record_id", "")
                is_synced = bool(local.get("synced", False))
                matched_feishu = None
                match_by = None  # "id" or "key"

                # 优先按 record_id 匹配
                if rid and rid in feishu_by_id:
                    matched_feishu = feishu_by_id[rid]
                    match_by = "id"
                else:
                    # 按 business_key 匹配（跨端去重）
                    local_key = local.get(business_key, "")
                    if local_key and local_key in feishu_by_key:
                        matched_feishu = feishu_by_key[local_key]
                        match_by = "key"

                if matched_feishu is None:
                    # 本地有飞书没有
                    # 关键保护：如果本地 synced=1（曾同步过），说明飞书删了它
                    # 即使删除检测被空结果/比例保护跳过，也不能推回去（会撤销用户删除）
                    # 只有 synced=0 才是真正的本地新建未推送
                    if is_synced:
                        result["skipped_invalid"] += 1
                        logger.info(f"{db_table} 跳过 synced=1 孤儿记录 {rid}（飞书已删但被保护未同步删除）")
                        continue
                    # synced=0 才推送到飞书
                    fields = build_fn(local)
                    if fields:
                        to_create.append({"fields": fields, "local": local})
                    else:
                        result["skipped_invalid"] += 1
                else:
                    # 都有 → 按字段归属合并
                    to_feishu_updates, _ = self._compute_field_updates(db_table, local, matched_feishu)
                    if to_feishu_updates:
                        # 同步时间总是更新
                        to_feishu_updates["同步时间"] = int(_time.time() * 1000)
                        # URL 字段特殊处理
                        for url_field in ("头像", "链接"):
                            if url_field in to_feishu_updates and to_feishu_updates[url_field]:
                                to_feishu_updates[url_field] = {
                                    "link": to_feishu_updates[url_field],
                                    "text": url_field,
                                }
                        to_update.append({
                            "record_id": matched_feishu["record_id"],
                            "fields": to_feishu_updates,
                            "local": local,
                        })
                        # 如果之前是按业务键匹配但 record_id 不一样，更新本地 record_id
                        if match_by == "key" and local.get("record_id") != matched_feishu["record_id"]:
                            try:
                                _sync_data = {"record_id": matched_feishu["record_id"], "synced": True}
                                _existing_ts = local.get("local_updated_at")
                                if _existing_ts:
                                    _sync_data["local_updated_at"] = _existing_ts
                                update_fn(local["record_id"], _sync_data)
                            except Exception:
                                pass
                    else:
                        result["skipped_uptodate"] += 1
                        # 如果是按业务键匹配但 record_id 不一样，仍然更新本地 record_id
                        if match_by == "key" and local.get("record_id") != matched_feishu["record_id"]:
                            try:
                                _sync_data = {"record_id": matched_feishu["record_id"], "synced": True}
                                _existing_ts = local.get("local_updated_at")
                                if _existing_ts:
                                    _sync_data["local_updated_at"] = _existing_ts
                                update_fn(local["record_id"], _sync_data)
                            except Exception:
                                pass

            # Step 4: 批量创建（每批 500 条）
            for i in range(0, len(to_create), 500):
                batch = to_create[i:i + 500]
                try:
                    payload = [{"fields": b["fields"]} for b in batch]
                    resp = self.feishu.batch_create_records(self.app_token, table_id, payload)
                    if resp.get("code") == 0:
                        result["created"] += len(batch)
                        # 更新本地的 record_id 和 synced 标记
                        recs = resp.get("data", {}).get("records", [])
                        for j, rec in enumerate(recs):
                            if j < len(batch):
                                nid = rec.get("record_id", "")
                                oid = batch[j]["local"].get("record_id", "")
                                if nid and oid:
                                    try:
                                        _sync_data = {"record_id": nid, "synced": True}
                                        _existing_ts = batch[j]["local"].get("local_updated_at")
                                        if _existing_ts:
                                            _sync_data["local_updated_at"] = _existing_ts
                                        update_fn(oid, _sync_data)
                                    except Exception as e:
                                        logger.warning(f"更新 record_id 失败 {oid}→{nid}: {e}")
                                elif nid:
                                    # 本地没 record_id，直接更新
                                    try:
                                        _sync_data = {"synced": True}
                                        _existing_ts = batch[j]["local"].get("local_updated_at")
                                        if _existing_ts:
                                            _sync_data["local_updated_at"] = _existing_ts
                                        update_fn(oid, _sync_data)
                                    except Exception:
                                        pass
                    else:
                        result["failed"] += len(batch)
                        result["errors"].append(f"批量创建失败: {resp.get('msg', '')}")
                except Exception as e:
                    result["failed"] += len(batch)
                    result["errors"].append(f"创建异常: {e}")

            # Step 5: 批量更新（每批 500 条）
            for i in range(0, len(to_update), 500):
                batch = to_update[i:i + 500]
                try:
                    payload = [{"record_id": b["record_id"], "fields": b["fields"]} for b in batch]
                    resp = self.feishu.batch_update_records(self.app_token, table_id, payload)
                    if resp.get("code") == 0:
                        result["updated"] += len(batch)
                        # 更新本地的 synced 标记
                        for b in batch:
                            oid = b["local"].get("record_id", "")
                            if oid:
                                try:
                                    _sync_data = {"synced": True}
                                    _existing_ts = b["local"].get("local_updated_at")
                                    if _existing_ts:
                                        _sync_data["local_updated_at"] = _existing_ts
                                    update_fn(oid, _sync_data)
                                except Exception:
                                    pass
                    else:
                        result["failed"] += len(batch)
                        result["errors"].append(f"批量更新失败: {resp.get('msg', '')}")
                except Exception as e:
                    result["failed"] += len(batch)
                    result["errors"].append(f"更新异常: {e}")

        except Exception as e:
            result["errors"].append(f"{db_table} 同步异常: {e}")
            logger.exception(f"{db_table} → 飞书 同步失败")

        return result

    # ========== 同步核心：飞书 → 本地（增量） ==========

    def _sync_from_feishu(self, db_table: str) -> dict:
        """飞书 → 本地 增量同步（含差集反推删除 + 字段差异更新）"""
        result = {"created": 0, "updated": 0, "deleted": 0,
                  "skipped_uptodate": 0, "skipped_duplicate": 0, "skipped_invalid": 0,
                  "failed": 0, "errors": []}
        cfg = self.TABLE_CONFIG.get(db_table)
        if not cfg:
            return result
        table_id = self._get_table_id(db_table)
        if not table_id:
            result["errors"].append(f"{cfg['label']} 未配置 table_id")
            return result

        convert_fn = getattr(self, cfg["from_feishu"])
        get_by_id_fn = getattr(self.db, cfg["get_by_id"])
        # 业务键查询：走 db（除 Cookie 外都是 db 的方法）
        biz_key_method = cfg["get_by_business_key"]
        if biz_key_method == "_get_cookie_by_value":
            get_by_key_fn = self._get_cookie_by_value
        else:
            get_by_key_fn = getattr(self.db, biz_key_method)
        insert_fn = getattr(self.db, cfg["insert"])
        update_fn = getattr(self.db, cfg["update"])
        business_key = self.BUSINESS_KEYS.get(db_table)

        try:
            # Step 1: 检测飞书端删除（差集反推）
            deletion_result = self._detect_feishu_deletions(table_id, db_table)
            result = self._merge_results(result, deletion_result)

            # Step 2: 拉取飞书全表
            feishu_records = self.feishu.get_all_records(self.app_token, table_id)
            feishu_ids_set = {r["record_id"] for r in feishu_records}  # 用于检测飞书端重复业务键

            # Step 3: 遍历飞书记录，比对并更新/创建本地
            for record in feishu_records:
                rid = record.get("record_id", "")
                fields = record.get("fields", {})
                local_data = convert_fn(record)
                if not local_data:
                    result["skipped_invalid"] += 1
                    continue

                # 查找本地是否已有此记录
                existing = None
                # 优先按 record_id 查
                if rid:
                    existing = get_by_id_fn(rid)
                # 按 business_key 查（跨端去重）
                if not existing:
                    key_val = local_data.get(business_key, "")
                    if key_val:
                        existing = get_by_key_fn(key_val)
                        # 检测飞书端重复业务键
                        if existing and existing.get("record_id"):
                            existing_rid = existing["record_id"]
                            if existing_rid != rid and existing_rid in feishu_ids_set:
                                # 飞书端有重复业务键（existing.record_id 也在飞书）
                                # 这条 record 是冗余的，跳过避免 record_id 反复横跳
                                result["skipped_duplicate"] += 1
                                logger.warning(
                                    f"{db_table} 飞书端检测到重复业务键 {business_key}={key_val}，"
                                    f"已有 {existing_rid}，跳过冗余记录 {rid}（请去飞书清理）"
                                )
                                continue
                            # 否则正常合并 + 更新 record_id
                            if existing_rid and existing_rid != rid:
                                try:
                                    _sync_data = {"record_id": rid, "synced": True}
                                    _existing_ts = existing.get("local_updated_at")
                                    if _existing_ts:
                                        _sync_data["local_updated_at"] = _existing_ts
                                    update_fn(existing_rid, _sync_data)
                                    existing["record_id"] = rid
                                except Exception:
                                    pass

                if existing:
                    # 都有 → 按字段归属合并
                    _, to_local_updates = self._compute_field_updates(db_table, existing, record)
                    if to_local_updates:
                        # 方案 B：用飞书的最后更新时间作为本地的 local_updated_at，
                        # 避免下次同步误判为"本地后改"。
                        feishu_ts = self._get_feishu_timestamp(record)
                        if feishu_ts:
                            to_local_updates["local_updated_at"] = datetime.fromtimestamp(
                                feishu_ts / 1000
                            ).strftime("%Y-%m-%d %H:%M:%S")
                        to_local_updates["synced"] = True
                        try:
                            update_fn(rid or existing["record_id"], to_local_updates)
                            result["updated"] += 1
                        except Exception as e:
                            result["failed"] += 1
                            result["errors"].append(f"{rid}: {e}")
                    else:
                        # 字段相同，但可能需要补 synced 标记
                        if not existing.get("synced"):
                            try:
                                _sync_data = {"synced": True}
                                _existing_ts = existing.get("local_updated_at")
                                if _existing_ts:
                                    _sync_data["local_updated_at"] = _existing_ts
                                update_fn(rid or existing["record_id"], _sync_data)
                            except Exception:
                                pass
                        result["skipped_uptodate"] += 1
                else:
                    # 飞书有本地没有 → 插入本地
                    local_data["record_id"] = rid
                    local_data["synced"] = True
                    try:
                        insert_fn(local_data)
                        result["created"] += 1
                    except sqlite3.IntegrityError:
                        result["skipped_duplicate"] += 1
                    except Exception as e:
                        result["failed"] += 1
                        result["errors"].append(f"{rid}: {e}")

        except Exception as e:
            result["errors"].append(f"{db_table} 同步异常: {e}")
            logger.exception(f"飞书 → {db_table} 同步失败")

        return result

    # ========== 删除同步 ==========

    def _push_tombstones_to_feishu(self, table_id: str, db_table: str) -> dict:
        """本地墓碑 → 删飞书：把本地标了 is_deleted=1 的记录从飞书删掉"""
        result = {"created": 0, "updated": 0, "deleted": 0,
                  "skipped_uptodate": 0, "skipped_duplicate": 0, "skipped_invalid": 0,
                  "failed": 0, "errors": []}
        try:
            tombstone_ids = self.db.get_deleted_ids(db_table)
            if not tombstone_ids:
                return result

            # 检查飞书是否真有这些记录
            feishu_records = self.feishu.get_all_records(self.app_token, table_id)
            feishu_ids = {r["record_id"] for r in feishu_records}
            to_delete = [rid for rid in tombstone_ids if rid in feishu_ids]

            if not to_delete:
                # 飞书端已经没有这些记录了，直接清墓碑
                for rid in tombstone_ids:
                    self.db.purge_tombstone(db_table, rid)
                return result

            for i in range(0, len(to_delete), 500):
                batch = to_delete[i:i + 500]
                try:
                    resp = self.feishu.batch_delete_records(self.app_token, table_id, batch)
                    if resp.get("code") == 0:
                        result["deleted"] += len(batch)
                        for rid in batch:
                            self.db.purge_tombstone(db_table, rid)
                    else:
                        result["failed"] += len(batch)
                        result["errors"].append(f"删除失败: {resp.get('msg', '')}")
                except Exception as e:
                    result["failed"] += len(batch)
                    result["errors"].append(f"删除异常: {e}")
        except Exception as e:
            result["errors"].append(f"墓碑推送异常: {e}")
            logger.exception(f"{db_table} 墓碑推送失败")
        return result

    def _detect_feishu_deletions(self, table_id: str, db_table: str, feishu_records: list = None) -> dict:
        """飞书删除 → 删本地：飞书有、本地没有（且 synced=1）说明飞书删了

        安全保护：
        - synced=1 过滤：本地新建未同步的不会被误删
        - 空结果保护：飞书返回 0 条直接跳过
        - 比例保护：飞书返回 < 本地 50% 时跳过

        参数：
        - feishu_records: 可选，预查询的飞书记录列表（避免重复拉取）
        """
        result = {"created": 0, "updated": 0, "deleted": 0,
                  "skipped_uptodate": 0, "skipped_duplicate": 0, "skipped_invalid": 0,
                  "failed": 0, "errors": []}
        try:
            if feishu_records is None:
                feishu_records = self.feishu.get_all_records(self.app_token, table_id)
            # 空结果保护
            if not feishu_records:
                result["skipped_invalid"] += 1
                logger.warning(f"{db_table} 飞书返回空，跳过删除检测")
                return result

            feishu_ids = {r["record_id"] for r in feishu_records}
            local_synced_ids = self.db.get_synced_active_ids(db_table)

            # 比例保护
            if local_synced_ids and len(feishu_records) < len(local_synced_ids) * self.DELETE_SAFETY_RATIO:
                result["skipped_invalid"] += 1
                msg = (f"{db_table} 安全保护触发：飞书 {len(feishu_records)} 条 < 本地 {len(local_synced_ids)} 条的 {self.DELETE_SAFETY_RATIO*100:.0f}%，"
                       f"跳过删除检测")
                result["errors"].append(msg)
                logger.warning(msg)
                return result

            orphan_ids = [rid for rid in local_synced_ids if rid not in feishu_ids]
            for rid in orphan_ids:
                try:
                    self.db.hard_delete(db_table, rid)
                    result["deleted"] += 1
                except Exception as e:
                    result["failed"] += 1
                    result["errors"].append(f"删除孤儿 {rid}: {e}")
        except Exception as e:
            result["errors"].append(f"删除检测异常: {e}")
            logger.exception(f"{db_table} 删除检测失败")
        return result

    # ========== 公开入口：单表单方向同步（供 SSE 进度展示用）==========

    def sync_collection_to_feishu(self) -> dict:
        """本地 → 云端：分享表"""
        return self._sync_to_feishu("share_cache")

    def sync_account_to_feishu(self) -> dict:
        """本地 → 云端：账号表"""
        return self._sync_to_feishu("account_cache")

    def sync_cookie_to_feishu(self) -> dict:
        """本地 → 云端：Cookie表"""
        return self._sync_to_feishu("cookie_cache")

    def sync_collection_from_feishu(self) -> dict:
        """云端 → 本地：分享表"""
        return self._sync_from_feishu("share_cache")

    def sync_account_from_feishu(self) -> dict:
        """云端 → 本地：账号表"""
        return self._sync_from_feishu("account_cache")

    def sync_cookie_from_feishu(self) -> dict:
        """云端 → 本地：Cookie表"""
        return self._sync_from_feishu("cookie_cache")

    # ========== 公开入口：增量同步（双向 6 步） ==========

    def sync_incremental(self) -> dict:
        """增量双向同步（6 步：3 表 × 2 方向）

        返回 {label: result} 形式的合并结果（用于启动时自动同步等不显示进度的场景）
        UI 调用应使用 get_incremental_steps() 拆分为 6 个独立步骤以获得进度展示
        """
        all_results = {}
        # 本地 → 飞书（3 表）
        for db_table in ("share_cache", "account_cache", "cookie_cache"):
            cfg = self.TABLE_CONFIG[db_table]
            label = f"本地 → 云端：{cfg['label']}"
            try:
                all_results[label] = self._sync_to_feishu(db_table)
            except Exception as e:
                all_results[label] = {"failed": 1, "errors": [str(e)]}
        # 飞书 → 本地（3 表）
        for db_table in ("share_cache", "account_cache", "cookie_cache"):
            cfg = self.TABLE_CONFIG[db_table]
            label = f"云端 → 本地：{cfg['label']}"
            try:
                all_results[label] = self._sync_from_feishu(db_table)
            except Exception as e:
                all_results[label] = {"failed": 1, "errors": [str(e)]}
        return all_results

    def get_incremental_steps(self) -> list:
        """获取增量同步的 6 个独立步骤（用于 SSE 进度展示）

        返回 [(label, callable), ...]
        """
        return [
            ("本地 → 云端：分享表", self.sync_collection_to_feishu),
            ("本地 → 云端：账号表", self.sync_account_to_feishu),
            ("本地 → 云端：Cookie表", self.sync_cookie_to_feishu),
            ("云端 → 本地：分享表", self.sync_collection_from_feishu),
            ("云端 → 本地：账号表", self.sync_account_from_feishu),
            ("云端 → 本地：Cookie表", self.sync_cookie_from_feishu),
        ]

    def get_full_steps(self, direction: str) -> list:
        """获取全盘同步的步骤列表（用于 SSE 进度展示）

        direction: "to-feishu"（以本地覆盖云端） | "from-feishu"（以云端覆盖本地）
        """
        if direction == "to-feishu":
            return [
                ("覆盖云端：分享表", lambda: self._full_to_feishu_single("share_cache")),
                ("覆盖云端：账号表", lambda: self._full_to_feishu_single("account_cache")),
                ("覆盖云端：Cookie表", lambda: self._full_to_feishu_single("cookie_cache")),
            ]
        else:
            return [
                ("覆盖本地：分享表", lambda: self._full_from_feishu_single("share_cache")),
                ("覆盖本地：账号表", lambda: self._full_from_feishu_single("account_cache")),
                ("覆盖本地：Cookie表", lambda: self._full_from_feishu_single("cookie_cache")),
            ]

    # ========== 公开入口：全盘同步（清空+重建，整体调用） ==========

    def sync_full_to_feishu(self) -> dict:
        """全盘同步：以本地为基准，清空飞书 → 推送本地全部

        ⚠️ 危险操作：会清空飞书表所有数据，二次确认后才能调用
        """
        all_results = {}
        for db_table in ("share_cache", "account_cache", "cookie_cache"):
            cfg = self.TABLE_CONFIG[db_table]
            label = f"覆盖云端：{cfg['label']}"
            try:
                all_results[label] = self._full_to_feishu_single(db_table)
            except Exception as e:
                all_results[label] = {"failed": 1, "errors": [str(e)]}
        return all_results

    def sync_full_from_feishu(self) -> dict:
        """全盘同步：以飞书为基准，清空本地 → 拉取飞书全部

        ⚠️ 危险操作：会清空本地表所有数据（含墓碑），二次确认后才能调用
        """
        all_results = {}
        for db_table in ("share_cache", "account_cache", "cookie_cache"):
            cfg = self.TABLE_CONFIG[db_table]
            label = f"覆盖本地：{cfg['label']}"
            try:
                all_results[label] = self._full_from_feishu_single(db_table)
            except Exception as e:
                all_results[label] = {"failed": 1, "errors": [str(e)]}
        return all_results

    def _full_to_feishu_single(self, db_table: str) -> dict:
        """以本地为基准覆盖飞书单表"""
        result = {"created": 0, "updated": 0, "deleted": 0,
                  "skipped_uptodate": 0, "skipped_duplicate": 0, "skipped_invalid": 0,
                  "failed": 0, "errors": []}
        cfg = self.TABLE_CONFIG[db_table]
        table_id = self._get_table_id(db_table)
        if not table_id:
            result["errors"].append(f"{cfg['label']} 未配置 table_id")
            return result

        build_fn = getattr(self, cfg["build_fields"])
        update_fn = getattr(self.db, cfg["update"])
        get_all_fn = getattr(self.db, cfg["get_all_local"])

        try:
            # Step 1: 拉取飞书全表，全删
            feishu_records = self.feishu.get_all_records(self.app_token, table_id)
            all_feishu_ids = [r["record_id"] for r in feishu_records]
            for i in range(0, len(all_feishu_ids), 500):
                batch = all_feishu_ids[i:i + 500]
                try:
                    resp = self.feishu.batch_delete_records(self.app_token, table_id, batch)
                    if resp.get("code") == 0:
                        result["deleted"] += len(batch)
                    else:
                        result["failed"] += len(batch)
                        result["errors"].append(f"清空失败: {resp.get('msg', '')}")
                except Exception as e:
                    result["failed"] += len(batch)
                    result["errors"].append(f"清空异常: {e}")

            # Step 2: 把本地全部记录推送到飞书
            local_records = get_all_fn()
            to_create = []
            for local in local_records:
                fields = build_fn(local)
                if fields:
                    to_create.append({"fields": fields, "local": local})
                else:
                    result["skipped_invalid"] += 1

            for i in range(0, len(to_create), 500):
                batch = to_create[i:i + 500]
                try:
                    payload = [{"fields": b["fields"]} for b in batch]
                    resp = self.feishu.batch_create_records(self.app_token, table_id, payload)
                    if resp.get("code") == 0:
                        result["created"] += len(batch)
                        # 更新本地 record_id 为飞书新分配的，标记 synced=1
                        recs = resp.get("data", {}).get("records", [])
                        for j, rec in enumerate(recs):
                            if j < len(batch):
                                nid = rec.get("record_id", "")
                                oid = batch[j]["local"].get("record_id", "")
                                if nid and oid:
                                    try:
                                        _sync_data = {"record_id": nid, "synced": True}
                                        _existing_ts = batch[j]["local"].get("local_updated_at")
                                        if _existing_ts:
                                            _sync_data["local_updated_at"] = _existing_ts
                                        update_fn(oid, _sync_data)
                                    except Exception as e:
                                        logger.warning(f"更新 record_id 失败 {oid}→{nid}: {e}")
                    else:
                        result["failed"] += len(batch)
                        result["errors"].append(f"重建失败: {resp.get('msg', '')}")
                except Exception as e:
                    result["failed"] += len(batch)
                    result["errors"].append(f"重建异常: {e}")
        except Exception as e:
            result["errors"].append(f"{db_table} 全盘推送异常: {e}")
            logger.exception(f"{db_table} 全盘推送失败")
        return result

    def _full_from_feishu_single(self, db_table: str) -> dict:
        """以飞书为基准覆盖本地单表"""
        result = {"created": 0, "updated": 0, "deleted": 0,
                  "skipped_uptodate": 0, "skipped_duplicate": 0, "skipped_invalid": 0,
                  "failed": 0, "errors": []}
        cfg = self.TABLE_CONFIG[db_table]
        table_id = self._get_table_id(db_table)
        if not table_id:
            result["errors"].append(f"{cfg['label']} 未配置 table_id")
            return result

        convert_fn = getattr(self, cfg["from_feishu"])
        insert_fn = getattr(self.db, cfg["insert"])

        try:
            # Step 1: 清空本地表（含墓碑）
            with self.db._connect() as conn:
                conn.execute(f"DELETE FROM {db_table}")
                conn.commit()

            # Step 2: 拉取飞书全表，逐条插入
            feishu_records = self.feishu.get_all_records(self.app_token, table_id)
            for record in feishu_records:
                rid = record.get("record_id", "")
                local_data = convert_fn(record)
                if not local_data:
                    result["skipped_invalid"] += 1
                    continue
                local_data["record_id"] = rid
                local_data["synced"] = True
                try:
                    insert_fn(local_data)
                    result["created"] += 1
                except sqlite3.IntegrityError:
                    result["skipped_duplicate"] += 1
                except Exception as e:
                    result["failed"] += 1
                    result["errors"].append(f"{rid}: {e}")

            # 记录清空的删除数（统计用）
            result["deleted"] = 0  # 已经清空了，不再额外统计
        except Exception as e:
            result["errors"].append(f"{db_table} 全盘拉取异常: {e}")
            logger.exception(f"{db_table} 全盘拉取失败")
        return result
