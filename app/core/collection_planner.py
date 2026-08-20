"""Pure planning and TTD account-list generation for collection batches."""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit


@dataclass
class PlannedAccount:
    record_id: str
    sec_user_id: str
    account_name: str
    platform: str
    mark: str
    url: str
    earliest: str | int
    status: str = "pending"
    message: str = ""


def _tags(value) -> set[str]:
    if isinstance(value, list):
        return {str(item).strip() for item in value if str(item).strip()}
    if not value:
        return set()
    return {
        tag.strip()
        for tag in re.split(r"[,，、\s]+", str(value))
        if tag.strip()
    }


def _last_date(value) -> date | None:
    if not value:
        return None
    normalized = str(value)[:10].replace("/", "-")
    try:
        return datetime.strptime(normalized, "%Y-%m-%d").date()
    except ValueError:
        return None


def _is_tiktok_profile_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in ("http", "https"):
            return False
        if parsed.hostname not in ("tiktok.com", "www.tiktok.com"):
            return False
        parts = parsed.path.strip("/").split("/")
        return len(parts) == 1 and parts[0].startswith("@") and len(parts[0]) > 1
    except ValueError:
        return False


def _earliest_for(row: dict, mode: str) -> str | int:
    window = row.get("collect_window_days")
    if window not in (None, ""):
        try:
            return int(window)
        except (TypeError, ValueError):
            pass
    if mode == "full":
        return ""
    last = _last_date(row.get("last_collected_at"))
    if last is None:
        return ""
    return (last - timedelta(days=1)).strftime("%Y/%m/%d")


def plan_collection(
    accounts: list[dict],
    rating_min: int = 3,
    tags: list[str] | None = None,
    account_names: str = "",
    record_ids: list[str] | None = None,
    platform: str = "douyin",
    mode: str = "incremental",
    today: date | None = None,
    created_after: date | None = None,
    skip_recent_days: int = 0,
) -> list[PlannedAccount]:
    wanted_tags = set(tags or [])
    # account_names 字段现在存的是 sec_user_id 列表（逗号分隔）
    sec_user_ids = {
        uid.strip()
        for uid in re.split(r"[,，\n]+", account_names or "")
        if uid.strip()
    }
    ids = set(record_ids or [])
    expected_platform = "抖音" if platform == "douyin" else "TikTok"
    result: list[PlannedAccount] = []

    candidates = [
        row
        for row in accounts
        if row.get("平台") == expected_platform
        and row.get("启用")
        and str(row.get("sec_user_id") or "").strip()
    ]
    if sec_user_ids:
        candidates = [row for row in candidates if row.get("sec_user_id") in sec_user_ids]
    if ids:
        candidates = [row for row in candidates if row.get("record_id") in ids]
    if wanted_tags:
        candidates = [row for row in candidates if _tags(row.get("标签")) & wanted_tags]
    candidates = [
        row for row in candidates if int(row.get("等级") or 0) >= rating_min
    ]
    # 账号创建时间筛选：只保留录入日期 >= created_after 的
    if created_after:
        candidates = [
            row for row in candidates
            if _last_date(row.get("created_at")) and
               _last_date(row.get("created_at")) >= created_after
        ]
    # 跳过最近 N 天内采过的账号
    if skip_recent_days > 0:
        cutoff = (today or date.today()) - timedelta(days=skip_recent_days)
        candidates = [
            row for row in candidates
            if not _last_date(row.get("last_collected_at"))
            or _last_date(row.get("last_collected_at")) < cutoff
        ]
    candidates.sort(
        key=lambda row: (-int(row.get("等级") or 0), str(row.get("账号名称") or ""))
    )

    for row in candidates:
        sec_user_id = str(row["sec_user_id"]).strip()
        name = str(row.get("账号名称") or sec_user_id)
        url = str(row.get("链接") or "").strip()
        status = "pending"
        message = ""
        if platform == "douyin":
            url = f"https://www.douyin.com/user/{sec_user_id}"
        elif not _is_tiktok_profile_url(url):
            status = "skipped"
            message = "TikTok 主页链接缺失"
            url = ""
        result.append(
            PlannedAccount(
                record_id=str(row.get("record_id") or ""),
                sec_user_id=sec_user_id,
                account_name=name,
                platform=platform,
                mark=name,
                url=url,
                earliest=_earliest_for(row, mode),
                status=status,
                message=message,
            )
        )
    return result


def write_ttd_accounts(
    settings_path: Path,
    platform: str,
    planned: list[PlannedAccount],
    folder_name: str = "",
    name_format: str = "",
) -> list[dict]:
    key = "accounts_urls" if platform == "douyin" else "accounts_urls_tiktok"
    entries = [
        {
            "mark": item.mark,
            "url": item.url,
            "tab": "post",
            "earliest": item.earliest,
            "latest": "",
            "enable": True,
        }
        for item in planned
        if item.status == "pending"
    ]
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    if settings_path.exists():
        with settings_path.open("r", encoding="utf-8") as file:
            settings = json.load(file)
    else:
        settings = {}
    settings[key] = entries
    # 覆写采集设置（空值不覆写，保留 TTD 原始默认）
    if folder_name:
        settings["folder_name"] = folder_name
    if name_format:
        settings["name_format"] = name_format

    temporary = settings_path.with_name(f"{settings_path.name}.doukhub.tmp")
    with temporary.open("w", encoding="utf-8") as file:
        json.dump(settings, file, ensure_ascii=False, indent=4)
        file.flush()
        os.fsync(file.fileno())
    os.replace(temporary, settings_path)
    return entries
