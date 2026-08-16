"""Config 模块测试"""
import json
from pathlib import Path

import pytest

from app.core.config import Config, DEFAULT_CONFIG


class TestConfig:
    """配置管理测试"""

    def test_creates_default_config_when_missing(self, tmp_path):
        """配置文件不存在时自动创建默认配置"""
        config_file = tmp_path / "sub" / "config.json"
        cfg = Config(config_file)
        assert config_file.exists()
        assert cfg.get("feishu.app_id") == ""
        assert cfg.concurrent_accounts == 3

    def test_loads_existing_config(self, tmp_path):
        """加载已有配置文件"""
        data = {"feishu": {"app_id": "test_id"}, "concurrent_accounts": 5}
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps(data), encoding="utf-8")

        cfg = Config(config_file)
        assert cfg.get("feishu.app_id") == "test_id"
        assert cfg.concurrent_accounts == 5

    def test_merge_defaults_fills_missing_keys(self, tmp_path):
        """缺失的配置项自动补充默认值"""
        data = {"feishu": {"app_id": "x"}}
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps(data), encoding="utf-8")

        cfg = Config(config_file)
        # feishu 其他字段应有默认值
        assert cfg.get("feishu.app_secret") == ""
        # downloader 整组应有默认值
        assert cfg.get("downloader.ttd_port") == 5555

    def test_get_dot_path(self, tmp_path):
        """点分路径读取"""
        config_file = tmp_path / "config.json"
        cfg = Config(config_file)
        assert cfg.get("cookie.rotation_mode") == "random"
        assert cfg.get("nonexistent.path", "fallback") == "fallback"

    def test_set_dot_path(self, tmp_path):
        """点分路径写入"""
        config_file = tmp_path / "config.json"
        cfg = Config(config_file)
        cfg.set("feishu.app_id", "new_id")
        assert cfg.get("feishu.app_id") == "new_id"

        cfg.set("new_section.new_key", 42)
        assert cfg.get("new_section.new_key") == 42

    def test_save_and_reload(self, tmp_path):
        """保存后重新加载数据一致"""
        config_file = tmp_path / "config.json"
        cfg = Config(config_file)
        cfg.set("feishu.app_id", "persist_test")
        cfg.save()

        cfg2 = Config(config_file)
        assert cfg2.get("feishu.app_id") == "persist_test"

    def test_properties(self, tmp_path):
        """属性访问器"""
        config_file = tmp_path / "config.json"
        cfg = Config(config_file)
        assert isinstance(cfg.feishu, dict)
        assert isinstance(cfg.downloader, dict)
        assert isinstance(cfg.local, dict)
        assert isinstance(cfg.cookie_config, dict)
        assert cfg.ttd_port == 5555
        assert cfg.xhs_port == 5556
        assert isinstance(cfg.data_dir, Path)
        assert isinstance(cfg.download_path, Path)
        assert isinstance(cfg.ttd_path, str)
        assert isinstance(cfg.xhs_path, str)

    def test_single_work_preferences_have_defaults(self, tmp_path):
        cfg = Config(tmp_path / "config.json")
        assert cfg.single_work["default_template_id"] == "default"
        assert cfg.single_work["templates"][0]["template"] == (
            "{create_time} {author} {title}"
        )
        assert cfg.single_work["recent_dirs"] == []

    def test_single_work_download_path_overrides_local_path(self, tmp_path):
        cfg = Config(tmp_path / "config.json")
        expected = tmp_path / "SingleWorks"
        cfg.set("single_work.download_path", str(expected))
        assert cfg.download_path == expected
