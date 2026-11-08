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
    parse_season,
    split_tags,
    tags_match,
    task_tags,
)
from app.plugins.downloadwatch.monitor import (
    REASON_BLACKLIST,
    REASON_FINISHED,
    REASON_PROGRESSING,
    REASON_REAP,
    REASON_TOO_YOUNG,
    evaluate,
    format_duration,
    is_finished,
    progress_text,
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
