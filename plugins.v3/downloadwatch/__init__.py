"""
下载监控插件主入口。

职责：按配置周期扫描下载器中「正在下载」的任务，两条规则任一命中即清理：
①「已等待时长 ≥ X 小时且进度 ≤ Y%」的卡死任务；②监控满 24 小时仍未下载完成的
任务（默认连本地文件一起删）。删除后把该种子记入本地黑名单；黑名单里的种子再次被
订阅下载时会被立即清理，从而避免同一个坏种子反复占用下载位。

删除任务时按下载历史定位对应订阅，把该集从订阅的「已下载进度」里撤回，否则宿主会认为
这一集已经下过、之后永远不再搜索新的种子。可选地再把种子标题追加进订阅排除规则，
从源头阻止重复下载（副作用是该标题的所有种子都会被排除）。
详情页用四个按钮切换监控中、已删除、黑名单与最近命中四个列表，任务进入监控与被删除
时均可推送通知；黑名单列表里的每一条都可以单独移除，误拉黑的种子不必手动改数据库。
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from apscheduler.triggers.cron import CronTrigger
from pydantic import BaseModel, Field

from app import schemas
from app.chain.download import DownloadChain
from app.db.oper.downloadhistory import DownloadHistoryOper
from app.db.oper.subscribe import SubscribeOper
from app.schemas.types import EventType, MediaType, MessageType
from app.sdk import scheduler as scheduler_sdk
from app.sdk.events import Event, eventmanager
from app.sdk.logging import logger
from app.sdk.plugin import _PluginBase

from .config import (
    DEFAULT_CONFIG,
    MAX_BLACKLIST,
    MAX_LIST_ROWS,
    MAX_NOTIFY_LINES,
    MAX_REMOVED_RECORDS,
    MAX_WATCH_RECORDS,
    merge_exclude,
    normalize_config,
    normalize_title,
    parse_episodes,
    parse_season,
    remove_blacklist_item,
    split_tags,
    tags_match,
)
from .monitor import (
    REASON_BLACKLIST,
    REASON_FORCE_TIMEOUT,
    compose_notify,
    evaluate,
    evaluate_force,
    format_duration,
    format_size,
    is_downloading,
    is_finished,
    progress_text,
    reason_text,
)

# 观察状态与黑名单的持久化键
_STATE_KEY = "watch_state"
# 详情页展示用的监控中/已删除记录
_RECORDS_KEY = "watch_records"
# 最近一次扫描结果的持久化键，供详情页在实例重建后仍能展示
_LAST_SCAN_KEY = "last_scan"
# 详情页当前选中分页的持久化键
_PAGE_TAB_KEY = "page_tab"
# 详情页的四个分页，(键, 按钮文字)
_PAGE_TABS: Tuple[Tuple[str, str], ...] = (
    ("watched", "监控中"),
    ("removed", "已删除"),
    ("blacklist", "黑名单"),
    ("detail", "最近命中"),
)
_DEFAULT_TAB = _PAGE_TABS[0][0]
# 一次性扫描任务的稳定标识
_ONCE_JOB_ID = "watch_once"
# 远程命令的动作标识，同时用于命令声明与事件判定
_ACTION_SCAN = "watch_scan"
_ACTION_STATUS = "watch_status"
# 本插件通知使用的场景类型，渠道必须勾选该场景才能收到
_MESSAGE_TYPE = MessageType.Plugin


class ScanResultData(BaseModel):
    """
    一次扫描的业务结果，作为统一响应的 data 段返回。
    """

    scanned: int = Field(default=0, description="本轮读取到的任务总数")
    removed: int = Field(default=0, description="本轮删除的任务数")
    waiting: int = Field(default=0, description="仍在观察中的任务数")
    blacklist: int = Field(default=0, description="黑名单条目数")
    dry_run: bool = Field(default=False, description="是否处于演练模式")
    detail: List[str] = Field(default_factory=list, description="本轮处理明细")


class StatusData(BaseModel):
    """
    详情页与状态接口共用的运行快照。
    """

    enabled: bool = Field(default=False, description="插件是否启用")
    max_hours: int = Field(default=0, description="等待时长阈值（小时）")
    min_progress: int = Field(default=0, description="进度阈值（百分比）")
    force_timeout: bool = Field(default=True, description="是否开启满时限强制清理")
    force_hours: int = Field(default=24, description="强制清理时长阈值（小时）")
    force_delete_files: bool = Field(default=True, description="强制清理时是否删除本地文件")
    tags: List[str] = Field(default_factory=list, description="监控标签")
    blacklist: int = Field(default=0, description="黑名单条目数")
    watched: int = Field(default=0, description="当前监控中的任务数")
    removed: int = Field(default=0, description="已删除记录条数")
    last_scan: Optional[str] = Field(default=None, description="最近一次扫描时间")
    last_message: Optional[str] = Field(default=None, description="最近一次扫描结论")


class DownloadWatch(_PluginBase):
    """
    下载任务超时清理与拉黑插件。
    """

    plugin_name = "下载监控"
    plugin_desc = "定时扫描正在下载的任务：超时且进度停滞，或监控满 24 小时仍未完成，都会自动删除（后者默认连本地文件一起删）；删除时回滚订阅进度并拉黑种子，让订阅重新搜索其它种子。"
    plugin_icon = "https://raw.githubusercontent.com/jxxghp/MoviePilot-Plugins/main/icons/Moviepilot_A.png"
    plugin_version = "1.6.0"
    plugin_label = "下载,清理"
    plugin_author = "kongchengll"
    plugin_config_prefix = "downloadwatch_"
    plugin_order = 70
    auth_level = 1

    def __init__(self) -> None:
        """
        初始化配置快照与运行态标记。
        """
        super().__init__()
        self._config: Dict[str, Any] = dict(DEFAULT_CONFIG)
        self._enabled = False
        self._cron_valid = True
        # 当前实例内的最近一次扫描结论，落盘后仍可恢复
        self._last_scan: Dict[str, Any] = {}
        # 详情页展示用的监控中与已删除记录
        self._records: Dict[str, List[Dict[str, Any]]] = {"watched": [], "removed": []}
        # 详情页当前选中的分页
        self._page_tab = _DEFAULT_TAB

    def init_plugin(self, config: Optional[Dict[str, Any]] = None) -> None:
        """
        读取配置并重建运行态，可被宿主重复调用。
        """
        # 先释放上一轮登记的一次性任务，保证热重载不残留
        self.stop_service()
        self._config = normalize_config(config)
        self._enabled = bool(self._config.get("enabled"))
        self._cron_valid = self._check_cron(self._config.get("cron", ""))
        self._last_scan = self._load_last_scan()
        self._records = self._load_records()
        self._page_tab = self._validate_tab(self._load_page_tab())

        if not self._enabled:
            logger.debug("下载监控：插件未启用")
            return
        if not self._cron_valid:
            logger.error(f"下载监控：cron 表达式非法 {self._config.get('cron')}，定时扫描不会生效")
        logger.info(
            "下载监控：已启用，卡死规则为等待 ≥ "
            f"{self._config.get('max_hours')} 小时且进度 ≤ {self._config.get('min_progress')}%，"
            f"强制清理：{'满 ' + str(self._config.get('force_hours')) + ' 小时未完成即删' if self._config.get('force_timeout') else '关'}，"
            f"扫描周期 {self._config.get('cron')}"
        )
        self._schedule_once()

    def _check_cron(self, expression: str) -> bool:
        """
        校验 cron 表达式是否可被调度器解析。
        """
        try:
            CronTrigger.from_crontab(expression)
            return True
        except Exception:  # noqa: BLE001 - 任意解析异常都按非法表达式处理
            return False

    def _schedule_once(self) -> None:
        """
        处理「保存后立即扫描一次」开关，并复位开关避免下次启动重复触发。
        """
        if not self._config.get("once"):
            return
        self._config["once"] = False
        try:
            self.update_config(self._config)
        except Exception as error:  # noqa: BLE001 - 回写失败不应阻断本次扫描
            logger.warning(f"下载监控：复位立即扫描开关失败：{error}")
        try:
            scheduler_sdk.add_plugin_once_job(
                self.__class__.__name__,
                _ONCE_JOB_ID,
                self.scan,
                "下载监控立即扫描",
                delay_seconds=3,
            )
            logger.info("下载监控：已登记一次立即扫描")
        except Exception as error:  # noqa: BLE001 - 宿主版本差异时降级为同步执行
            logger.warning(f"下载监控：登记立即扫描失败，改为同步执行：{error}")
            self.scan()

    def get_state(self) -> bool:
        """
        返回插件是否启用。
        """
        return self._enabled

    # ------------------------------------------------------------------
    # 扫描与清理
    # ------------------------------------------------------------------

    def scan(self, notify: Optional[bool] = None) -> Dict[str, Any]:
        """
        执行一轮扫描，按阈值删除卡死任务并维护黑名单。

        :param notify: 是否发送通知；None 表示沿用配置
        :return: 扫描结论字典，供接口、命令与页面复用
        """
        if not self._enabled:
            return self._summary(success=False, message="插件未启用，跳过扫描")

        chain = DownloadChain()
        try:
            # include_all_tags 必须为 True：默认只返回带宿主动态标签的任务，
            # 用户自己打的标签任务会被整体漏掉
            torrents = chain.list_torrents(include_all_tags=True) or []
        except Exception as error:  # noqa: BLE001 - 下载器不可用时只记录不抛出
            logger.error(f"下载监控：读取下载任务失败：{error}")
            return self._summary(success=False, message=f"读取下载任务失败：{error}")

        now = int(time.time())
        state = self._load_state()
        first_seen: Dict[str, Dict[str, Any]] = state["first_seen"]
        blacklist: List[Dict[str, Any]] = state["blacklist"]
        black_hashes = {str(item.get("hash") or "").lower() for item in blacklist}

        wanted_tags = split_tags(self._config.get("tags"))
        max_hours = int(self._config.get("max_hours") or 24)
        min_progress = int(self._config.get("min_progress") or 0)
        force_timeout = bool(self._config.get("force_timeout"))
        force_hours = int(self._config.get("force_hours") or 24)
        force_delete_files = bool(self._config.get("force_delete_files"))
        use_blacklist = bool(self._config.get("use_blacklist"))
        delete_files = bool(self._config.get("delete_files"))
        dry_run = bool(self._config.get("dry_run"))

        alive: set[str] = set()
        # 处于「正在下载」状态的任务数，用于说明本轮实际参与判定的范围
        downloading_total = 0
        detail: List[str] = []
        removed = 0
        # 本轮由「满时限未完成」规则实际清理的任务数，用于说明删除文件的口径
        force_removed = 0
        waiting = 0
        # 本轮新进入监控的任务描述，以及详情页用的监控中快照
        watch_events: List[str] = []
        watch_rows: List[Dict[str, Any]] = []
        # 本轮删除的任务描述，供通知与详情页使用
        remove_events: List[str] = []
        remove_rows: List[Dict[str, Any]] = []
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        for torrent in torrents:
            torrent_hash = str(getattr(torrent, "hash", "") or "").strip().lower()
            if not torrent_hash:
                continue
            alive.add(torrent_hash)

            title = normalize_title(getattr(torrent, "title", "") or getattr(torrent, "name", ""))
            title = title or torrent_hash
            progress = getattr(torrent, "progress", 0)
            downloader = str(getattr(torrent, "downloader", "") or "")
            size = format_size(getattr(torrent, "size", 0))
            tags_text = str(getattr(torrent, "tags", "") or "")
            speed = str(getattr(torrent, "dlspeed", "") or "")

            # 只监控正在下载的任务：暂停、已完成、做种中等任务一律跳过，
            # 同时清掉观察计时，避免任务恢复下载后沿用暂停期间的等待时长
            if not is_downloading(getattr(torrent, "state", None)):
                first_seen.pop(torrent_hash, None)
                continue
            downloading_total += 1

            # 已完成或做种中的任务永不处理，同时清掉观察计时
            if is_finished(progress):
                first_seen.pop(torrent_hash, None)
                continue

            record = first_seen.get(torrent_hash)
            is_new = record is None
            age_seconds: Optional[int] = None
            if record:
                try:
                    age_seconds = max(0, now - int(record.get("ts") or now))
                except (TypeError, ValueError):
                    age_seconds = 0
            else:
                # 首次观测到该任务，只记录时间，本轮不判定：
                # 宿主返回的任务对象没有「添加时间」字段，只能用首见时间近似等待时长
                first_seen[torrent_hash] = {
                    "ts": now,
                    "title": title,
                    "downloader": downloader,
                }

            if use_blacklist and torrent_hash in black_hashes:
                reason = REASON_BLACKLIST
                item_delete_files = delete_files
            else:
                if not tags_match(tags_text, wanted_tags):
                    # 标签不匹配的任务不参与判定，但保留观察计时
                    continue
                # 先判「满时限未完成」：这条规则不看进度，命中后按自己的开关删本地文件，
                # 优先级高于卡死规则，避免同一个任务被算成两条原因
                force_decision = (
                    evaluate_force(
                        progress=progress,
                        age_seconds=age_seconds,
                        force_hours=force_hours,
                    )
                    if force_timeout
                    else None
                )
                if force_decision and force_decision.reap:
                    reason = REASON_FORCE_TIMEOUT
                    item_delete_files = True if force_delete_files else delete_files
                else:
                    decision = evaluate(
                        progress=progress,
                        age_seconds=age_seconds,
                        max_hours=max_hours,
                        min_progress=min_progress,
                    )
                    if not decision.reap:
                        watch_rows.append(
                            self._watch_row(
                                title=title,
                                progress=progress,
                                age_seconds=age_seconds,
                                size=size,
                                tags=tags_text,
                                downloader=downloader,
                                speed=speed,
                                first_ts=record.get("ts") if record else now,
                            )
                        )
                        if is_new:
                            watch_events.append(
                                self._describe(torrent, title, progress, age_seconds, decision.reason)
                            )
                        continue
                    reason = decision.reason
                    item_delete_files = delete_files

            label = self._describe(torrent, title, progress, age_seconds, reason)
            if reason == REASON_FORCE_TIMEOUT and item_delete_files:
                label += " · 连本地文件一起删"
            if dry_run:
                detail.append(f"{label}（演练模式，未删除）")
                continue

            if not self._remove(
                chain, torrent_hash, getattr(torrent, "downloader", None), item_delete_files
            ):
                continue

            removed += 1
            if reason == REASON_FORCE_TIMEOUT:
                force_removed += 1
            first_seen.pop(torrent_hash, None)
            if use_blacklist:
                # 同一个种子被反复下载时只保留最新一条，否则列表里会堆出一串同名记录
                blacklist = [
                    item
                    for item in blacklist
                    if str(item.get("hash") or "").strip().lower() != torrent_hash
                ]
                blacklist.append(
                    {
                        "hash": torrent_hash,
                        "title": title,
                        "downloader": downloader,
                        "removed_at": stamp,
                        "reason": reason,
                    }
                )
                black_hashes.add(torrent_hash)

            note = ""
            # 删除任务后必须先回收订阅进度：宿主在提交下载时就写入了订阅的「已下载集数」，
            # 不回滚的话订阅会认为该集已完成，之后无论重新订阅还是刷新都不会再搜这一集
            history = self._load_download_history(torrent_hash)
            progress_note = self._rollback_subscribe(history, title)
            exclude_note = ""
            if self._config.get("sync_subscribe"):
                exclude_note = self._sync_subscribe_exclude(torrent, title, history)
            note = "；".join(part for part in (progress_note, exclude_note) if part)
            detail.append(f"{label}{'｜' + note if note else ''}")
            remove_events.append(label)
            remove_rows.append(
                self._remove_row(
                    title=title,
                    progress=progress,
                    age_seconds=age_seconds,
                    reason=reason,
                    size=size,
                    downloader=downloader,
                    delete_files=item_delete_files,
                    removed_at=stamp,
                    note=note,
                )
            )

        # 只保留仍然存在的任务，避免长期运行后观察表无限增长
        first_seen = {key: value for key, value in first_seen.items() if key in alive}
        if len(blacklist) > MAX_BLACKLIST:
            blacklist = blacklist[-MAX_BLACKLIST:]
        # 「仍在观察」与详情页表格口径保持一致：标签命中的未完成任务
        waiting = len(watch_rows)
        self._save_state({"first_seen": first_seen, "blacklist": blacklist})

        # 监控中快照按本轮结果整体替换；已删除记录按时间累积，超上限裁掉最旧的
        previous_removed = self._records.get("removed") or []
        records = {
            "watched": watch_rows[-MAX_WATCH_RECORDS:],
            "removed": (previous_removed + remove_rows)[-MAX_REMOVED_RECORDS:],
        }
        self._records = records
        self._save_records(records)

        message = self._compose_message(
            len(torrents), downloading_total, removed, waiting, len(blacklist), dry_run, detail
        )
        summary = self._summary(
            success=True,
            message=message,
            scanned=len(torrents),
            removed=removed,
            force_removed=force_removed,
            waiting=waiting,
            blacklist=len(blacklist),
            dry_run=dry_run,
            detail=detail[:MAX_NOTIFY_LINES],
            watched=len(watch_rows),
            watched_new=len(watch_events),
            removed_list=len(records["removed"]),
        )
        self._remember_scan(summary)

        self._notify_scan(
            notify=notify,
            watch_events=watch_events,
            remove_events=remove_events,
            watched_total=len(watch_rows),
            blacklist_total=len(blacklist),
            delete_files=delete_files,
            force_delete=bool(force_removed),
            dry_run=dry_run,
        )
        return summary

    def _notify_scan(
        self,
        *,
        notify: Optional[bool],
        watch_events: List[str],
        remove_events: List[str],
        watched_total: int,
        blacklist_total: int,
        delete_files: bool,
        force_delete: bool = False,
        dry_run: bool = False,
    ) -> None:
        """
        按开关发送「新增监控 + 已删除」通知。

        两类事件合并成一条消息，避免一次扫描刷两条通知；演练模式下删除事件
        不会产生，因此只会看到新增监控。

        :param notify: 调用方指定的通知开关；None 表示沿用配置
        :param watch_events: 本轮新进入监控的任务描述
        :param remove_events: 本轮实际删除的任务描述
        :param watched_total: 当前监控中任务数
        :param blacklist_total: 黑名单条目数
        :param delete_files: 删除任务时是否连文件一起删
        :param force_delete: 本轮是否有按强制时限清理且连文件删除的任务
        :param dry_run: 是否处于演练模式
        """
        if notify is None:
            if not self._config.get("notify"):
                return
            notify = True
        if not notify:
            return

        watched = watch_events if self._config.get("notify_watch") else []
        removed = remove_events if self._config.get("notify_remove") else []
        draft = compose_notify(
            watched=watched,
            removed=removed,
            watched_total=watched_total,
            blacklist_total=blacklist_total,
            delete_files=delete_files,
            limit=MAX_NOTIFY_LINES,
            force_delete=force_delete,
        )
        if draft is None:
            return
        title = f"【演练】{draft.title}" if dry_run and not removed else draft.title
        self.post_message(mtype=_MESSAGE_TYPE, title=title, text=draft.text)

    @staticmethod
    def _watch_row(
        *,
        title: str,
        progress: Any,
        age_seconds: Optional[int],
        size: str,
        tags: str,
        downloader: str,
        speed: str,
        first_ts: Any,
    ) -> Dict[str, Any]:
        """
        生成详情页「监控中」表格的一行。
        """
        try:
            first_text = datetime.fromtimestamp(int(first_ts)).strftime("%m-%d %H:%M")
        except (TypeError, ValueError, OSError):
            first_text = "—"
        return {
            "title": title,
            "progress": progress_text(progress),
            "wait": format_duration(age_seconds) if age_seconds is not None else "刚开始",
            "size": size,
            "speed": speed or "—",
            "tags": tags or "—",
            "downloader": downloader or "—",
            "since": first_text,
        }

    @staticmethod
    def _remove_row(
        *,
        title: str,
        progress: Any,
        age_seconds: Optional[int],
        reason: str,
        size: str,
        downloader: str,
        delete_files: bool,
        removed_at: str,
        note: str,
    ) -> Dict[str, Any]:
        """
        生成详情页「已删除」表格的一行。
        """
        return {
            "time": removed_at,
            "title": title,
            "progress": progress_text(progress),
            "wait": format_duration(age_seconds) if age_seconds is not None else "—",
            "size": size,
            "reason": reason_text(reason),
            "downloader": downloader or "—",
            "files": "已删除" if delete_files else "保留",
            "note": note or "—",
        }

    def _remove(
        self,
        chain: DownloadChain,
        torrent_hash: str,
        downloader: Optional[str],
        delete_files: bool,
    ) -> bool:
        """
        删除指定下载任务。

        :param chain: 下载链实例，复用同一实例可避免重复构造组合根
        :param torrent_hash: 任务 hash
        :param downloader: 任务所属下载器名称，可能为空
        :param delete_files: 是否同时删除已下载文件
        :return: 下载器是否确认执行删除
        """
        try:
            result = chain.remove_torrents(
                hashs=[torrent_hash],
                delete_file=delete_files,
                downloader=downloader or None,
            )
        except Exception as error:  # noqa: BLE001 - 单个任务失败不影响整轮扫描
            logger.warning(f"下载监控：删除任务 {torrent_hash} 异常：{error}")
            return False
        if not result:
            logger.warning(f"下载监控：下载器未确认删除任务 {torrent_hash}")
            return False
        logger.info(f"下载监控：已删除任务 {torrent_hash}（删除文件={delete_files}）")
        return True

    @staticmethod
    def _load_download_history(torrent_hash: str) -> Optional[Any]:
        """
        读取任务对应的下载历史，用于定位订阅与集数。

        宿主在把种子提交给下载器时就写入了下载历史，这里读到的记录带着媒体来源、
        媒体 ID 与集数文本，是插件唯一能可靠还原「这个任务属于哪个订阅的哪一集」的依据。

        :param torrent_hash: 下载任务 hash
        :return: 下载历史记录；读取失败或不存在时返回 None
        """
        try:
            return DownloadHistoryOper().get_by_hash(torrent_hash)
        except Exception as error:  # noqa: BLE001 - 历史不可读时只跳过依赖它的步骤
            logger.warning(f"下载监控：读取下载历史失败：{error}")
            return None

    @staticmethod
    def _match_subscriptions(
        media_source: str,
        media_id: str,
        mtype: str,
        season: Optional[int],
    ) -> List[Any]:
        """
        按媒体标识定位订阅；能确定季号时只保留同季订阅。

        :param media_source: 媒体来源，例如 tmdb
        :param media_id: 媒体来源内的 ID
        :param mtype: 媒体类型
        :param season: 季号，可为空
        :return: 匹配到的订阅列表
        """
        try:
            subscriptions = SubscribeOper().list()
        except Exception as error:  # noqa: BLE001 - 订阅不可读时不影响删除结论
            logger.warning(f"下载监控：读取订阅失败：{error}")
            return []
        matched = [
            item
            for item in subscriptions
            if str(getattr(item, "media_source", "") or "") == media_source
            and str(getattr(item, "media_id", "") or "") == media_id
            and str(getattr(item, "type", "") or "") == mtype
        ]
        if len(matched) > 1 and season is not None:
            same_season = [item for item in matched if getattr(item, "season", None) == season]
            if same_season:
                matched = same_season
        return matched

    def _rollback_subscribe(self, history: Optional[Any], title: str) -> str:
        """
        把被删任务覆盖的集数从订阅的已下载进度里撤回。

        宿主提交下载时就把这些集写进了订阅的 note（以及洗版订阅的 episode_priority），
        不撤回则订阅认为该集已经下过，永远不会再搜索新的种子。

        :param history: 该任务的下载历史，可为 None
        :param title: 任务标题，历史缺少集数文本时用标题兜底解析
        :return: 处理结论文本
        """
        if not history:
            return "无下载历史，未回滚订阅进度"
        media_source = str(getattr(history, "media_source", "") or "")
        media_id = str(getattr(history, "media_id", "") or "")
        mtype = str(getattr(history, "type", "") or "")
        if not media_source or not media_id or not mtype:
            return "下载历史缺少媒体标识，未回滚订阅进度"

        season = parse_season(getattr(history, "seasons", None))
        subscriptions = self._match_subscriptions(media_source, media_id, mtype, season)
        if not subscriptions:
            return "未找到对应订阅，未回滚订阅进度"
        if len(subscriptions) > 1:
            return f"匹配到 {len(subscriptions)} 个订阅，未回滚订阅进度"

        subscribe = subscriptions[0]
        episodes = parse_episodes(getattr(history, "episodes", None)) or parse_episodes(title)
        note = [int(item) for item in (getattr(subscribe, "note", None) or []) if str(item).isdigit()]
        if mtype == MediaType.TV.value:
            if not episodes:
                return "无法确定被删任务的集数，未回滚订阅进度"
            targets = set(episodes)
        else:
            # 电影与音乐只有一项，插件删除任务即视为该项未完成
            targets = set(note)

        payload: Dict[str, Any] = {}
        new_note = [episode for episode in note if episode not in targets]
        if new_note != note:
            payload["note"] = new_note
        priority = dict(getattr(subscribe, "episode_priority", None) or {})
        if priority:
            target_keys = {str(item) for item in targets}
            kept = {key: value for key, value in priority.items() if str(key) not in target_keys}
            if kept != priority:
                payload["episode_priority"] = kept
        if not payload:
            return f"订阅《{subscribe.name}》未记录该集，无需回滚"

        try:
            SubscribeOper().update(subscribe.id, payload)
        except Exception as error:  # noqa: BLE001 - 写失败需要显式告知用户
            logger.warning(f"下载监控：回滚订阅进度失败：{error}")
            return "回滚订阅进度失败"

        # 进度回滚后这份下载历史已无对应任务，顺手删掉，避免订阅详情继续显示已下载文件
        history_id = getattr(history, "id", None)
        if history_id is not None:
            try:
                DownloadHistoryOper().delete_history(int(history_id))
            except Exception as error:  # noqa: BLE001 - 历史清理失败不影响进度回滚
                logger.warning(f"下载监控：删除下载历史失败：{error}")
        logger.info(
            f"下载监控：已从订阅《{subscribe.name}》回滚 {sorted(targets)} 的下载进度"
        )
        return f"已回滚订阅《{subscribe.name}》的 {len(targets)} 集进度"

    def _sync_subscribe_exclude(self, torrent: Any, title: str, history: Optional[Any]) -> str:
        """
        把种子标题追加到匹配订阅的排除规则，阻止再次订阅时选中同一个种子。

        只有在能唯一定位到订阅时才写入，宁可跳过也不批量改用户配置。

        :param torrent: 下载任务对象
        :param title: 已归一化的标题，仅用于兜底匹配
        :param history: 该任务的下载历史，可为 None
        :return: 处理结论文本
        """
        if not history:
            return "无下载历史，未同步排除规则"
        media_source = str(getattr(history, "media_source", "") or "")
        media_id = str(getattr(history, "media_id", "") or "")
        mtype = str(getattr(history, "type", "") or "")
        if not media_source or not media_id or not mtype:
            return "下载历史缺少媒体标识，未同步排除规则"

        season = parse_season(getattr(history, "seasons", None)) or parse_season(
            getattr(torrent, "season_episode", None)
        )
        matched = self._match_subscriptions(media_source, media_id, mtype, season)
        if not matched:
            return "未找到对应订阅，仅保留本地黑名单"
        if len(matched) > 1:
            return f"匹配到 {len(matched)} 个订阅，未自动修改排除规则"

        subscribe = matched[0]
        current = subscribe.exclude or ""
        merged = merge_exclude(current, getattr(torrent, "title", "") or title)
        if merged == current.strip().rstrip("|"):
            return f"订阅《{subscribe.name}》已包含该排除规则"
        try:
            SubscribeOper().update(subscribe.id, {"exclude": merged})
        except Exception as error:  # noqa: BLE001 - 写配置失败需要显式告知用户
            logger.warning(f"下载监控：写入订阅排除规则失败：{error}")
            return "写入订阅排除规则失败"
        logger.info(f"下载监控：已把种子标题写入订阅《{subscribe.name}》的排除规则")
        return f"已写入订阅《{subscribe.name}》排除规则"

    @staticmethod
    def _describe(
        torrent: Any,
        title: str,
        progress: Any,
        age_seconds: Optional[int],
        reason: str,
    ) -> str:
        """
        生成一条便于阅读的处理说明。
        """
        parts = [title]
        season_episode = str(getattr(torrent, "season_episode", "") or "")
        if season_episode:
            parts.append(season_episode)
        parts.append(f"进度 {progress_text(progress)}")
        if age_seconds is not None:
            parts.append(f"已等待 {format_duration(age_seconds)}")
        if reason == REASON_FORCE_TIMEOUT:
            parts.append("满时限未完成，强制清理")
        elif reason == REASON_BLACKLIST:
            parts.append("命中黑名单")
        downloader = str(getattr(torrent, "downloader", "") or "")
        if downloader:
            parts.append(downloader)
        return " · ".join(parts)

    @staticmethod
    def _compose_message(
        scanned: int,
        downloading: int,
        removed: int,
        waiting: int,
        blacklist: int,
        dry_run: bool,
        detail: List[str],
    ) -> str:
        """
        组装通知与页面共用的结论文本。
        """
        head = (
            f"本轮读取 {scanned} 个任务（正在下载 {downloading} 个），"
            f"清理 {removed} 个，仍在观察 {waiting} 个，黑名单 {blacklist} 条。"
        )
        if dry_run:
            head = "【演练模式】" + head + "本次未真正删除任何任务。"
        if not detail:
            return head
        lines = [head, ""]
        for item in detail[:MAX_NOTIFY_LINES]:
            lines.append(f"· {item}")
        if len(detail) > MAX_NOTIFY_LINES:
            lines.append(f"· 其余 {len(detail) - MAX_NOTIFY_LINES} 条已省略")
        return "\n".join(lines)

    @staticmethod
    def _summary(success: bool, message: str, **kwargs: Any) -> Dict[str, Any]:
        """
        构造统一的扫描结论字典。
        """
        summary = {
            "success": success,
            "message": message,
            "scanned": 0,
            "removed": 0,
            "waiting": 0,
            "blacklist": 0,
            "dry_run": False,
            "detail": [],
        }
        summary.update(kwargs)
        return summary

    def _remember_scan(self, summary: Dict[str, Any]) -> None:
        """
        记录最近一次扫描结论，供页面与状态接口展示。
        """
        self._last_scan = {
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "message": summary.get("message") or "",
            "removed": summary.get("removed") or 0,
            "detail": list(summary.get("detail") or []),
        }
        try:
            self.save_data(_LAST_SCAN_KEY, self._last_scan)
        except Exception as error:  # noqa: BLE001 - 记录失败不影响扫描结论
            logger.debug(f"下载监控：保存扫描记录失败：{error}")

    def _load_last_scan(self) -> Dict[str, Any]:
        """
        读取上一次扫描结论。
        """
        try:
            data = self.get_data(_LAST_SCAN_KEY)
        except Exception as error:  # noqa: BLE001 - 数据缺失按无记录处理
            logger.debug(f"下载监控：读取扫描记录失败：{error}")
            return {}
        return dict(data) if isinstance(data, dict) else {}

    def _load_state(self) -> Dict[str, Any]:
        """
        读取观察表与黑名单，异常结构按空处理。
        """
        try:
            raw = self.get_data(_STATE_KEY)
        except Exception as error:  # noqa: BLE001 - 数据缺失按空状态处理
            logger.debug(f"下载监控：读取运行状态失败：{error}")
            raw = None
        state = raw if isinstance(raw, dict) else {}
        first_seen = state.get("first_seen")
        blacklist = state.get("blacklist")
        return {
            "first_seen": dict(first_seen) if isinstance(first_seen, dict) else {},
            "blacklist": [item for item in blacklist if isinstance(item, dict)] if isinstance(blacklist, list) else [],
        }

    def _save_state(self, state: Dict[str, Any]) -> None:
        """
        持久化观察表与黑名单。
        """
        try:
            self.save_data(_STATE_KEY, state)
        except Exception as error:  # noqa: BLE001 - 保存失败只记录，避免影响删除结果
            logger.warning(f"下载监控：保存运行状态失败：{error}")

    def _load_records(self) -> Dict[str, List[Dict[str, Any]]]:
        """
        读取详情页展示用的监控中与已删除记录。
        """
        try:
            raw = self.get_data(_RECORDS_KEY)
        except Exception as error:  # noqa: BLE001 - 数据缺失按空记录处理
            logger.debug(f"下载监控：读取记录失败：{error}")
            raw = None
        data = raw if isinstance(raw, dict) else {}
        return {
            "watched": [item for item in data.get("watched") or [] if isinstance(item, dict)]
            if isinstance(data.get("watched"), list) else [],
            "removed": [item for item in data.get("removed") or [] if isinstance(item, dict)]
            if isinstance(data.get("removed"), list) else [],
        }

    def _save_records(self, records: Dict[str, Any]) -> None:
        """
        持久化详情页展示用的记录。
        """
        try:
            self.save_data(_RECORDS_KEY, records)
        except Exception as error:  # noqa: BLE001 - 记录失败不影响扫描结论
            logger.warning(f"下载监控：保存记录失败：{error}")

    @staticmethod
    def _validate_tab(tab: Any) -> str:
        """
        把任意输入收敛到合法分页键。
        """
        text = str(tab or "").strip().lower()
        return text if text in {key for key, _ in _PAGE_TABS} else _DEFAULT_TAB

    def _load_page_tab(self) -> str:
        """
        读取详情页上次选中的分页。
        """
        try:
            return self._validate_tab(self.get_data(_PAGE_TAB_KEY))
        except Exception as error:  # noqa: BLE001 - 读取失败回落到默认分页
            logger.debug(f"下载监控：读取分页失败：{error}")
            return _DEFAULT_TAB

    def _save_page_tab(self, tab: str) -> str:
        """
        持久化详情页选中的分页。
        """
        value = self._validate_tab(tab)
        self._page_tab = value
        try:
            self.save_data(_PAGE_TAB_KEY, value)
        except Exception as error:  # noqa: BLE001 - 保存失败不影响本次切换
            logger.warning(f"下载监控：保存分页失败：{error}")
        return value

    # ------------------------------------------------------------------
    # 命令与接口
    # ------------------------------------------------------------------

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """
        注册远程命令，支持手动扫描与查看状态。
        """
        return [
            {
                "cmd": "/dw_scan",
                "event": EventType.PluginAction,
                "desc": "下载监控：立即扫描一次",
                "category": "插件命令",
                "data": {"action": _ACTION_SCAN},
            },
            {
                "cmd": "/dw_status",
                "event": EventType.PluginAction,
                "desc": "下载监控：查看最近一次扫描结果",
                "category": "插件命令",
                "data": {"action": _ACTION_STATUS},
            },
        ]

    @eventmanager.register(EventType.PluginAction)
    def handle_command(self, event: Event) -> None:
        """
        只处理属于本插件的远程命令动作。
        """
        event_data = event.event_data or {}
        action = event_data.get("action")
        if action not in {_ACTION_SCAN, _ACTION_STATUS}:
            return
        if action == _ACTION_SCAN:
            result = self.scan(notify=False)
            self.post_message(mtype=_MESSAGE_TYPE, title="下载监控", text=str(result.get("message") or ""))
            return
        status = self.api_status().data
        text = (
            f"插件状态：{'已启用' if status and status.enabled else '未启用'}\n"
            f"判定条件：等待 ≥ {status.max_hours if status else 0} 小时且进度 ≤ {status.min_progress if status else 0}%\n"
            f"强制清理：{'满 ' + str(status.force_hours if status else 24) + ' 小时未完成即删' if status and status.force_timeout else '关'}"
            f"{'（连本地文件）' if status and status.force_timeout and status.force_delete_files else ''}\n"
            f"监控中：{status.watched if status else 0} 个　已删除：{status.removed if status else 0} 条\n"
            f"黑名单：{status.blacklist if status else 0} 条\n"
            f"最近扫描：{status.last_scan if status and status.last_scan else '尚未扫描'}\n"
            f"最近结论：{status.last_message if status and status.last_message else '无'}"
        )
        self.post_message(mtype=_MESSAGE_TYPE, title="下载监控状态", text=text)

    def get_api(self) -> List[Dict[str, Any]]:
        """
        注册扫描与状态接口。
        """
        return [
            {
                "path": "/scan_now",
                "endpoint": self.api_scan_now,
                "methods": ["POST"],
                "auth": "bear",
                "response_model": schemas.Response[ScanResultData],
                "summary": "立即扫描下载任务",
            },
            {
                "path": "/status",
                "endpoint": self.api_status,
                "methods": ["GET"],
                "auth": "bear",
                "response_model": schemas.Response[StatusData],
                "summary": "查询下载监控运行状态",
            },
            {
                "path": "/tab",
                "endpoint": self.api_tab,
                "methods": ["GET"],
                "auth": "bear",
                "response_model": schemas.Response[Dict[str, Any]],
                "summary": "切换详情页列表",
            },
            {
                "path": "/blacklist/remove",
                "endpoint": self.api_blacklist_remove,
                "methods": ["GET"],
                "auth": "bear",
                "response_model": schemas.Response[Dict[str, Any]],
                "summary": "移除一条黑名单记录",
            },
        ]

    def api_tab(self, tab: str = _DEFAULT_TAB) -> "schemas.Response[Dict[str, Any]]":
        """
        记录详情页当前选中的分页，页面随后由前端重新拉取。

        必须返回宿主统一的三段式响应：页面渲染组件按 data 模式解析接口结果，
        缺少 data 段会被判为无效响应，前端只弹「服务端返回了无效响应」。

        :param tab: 分页键，watched / removed / blacklist / detail
        :return: 宿主统一响应结构
        """
        value = self._save_page_tab(tab)
        label = dict(_PAGE_TABS).get(value, value)
        return schemas.Response(
            success=True,
            message=f"已切换到「{label}」",
            data={"tab": value},
        )

    def api_blacklist_remove(
        self, hash: str = "", title: str = ""
    ) -> "schemas.Response[Dict[str, Any]]":
        """
        从本地黑名单里移除一条记录。

        页面上的「移除」按钮按 hash 传参，hash 缺失时才退回标题全等匹配。移除后该
        种子不再被当轮秒删；如果它仍在下载器里，下一轮扫描会重新按阈值判断。

        :param hash: 待移除种子的 hash
        :param title: 待移除种子的标题，仅在缺少 hash 时使用
        :return: 宿主统一响应结构
        """
        state = self._load_state()
        current = len(state["blacklist"])
        if not str(hash or "").strip() and not str(title or "").strip():
            return schemas.Response(
                success=False,
                message="缺少要移除的种子标识",
                data={"removed": 0, "blacklist": current},
            )
        kept, removed = remove_blacklist_item(state["blacklist"], hash, title)
        if not removed:
            return schemas.Response(
                success=False,
                message="黑名单里已经没有这条记录，可能刚被移除",
                data={"removed": 0, "blacklist": len(kept)},
            )
        self._save_state({"first_seen": state["first_seen"], "blacklist": kept})
        name = str(removed[0].get("title") or removed[0].get("hash") or "未知种子")
        logger.info(f"下载监控：已手动移除黑名单条目「{name}」，剩余 {len(kept)} 条")
        return schemas.Response(
            success=True,
            message=f"已移除「{name}」，剩余 {len(kept)} 条",
            data={"removed": len(removed), "blacklist": len(kept)},
        )

    def api_scan_now(self) -> "schemas.Response[ScanResultData]":
        """
        立即执行一轮扫描，忽略通知配置的静默要求。

        :return: 宿主统一响应结构
        """
        result = self.scan(notify=False)
        return schemas.Response(
            success=bool(result.get("success")),
            message=str(result.get("message") or ""),
            data=ScanResultData(
                scanned=int(result.get("scanned") or 0),
                removed=int(result.get("removed") or 0),
                waiting=int(result.get("waiting") or 0),
                blacklist=int(result.get("blacklist") or 0),
                dry_run=bool(result.get("dry_run")),
                detail=list(result.get("detail") or []),
            ),
        )

    def api_status(self) -> "schemas.Response[StatusData]":
        """
        返回当前配置与最近一次扫描结果。

        :return: 宿主统一响应结构
        """
        state = self._load_state()
        records = self._records or self._load_records()
        last_scan = self._last_scan or self._load_last_scan()
        return schemas.Response(
            success=True,
            message="OK",
            data=StatusData(
                enabled=self._enabled,
                max_hours=int(self._config.get("max_hours") or 0),
                min_progress=int(self._config.get("min_progress") or 0),
                force_timeout=bool(self._config.get("force_timeout")),
                force_hours=int(self._config.get("force_hours") or 24),
                force_delete_files=bool(self._config.get("force_delete_files")),
                tags=split_tags(self._config.get("tags")),
                blacklist=len(state["blacklist"]),
                watched=len(records.get("watched") or []),
                removed=len(records.get("removed") or []),
                last_scan=str(last_scan.get("time") or "") or None,
                last_message=str(last_scan.get("message") or "") or None,
            ),
        )

    def get_service(self) -> List[Dict[str, Any]]:
        """
        注册周期扫描服务。
        """
        if not self.get_state() or not self._cron_valid:
            return []
        return [
            {
                "id": f"{self.__class__.__name__}.Scan",
                "name": "下载监控扫描",
                "trigger": CronTrigger.from_crontab(self._config.get("cron", "")),
                "func": self.scan,
                "kwargs": {},
            }
        ]

    # ------------------------------------------------------------------
    # 页面
    # ------------------------------------------------------------------

    @staticmethod
    def _col(cols: int, content: List[dict]) -> dict:
        """
        生成一个响应式栅格列。
        """
        return {"component": "VCol", "props": {"cols": 12, "md": cols}, "content": content}

    @staticmethod
    def _switch(model: str, label: str, hint: Optional[str] = None) -> dict:
        """
        生成一个开关项。
        """
        props: Dict[str, Any] = {"model": model, "label": label}
        if hint:
            props["hint"] = hint
            props["persistent-hint"] = True
        return {"component": "VSwitch", "props": props}

    @staticmethod
    def _text(model: str, label: str, hint: Optional[str] = None, **extra: Any) -> dict:
        """
        生成一个文本输入项。
        """
        props: Dict[str, Any] = {"model": model, "label": label}
        if hint:
            props["hint"] = hint
            props["persistent-hint"] = True
        props.update(extra)
        return {"component": "VTextField", "props": props}

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """
        返回配置页面与默认配置。
        """
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            self._col(3, [self._switch("enabled", "启用插件")]),
                            self._col(3, [self._switch("notify", "消息通知")]),
                            self._col(3, [self._switch("dry_run", "演练模式")]),
                            self._col(3, [self._switch("once", "保存后立即扫描")]),
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            self._col(
                                3,
                                [
                                    self._switch(
                                        "notify_watch",
                                        "监控通知",
                                        "有任务新进入监控时推送",
                                    )
                                ],
                            ),
                            self._col(
                                3,
                                [
                                    self._switch(
                                        "notify_remove",
                                        "删除通知",
                                        "删除任务时推送",
                                    )
                                ],
                            ),
                            self._col(3, [self._switch("delete_files", "同时删除文件")]),
                            self._col(3, [self._switch("use_blacklist", "删除后拉黑")]),
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            self._col(
                                6,
                                [
                                    {
                                        "component": "VCronField",
                                        "props": {
                                            "model": "cron",
                                            "label": "扫描周期",
                                            "placeholder": "5位cron表达式",
                                        },
                                    }
                                ],
                            ),
                            self._col(
                                3,
                                [
                                    self._text(
                                        "max_hours",
                                        "等待时长阈值（小时）",
                                        "从插件首次看到该任务开始计时",
                                        type="number",
                                    )
                                ],
                            ),
                            self._col(
                                3,
                                [
                                    self._text(
                                        "min_progress",
                                        "进度阈值（%）",
                                        "进度不高于该值才判定为卡死",
                                        type="number",
                                    )
                                ],
                            ),
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            self._col(
                                3,
                                [
                                    self._switch(
                                        "force_timeout",
                                        "满 24H 强制清理",
                                        "监控满设定小时仍未下载完成就直接删除，不再看进度阈值",
                                    )
                                ],
                            ),
                            self._col(
                                3,
                                [
                                    self._text(
                                        "force_hours",
                                        "强制清理时长（小时）",
                                        "默认 24 小时，从插件首次监控到该任务开始计时",
                                        type="number",
                                    )
                                ],
                            ),
                            self._col(
                                6,
                                [
                                    self._switch(
                                        "force_delete_files",
                                        "强制清理时删除本地文件",
                                        "只作用于这条规则：删除下载任务时连已下载文件一起删，"
                                        "不受上方「同时删除文件」开关影响，且不可恢复。",
                                    )
                                ],
                            ),
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            self._col(
                                6,
                                [
                                    self._text(
                                        "tags",
                                        "监控标签",
                                        "多个用逗号分隔，留空表示监控全部任务",
                                    )
                                ],
                            ),
                            self._col(
                                6,
                                [
                                    self._switch(
                                        "sync_subscribe",
                                        "写入订阅排除规则",
                                        "可选：把整个种子标题写进订阅排除正则。副作用是同一标题的"
                                        "其它种子（含其它站点）也会被排除，若该剧只有这一个版本，"
                                        "这一集将一直下不到；删除后回滚订阅进度是本插件默认行为，"
                                        "不依赖该开关。",
                                    )
                                ],
                            ),
                        ],
                    },
                    {
                        "component": "VAlert",
                        "props": {
                            "type": "warning",
                            "variant": "tonal",
                            "title": "判定说明（两条规则任一命中即清理）",
                            "text": "规则一：同时满足「已等待 ≥ 等待时长阈值」与「进度 ≤ 进度阈值」判为卡死。"
                                    "规则二：监控满强制清理时长（默认 24 小时）仍未下载完成，直接删除任务，"
                                    "默认连本地文件一起删，不看进度、不可恢复。"
                                    "两条规则都只作用于正在下载且标签命中的任务；已完成/做种中的任务永不处理。"
                                    "下载器不提供任务添加时间，计时按插件首次观测到该任务的时间计算。",
                        },
                    },
                ],
            }
        ], dict(DEFAULT_CONFIG)

    def get_page(self) -> List[dict]:
        """
        返回详情页：顶部四个等宽按钮分页，下方只渲染当前分页的列表。
        """
        state = self._load_state()
        records = self._records or self._load_records()
        last_scan = self._last_scan or self._load_last_scan()
        blacklist = list(state.get("blacklist") or [])
        watched_rows = list(records.get("watched") or [])
        removed_rows = list(records.get("removed") or [])
        detail = list(last_scan.get("detail") or [])
        counts = {
            "watched": len(watched_rows),
            "removed": len(removed_rows),
            "blacklist": len(blacklist),
            "detail": len(detail),
        }
        active = self._validate_tab(self._page_tab)

        page: List[dict] = [
            self._config_card(last_scan),
            {
                "component": "VRow",
                "props": {"class": "mt-3"},
                "content": [
                    self._tab_column(key, label, counts.get(key, 0), key == active)
                    for key, label in _PAGE_TABS
                ],
            },
            {
                "component": "div",
                "props": {
                    "class": "text-overline text-medium-emphasis mt-4 mb-2",
                },
                "text": dict(_PAGE_TABS).get(active, ""),
            },
        ]

        rows, empty_text, empty_kind = self._tab_content(
            active, watched_rows, removed_rows, blacklist, detail
        )
        if rows:
            if active == "blacklist":
                page.append(self._blacklist_hint())
            page.extend(rows)
            total = counts.get(active, 0)
            if total > len(rows):
                page.append(
                    self._footnote(f"仅显示最近 {len(rows)} 条，共 {total} 条。")
                )
        else:
            page.append(
                {
                    "component": "VCard",
                    "props": {"variant": "outlined", "class": "pa-6"},
                    "content": [
                        {
                            "component": "div",
                            "props": {"class": "d-flex flex-column align-center"},
                            "content": [
                                {
                                    "component": "VAvatar",
                                    "props": {
                                        "size": 44,
                                        "color": empty_kind,
                                        "variant": "tonal",
                                        "class": "mb-3",
                                    },
                                    "text": "0",
                                },
                                {
                                    "component": "div",
                                    "props": {"class": "text-subtitle-2 mb-1"},
                                    "text": "暂无数据",
                                },
                                {
                                    "component": "div",
                                    "props": {
                                        "class": "text-caption text-medium-emphasis text-center",
                                        "style": "max-width: 620px",
                                    },
                                    "text": empty_text,
                                },
                            ],
                        }
                    ],
                }
            )

        if not self._enabled:
            page.append(
                {
                    "component": "VAlert",
                    "props": {
                        "type": "warning",
                        "variant": "tonal",
                        "density": "compact",
                        "class": "mt-3",
                        "title": "插件未启用",
                        "text": "启用并保存配置后才会注册定时扫描服务；也可以先开启演练模式确认阈值是否合适。",
                    },
                }
            )
        return page

    def _tab_content(
        self,
        tab: str,
        watched_rows: List[Dict[str, Any]],
        removed_rows: List[Dict[str, Any]],
        blacklist: List[Dict[str, Any]],
        detail: List[Any],
    ) -> Tuple[List[dict], str, str]:
        """
        按当前分页生成列表元素。

        :return: (列表元素, 空数据说明, 空数据提示类型)
        """
        if tab == "watched":
            rows = [
                self._entry_card(
                    title=str(item.get("title") or "未知任务"),
                    chips=self._watched_chips(item),
                    marker="监",
                    color="primary",
                )
                for item in watched_rows[-MAX_LIST_ROWS:]
            ]
            return (
                rows,
                "当前没有正在下载的任务。插件只监控状态为「下载中」且标签命中的任务，"
                "暂停、已完成和做种中的任务不会出现在这里。",
                "info",
            )
        if tab == "removed":
            # 最新的排在最上面
            rows = [
                self._entry_card(
                    title=str(item.get("title") or "未知任务"),
                    chips=self._removed_chips(item),
                    marker="删",
                    color="warning",
                )
                for item in list(reversed(removed_rows))[:MAX_LIST_ROWS]
            ]
            return (
                rows,
                "还没有删除过任务。可以开启演练模式跑一轮，先在「最近命中」里确认会被删掉的任务。",
                "info",
            )
        if tab == "blacklist":
            rows = [
                self._blacklist_row(item)
                for item in list(reversed(blacklist))[:MAX_LIST_ROWS]
            ]
            return (
                rows,
                "黑名单为空。删除过任务并开启「删除后拉黑」后，被删的种子会记录在这里。",
                "info",
            )
        dry_run = bool(self._config.get("dry_run"))
        rows = [
            self._entry_card(
                title=str(line),
                chips=[],
                marker="记",
                color="warning",
            )
            for line in list(reversed(detail))[:MAX_LIST_ROWS]
        ]
        return (
            rows,
            "最近一次扫描没有命中任何任务。"
            + ("当前处于演练模式，命中的任务只记录不删除。" if dry_run else ""),
            "info",
        )

    def _config_card(self, last_scan: Dict[str, Any]) -> dict:
        """
        顶部运行配置卡：状态、阈值、次要配置分层展示，避免长文本挤成一团。
        """
        tags = split_tags(self._config.get("tags"))
        notify_state = "关" if not self._config.get("notify") else (
            "监控/删除"
            if self._config.get("notify_watch") and self._config.get("notify_remove")
            else ("仅监控" if self._config.get("notify_watch")
                  else ("仅删除" if self._config.get("notify_remove") else "关"))
        )
        yes_no = lambda flag: "是" if self._config.get(flag) else "否"
        force_on = bool(self._config.get("force_timeout"))
        force_chip = f"满 {self._config.get('force_hours')}H 强制清理 {'开' if force_on else '关'}"
        if force_on and self._config.get("force_delete_files"):
            force_chip += "（连文件删）"
        chips = [
            self._chip(f"等待 ≥ {self._config.get('max_hours')} 小时", "primary", True),
            self._chip(f"进度 ≤ {self._config.get('min_progress')}%", "primary", True),
            self._chip(force_chip, "error"),
            self._chip(f"标签 {'、'.join(tags) if tags else '全部任务'}"),
            self._chip(f"周期 {self._config.get('cron')}"),
            self._chip(f"删除文件 {yes_no('delete_files')}"),
            self._chip(f"演练 {yes_no('dry_run')}"),
            self._chip(f"拉黑 {yes_no('use_blacklist')}"),
            self._chip(f"通知 {notify_state}"),
        ]
        note = (
            "删除任务时自动回滚订阅进度并重新搜索该集（固定开启）"
            f"　·　写入订阅排除：{'开' if self._config.get('sync_subscribe') else '关'}"
        )
        return {
            "component": "VCard",
            "props": {"variant": "outlined", "class": "pa-4"},
            "content": [
                {
                    "component": "div",
                    "props": {"class": "d-flex align-center flex-wrap"},
                    "content": [
                        {
                            "component": "div",
                            "props": {
                                "class": "bg-primary rounded mr-2 flex-shrink-0",
                                "style": "width: 4px; height: 18px",
                            },
                        },
                        {
                            "component": "span",
                            "props": {"class": "text-subtitle-2 font-weight-bold"},
                            "text": "运行配置",
                        },
                        {
                            "component": "VChip",
                            "props": {
                                "size": "x-small",
                                "label": True,
                                "variant": "tonal",
                                "color": "success" if self._enabled else "warning",
                                "class": "ml-2",
                            },
                            "text": "已启用" if self._enabled else "未启用",
                        },
                        {
                            "component": "span",
                            "props": {
                                "class": "text-caption text-medium-emphasis ml-auto"
                            },
                            "text": f"最近扫描 {last_scan.get('time') or '尚未扫描'}",
                        },
                    ],
                },
                {
                    "component": "div",
                    "props": {"class": "d-flex flex-wrap mt-3"},
                    "content": chips,
                },
                {
                    "component": "div",
                    "props": {"class": "text-caption text-medium-emphasis mt-2"},
                    "text": f"说明：{note}",
                },
            ],
        }

    @staticmethod
    def _chip(text: str, color: Optional[str] = None, prominent: bool = False) -> dict:
        """
        生成一枚小信息标签。前两个关键阈值用实心色调，其余用细边框降噪。
        """
        props: Dict[str, Any] = {
            "size": "x-small",
            "label": True,
            "variant": "tonal" if prominent else "outlined",
            "class": "mr-2 mb-2",
        }
        if color:
            props["color"] = color
        return {"component": "VChip", "props": props, "text": text}

    @staticmethod
    def _footnote(text: str) -> dict:
        """
        列表下方的浅色脚注。
        """
        return {
            "component": "div",
            "props": {"class": "text-caption text-medium-emphasis mt-2"},
            "text": text,
        }

    def _tab_column(self, key: str, label: str, count: int, active: bool) -> dict:
        """
        生成一枚等宽等高的分页按钮：上方「图标 + 名称」，下方数字，选中项整块高亮。
        """
        props: Dict[str, Any] = {
            "block": True,
            "height": 68,
            "rounded": "lg",
            "class": "text-none pa-2",
            "variant": "tonal" if active else "outlined",
        }
        if active:
            props["color"] = "primary"
        return self._col(
            3,
            [
                {
                    "component": "VBtn",
                    "props": props,
                    "events": {
                        "click": {
                            "api": f"/plugin/{self.__class__.__name__}/tab",
                            "method": "get",
                            "params": {"tab": key},
                        }
                    },
                    "content": [
                        {
                            "component": "div",
                            "props": {"class": "d-flex flex-column align-center justify-center"},
                            "content": [
                                {
                                    "component": "div",
                                    "props": {"class": "d-flex align-center"},
                                    "content": [
                                        {
                                            "component": "div",
                                            "props": {
                                                "class": "rounded-circle mr-2 flex-shrink-0",
                                                "style": "width: 7px; height: 7px; background: currentColor;"
                                                + ("" if active else " opacity: 0.45"),
                                            },
                                        },
                                        {
                                            "component": "span",
                                            "props": {"class": "text-body-2"},
                                            "text": label,
                                        },
                                    ],
                                },
                                {
                                    "component": "div",
                                    "props": {"class": "text-h6 font-weight-bold mt-1"},
                                    "text": str(count),
                                },
                            ],
                        }
                    ],
                }
            ],
        )

    @staticmethod
    def _blacklist_hint() -> dict:
        """
        黑名单分页顶部的浅色操作提示。
        """
        return {
            "component": "div",
            "props": {
                "class": "text-caption text-medium-emphasis d-flex align-center mb-2",
            },
            "content": [
                {
                    "component": "span",
                    "text": "提示：点条目右侧的「移除」即可删掉这条拉黑记录；移除后该种子再出现不会再被自动删除。",
                },
            ],
        }

    def _entry_card(
        self,
        title: str,
        chips: List[str],
        marker: str,
        color: str,
        action: Optional[dict] = None,
    ) -> dict:
        """
        生成一条卡片式记录：左侧状态徽标，中间标题 + 小标签，右侧可选操作按钮。

        用卡片而不是 VAlert，是为了让长标题单独成行、元数据按标签分组，信息密度更高
        且不会像一整串 `·` 文本那样糊成一片。徽标用文字而非 mdi 图标：宿主的图标是
        构建时静态扫描打包的，插件页 JSON 里临时写图标名不一定能加载出来。

        :param title: 记录标题
        :param chips: 元数据标签文本
        :param marker: 左侧徽标文字（单个汉字）
        :param color: 状态色
        :param action: 右侧操作按钮（黑名单的「移除」）
        :return: 页面元素
        """
        main: List[dict] = [
            {
                "component": "div",
                "props": {
                    "class": "text-body-2 font-weight-medium",
                    "style": "overflow-wrap: anywhere",
                },
                "text": title,
            }
        ]
        if chips:
            main.append(
                {
                    "component": "div",
                    "props": {"class": "d-flex flex-wrap mt-2"},
                    "content": [
                        # 前两个是判断依据（或时间/原因），用实心色调强调；其余用细边框降噪
                        self._chip(
                            text,
                            color if index < 2 else None,
                            index < 2,
                        )
                        for index, text in enumerate(chips)
                    ],
                }
            )
        inner: List[dict] = [
            {
                "component": "VAvatar",
                "props": {
                    "size": 30,
                    "color": color,
                    "variant": "tonal",
                    "rounded": "lg",
                    "class": "mr-3 mt-1 flex-shrink-0 text-body-2 font-weight-bold",
                },
                "text": marker,
            },
            {
                "component": "div",
                "props": {"class": "flex-grow-1", "style": "min-width: 0"},
                "content": main,
            },
        ]
        if action:
            inner.append(action)
        return {
            "component": "VCard",
            "props": {"variant": "outlined", "class": "mb-2"},
            "content": [
                {
                    "component": "div",
                    "props": {"class": "d-flex align-start pa-3"},
                    "content": inner,
                }
            ],
        }

    def _blacklist_row(self, item: Dict[str, Any]) -> dict:
        """
        生成一条黑名单记录：卡片 + 右侧「移除」按钮。

        :param item: 黑名单条目
        :return: 页面元素
        """
        key = str(item.get("hash") or "").strip()
        action = None
        if key:
            action = {
                "component": "VBtn",
                "props": {
                    "color": "error",
                    "variant": "tonal",
                    "size": "small",
                    "rounded": "lg",
                    "class": "text-none ml-3 flex-shrink-0",
                },
                "content": [
                    {"component": "span", "text": "× 移除"},
                ],
                "events": {
                    "click": {
                        "api": f"/plugin/{self.__class__.__name__}/blacklist/remove",
                        "method": "get",
                        "params": {"hash": key},
                    }
                },
            }
        return self._entry_card(
            title=str(item.get("title") or "未知种子"),
            chips=self._blacklist_chips(item),
            marker="禁",
            color="error",
            action=action,
        )

    @staticmethod
    def _watched_chips(row: Dict[str, Any]) -> List[str]:
        """
        监控中任务的元数据标签。
        """
        return [
            f"进度 {row.get('progress') or '0.0%'}",
            f"已等待 {row.get('wait') or '—'}",
            f"大小 {row.get('size') or '未知'}",
            f"速度 {row.get('speed') or '—'}",
            f"标签 {row.get('tags') or '—'}",
            f"首见 {row.get('since') or '—'}",
        ]

    @staticmethod
    def _removed_chips(row: Dict[str, Any]) -> List[str]:
        """
        已删除任务的元数据标签。
        """
        parts = [
            str(row.get("time") or "—"),
            f"进度 {row.get('progress') or '—'}",
            f"已等待 {row.get('wait') or '—'}",
            f"原因 {row.get('reason') or '—'}",
            f"文件{row.get('files') or '—'}",
        ]
        note = str(row.get("note") or "").strip()
        if note and note != "—":
            parts.append(note)
        return parts

    @staticmethod
    def _blacklist_chips(item: Dict[str, Any]) -> List[str]:
        """
        黑名单条目的元数据标签。
        """
        parts = [
            str(item.get("removed_at") or "—"),
            reason_text(str(item.get("reason") or "")),
            f"hash {str(item.get('hash') or '')[:12]}",
        ]
        downloader = str(item.get("downloader") or "").strip()
        if downloader:
            parts.append(f"下载器 {downloader}")
        return parts

    def stop_service(self) -> None:
        """
        释放本插件登记的一次性任务。
        """
        try:
            scheduler_sdk.remove_plugin_once_job(self.__class__.__name__, _ONCE_JOB_ID)
        except Exception as error:  # noqa: BLE001 - 未登记或宿主版本不支持时忽略
            logger.debug(f"下载监控：清理一次性任务失败：{error}")
