import json
from datetime import date

from app.core.collection_planner import plan_collection, write_ttd_accounts


def account(**overrides):
    data = {
        "record_id": "a1",
        "账号名称": "一号",
        "平台": "抖音",
        "链接": "",
        "sec_user_id": "sec1",
        "等级": 4,
        "标签": "多, 个人",
        "启用": 1,
        "last_collected_at": None,
        "collect_window_days": None,
    }
    data.update(overrides)
    return data


def test_filters_enabled_douyin_accounts_and_sorts_by_rating():
    planned = plan_collection(
        [
            account(record_id="a2", 账号名称="二号", sec_user_id="sec2", 等级=3),
            account(record_id="a1", 等级=4),
            account(
                record_id="a3",
                账号名称="三号",
                sec_user_id="sec3",
                等级=5,
                启用=0,
            ),
            account(record_id="a4", 账号名称="四号", sec_user_id="", 等级=5),
        ],
        rating_min=3,
    )
    assert [item.sec_user_id for item in planned] == ["sec1", "sec2"]
    assert all(item.status == "pending" for item in planned)


def test_tag_and_name_filters():
    planned = plan_collection(
        [
            account(sec_user_id="sec1", 账号名称="一号", 标签="多"),
            account(record_id="a2", 账号名称="二号", sec_user_id="sec2", 标签="个人"),
        ],
        tags=["多"],
    )
    assert [item.sec_user_id for item in planned] == ["sec1"]

    planned = plan_collection(
        [
            account(sec_user_id="sec1", 账号名称="一号"),
            account(record_id="a2", 账号名称="二号", sec_user_id="sec2"),
        ],
        account_names="二号",
    )
    assert [item.sec_user_id for item in planned] == ["sec2"]


def test_first_collection_is_full_and_next_is_incremental_with_overlap():
    today = date(2026, 8, 15)
    first = plan_collection([account()], mode="incremental", today=today)
    assert first[0].earliest == ""

    second = plan_collection(
        [account(last_collected_at="2026-08-15 10:00:00")],
        mode="incremental",
        today=today,
    )
    assert second[0].earliest == "2026/08/14"


def test_fixed_window_takes_precedence_and_full_mode_can_force_full():
    fixed = plan_collection(
        [account(last_collected_at="2026-08-15 10:00:00", collect_window_days=200)],
        mode="incremental",
    )
    assert fixed[0].earliest == 200

    forced_full = plan_collection(
        [account(last_collected_at="2026-08-15 10:00:00")],
        mode="full",
    )
    assert forced_full[0].earliest == ""


def test_tiktok_requires_profile_link_and_douyin_url_is_generated():
    planned = plan_collection(
        [
            account(平台="TikTok", sec_user_id="tiksec", 链接=""),
            account(
                record_id="a2",
                账号名称="二号",
                sec_user_id="tiksec2",
                平台="TikTok",
                链接="https://www.tiktok.com/@two",
            ),
        ],
        platform="tiktok",
    )
    by_id = {item.sec_user_id: item for item in planned}
    assert by_id["tiksec"].status == "skipped"
    assert "主页链接缺失" in by_id["tiksec"].message
    assert by_id["tiksec2"].url == "https://www.tiktok.com/@two"


def test_tiktok_accepts_only_profile_urls_on_tiktok_hosts():
    planned = plan_collection(
        [
            account(
                record_id="a1",
                sec_user_id="profile",
                平台="TikTok",
                链接="https://www.tiktok.com/@valid",
            ),
            account(
                record_id="a2",
                sec_user_id="video",
                平台="TikTok",
                链接="https://www.tiktok.com/@user/video/123",
            ),
            account(
                record_id="a3",
                sec_user_id="offsite",
                平台="TikTok",
                链接="https://example.com/tiktok.com/@user",
            ),
        ],
        platform="tiktok",
    )
    by_id = {item.sec_user_id: item for item in planned}
    assert by_id["profile"].status == "pending"
    assert by_id["profile"].url == "https://www.tiktok.com/@valid"
    assert by_id["video"].status == "skipped"
    assert by_id["offsite"].status == "skipped"


def test_write_ttd_accounts_preserves_unrelated_settings(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text(
        json.dumps(
            {
                "cookie": "preserve",
                "accounts_urls": [{"mark": "old", "url": "old"}],
                "root": "D:/Media",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    planned = plan_collection([account()])
    entries = write_ttd_accounts(settings, "douyin", planned)

    saved = json.loads(settings.read_text(encoding="utf-8"))
    assert saved["cookie"] == "preserve"
    assert saved["root"] == "D:/Media"
    assert entries == saved["accounts_urls"]
    assert entries[0] == {
        "mark": "一号",
        "url": "https://www.douyin.com/user/sec1",
        "tab": "post",
        "earliest": "",
        "latest": "",
        "enable": True,
    }
    assert not list(tmp_path.glob("*.tmp"))
