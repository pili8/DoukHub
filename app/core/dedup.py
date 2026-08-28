"""DoukHub 文件查重模块

基于成熟方案的三级过滤思路（size → partial hash → full hash）实现：
1. 按文件大小分组（内容相同则大小必然相同，大小不同的直接排除）
2. 组内只读文件开头 64KB 算部分哈希（便宜，排除大小相同但内容不同的）
3. 剩下的极少数再完整读一遍算全哈希（贵，但数量已极少）

附加：
- 哈希缓存：记录路径+大小+mtime 对应的全哈希，二次扫描复用
- 作品 id 提取：从文件名识别 19 位连续数字（抖音 aweme_id），仅作展示辅助，不参与删除判定
- 结果与回收区独立文件存储，不污染主库
"""
import hashlib
import json
import os
import re
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path

from .tasks import get_task_manager

# 数据目录：与主库同目录（~/.doukhub/）
_DATA_DIR = Path.home() / ".doukhub"
_CACHE_FILE = _DATA_DIR / "dedup_cache.json"
_RESULT_FILE = _DATA_DIR / "dedup_result.json"
_DEFAULT_RECYCLE_DIR = _DATA_DIR / "dedup_recycle"
_RECYCLE_DIR = _DEFAULT_RECYCLE_DIR


def get_recycle_dir() -> Path:
    """当前回收区目录。"""
    return _RECYCLE_DIR


def set_recycle_dir(path_str: str) -> Path:
    """设置回收区目录；空或非法路径回退默认。返回生效路径。"""
    global _RECYCLE_DIR
    p = (path_str or "").strip()
    if p:
        try:
            cand = Path(p).expanduser().resolve()
        except OSError:
            cand = Path(p).expanduser()
        try:
            cand.mkdir(parents=True, exist_ok=True)
            _RECYCLE_DIR = cand
        except OSError:
            _RECYCLE_DIR = _DEFAULT_RECYCLE_DIR
    else:
        _RECYCLE_DIR = _DEFAULT_RECYCLE_DIR
    try:
        _RECYCLE_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        _RECYCLE_DIR = _DEFAULT_RECYCLE_DIR
    return _RECYCLE_DIR

# 部分哈希读取的字节数（文件开头）
HEAD_BYTES = 64 * 1024
# 全哈希分块大小
CHUNK = 1024 * 1024
# 作品 id 正则（抖音 aweme_id 为 19 位数字）
_ID_RE = re.compile(r"(?<!\d)(\d{19})(?!\d)")

# 扫描文件扩展名白名单（视频/图片/音频/描述，覆盖附属文件）
VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi", ".flv", ".ts"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".bmp", ".gif"}
AUDIO_EXTS = {".mp3", ".m4a", ".wav", ".aac"}
META_EXTS = {".json", ".txt"}
ALL_EXTS = VIDEO_EXTS | IMAGE_EXTS | AUDIO_EXTS | META_EXTS

# 后台扫描状态
SCAN_STATE = {
    "running": False,
    "stage": "",          # collecting / hashing / done
    "total_files": 0,     # 收集到的文件总数
    "scanned": 0,         # 已处理文件数
    "groups": 0,          # 重复组数
    "message": "",
    "started_at": None,
    "finished_at": None,
}
_SCAN_LOCK = threading.Lock()
_SCAN_THREAD = None


def _now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _set_state(**kwargs) -> None:
    with _SCAN_LOCK:
        SCAN_STATE.update(kwargs)


def get_scan_state() -> dict:
    with _SCAN_LOCK:
        return dict(SCAN_STATE)


# ---------- 哈希 ----------

def _partial_hash(path: Path) -> str:
    """读文件开头 HEAD_BYTES 算哈希（便宜）。"""
    h = hashlib.blake2b(digest_size=16)
    with open(path, "rb") as f:
        h.update(f.read(HEAD_BYTES))
    return h.hexdigest()


def _full_hash(path: Path) -> str:
    """分块读完整文件算哈希（贵）。"""
    h = hashlib.blake2b(digest_size=32)
    with open(path, "rb") as f:
        while True:
            block = f.read(CHUNK)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def _extract_work_id(name: str) -> str | None:
    """从文件名提取作品 id（19 位连续数字）。"""
    m = _ID_RE.search(name)
    return m.group(1) if m else None


# ---------- 哈希缓存 ----------

def _load_cache() -> dict:
    try:
        return json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_cache(cache: dict) -> None:
    try:
        _CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def _cached_full_hash(path: Path, stat: os.stat_result, cache: dict) -> str | None:
    """命中缓存（路径+大小+mtime 均未变）则返回哈希，否则返回 None。"""
    entry = cache.get(str(path))
    if entry and entry.get("size") == stat.st_size and entry.get("mtime") == stat.st_mtime:
        return entry.get("hash")
    return None


# ---------- 收集文件 ----------

def collect_files(roots: list[str], exts: set[str] = ALL_EXTS, on_progress=None) -> list[dict]:
    """递归收集指定目录下的目标文件，返回 {path, size, mtime} 列表。"""
    files = []
    seen = set()
    for root in roots:
        root_path = Path(root)
        if not root_path.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(root_path):
            # 跳过回收区自身，避免扫到已隔离文件
            _rc = get_recycle_dir()
            if _rc in Path(dirpath).parents or Path(dirpath) == _rc:
                dirnames[:] = []
                continue
            for fn in filenames:
                p = Path(dirpath) / fn
                if p.suffix.lower() not in exts:
                    continue
                key = str(p.resolve())
                if key in seen:
                    continue
                try:
                    st = p.stat()
                except OSError:
                    continue
                seen.add(key)
                files.append({"path": key, "size": st.st_size, "mtime": st.st_mtime, "name": fn})
    return files


# ---------- 查重核心（三级过滤） ----------

def find_duplicates(files: list[dict], cache: dict, on_progress=None) -> list[dict]:
    """三级过滤找出重复组。返回 [{hash, size, files: [...]}]。"""
    # 1. 按大小分组
    by_size: dict[int, list[dict]] = {}
    for f in files:
        by_size.setdefault(f["size"], []).append(f)

    # 2. 大小组内算部分哈希
    candidates = []  # 存疑文件（大小撞车，进入下一步）
    for size, group in by_size.items():
        if len(group) < 2:
            continue
        by_partial: dict[str, list[dict]] = {}
        for f in group:
            p = Path(f["path"])
            try:
                ph = _partial_hash(p)
            except OSError:
                continue
            f["partial"] = ph
            by_partial.setdefault(ph, []).append(f)
        for ph, sub in by_partial.items():
            if len(sub) >= 2:
                candidates.extend(sub)

    # 3. 存疑文件算全哈希（走缓存）
    by_full: dict[str, list[dict]] = {}
    total = len(candidates)
    done = 0
    for f in candidates:
        p = Path(f["path"])
        try:
            st = p.stat()
            fh = _cached_full_hash(p, st, cache)
            if fh is None:
                fh = _full_hash(p)
                cache[str(p.resolve())] = {"size": st.st_size, "mtime": st.st_mtime, "hash": fh}
        except OSError:
            done += 1
            if on_progress:
                on_progress(done, total)
            continue
        f["hash"] = fh
        by_full.setdefault(fh, []).append(f)
        done += 1
        if on_progress:
            on_progress(done, total)

    # 4. 聚合重复组
    groups = []
    for fh, group in by_full.items():
        if len(group) < 2:
            continue
        groups.append({
            "hash": fh,
            "size": group[0]["size"],
            "files": [{
                "path": f["path"],
                "name": f["name"],
                "size": f["size"],
                "mtime": f["mtime"],
                "work_id": _extract_work_id(f["name"]),
            } for f in group],
        })

    # 按文件大小降序（大文件组优先，节省空间最多）
    groups.sort(key=lambda g: g["size"] * len(g["files"]), reverse=True)
    return groups

def find_id_duplicates(files: list[dict], content_groups: list[dict]) -> list[dict]:
    """仅对视频文件按作品ID分组，抓出同一作品的多份下载（可能不同清晰度/格式）。

    图集图片天然不参与（扩展名不是视频的跳过），不会被误判。
    已被内容哈希组判定为重复的组（同内容两份）不重复展示。
    """
    covered: set[str] = set()
    for g in content_groups:
        for f in g["files"]:
            covered.add(f["path"])

    by_id: dict[str, list[dict]] = {}
    for f in files:
        if Path(f["path"]).suffix.lower() not in VIDEO_EXTS:
            continue
        wid = _extract_work_id(f["name"])
        if not wid:
            continue
        by_id.setdefault(wid, []).append(f)

    groups = []
    for wid, group in by_id.items():
        if len(group) < 2:
            continue
        # 全组都已被内容哈希组覆盖（内容完全相同）→ 不重复展示
        if all(f["path"] in covered for f in group):
            continue
        groups.append({
            "work_id": wid,
            "files": [{
                "path": f["path"],
                "name": f["name"],
                "size": f["size"],
                "mtime": f["mtime"],
                "work_id": wid,
            } for f in sorted(group, key=lambda f: f["size"], reverse=True)],
        })
    groups.sort(key=lambda g: sum(f["size"] for f in g["files"]), reverse=True)
    return groups

# ---------- 后台扫描 ----------

def _scan_worker(task_id: str, roots: list[str], exts: set[str]) -> None:
    tm = get_task_manager()

    def _log(msg: str, level: str = "info") -> None:
        tm.add_log(task_id, msg, level)

    _set_state(running=True, stage="collecting", total_files=0, scanned=0,
               groups=0, message="正在收集文件...", started_at=_now_str(), finished_at=None)
    tm.update(task_id, status="running")
    _log(f"开始查重，共 {len(roots)} 个目录：")
    for r in roots:
        _log("  扫描目录 " + r)
    try:
        files = collect_files(roots, exts)
        total = len(files)
        _set_state(stage="hashing", total_files=total, scanned=0,
                   message=f"已收集 {total} 个文件，开始查重...")
        tm.update(task_id, total=total)
        _log(f"共收集 {total} 个文件，开始比对内容...")
        if total == 0:
            _log("没有找到匹配类型的文件", "warn")

        cache = _load_cache()
        last_pct = -1

        def progress(done, t):
            _set_state(scanned=done, message=f"查重中 {done}/{t}...")
            tm.update(task_id, success=done)
            nonlocal last_pct
            if t > 0:
                pct = int(done * 100 / t)
                if pct >= last_pct + 5:
                    last_pct = pct
                    _log(f"  查重进度 {done}/{t}（{pct}%）")

        groups = find_duplicates(files, cache, on_progress=progress)
        id_groups = find_id_duplicates(files, groups)

        dup_files = sum(len(g["files"]) for g in groups)
        _log(f"查重完成：内容相同 {len(groups)} 组（{dup_files} 个文件）、作品ID相同 {len(id_groups)} 组", "ok")
        _set_state(stage="done", scanned=total, groups=len(groups),
                   message="扫描完成", finished_at=_now_str())
        tm.update(task_id, status="done", success=total)

        # 落盘结果
        result = {
            "generated_at": _now_str(),
            "scanned_files": total,
            "groups": groups,
            "id_groups": id_groups,
        }
        _RESULT_FILE.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        _save_cache(cache)
        _set_state(running=False)
    except Exception as e:
        _set_state(running=False, stage="error", message=f"扫描失败：{e}", finished_at=_now_str())
        tm.update(task_id, status="failed", error=str(e))
        _log(f"扫描失败：{e}", "error")


def start_scan(roots: list[str], exts: list[str] | None = None) -> dict:
    """启动后台扫描，返回 task_id 供前端轮询进度与日志。"""
    if SCAN_STATE["running"]:
        return {"success": False, "error": "已有扫描在进行中"}

    clean_roots = [r for r in roots if (r or "").strip() and Path(r).is_dir()]
    if not clean_roots:
        return {"success": False, "error": "没有有效的扫描目录"}

    ext_set = set(exts) if exts else ALL_EXTS
    tm = get_task_manager()
    task = tm.create("dedup")
    task_id = task.task_id
    global _SCAN_THREAD
    _SCAN_THREAD = threading.Thread(target=_scan_worker, args=(task_id, clean_roots, ext_set), daemon=True)
    _SCAN_THREAD.start()
    return {"success": True, "roots": clean_roots, "task_id": task_id}


# ---------- 结果读取 ----------

def get_result() -> dict | None:
    try:
        return json.loads(_RESULT_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


# ---------- 移动 / 还原 / 删除 ----------

def _recycle_path(original: str) -> Path:
    """把原绝对路径映射到回收区内的相对结构，保留原目录层级避免重名。"""
    p = Path(original)
    parts = []
    # 盘符 "D:" 转为 "D"
    if p.drive:
        parts.append(p.drive.rstrip(":\\/"))
    parts.extend(part for part in p.parts[1:] if part not in ("/", "\\"))
    return get_recycle_dir().joinpath(*parts)


def move_to_recycle(paths: list[str]) -> dict:
    """把文件移动到回收区（保留目录结构，可还原）。"""
    moved, failed = [], []
    for raw in paths:
        src = Path(raw)
        if not src.exists():
            failed.append({"path": raw, "reason": "文件不存在"})
            continue
        dst = _recycle_path(raw)
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
            moved.append({"from": raw, "to": str(dst)})
        except Exception as e:
            failed.append({"path": raw, "reason": str(e)})
    return {"success": len(failed) == 0, "moved": len(moved), "failed": failed}


def list_recycle() -> list[dict]:
    """列出回收区文件（含原始路径信息）。"""
    rc = get_recycle_dir()
    if not rc.exists():
        return []
    items = []
    for dirpath, dirnames, filenames in os.walk(rc):
        for fn in filenames:
            p = Path(dirpath) / fn
            try:
                st = p.stat()
            except OSError:
                continue
            items.append({
                "path": str(p),
                "name": fn,
                "size": st.st_size,
                "mtime": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
            })
    items.sort(key=lambda x: x["mtime"], reverse=True)
    return items


def restore_from_recycle(paths: list[str]) -> dict:
    """把回收区文件还原到原位置。

    用 copy2 复制回原位，删除回收区源文件交给用户统一清空回收区时处理，
    避免在程序内直接删除文件。
    """
    restored, failed = [], []
    for raw in paths:
        p = Path(raw)
        try:
            rel = p.relative_to(get_recycle_dir())
        except ValueError:
            failed.append({"path": raw, "reason": "不在回收区内"})
            continue
        parts = rel.parts
        if parts and re.match(r"^[A-Za-z]$", parts[0]):
            drive = parts[0] + ":\\"
            rest = parts[1:]
        else:
            drive = ""
            rest = parts
        original = Path(drive).joinpath(*rest)
        try:
            original.parent.mkdir(parents=True, exist_ok=True)
            if original.exists():
                failed.append({"path": raw, "reason": f"原位置已存在文件：{original}"})
                continue
            shutil.copy2(str(p), str(original))
            restored.append({"from": raw, "to": str(original)})
        except Exception as e:
            failed.append({"path": raw, "reason": str(e)})
    return {"success": len(failed) == 0, "restored": len(restored), "failed": failed}


def delete_from_recycle(paths: list[str]) -> dict:
    """清理回收区文件。

    出于安全与可恢复考虑，程序内不直接永久删除文件。
    此接口仅返回引导：由用户在资源管理器中删除回收区文件（系统会移入回收站）。
    """
    return {
        "success": False,
        "deleted": 0,
        "failed": [],
        "recycle_dir": str(get_recycle_dir()),
        "hint": "为安全起见，请在资源管理器中打开回收区文件夹后手动删除（系统会移入回收站，可恢复）。",
    }
