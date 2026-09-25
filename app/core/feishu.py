"""飞书开放 API 交互模块"""
import logging
import time
from typing import Any

import httpx

FEISHU_BASE = "https://open.feishu.cn/open-apis"
logger = logging.getLogger("doukhub.feishu")


class FeishuClient:
    """飞书 API 客户端"""

    def __init__(self, app_id: str, app_secret: str):
        self.app_id = app_id
        self.app_secret = app_secret
        self._token: str = ""
        self._token_expires_at: float = 0
        self._client = httpx.Client(timeout=30)

    def _ensure_token(self) -> str:
        """确保 access token 有效，过期则刷新"""
        if self._token and time.time() < self._token_expires_at - 60:
            return self._token
        resp = self._client.post(
            f"{FEISHU_BASE}/auth/v3/tenant_access_token/internal",
            json={"app_id": self.app_id, "app_secret": self.app_secret},
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"飞书认证失败: {data.get('msg')}")
        self._token = data["tenant_access_token"]
        self._token_expires_at = time.time() + data.get("expire", 7200)
        return self._token

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._ensure_token()}"}

    def test_connection(self) -> dict:
        """测试飞书连接是否正常"""
        try:
            self._ensure_token()
            return {"success": True, "message": "连接成功"}
        except Exception as e:
            return {"success": False, "message": str(e)}

    def list_records(
        self,
        app_token: str,
        table_id: str,
        page_size: int = 500,
        page_token: str = "",
        filter_expr: str = "",
    ) -> dict:
        """读取多维表格记录"""
        params: dict[str, Any] = {"page_size": page_size}
        if page_token:
            params["page_token"] = page_token
        if filter_expr:
            params["filter"] = filter_expr
        resp = self._client.get(
            f"{FEISHU_BASE}/bitable/v1/apps/{app_token}/tables/{table_id}/records",
            headers=self._headers(),
            params=params,
        )
        resp.raise_for_status()
        return resp.json()

    def get_all_records(self, app_token: str, table_id: str) -> list[dict]:
        """获取多维表格全部记录（自动翻页）"""
        all_items = []
        page_token = ""
        while True:
            data = self.list_records(app_token, table_id, page_token=page_token)
            if data.get("code") != 0:
                raise RuntimeError(f"读取飞书表格失败: {data.get('msg')}")
            items = data.get("data", {}).get("items", [])
            all_items.extend(items)
            if not data.get("data", {}).get("has_more", False):
                break
            page_token = data["data"].get("page_token", "")
        return all_items

    def update_record(
        self,
        app_token: str,
        table_id: str,
        record_id: str,
        fields: dict,
    ) -> dict:
        """更新多维表格记录"""
        resp = self._client.put(
            f"{FEISHU_BASE}/bitable/v1/apps/{app_token}/tables/{table_id}/records/{record_id}",
            headers=self._headers(),
            json={"fields": fields},
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"飞书更新失败: {data.get('msg', '未知错误')} (code={data.get('code')})")
        return data

    def create_record(
        self, app_token: str, table_id: str, fields: dict
    ) -> dict:
        """创建多维表格记录"""
        resp = self._client.post(
            f"{FEISHU_BASE}/bitable/v1/apps/{app_token}/tables/{table_id}/records",
            headers=self._headers(),
            json={"fields": fields},
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"飞书创建失败: {data.get('msg', '未知错误')} (code={data.get('code')})")
        return data

    def batch_create_records(
        self,
        app_token: str,
        table_id: str,
        records: list[dict],
    ) -> dict:
        """批量创建多维表格记录
        records: [{"fields": {...}}, {"fields": {...}}, ...]
        一次最多 500 条
        """
        resp = self._client.post(
            f"{FEISHU_BASE}/bitable/v1/apps/{app_token}/tables/{table_id}/records/batch_create",
            headers=self._headers(),
            json={"records": records},
        )
        resp.raise_for_status()
        return resp.json()

    def delete_record(
        self, app_token: str, table_id: str, record_id: str
    ) -> dict:
        """删除多维表格记录"""
        resp = self._client.delete(
            f"{FEISHU_BASE}/bitable/v1/apps/{app_token}/tables/{table_id}/records/{record_id}",
            headers=self._headers(),
        )
        resp.raise_for_status()
        return resp.json()

    def batch_update_records(
        self,
        app_token: str,
        table_id: str,
        records: list[dict],
    ) -> dict:
        """批量更新多维表格记录
        records: [{"record_id": "xxx", "fields": {...}}, ...]
        """
        resp = self._client.post(
            f"{FEISHU_BASE}/bitable/v1/apps/{app_token}/tables/{table_id}/records/batch_update",
            headers=self._headers(),
            json={"records": records},
        )
        resp.raise_for_status()
        return resp.json()

    def batch_delete_records(
        self,
        app_token: str,
        table_id: str,
        records: list[str],
    ) -> dict:
        """批量删除多维表格记录
        records: ["record_id1", "record_id2", ...]
        一次最多 500 条
        """
        resp = self._client.post(
            f"{FEISHU_BASE}/bitable/v1/apps/{app_token}/tables/{table_id}/records/batch_delete",
            headers=self._headers(),
            json={"records": records},
        )
        resp.raise_for_status()
        return resp.json()

    def close(self):
        self._client.close()

    # --- 字段管理 ---

    def list_fields(self, app_token: str, table_id: str) -> list[dict]:
        """获取多维表格所有字段"""
        resp = self._client.get(
            f"{FEISHU_BASE}/bitable/v1/apps/{app_token}/tables/{table_id}/fields",
            headers=self._headers(),
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"获取字段失败: {data.get('msg')}")
        return data.get("data", {}).get("items", [])

    def create_field(self, app_token: str, table_id: str, field_name: str, field_type: int, options: dict = None) -> dict:
        """创建多维表格字段
        field_type: 1=文本, 2=数字, 3=单选, 4=多选, 5=日期, 7=复选框, 15=URL
        """
        body = {"field_name": field_name, "type": field_type}
        if options:
            body["property"] = options
        resp = self._client.post(
            f"{FEISHU_BASE}/bitable/v1/apps/{app_token}/tables/{table_id}/fields",
            headers=self._headers(),
            json=body,
        )
        try:
            data = resp.json()
        except Exception:
            resp.raise_for_status()
            raise RuntimeError(f"创建字段失败: 响应无法解析 HTTP {resp.status_code}")
        code = data.get("code")
        if resp.status_code >= 400 or code not in self._FIELD_OK_CODES:
            raise RuntimeError(
                f"创建字段失败: HTTP {resp.status_code} code={code} "
                f"msg={data.get('msg')} body={body}"
            )
        return data

    # 更新字段接口可接受的业务码：0=成功，1254606=DataNotChange（值未变化，等价成功）
    _FIELD_OK_CODES = (0, 1254606)

    def update_field(self, app_token: str, table_id: str, field_id: str, body: dict) -> dict:
        """更新多维表格字段定义（重命名字段 / 补选项）

        ⚠️ 飞书 PUT fields 接口**强制要求同时传 field_name 与 type**：
        只传其中任一项都会返回 99992402 field validation failed
        （field_violations: "field_name is required" / "type is required"）。
        且该错误 HTTP 状态是 400、真实业务码在 body 里 —— 必须解析 body 再判断，
        否则只看到 HTTPError 400，错误被静默吞掉。
        """
        resp = self._client.put(
            f"{FEISHU_BASE}/bitable/v1/apps/{app_token}/tables/{table_id}/fields/{field_id}",
            headers=self._headers(),
            json=body,
        )
        try:
            data = resp.json()
        except Exception:
            resp.raise_for_status()
            raise RuntimeError(f"更新字段失败: 响应无法解析 HTTP {resp.status_code}")
        code = data.get("code")
        if resp.status_code >= 400 or code not in self._FIELD_OK_CODES:
            raise RuntimeError(
                f"更新字段失败: HTTP {resp.status_code} code={code} "
                f"msg={data.get('msg')} body={body}"
            )
        return data

    @staticmethod
    def _get_legacy_field_renames(table_type: str) -> list[tuple]:
        """获取旧字段名 → 新字段名的映射（v2 字段对齐）。
        返回 [(old_name, new_name), ...]
        """
        legacy_map = {
            "collection": [
                ("Share", "分享码"),
                ("地址", "分享码"),
                ("同步状态", "解析状态"),  # 旧版叫「同步状态」单选
                ("已同步", "解析状态"),    # v2.2 语义调整
                ("已解析", "解析状态"),    # v3 改为「解析状态」单选枚举
                ("已就绪", "解析状态"),    # v3.1 字段名调整
            ],
            "account": [],  # 账号表 v1 已迁移过
            "cookie": [
                ("最后验证时间", "验证时间"),  # 旧版叫「最后验证时间」，v2 改为「验证时间」
            ],
        }
        return legacy_map.get(table_type, [])

    @staticmethod
    def _get_required_fields(table_type: str) -> list[tuple]:
        """获取不同表类型的必需字段定义

        field_type: 1=文本, 2=数字, 3=单选, 4=多选, 5=日期, 7=复选框, 15=URL,
                    1001=创建时间, 1002=最后更新时间（系统自动维护）
        业务字段名与本地数据库 100% 一致（v2）

        v4（行级 LWW）：三张表统一加「修改时间」字段（类型 1002，飞书自动维护，
        记录级最后修改时间，毫秒）——这是行级 LWW 的时间基准。
        ⚠️ 旧版误用 1001（创建时间，永不变化）建过「最后更新时间」字段，
        该字段已废弃不再读写；ensure_fields 只按名字判存在，旧字段留在表里无害。
        """
        # 飞书「修改时间」系统字段（类型 1002），自动记录最后修改时间（毫秒级）
        common_lww_field = ("修改时间", 1002, None)

        if table_type == "collection":
            # 分享表：原始数据源
            return [
                ("分享码", 1, None),             # 文本，抖音分享码（如 iMLuCKjq）
                ("平台", 3, {"options": [{"name": "douyin"}, {"name": "tiktok"}, {"name": "xhs"}]}),
                ("等级", 2, {"formatter": "0"}),             # 数字 1-4，整数显示（无小数点）
                ("标签", 4, None),             # 多选标签
                ("sec_user_id", 1, None),      # 自动回填
                ("解析状态", 3, {"options": [{"name": "待解析"}, {"name": "已就绪"}, {"name": "已生成"}, {"name": "已删除"}, {"name": "解析失败"}, {"name": "链接失效"}, {"name": "非主页链接"}, {"name": "暂不支持"}]}),  # 单选：状态枚举
                ("备注", 1, None),             # 用户备注 + 合并信息
                ("粉丝数", 2, None),           # 自动回填
                ("作品数", 2, None),           # 自动回填
                ("账号名称", 1, None),         # 可选，不填则自动获取
                ("同步时间", 5, None),         # 自动回填
                common_lww_field,             # 方案 B：LWW 时间戳
            ]
        elif table_type == "cookie":
            # Cookie 表
            return [
                ("Cookie", 1, None),           # Cookie 字符串
                ("平台", 3, {"options": [{"name": "douyin"}, {"name": "tiktok"}, {"name": "xhs"}, {"name": "通用"}]}),
                ("状态", 3, {"options": [{"name": "正常"}, {"name": "失效"}]}),
                ("启用", 7, None),             # 是否参与轮换
                ("备注", 1, None),
                ("验证时间", 5, None),         # 上次验证时间
                ("同步时间", 5, None),         # 自动回填
                common_lww_field,             # 方案 B：LWW 时间戳
            ]
        else:
            # 账号表（默认）
            return [
                ("账号名称", 1, None),
                ("平台", 3, {"options": [{"name": "douyin"}, {"name": "tiktok"}, {"name": "xhs"}]}),
                ("链接", 15, None),
                ("sec_user_id", 1, None),
                ("等级", 2, {"formatter": "0"}),             # 数字 1-4，整数显示（无小数点）
                ("标签", 4, None),
                ("启用", 7, None),
                ("采集类型", 3, {"options": [{"name": "发布"}, {"name": "喜欢"}, {"name": "收藏"}]}),
                ("备注", 1, None),
                ("粉丝数", 2, None),
                ("作品数", 2, None),
                ("签名", 1, None),
                ("头像", 15, None),
                ("获取状态", 3, {"options": [{"name": "待获取"}, {"name": "已获取"}, {"name": "获取失败"}]}),  # 单选：获取状态枚举
                ("同步时间", 5, None),
                common_lww_field,             # 方案 B：LWW 时间戳
            ]

    def ensure_fields(self, app_token: str, table_id: str, table_type: str = "account") -> dict:
        """确保表格有所有必需字段，缺少的自动创建，旧名自动重命名。

        table_type: "account" | "collection" | "cookie"

        步骤：
        1. 检查并重命名旧字段（如 Share → 分享码，同步状态 → 已同步）
        2. 创建缺失的必需字段
        """
        required_fields = self._get_required_fields(table_type)

        existing = self.list_fields(app_token, table_id)
        existing_names = {f["field_name"]: f for f in existing}

        # v2 字段重命名：把旧名改成新名
        renamed = []
        conflicts = []   # 新旧字段名同时存在的（保留原样，仅提示）
        for old_name, new_name in self._get_legacy_field_renames(table_type):
            if old_name in existing_names and new_name not in existing_names:
                field_id = existing_names[old_name]["field_id"]
                cur_type = existing_names[old_name].get("type")
                if cur_type is None:
                    logger.warning(f"字段 {old_name} 缺少 type 信息，跳过重命名 {old_name}→{new_name}")
                    continue
                try:
                    # 飞书改字段名必须带 type，否则 99992402 field validation failed
                    self.update_field(app_token, table_id, field_id,
                                      {"field_name": new_name, "type": cur_type})
                    renamed.append(f"{old_name}→{new_name}")
                    # 更新本地索引
                    existing_names[new_name] = existing_names.pop(old_name)
                    existing_names[new_name]["field_name"] = new_name
                except Exception as e:
                    # 重命名失败不阻断，继续创建新字段
                    logger.warning(f"重命名字段 {old_name}→{new_name} 失败: {e}")
            elif old_name in existing_names and new_name in existing_names:
                # 新旧字段名在云端同时存在：目标名已被占用，**没有冲突需要"让位"**。
                # ⚠️ 这里原先会把旧字段自动改名为 <old>_legacy —— 那属于擅自改动用户的表结构，
                # 飞书里若有视图/公式引用该字段就会失效。改为保留原样、只记录警告，
                # 由用户自行决定是否清理。（改名的写法则已修正为必须带 type，见 update_field。）
                conflicts.append(old_name)
                logger.warning(
                    f"字段「{old_name}」与「{new_name}」在云端同时存在，已保留原样未改动；"
                    f"如需清理请手动在飞书表中处理"
                )

        # 创建缺失字段
        created = []
        skipped = []
        options_added = []
        lww_field_warning = ""
        for name, ftype, opts in required_fields:
            if name in existing_names:
                skipped.append(name)
                # 已存在的单选/多选字段：把期望里多出来的枚举值补进去。
                # 之前只建字段、不改选项，导致「解析状态」新增的取值在飞书侧写不进去。
                if opts and ftype in (3, 4):
                    try:
                        added = self._sync_field_options(
                            app_token, table_id, existing_names[name], opts
                        )
                        if added:
                            options_added.append(f"{name}(+{'/'.join(added)})")
                    except Exception as e:
                        # 补选项失败不阻断同步：老选项仍可用，只是新状态值会写失败
                        logger.warning(f"补齐字段「{name}」选项失败（不影响同步）: {e}")
            else:
                try:
                    self.create_field(app_token, table_id, name, ftype, opts)
                    created.append(name)
                except Exception as e:
                    # v4：「修改时间」（类型 1002）是飞书系统字段，
                    # API 可能不支持通过 create_field 创建。失败时不阻断同步，
                    # 提示用户手动在飞书表里添加。
                    if ftype == 1002:
                        lww_field_warning = (
                            f"⚠️ 无法自动创建「修改时间」字段（飞书 API 可能不支持创建系统字段）。"
                            f"请手动在飞书表里添加「修改时间」字段（类型：最后更新时间/修改时间），"
                            f"否则飞书端的改动将无法同步回本地。"
                        )
                        logger.warning(lww_field_warning)
                    else:
                        return {"success": False, "message": f"创建字段 {name} 失败: {e}"}

        msg_parts = []
        if created:
            msg_parts.append(f"创建 {len(created)} 个字段")
        if skipped:
            msg_parts.append(f"跳过 {len(skipped)} 个已存在")
        if renamed:
            msg_parts.append(f"重命名 {len(renamed)} 个")
        if conflicts:
            msg_parts.append(f"字段名并存未改动 {len(conflicts)} 个（{'、'.join(conflicts)}）")
        if options_added:
            msg_parts.append(f"补充选项：{'、'.join(options_added)}")
        if lww_field_warning:
            msg_parts.append(lww_field_warning)

        return {
            "success": True,
            "message": "，".join(msg_parts) if msg_parts else "字段齐全",
            "created": created,
            "skipped": skipped,
            "renamed": renamed,
            "field_conflicts": conflicts,
            "options_added": options_added,
        }

    def _sync_field_options(
        self, app_token: str, table_id: str, field: dict, options: dict
    ) -> list[str]:
        """把缺失的单选项补进已有字段，返回本次新增的选项名列表。

        飞书更新 property.options 是**整体替换**：必须把现有选项（连同 id）原样回传，
        只有新选项才只给 name。若不带 id，飞书会把旧选项当成新选项重建，
        引用这些选项的单元格数据就会错位——所以这里用 dict(o) 原样保留，
        既保住 id，也保住 color 等属性。
        """
        wanted = [o.get("name") for o in (options.get("options") or []) if o.get("name")]
        if not wanted:
            return []

        current = (field.get("property") or {}).get("options") or []
        have = {o.get("name") for o in current if o.get("name")}
        missing = [name for name in wanted if name not in have]
        if not missing:
            return []
        if not field.get("field_name") or field.get("type") is None:
            logger.warning(f"字段「{field.get('field_name')}」缺少 field_name/type，跳过补充选项")
            return []

        prop = dict(field.get("property") or {})
        prop["options"] = [dict(o) for o in current if o.get("name")] + [
            {"name": name} for name in missing
        ]
        # 飞书 PUT fields 强制要求 field_name + type，否则 99992402（只传 property 必然失败）
        self.update_field(app_token, table_id, field["field_id"], {
            "field_name": field.get("field_name"),
            "type": field.get("type"),
            "property": prop,
        })
        logger.info(f"字段「{field.get('field_name')}」补充选项: {missing}")
        return missing
