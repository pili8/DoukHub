# -*- coding: utf-8 -*-
r"""容器内路径 → PC / Mac 上可访问路径的映射。

DoukHub 常部署在 Docker 里，容器内看到的是 `/media/...` 这类路径，
而用户在 PC / Mac 上要通过 SMB 访问同一份文件。本模块负责这层翻译：

    /media/many/UID123_名字_发布作品
        → PC : \\Nas-Gming\03_Yellow\.media\doukhub\many\UID123_名字_发布作品
        → Mac: smb://Nas-Gming/03_Yellow/.media/doukhub/many/UID123_名字_发布作品

映射前缀可用环境变量覆盖（容器里改 compose 的 environment 即可）：
    DOUKHUB_UNC_MEDIA  容器内 /media 对应的 SMB 路径（UNC 写法）
"""
import os

# 容器内 /media 对应的 SMB 路径（UNC 写法）。按当前 NAS 部署环境给默认值。
DEFAULT_MEDIA_UNC = r"\\Nas-Gming\03_Yellow\.media\doukhub"

# 需要翻译的容器内前缀 → 对应环境变量
_PREFIX_ENV = {
    "/media": "DOUKHUB_UNC_MEDIA",
}


def media_unc_root() -> str:
    """取 /media 对应的 SMB 根（UNC 写法）。"""
    return os.environ.get("DOUKHUB_UNC_MEDIA", DEFAULT_MEDIA_UNC).rstrip("\\/")


def to_unc(path: str) -> str:
    """把容器内路径翻成 PC 上的 UNC 路径；翻不出来返回空串。"""
    p = str(path or "").replace("\\", "/").rstrip("/")
    if not p.startswith("/"):
        return ""
    for prefix, env_name in _PREFIX_ENV.items():
        if p == prefix or p.startswith(prefix + "/"):
            root = os.environ.get(env_name, DEFAULT_MEDIA_UNC if prefix == "/media" else "")
            root = root.rstrip("\\/")
            if not root:
                return ""
            rest = p[len(prefix):].strip("/")
            return root + ("\\" + rest.replace("/", "\\") if rest else "")
    return ""


def unc_to_smb(unc: str) -> str:
    """UNC → macOS Finder 能直接用的 smb:// 形式；不是 UNC 则返回空串。"""
    u = str(unc or "")
    if not u.startswith("\\\\"):
        return ""
    return "smb://" + u[2:].replace("\\", "/")


def pc_and_mac(container_path: str) -> tuple[str, str]:
    """一次给出 (PC 路径, Mac 路径)；翻不出来则均为空串。"""
    unc = to_unc(container_path)
    if not unc:
        return "", ""
    return unc, unc_to_smb(unc)
