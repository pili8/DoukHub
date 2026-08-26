"""存储方案模块 v3（多方案列表 + 主/次标记，2026-08-23）

单作品 / 增量采集各维护一份方案列表，每个方案携带该采集类型的完整设置：
- single 方案：{ id, name, path(下载路径), name_format(命名模板，可空=继承默认) }
- batch 方案：额外携带引擎参数 folder_mode/music/dynamic_cover/static_cover/max_size/storage_format
- 每个方案可标记 role：
    primary   = 主方案（auto 模式首选）
    secondary = 次方案（主不可用时的故障转移）
    其余方案 = 自由指定使用（不参与 auto 自动切换）

使用方式 choice：
- "auto"          ：主方案不可用 → 自动切次方案
- "p:<id>"        ：强制使用指定方案（不切换）
- "primary"/"secondary"：快捷指向主/次方案（等价 p:<id>，兼容旧值）

迁移链：v1（profiles 列表 + use）→ v3；v2（primary/secondary 两槽）→ v3。
"""
import logging
import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

logger = logging.getLogger("doukhub.storage_profiles")

DEFAULT_SINGLE_NFMT = "{create_time} {author} {title}"
DEFAULT_BATCH_NFMT = "create_time type nickname desc"

# batch 方案携带的引擎级参数（无值时的全局兜底）
ENGINE_KEYS = ("folder_mode", "music", "dynamic_cover", "static_cover", "max_size", "storage_format", "max_pages")


def _new_id() -> str:
    return f"sp_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}"


def _norm_profile(raw: dict | None, scope: str, is_primary: bool = False, is_secondary: bool = False) -> dict | None:
    if not isinstance(raw, dict):
        return None
    path = str(raw.get("path") or "").strip()
    if not path and not str(raw.get("name") or "").strip():
        return None  # 既无路径又无名称的空壳不入库；仅有名称的作为草稿保留
    role = str(raw.get("role") or "").strip()
    if role not in ("primary", "secondary"):
        role = "primary" if is_primary else ("secondary" if is_secondary else "")
    p = {
        "id": str(raw.get("id") or _new_id()),
        "name": (str(raw.get("name") or "").strip() or "未命名")[:60],
        "path": path,
        "name_format": str(raw.get("name_format") or "").strip(),
        "role": role,
        "enabled": bool(raw.get("enabled", True)),
    }
    if scope == "batch":
        for k in ENGINE_KEYS:
            v = raw.get(k)
            if k in ("max_size", "max_pages"):
                p[k] = int(v) if v else 0
            elif k == "storage_format":
                p[k] = str(v or "")
            else:
                p[k] = bool(v)
    return p


def ensure_migrated(config) -> dict:
    """确保 storage_profiles 为 v3 结构；从 v1/v2 迁移。返回完整 state。"""
    state = config.storage_profiles
    if not isinstance(state, dict):
        state = {}
    dirty = False

    for scope, fallback_nfmt in (("single", DEFAULT_SINGLE_NFMT), ("batch", DEFAULT_BATCH_NFMT)):
        item = state.get(scope)
        if not isinstance(item, dict):
            item = None
        profiles = item.get("profiles") if item else None

        # v2：primary/secondary 两槽 → v3 列表（注意：_merge_defaults 可能已补 profiles=[]，空列表也触发）
        if not profiles and item and (item.get("primary") is not None or item.get("secondary") is not None):
            v2_profiles = []
            pr = item.get("primary")
            if isinstance(pr, dict) and (pr.get("path") or "").strip():
                v2_profiles.append({**pr, "role": "primary", "enabled": True})
            se = item.get("secondary")
            if isinstance(se, dict) and (se.get("path") or "").strip():
                v2_profiles.append({**se, "role": "secondary", "enabled": True})
            legacy = item.get("legacy_profiles") or []
            for p in legacy:
                if isinstance(p, dict) and (p.get("path") or "").strip():
                    v2_profiles.append({**p, "role": "", "enabled": bool(p.get("enabled", True))})
            item = {
                "default_name_format": item.get("default_name_format") or fallback_nfmt,
                "profiles": v2_profiles,
            }
            state[scope] = item
            dirty = True

        profiles = item.get("profiles") if item else None
        if profiles is None:
            # 全新初始化
            if scope == "single":
                global_dir = (config.single_work.get("download_path") or "").strip()
            else:
                global_dir = (config.local.get("download_path") or "").strip()
            item = {
                "default_name_format": fallback_nfmt,
                "profiles": [] if not global_dir else [{
                    "id": _new_id(), "name": "默认方案", "path": global_dir,
                    "name_format": "", "role": "primary", "enabled": True,
                }],
            }
            state[scope] = item
            dirty = True

    # v1 结构兼容：profiles 元素补充 role（迁移后 id 保留），确保主次标记
    for scope in ("single", "batch"):
        item = state.get(scope) or {}
        profiles = item.get("profiles")
        if not isinstance(profiles, list):
            continue
        changed = False
        # 第一遍：把 v1 的 use=p:<id> 指定方案提为主方案
        use = item.get("use") or "auto"
        if use.startswith("p:") and not any(p.get("role") == "primary" for p in profiles if isinstance(p, dict)):
            target_id = use[2:]
            for p in profiles:
                if isinstance(p, dict) and p.get("id") == target_id:
                    p["role"] = "primary"
                    changed = True
                    break
        # 未标记主/次的方案：第一个可用作主，第二个可用作次
        usable = [p for p in profiles if isinstance(p, dict) and (p.get("path") or "").strip()]
        have_primary = any(p.get("role") == "primary" for p in usable)
        if not have_primary and usable:
            usable[0]["role"] = "primary"
            changed = True
        have_secondary = any(p.get("role") == "secondary" for p in usable)
        if not have_secondary and len(usable) > 1:
            for p in usable:
                if p.get("role") != "primary":
                    p["role"] = "secondary"
                    changed = True
                    break
        if changed:
            dirty = True

    if dirty:
        config.set("storage_profiles", state)
        config.save()
    return state


# ---------- 探测 ----------

def check_path(path_str: str, timeout: float = 3.0) -> tuple[bool, str]:
    """探测路径可用性：本地=目录存在且可写；远程 UNC/SMB=带超时可达性检测。
    返回 (ok, reason)。"""
    path_str = (path_str or "").strip()
    if not path_str:
        return False, "路径为空"

    def _probe(raw: str) -> tuple[bool, str]:
        try:
            p = Path(raw).expanduser()
        except Exception as exc:  # 非法路径
            return False, f"路径格式无效: {exc}"
        try:
            p.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return False, f"目录不可创建: {exc.strerror or exc}"
        try:
            if not p.is_dir():
                return False, "不是有效目录"
        except OSError as exc:
            return False, f"目录不可访问: {exc.strerror or exc}"
        probe = p / f".doukhub_probe_{os.getpid()}"
        try:
            probe.write_text("ok", encoding="utf-8")
            probe.unlink(missing_ok=True)
            return True, "可用"
        except OSError as exc:
            return False, f"目录不可写: {exc.strerror or exc}"
        except Exception as exc:
            return False, str(exc)

    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(_probe, path_str)
            return future.result(timeout=timeout)
    except TimeoutError:
        return False, "连接超时（目标可能离线）"
    except Exception as exc:
        return False, str(exc)


def check_profiles(profiles: list[dict], timeout: float = 3.0) -> dict[str, dict]:
    """批量探测，返回 {id: {ok, reason}}"""
    return {
        p["id"]: {"ok": ok, "reason": reason}
        for p in profiles
        for ok, reason in [check_path(p.get("path", ""), timeout)]
    }


# ---------- 生效方案解析 ----------

def get_profiles(state_item: dict) -> list[dict]:
    """返回启用且有路径的方案列表（含 role 标记）。"""
    out = []
    for p in (state_item or {}).get("profiles") or []:
        if not isinstance(p, dict):
            continue
        if not p.get("enabled", True):
            continue
        if not (p.get("path") or "").strip():
            continue
        out.append(p)
    return out


def _probe_item(p: dict) -> dict:
    ok, reason = check_path(p.get("path", ""))
    return {
        "id": p.get("id", ""),
        "name": p.get("name", ""),
        "path": p.get("path", ""),
        "role": p.get("role", ""),
        "ok": ok,
        "reason": reason,
    }


def _find_by_role(profiles: list[dict], role: str) -> dict | None:
    return next((p for p in profiles if p.get("role") == role), None)


def resolve_pair(config, scope: str, primary_id: str = "", secondary_id: str = "") -> tuple[dict | None, list[dict]]:
    """按「主/次方案 ID」解析生效方案（v4 交互：采集入口双下拉）。

    primary_id / secondary_id 为空时，回落到设置页的默认主/次角色。
    执行：探测主 → 可用即用（整套设置）；不可用探测次 → 可用即用；都不行返回 None。
    """
    ensure_migrated(config)
    profiles = get_profiles(config.storage_profiles.get(scope) or {})

    def by_id(pid):
        return next((p for p in profiles if p.get("id") == pid), None) if pid else None

    primary = by_id(primary_id) or _find_by_role(profiles, "primary") or (profiles[0] if profiles else None)
    secondary = by_id(secondary_id) or _find_by_role(profiles, "secondary")

    diags: list[dict] = []
    if primary:
        d = _probe_item({**primary, "_slot": "primary"})
        diags.append(d)
        if d["ok"]:
            return primary, diags
    else:
        diags.append({"id": "", "name": "主方案", "path": "", "role": "primary",
                      "ok": False, "reason": "未配置主方案"})
    if secondary:
        d = _probe_item({**secondary, "_slot": "secondary"})
        diags.append(d)
        if d["ok"]:
            return secondary, diags
    elif primary:
        diags.append({"id": "", "name": "次方案", "path": "", "role": "secondary",
                      "ok": False, "reason": "未配置次方案（主方案也不可用）"})
    return None, diags


def resolve_active_profile(config, scope: str, choice: str = "auto") -> tuple[dict | None, list[dict]]:
    """解析当前生效的存储方案。

    scope: "single" | "batch"
    choice: "auto"（主→次故障转移）| "p:<id>"（指定方案）| "primary" | "secondary"
    返回 (profile, diagnostics)；profile 为 None 表示不可用或未配置（调用方应报错停止）。
    """
    ensure_migrated(config)
    profiles = get_profiles(config.storage_profiles.get(scope) or {})
    choice = (choice or "auto").strip().lower()
    diags: list[dict] = []

    if choice.startswith("p:"):
        target_id = choice[2:]
        target = next((p for p in profiles if p.get("id") == target_id), None)
        if target is None:
            return None, [{
                "id": target_id, "name": "指定方案", "path": "",
                "ok": False, "reason": "指定方案不存在或未启用",
            }]
        d = _probe_item(target)
        diags.append(d)
        return (target if d["ok"] else None), diags

    if choice == "primary":
        target = _find_by_role(profiles, "primary")
        if target is None:
            return None, [{"id": "", "name": "主方案", "path": "", "role": "primary",
                           "ok": False, "reason": "未设置主方案"}]
        d = _probe_item(target)
        diags.append(d)
        return (target if d["ok"] else None), diags

    if choice == "secondary":
        target = _find_by_role(profiles, "secondary")
        if target is None:
            return None, [{"id": "", "name": "次方案", "path": "", "role": "secondary",
                           "ok": False, "reason": "未设置次方案"}]
        d = _probe_item(target)
        diags.append(d)
        return (target if d["ok"] else None), diags

    # auto：主 → 次
    candidates = []
    primary = _find_by_role(profiles, "primary")
    if primary:
        candidates.append(("primary", primary))
    secondary = _find_by_role(profiles, "secondary")
    if secondary:
        candidates.append(("secondary", secondary))
    if not candidates:
        # 无主无次：按列表顺序逐个探测（兼容，取第一个可用）
        for p in profiles:
            candidates.append(("auto", p))
    if not candidates:
        return None, [{
            "id": "none", "name": "存储方案", "path": "",
            "ok": False, "reason": "未配置任何可用方案",
        }]
    for slot, p in candidates:
        d = _probe_item({**p, "_slot": slot})
        diags.append(d)
        if d["ok"]:
            return p, diags
    return None, diags


def resolve_name_format(config, scope: str, profile: dict | None = None) -> str:
    """解析命名模板：方案级 → 该套默认 → 系统兜底。"""
    state = config.storage_profiles.get(scope) or {}
    if profile and (profile.get("name_format") or "").strip():
        return profile["name_format"].strip()
    default = (state.get("default_name_format") or "").strip()
    if default:
        return default
    return DEFAULT_SINGLE_NFMT if scope == "single" else DEFAULT_BATCH_NFMT


def resolve_engine_params(config, scope: str, profile: dict | None = None) -> dict:
    """解析增量引擎参数（batch 方案内字段 → 全局 defaults 兜底）。single 返回 None。"""
    if scope != "batch":
        return None
    d = config.collection_defaults
    out = {}
    for k in ENGINE_KEYS:
        v = profile.get(k) if profile else None
        if v is None:
            v = d.get(k, 0 if k in ("max_size", "max_pages") else False)
        out[k] = v
    return out


# ---------- 保存 ----------

def save_state(config, payload: dict) -> dict:
    """保存两套存储方案 state（由设置页整体提交）。payload: {single: {...}, batch: {...}}
    item.profiles: 方案列表（role 标记主/次）。
    无路径方案作为草稿入库（不参与执行/探测）；被动转正（自动补主/次）记录在 _auto_promoted。"""
    state = ensure_migrated(config)
    auto_promoted: list[dict] = []
    for scope in ("single", "batch"):
        item = payload.get(scope)
        if not isinstance(item, dict):
            continue
        default_nfmt = str(item.get("default_name_format") or "").strip()
        if not default_nfmt:
            default_nfmt = DEFAULT_SINGLE_NFMT if scope == "single" else DEFAULT_BATCH_NFMT

        raw_profiles = item.get("profiles")
        if not isinstance(raw_profiles, list):
            continue
        profiles = []
        for rp in raw_profiles:
            if not isinstance(rp, dict):
                continue
            n = _norm_profile(rp, scope)
            if n:
                profiles.append(n)  # 草稿（无路径）也入库

        # 仅对有路径且启用的方案补主/次（草稿/停用方案不参与角色分配）
        usable = [p for p in profiles if (p.get("path") or "").strip() and p.get("enabled", True)]
        # 交换冲突：主/次重复 → 仅保留第一个 primary，其余降级（记录降级者，不参与本轮补次）
        seen_primary = False
        demoted_ids: set[str] = set()
        for p in profiles:
            if p.get("role") == "primary":
                if seen_primary:
                    p["role"] = ""
                    demoted_ids.add(p.get("id"))
                seen_primary = True

        old_item = (ensure_migrated(config) or {}).get(scope) or {}
        old_by_id = {p.get("id"): p for p in (old_item.get("profiles") or []) if isinstance(p, dict)}

        def _promote(p, role):
            p["role"] = role
            if p.get("id") and old_by_id.get(p.get("id"), {}).get("role") != role:
                auto_promoted.append({"scope": scope, "id": p.get("id"), "name": p.get("name") or "未命名方案", "role": role})

        # 校验主方案：全列表无主（停用/草稿方案的徽标不算数）时，才由第一个可用方案转正
        if not any(p.get("role") == "primary" for p in profiles) and usable:
            _promote(usable[0], "primary")
        # 次方案：仅当用户从未设置过次方案且可用方案不止一个时，自动补一个。
        # 本轮因主冲突被降级的方案不自动转次（用户意图是取消其角色，不是换角色）
        old_has_secondary = any(pp.get("role") == "secondary" for pp in old_by_id.values())
        if not old_has_secondary and not any(p.get("role") == "secondary" for p in profiles) and len(usable) > 1:
            for p in usable:
                if p.get("role") != "primary" and p.get("id") not in demoted_ids:
                    _promote(p, "secondary")
                    break

        state[scope] = {
            "default_name_format": default_nfmt,
            "profiles": profiles,
        }
    _sync_legacy_paths(config, state)
    config.set("storage_profiles", state)
    config.save()
    if auto_promoted:
        state = dict(state)
        state["_auto_promoted"] = auto_promoted
    return state


def update_profile(config, scope: str, profile_id: str, patch: dict) -> dict | None:
    """就地更新单个存储方案（采集页「✎ 编辑命名」等场景）。
    只接受白名单字段，合并到现有方案后重新规范化；未找到返回 None。
    注意：路径为空时不删除方案，而是保留原路径。"""
    if scope not in ("single", "batch") or not isinstance(patch, dict):
        return None
    state = ensure_migrated(config)
    item = state.get(scope)
    if not isinstance(item, dict):
        return None
    profiles = item.get("profiles")
    if not isinstance(profiles, list):
        return None
    idx = next((i for i, p in enumerate(profiles)
                if isinstance(p, dict) and p.get("id") == profile_id), None)
    if idx is None:
        return None

    existing = dict(profiles[idx])
    allowed = {"name", "path", "name_format", "role", "enabled"}
    if scope == "batch":
        allowed |= set(ENGINE_KEYS)
    for k in allowed:
        if k in patch:
            existing[k] = patch[k]

    # 路径为空 -> 保留原路径（不删方案）
    if not str(existing.get("path") or "").strip():
        existing["path"] = profiles[idx].get("path") or ""

    merged = _norm_profile(existing, scope)
    if not merged:
        return None
    profiles[idx] = merged

    # 角色冲突：与 save_state 对齐（仅去重 primary）
    seen_primary = False
    for p in profiles:
        if p.get("role") == "primary":
            if seen_primary:
                p["role"] = ""
            seen_primary = True

    item["profiles"] = profiles
    state[scope] = item
    _sync_legacy_paths(config, state)
    config.set("storage_profiles", state)
    config.save()
    return merged


def _sync_legacy_paths(config, state: dict) -> None:
    """把各套主方案路径同步到旧字段，保证旧代码兜底可用。"""
    for scope, cfg_path in (("single", "single_work.download_path"),
                            ("batch", "local.download_path")):
        profiles = get_profiles(state.get(scope) or {})
        primary = _find_by_role(profiles, "primary") or (profiles[0] if profiles else None)
        path = (primary.get("path") or "").strip() if primary else ""
        if path:
            config.set(cfg_path, path)