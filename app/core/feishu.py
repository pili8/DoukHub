"""飞书开放 API 交互模块"""
import time
from typing import Any

import httpx

FEISHU_BASE = "https://open.feishu.cn/open-apis"


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
        return resp.json()

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
        return resp.json()

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
        resp.raise_for_status()
        return resp.json()

    @staticmethod
    def _get_required_fields(table_type: str) -> list[tuple]:
        """获取不同表类型的必需字段定义

        field_type: 1=文本, 2=数字, 3=单选, 4=多选, 5=日期, 7=复选框, 15=URL
        """
        if table_type == "collection":
            # 采集表：原始数据源
            return [
                ("地址", 1, None),             # 文本，可能是短链缩略路径如 m3HL2u1R1YM
                ("等级", 2, None),             # 数字 1-4
                ("标签", 4, None),             # 多选标签
                ("账号名称", 1, None),         # 可选，不填则自动获取
                ("平台", 3, {"options": [{"name": "抖音"}, {"name": "TikTok"}, {"name": "小红书"}]}),
                ("备注", 1, None),
                ("sec_user_id", 1, None),      # 自动回填
                ("昵称", 1, None),             # 自动回填
                ("粉丝数", 2, None),           # 自动回填
                ("作品数", 2, None),           # 自动回填
                ("签名", 1, None),             # 自动回填
                ("头像", 1, None),             # 自动回填（文本存 URL）
                ("同步状态", 3, {"options": [{"name": "待同步"}, {"name": "已同步"}, {"name": "失败"}]}),
                ("同步时间", 5, None),         # 自动回填
            ]
        elif table_type == "cookie":
            # Cookie 表
            return [
                ("Cookie", 1, None),           # Cookie 字符串
                ("平台", 3, {"options": [{"name": "抖音"}, {"name": "TikTok"}, {"name": "小红书"}, {"name": "通用"}]}),
                ("状态", 3, {"options": [{"name": "正常"}, {"name": "失效"}]}),
                ("启用", 7, None),             # 是否参与轮换
                ("备注", 1, None),
                ("最后验证时间", 5, None),
            ]
        else:
            # 账号表（默认）
            return [
                ("账号名称", 1, None),
                ("平台", 3, {"options": [{"name": "抖音"}, {"name": "TikTok"}, {"name": "小红书"}]}),
                ("链接", 15, None),
                ("等级", 2, None),             # 数字 1-4
                ("标签", 4, None),
                ("启用", 7, None),
                ("采集类型", 3, {"options": [{"name": "发布"}, {"name": "喜欢"}, {"name": "收藏"}]}),
                ("代理", 1, None),
                ("备注", 1, None),
                ("sec_user_id", 1, None),
                ("昵称", 1, None),
                ("粉丝数", 2, None),
                ("作品数", 2, None),
                ("签名", 1, None),
                ("头像", 15, None),
                ("同步时间", 5, None),
            ]

    def ensure_fields(self, app_token: str, table_id: str, table_type: str = "account") -> dict:
        """确保表格有所有必需字段，缺少的自动创建

        table_type: "account" | "collection" | "cookie"
        """
        required_fields = self._get_required_fields(table_type)

        existing = self.list_fields(app_token, table_id)
        existing_names = {f["field_name"] for f in existing}

        created = []
        skipped = []
        for name, ftype, opts in required_fields:
            if name in existing_names:
                skipped.append(name)
            else:
                try:
                    self.create_field(app_token, table_id, name, ftype, opts)
                    created.append(name)
                except Exception as e:
                    return {"success": False, "message": f"创建字段 {name} 失败: {e}"}

        return {
            "success": True,
            "message": f"已创建 {len(created)} 个字段，跳过 {len(skipped)} 个已存在字段",
            "created": created,
            "skipped": skipped,
        }
