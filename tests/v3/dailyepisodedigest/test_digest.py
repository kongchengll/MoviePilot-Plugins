"""
每日入库速报的纯逻辑测试。

按宿主约定使用生产命名空间 `app.plugins.<plugin_id>` 导入，避免同一源码被加载成两个模块。
"""

from __future__ import annotations

from datetime import datetime

import pytest

from app.plugins.dailyepisodedigest.config import (
    WINDOW_LAST24H,
    WINDOW_TODAY,
    WINDOW_YESTERDAY,
    normalize_config,
    parse_window_alias,
    resolve_window,
    split_words,
)
from app.plugins.dailyepisodedigest.digest import (
    attach_subscription_totals,
    build_digest,
    episode_progress,
    format_episodes,
    parse_episodes,
    render,
)

DAY_START = "2026-10-23 00:00:00"
DAY_END = "2026-10-24 00:00:00"


def _record(**overrides):
    """
    构造一条整理历史记录，默认是一条落在窗口内的剧集记录。
    """
    base = {
        "title": "漫长的季节",
        "type": "电视剧",
        "category": "电视剧",
        "year": "2023",
        "seasons": "S01",
        "episodes": "E01-E03",
        "date": "2026-10-23 21:40:00",
        "image": "https://example.com/a.jpg",
    }
    base.update(overrides)
    return base


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("E01-E03", [1, 2, 3]),
        ("E01、E02", [1, 2]),
        ("第01-03集", [1, 2, 3]),
        ("S02E05", [5]),
        ("E01-E03、E07", [1, 2, 3, 7]),
        ("", []),
        (None, []),
    ],
)
def test_parse_episodes(raw, expected):
    """
    集数解析需兼容区间、枚举与季前缀写法。
    """
    assert parse_episodes(raw) == expected


def test_parse_episodes_ignores_absurd_range():
    """
    跨度过大的区间按脏数据处理，只保留区间两端而不展开。
    """
    assert parse_episodes("E01-E999") == [1, 999]


def test_format_episodes():
    """
    集号应压缩为连续区间文本。
    """
    assert format_episodes([1, 2, 3, 5]) == "E01-E03、E05"
    assert format_episodes([7]) == "E07"
    assert format_episodes([]) == ""


def test_build_digest_merges_same_season():
    """
    同一部剧的多条记录（洗版、分批整理）应合并并去重集号。
    """
    digest = build_digest(
        [
            _record(episodes="E01-E02"),
            _record(episodes="E02-E03", date="2026-10-23 22:10:00"),
        ],
        start=DAY_START,
        end=DAY_END,
        day_label="10月23日",
    )
    assert digest.total_shows == 1
    assert digest.groups[0].episodes == [1, 2, 3]
    assert digest.groups[0].last_time == "2026-10-23 22:10:00"


def test_build_digest_filters_window_and_type():
    """
    窗口外记录与非目标类型应被排除。
    """
    digest = build_digest(
        [
            _record(date="2026-10-22 23:59:59"),
            _record(title="沙丘2", type="电影", seasons=None, episodes=None),
            _record(title="音乐专辑", type="音乐"),
        ],
        start=DAY_START,
        end=DAY_END,
        day_label="10月23日",
    )
    assert [group.title for group in digest.groups] == ["沙丘2"]
    assert digest.total_movies == 1
    assert digest.total_episodes == 0


def test_build_digest_respects_movie_switch_and_exclude_words():
    """
    关闭电影统计与排除关键词都应生效。
    """
    digest = build_digest(
        [
            _record(title="沙丘2", type="电影", seasons=None, episodes=None),
            _record(title="某部预告片特辑"),
        ],
        start=DAY_START,
        end=DAY_END,
        day_label="10月23日",
        include_movie=False,
        exclude_words=["特辑"],
    )
    assert digest.is_empty


def test_build_digest_sorts_by_latest_and_truncates():
    """
    按最后入库时间倒序，并在超过上限时记录被截断的数量。
    """
    records = [
        _record(title=f"剧集{index}", seasons="S01", date=f"2026-10-23 1{index}:00:00")
        for index in range(5)
    ]
    digest = build_digest(records, start=DAY_START, end=DAY_END, day_label="10月23日", max_items=3)
    assert [group.title for group in digest.groups] == ["剧集4", "剧集3", "剧集2"]
    assert digest.dropped == 2


def test_render_output_shape():
    """
    正文应包含概要、逐部条目与第二行明细。
    """
    digest = build_digest(
        [_record()],
        start=DAY_START,
        end=DAY_END,
        day_label="10月23日",
    )
    title, text = render(digest)
    assert title == "每日入库速报 · 10月23日"
    assert "共 1 部" in text
    assert "1. 漫长的季节" in text
    assert "S01 E01-E03 · 3 集" in text


def test_render_empty():
    """
    无入库时只返回标题与提示语。
    """
    digest = build_digest([], start=DAY_START, end=DAY_END, day_label="10月23日")
    title, text = render(digest, empty_hint="10月23日没有新入库内容")
    assert title == "每日入库速报 · 10月23日"
    assert text == "10月23日没有新入库内容"


def test_normalize_config_image_only():
    """
    「仅发送图片」默认开启，可显式关闭以恢复标题与执行回执。
    """
    assert normalize_config(None)["image_only"] is True
    assert normalize_config({"image_only": False})["image_only"] is False
    assert normalize_config({"image_only": "false"})["image_only"] is False


def test_resolve_window_yesterday():
    """
    昨日窗口应覆盖前一自然日整天。
    """
    start, end, label = resolve_window(WINDOW_YESTERDAY, datetime(2026, 10, 24, 9, 0, 0))
    assert (start, end, label) == ("2026-10-23 00:00:00", "2026-10-24 00:00:00", "10月23日")


def test_resolve_window_today_and_last24h():
    """
    今日窗口从零点起算，最近 24 小时按滚动区间。
    """
    now = datetime(2026, 10, 24, 9, 30, 0)
    assert resolve_window(WINDOW_TODAY, now)[:2] == ("2026-10-24 00:00:00", "2026-10-24 09:30:00")
    assert resolve_window(WINDOW_LAST24H, now)[:2] == (
        "2026-10-23 09:30:00",
        "2026-10-24 09:30:00",
    )


def test_normalize_config_fallbacks():
    """
    非法配置应回落到默认值，且不因空配置抛异常。
    """
    config = normalize_config(None)
    assert config["window"] == WINDOW_YESTERDAY
    assert config["media_types"] == ["电视剧", "电影"]
    assert config["image_items"] == 8
    assert config["font_path"] == ""

    config = normalize_config(
        {
            "window": "不存在的模式",
            "media_types": "电视剧,音乐",
            "image_items": "0",
            "cron": "  ",
        }
    )
    assert config["window"] == WINDOW_YESTERDAY
    assert config["media_types"] == ["电视剧"]
    assert config["image_items"] == 1
    assert config["cron"] == "0 9 * * *"


def test_split_words():
    """
    排除词应兼容中英文逗号与顿号。
    """
    assert split_words("特辑，预告、花絮, 彩蛋") == ["特辑", "预告", "花絮", "彩蛋"]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("today", WINDOW_TODAY),
        (" 24H ", WINDOW_LAST24H),
        ("last24h", WINDOW_LAST24H),
        ("yesterday", WINDOW_YESTERDAY),
        ("", None),
        (None, None),
        ("随便写点", None),
    ],
)
def test_parse_window_alias(raw, expected):
    """
    命令参数应能映射到统计窗口，无法识别时返回 None 表示沿用配置。
    """
    assert parse_window_alias(raw) == expected


def _digest_with_shows():
    """
    构造一部 S01 剧集（本批入库到 E14）供进度匹配测试。
    """
    return build_digest(
        [_record(episodes="E13-E14")],
        start=DAY_START,
        end=DAY_END,
        day_label="10月23日",
    )


def test_subscription_total_manual_wins():
    """
    手动指定的总集数应优先于宿主维护值。
    """
    digest = _digest_with_shows()
    matched = attach_subscription_totals(
        digest,
        [{"name": "漫长的季节", "season": 1, "total_episode": 12, "manual_total_episode": 35}],
    )
    assert matched == 1
    assert digest.groups[0].total_episodes == 35
    assert episode_progress(digest.groups[0]) == "14/35"


def test_subscription_total_by_title_and_season():
    """
    归一化标题（含空格标点）加季号应能匹配，季号不一致时不匹配。
    """
    digest = _digest_with_shows()
    attach_subscription_totals(
        digest,
        [{"name": " 漫长的季节 ", "season": 1, "total_episode": 30}],
    )
    assert digest.groups[0].total_episodes == 30

    other = _digest_with_shows()
    attach_subscription_totals(
        other,
        [{"name": "漫长的季节", "season": 2, "total_episode": 30}],
    )
    assert other.groups[0].total_episodes is None
    assert episode_progress(other.groups[0]) == "14"


def test_subscription_total_by_media_identity():
    """
    标题不同但媒体身份一致时，仍应通过身份精确匹配到总集数。
    """
    digest = build_digest(
        [_record(title="译名A", media_source="douban", media_id="3600001")],
        start=DAY_START,
        end=DAY_END,
        day_label="10月23日",
    )
    attach_subscription_totals(
        digest,
        [{
            "name": "原名B", "season": 1, "total_episode": 28,
            "media_source": "douban", "media_id": "3600001",
        }],
    )
    assert digest.groups[0].total_episodes == 28


def test_episode_progress_skips_movies_and_empty():
    """
    电影与无集号剧集不显示进度徽标。
    """
    digest = build_digest(
        [
            _record(title="沙丘2", type="电影", seasons=None, episodes=None),
            _record(title="无集号剧", episodes=""),
        ],
        start=DAY_START,
        end=DAY_END,
        day_label="10月23日",
    )
    assert all(episode_progress(group) == "" for group in digest.groups)


def test_render_shows_progress_when_matched():
    """
    正文在匹配到订阅后应在剧名后标注进度。
    """
    digest = _digest_with_shows()
    attach_subscription_totals(digest, [{"name": "漫长的季节", "season": 1, "total_episode": 32}])
    _, text = render(digest)
    assert "1. 漫长的季节  [14/32]" in text
