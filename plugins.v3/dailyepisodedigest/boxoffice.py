"""
猫眼实时票房数据获取。

从猫眼专业版 dashboard 页面的 AJAX 接口拉取当日票房榜单，
供日报卡片末尾追加展示。

接口地址：https://piaofang.maoyan.com/dashboard-ajax/movie
该接口无需签名或 token，直接 GET 即可返回完整 JSON。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

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
    realtime_box: str = ""       # 实时票房（万元）
    total_box: str = ""          # 累计票房（万元）
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


def _safe_str(value) -> str:
    """安全转换为字符串，None 返回空串。"""
    if value is None:
        return ""
    return str(value).strip()


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

    data = BoxOfficeData(
        total_box=_safe_str(nation.get("sumBoxDesc")),
        split_box=_safe_str(nation.get("splitSumBox")),
        show_count=_safe_str(nation.get("showCount")),
        audience_count=_safe_str(nation.get("peopleCount")),
        update_time=_safe_str(nation.get("updateInfo")),
    )

    raw_movies = movie_list.get("list") or []
    for index, item in enumerate(raw_movies[:top_n]):
        movie = BoxOfficeMovie(
            rank=index + 1,
            title=_safe_str(item.get("movieName")),
            realtime_box=_safe_str(item.get("boxDesc")),
            total_box=_safe_str(item.get("sumBoxDesc")),
            box_rate=_safe_str(item.get("boxRate")),
            show_rate=_safe_str(item.get("showRate")),
        )
        if movie.title:
            data.movies.append(movie)

    return data
