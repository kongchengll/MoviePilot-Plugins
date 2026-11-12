"""
下载监控的纯逻辑测试。

按宿主约定使用生产命名空间 `app.plugins.<plugin_id>` 导入，避免同一源码被加载成两个模块。
"""

from __future__ import annotations

import pytest

from app.plugins.downloadwatch.config import (
    DEFAULT_CONFIG,
    merge_exclude,
    normalize_config,
    parse_episodes,
    parse_season,
    remove_blacklist_item,
    split_tags,
    tags_match,
    task_tags,
)
from app.plugins.downloadwatch.monitor import (
    REASON_BLACKLIST,
    REASON_FINISHED,
    REASON_FORCE_TIMEOUT,
    REASON_PROGRESSING,
    REASON_REAP,
    REASON_TOO_YOUNG,
    compose_notify,
    evaluate,
    evaluate_force,
    format_duration,
    format_size,
    is_downloading,
    is_finished,
    progress_text,
    reason_text,
)


def test_finished_task_never_reaped():
    """
    已完成任务即使等待很久也永远不删。
    """
    decision = evaluate(progress=100, age_seconds=10 * 86400, max_hours=1, min_progress=50)
    assert decision.reap is False
    assert decision.reason == REASON_FINISHED


def test_near_finished_counts_as_finished():
    """
    进度 99.95 视为已完成，避免卡在最后一点被误删。
    """
    assert is_finished(99.95) is True
    assert is_finished(99.0) is False


def test_first_sight_is_not_reaped():
    """
    首次观测只记录时间，本轮不判定。
    """
    decision = evaluate(progress=0, age_seconds=None, max_hours=24, min_progress=5)
    assert decision.reap is False
    assert decision.reason == REASON_TOO_YOUNG


def test_young_task_kept():
    """
    未达到时长阈值时保留任务。
    """
    decision = evaluate(progress=0, age_seconds=3600, max_hours=24, min_progress=5)
    assert decision.reap is False
    assert decision.reason == REASON_TOO_YOUNG


def test_progressing_task_kept():
    """
    进度已经超过阈值说明任务在动，不删。
    """
    decision = evaluate(progress=30, age_seconds=48 * 3600, max_hours=24, min_progress=5)
    assert decision.reap is False
    assert decision.reason == REASON_PROGRESSING


def test_boundary_both_conditions_inclusive():
    """
    时长恰好等于阈值、进度恰好等于阈值时判定删除（≥ 与 ≤ 都是闭区间）。
    """
    decision = evaluate(progress=5, age_seconds=24 * 3600, max_hours=24, min_progress=5)
    assert decision.reap is True
    assert decision.reason == REASON_REAP


def test_one_second_before_threshold_kept():
    """
    差一秒没到阈值也不删，保证边界不吃掉任务。
    """
    decision = evaluate(progress=0, age_seconds=24 * 3600 - 1, max_hours=24, min_progress=5)
    assert decision.reap is False


def test_zero_max_hours_treated_as_one_hour():
    """
    阈值非法为 0 时按 1 小时兜底，避免刚加任务就被秒删。
    """
    decision = evaluate(progress=0, age_seconds=1800, max_hours=0, min_progress=5)
    assert decision.reap is False


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (0, "不到 1 分钟"),
        (59, "不到 1 分钟"),
        (60, "1 分钟"),
        (3599, "59 分钟"),
        (3600, "1.0 小时"),
        (86400, "1.0 天"),
        (None, "不到 1 分钟"),
    ],
)
def test_format_duration(seconds, expected):
    """
    时长展示覆盖分钟、小时、天与空值。
    """
    assert format_duration(seconds) == expected


def test_progress_text_clamped():
    """
    进度文本限制在 0-100 之间并保留一位小数。
    """
    assert progress_text(12.34) == "12.3%"
    assert progress_text(-5) == "0.0%"
    assert progress_text(120) == "100.0%"


def test_tags_match_without_filter():
    """
    未配置监控标签时全部任务都算命中。
    """
    assert tags_match("MOVIEPILOT", []) is True
    assert tags_match(None, []) is True


def test_tags_match_case_insensitive():
    """
    标签匹配忽略大小写，并支持下载器返回的逗号分隔多标签。
    """
    assert tags_match("MOVIEPILOT, 追更", ["追更"]) is True
    assert tags_match("moviepilot", ["MOVIEPILOT"]) is True
    assert tags_match("MOVIEPILOT", ["追更"]) is False


def test_task_tags_normalization():
    """
    标签文本按中英文逗号与顿号拆分并转小写。
    """
    assert task_tags("A，b、C") == {"a", "b", "c"}
    assert task_tags(None) == set()


def test_split_tags_accepts_list():
    """
    表单既可能传字符串也可能传数组，都要能拆开。
    """
    assert split_tags("电影, 剧集") == ["电影", "剧集"]
    assert split_tags(["电影", " 剧集 "]) == ["电影", "剧集"]
    assert split_tags("") == []


def test_merge_exclude_appends_branch():
    """
    订阅排除字段整体是一条正则，追加时用 `|` 拼接而不是换行。
    """
    assert merge_exclude("", "Some.Show.2024.1080p") == r"Some\.Show\.2024\.1080p"
    assert merge_exclude("XXX", "YYY") == r"XXX|YYY"
    # 已有尾部竖线时不重复
    assert merge_exclude("XXX|", "YYY") == r"XXX|YYY"


def test_merge_exclude_is_idempotent():
    """
    同一标题重复合并不会让规则串无限变长。
    """
    once = merge_exclude("XXX", "YYY")
    assert merge_exclude(once, "YYY") == once


def test_parse_season():
    """
    从季集文本解析季号，无法识别时返回 None。
    """
    assert parse_season("S02E05") == 2
    assert parse_season("s12") == 12
    assert parse_season("") is None
    assert parse_season(None) is None


def test_normalize_config_defaults_and_bounds():
    """
    非法配置回落到默认值，数值阈值被限制在合理范围内。
    """
    config = normalize_config(None)
    assert config["enabled"] is False
    assert config["max_hours"] == DEFAULT_CONFIG["max_hours"]
    assert config["cron"] == DEFAULT_CONFIG["cron"]

    clamped = normalize_config({"max_hours": 0, "min_progress": 200, "cron": "  "})
    assert clamped["max_hours"] == 1
    assert clamped["min_progress"] == 99
    assert clamped["cron"] == DEFAULT_CONFIG["cron"]


def test_normalize_config_bool_from_string():
    """
    表单可能以字符串提交布尔值。
    """
    config = normalize_config({"enabled": "true", "delete_files": "1", "dry_run": "false"})
    assert config["enabled"] is True
    assert config["delete_files"] is True
    assert config["dry_run"] is False


def test_blacklist_reason_constant_distinct():
    """
    黑名单原因与阈值原因需要区分，通知里才能看出是"再次出现"还是"第一次超时"。
    """
    assert REASON_BLACKLIST != REASON_REAP


def test_format_size():
    """
    容量展示按 1024 进制进位，未知大小给占位文本。
    """
    assert format_size(0) == "未知"
    assert format_size(None) == "未知"
    assert format_size(860) == "860 B"
    assert format_size(1536) == "1.5 KB"
    assert format_size(2 * 1024 ** 3) == "2.0 GB"
    assert format_size(3 * 1024 ** 4) == "3.0 TB"


def test_reason_text():
    """
    判定原因要能翻译成页面与通知里的短语。
    """
    assert reason_text(REASON_REAP) == "超时且进度停滞"
    assert reason_text(REASON_BLACKLIST) == "命中黑名单"
    assert reason_text(REASON_FINISHED) == "已完成"
    assert reason_text(REASON_PROGRESSING) == "进度正常"
    assert reason_text("unknown") == "观察中"


def test_compose_notify_returns_none_without_events():
    """
    没有任何事件时不产生通知，避免每轮扫描都推空消息。
    """
    assert compose_notify(
        watched=[], removed=[], watched_total=0, blacklist_total=0, delete_files=False
    ) is None


def test_compose_notify_watch_only():
    """
    只有新增监控时，标题与正文只包含监控段。
    """
    draft = compose_notify(
        watched=["A · 进度 0.0%"], removed=[], watched_total=3,
        blacklist_total=1, delete_files=False,
    )
    assert draft is not None
    assert draft.title == "下载监控：新增监控 1 个任务"
    assert "【新增监控】1 个任务" in draft.text
    assert "已删除" not in draft.text
    assert "当前监控中 3 个任务，黑名单 1 条。" in draft.text


def test_compose_notify_merges_both_sections():
    """
    同一轮既有删除又有新增监控时合并成一条通知，删除段在前。
    """
    draft = compose_notify(
        watched=["A"], removed=["B", "C"], watched_total=1,
        blacklist_total=0, delete_files=True,
    )
    assert draft is not None
    assert draft.title == "下载监控：删除 2 个任务｜新增监控 1 个任务"
    assert draft.text.startswith("【已删除】2 个任务（删除文件：是）")
    assert "【新增监控】1 个任务" in draft.text


def test_compose_notify_truncates_long_sections():
    """
    单段最多列举 limit 行，其余折叠成数量，避免通知过长。
    """
    draft = compose_notify(
        watched=[], removed=[f"任务{i}" for i in range(25)],
        watched_total=0, blacklist_total=0, delete_files=False, limit=10,
    )
    assert draft is not None
    assert "其余 15 条已省略" in draft.text
    assert draft.text.count("· ") == 11


def test_notify_switches_default_on():
    """
    监控/删除通知默认开启，总开关独立控制。
    """
    config = normalize_config({"notify": False})
    assert config["notify"] is False
    assert config["notify_watch"] is True
    assert config["notify_remove"] is True

    config = normalize_config({"notify_watch": "0", "notify_remove": "true"})
    assert config["notify_watch"] is False
    assert config["notify_remove"] is True


def test_is_downloading_accepts_only_running_tasks():
    """
    只把宿主归一后的 downloading 视为正在下载；暂停与已完成任务不参与监控。
    """
    assert is_downloading("downloading") is True
    assert is_downloading(" Downloading ") is True
    # 老版本投影可能不给 state，按正在下载兜底
    assert is_downloading(None) is True
    assert is_downloading("") is True
    assert is_downloading("paused") is False
    assert is_downloading("completed") is False


def test_validate_tab_falls_back_to_default():
    """
    分页键只接受四个合法值，非法输入回落到「监控中」，避免持久化脏数据。
    """
    from app.plugins.downloadwatch import DownloadWatch

    assert DownloadWatch._validate_tab("watched") == "watched"
    assert DownloadWatch._validate_tab(" REMOVED ") == "removed"
    assert DownloadWatch._validate_tab("blacklist") == "blacklist"
    assert DownloadWatch._validate_tab("detail") == "detail"
    assert DownloadWatch._validate_tab("../../etc/passwd") == "watched"
    assert DownloadWatch._validate_tab(None) == "watched"


def test_parse_episodes_handles_host_history_text():
    """
    下载历史用 Exx 文本记录集数，种子标题用 SxxExx，两种写法都要能还原集号。
    """
    assert parse_episodes("E01") == [1]
    assert parse_episodes("E01-E03") == [1, 2, 3]
    assert parse_episodes("E01,E05") == [1, 5]
    assert parse_episodes("S02E07") == [7]
    assert parse_episodes("第03集") == []
    assert parse_episodes(None) == []
    assert parse_episodes("") == []


def test_parse_episodes_ignores_absurd_range():
    """
    跨度异常的区间只取起点，避免把版本号一类的数字展开成几百集。
    """
    assert parse_episodes("E01-E900") == [1]


def test_remove_blacklist_item_matches_hash_case_insensitively():
    """
    下载器返回的 hash 大小写不统一，移除时按小写比对，且只删命中的一条。
    """
    blacklist = [
        {"hash": "AAA111", "title": "Bad.One.1080p"},
        {"hash": "bbb222", "title": "Bad.Two.1080p"},
    ]
    kept, removed = remove_blacklist_item(blacklist, hash_value="aaa111")
    assert [item["hash"] for item in removed] == ["AAA111"]
    assert [item["hash"] for item in kept] == ["bbb222"]


def test_remove_blacklist_item_falls_back_to_title():
    """
    老记录可能没有 hash，此时按标题全等匹配，避免误删同名不同版本之外的条目。
    """
    blacklist = [
        {"hash": "", "title": "Bad.One.1080p"},
        {"hash": "", "title": "Bad.One.1080p.EXTENDED"},
    ]
    kept, removed = remove_blacklist_item(blacklist, title=" Bad.One.1080p ")
    assert len(removed) == 1 and removed[0]["title"] == "Bad.One.1080p"
    assert [item["title"] for item in kept] == ["Bad.One.1080p.EXTENDED"]


def test_remove_blacklist_item_requires_identifier():
    """
    既没有 hash 也没有标题时不得改动黑名单，防止误清空。
    """
    blacklist = [{"hash": "aaa111", "title": "Bad.One.1080p"}]
    kept, removed = remove_blacklist_item(blacklist, hash_value="", title="")
    assert removed == []
    assert len(kept) == 1


def test_remove_blacklist_item_ignores_dirty_entries():
    """
    持久化数据可能混入非字典条目，移除时直接丢弃，不抛异常。
    """
    kept, removed = remove_blacklist_item(
        [{"hash": "aaa111", "title": "Bad"}, "oops", None], hash_value="zzz"
    )
    assert removed == []
    assert kept == [{"hash": "aaa111", "title": "Bad"}]


def test_force_timeout_reaps_slow_but_active_task():
    """
    强制规则不看进度：满时限仍未完成就清理，兜住「一直在动但下不完」的慢种。
    """
    decision = evaluate_force(progress=80, age_seconds=25 * 3600, force_hours=24)
    assert decision.reap is True
    assert decision.reason == REASON_FORCE_TIMEOUT


def test_force_timeout_protects_finished_and_young_tasks():
    """
    已完成、刚开始观察、未满时限的任务都不清理。
    """
    assert evaluate_force(progress=99.95, age_seconds=25 * 3600, force_hours=24).reason == REASON_FINISHED
    assert evaluate_force(progress=80, age_seconds=None, force_hours=24).reason == REASON_TOO_YOUNG
    assert evaluate_force(
        progress=80, age_seconds=24 * 3600 - 1, force_hours=24
    ).reason == REASON_TOO_YOUNG
    assert evaluate_force(progress=80, age_seconds=26 * 3600, force_hours=48).reap is False


def test_force_timeout_hour_has_a_floor():
    """
    非法或为零的时长回落到 1 小时，避免每轮扫描把刚加的任务全删掉。
    """
    assert evaluate_force(progress=0, age_seconds=30 * 60, force_hours=0).reap is False
    assert evaluate_force(progress=0, age_seconds=2 * 3600, force_hours=0).reap is True


def test_force_timeout_reason_is_readable():
    assert reason_text(REASON_FORCE_TIMEOUT) == "满时限未完成（强制清理）"


def test_compose_notify_explains_mixed_delete_file_rules():
    """
    普通规则不删文件、强制规则删文件时，通知要说明口径而不是笼统写「否」。
    """
    plain = compose_notify(
        watched=[], removed=["a"], watched_total=0, blacklist_total=0, delete_files=False
    )
    assert plain.text.startswith("【已删除】1 个任务（删除文件：否）")
    mixed = compose_notify(
        watched=[], removed=["a"], watched_total=0, blacklist_total=0,
        delete_files=False, force_delete=True,
    )
    assert "按规则（强制超时任务连本地文件删）" in mixed.text


def test_default_config_enables_force_timeout_with_file_deletion():
    """
    满 24H 强制清理默认开启，且默认连本地文件一起删。
    """
    assert DEFAULT_CONFIG["force_timeout"] is True
    assert DEFAULT_CONFIG["force_hours"] == 24
    assert DEFAULT_CONFIG["force_delete_files"] is True
    normalized = normalize_config({"force_hours": 0, "force_delete_files": "false"})
    assert normalized["force_hours"] == 1
    assert normalized["force_delete_files"] is False
    assert normalized["force_timeout"] is True
