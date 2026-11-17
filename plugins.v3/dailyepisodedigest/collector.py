"""
宿主数据访问层：通过稳定 SDK 读取整理历史。

只使用 `app.sdk.queries` 的只读快照接口，不直接查询宿主 ORM，也不持有 Session。
"""

from __future__ import annotations

from typing import Any, Iterable
from urllib.parse import urlencode

from app.schemas.types import MediaType
from app.sdk.config import settings
from app.sdk.logging import logger
from app.sdk.network import RequestUtils
from app.sdk.queries import (
    QueryPageRequest,
    QuerySort,
    QuerySortDirection,
    QuerySortField,
    TransferHistoryFilter,
    list_subscriptions,
    list_transfer_history,
)

# 单页上限由宿主合同约束为 200，超出会被拒绝
_PAGE_SIZE = 200
# 单次推送最多翻页数，避免异常数据导致长时间占用调度线程
_MAX_PAGES = 50
# 订阅总量通常远小于整理历史，单独设一个更小的翻页上限
_MAX_SUB_PAGES = 10

_TYPE_MAP = {
    "电视剧": MediaType.TV,
    "电影": MediaType.MOVIE,
}


def _to_record(snapshot: Any) -> dict[str, Any]:
    """
    把宿主只读快照投影成纯字典，避免上层依赖宿主模型结构。
    """
    return {
        "id": getattr(snapshot, "id", None),
        "title": getattr(snapshot, "title", None),
        "type": getattr(snapshot, "type", None),
        "category": getattr(snapshot, "category", None),
        "year": getattr(snapshot, "year", None),
        # 媒体身份供与订阅做精确匹配，缺失时降级为标题+季匹配
        "media_source": getattr(snapshot, "media_source", None),
        "media_id": getattr(snapshot, "media_id", None),
        "seasons": getattr(snapshot, "seasons", None),
        "episodes": getattr(snapshot, "episodes", None),
        "date": getattr(snapshot, "date", None),
        "image": getattr(snapshot, "image", None),
        "status": bool(getattr(snapshot, "status", False)),
    }


def fetch_records(start: str, end: str, media_types: Iterable[str]) -> list[dict[str, Any]]:
    """
    读取窗口内的成功整理记录。

    宿主查询合同不支持时间范围筛选，因此按日期倒序翻页，遇到早于窗口起点的记录即停止。
    记录量按天计算通常只有一页，翻页上限用于兜底异常数据。

    :param start: 窗口起点，本地时间字符串
    :param end: 窗口终点，本地时间字符串，不含
    :param media_types: 需要统计的媒体类型中文值集合
    :return: 投影后的记录字典列表，不保证顺序
    """
    wanted = tuple(_TYPE_MAP[item] for item in media_types if item in _TYPE_MAP)
    if not wanted:
        return []

    filters = TransferHistoryFilter(status=True, media_types=wanted)
    sort = QuerySort(field=QuerySortField.DATE, direction=QuerySortDirection.DESC)
    records: list[dict[str, Any]] = []

    for page in range(1, _MAX_PAGES + 1):
        result = list_transfer_history(
            filters,
            QueryPageRequest(page=page, count=_PAGE_SIZE, sort=sort),
        )
        items = list(getattr(result, "items", []) or [])
        if not items:
            break

        reached_older = False
        for snapshot in items:
            record = _to_record(snapshot)
            record_time = str(record.get("date") or "")
            # 倒序扫描，时间缺失或早于窗口起点即说明后续都更早
            if not record_time or record_time < start:
                reached_older = True
                continue
            if record_time >= end:
                continue
            records.append(record)
        if reached_older:
            break

        total = int(getattr(result, "total", 0) or 0)
        if page * _PAGE_SIZE >= total:
            break
    else:
        logger.warning(f"每日入库速报：整理历史翻页超过 {_MAX_PAGES} 页，已截断统计")

    logger.debug(f"每日入库速报：窗口 {start} ~ {end} 命中 {len(records)} 条整理记录")
    return records


def _to_subscription(snapshot: Any) -> dict[str, Any]:
    """
    把订阅只读快照投影成纯字典，只保留匹配总集数所需的字段。
    """
    return {
        "name": getattr(snapshot, "name", None),
        "year": getattr(snapshot, "year", None),
        "season": getattr(snapshot, "season", None),
        "total_episode": getattr(snapshot, "total_episode", None),
        "manual_total_episode": getattr(snapshot, "manual_total_episode", None),
        "media_source": getattr(snapshot, "media_source", None),
        "media_id": getattr(snapshot, "media_id", None),
    }


def fetch_subscriptions() -> list[dict[str, Any]]:
    """
    读取全部订阅快照，用于把「当前季总集数」匹配到入库剧集。

    注解快照的 total_episode 由宿主维护，manual_total_episode 为用户手动指定，
    后者优先使用。查询失败时返回空列表，让日报降级为不显示总集数而不是中断推送。

    :return: 投影后的订阅字典列表
    """
    try:
        sort = QuerySort(field=QuerySortField.ID, direction=QuerySortDirection.ASC)
        subscriptions: list[dict[str, Any]] = []
        for page in range(1, _MAX_SUB_PAGES + 1):
            result = list_subscriptions(page=QueryPageRequest(page=page, count=_PAGE_SIZE, sort=sort))
            items = list(getattr(result, "items", []) or [])
            if not items:
                break
            subscriptions.extend(_to_subscription(snapshot) for snapshot in items)
            total = int(getattr(result, "total", 0) or 0)
            if page * _PAGE_SIZE >= total:
                break
        else:
            logger.warning(f"每日入库速报：订阅翻页超过 {_MAX_SUB_PAGES} 页，已截断总集数匹配")
        logger.debug(f"每日入库速报：读取 {len(subscriptions)} 条订阅用于总集数匹配")
        return subscriptions
    except Exception as error:  # noqa: BLE001 - 订阅查询失败不应中断日报
        logger.warning(f"每日入库速报：读取订阅失败，本次不显示总集数：{error}")
        return []


def fetch_calendar(start_date: str, end_date: str) -> Optional[list[Any]]:
    """
    从宿主订阅日历接口读取指定日期窗口的播出事件（左闭右开，日期格式 YYYY-MM-DD）。

    走容器内 HTTP 自调用并使用宿主 API_TOKEN 认证，避免插件依赖宿主的异步链与非稳定 SDK。
    查询失败返回 None，让调用方区分「无更新」与「取数失败」。

    :param start_date: 起始日期（含），如 `2026-11-18`
    :param end_date: 结束日期（不含），如 `2026-11-19`
    :return: 日历条目列表；失败返回 None
    """
    token = str(getattr(settings, "API_TOKEN", "") or "").strip()
    if not token:
        logger.debug("每日入库速报：宿主未配置 API_TOKEN，跳过订阅日历读取")
        return None
    try:
        port = int(getattr(settings, "PORT", 3001) or 3001)
    except (TypeError, ValueError):
        port = 3001

    params = urlencode(
        {"start": start_date, "end": end_date, "page": 1, "count": 200, "token": token}
    )
    url = f"http://127.0.0.1:{port}/api/v1/subscribe/calendar?{params}"
    try:
        response = RequestUtils(timeout=15).get_res(url)
        if response is None or getattr(response, "status_code", 0) != 200:
            logger.warning(
                f"每日入库速报：订阅日历请求失败 "
                f"HTTP {getattr(response, 'status_code', '无响应')}"
            )
            return None
        payload = response.json()
        if isinstance(payload, dict) and payload.get("success") is False:
            logger.warning(f"每日入库速报：订阅日历返回失败：{payload.get('message')}")
            return None
        data = payload.get("data", payload) if isinstance(payload, dict) else {}
        items = data.get("items", []) if isinstance(data, dict) else []
        logger.debug(f"每日入库速报：订阅日历 {start_date}~{end_date} 命中 {len(items)} 条")
        return list(items or [])
    except Exception as error:  # noqa: BLE001 - 日历失败不影响主日报
        logger.warning(f"每日入库速报：读取订阅日历失败，跳过明日更新区块：{error}")
        return None
