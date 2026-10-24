"""
每日入库速报的纯逻辑层：把整理历史快照聚合成可渲染的摘要。

本模块只依赖标准库，不导入任何宿主接口，因此可以脱离 MoviePilot 宿主单独测试。
宿主取数在 `collector`，图片合成在 `poster`，插件生命周期在 `__init__`。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Optional

# 单条记录里可能出现的集数区间，兼容 "E01-E03"、"第01-03集"、"01~03" 等写法
_RANGE_PATTERN = re.compile(r"(\d{1,4})\s*[-~—]\s*(?:[EeＳS]|第)?\s*(\d{1,4})")
_NUMBER_PATTERN = re.compile(r"\d{1,4}")
# 季前缀需先剔除，否则 "S02E05" 会被误解析成第 2 集和第 5 集
_SEASON_PREFIX_PATTERN = re.compile(r"[SsＳ]\s*\d{1,3}")

# 区间跨度上限，超过视为脏数据，避免把 "2024-2025" 这类年份差展开成上千集
_MAX_RANGE_SPAN = 500

# 电影没有集概念，统一用空集数表示
_MOVIE_TYPE = "电影"
_TV_TYPE = "电视剧"


@dataclass
class EpisodeGroup:
    """
    一部剧（或一部电影）在统计窗口内的入库汇总。
    """

    title: str
    media_type: str
    season: Optional[str] = None
    category: Optional[str] = None
    year: Optional[str] = None
    episodes: list[int] = field(default_factory=list)
    last_time: str = ""
    poster: Optional[str] = None

    @property
    def is_movie(self) -> bool:
        """
        该分组是否为电影。
        """
        return self.media_type == _MOVIE_TYPE

    @property
    def episode_count(self) -> int:
        """
        该分组在窗口内新增的集数；电影按 1 计。
        """
        return len(self.episodes) if not self.is_movie else 1

    @property
    def season_label(self) -> str:
        """
        规范化的季标签，如 `S02`；没有季信息时返回空串。
        """
        raw = (self.season or "").strip()
        if not raw:
            return ""
        matched = re.search(r"\d{1,3}", raw)
        if not matched:
            return raw
        return f"S{int(matched.group()):02d}"


@dataclass
class Digest:
    """
    一次推送所需的完整数据，渲染层只读取这里，不再访问宿主。
    """

    day_label: str
    start: str
    end: str
    groups: list[EpisodeGroup] = field(default_factory=list)
    dropped: int = 0

    @property
    def total_shows(self) -> int:
        """
        入库部数。
        """
        return len(self.groups)

    @property
    def total_episodes(self) -> int:
        """
        入库集数，只累计剧集，电影不计集。
        """
        return sum(group.episode_count for group in self.groups if not group.is_movie)

    @property
    def total_movies(self) -> int:
        """
        入库电影数量。
        """
        return sum(1 for group in self.groups if group.is_movie)

    @property
    def is_empty(self) -> bool:
        """
        窗口内没有任何入库。
        """
        return not self.groups


def parse_episodes(raw: Optional[str]) -> list[int]:
    """
    从整理历史的剧集字段解析出集号集合。

    同时支持区间与枚举写法，重复集号会被合并，无法解析时返回空列表。

    :param raw: 形如 `E01-E03`、`E01、E02`、`第01-03集` 的原始文本
    :return: 去重升序后的集号列表
    """
    if not raw:
        return []
    text = _SEASON_PREFIX_PATTERN.sub(" ", str(raw))
    numbers: set[int] = set()
    consumed: list[tuple[int, int]] = []
    for matched in _RANGE_PATTERN.finditer(text):
        begin, end = int(matched.group(1)), int(matched.group(2))
        if begin > end:
            begin, end = end, begin
        if end - begin > _MAX_RANGE_SPAN:
            continue
        numbers.update(range(begin, end + 1))
        consumed.append(matched.span())
    # 区间已被消费，剩下的裸数字才按单集处理，避免把区间两端重复计入
    remainder = text
    for begin, end in sorted(consumed, reverse=True):
        remainder = remainder[:begin] + " " * (end - begin) + remainder[end:]
    for matched in _NUMBER_PATTERN.finditer(remainder):
        numbers.add(int(matched.group()))
    return sorted(number for number in numbers if number > 0)


def format_episodes(numbers: Iterable[int]) -> str:
    """
    把集号列表格式化为连续区间文本。

    :param numbers: 集号集合
    :return: 形如 `E01-E03、E05` 的文本，空集返回空串
    """
    ordered = sorted({int(number) for number in numbers if int(number) > 0})
    if not ordered:
        return ""
    if len(ordered) == 1:
        return f"E{ordered[0]:02d}"
    parts: list[str] = []
    start = previous = ordered[0]
    for number in ordered[1:]:
        if number == previous + 1:
            previous = number
            continue
        parts.append(f"E{start:02d}" if start == previous else f"E{start:02d}-E{previous:02d}")
        start = previous = number
    parts.append(f"E{start:02d}" if start == previous else f"E{start:02d}-E{previous:02d}")
    return "、".join(parts)


def _record_time(record: Mapping[str, Any]) -> str:
    """
    读取记录时间，缺失时返回空串，便于统一参与字符串比较。
    """
    return str(record.get("date") or "")


def _in_window(record_time: str, start: str, end: str) -> bool:
    """
    判断记录时间是否落在半开区间 [start, end) 内。

    宿主写入的时间是本地时间字符串 `%Y-%m-%d %H:%M:%S`，可直接按字典序比较。
    """
    if not record_time:
        return False
    return start <= record_time < end


def _is_excluded(title: str, words: list[str]) -> bool:
    """
    标题命中任一排除词时返回 True。
    """
    lowered = title.lower()
    return any(word and word in lowered for word in words)


def build_digest(
    records: Iterable[Mapping[str, Any]],
    *,
    start: str,
    end: str,
    day_label: str,
    include_movie: bool = True,
    exclude_words: Optional[list[str]] = None,
    max_items: int = 20,
) -> Digest:
    """
    把整理历史记录聚合成按部合并的摘要。

    同一部剧的多条记录会按 `标题 + 季` 合并并去重集号，从而抵消洗版、
    分批整理造成的重复；排序按最后一次入库时间倒序，最新入库排在最前。

    :param records: 记录字典，需包含 title/type/seasons/episodes/date/image 等字段
    :param start: 统计窗口起点，本地时间字符串
    :param end: 统计窗口终点，本地时间字符串，不含
    :param day_label: 展示用日期标签，如 `10月23日`
    :param include_movie: 是否统计电影
    :param exclude_words: 标题排除词，命中即跳过
    :param max_items: 最多保留多少部，超出部分计入 `dropped`
    :return: 聚合后的摘要对象
    """
    words = [word.strip().lower() for word in (exclude_words or []) if word.strip()]
    merged: dict[tuple[str, str], EpisodeGroup] = {}

    for record in records:
        title = str(record.get("title") or "").strip()
        if not title:
            continue
        media_type = str(record.get("type") or "").strip()
        if media_type == _MOVIE_TYPE and not include_movie:
            continue
        if media_type not in (_TV_TYPE, _MOVIE_TYPE):
            continue
        record_time = _record_time(record)
        if not _in_window(record_time, start, end):
            continue
        if _is_excluded(title, words):
            continue

        season = str(record.get("seasons") or "").strip() or None
        key = (title, season or "")
        group = merged.get(key)
        if group is None:
            group = EpisodeGroup(
                title=title,
                media_type=media_type,
                season=season,
                category=str(record.get("category") or "").strip() or None,
                year=str(record.get("year") or "").strip() or None,
                last_time=record_time,
            )
            merged[key] = group
        if not group.is_movie:
            group.episodes = sorted(set(group.episodes) | set(parse_episodes(record.get("episodes"))))
        if record_time > group.last_time:
            group.last_time = record_time
        poster = str(record.get("image") or "").strip()
        if poster and not group.poster:
            group.poster = poster

    ordered = sorted(merged.values(), key=lambda item: item.last_time, reverse=True)
    dropped = 0
    if max_items and len(ordered) > max_items:
        dropped = len(ordered) - max_items
        ordered = ordered[:max_items]

    return Digest(
        day_label=day_label,
        start=start,
        end=end,
        groups=ordered,
        dropped=dropped,
    )


def group_detail(group: EpisodeGroup) -> str:
    """
    拼装单部作品第二行的信息，如 `S02 E03、E04 · 2 集`。
    """
    if group.is_movie:
        return f"{group.year} · 电影" if group.year else "电影"
    season = group.season_label
    episodes = format_episodes(group.episodes)
    if season and episodes:
        return f"{season} {episodes} · {len(group.episodes)} 集"
    if season:
        return f"{season} · {len(group.episodes)} 集"
    if episodes:
        return f"{episodes} · {len(group.episodes)} 集"
    return f"{len(group.episodes)} 集"


def _summary_line(digest: Digest) -> str:
    """
    拼装首行统计概要，剧集与电影分开描述，避免部数重复计数造成歧义。
    """
    shows = digest.total_shows - digest.total_movies
    if digest.total_movies and not shows:
        return f"共 {digest.total_movies} 部电影"
    parts: list[str] = []
    if shows:
        parts.append(f"{shows} 部剧集")
    if digest.total_episodes:
        parts.append(f"{digest.total_episodes} 集")
    if digest.total_movies:
        parts.append(f"{digest.total_movies} 部电影")
    return "共 " + " · ".join(parts)


def render(digest: Digest, empty_hint: str = "没有新入库内容") -> tuple[str, str]:
    """
    把摘要渲染成通知标题与正文。

    :param digest: 聚合后的摘要
    :param empty_hint: 窗口内无入库时的提示语
    :return: `(标题, 正文)` 二元组
    """
    title = f"每日入库速报 · {digest.day_label}"
    if digest.is_empty:
        return title, empty_hint

    lines: list[str] = [_summary_line(digest), ""]
    for index, group in enumerate(digest.groups, start=1):
        lines.append(f"{index}. {group.title}")
        lines.append(f"   {group_detail(group)}")
    if digest.dropped:
        lines.append("")
        lines.append(f"…另有 {digest.dropped} 部未列出")
    return title, "\n".join(lines)


def short_time(raw: str) -> str:
    """
    把宿主时间字符串压缩成 `10-23 23:02`，用于逐部消息正文。
    """
    text = (raw or "").strip()
    if len(text) < 16:
        return ""
    return f"{text[5:10]} {text[11:16]}"
