"""飞书双向同步 v4 —— 行级 LWW + 本地映射表

核心设计（取代 v3 的字段分类 LWW + 墓碑 + synced 推断）：

1. 时间基准：飞书表加一个原生「修改时间」字段（类型 1002，飞书自动维护，毫秒）。
   本地基准是业务表自带的 local_updated_at（database.py 自动维护）。
   两端时间戳都恒有值，LWW 比较才真正成立（v3 的死穴：缺时间戳默认飞书赢）。

2. 行级 LWW：不再逐字段分类归属。整行比较，谁最后修改谁赢：
   - 本地后改 → 整行推送到飞书（含空值显式清空）
   - 飞书后改 → 整行写回本地（含清空 + local_updated_at 对齐飞书时间）
   判断"谁改过"依据映射表记录的上次同步时间戳，而不是猜测。

3. 本地映射表 feishu_sync_map（database.py）：
   (table_type, local_key) → (record_id, feishu_ts, local_ts)
   - record_id 显式保存，不再存在业务表里靠差集反推 → synced 孤儿问题消失
   - 删除传播：映射在、本地行没了 → 删飞书；映射在、飞书记录没了 → 删本地
   - 两向删除都有 50% 比例保护（防 API 截断误删全表）

4. 业务唯一键：分享表=share_code(飞书:分享码)，账号表=sec_user_id，Cookie表=Cookie

5. 全盘覆盖改造：不再"清空+重建"（中途失败留空表、record_id 全变）。
   改为按键差异覆盖：本地多的推上去/飞书多的删掉（to-feishu），反向同理。
   record_id 稳定，任何时刻中断都只是"部分完成"，不会出现空表。

6. 兼容性：保留 v3 的公开接口签名（sync_incremental / get_incremental_steps /
   get_full_steps / sync_full_* / 6 个单表方法 / _merge_results / 结果 schema），
   main.py 的后台任务与 UI 无需改动。
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

# 飞书侧自动维护的时间字段（毫秒 int）。v4 起统一用「修改时间」。
FEISHU_TS_FIELD = "修改时间"
# 推送/比较时排除的字段（同步动作自己维护，不参与行级 LWW）
_SYNC_EXCLUDED_FIELDS = {"同步时间", FEISHU_TS_FIELD}


class FeishuSyncer:
    """飞书双向同步器 v4（行级 LWW + 映射表）"""

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

    # 三张同步表的配置（本地表名 → 飞书 table_id 属性名 + 转换/读写方法名）
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

    # 删除同步安全阈值：待删数量 > 映射数的 50% 时跳过删除（防 API 截断误删全表）
    DELETE_SAFETY_RATIO = 0.5

    # 本地行整行覆盖时允许显式清空的字段（飞书→本地）。None = 置 NULL。
    _LOCAL_CLEARABLE = {
        "share_cache": ["备注", "账号名称", "sec_user_id", "标签", "粉丝数", "作品数"],
        "account_cache": ["备注", "签名", "头像", "链接", "标签", "采集类型", "粉丝数", "作品数"],
        "cookie_cache": ["备注", "验证时间"],
    }

    # 推送时允许显式清空的飞书字段（本地→飞书）。文本清空用 ""，多选用 []。
    _FEISHU_CLEAR_TEXT = {"备注", "账号名称", "签名", "状态", "解析状态", "采集类型"}
    _FEISHU_CLEAR_MULTI = {"标签"}

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
        """标签字段标准化：飞书是多选数组，本地是 JSON 字符串"""
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

    # ========== 时间戳辅助 ==========

    @staticmethod
    def _parse_local_timestamp(ts) -> int:
        """本地 local_updated_at（秒级字符串）→ 毫秒 int；解析失败返回 0"""
        if not ts:
            return 0
        try:
            if isinstance(ts, (int, float)):
                return int(ts)
            dt = datetime.strptime(str(ts), "%Y-%m-%d %H:%M:%S")
            return int(dt.timestamp() * 1000)
        except Exception:
            return 0

    @staticmethod
    def _ms_to_local_str(ms: int) -> str:
        """毫秒时间戳 → 本地 "YYYY-MM-DD HH:MM:SS"（秒精度，向下取整）"""
        return datetime.fromtimestamp(int(ms) // 1000).strftime("%Y-%m-%d %H:%M:%S")

    def _get_feishu_timestamp(self, feishu_record: dict) -> int:
        """从飞书记录提取「修改时间」（毫秒）。

        字段缺失或未建时返回 0：此时视为"飞书侧改动不可知"，
        本地改动照常推送，飞书端改动暂不回写（不会误覆盖）。
        """
        fields = feishu_record.get("fields", {})
        ts = fields.get(FEISHU_TS_FIELD, 0)
        try:
            return int(ts) if ts else 0
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _merge_results(*results: dict) -> dict:
        """合并多个同步结果 dict，累加计数与错误"""
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

    # ========== 字段构建：本地 → 飞书（整行） ==========

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
        fields["状态"] = status if status else "正常"
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

    # ========== 飞书记录 → 本地数据（整行） ==========

    def _feishu_record_to_local_collection(self, record):
        fields = record.get("fields", {})
        share = self._parse_text_value(fields.get("分享码", ""))
        if not share.strip():
            return None
        data = {
            "share_code": share,
            "平台": self._parse_text_value(fields.get("平台", "")),
            "等级": self._safe_int(fields.get("等级", 3), 3),
            "解析状态": self._parse_text_value(fields.get("解析状态")) or "待解析",
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
            data["获取状态"] = self._parse_text_value(fields.get("获取状态")) or "待获取"
        return data

    def _feishu_record_to_local_cookie(self, record):
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

    # ========== 行级比较（v4 核心） ==========

    def _row_push_fields(self, db_table: str, local: dict, feishu_fields: dict) -> dict:
        """本地行 → 待推送的飞书整行字段（含空值显式清空）。

        - build_fn 产出非空字段
        - 飞书有值而本地为空的"可清空字段"，显式补 ""/[] 让飞书清掉
          （修复 v3"字段清空永远无法同步"的问题）
        """
        fields = getattr(self, self.TABLE_CONFIG[db_table]["build_fields"])(local)
        for k in self._FEISHU_CLEAR_TEXT:
            if k not in fields and self._parse_text_value(feishu_fields.get(k, "")).strip():
                fields[k] = ""
        for k in self._FEISHU_CLEAR_MULTI:
            if k not in fields and self._normalize_tags(feishu_fields.get(k)):
                fields[k] = []
        return fields

    def _field_equal(self, key: str, local_val, feishu_val) -> bool:
        """单个字段等价比较（本地已序列化成飞书形状后比较）"""
        if key == "标签":
            lt = set(map(str, self._normalize_tags(local_val) or []))
            ft = set(map(str, self._normalize_tags(feishu_val) or []))
            return lt == ft
        if key == "验证时间":
            return self._safe_int(local_val) == self._safe_int(feishu_val)
        if key in ("等级", "粉丝数", "作品数"):
            return self._safe_int(local_val) == self._safe_int(feishu_val)
        if key == "启用":
            return self._safe_bool(local_val) == self._safe_bool(feishu_val)
        # 其余（含 URL / 文本 / 单选）：统一转文本去空格比较
        return self._parse_text_value(local_val).strip() == self._parse_text_value(feishu_val).strip()

    def _fields_equal(self, local_fields: dict, feishu_fields: dict) -> bool:
        """整行等价比较（排除同步动作自维护字段）"""
        keys = (set(local_fields) | set(feishu_fields)) - _SYNC_EXCLUDED_FIELDS
        for k in keys:
            if k not in local_fields:
                # 本地没有且不可显式清空的字段（URL/日期/数值）：跳过，
                # 避免因"无法表示空值"造成的永远不等与反复推送
                if k in self._FEISHU_CLEAR_TEXT or k in self._FEISHU_CLEAR_MULTI:
                    return False
                continue
            if not self._field_equal(k, local_fields.get(k), feishu_fields.get(k)):
                return False
        return True

    def _row_pull_data(self, db_table: str, feishu_record: dict) -> dict:
        """飞书行 → 本地整行数据（含清空），用于飞书赢时整行写回本地"""
        data = getattr(self, self.TABLE_CONFIG[db_table]["from_feishu"])(feishu_record) or {}
        for k in self._LOCAL_CLEARABLE.get(db_table, []):
            if k not in data:
                data[k] = None
        return data

    # ========== 同步核心：单表双向（v4） ==========

    def _empty_result(self) -> dict:
        return {"created": 0, "updated": 0, "deleted": 0,
                "skipped_uptodate": 0, "skipped_duplicate": 0, "skipped_invalid": 0,
                "failed": 0, "errors": []}

    def _sync_table(self, db_table: str) -> dict:
        """单表双向同步（行级 LWW + 映射表）

        流程：
        1. 拉飞书全表（一次）+ 读本地全表（一次）+ 读映射表（一次）
        2. 逐行 LWW：相等→跳过；一方改→该方赢；双方都改→时间戳新者赢
        3. 删除传播（双向，带 50% 比例保护）+ 映射表 GC
        """
        result = self._empty_result()
        cfg = self.TABLE_CONFIG.get(db_table)
        if not cfg:
            return result
        label = cfg["label"]
        table_id = self._get_table_id(db_table)
        if not table_id:
            result["errors"].append(f"{label} 未配置 table_id")
            return result

        business_key = self.BUSINESS_KEYS[db_table]
        feishu_bk = self.FEISHU_BUSINESS_KEYS.get(db_table, business_key)
        build_fn = getattr(self, cfg["build_fields"])
        convert_fn = getattr(self, cfg["from_feishu"])
        get_all_fn = getattr(self.db, cfg["get_all_local"])
        insert_fn = getattr(self.db, cfg["insert"])
        update_fn = getattr(self.db, cfg["update"])

        # 自愈：确保「修改时间」等必需字段存在（幂等；失败不阻断同步）
        table_kind = {"share_cache": "collection", "account_cache": "account",
                      "cookie_cache": "cookie"}[db_table]
        try:
            self.feishu.ensure_fields(self.app_token, table_id, table_kind)
        except Exception as e:
            logger.warning(f"{label} ensure_fields 失败（不阻断同步）: {e}")

        try:
            feishu_records = self.feishu.get_all_records(self.app_token, table_id)
        except Exception as e:
            result["errors"].append(f"{label} 拉取飞书失败: {e}")
            result["failed"] += 1
            logger.exception(f"{db_table} 拉取飞书失败")
            return result

        # --- 索引 ---
        feishu_by_key = {}
        feishu_ids = set()
        for r in feishu_records:
            rid = r.get("record_id", "")
            feishu_ids.add(rid)
            kv = self._parse_text_value(r.get("fields", {}).get(feishu_bk, ""))
            if kv:
                if kv in feishu_by_key:
                    result["skipped_duplicate"] += 1
                    logger.warning(f"{db_table} 飞书端重复业务键 {feishu_bk}={kv}，跳过多余记录")
                else:
                    feishu_by_key[kv] = r
            else:
                result["skipped_invalid"] += 1

        local_rows = get_all_fn()
        local_by_key = {}
        for row in local_rows:
            kv = str(row.get(business_key) or "")
            if kv:
                local_by_key[kv] = row
            else:
                result["skipped_invalid"] += 1

        mapping = self.db.get_sync_map(db_table)

        to_create = []       # (local, fields)
        to_update = []       # (rid, fields, key, local)
        delete_local = []    # (key, local)  飞书删了 → 删本地
        delete_feishu = []   # (key, rid)    本地删了 → 删飞书
        map_bind = []        # (key, rid, f_ts, l_ts)  内容一致时的映射刷新
        new_local_rows = []  # 飞书新建 → 插入本地

        # --- Pass 1: 遍历本地行 ---
        for key, local in local_by_key.items():
            f_rec = feishu_by_key.get(key)
            m = mapping.get(key)
            local_ts = self._parse_local_timestamp(local.get("local_updated_at"))

            if f_rec is None:
                if m:
                    # 曾同步过（映射在）而飞书已无此记录 → 飞书端删除 → 删本地
                    delete_local.append((key, local))
                else:
                    # 本地新建，从未同步 → 推送到飞书
                    fields = build_fn(local)
                    if fields:
                        to_create.append((local, fields))
                    else:
                        result["skipped_invalid"] += 1
                continue

            rid = f_rec.get("record_id", "")
            f_ts = self._get_feishu_timestamp(f_rec)
            feishu_fields = f_rec.get("fields", {})
            push_fields = self._row_push_fields(db_table, local, feishu_fields)

            if self._fields_equal(push_fields, feishu_fields):
                # 内容一致：只刷新映射（record_id 可能因键匹配而变化）
                map_bind.append((key, rid, f_ts, local_ts))
                result["skipped_uptodate"] += 1
                if local.get("record_id") != rid and local.get("record_id"):
                    try:
                        update_fn(local["record_id"], {"record_id": rid, "synced": True,
                                                       "local_updated_at": local.get("local_updated_at")})
                    except Exception:
                        pass
                continue

            feishu_changed = (m is None) or (f_ts != m.get("feishu_ts", 0))
            local_changed = (m is None) or (local_ts != m.get("local_ts", 0))

            if feishu_changed and local_changed:
                # 双方都改过 → LWW：时间戳新者赢（相等时飞书赢，保守）
                winner = "local" if local_ts > f_ts else "feishu"
            elif local_changed:
                winner = "local"
            elif feishu_changed:
                winner = "feishu"
            else:
                # 映射时间戳一致但内容不一致：上次推送/回写中途失败的残局，
                # 以本地权威重推一次自愈
                winner = "local"

            if winner == "local":
                if local_ts > 0 or f_ts > 0 or m is None:
                    to_update.append((rid, push_fields, key, local))
                else:
                    result["skipped_invalid"] += 1
            else:
                # 飞书赢 → 整行写回本地（含清空），local_updated_at 对齐飞书
                pull = self._row_pull_data(db_table, f_rec)
                pull["record_id"] = rid
                pull["synced"] = True
                if f_ts:
                    pull["local_updated_at"] = self._ms_to_local_str(f_ts)
                oid = local.get("record_id", "") or rid
                try:
                    if oid:
                        update_fn(oid, pull)
                        result["updated"] += 1
                        self.db.upsert_sync_map(
                            db_table, key, rid,
                            f_ts, self._ms_to_local_str_ms(f_ts),
                        )
                    else:
                        result["skipped_invalid"] += 1
                except Exception as e:
                    result["failed"] += 1
                    result["errors"].append(f"{label} {key}: {e}")
                    logger.warning(f"{db_table} 回写本地失败 {key}: {e}")

        # --- Pass 2: 遍历飞书记录（本地没有的） ---
        for r in feishu_records:
            rid = r.get("record_id", "")
            kv = self._parse_text_value(r.get("fields", {}).get(feishu_bk, ""))
            if not kv or kv in local_by_key:
                continue
            if kv in mapping:
                # 映射在、本地行没了 → 本地删除 → 传播到飞书
                delete_feishu.append((kv, rid))
                continue
            # 飞书新建 → 插入本地
            f_ts = self._get_feishu_timestamp(r)
            data = convert_fn(r)
            if not data:
                result["skipped_invalid"] += 1
                continue
            data["record_id"] = rid
            data["synced"] = True
            if f_ts:
                data["local_updated_at"] = self._ms_to_local_str(f_ts)
            new_local_rows.append((kv, data, rid, f_ts))

        # --- 删除传播（双向，各带比例保护；小表（映射<4行）不设防，避免单条删除永远被拦） ---
        mapped_total = max(len(mapping), 1)
        guard_on = len(mapping) >= 4

        if delete_local:
            if guard_on and len(delete_local) > mapped_total * self.DELETE_SAFETY_RATIO:
                result["skipped_invalid"] += len(delete_local)
                msg = (f"{label} 安全保护：飞书侧疑似批量删除（{len(delete_local)} 条 > "
                       f"映射 {mapped_total} 条的 {self.DELETE_SAFETY_RATIO:.0%}），跳过删除本地")
                result["errors"].append(msg)
                logger.warning(msg)
            else:
                for key, local in delete_local:
                    try:
                        oid = local.get("record_id", "")
                        if oid:
                            self.db.hard_delete(db_table, oid)
                        result["deleted"] += 1
                        self.db.delete_sync_map(db_table, key)
                    except Exception as e:
                        result["failed"] += 1
                        result["errors"].append(f"{label} 删除本地 {key}: {e}")

        if delete_feishu:
            if guard_on and len(delete_feishu) > mapped_total * self.DELETE_SAFETY_RATIO:
                result["skipped_invalid"] += len(delete_feishu)
                msg = (f"{label} 安全保护：本地侧疑似批量删除（{len(delete_feishu)} 条），"
                       f"跳过删除飞书")
                result["errors"].append(msg)
                logger.warning(msg)
            else:
                rids = [rid for _, rid in delete_feishu if rid]
                ok = True
                for i in range(0, len(rids), 500):
                    try:
                        resp = self.feishu.batch_delete_records(self.app_token, table_id, rids[i:i + 500])
                        if resp.get("code") != 0:
                            ok = False
                            result["failed"] += len(rids[i:i + 500])
                            result["errors"].append(f"{label} 删除飞书失败: {resp.get('msg', '')}")
                    except Exception as e:
                        ok = False
                        result["failed"] += len(rids[i:i + 500])
                        result["errors"].append(f"{label} 删除飞书异常: {e}")
                if ok:
                    result["deleted"] += len(rids)
                    for key, _ in delete_feishu:
                        self.db.delete_sync_map(db_table, key)

        # --- 新建：本地 → 飞书（批量创建） ---
        for i in range(0, len(to_create), 500):
            batch = to_create[i:i + 500]
            try:
                payload = [{"fields": b[1]} for b in batch]
                resp = self.feishu.batch_create_records(self.app_token, table_id, payload)
                if resp.get("code") == 0:
                    result["created"] += len(batch)
                    recs = resp.get("data", {}).get("records", [])
                    now_ms = int(_time.time() * 1000)
                    for j, rec in enumerate(recs):
                        if j >= len(batch):
                            break
                        local, fields = batch[j]
                        key = str(local.get(business_key) or "")
                        nid = rec.get("record_id", "")
                        lts = self._parse_local_timestamp(local.get("local_updated_at"))
                        self.db.upsert_sync_map(db_table, key, nid, now_ms, lts)
                        oid = local.get("record_id", "")
                        if nid and oid:
                            try:
                                update_fn(oid, {"record_id": nid, "synced": True,
                                                "local_updated_at": local.get("local_updated_at")
                                                or self._ms_to_local_str(now_ms)})
                            except Exception as e:
                                logger.warning(f"更新本地 record_id 失败 {oid}→{nid}: {e}")
                else:
                    result["failed"] += len(batch)
                    result["errors"].append(f"{label} 批量创建失败: {resp.get('msg', '')}")
            except Exception as e:
                result["failed"] += len(batch)
                result["errors"].append(f"{label} 创建异常: {e}")

        # --- 更新：本地 → 飞书（批量整行覆盖） ---
        for i in range(0, len(to_update), 500):
            batch = to_update[i:i + 500]
            try:
                payload = [{"record_id": b[0], "fields": b[1]} for b in batch if b[0]]
                resp = self.feishu.batch_update_records(self.app_token, table_id, payload)
                if resp.get("code") == 0:
                    result["updated"] += len(payload)
                    now_ms = int(_time.time() * 1000)
                    for rid, _fields, key, local in batch:
                        lts = self._parse_local_timestamp(local.get("local_updated_at"))
                        self.db.upsert_sync_map(db_table, key, rid, now_ms, lts)
                        oid = local.get("record_id", "")
                        if oid:
                            try:
                                update_fn(oid, {"synced": True,
                                                "local_updated_at": local.get("local_updated_at")
                                                or self._ms_to_local_str(now_ms)})
                            except Exception:
                                pass
                else:
                    result["failed"] += len(payload)
                    result["errors"].append(f"{label} 批量更新失败: {resp.get('msg', '')}")
            except Exception as e:
                result["failed"] += len(payload)
                result["errors"].append(f"{label} 更新异常: {e}")

        # --- 新建：飞书 → 本地 ---
        for key, data, rid, f_ts in new_local_rows:
            try:
                insert_fn(data)
                result["created"] += 1
                self.db.upsert_sync_map(
                    db_table, key, rid, f_ts,
                    self._parse_local_timestamp(data.get("local_updated_at")),
                )
            except sqlite3.IntegrityError:
                result["skipped_duplicate"] += 1
            except Exception as e:
                result["failed"] += 1
                result["errors"].append(f"{label} {key}: {e}")

        # --- 映射刷新（内容一致） ---
        for key, rid, f_ts, l_ts in map_bind:
            self.db.upsert_sync_map(db_table, key, rid, f_ts, l_ts)

        # --- 映射 GC：两端都不存在了就清掉 ---
        for key, m in mapping.items():
            if key not in local_by_key and m.get("record_id") not in feishu_ids:
                self.db.delete_sync_map(db_table, key)

        return result

    @staticmethod
    def _ms_to_local_str_ms(ms: int) -> int:
        """毫秒时间戳取整到秒精度（与 local_updated_at 字符串往返一致）"""
        return int(ms) // 1000 * 1000

    # ========== 公开入口：单表同步（兼容 v3 签名） ==========

    def sync_collection_to_feishu(self) -> dict:
        return self._sync_table("share_cache")

    def sync_account_to_feishu(self) -> dict:
        return self._sync_table("account_cache")

    def sync_cookie_to_feishu(self) -> dict:
        return self._sync_table("cookie_cache")

    def sync_collection_from_feishu(self) -> dict:
        return self._sync_table("share_cache")

    def sync_account_from_feishu(self) -> dict:
        return self._sync_table("account_cache")

    def sync_cookie_from_feishu(self) -> dict:
        return self._sync_table("cookie_cache")

    # ========== 公开入口：增量同步（3 表双向） ==========

    def _record_sync_history(self, all_results: dict, started: datetime, trigger: str = "manual") -> None:
        """把一次飞书同步的合并结果写进任务历史（best-effort，绝不影响同步主流程）"""
        try:
            total = success = failed = 0
            errors: list[str] = []
            for label, r in (all_results or {}).items():
                r = r or {}
                f = int(r.get("failed") or 0)
                c = int(r.get("created") or 0)
                u = int(r.get("updated") or 0)
                d = int(r.get("deleted") or 0)
                failed += f
                success += c + u + d
                total += c + u + d + f
                for err in (r.get("errors") or [])[:1]:
                    errors.append(f"{label}: {err}")
            finished = datetime.now()
            self.db.add_sync_history({
                "task_type": "feishu_sync",
                "status": "failed" if failed else "done",
                "total": total,
                "success": success,
                "failed": failed,
                "skipped": 0,
                "error": "；".join(errors)[:500],
                "started_at": started.strftime("%Y-%m-%d %H:%M:%S"),
                "finished_at": finished.strftime("%Y-%m-%d %H:%M:%S"),
                "duration_sec": round((finished - started).total_seconds(), 1),
                "trigger_source": trigger,
            })
        except Exception:
            pass  # 留痕失败不影响同步

    def sync_incremental(self, trigger: str = "manual") -> dict:
        """增量双向同步（3 表，每表一次全量拉取 + 行级 LWW）

        返回 {label: result} 形式的合并结果。
        """
        started = datetime.now()
        all_results = {}
        for db_table in ("share_cache", "account_cache", "cookie_cache"):
            cfg = self.TABLE_CONFIG[db_table]
            label = f"双向同步：{cfg['label']}"
            try:
                all_results[label] = self._sync_table(db_table)
            except Exception as e:
                all_results[label] = {"failed": 1, "errors": [str(e)]}
        self._record_sync_history(all_results, started, trigger)
        return all_results

    def get_incremental_steps(self) -> list:
        """获取同步的 3 个独立步骤（用于后台任务进度展示）

        返回 [(label, callable), ...]，callable 返回单表结果 dict
        """
        return [
            ("双向同步：分享表", self.sync_collection_to_feishu),
            ("双向同步：账号表", self.sync_account_to_feishu),
            ("双向同步：Cookie表", self.sync_cookie_to_feishu),
        ]

    def get_full_steps(self, direction: str) -> list:
        """获取全盘覆盖的步骤列表（用于 SSE 进度展示）

        direction: "to-feishu"（以本地覆盖云端） | "from-feishu"（以云端覆盖本地）
        v4：按键差异覆盖（不再清空重建，record_id 稳定，中途失败不出空表）
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

    # ========== 公开入口：全盘覆盖（按键差异，不再清空重建） ==========

    def sync_full_to_feishu(self) -> dict:
        """以本地为基准覆盖飞书：本地全部推送（按键建/改），飞书多余的删除"""
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
        """以飞书为基准覆盖本地：飞书全部写回（按键建/改），本地多余的删除"""
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
        """以本地为基准覆盖飞书单表（按键差异：建/改/删，不清空重建）"""
        result = self._empty_result()
        cfg = self.TABLE_CONFIG.get(db_table)
        if not cfg:
            return result
        table_id = self._get_table_id(db_table)
        if not table_id:
            result["errors"].append(f"{cfg['label']} 未配置 table_id")
            return result

        business_key = self.BUSINESS_KEYS[db_table]
        feishu_bk = self.FEISHU_BUSINESS_KEYS.get(db_table, business_key)
        build_fn = getattr(self, cfg["build_fields"])
        get_all_fn = getattr(self.db, cfg["get_all_local"])
        update_fn = getattr(self.db, cfg["update"])

        try:
            feishu_records = self.feishu.get_all_records(self.app_token, table_id)
            feishu_by_key = {}
            feishu_ids = set()
            for r in feishu_records:
                feishu_ids.add(r.get("record_id", ""))
                kv = self._parse_text_value(r.get("fields", {}).get(feishu_bk, ""))
                if kv and kv not in feishu_by_key:
                    feishu_by_key[kv] = r

            local_rows = get_all_fn()
            local_keys = set()
            to_create, to_update = [], []
            for local in local_rows:
                kv = str(local.get(business_key) or "")
                if not kv:
                    result["skipped_invalid"] += 1
                    continue
                local_keys.add(kv)
                f_rec = feishu_by_key.get(kv)
                if f_rec:
                    to_update.append((f_rec.get("record_id", ""), build_fn(local), kv, local))
                else:
                    to_create.append((local, build_fn(local)))

            # 飞书多余（本地没有该键）→ 删除
            orphans = [
                (self._parse_text_value(r.get("fields", {}).get(feishu_bk, "")), r.get("record_id", ""))
                for r in feishu_records
                if self._parse_text_value(r.get("fields", {}).get(feishu_bk, ""))
                and self._parse_text_value(r.get("fields", {}).get(feishu_bk, "")) not in local_keys
            ]
            rids = [rid for _, rid in orphans if rid]
            for i in range(0, len(rids), 500):
                try:
                    resp = self.feishu.batch_delete_records(self.app_token, table_id, rids[i:i + 500])
                    if resp.get("code") == 0:
                        result["deleted"] += len(rids[i:i + 500])
                        for kv, _ in orphans[i:i + 500]:
                            self.db.delete_sync_map(db_table, kv)
                    else:
                        result["failed"] += len(rids[i:i + 500])
                        result["errors"].append(f"{cfg['label']} 删除多余记录失败: {resp.get('msg', '')}")
                except Exception as e:
                    result["failed"] += len(rids[i:i + 500])
                    result["errors"].append(f"{cfg['label']} 删除多余记录异常: {e}")

            # 批量创建
            now_ms = int(_time.time() * 1000)
            for i in range(0, len(to_create), 500):
                batch = to_create[i:i + 500]
                try:
                    payload = [{"fields": b[1]} for b in batch]
                    resp = self.feishu.batch_create_records(self.app_token, table_id, payload)
                    if resp.get("code") == 0:
                        result["created"] += len(batch)
                        recs = resp.get("data", {}).get("records", [])
                        for j, rec in enumerate(recs):
                            if j >= len(batch):
                                break
                            local, _fields = batch[j]
                            key = str(local.get(business_key) or "")
                            nid = rec.get("record_id", "")
                            self.db.upsert_sync_map(
                                db_table, key, nid, now_ms,
                                self._parse_local_timestamp(local.get("local_updated_at")),
                            )
                            oid = local.get("record_id", "")
                            if nid and oid:
                                try:
                                    update_fn(oid, {"record_id": nid, "synced": True,
                                                    "local_updated_at": local.get("local_updated_at")
                                                    or self._ms_to_local_str(now_ms)})
                                except Exception:
                                    pass
                    else:
                        result["failed"] += len(batch)
                        result["errors"].append(f"{cfg['label']} 创建失败: {resp.get('msg', '')}")
                except Exception as e:
                    result["failed"] += len(batch)
                    result["errors"].append(f"{cfg['label']} 创建异常: {e}")

            # 批量更新（整行覆盖，不看时间戳）
            for i in range(0, len(to_update), 500):
                batch = to_update[i:i + 500]
                try:
                    payload = [{"record_id": b[0], "fields": b[1]} for b in batch if b[0]]
                    resp = self.feishu.batch_update_records(self.app_token, table_id, payload)
                    if resp.get("code") == 0:
                        result["updated"] += len(payload)
                        for rid, _fields, key, local in batch:
                            self.db.upsert_sync_map(
                                db_table, key, rid, now_ms,
                                self._parse_local_timestamp(local.get("local_updated_at")),
                            )
                            oid = local.get("record_id", "")
                            if oid:
                                try:
                                    update_fn(oid, {"synced": True,
                                                    "local_updated_at": local.get("local_updated_at")
                                                    or self._ms_to_local_str(now_ms)})
                                except Exception:
                                    pass
                    else:
                        result["failed"] += len(payload)
                        result["errors"].append(f"{cfg['label']} 更新失败: {resp.get('msg', '')}")
                except Exception as e:
                    result["failed"] += len(payload)
                    result["errors"].append(f"{cfg['label']} 更新异常: {e}")
        except Exception as e:
            result["errors"].append(f"{db_table} 全盘推送异常: {e}")
            logger.exception(f"{db_table} 全盘推送失败")
        return result

    def _full_from_feishu_single(self, db_table: str) -> dict:
        """以飞书为基准覆盖本地单表（按键差异：建/改/删，不清空重建）"""
        result = self._empty_result()
        cfg = self.TABLE_CONFIG.get(db_table)
        if not cfg:
            return result
        table_id = self._get_table_id(db_table)
        if not table_id:
            result["errors"].append(f"{cfg['label']} 未配置 table_id")
            return result

        business_key = self.BUSINESS_KEYS[db_table]
        feishu_bk = self.FEISHU_BUSINESS_KEYS.get(db_table, business_key)
        convert_fn = getattr(self, cfg["from_feishu"])
        get_all_fn = getattr(self.db, cfg["get_all_local"])
        insert_fn = getattr(self.db, cfg["insert"])
        update_fn = getattr(self.db, cfg["update"])

        try:
            feishu_records = self.feishu.get_all_records(self.app_token, table_id)
            feishu_by_key = {}
            for r in feishu_records:
                kv = self._parse_text_value(r.get("fields", {}).get(feishu_bk, ""))
                if kv and kv not in feishu_by_key:
                    feishu_by_key[kv] = r

            now_ms = int(_time.time() * 1000)
            local_keys = set()
            for r in feishu_records:
                rid = r.get("record_id", "")
                kv = self._parse_text_value(r.get("fields", {}).get(feishu_bk, ""))
                if not kv:
                    result["skipped_invalid"] += 1
                    continue
                data = convert_fn(r)
                if not data:
                    result["skipped_invalid"] += 1
                    continue
                f_ts = self._get_feishu_timestamp(r)
                data["record_id"] = rid
                data["synced"] = True
                if f_ts:
                    data["local_updated_at"] = self._ms_to_local_str(f_ts)
                existing = None
                try:
                    biz = cfg["get_by_business_key"]
                    if biz == "_get_cookie_by_value":
                        existing = self._get_cookie_by_value(kv)
                    else:
                        existing = getattr(self.db, biz)(kv)
                except Exception:
                    existing = None
                if existing:
                    local_keys.add(kv)
                    oid = existing.get("record_id", "") or rid
                    try:
                        update_fn(oid, data)
                        result["updated"] += 1
                        self.db.upsert_sync_map(
                            db_table, kv, rid,
                            f_ts, self._parse_local_timestamp(data.get("local_updated_at")),
                        )
                    except Exception as e:
                        result["failed"] += 1
                        result["errors"].append(f"{cfg['label']} {kv}: {e}")
                else:
                    try:
                        insert_fn(data)
                        result["created"] += 1
                        self.db.upsert_sync_map(
                            db_table, kv, rid,
                            f_ts, self._parse_local_timestamp(data.get("local_updated_at")),
                        )
                    except sqlite3.IntegrityError:
                        result["skipped_duplicate"] += 1
                    except Exception as e:
                        result["failed"] += 1
                        result["errors"].append(f"{cfg['label']} {kv}: {e}")

            # 本地多余（飞书没有该键）→ 硬删除 + 清映射
            for local in get_all_fn():
                kv = str(local.get(business_key) or "")
                if not kv or kv in feishu_by_key or kv in local_keys:
                    continue
                oid = local.get("record_id", "")
                if oid:
                    self.db.hard_delete(db_table, oid)
                self.db.delete_sync_map(db_table, kv)
                result["deleted"] += 1
        except Exception as e:
            result["errors"].append(f"{db_table} 全盘拉取异常: {e}")
            logger.exception(f"{db_table} 全盘拉取失败")
        return result
