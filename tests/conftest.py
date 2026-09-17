"""共享测试 fixtures"""
import os
import sys
import tempfile
from pathlib import Path

import pytest

# 确保项目根目录在 sys.path 中
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def isolated_data_root(tmp_path, monkeypatch):
    """所有测试默认使用临时数据根，禁止触碰真实 ~/.doukhub"""
    monkeypatch.setenv("DOUKHUB_DATA_ROOT", str(tmp_path))

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
