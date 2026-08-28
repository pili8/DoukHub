"""采集方案（预设）管理

方案是"应用按什么规则采集"的配置，属于应用配置的一部分，
随配置一起存在数据库 settings 表（config 的 collection_presets 键）。
对外接口与原 collection_presets 表完全一致，调用方无感。
"""
from datetime import datetime
from typing import Any, Optional

_KEY = "collection_presets"

DEFAULT_PRESETS = [
    {"name": "日常 4星+（增量）", "rating_min": 4, "tags": "", "account_names": "", "platform": "douyin", "mode": "incremental", "is_default": 1},
    {"name": "日常 3星+（增量）", "rating_min": 3, "tags": "", "account_names": "", "platform": "douyin", "mode": "incremental", "is_default": 0},
    {"name": "TikTok 全部（增量）", "rating_min": 3, "tags": "", "account_names": "", "platform": "tiktok", "mode": "incremental", "is_default": 0},
    {"name": "连通性测试", "rating_min": 4, "tags": "", "account_names": "", "platform": "douyin", "mode": "incremental", "is_default": 0},
]


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def list_presets(config) -> list[dict]:
    """返回所有采集方案；首次使用自动初始化默认方案。"""
    presets = config.get(_KEY)
    if not presets:
        presets = _init_defaults(config)
    return presets


def _init_defaults(config) -> list[dict]:
    now = _now()
    presets = [{**p, "id": i + 1, "created_at": now, "updated_at": now}
               for i, p in enumerate(DEFAULT_PRESETS)]
    config.set(_KEY, presets)
    config.save()
    return presets


def get_preset(config, preset_id: int) -> Optional[dict]:
    for p in list_presets(config):
        if p.get("id") == preset_id:
            return p
    return None


def create_preset(config, data: dict) -> dict:
    presets = list_presets(config)
    pid = max((p.get("id", 0) for p in presets), default=0) + 1
    now = _now()
    preset = {**data, "id": pid, "created_at": now, "updated_at": now,
              "is_default": 1 if data.get("is_default") else 0}
    presets.append(preset)
    config.set(_KEY, presets)
    config.save()
    return preset


def update_preset(config, preset_id: int, data: dict) -> Optional[dict]:
    presets = list_presets(config)
    for i, p in enumerate(presets):
        if p.get("id") == preset_id:
            merged = {**p, **data, "id": preset_id, "updated_at": _now()}
            presets[i] = merged
            config.set(_KEY, presets)
            config.save()
            return merged
    return None


def delete_preset(config, preset_id: int) -> bool:
    presets = list_presets(config)
    new = [p for p in presets if p.get("id") != preset_id]
    if len(new) == len(presets):
        return False
    config.set(_KEY, new)
    config.save()
    return True


def set_default_preset(config, preset_id: int) -> bool:
    presets = list_presets(config)
    found = False
    for p in presets:
        if p.get("id") == preset_id:
            p["is_default"] = 1
            found = True
        else:
            p["is_default"] = 0
    if not found:
        return False
    config.set(_KEY, presets)
    config.save()
    return True
