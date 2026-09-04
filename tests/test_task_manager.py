import asyncio
import json

from app.core import database
from app.core.tasks import TaskManager


class FakeDatabase:
    histories = []

    def add_sync_history(self, data):
        self.histories.append(data)
        return len(self.histories)


def test_task_partial_failure_and_full_history_are_persisted(monkeypatch):
    fake_db = FakeDatabase()
    monkeypatch.setattr(database, "Database", lambda: fake_db)
    manager = TaskManager()
    task = manager.create("update_collection")

    async def job(current_task):
        for i in range(205):
            manager.add_log(current_task.task_id, f"line-{i}")
        manager.update(current_task.task_id, total=205, success=203, failed=2)

    asyncio.run(manager.run_serial(task, job))

    assert task.status == "failed"
    assert task.error == "部分失败: 2 条"
    payload = task.to_dict()
    assert payload["log_total"] == 205
    assert payload["log_truncated"] is True
    assert len(payload["log"]) == 200
    assert payload["log"][0]["message"] == "line-5"

    saved = fake_db.histories[-1]
    assert saved["status"] == "failed"
    assert saved["error"] == "部分失败: 2 条"
    assert len(json.loads(saved["log_json"])) == 205
