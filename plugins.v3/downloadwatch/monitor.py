"""
下载任务的判定与展示逻辑。

这里全部是纯函数：只接收进度、首见时间等普通数值，不导入宿主，方便单测覆盖
阈值边界，也避免把判定规则散落到扫描流程里。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

# 任务已下载完成（含做种中），任何情况下都不处理
REASON_FINISHED = "finished"
# 观察时长不足阈值
REASON_TOO_YOUNG = "too_young"
# 进度已经跑过阈值，说明任务在动
REASON_PROGRESSING = "progressing"
# 同时满足时长与进度阈值
REASON_REAP = "reap"
# 命中本地黑名单
REASON_BLACKLIST = "blacklist"


@dataclass(frozen=True)
class Decision:
    """
    单个任务的判定结果。

    :param reap: 是否应当删除该任务
    :param reason: 判定原因，供日志和通知展示
    """

    reap: bool
    reason: str


def is_finished(progress: Optional[float]) -> bool:
    """
    判断任务是否已经下载完成。

    :param progress: 下载器报告的完成百分比，取值 0-100
    """
    try:
        value = float(progress or 0)
    except (TypeError, ValueError):
        return False
    # 下载器偶尔会报 99.99x，按已完成处理，避免卡在最后一刻被误删
    return value >= 99.9


def evaluate(
    *,
    progress: Optional[float],
    age_seconds: Optional[int],
    max_hours: int,
    min_progress: int,
) -> Decision:
    """
    按「时长 ≥ X 小时 且 进度 ≤ Y%」判定任务是否卡死。

    :param progress: 下载器报告的完成百分比，取值 0-100
    :param age_seconds: 任务从首次被观测到现在的秒数；None 表示本轮刚开始观察
    :param max_hours: 等待时长阈值（小时）
    :param min_progress: 进度阈值（百分比）
    :return: 判定结果
    """
    if is_finished(progress):
        return Decision(reap=False, reason=REASON_FINISHED)

    if age_seconds is None:
        return Decision(reap=False, reason=REASON_TOO_YOUNG)

    threshold = max(1, int(max_hours)) * 3600
    if int(age_seconds) < threshold:
        return Decision(reap=False, reason=REASON_TOO_YOUNG)

    try:
        current = float(progress or 0)
    except (TypeError, ValueError):
        current = 0.0
    if current > float(min_progress):
        return Decision(reap=False, reason=REASON_PROGRESSING)

    return Decision(reap=True, reason=REASON_REAP)


def format_duration(seconds: Optional[int]) -> str:
    """
    把秒数格式化成便于阅读的时长。

    :param seconds: 秒数；None 或负数按 0 处理
    :return: 如 `35 分钟`、`26.4 小时`、`3.2 天`
    """
    try:
        total = max(0, int(seconds or 0))
    except (TypeError, ValueError):
        total = 0
    if total < 60:
        return "不到 1 分钟"
    if total < 3600:
        return f"{total // 60} 分钟"
    if total < 86400:
        return f"{total / 3600:.1f} 小时"
    return f"{total / 86400:.1f} 天"


def progress_text(progress: Optional[float]) -> str:
    """
    把进度格式化为一位小数的百分比文本。
    """
    try:
        value = float(progress or 0)
    except (TypeError, ValueError):
        value = 0.0
    return f"{max(0.0, min(100.0, value)):.1f}%"
