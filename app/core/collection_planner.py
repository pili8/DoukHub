"""Pure planning and TTD account-list generation for collection batches."""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path


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
) -> list[PlannedAccount]:
    wanted_tags = set(tags or [])
    names = {
        name.strip()
        for name in re.split(r"[,，\n]+", account_names or "")
        if name.strip()
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
    if names:
        candidates = [row for row in candidates if row.get("账号名称") in names]
    if ids:
        candidates = [row for row in candidates if row.get("record_id") in ids]
    if wanted_tags:
        candidates = [row for row in candidates if _tags(row.get("标签")) & wanted_tags]
    candidates = [
        row for row in candidates if int(row.get("等级") or 0) >= rating_min
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
        elif "tiktok.com/" not in url:
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

    temporary = settings_path.with_name(f"{settings_path.name}.doukhub.tmp")
    with temporary.open("w", encoding="utf-8") as file:
        json.dump(settings, file, ensure_ascii=False, indent=4)
        file.flush()
        os.fsync(file.fileno())
    os.replace(temporary, settings_path)
    return entries
