"""日志 / 缓存清理：列出可清理项并执行清理。

通用机制：list_items 返回所有可清理项（含名称、大小、说明），
clean_item 按项执行清理。以后新增日志/缓存类别，只需在这里加一项。
"""
from pathlib import Path
import shutil


def _dir_size(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file():
        return path.stat().st_size
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def _file_count(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file():
        return 1
    return sum(1 for p in path.rglob("*") if p.is_file())


def list_items(config, root: Path) -> list[dict]:
    """返回所有可清理项。root 为项目根目录。"""
    log_dir = config.app_data_dir / "collection_logs"
    batch = sorted(log_dir.glob("*.log"))
    batch_size = sum(f.stat().st_size for f in batch)

    app_logs = [root / "doukhub.log", root / "doukhub_err.log",
                root / "TikTokDownloader" / "logs" / "doukhub_TikTokDownloader.log"]
    app_logs = [p for p in app_logs if p.exists()]
    app_logs_size = sum(p.stat().st_size for p in app_logs)

    user_dir = Path.home() / ".doukhub"
    dedup_cache = user_dir / "dedup_cache.json"
    dedup_result = user_dir / "dedup_result.json"
    ttd_cache = root / "TikTokDownloader" / "Volume" / "Cache"
    probe = root / "Download"

    return [
        {"id": "batch_logs", "name": "批次日志", "count": len(batch), "size_bytes": batch_size,
         "desc": "每次采集的详细过程记录（含作品标题），保留最近 30 个", "action": "清理旧批次（保留30）"},
        {"id": "app_logs", "name": "应用日志", "count": len(app_logs), "size_bytes": app_logs_size,
         "desc": "应用运行 / 错误 / 下载器日志", "action": "清空"},
        {"id": "dedup_cache", "name": "查重哈希缓存", "count": 1 if dedup_cache.exists() else 0,
         "size_bytes": _dir_size(dedup_cache), "desc": "文件查重提速缓存，清除后下次扫描重新计算", "action": "清除"},
        {"id": "dedup_result", "name": "查重结果", "count": 1 if dedup_result.exists() else 0,
         "size_bytes": _dir_size(dedup_result), "desc": "上一次文件查重的结果", "action": "清除"},
        {"id": "ttd_cache", "name": "下载器临时缓存", "count": _file_count(ttd_cache),
         "size_bytes": _dir_size(ttd_cache), "desc": "TTD 下载过程中的临时文件", "action": "清除"},
        {"id": "probe_files", "name": "探测残留文件", "count": len(list(probe.glob(".doukhub_probe_*"))) if probe.exists() else 0,
         "size_bytes": 0, "desc": "连通性探测留下的临时文件", "action": "清除"},
    ]


def clean_item(item_id: str, config, root: Path) -> dict:
    """执行单项清理，返回 {cleaned, freed_bytes}。未知项返回 0。"""
    log_dir = config.app_data_dir / "collection_logs"
    user_dir = Path.home() / ".doukhub"
    freed = 0
    cleaned = 0

    if item_id == "batch_logs":
        batch = sorted(log_dir.glob("*.log"))
        for f in batch[:-30]:
            freed += f.stat().st_size
            try:
                f.unlink()
                cleaned += 1
            except OSError:
                pass

    elif item_id == "app_logs":
        for name in ["doukhub.log", "doukhub_err.log",
                     "TikTokDownloader/logs/doukhub_TikTokDownloader.log"]:
            p = root / name
            if p.exists():
                freed += p.stat().st_size
                try:
                    p.unlink()
                    cleaned += 1
                except OSError:
                    # 文件被占用（正在写）时无法删除，改为截断
                    try:
                        with open(p, "w", encoding="utf-8"):
                            pass
                        cleaned += 1
                    except OSError:
                        pass

    elif item_id == "dedup_cache":
        p = user_dir / "dedup_cache.json"
        if p.exists():
            freed = p.stat().st_size
            try:
                p.unlink()
                cleaned = 1
            except OSError:
                pass

    elif item_id == "dedup_result":
        p = user_dir / "dedup_result.json"
        if p.exists():
            freed = p.stat().st_size
            try:
                p.unlink()
                cleaned = 1
            except OSError:
                pass

    elif item_id == "ttd_cache":
        cache_dir = root / "TikTokDownloader" / "Volume" / "Cache"
        if cache_dir.exists():
            for p in cache_dir.iterdir():
                freed += _dir_size(p)
                try:
                    if p.is_dir():
                        shutil.rmtree(p)
                    else:
                        p.unlink()
                    cleaned += 1
                except OSError:
                    pass

    elif item_id == "probe_files":
        probe = root / "Download"
        if probe.exists():
            for p in probe.glob(".doukhub_probe_*"):
                freed += p.stat().st_size
                try:
                    p.unlink()
                    cleaned += 1
                except OSError:
                    pass

    return {"cleaned": cleaned, "freed_bytes": freed}
