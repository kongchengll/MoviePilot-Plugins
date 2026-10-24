"""
宿主数据访问层：通过稳定 SDK 读取整理历史。

只使用 `app.sdk.queries` 的只读快照接口，不直接查询宿主 ORM，也不持有 Session。
"""

from __future__ import annotations

from typing import Any, Iterable

from app.schemas.types import MediaType
from app.sdk.logging import logger
from app.sdk.queries import (
    QueryPageRequest,
    QuerySort,
    QuerySortDirection,
    QuerySortField,
    TransferHistoryFilter,
    list_transfer_history,
)

# 单页上限由宿主合同约束为 200，超出会被拒绝
_PAGE_SIZE = 200
# 单次推送最多翻页数，避免异常数据导致长时间占用调度线程
_MAX_PAGES = 50

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
