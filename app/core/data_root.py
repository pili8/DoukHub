"""Resolve the fixed bootstrap pointer to DoukHub's application data root."""
from __future__ import annotations

import json
import os
from pathlib import Path


class DataRootError(RuntimeError):
    pass


DEFAULT_DATA_ROOT = Path.home() / ".doukhub"
BOOTSTRAP_PATH = DEFAULT_DATA_ROOT / "data_root.json"
RESERVED_NAMES = ("doukhub.db", "history.db", "backups", "collection_logs")


def _root_error(root: Path, bootstrap: Path | None = None) -> DataRootError:
    suffix = f"\n引导文件：{bootstrap}" if bootstrap else ""
    return DataRootError(f"应用数据目录不可用：{root}{suffix}")


def _atomic_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    os.replace(temp, path)


def app_data_root() -> Path:
    env_root = os.getenv("DOUKHUB_DATA_ROOT")
    if env_root:
        root = Path(env_root).expanduser().resolve()
        if not root.exists():
            raise _root_error(root, BOOTSTRAP_PATH)
        return root

    if not BOOTSTRAP_PATH.exists():
        DEFAULT_DATA_ROOT.mkdir(parents=True, exist_ok=True)
        _atomic_write(
            BOOTSTRAP_PATH,
            {"version": 1, "data_dir": str(DEFAULT_DATA_ROOT.resolve())},
        )

    try:
        payload = json.loads(BOOTSTRAP_PATH.read_text(encoding="utf-8"))
        root = Path(str(payload["data_dir"])).expanduser()
    except Exception as exc:
        raise DataRootError(f"引导文件损坏：{BOOTSTRAP_PATH}") from exc

    if not root.is_absolute():
        raise DataRootError(f"引导文件必须使用绝对路径：{BOOTSTRAP_PATH}")
    root = root.resolve()
    if not root.is_dir():
        raise _root_error(root, BOOTSTRAP_PATH)
    return root


def validate_target(raw: str) -> dict:
    raw = (raw or "").strip().strip('"')
    if not raw:
        return {"valid": False, "message": "路径不能为空", "target": ""}

    try:
        target = Path(os.path.expandvars(raw)).expanduser().resolve()
    except Exception as exc:
        return {"valid": False, "message": f"路径无效：{exc}", "target": raw}

    if not target.parent.exists():
        return {"valid": False, "message": "上级目录不存在，请先创建或改用已存在的目录", "target": str(target)}

    current = app_data_root()
    if target == current or target in current.parents or current in target.parents:
        return {"valid": False, "message": "新目录不能是当前目录或其嵌套目录", "target": str(target)}

    conflicts = [name for name in RESERVED_NAMES if (target / name).exists()]
    if conflicts:
        return {"valid": False, "message": "目标已存在：" + "、".join(conflicts), "target": str(target)}

    probe = target.parent / ".doukhub-write-test"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except Exception as exc:
        return {"valid": False, "message": f"父目录不可写：{exc}", "target": str(target)}

    return {"valid": True, "message": "目录可用（开始迁移时才创建）", "target": str(target)}


def write_bootstrap(path: Path) -> None:
    _atomic_write(
        BOOTSTRAP_PATH, {"version": 1, "data_dir": str(path.resolve())}
    )
