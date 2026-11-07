"""
猫眼实时票房数据获取。

从猫眼专业版 dashboard 页面的 AJAX 接口拉取当日票房榜单，
供日报卡片末尾追加展示。

接口地址：https://piaofang.maoyan.com/dashboard-ajax/movie
该接口无需签名或 token，直接 GET 即可返回完整 JSON。

注意：猫眼接口数据结构会不定期变更，解析时同时兼容新旧字段。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, List, Optional

import httpx2

# 猫眼票房 AJAX 接口，返回大盘概览与榜单明细
_MAOYAN_API = "https://piaofang.maoyan.com/dashboard-ajax/movie"
_TIMEOUT = 10.0

# 请求头，模拟浏览器访问
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://piaofang.maoyan.com/dashboard",
    "Accept": "application/json, text/plain, */*",
}


@dataclass
class BoxOfficeMovie:
    """单部影片的票房信息。"""

    rank: int = 0
    title: str = ""
    realtime_box: str = ""       # 实时票房（带单位）
    total_box: str = ""          # 累计票房（带单位）
    box_rate: str = ""           # 票房占比
    show_rate: str = ""          # 排片占比


@dataclass
class BoxOfficeData:
    """票房数据集合。"""

    total_box: str = ""          # 大盘总票房
    split_box: str = ""          # 分账票房
    show_count: str = ""         # 场次
    audience_count: str = ""     # 人次
    update_time: str = ""        # 数据更新时间
    movies: List[BoxOfficeMovie] = field(default_factory=list)
    error: Optional[str] = None  # 获取失败时的错误信息

    @property
    def is_valid(self) -> bool:
        """数据是否有效可用。"""
        return self.error is None and bool(self.movies)


def _safe_str(value: Any) -> str:
    """安全转换为字符串，None 返回空串。"""
    if value is None:
        return ""
    return str(value).strip()


def _split_unit_text(obj: Any) -> str:
    """
    处理猫眼新结构 {num, unit}，拼接为 "4984.3万" 这样的文本。
    如果传入的不是字典则退化为字符串。
    """
    if isinstance(obj, dict):
        num = _safe_str(obj.get("num"))
        unit = _safe_str(obj.get("unit"))
        return f"{num}{unit}" if num else ""
    return _safe_str(obj)


def _format_timestamp(ts: Any) -> str:
    """
    把毫秒时间戳格式化为 HH:MM。
    """
    try:
        seconds = int(ts) / 1000
        return datetime.fromtimestamp(seconds).strftime("%H:%M")
    except (TypeError, ValueError, OSError):
        return ""


def fetch_boxoffice(top_n: int = 5) -> BoxOfficeData:
    """
    拉取猫眼实时票房数据。

    :param top_n: 返回榜单前几名，1-10
    :return: 票房数据集合；失败时 error 字段填充原因
    """
    top_n = max(1, min(10, top_n))
    try:
        with httpx2.Client(timeout=_TIMEOUT, follow_redirects=True) as client:
            response = client.get(_MAOYAN_API, headers=_HEADERS)
        if response.status_code != 200:
            return BoxOfficeData(error=f"猫眼接口返回 HTTP {response.status_code}")
        payload = response.json()
    except Exception as error:  # noqa: BLE001 - 网络或解析异常统一降级
        return BoxOfficeData(error=f"获取票房失败：{error}")

    movie_list = payload.get("movieList") or {}
    nation = movie_list.get("nationBoxInfo") or {}

    # 大盘：兼容新旧字段
    total_box = (
        _split_unit_text(nation.get("nationBoxSplitUnit"))
        or _safe_str(nation.get("sumBoxDesc"))
    )
    split_box = (
        _split_unit_text(nation.get("nationSplitBoxSplitUnit"))
        or _safe_str(nation.get("splitSumBox"))
    )
    show_count = _safe_str(nation.get("showCountDesc")) or _safe_str(nation.get("showCount"))
    audience_count = _safe_str(nation.get("viewCountDesc")) or _safe_str(nation.get("peopleCount"))

    # 更新时间：新结构在 movieList.updateInfo.updateTimestamp（毫秒），旧结构是字符串
    update_time = ""
    update_info = movie_list.get("updateInfo")
    if isinstance(update_info, dict):
        update_time = _format_timestamp(update_info.get("updateTimestamp"))
    elif update_info:
        update_time = _safe_str(update_info)

    data = BoxOfficeData(
        total_box=total_box,
        split_box=split_box,
        show_count=show_count,
        audience_count=audience_count,
        update_time=update_time,
    )

    raw_movies = movie_list.get("list") or []
    for index, item in enumerate(raw_movies[:top_n]):
        # 电影名：新结构在 movieInfo.movieName，旧结构直接 movieName
        movie_info = item.get("movieInfo") or {}
        title = _safe_str(movie_info.get("movieName")) or _safe_str(item.get("movieName"))

        # 实时票房：新结构 boxSplitUnit {num,unit}，旧结构 boxDesc
        realtime_box = _split_unit_text(item.get("boxSplitUnit")) or _safe_str(item.get("boxDesc"))

        # 累计票房：新旧都有 sumBoxDesc
        total_box_str = _safe_str(item.get("sumBoxDesc"))

        box_rate = _safe_str(item.get("boxRate"))
        show_rate = _safe_str(item.get("showRate"))

        movie = BoxOfficeMovie(
            rank=index + 1,
            title=title,
            realtime_box=realtime_box,
            total_box=total_box_str,
            box_rate=box_rate,
            show_rate=show_rate,
        )
        if movie.title:
            data.movies.append(movie)

    return data
