"""
插件配置的默认值、规范化与统计窗口计算。

保持纯标准库实现，便于脱离宿主验证窗口边界与配置兜底逻辑。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Mapping, Optional

# 统计窗口模式
WINDOW_YESTERDAY = "yesterday"
WINDOW_TODAY = "today"
WINDOW_LAST24H = "last24h"

WINDOW_OPTIONS: list[dict[str, str]] = [
    {"title": "昨日 00:00 - 24:00", "value": WINDOW_YESTERDAY},
    {"title": "今日 00:00 - 当前", "value": WINDOW_TODAY},
    {"title": "最近 24 小时", "value": WINDOW_LAST24H},
]

# 命令参数别名，手动触发时可临时指定统计范围
_WINDOW_ALIASES: dict[str, str] = {
    "yesterday": WINDOW_YESTERDAY,
    "today": WINDOW_TODAY,
    "24h": WINDOW_LAST24H,
    "last24h": WINDOW_LAST24H,
}

# 媒体类型选项，取值与宿主 MediaType 的中文值一致
MEDIA_TYPE_OPTIONS: list[dict[str, str]] = [
    {"title": "电视剧", "value": "电视剧"},
    {"title": "电影", "value": "电影"},
]

DEFAULT_CONFIG: dict[str, Any] = {
    "enabled": False,
    "cron": "0 9 * * *",
    "window": WINDOW_YESTERDAY,
    "media_types": ["电视剧", "电影"],
    "exclude_words": "",
    "image_items": 8,
    "public_domain": "",
    "font_path": "",
    "notify_empty": True,
    "run_once": False,
}

_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"


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
        number = int(str(value).strip())
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
    result["cron"] = cron or DEFAULT_CONFIG["cron"]

    window = str(raw.get("window") or "").strip()
    result["window"] = window if window in {item["value"] for item in WINDOW_OPTIONS} else WINDOW_YESTERDAY

    media_types = raw.get("media_types")
    if isinstance(media_types, str):
        media_types = [part.strip() for part in media_types.split(",") if part.strip()]
    if not isinstance(media_types, list):
        media_types = list(DEFAULT_CONFIG["media_types"])
    allowed_types = {item["value"] for item in MEDIA_TYPE_OPTIONS}
    result["media_types"] = [item for item in media_types if item in allowed_types]

    result["exclude_words"] = str(raw.get("exclude_words") or "").strip()
    result["image_items"] = _as_int(raw.get("image_items"), DEFAULT_CONFIG["image_items"], 1, 20)
    result["public_domain"] = str(raw.get("public_domain") or "").strip().rstrip("/")
    result["font_path"] = str(raw.get("font_path") or "").strip()
    result["notify_empty"] = _as_bool(raw.get("notify_empty"), DEFAULT_CONFIG["notify_empty"])
    result["run_once"] = _as_bool(raw.get("run_once"), False)
    return result


def split_words(text: str) -> list[str]:
    """
    把逗号分隔的排除词拆成列表，兼容中英文逗号与顿号。
    """
    normalized = (text or "").replace("，", ",").replace("、", ",")
    return [part.strip() for part in normalized.split(",") if part.strip()]


def parse_window_alias(text: Any) -> Optional[str]:
    """
    把命令参数解析成统计窗口标识，用于手动触发时临时覆盖配置。

    :param text: 命令后面的参数，如 `today`、`24h`
    :return: 可用的窗口标识；参数为空或无法识别时返回 None 表示沿用配置
    """
    key = str(text or "").strip().lower()
    if not key:
        return None
    return _WINDOW_ALIASES.get(key)


def _day_label(moment: datetime) -> str:
    """
    生成通知里展示的日期标签，如 `10月23日`。
    """
    return f"{moment.month}月{moment.day}日"


def resolve_window(mode: str, now: Optional[datetime] = None) -> tuple[str, str, str]:
    """
    按模式计算统计窗口，返回左闭右开区间与展示用日期。

    :param mode: `yesterday` / `today` / `last24h`
    :param now: 参考时间，缺省取当前本地时间
    :return: `(窗口起点, 窗口终点, 日期标签)`，时间格式与宿主整理历史一致
    """
    current = (now or datetime.now()).replace(microsecond=0)
    if mode == WINDOW_TODAY:
        start = current.replace(hour=0, minute=0, second=0)
        return start.strftime(_TIME_FORMAT), current.strftime(_TIME_FORMAT), _day_label(current)
    if mode == WINDOW_LAST24H:
        start = current - timedelta(hours=24)
        return start.strftime(_TIME_FORMAT), current.strftime(_TIME_FORMAT), _day_label(current)
    today_start = current.replace(hour=0, minute=0, second=0)
    start = today_start - timedelta(days=1)
    return start.strftime(_TIME_FORMAT), today_start.strftime(_TIME_FORMAT), _day_label(start)
