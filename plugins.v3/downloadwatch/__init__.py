"""
下载监控插件主入口。

职责：按配置周期扫描下载器任务，把「已等待时长 ≥ X 小时且进度 ≤ Y%」的任务判定为
卡死，删除任务（可选连文件一起删），并把该种子记入本地黑名单；黑名单里的种子
再次被订阅下载时会被立即清理，从而避免同一个坏种子反复占用下载位。
可选地把种子标题追加进对应订阅的排除规则，从源头阻止重复下载。
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from apscheduler.triggers.cron import CronTrigger
from pydantic import BaseModel, Field

from app import schemas
from app.chain.download import DownloadChain
from app.db.oper.subscribe import SubscribeOper
from app.schemas.types import EventType, MessageType
from app.sdk import scheduler as scheduler_sdk
from app.sdk.events import Event, eventmanager
from app.sdk.logging import logger
from app.sdk.plugin import _PluginBase

from .config import (
    DEFAULT_CONFIG,
    MAX_BLACKLIST,
    MAX_NOTIFY_LINES,
    merge_exclude,
    normalize_config,
    normalize_title,
    parse_season,
    split_tags,
    tags_match,
)
from .monitor import (
    REASON_BLACKLIST,
    evaluate,
    format_duration,
    is_finished,
    progress_text,
)

# 观察状态与黑名单的持久化键
_STATE_KEY = "watch_state"
# 最近一次扫描结果的持久化键，供详情页在实例重建后仍能展示
_LAST_SCAN_KEY = "last_scan"
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
    tags: List[str] = Field(default_factory=list, description="监控标签")
    blacklist: int = Field(default=0, description="黑名单条目数")
    last_scan: Optional[str] = Field(default=None, description="最近一次扫描时间")
    last_message: Optional[str] = Field(default=None, description="最近一次扫描结论")


class DownloadWatch(_PluginBase):
    """
    下载任务超时清理与拉黑插件。
    """

    plugin_name = "下载监控"
    plugin_desc = "定时扫描下载任务，等待超过设定时长且进度仍低于阈值的任务自动删除，并把种子拉黑避免重复下载。"
    plugin_icon = "https://raw.githubusercontent.com/jxxghp/MoviePilot-Plugins/main/icons/Moviepilot_A.png"
    plugin_version = "1.0.0"
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

        if not self._enabled:
            logger.debug("下载监控：插件未启用")
            return
        if not self._cron_valid:
            logger.error(f"下载监控：cron 表达式非法 {self._config.get('cron')}，定时扫描不会生效")
        logger.info(
            "下载监控：已启用，判定条件为等待 ≥ "
            f"{self._config.get('max_hours')} 小时且进度 ≤ {self._config.get('min_progress')}%，"
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
        use_blacklist = bool(self._config.get("use_blacklist"))
        delete_files = bool(self._config.get("delete_files"))
        dry_run = bool(self._config.get("dry_run"))

        alive: set[str] = set()
        detail: List[str] = []
        removed = 0
        waiting = 0

        for torrent in torrents:
            torrent_hash = str(getattr(torrent, "hash", "") or "").strip().lower()
            if not torrent_hash:
                continue
            alive.add(torrent_hash)

            title = normalize_title(getattr(torrent, "title", "") or getattr(torrent, "name", ""))
            title = title or torrent_hash
            progress = getattr(torrent, "progress", 0)

            # 已完成或做种中的任务永不处理，同时清掉观察计时
            if is_finished(progress):
                first_seen.pop(torrent_hash, None)
                continue

            record = first_seen.get(torrent_hash)
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
                    "downloader": str(getattr(torrent, "downloader", "") or ""),
                }

            if use_blacklist and torrent_hash in black_hashes:
                reason = REASON_BLACKLIST
            else:
                if not tags_match(getattr(torrent, "tags", ""), wanted_tags):
                    # 标签不匹配的任务不参与判定，但保留观察计时
                    continue
                decision = evaluate(
                    progress=progress,
                    age_seconds=age_seconds,
                    max_hours=max_hours,
                    min_progress=min_progress,
                )
                if not decision.reap:
                    if age_seconds is not None:
                        waiting += 1
                    continue
                reason = decision.reason

            label = self._describe(torrent, title, progress, age_seconds, reason)
            if dry_run:
                detail.append(f"{label}（演练模式，未删除）")
                continue

            if not self._remove(chain, torrent_hash, getattr(torrent, "downloader", None), delete_files):
                continue

            removed += 1
            first_seen.pop(torrent_hash, None)
            if use_blacklist:
                blacklist.append(
                    {
                        "hash": torrent_hash,
                        "title": title,
                        "downloader": str(getattr(torrent, "downloader", "") or ""),
                        "removed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "reason": reason,
                    }
                )
                black_hashes.add(torrent_hash)

            note = ""
            if self._config.get("sync_subscribe"):
                note = self._sync_subscribe_exclude(torrent, title)
            detail.append(f"{label}{'｜' + note if note else ''}")

        # 只保留仍然存在的任务，避免长期运行后观察表无限增长
        first_seen = {key: value for key, value in first_seen.items() if key in alive}
        if len(blacklist) > MAX_BLACKLIST:
            blacklist = blacklist[-MAX_BLACKLIST:]
        self._save_state({"first_seen": first_seen, "blacklist": blacklist})

        message = self._compose_message(len(torrents), removed, waiting, len(blacklist), dry_run, detail)
        summary = self._summary(
            success=True,
            message=message,
            scanned=len(torrents),
            removed=removed,
            waiting=waiting,
            blacklist=len(blacklist),
            dry_run=dry_run,
            detail=detail[:MAX_NOTIFY_LINES],
        )
        self._remember_scan(summary)

        should_notify = self._config.get("notify") if notify is None else notify
        if should_notify and detail:
            self.post_message(
                mtype=_MESSAGE_TYPE,
                title="下载监控：已清理卡死任务" if removed else "下载监控：演练结果",
                text=message,
            )
        elif should_notify and removed:
            self.post_message(mtype=_MESSAGE_TYPE, title="下载监控", text=message)
        return summary

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

    def _sync_subscribe_exclude(self, torrent: Any, title: str) -> str:
        """
        把种子标题追加到匹配订阅的排除规则，阻止再次订阅时选中同一个种子。

        只有在能唯一定位到订阅时才写入，宁可跳过也不批量改用户配置。

        :param torrent: 下载任务对象
        :param title: 已归一化的标题，仅用于兜底匹配
        :return: 处理结论文本
        """
        name = str(getattr(torrent, "name", "") or "").strip() or title
        season = parse_season(getattr(torrent, "season_episode", None))
        try:
            subscriptions = SubscribeOper().list()
        except Exception as error:  # noqa: BLE001 - 订阅不可读时不影响删除结论
            logger.warning(f"下载监控：读取订阅失败，跳过排除规则同步：{error}")
            return "订阅读取失败，未同步排除规则"

        matched = []
        for subscribe in subscriptions:
            if (subscribe.name or "").strip() != name:
                continue
            subscribe_season = getattr(subscribe, "season", None)
            if season is not None and subscribe_season is not None and subscribe_season != season:
                continue
            matched.append(subscribe)

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
        if reason == REASON_BLACKLIST:
            parts.append("命中黑名单")
        downloader = str(getattr(torrent, "downloader", "") or "")
        if downloader:
            parts.append(downloader)
        return " · ".join(parts)

    @staticmethod
    def _compose_message(
        scanned: int,
        removed: int,
        waiting: int,
        blacklist: int,
        dry_run: bool,
        detail: List[str],
    ) -> str:
        """
        组装通知与页面共用的结论文本。
        """
        head = f"本轮读取 {scanned} 个任务，清理 {removed} 个，仍在观察 {waiting} 个，黑名单 {blacklist} 条。"
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
        ]

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
        last_scan = self._last_scan or self._load_last_scan()
        return schemas.Response(
            success=True,
            message="OK",
            data=StatusData(
                enabled=self._enabled,
                max_hours=int(self._config.get("max_hours") or 0),
                min_progress=int(self._config.get("min_progress") or 0),
                tags=split_tags(self._config.get("tags")),
                blacklist=len(state["blacklist"]),
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
                            self._col(3, [self._switch("notify", "清理后通知")]),
                            self._col(3, [self._switch("dry_run", "演练模式")]),
                            self._col(3, [self._switch("once", "保存后立即扫描")]),
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
                                6,
                                [
                                    self._text(
                                        "tags",
                                        "监控标签",
                                        "多个用逗号分隔，留空表示监控全部任务",
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
                                    self._switch(
                                        "sync_subscribe",
                                        "写入订阅排除规则",
                                        "把种子标题追加到对应订阅的排除正则，需能唯一定位订阅",
                                    )
                                ],
                            ),
                        ],
                    },
                    {
                        "component": "VAlert",
                        "props": {
                            "type": "info",
                            "variant": "tonal",
                            "title": "判定说明",
                            "text": "同时满足「已等待 ≥ 等待时长阈值」与「进度 ≤ 进度阈值」才会删除；"
                                    "已完成或做种中的任务永不处理。下载器不提供任务添加时间，"
                                    "等待时长按插件首次观测到该任务的时间计算。",
                        },
                    },
                ],
            }
        ], dict(DEFAULT_CONFIG)

    def get_page(self) -> List[dict]:
        """
        返回详情页，展示当前配置与最近一次扫描结论。
        """
        state = self._load_state()
        last_scan = self._last_scan or self._load_last_scan()
        tags = split_tags(self._config.get("tags"))
        status_text = (
            f"插件状态：{'已启用' if self._enabled else '未启用'}\n"
            f"判定条件：等待 ≥ {self._config.get('max_hours')} 小时 且 进度 ≤ {self._config.get('min_progress')}%\n"
            f"监控标签：{'、'.join(tags) if tags else '全部任务'}\n"
            f"扫描周期：{self._config.get('cron')}\n"
            f"删除文件：{'是' if self._config.get('delete_files') else '否'}　"
            f"演练模式：{'是' if self._config.get('dry_run') else '否'}　"
            f"拉黑：{'是' if self._config.get('use_blacklist') else '否'}"
        )
        page: List[dict] = [
            {
                "component": "VAlert",
                "props": {
                    "type": "info",
                    "variant": "tonal",
                    "title": "运行配置",
                    "text": status_text,
                },
            },
            {
                "component": "VRow",
                "content": [
                    self._col(
                        4,
                        [
                            {
                                "component": "VAlert",
                                "props": {
                                    "type": "success",
                                    "variant": "tonal",
                                    "title": "最近扫描",
                                    "text": str(last_scan.get("time") or "尚未扫描"),
                                },
                            }
                        ],
                    ),
                    self._col(
                        4,
                        [
                            {
                                "component": "VAlert",
                                "props": {
                                    "type": "warning",
                                    "variant": "tonal",
                                    "title": "黑名单",
                                    "text": f"{len(state['blacklist'])} 条种子，再次出现会被立即清理",
                                },
                            }
                        ],
                    ),
                    self._col(
                        4,
                        [
                            {
                                "component": "VAlert",
                                "props": {
                                    "type": "info",
                                    "variant": "tonal",
                                    "title": "观察中",
                                    "text": f"{len(state['first_seen'])} 个任务正在计时",
                                },
                            }
                        ],
                    ),
                ],
            },
        ]
        detail = list(last_scan.get("detail") or [])
        if detail:
            page.append(
                {
                    "component": "VAlert",
                    "props": {
                        "type": "warning",
                        "variant": "tonal",
                        "title": "最近清理明细",
                        "text": "\n".join(str(item) for item in detail[-MAX_NOTIFY_LINES:]),
                    },
                }
            )
        if not self._enabled:
            page.append(
                {
                    "component": "VAlert",
                    "props": {
                        "type": "error",
                        "variant": "tonal",
                        "title": "插件未启用",
                        "text": "启用并保存配置后才会注册定时扫描服务；也可以先开启演练模式确认阈值是否合适。",
                    },
                }
            )
        return page

    def stop_service(self) -> None:
        """
        释放本插件登记的一次性任务。
        """
        try:
            scheduler_sdk.remove_plugin_once_job(self.__class__.__name__, _ONCE_JOB_ID)
        except Exception as error:  # noqa: BLE001 - 未登记或宿主版本不支持时忽略
            logger.debug(f"下载监控：清理一次性任务失败：{error}")
