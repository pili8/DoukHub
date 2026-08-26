"""DoukHub 配置管理模块"""
import json
from pathlib import Path
from typing import Any

# 用户目录下的配置（不会被项目更新覆盖）
USER_CONFIG_DIR = Path.home() / ".doukhub"
USER_CONFIG_FILE = USER_CONFIG_DIR / "config.json"

# 项目目录下的配置（旧位置，用于迁移）
PROJECT_CONFIG_DIR = Path(__file__).resolve().parent.parent.parent / "config"
PROJECT_CONFIG_FILE = PROJECT_CONFIG_DIR / "doukhub.json"

DEFAULT_CONFIG = {
    "feishu": {
        "app_id": "",
        "app_secret": "",
        "app_token": "",
        "collection_table_id": "",   # 分享表
        "account_table_id": "",      # 账号表
        "cookie_table_id": "",       # Cookie 表
    },
    "downloader": {
        "tiktok_downloader_path": "./TikTokDownloader",
        "xhs_downloader_path": "./XHS-Downloader",
        "ttd_port": 5555,
        "xhs_port": 5556,
        "auto_start_services": True,
        "keep_services_alive": True,
    },
    "local": {
        "download_path": "",  # 空字符串表示使用默认路径（运行时根据平台决定）
        "storage_format": "xlsx",
        "data_dir": "./data",
    },
    "cookie": {
        "rotation_mode": "random",
        "usage_limit": 10,
    },
    "tags": {
        "个": "个人",
        "人": "个人",
        "图": "图集",
        "集": "图集",
        "自": "自拍",
        "拍": "自拍",
        "分": "分享",
        "享": "分享",
        "街": "街拍",
        "商": "商业",
        "业": "商业",
        "模": "模特",
        "特": "模特",
        "展": "展会",
        "会": "展会",
        "直": "直播LIVE",
        "播": "直播LIVE",
        "长": "长腿",
        "腿": "长腿",
        "酒": "酒吧",
        "吧": "酒吧",
        "户": "户外",
        "外": "户外",
        "太多": "多",
        "多": "多",
        "南充": "南充",
    },
    "concurrent_accounts": 3,
    "api": {
        "enabled": False,       # 是否启用 API 请求模式
        "api_key": "",          # 专属 API Key（为空时自动生成）
        "default_resolve_mode": "auto",  # 默认解析模式: auto/api/ttd
    },
    "single_work": {
        "download_path": "",
        "recent_dirs": [],
        "default_template_id": "default",
        "templates": [{
            "id": "default",
            "name": "默认模板",
            "template": "{create_time} {author} {title}",
            "is_default": True,
            "created_at": "2026-08-16 00:00:00",
            "updated_at": "2026-08-16 00:00:00",
        }],
    },
    "collection_defaults": {
        "folder_name": "Download",
        "name_format": "create_time type nickname desc",
        "split": "-",
        "date_format": "%Y%m%d_%H%M%S",
        "folder_mode": False,
        "music": False,
        "dynamic_cover": False,
        "static_cover": False,
        "max_size": 0,
        "storage_format": "",
    },
    # 存储方案（2026-08-23 v3）：单作品/增量各自一份方案列表，
    # 每个方案可标记 role: primary(主)/secondary(次)/空(自由指定)，支持任意多个方案
    # choice: "auto"（主→次）| "p:<id>"（指定方案）| "primary" | "secondary"
    "storage_profiles": {
        "single": {
            "default_name_format": "{create_time} {author} {title}",
            "profiles": [],
        },
        "batch": {
            "default_name_format": "create_time type nickname desc",
            "profiles": [],
        },
    },
}


class Config:
    """DoukHub 全局配置管理"""

    def __init__(self, config_path: Path | None = None):
        self._path = config_path or self._resolve_config_path()
        self._data: dict = {}
        self.load()

    @staticmethod
    def _resolve_config_path() -> Path:
        """确定配置文件路径：优先用户目录，其次项目目录"""
        if USER_CONFIG_FILE.exists():
            return USER_CONFIG_FILE
        # 如果项目目录有旧配置，迁移到用户目录
        if PROJECT_CONFIG_FILE.exists():
            USER_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            import shutil
            shutil.copy2(PROJECT_CONFIG_FILE, USER_CONFIG_FILE)
            return USER_CONFIG_FILE
        # 新建配置到用户目录
        return USER_CONFIG_FILE

    def load(self) -> None:
        """加载配置文件，不存在则创建默认配置"""
        if self._path.exists():
            with open(self._path, "r", encoding="utf-8") as f:
                self._data = json.load(f)
            # 补充缺失的默认值
            self._merge_defaults(self._data, DEFAULT_CONFIG)
        else:
            self._data = DEFAULT_CONFIG.copy()
            self.save()

    def _merge_defaults(self, data: dict, defaults: dict) -> None:
        """递归补充缺失的配置项"""
        for key, value in defaults.items():
            if key not in data:
                data[key] = value
            elif isinstance(value, dict) and isinstance(data[key], dict):
                self._merge_defaults(data[key], value)

    def save(self) -> None:
        """保存配置到文件"""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._path, "w", encoding="utf-8") as f:
            json.dump(self._data, f, indent=4, ensure_ascii=False)

    def get(self, dot_path: str, default: Any = None) -> Any:
        """通过点分路径获取配置值，如 'feishu.app_id'"""
        keys = dot_path.split(".")
        value = self._data
        for key in keys:
            if isinstance(value, dict) and key in value:
                value = value[key]
            else:
                return default
        return value

    def set(self, dot_path: str, value: Any) -> None:
        """通过点分路径设置配置值"""
        keys = dot_path.split(".")
        data = self._data
        for key in keys[:-1]:
            if key not in data:
                data[key] = {}
            data = data[key]
        data[keys[-1]] = value

    @property
    def feishu(self) -> dict:
        return self._data.get("feishu", {})

    @property
    def downloader(self) -> dict:
        return self._data.get("downloader", {})

    @property
    def local(self) -> dict:
        return self._data.get("local", {})

    @property
    def cookie_config(self) -> dict:
        return self._data.get("cookie", {})

    @property
    def single_work(self) -> dict:
        return self._data.get("single_work", {})

    @property
    def collection_defaults(self) -> dict:
        """增量采集的全局默认设置（folder_name + name_format + 引擎参数）"""
        return self._data.get("collection_defaults", {
            "folder_name": "Download",
            "name_format": "create_time type nickname desc",
            "split": "-",
            "date_format": "%Y%m%d_%H%M%S",
            "folder_mode": False,
            "music": False,
            "dynamic_cover": False,
            "static_cover": False,
            "max_size": 0,
            "storage_format": "",
        })

    @property
    def concurrent_accounts(self) -> int:
        return self._data.get("concurrent_accounts", 3)

    @property
    def storage_profiles(self) -> dict:
        """存储方案（single/batch 两套有序列表）"""
        return self._data.get("storage_profiles", {
            "single": {"use": "auto", "default_name_format": "{create_time} {author} {title}", "profiles": []},
            "batch": {"use": "auto", "default_name_format": "create_time type nickname desc", "profiles": []},
        })

    @property
    def api_config(self) -> dict:
        return self._data.get("api", {})

    @property
    def api_enabled(self) -> bool:
        return self.api_config.get("enabled", False)

    @property
    def api_key(self) -> str:
        return self.api_config.get("api_key", "")

    @property
    def data_dir(self) -> Path:
        """本地数据目录"""
        path = Path(self.local.get("data_dir", "./data"))
        if not path.is_absolute():
            path = Path(__file__).resolve().parent.parent / path.name
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def download_path(self) -> Path:
        """下载文件存储路径：优先单作品目录，其次本地配置，默认 DoukHub/Download"""
        single_path = self.single_work.get("download_path", "")
        if single_path:
            return Path(single_path)
        path = self.local.get("download_path", "")
        if not path:
            return Path(__file__).resolve().parent.parent.parent / "Download"
        return Path(path)

    @property
    def ttd_path(self) -> str:
        """TikTokDownloader 路径，为空则使用 DoukHub 内的默认路径"""
        path = self.downloader.get("tiktok_downloader_path", "")
        if not path:
            return str(Path(__file__).resolve().parent.parent.parent / "TikTokDownloader")
        return path

    @property
    def xhs_path(self) -> str:
        """XHS-Downloader 路径，为空则使用 DoukHub 内的默认路径"""
        path = self.downloader.get("xhs_downloader_path", "")
        if not path:
            return str(Path(__file__).resolve().parent.parent.parent / "XHS-Downloader")
        return path

    @property
    def ttd_port(self) -> int:
        return self.downloader.get("ttd_port", 5555)

    @property
    def keep_services_alive(self) -> bool:
        """是否持续保持 TTD/XHS 在线（心跳检查 + 自动重启）"""
        return self.downloader.get("keep_services_alive", True)

    @property
    def xhs_port(self) -> int:
        return self.downloader.get("xhs_port", 5556)
