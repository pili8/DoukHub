"""DoukHub 数据库备份模块

参照 EntHub 的备份思路，针对 DoukHub 主库（~/.doukhub/doukhub.db）实现：
- VACUUM INTO 一致性备份（不锁库、不丢 WAL 数据）
- 恢复前自动备份（保险，防止误操作丢失数据）
- 恢复后完整性校验
- 保留最近 N 份，自动清理旧备份
- 每日自动备份 + 启动时检查
- 数据库压缩（vacuum）与碎片率统计
"""
import os
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

from .database import DB_PATH

# 保留的备份份数
BACKUP_KEEP_COUNT = 7


def get_backup_dir() -> Path:
    """备份目录：与主库同目录下的 backups/ 子目录。"""
    return DB_PATH.parent / "backups"


def create_backup(reason: str = "手动备份") -> dict:
    """创建数据库备份。

    使用 VACUUM INTO 让 SQLite 自己导出一份干净一致的副本，
    避免直接拷贝文件时遇到正在写入导致的损坏，也不会锁库。
    """
    if not DB_PATH.exists():
        return {"success": False, "error": "数据库文件不存在", "filename": None}

    backup_dir = get_backup_dir()
    backup_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    backup_filename = f"doukhub_{timestamp}.db"
    backup_path = backup_dir / backup_filename

    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=30.0)
        conn.execute(f"VACUUM INTO '{backup_path}'")
        conn.close()

        size = backup_path.stat().st_size
        return {
            "success": True,
            "filename": backup_filename,
            "filepath": str(backup_path),
            "size": size,
            "timestamp": timestamp,
            "reason": reason,
        }
    except Exception as e:
        # 清理可能产生的半成品
        if backup_path.exists():
            try:
                backup_path.unlink()
            except OSError:
                pass
        return {"success": False, "error": str(e), "filename": None}


def list_backups() -> list:
    """列出所有备份文件，按时间倒序。"""
    backup_dir = get_backup_dir()
    if not backup_dir.exists():
        return []

    backups = []
    for f in backup_dir.glob("doukhub_*.db"):
        stat = f.stat()
        try:
            timestamp = f.stem.replace("doukhub_", "")
            dt = datetime.strptime(timestamp, "%Y-%m-%d_%H-%M-%S")
            time_str = dt.strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            time_str = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S")

        backups.append({
            "filename": f.name,
            "filepath": str(f),
            "size": stat.st_size,
            "time": time_str,
        })

    backups.sort(key=lambda x: x["time"], reverse=True)
    return backups


def delete_backup(filename: str) -> dict:
    """删除指定备份（带路径穿越防护）。"""
    backup_dir = get_backup_dir()
    backup_path = backup_dir / filename

    # 安全检查：确保文件确实在备份目录内
    try:
        if not backup_path.resolve().is_relative_to(backup_dir.resolve()):
            return {"success": False, "error": "非法路径"}
    except AttributeError:
        # Python < 3.9 无 is_relative_to，退化为简单判断
        if backup_dir.resolve() not in backup_path.resolve().parents:
            return {"success": False, "error": "非法路径"}

    if not backup_path.exists():
        return {"success": False, "error": "文件不存在"}

    try:
        backup_path.unlink()
        return {"success": True}
    except Exception as e:
        return {"success": False, "error": str(e)}


def _clear_wal_shm(db_path: Path) -> None:
    """清理 WAL/SHM 附属文件（替换数据库文件后必须清理，否则读到旧数据）。"""
    for suffix in ("-wal", "-shm"):
        p = db_path.parent / f"{db_path.name}{suffix}"
        if p.exists():
            try:
                p.unlink()
            except OSError:
                pass


def _verify_sqlite(path: Path) -> bool:
    """校验文件是否是有效的 DoukHub 数据库。"""
    try:
        conn = sqlite3.connect(str(path))
        conn.execute("SELECT COUNT(*) FROM account_cache").fetchone()
        conn.execute("SELECT COUNT(*) FROM share_cache").fetchone()
        conn.execute("SELECT COUNT(*) FROM cookie_cache").fetchone()
        conn.close()
        return True
    except Exception:
        return False


def restore_backup(filename: str) -> dict:
    """从备份文件恢复数据库。

    步骤：
      1. 校验备份文件存在且是合法数据库
      2. 先自动备份当前数据库（保险）
      3. 用备份文件替换当前数据库，清理 WAL/SHM
      4. 校验恢复后的完整性
    """
    backup_dir = get_backup_dir()
    backup_path = backup_dir / filename

    # 路径穿越防护
    try:
        if not backup_path.resolve().is_relative_to(backup_dir.resolve()):
            return {"success": False, "error": "非法路径"}
    except AttributeError:
        if backup_dir.resolve() not in backup_path.resolve().parents:
            return {"success": False, "error": "非法路径"}

    if not backup_path.exists():
        return {"success": False, "error": "备份文件不存在"}

    # 1. 校验备份是合法数据库
    if not _verify_sqlite(backup_path):
        return {"success": False, "error": "备份文件不是有效的 DoukHub 数据库"}

    # 2. 恢复前自动备份当前数据（保险）
    auto_backup = create_backup(reason="恢复前自动备份")
    if not auto_backup["success"]:
        return {"success": False, "error": f"恢复前自动备份失败：{auto_backup.get('error')}"}

    # 3. 替换数据库文件
    try:
        shutil.copy2(str(backup_path), str(DB_PATH))
        _clear_wal_shm(DB_PATH)
    except Exception as e:
        return {"success": False, "error": f"替换数据库失败：{e}"}

    # 4. 校验恢复后的数据库
    try:
        conn = sqlite3.connect(str(DB_PATH))
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        accounts = conn.execute("SELECT COUNT(*) FROM account_cache").fetchone()[0]
        shares = conn.execute("SELECT COUNT(*) FROM share_cache").fetchone()[0]
        cookies = conn.execute("SELECT COUNT(*) FROM cookie_cache").fetchone()[0]
        conn.close()
    except Exception as e:
        return {"success": False, "error": f"恢复后校验失败：{e}", "backup_filename": auto_backup["filename"]}

    if integrity != "ok":
        return {"success": False, "error": f"恢复后完整性检查未通过：{integrity}", "backup_filename": auto_backup["filename"]}

    return {
        "success": True,
        "message": f"已从 {filename} 恢复（账号 {accounts} 条 / 分享 {shares} 条 / Cookie {cookies} 条），需重启应用生效",
        "backup_filename": auto_backup["filename"],
    }


def cleanup_old_backups(keep_count: int = BACKUP_KEEP_COUNT) -> dict:
    """清理旧备份，只保留最近 keep_count 份。"""
    backups = list_backups()
    if len(backups) <= keep_count:
        return {"deleted": 0, "kept": len(backups)}

    deleted = 0
    for backup in backups[keep_count:]:
        if delete_backup(backup["filename"])["success"]:
            deleted += 1

    return {"deleted": deleted, "kept": keep_count}


def get_db_stats() -> dict:
    """获取数据库统计：大小、总页数、空闲页数、碎片率、可回收空间。"""
    if not DB_PATH.exists():
        return {"size": 0, "page_count": 0, "freelist_count": 0, "page_size": 0,
                "fragmentation": 0.0, "reclaimable_bytes": 0}

    try:
        size = DB_PATH.stat().st_size
        conn = sqlite3.connect(str(DB_PATH))
        page_count = conn.execute("PRAGMA page_count").fetchone()[0]
        freelist_count = conn.execute("PRAGMA freelist_count").fetchone()[0]
        page_size = conn.execute("PRAGMA page_size").fetchone()[0]
        conn.close()

        fragmentation = round(freelist_count * 100.0 / page_count, 2) if page_count else 0.0
        reclaimable_bytes = freelist_count * page_size

        return {
            "size": size,
            "page_count": page_count,
            "freelist_count": freelist_count,
            "page_size": page_size,
            "fragmentation": fragmentation,
            "reclaimable_bytes": reclaimable_bytes,
        }
    except Exception as e:
        return {"size": 0, "page_count": 0, "freelist_count": 0, "page_size": 0,
                "fragmentation": 0.0, "reclaimable_bytes": 0, "error": str(e)}


def _table_count(path: Path) -> dict:
    """统计一个数据库里三张核心表的记录数。"""
    conn = sqlite3.connect(str(path))
    try:
        return {
            "accounts": conn.execute("SELECT COUNT(*) FROM account_cache").fetchone()[0],
            "shares": conn.execute("SELECT COUNT(*) FROM share_cache").fetchone()[0],
            "cookies": conn.execute("SELECT COUNT(*) FROM cookie_cache").fetchone()[0],
        }
    finally:
        conn.close()


def vacuum_database() -> dict:
    """压缩数据库，回收空闲页空间。

    步骤：
      1. 先自动备份（保险）
      2. VACUUM INTO 到临时文件（不锁原库）
      3. 校验临时文件记录数与原库一致
      4. 原子替换原文件 + 清理 WAL/SHM
    """
    if not DB_PATH.exists():
        return {"success": False, "error": "数据库文件不存在"}

    before_size = DB_PATH.stat().st_size

    # 1. 压缩前备份
    backup_result = create_backup(reason="压缩前自动备份")
    if not backup_result["success"]:
        return {"success": False, "error": f"压缩前备份失败：{backup_result.get('error')}"}

    temp_path = DB_PATH.parent / "doukhub_vacuuming.db"
    if temp_path.exists():
        temp_path.unlink()

    try:
        # 2. VACUUM INTO 到临时文件
        src_conn = sqlite3.connect(str(DB_PATH), timeout=30.0)
        src_conn.execute(f"VACUUM INTO '{temp_path}'")
        src_conn.close()

        # 3. 校验记录数一致
        orig_counts = _table_count(DB_PATH)
        new_counts = _table_count(temp_path)
        for key in ("accounts", "shares", "cookies"):
            if orig_counts[key] != new_counts[key]:
                temp_path.unlink()
                return {"success": False, "error": f"校验失败：{key} 记录数不一致（原 {orig_counts[key]} / 新 {new_counts[key]}）"}

        verify_conn = sqlite3.connect(str(temp_path))
        integrity = verify_conn.execute("PRAGMA integrity_check").fetchone()[0]
        verify_conn.close()
        if integrity != "ok":
            temp_path.unlink()
            return {"success": False, "error": f"完整性检查未通过：{integrity}"}

        # 4. 原子替换
        os.replace(str(temp_path), str(DB_PATH))
        _clear_wal_shm(DB_PATH)

        after_size = DB_PATH.stat().st_size
        freed = before_size - after_size

        cleanup_old_backups(BACKUP_KEEP_COUNT)

        return {
            "success": True,
            "before_size": before_size,
            "after_size": after_size,
            "freed": freed,
            "backup_filename": backup_result["filename"],
        }
    except Exception as e:
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass
        return {"success": False, "error": str(e)}


def check_daily_backup() -> dict:
    """检查是否需要每日备份（距离上次备份超过 24 小时则创建）。"""
    backups = list_backups()
    if not backups:
        result = create_backup(reason="首次自动备份")
        if result["success"]:
            cleanup_old_backups(BACKUP_KEEP_COUNT)
        return result

    try:
        latest_dt = datetime.strptime(backups[0]["time"], "%Y-%m-%d %H:%M:%S")
        hours_since = (datetime.now() - latest_dt).total_seconds() / 3600
    except ValueError:
        return {"success": False, "skipped": True, "reason": "无法解析最近备份时间"}

    if hours_since >= 24:
        result = create_backup(reason="每日自动备份")
        if result["success"]:
            cleanup_old_backups(BACKUP_KEEP_COUNT)
        return result

    return {"success": False, "skipped": True, "reason": "今日已备份"}
