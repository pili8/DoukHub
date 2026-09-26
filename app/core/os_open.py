# -*- coding: utf-8 -*-
"""跨平台辅助：用系统文件管理器打开目录。

- Windows: os.startfile（资源管理器）
- macOS:   open
- Linux:   xdg-open（Docker 等无 GUI 环境会失败，调用方走「复制路径」兜底）

统一入口 open_folder()，返回 (是否成功, 失败原因)，由调用方决定弹窗还是让前端复制路径。
"""
import os
import subprocess
import sys
from pathlib import Path


def open_folder(path: Path) -> tuple[bool, str]:
    """尝试用系统文件管理器打开目录，返回 (是否成功, 失败原因)。"""
    p = Path(path)
    try:
        if sys.platform == "win32":
            os.startfile(str(p))
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(p)])
        else:
            subprocess.Popen(["xdg-open", str(p)])
        return True, ""
    except Exception as e:
        return False, str(e)
