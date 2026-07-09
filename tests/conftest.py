"""共享测试 fixtures"""
import os
import sys
import tempfile
from pathlib import Path

import pytest

# 确保项目根目录在 sys.path 中
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture
def tmp_dir(tmp_path):
    """提供临时目录"""
    return tmp_path


@pytest.fixture
def tmp_config(tmp_path):
    """提供临时配置文件路径"""
    config_file = tmp_path / "config.json"
    return config_file


@pytest.fixture
def tmp_data_dir(tmp_path):
    """提供临时数据目录"""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    return data_dir
