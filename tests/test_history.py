"""HistoryDB 模块测试"""
import pytest

from app.core.history import HistoryDB


class TestHistoryDB:
    """采集历史记录测试"""

    def test_init_creates_tables(self, tmp_data_dir):
        """初始化创建数据表"""
        db = HistoryDB(tmp_data_dir)
        assert db.db_path.exists()

    def test_add_and_get_records(self, tmp_data_dir):
        """添加并查询记录"""
        db = HistoryDB(tmp_data_dir)
        record_id = db.add_record({
            "account_name": "测试账号",
            "platform": "抖音",
            "sec_user_id": "test123",
            "collection_type": "发布",
            "works_count": 10,
            "success_count": 8,
            "fail_count": 2,
            "started_at": "2025-01-01 12:00:00",
            "finished_at": "2025-01-01 12:05:00",
            "duration_seconds": 300.0,
            "status": "success",
            "error_message": "",
        })
        assert record_id > 0

        records = db.get_records()
        assert len(records) == 1
        assert records[0]["account_name"] == "测试账号"
        assert records[0]["works_count"] == 10

    def test_get_records_with_status_filter(self, tmp_data_dir):
        """按状态过滤记录"""
        db = HistoryDB(tmp_data_dir)
        db.add_record({"account_name": "a", "status": "success"})
        db.add_record({"account_name": "b", "status": "failed"})
        db.add_record({"account_name": "c", "status": "success"})

        success_records = db.get_records(status="success")
        assert len(success_records) == 2

        failed_records = db.get_records(status="failed")
        assert len(failed_records) == 1

    def test_get_records_pagination(self, tmp_data_dir):
        """分页查询"""
        db = HistoryDB(tmp_data_dir)
        for i in range(10):
            db.add_record({"account_name": f"acc_{i}"})

        page1 = db.get_records(limit=3, offset=0)
        page2 = db.get_records(limit=3, offset=3)
        assert len(page1) == 3
        assert len(page2) == 3
        assert page1[0]["id"] != page2[0]["id"]

    def test_get_stats(self, tmp_data_dir):
        """统计信息"""
        db = HistoryDB(tmp_data_dir)
        db.add_record({"account_name": "a", "status": "success", "started_at": "2099-01-01 00:00:00"})
        db.add_record({"account_name": "b", "status": "failed"})

        stats = db.get_stats()
        assert stats["total"] == 2
        assert stats["success"] == 1
        assert stats["failed"] == 1

    def test_add_and_get_tasks(self, tmp_data_dir):
        """定时任务增查"""
        db = HistoryDB(tmp_data_dir)
        task_id = db.add_task("每日巡检", "0 2 * * *", "3,4,5")
        assert task_id > 0

        tasks = db.get_tasks()
        assert len(tasks) == 1
        assert tasks[0]["name"] == "每日巡检"
        assert tasks[0]["cron_expression"] == "0 2 * * *"

    def test_update_task(self, tmp_data_dir):
        """更新定时任务"""
        db = HistoryDB(tmp_data_dir)
        task_id = db.add_task("测试任务", "0 3 * * *")
        db.update_task(task_id, {"name": "改名任务", "enabled": False})

        tasks = db.get_tasks()
        assert tasks[0]["name"] == "改名任务"
        assert tasks[0]["enabled"] == 0  # SQLite stores bool as int

    def test_delete_task(self, tmp_data_dir):
        """删除定时任务"""
        db = HistoryDB(tmp_data_dir)
        task_id = db.add_task("待删除", "0 0 * * *")
        db.delete_task(task_id)

        tasks = db.get_tasks()
        assert len(tasks) == 0
