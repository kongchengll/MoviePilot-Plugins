"""
下载监控插件的配置默认值、规范化与文本匹配工具。

保持纯标准库实现，便于脱离宿主单独验证阈值判定、标签匹配与排除规则拼接。
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Mapping, Optional

# 默认扫描周期：每 10 分钟一次
DEFAULT_CRON = "*/10 * * * *"

# 黑名单保留上限，避免长期运行后数据无限增长
MAX_BLACKLIST = 500

# 换源计数保留上限，按「订阅 + 集」记录，超出后裁掉最旧的
MAX_SWAPS = 200

# 详情页「已删除」记录保留条数
MAX_REMOVED_RECORDS = 100

# 详情页「监控中」快照保留条数
MAX_WATCH_RECORDS = 200

# 单轮通知里最多列举的条目数，超出部分只给数量
MAX_NOTIFY_LINES = 10

# 详情页单个列表最多渲染的条目数，避免任务很多时页面 JSON 过大
MAX_LIST_ROWS = 50

DEFAULT_CONFIG: dict[str, Any] = {
    "enabled": False,
    "cron": DEFAULT_CRON,
    # 已等待时长阈值（小时）：大于等于该值才可能被判为卡死
    "max_hours": 24,
    # 进度阈值（百分比）：小于等于该值才可能被判为卡死
    "min_progress": 5,
    # 强制清理：任务从首次被监控起满 force_hours 小时仍未下载完成就删除，不看进度
    "force_timeout": True,
    # 强制清理的时长阈值（小时）
    "force_hours": 24,
    # 强制清理时是否连本地文件一起删，不受 delete_files 影响
    "force_delete_files": True,
    # 只监控带这些标签的任务，留空表示不按标签过滤
    "tags": "",
    # 删除任务时是否同时删除已下载文件
    "delete_files": False,
    # 通知总开关
    "notify": True,
    # 任务进入监控时通知
    "notify_watch": True,
    # 删除任务时通知
    "notify_remove": True,
    # 演练模式：只记录将要删除的任务，不真正删除
    "dry_run": False,
    # 删除后写入本地黑名单，任务再次出现时立即清理
    "use_blacklist": True,
    # 删除后把种子标题写进订阅排除规则，让宿主改搜其它种子（换源）
    "sync_subscribe": True,
    # 同一订阅同一集最多自动换源几次，0 表示不限制
    "swap_limit": 5,
    # 保存配置后立即扫描一次
    "once": False,
}

_SEASON_PATTERN = re.compile(r"[Ss](\d{1,2})")

# 集号文本：支持 E01、E01-E03、E01、E02 等宿主下载历史与种子标题里的常见写法
_EPISODE_PATTERN = re.compile(r"[Ee](\d{1,4})(?:\s*[-~]\s*[Ee]?(\d{1,4}))?")

# 订阅排除规则里未转义的 `|` 才是分支分隔符，`\|` 属于分支内容
_EXCLUDE_SPLIT = re.compile(r"(?<!\\)\|")


def _as_bool(value: Any, default: bool = False) -> bool:
    """
    把表单传入的任意值归一为布尔值。
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _as_int(value: Any, default: int, minimum: int = 0, maximum: int = 500) -> int:
    """
    把表单传入的任意值归一为范围内的整数。
    """
    try:
        number = int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, number))


def normalize_config(config: Optional[Mapping[str, Any]]) -> dict[str, Any]:
    """
    用默认值补齐缺失项并修正非法取值。

    :param config: 宿主传入的原始配置，可能为 None
    :return: 可直接使用的完整配置字典
    """
    raw = dict(config or {})
    result = dict(DEFAULT_CONFIG)

    result["enabled"] = _as_bool(raw.get("enabled"), DEFAULT_CONFIG["enabled"])

    cron = str(raw.get("cron") or "").strip()
    result["cron"] = cron or DEFAULT_CRON

    result["max_hours"] = _as_int(raw.get("max_hours"), DEFAULT_CONFIG["max_hours"], 1, 8760)
    result["min_progress"] = _as_int(raw.get("min_progress"), DEFAULT_CONFIG["min_progress"], 0, 99)
    result["force_timeout"] = _as_bool(raw.get("force_timeout"), DEFAULT_CONFIG["force_timeout"])
    result["force_hours"] = _as_int(raw.get("force_hours"), DEFAULT_CONFIG["force_hours"], 1, 8760)
    result["force_delete_files"] = _as_bool(
        raw.get("force_delete_files"), DEFAULT_CONFIG["force_delete_files"]
    )
    result["tags"] = str(raw.get("tags") or "").strip()

    result["delete_files"] = _as_bool(raw.get("delete_files"), DEFAULT_CONFIG["delete_files"])
    result["notify"] = _as_bool(raw.get("notify"), DEFAULT_CONFIG["notify"])
    result["notify_watch"] = _as_bool(raw.get("notify_watch"), DEFAULT_CONFIG["notify_watch"])
    result["notify_remove"] = _as_bool(raw.get("notify_remove"), DEFAULT_CONFIG["notify_remove"])
    result["dry_run"] = _as_bool(raw.get("dry_run"), DEFAULT_CONFIG["dry_run"])
    result["use_blacklist"] = _as_bool(raw.get("use_blacklist"), DEFAULT_CONFIG["use_blacklist"])
    result["sync_subscribe"] = _as_bool(raw.get("sync_subscribe"), DEFAULT_CONFIG["sync_subscribe"])
    result["swap_limit"] = _as_int(raw.get("swap_limit"), DEFAULT_CONFIG["swap_limit"], 0, 50)
    result["once"] = _as_bool(raw.get("once"), False)
    return result


def split_tags(text: Any) -> list[str]:
    """
    把逗号分隔的标签配置拆成列表，兼容中英文逗号与顿号。
    """
    if isinstance(text, (list, tuple, set)):
        parts = [str(item) for item in text]
    else:
        normalized = str(text or "").replace("，", ",").replace("、", ",")
        parts = normalized.split(",")
    return [part.strip() for part in parts if part.strip()]


def task_tags(value: Any) -> set[str]:
    """
    把下载器返回的任务标签文本归一为小写集合。

    :param value: 下载器返回的标签文本，通常形如 `MOVIEPILOT, 电影`
    :return: 小写标签集合
    """
    if isinstance(value, (list, tuple, set)):
        parts = [str(item) for item in value]
    else:
        parts = re.split(r"[,，、]", str(value or ""))
    return {part.strip().lower() for part in parts if part.strip()}


def tags_match(value: Any, wanted: Iterable[str]) -> bool:
    """
    判断任务标签是否命中监控标签。

    :param value: 下载器返回的任务标签文本
    :param wanted: 配置的监控标签列表；为空表示不过滤
    :return: 命中或无标签过滤时返回 True
    """
    targets = {str(item).strip().lower() for item in wanted if str(item).strip()}
    if not targets:
        return True
    return bool(task_tags(value) & targets)


def normalize_title(title: Any) -> str:
    """
    归一化标题，仅用于展示与日志比对，不作为删除依据。
    """
    return re.sub(r"\s+", " ", str(title or "")).strip()


def escape_regex(text: Any) -> str:
    """
    转义正则元字符，让种子标题可以安全地当作排除规则使用。
    """
    return re.escape(str(text or "").strip())


def merge_exclude(existing: Any, keyword: Any) -> str:
    """
    把新关键词并入订阅的排除规则串。

    宿主的订阅「排除」字段整体是一条正则，多条规则以 `|` 分支拼接，因此这里
    直接追加分支而不是换行。

    :param existing: 订阅当前的排除规则
    :param keyword: 待追加的种子标题
    :return: 合并后的排除规则串；无变化时返回原值
    """
    current = str(existing or "").strip().rstrip("|")
    escaped = exclude_rule(keyword)
    if not escaped:
        return current
    if not current:
        return escaped
    # 已经包含同一条规则时保持原样，避免反复保存把规则串越拼越长
    if escaped in {part.strip() for part in _EXCLUDE_SPLIT.split(current)}:
        return current
    return f"{current}|{escaped}"


def exclude_rule(title: Any) -> str:
    """
    把种子标题转成可安全嵌入订阅排除正则的一个分支。

    标题里带 `[`、`(`、`+` 等正则元字符时，直接拼进排除规则会被当成语法，
    轻则误排除整部剧，重则正则编译失败导致该订阅搜索全挂，所以统一转义。

    :param title: 种子原始标题
    :return: 转义后的排除分支
    """
    return escape_regex(title)


def remove_exclude_rule(existing: Any, rule: Any) -> tuple[str, bool]:
    """
    从订阅排除规则里删掉指定分支。

    :param existing: 订阅当前的排除规则
    :param rule: 待移除的分支（通常是 :func:`exclude_rule` 的产物）
    :return: (新的排除规则, 是否真的删掉了分支)
    """
    current = str(existing or "").strip()
    target = str(rule or "").strip()
    if not current or not target:
        return current, False
    parts = _EXCLUDE_SPLIT.split(current)
    kept = [part for part in parts if part.strip() and part.strip() != target]
    if len(kept) == len([part for part in parts if part.strip()]):
        return current, False
    return "|".join(part.strip() for part in kept), True


def swap_allowed(count: Any, limit: Any) -> bool:
    """
    判断同一订阅同一集是否还允许继续自动换源。

    换源上限是防死循环的兜底：所有可选种子都卡住时，插件每轮删一个、宿主每轮
    重下一个，会把下载器刷爆，所以超过上限后改为放弃该集并提示人工处理。

    :param count: 该集已经换源的次数
    :param limit: 上限；0 或负数表示不限制
    :return: 是否允许继续换源
    """
    try:
        maximum = int(limit or 0)
    except (TypeError, ValueError):
        maximum = 0
    if maximum <= 0:
        return True
    try:
        used = int(count or 0)
    except (TypeError, ValueError):
        used = 0
    return used < maximum


def remove_blacklist_item(
    blacklist: Iterable[Any], hash_value: Any = "", title: Any = ""
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """
    从黑名单里摘掉指定条目。

    优先按种子 hash 精确匹配（大小写不敏感），只有在没给 hash 时才退化为按标题
    全等匹配 —— 标题可能被同名种子的不同压制版本复用，不能作为默认依据。

    :param blacklist: 当前黑名单条目列表
    :param hash_value: 待移除种子的 hash
    :param title: 待移除种子的标题，仅在缺少 hash 时使用
    :return: (保留的条目, 被移除的条目)
    """
    items = [item for item in blacklist if isinstance(item, dict)]
    key = str(hash_value or "").strip().lower()
    name = normalize_title(title)
    if not key and not name:
        return items, []

    kept: list[dict[str, Any]] = []
    removed: list[dict[str, Any]] = []
    for item in items:
        item_hash = str(item.get("hash") or "").strip().lower()
        hit = item_hash == key if key else normalize_title(item.get("title")) == name
        if hit:
            removed.append(item)
            continue
        kept.append(item)
    return kept, removed


def parse_season(season_episode: Any) -> Optional[int]:
    """
    从 `S01E03` 一类的季集文本里取出季号。

    :return: 季号；无法识别时返回 None
    """
    match = _SEASON_PATTERN.search(str(season_episode or ""))
    if not match:
        return None
    return int(match.group(1))


def parse_episodes(text: Any) -> list[int]:
    """
    从 `E01`、`E01-E03`、`E01、E02` 一类的文本里取出集号列表。

    宿主下载历史把季集写成 Exx 文本，种子标题里则是 SxxExx；两种写法都能命中。
    范围写法会展开，但为避免把年份之类的异常区间当成集数，跨度超过 200 时只取起点。

    :param text: 季集或标题文本
    :return: 升序去重的集号列表；无法识别时返回空列表
    """
    result: set[int] = set()
    for match in _EPISODE_PATTERN.finditer(str(text or "")):
        start = int(match.group(1))
        end = match.group(2)
        if end is None:
            if start > 0:
                result.add(start)
            continue
        finish = int(end)
        if 0 <= finish - start <= 200:
            result.update(range(start, finish + 1))
        elif start > 0:
            result.add(start)
    return sorted(result)
