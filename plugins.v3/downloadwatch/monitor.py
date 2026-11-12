"""
下载任务的判定与展示逻辑。

这里全部是纯函数：只接收进度、首见时间等普通数值，不导入宿主，方便单测覆盖
阈值边界，也避免把判定规则散落到扫描流程里。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Sequence

# 任务已下载完成（含做种中），任何情况下都不处理
REASON_FINISHED = "finished"
# 观察时长不足阈值
REASON_TOO_YOUNG = "too_young"
# 进度已经跑过阈值，说明任务在动
REASON_PROGRESSING = "progressing"
# 同时满足时长与进度阈值
REASON_REAP = "reap"
# 监控满强制时限仍未下载完成
REASON_FORCE_TIMEOUT = "force_timeout"
# 命中本地黑名单
REASON_BLACKLIST = "blacklist"

# 宿主归一后的任务状态取值
STATE_DOWNLOADING = "downloading"
STATE_PAUSED = "paused"
STATE_COMPLETED = "completed"


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


def is_downloading(state: Optional[str]) -> bool:
    """
    判断任务是否处于「正在下载」状态。

    宿主已把各下载器的原始状态归一成 downloading / paused / completed 三种；
    任务对象缺省 state 时按 downloading 处理，兼容老版本投影。

    :param state: 宿主归一后的任务状态
    """
    text = str(state or "").strip().lower()
    return not text or text == STATE_DOWNLOADING


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


def evaluate_force(
    *,
    progress: Optional[float],
    age_seconds: Optional[int],
    force_hours: int,
) -> Decision:
    """
    按「监控满 N 小时仍未下载完成」判定任务是否需要强制清理。

    与 :func:`evaluate` 并列但互不干扰：这里不看进度阈值，只要任务没到
    ``is_finished`` 的完成线，计时满就清理；这样能兜住「一直在动但 24 小时
    也下不完」的慢种，避免它长期占着下载位。

    :param progress: 下载器报告的完成百分比，取值 0-100
    :param age_seconds: 任务从首次被观测到现在的秒数；None 表示本轮刚开始观察
    :param force_hours: 强制清理时长阈值（小时）
    :return: 判定结果
    """
    if is_finished(progress):
        return Decision(reap=False, reason=REASON_FINISHED)

    if age_seconds is None:
        return Decision(reap=False, reason=REASON_TOO_YOUNG)

    threshold = max(1, int(force_hours)) * 3600
    if int(age_seconds) < threshold:
        return Decision(reap=False, reason=REASON_TOO_YOUNG)

    return Decision(reap=True, reason=REASON_FORCE_TIMEOUT)


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


def format_size(size: Optional[float]) -> str:
    """
    把字节数格式化为便于阅读的容量文本。

    :param size: 文件大小，单位为字节
    :return: 如 `860 B`、`1.4 GB`；取值不可用时返回 `未知`
    """
    try:
        value = float(size or 0)
    except (TypeError, ValueError):
        return "未知"
    if value <= 0:
        return "未知"
    units = ("B", "KB", "MB", "GB", "TB")
    index = 0
    while value >= 1024 and index < len(units) - 1:
        value /= 1024
        index += 1
    if index == 0:
        return f"{int(value)} B"
    return f"{value:.1f} {units[index]}"


def reason_text(reason: str) -> str:
    """
    把判定原因转换成人能看懂的短语，供通知与页面使用。
    """
    if reason == REASON_REAP:
        return "超时且进度停滞"
    if reason == REASON_FORCE_TIMEOUT:
        return "满时限未完成（强制清理）"
    if reason == REASON_BLACKLIST:
        return "命中黑名单"
    if reason == REASON_FINISHED:
        return "已完成"
    if reason == REASON_PROGRESSING:
        return "进度正常"
    return "观察中"


@dataclass(frozen=True)
class NotifyDraft:
    """
    一条待发送的通知。

    :param title: 通知标题
    :param text: 通知正文
    """

    title: str
    text: str


def compose_notify(
    *,
    watched: Sequence[str],
    removed: Sequence[str],
    watched_total: int,
    blacklist_total: int,
    delete_files: bool,
    limit: int = 10,
    force_delete: bool = False,
) -> Optional[NotifyDraft]:
    """
    组装「新增监控 + 已删除」通知。

    两类事件合并进同一条消息，避免一次扫描发两条通知；只有开关打开且确实有
    对应事件时才产出条目。正文按段列举，单段最多 ``limit`` 行。

    :param watched: 本轮新进入监控的任务描述
    :param removed: 本轮删除的任务描述
    :param watched_total: 当前仍在监控中的任务总数
    :param blacklist_total: 当前黑名单条目数
    :param delete_files: 是否连文件一起删除，用于说明删除口径
    :param limit: 每段最多列举的行数
    :param force_delete: 本轮是否存在「满时限强制清理」且连本地文件删除的任务
    :return: 待发送通知；没有可发送内容时返回 None
    """
    watched_lines: List[str] = [str(item) for item in watched if str(item).strip()]
    removed_lines: List[str] = [str(item) for item in removed if str(item).strip()]
    if not watched_lines and not removed_lines:
        return None

    titles: List[str] = []
    if removed_lines:
        titles.append(f"删除 {len(removed_lines)} 个任务")
    if watched_lines:
        titles.append(f"新增监控 {len(watched_lines)} 个任务")
    title = "下载监控：" + "｜".join(titles)

    lines: List[str] = []
    if removed_lines:
        # 强制清理有自己独立的「连文件删」开关，混在一轮通知里时不能只报普通规则的口径
        file_note = "是" if delete_files else "否"
        if force_delete and not delete_files:
            file_note = "按规则（强制超时任务连本地文件删）"
        lines.append(f"【已删除】{len(removed_lines)} 个任务（删除文件：{file_note}）")
        lines.extend(_bullets(removed_lines, limit))
    if watched_lines:
        if lines:
            lines.append("")
        lines.append(f"【新增监控】{len(watched_lines)} 个任务")
        lines.extend(_bullets(watched_lines, limit))
    lines.append("")
    lines.append(f"当前监控中 {max(0, int(watched_total))} 个任务，黑名单 {max(0, int(blacklist_total))} 条。")
    return NotifyDraft(title=title, text="\n".join(lines))


def _bullets(lines: Sequence[str], limit: int) -> List[str]:
    """
    把条目渲染成列表行，超出上限的部分只给数量。
    """
    maximum = max(1, int(limit))
    rendered = [f"· {line}" for line in lines[:maximum]]
    if len(lines) > maximum:
        rendered.append(f"· 其余 {len(lines) - maximum} 条已省略")
    return rendered
