"""
每日入库速报插件主入口。

职责：按配置的 cron 定时统计整理历史中新增入库的剧集与电影，渲染成一张日报长图，
再通过 MoviePilot 自身的通知渠道推送；同时提供图片接口给通知渠道拉取这张图，
并注册远程命令供用户随时手动触发一次推送。
"""

from __future__ import annotations

import re
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from apscheduler.triggers.cron import CronTrigger
from fastapi import HTTPException
from starlette.responses import FileResponse

from app.schemas.types import EventType, MessageType
from app.sdk import scheduler as scheduler_sdk
from app.sdk.config import settings
from app.sdk.events import Event, eventmanager
from app.sdk.logging import logger
from app.sdk.plugin import _PluginBase

from .card import prune_old_cards, render_card
from .collector import fetch_records
from .config import (
    DEFAULT_CONFIG,
    MEDIA_TYPE_OPTIONS,
    WINDOW_OPTIONS,
    WINDOW_YESTERDAY,
    normalize_config,
    parse_window_alias,
    resolve_window,
    split_words,
)
from .digest import Digest, EpisodeGroup, build_digest, render
from .fonts import FontBook, locate_font, resolve_font

# 卡片文件名格式，同时用于校验接口入参，杜绝路径穿越
_CARD_NAME_PATTERN = re.compile(r"^digest_\d{8}_\d{6}\.jpg$")
# 一次性任务的稳定标识
_ONCE_JOB_ID = "digest_once"
# 插件接口前缀，与宿主动态路由拼装规则保持一致
_API_PREFIX = "/api/v1/plugin"
# 远程命令的动作标识，同时用于命令声明与事件判定
_ACTION_PUSH = "digest_push"
_ACTION_STATUS = "digest_status"
# 手动触发的冷却时间，避免连续发命令刷屏
_MANUAL_COOLDOWN_SECONDS = 60
# 本插件通知使用的场景类型，渠道必须在场景开关里勾选该值才能收到
_MESSAGE_TYPE = MessageType.Plugin


class DailyEpisodeDigest(_PluginBase):
    """
    每日新入库剧集与电影的图片日报插件。
    """

    plugin_name = "每日入库速报"
    plugin_desc = "定时统计每天新入库的剧集与电影，渲染成带封面的日报长图，通过 MoviePilot 通知渠道推送。"
    plugin_icon = "https://raw.githubusercontent.com/jxxghp/MoviePilot-Plugins/main/icons/Moviepilot_A.png"
    plugin_version = "2.5.0"
    plugin_label = "媒体库,通知"
    plugin_author = "kongchengll"
    plugin_config_prefix = "dailyepisodedigest_"
    plugin_order = 60
    auth_level = 1

    def __init__(self) -> None:
        """
        初始化宿主基类、配置快照与运行态标记。
        """
        super().__init__()
        self._config: Dict[str, Any] = dict(DEFAULT_CONFIG)
        self._enabled = False
        self._cron_valid = True
        self._font_book: Optional[FontBook] = None
        # 手动推送冷却计时，仅在当前实例内生效
        self._last_manual_at = 0.0

    def init_plugin(self, config: Optional[Dict[str, Any]] = None) -> None:
        """
        读取配置并重建运行态，可被宿主重复调用。
        """
        # 先释放上一轮资源，保证热重载与重复保存配置时不残留任务
        self.stop_service()
        self._font_book = None
        self._config = normalize_config(config)
        self._enabled = bool(self._config.get("enabled"))
        self._cron_valid = self._check_cron(self._config.get("cron", ""))
        if not self._enabled:
            logger.debug("每日入库速报：插件未启用")
            return
        if not self._cron_valid:
            logger.error(f"每日入库速报：cron 表达式非法 {self._config.get('cron')}，定时推送不会生效")
        logger.info(f"每日入库速报：已启用，推送周期 {self._config.get('cron')}")
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
        处理“保存后立即推送一次”开关，并复位开关避免下次启动重复触发。
        """
        if not self._config.get("run_once"):
            return
        self._config["run_once"] = False
        try:
            self.update_config(self._config)
        except Exception as error:  # noqa: BLE001 - 配置回写失败不应阻断本次推送
            logger.warning(f"每日入库速报：复位立即推送开关失败：{error}")
        try:
            scheduler_sdk.add_plugin_once_job(
                self.__class__.__name__,
                _ONCE_JOB_ID,
                self.push_digest,
                "每日入库速报立即推送",
                delay_seconds=3,
            )
            logger.info("每日入库速报：已登记一次立即推送")
        except Exception as error:  # noqa: BLE001 - 宿主版本差异时降级为同步执行
            logger.warning(f"每日入库速报：登记立即推送失败，改为同步执行：{error}")
            self.push_digest()

    def get_state(self) -> bool:
        """
        返回插件是否启用。
        """
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """
        注册远程命令，供用户在消息渠道里手动触发推送或查看诊断信息。
        """
        return [
            {
                "cmd": "/digest_push",
                "event": EventType.PluginAction,
                "desc": "立即推送每日入库速报",
                "category": "插件命令",
                "data": {"action": _ACTION_PUSH},
            },
            {
                "cmd": "/digest_status",
                "event": EventType.PluginAction,
                "desc": "查看每日入库速报诊断信息",
                "category": "插件命令",
                "data": {"action": _ACTION_STATUS},
            },
        ]

    @eventmanager.register(EventType.PluginAction)
    def on_plugin_action(self, event: Event) -> None:
        """
        响应插件命令，只处理属于本插件的动作。

        命令可带一个可选参数指定统计窗口：`/digest_push today`、`/digest_push 24h`，
        缺省时使用配置中的统计范围。
        """
        event_data = event.event_data if isinstance(event.event_data, dict) else {}
        action = event_data.get("action")
        if action not in (_ACTION_PUSH, _ACTION_STATUS):
            return

        channel = event_data.get("channel")
        userid = event_data.get("user")

        if action == _ACTION_STATUS:
            self._ack(channel, userid, self._status_text())
            return

        now = time.monotonic()
        if self._last_manual_at and now - self._last_manual_at < _MANUAL_COOLDOWN_SECONDS:
            remain = int(_MANUAL_COOLDOWN_SECONDS - (now - self._last_manual_at))
            logger.info(f"每日入库速报：手动推送过于频繁，忽略本次请求（{remain} 秒后可再试）")
            self._ack(channel, userid, f"操作过于频繁，请 {remain} 秒后再试")
            return
        self._last_manual_at = now

        window = parse_window_alias(event_data.get("arg_str"))
        logger.info(f"每日入库速报：收到手动推送命令，统计范围 {window or '配置值'}")
        result = self.push_digest(window=window)
        if result is None:
            self._ack(channel, userid, "推送失败，请查看 MoviePilot 日志")
            return
        if result.get("skipped"):
            self._ack(channel, userid, f"{result.get('label')}没有新入库内容，已跳过推送")
            return
        if result.get("empty"):
            self._ack(channel, userid, f"已推送 {result.get('label')}：没有新入库内容")
            return
        self._ack(
            channel,
            userid,
            f"已推送 {result.get('label')} 的入库速报："
            f"{result.get('shows')} 部 / {result.get('episodes')} 集"
            f"{'（含图片）' if result.get('image') else '（纯文本）'}",
        )

    def _ack(self, channel: Any, userid: Any, text: str) -> None:
        """
        向触发命令的用户回一条执行结果，没有来源用户时不回。
        """
        if not userid:
            return
        try:
            self.post_message(
                channel=channel,
                mtype=MessageType.Plugin,
                title="每日入库速报",
                text=text,
                userid=userid,
            )
        except Exception as error:  # noqa: BLE001 - 回执失败不影响推送结果
            logger.debug(f"每日入库速报：发送命令回执失败：{error}")

    def _status_text(self) -> str:
        """
        汇总运行态、外网地址、字体与通知渠道开关，用于排查收不到消息的原因。

        重点检查通知渠道的场景开关是否勾选了「插件」：宿主在
        `app/modules/__init__.py::check_message` 里对未命中场景的广播消息直接丢弃，
        且不写任何日志，因此这一步只能靠自查。
        """
        lines: List[str] = ["【每日入库速报 · 诊断】"]
        lines.append(f"插件：{'已启用' if self._enabled else '未启用'}")
        cron = self._config.get("cron", "")
        lines.append(
            f"推送周期：{cron}" + ("" if self._cron_valid else "（表达式非法，定时推送不会生效）")
        )

        start, end, label = resolve_window(self._config.get("window", WINDOW_YESTERDAY))
        lines.append(f"统计窗口：{label}（{start} ~ {end}）")
        media_types = "、".join(self._config.get("media_types") or []) or "未选择"
        lines.append(f"统计类型：{media_types}")
        lines.append(f"无新增也推送：{'是' if self._config.get('notify_empty') else '否'}")

        candidates = self._base_candidates()
        lines.append(f"图片地址候选：{' → '.join(candidates)}")
        lines.append("（前两个需能被宿主解析；最后一个仅 Telegram/微信 等由宿主下载图片的渠道可用）")

        font_path, font_note = locate_font(
            self.get_data_path(), self._config.get("font_path", "")
        )
        lines.append(f"中文字体：{'已就绪' if font_path else f'未就绪（{font_note}）'}")

        lines.append("通知渠道：")
        lines.extend(self._channel_status())

        last = self.get_data("last_digest") or {}
        if last:
            lines.append(
                f"上次推送：{last.get('time')}（{last.get('shows')} 部 / "
                f"{last.get('episodes')} 集，{'含图片' if last.get('image') else '纯文本'}）"
            )
            if last.get("image_url"):
                lines.append(f"图片地址：{last.get('image_url')}")
                lines.append(f"地址自检：{last.get('image_check') or '未执行'}")
        else:
            lines.append("上次推送：无记录")
        return "\n".join(lines)

    @staticmethod
    def _channel_status() -> List[str]:
        """
        列出已启用的通知渠道，并标注是否允许「插件」场景。
        """
        try:
            from app.sdk.services import NotificationHelper

            configs = NotificationHelper().get_configs() or {}
        except Exception as error:  # noqa: BLE001 - 宿主版本差异时降级
            return [f"· 读取通知渠道失败：{error}"]

        if not configs:
            return ["· 未配置任何通知渠道"]

        lines: List[str] = []
        for name, conf in configs.items():
            switchs = list(getattr(conf, "switchs", None) or [])
            if _MESSAGE_TYPE.value in switchs:
                lines.append(f"· {name}：正常")
            else:
                lines.append(f"· {name}：未勾选「{_MESSAGE_TYPE.value}」，本插件的通知会被丢弃")
        return lines

    def get_api(self) -> List[Dict[str, Any]]:
        """
        注册日报图片接口与详情页按钮使用的测试接口。
        """
        return [
            {
                "path": "/card/{name}",
                "endpoint": self.serve_card,
                "methods": ["GET"],
                # 通知渠道由服务端抓取图片，无法携带插件鉴权头，只能匿名访问；
                # 入参用文件名白名单校验，只暴露本插件生成的图片
                "allow_anonymous": True,
                "response_model": None,
                "response_class": FileResponse,
                "responses": {
                    200: {
                        "content": {
                            "image/jpeg": {"schema": {"type": "string", "format": "binary"}}
                        }
                    }
                },
                "summary": "获取每日入库日报图片",
            },
            {
                "path": "/test_text",
                "endpoint": self.api_test_text,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "发送纯文本测试消息",
            },
            {
                "path": "/test_image",
                "endpoint": self.api_test_image,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "发送带图测试消息",
            },
            {
                "path": "/push_now",
                "endpoint": self.api_push_now,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "立即推送真实日报",
            },
        ]

    def serve_card(self, name: str) -> FileResponse:
        """
        返回指定日报图片。

        :param name: 图片文件名
        :return: 图片文件响应
        """
        if not _CARD_NAME_PATTERN.fullmatch(name or ""):
            raise HTTPException(status_code=404, detail="日报图片不存在")
        path = self.get_data_path() / name
        if not path.is_file():
            raise HTTPException(status_code=404, detail="日报图片不存在")
        return FileResponse(
            path,
            media_type="image/jpeg",
            headers={"Cache-Control": "public, max-age=86400"},
        )

    def get_service(self) -> List[Dict[str, Any]]:
        """
        注册每日定时推送服务。
        """
        if not self.get_state() or not self._cron_valid:
            return []
        return [
            {
                "id": f"{self.__class__.__name__}.Digest",
                "name": "每日入库速报推送",
                "trigger": CronTrigger.from_crontab(self._config.get("cron", "")),
                "func": self.push_digest,
                "kwargs": {},
            }
        ]

    def api_test_text(self) -> Dict[str, Any]:
        """
        发送一条纯文本测试消息，用于确认通知渠道本身是否通畅。

        :return: 供页面展示的执行结果
        """
        return self._run_test(with_image=False)

    def api_test_image(self) -> Dict[str, Any]:
        """
        用样例数据生成一张日报图并发送，用于确认图片链路是否通畅。

        :return: 供页面展示的执行结果
        """
        return self._run_test(with_image=True)

    def api_push_now(self) -> Dict[str, Any]:
        """
        按当前配置立即推送一次真实日报。

        :return: 供页面展示的执行结果
        """
        result = self.push_digest()
        if result is None:
            message = "推送失败，请查看 MoviePilot 日志"
        elif result.get("skipped"):
            message = f"{result.get('label')} 没有新入库内容，已按配置跳过"
        elif result.get("empty"):
            message = f"已推送 {result.get('label')}：没有新入库内容"
        else:
            message = (
                f"已推送 {result.get('label')}：{result.get('shows')} 部 / "
                f"{result.get('episodes')} 集"
                f"{'，含图片' if result.get('image') else '，纯文本'}"
            )
        self._remember_test("推送真实日报", message)
        return {"success": result is not None, "message": message}

    def _run_test(self, *, with_image: bool) -> Dict[str, Any]:
        """
        构造并发送测试消息。

        :param with_image: 是否使用样例数据生成配图
        :return: 供页面展示的执行结果
        """
        title = "每日入库速报 · 测试"
        check: Optional[str] = None
        image: Optional[str] = None
        if with_image:
            image, check = self._build_image(self._sample_digest())
            if not image:
                message = "图片未能生成：请检查「外网访问地址」与中文字体，详情见日志"
                self._remember_test("测试图片", message)
                return {"success": False, "message": message}
            text = f"这是一条带图测试消息。图片地址：{image}\n地址自检：{check}"
        else:
            text = (
                "这是一条纯文本测试消息。收到说明通知渠道通畅；"
                "没收到请检查渠道的「场景开关」是否勾选了「插件」。\n"
                + "\n".join(self._channel_status())
            )

        try:
            self.post_message(mtype=_MESSAGE_TYPE, title=title, text=text, image=image)
        except Exception as error:  # noqa: BLE001 - 测试失败要回给页面而不是抛出
            message = f"发送失败：{error}"
            self._remember_test("测试文本" if not with_image else "测试图片", message)
            return {"success": False, "message": message}

        if with_image:
            message = f"已提交带图测试消息。图片地址：{image}｜地址自检：{check}"
        else:
            message = "已提交纯文本测试消息。若未收到，请检查渠道场景开关是否勾选「插件」。"
        self._remember_test("测试文本" if not with_image else "测试图片", message, image, check)
        logger.info(f"每日入库速报：{message}")
        return {"success": True, "message": message}

    @staticmethod
    def _sample_digest() -> Digest:
        """
        构造用于图片测试的样例数据，不依赖真实入库记录。
        """
        return Digest(
            day_label="测试",
            start="",
            end="",
            groups=[
                EpisodeGroup(
                    title="示例剧集 · 第一季",
                    media_type="电视剧",
                    season="S01",
                    episodes=[1, 2, 3],
                ),
                EpisodeGroup(
                    title="示例电影",
                    media_type="电影",
                    year="2026",
                ),
            ],
        )

    def _remember_test(
        self,
        kind: str,
        message: str,
        image_url: Optional[str] = None,
        image_check: Optional[str] = None,
    ) -> None:
        """
        记录最近一次测试结果，供详情页刷新后展示。
        """
        try:
            self.save_data(
                "last_test",
                {
                    "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "kind": kind,
                    "message": message,
                    "image_url": image_url,
                    "image_check": image_check,
                },
            )
        except Exception as error:  # noqa: BLE001 - 记录失败不影响测试结论
            logger.debug(f"每日入库速报：保存测试记录失败：{error}")

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
                            self._col(4, [self._switch("enabled", "启用插件")]),
                            self._col(4, [self._switch("notify_empty", "无新增也推送")]),
                            self._col(4, [self._switch("run_once", "保存后立即推送一次")]),
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
                                            "label": "推送周期",
                                            "placeholder": "5位cron表达式",
                                        },
                                    }
                                ],
                            ),
                            self._col(
                                6,
                                [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "model": "window",
                                            "label": "统计范围",
                                            "items": WINDOW_OPTIONS,
                                        },
                                    }
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
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "multiple": True,
                                            "chips": True,
                                            "model": "media_types",
                                            "label": "统计类型",
                                            "items": MEDIA_TYPE_OPTIONS,
                                        },
                                    }
                                ],
                            ),
                            self._col(
                                6,
                                [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "image_items",
                                            "label": "图片内最多列出部数",
                                            "type": "number",
                                            "placeholder": "1-20，超出部分在图片底部提示",
                                        },
                                    }
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
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "exclude_words",
                                            "label": "排除关键词",
                                            "placeholder": "逗号分隔，命中标题则跳过",
                                        },
                                    }
                                ],
                            ),
                            self._col(
                                6,
                                [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "public_domain",
                                            "label": "外网访问地址",
                                            "placeholder": "留空则使用 MoviePilot 设置中的外网访问地址",
                                        },
                                    }
                                ],
                            ),
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            self._col(
                                12,
                                [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "font_path",
                                            "label": "中文字体路径",
                                            "placeholder": "留空则自动查找或下载 Noto Sans CJK",
                                        },
                                    }
                                ],
                            )
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            self._col(
                                12,
                                [
                                    {
                                        "component": "VAlert",
                                        "props": {
                                            "type": "info",
                                            "variant": "tonal",
                                            "text": "【重要】通知渠道的「场景开关」必须勾选「插件」，"
                                            "否则宿主的 check_message 会把本插件的广播消息直接丢弃且不写日志。"
                                            "保存后可在消息渠道发送 /digest_status 查看诊断，"
                                            "或点详情页的测试按钮验证。"
                                            "日报图片以链接形式交给渠道，插件会依次实测「外网访问地址 → 宿主域名 → 容器内地址」，"
                                            "取第一个可访问的；外网地址解析不了时会回退到容器内地址，"
                                            "此时 Telegram、微信能正常收图，钉钉、飞书这类由客户端拉图的渠道会看不到图。"
                                            "MoviePilot 镜像不含中文字体，插件会在首次出图时自动下载并缓存到插件数据目录；"
                                            "离线环境可把任意中文字体放到插件目录 fonts/ 下，或用「中文字体路径」指定。"
                                            "统计口径为 MoviePilot 整理入库记录，手工放入媒体库的文件不计入。"
                                            "手动触发：/digest_push，支持 /digest_push today 或 24h 临时切换统计范围。",
                                        },
                                    }
                                ],
                            )
                        ],
                    },
                ]
            }
        ], dict(self._config)

    @staticmethod
    def _col(width: int, content: List[dict]) -> dict:
        """
        构造栅格列，减少表单描述的重复结构。
        """
        return {"component": "VCol", "props": {"cols": 12, "md": width}, "content": content}

    @staticmethod
    def _switch(model: str, label: str) -> dict:
        """
        构造开关组件。
        """
        return {"component": "VSwitch", "props": {"model": model, "label": label}}

    def get_page(self) -> List[dict]:
        """
        返回详情页：测试按钮、最近一次测试结果与上次推送摘要。
        """
        content: List[dict] = [self._action_bar()]

        test = self.get_data("last_test") or {}
        if test:
            content.append(
                {
                    "component": "VAlert",
                    "props": {
                        "type": "info",
                        "variant": "tonal",
                        "text": f"最近测试 {test.get('time')}｜{test.get('kind')}\n"
                        f"{test.get('message')}",
                    },
                }
            )

        last = self.get_data("last_digest") or {}
        if not last:
            content.append(
                {
                    "component": "VAlert",
                    "props": {
                        "type": "warning",
                        "variant": "tonal",
                        "text": "尚未推送过。点上方的按钮可以直接测试。",
                    },
                }
            )
            return content

        summary = (
            f"上次推送：{last.get('time') or '-'}｜窗口 {last.get('start')} ~ {last.get('end')}"
            f"｜{last.get('shows', 0)} 部 / {last.get('episodes', 0)} 集"
        )
        if last.get("movies"):
            summary += f"｜含电影 {last.get('movies')} 部"
        summary += "｜图片已附带" if last.get("image") else "｜纯文本（未生成图片）"
        content.append(
            {
                "component": "VAlert",
                "props": {"type": "success", "variant": "tonal", "text": summary},
            }
        )
        if last.get("image_url"):
            content.append(
                {
                    "component": "VAlert",
                    "props": {
                        "type": "info",
                        "variant": "outlined",
                        "text": f"图片地址：{last.get('image_url')}\n"
                        f"地址自检：{last.get('image_check') or '未执行'}",
                    },
                }
            )
        return content

    def _action_bar(self) -> dict:
        """
        构造详情页的操作按钮，点击后调用本插件 API，宿主会自动刷新本页。
        """
        plugin_id = self.__class__.__name__

        def button(text: str, path: str, color: str = "primary", variant: str = "tonal") -> dict:
            return {
                "component": "VBtn",
                "props": {
                    "color": color,
                    "variant": variant,
                    "size": "small",
                    "class": "mr-2 mb-2",
                },
                "text": text,
                "events": {
                    "click": {
                        "api": f"plugin/{plugin_id}/{path}",
                        "method": "post",
                    }
                },
            }

        return {
            "component": "div",
            "props": {"class": "d-flex flex-wrap align-center mb-2"},
            "content": [
                button("发送测试文本", "test_text"),
                button("发送测试图片", "test_image"),
                button("立即推送真实日报", "push_now", color="secondary", variant="outlined"),
            ],
        }

    def stop_service(self) -> None:
        """
        释放本插件登记的一次性任务，可重复调用。
        """
        try:
            scheduler_sdk.remove_plugin_once_job(self.__class__.__name__, _ONCE_JOB_ID)
        except Exception as error:  # noqa: BLE001 - 宿主版本差异时忽略
            logger.debug(f"每日入库速报：移除一次性任务失败：{error}")

    def push_digest(self, window: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """
        执行一次统计、出图与推送，供定时服务、立即推送与远程命令共用。

        :param window: 临时覆盖配置中的统计范围，用于手动指定窗口
        :return: 本次推送概要；取数失败时返回 None
        """
        config = self._config
        mode = window or config.get("window", WINDOW_YESTERDAY)
        start, end, day_label = resolve_window(mode)
        media_types = list(config.get("media_types") or [])
        try:
            records = fetch_records(start, end, media_types)
        except Exception as error:  # noqa: BLE001 - 取数失败只影响本次推送
            logger.error(f"每日入库速报：读取整理历史失败：{error}")
            return None

        digest = build_digest(
            records,
            start=start,
            end=end,
            day_label=day_label,
            include_movie="电影" in media_types,
            exclude_words=split_words(config.get("exclude_words", "")),
            max_items=int(config.get("image_items") or 8),
        )
        logger.info(
            f"每日入库速报：{day_label} 窗口内命中 {len(records)} 条整理记录，"
            f"聚合为 {digest.total_shows} 部 / {digest.total_episodes} 集"
        )
        summary: Dict[str, Any] = {
            "label": day_label,
            "shows": digest.total_shows,
            "episodes": digest.total_episodes,
            "movies": digest.total_movies,
            "empty": digest.is_empty,
            "skipped": False,
            "image": False,
            "image_url": None,
            "image_check": None,
        }
        if digest.is_empty and not config.get("notify_empty"):
            logger.info(f"每日入库速报：{day_label}无新增入库，按配置跳过推送")
            summary["skipped"] = True
            return summary

        title, fallback_text = render(digest, empty_hint=f"{day_label}没有新入库内容")
        image, image_check = self._build_image(digest)
        # 有图时正文退化为一句概要，作为不支持图片渠道的兜底；无图时正文即完整清单
        text = self._caption(digest) if image else fallback_text
        self._send(title=title, text=text, image=image)
        self._remember(digest, title, text, image, image_check)
        summary["image"] = bool(image)
        summary["image_url"] = image
        summary["image_check"] = image_check
        return summary

    def _caption(self, digest: Digest) -> str:
        """
        生成配图消息的简短正文。
        """
        if digest.is_empty:
            return f"{digest.day_label}没有新入库内容"
        shows = digest.total_shows - digest.total_movies
        parts: list[str] = []
        if shows:
            parts.append(f"{shows} 部剧集")
        if digest.total_episodes:
            parts.append(f"{digest.total_episodes} 集")
        if digest.total_movies:
            parts.append(f"{digest.total_movies} 部电影")
        return "共 " + " · ".join(parts) if parts else "无入库"

    def _build_image(self, digest: Digest) -> Tuple[Optional[str], Optional[str]]:
        """
        生成日报图片并返回可被通知渠道访问的地址。

        外网地址可能无法从宿主内部解析（DDNS、内网 DNS 等），因此逐个候选地址实测，
        取第一个返回 200 的地址；都不通时保留首个候选，交给渠道自行判断。

        :return: `(图片地址, 自检结果)`，任一步失败时地址为 None
        """
        if digest.is_empty:
            return None, None

        font_book = self._ensure_font()
        if font_book is None:
            return None, None

        name = f"digest_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jpg"
        data_path = self.get_data_path()
        target = data_path / name
        if not render_card(digest, font_book, target, max_rows=int(self._config.get("image_items") or 8)):
            logger.warning("每日入库速报：日报图片生成失败，本次降级为纯文本推送")
            return None, None
        prune_old_cards(data_path)

        route = f"{_API_PREFIX}/{self.__class__.__name__}/card/{name}"
        chosen: Optional[str] = None
        chosen_check: Optional[str] = None
        for index, base in enumerate(self._base_candidates()):
            url = f"{base}{route}"
            check = self._verify_image_url(url)
            if chosen is None:
                chosen, chosen_check = url, check
            if check == "HTTP 200":
                chosen, chosen_check = url, check
                if index > 0:
                    logger.warning(
                        f"每日入库速报：外网地址不可用，图片改由 {base} 提供；"
                        "该回退只对由宿主下载图片的渠道（Telegram、微信）有效，"
                        "钉钉、飞书这类由客户端拉图的渠道会看不到图"
                    )
                break

        size = target.stat().st_size if target.is_file() else 0
        logger.info(
            f"每日入库速报：日报图片已生成 {target}（{size} 字节），"
            f"使用地址 {chosen}，自检 {chosen_check}"
        )
        return chosen, chosen_check

    @staticmethod
    def _verify_image_url(url: str) -> str:
        """
        从宿主内部请求一次图片地址，用于判断地址是否可达。

        容器内访问自身公网域名可能受回环/NAT 影响，失败不代表外部不可用；
        但返回 200 说明链路完全正常，返回 4xx 则说明路径或鉴权有问题。
        """
        try:
            import httpx2

            with httpx2.Client(timeout=8.0, follow_redirects=True) as client:
                response = client.get(url)
            return f"HTTP {response.status_code}"
        except Exception as error:  # noqa: BLE001 - 自检失败只作为诊断信息
            return f"请求失败（{error}）"

    def _ensure_font(self) -> Optional[FontBook]:
        """
        惰性解析字体并缓存，避免每次推送都重新查找。
        """
        if self._font_book is None:
            self._font_book = resolve_font(self.get_data_path(), self._config.get("font_path", ""))
        return self._font_book

    def _send(self, *, title: str, text: str, image: Optional[str]) -> None:
        """
        通过宿主通知渠道发送消息。
        """
        link = settings.MP_DOMAIN("#/history")
        try:
            self.post_message(
                mtype=MessageType.Plugin,
                title=title,
                text=text,
                image=image,
                link=link,
            )
            logger.info(
                f"每日入库速报：已提交通知「{title}」{'（含图片）' if image else '（纯文本）'}；"
                f"渠道若未在场景开关里勾选「{_MESSAGE_TYPE.value}」，消息会被宿主静默丢弃"
            )
        except Exception as error:  # noqa: BLE001 - 通知失败不应中断插件
            logger.error(f"每日入库速报：发送通知失败：{error}")

    def _remember(
        self,
        digest: Digest,
        title: str,
        text: str,
        image: Optional[str],
        image_check: Optional[str] = None,
    ) -> None:
        """
        缓存本次推送结果，供详情页与诊断命令展示。
        """
        try:
            self.save_data(
                "last_digest",
                {
                    "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "start": digest.start,
                    "end": digest.end,
                    "title": title,
                    "text": text,
                    "shows": digest.total_shows,
                    "episodes": digest.total_episodes,
                    "movies": digest.total_movies,
                    "image": image,
                    "image_url": image,
                    "image_check": image_check,
                },
            )
        except Exception as error:  # noqa: BLE001 - 缓存失败不影响推送结果
            logger.warning(f"每日入库速报：保存推送记录失败：{error}")

    def _base_candidates(self) -> List[str]:
        """
        按优先级返回图片基础地址候选。

        前两项来自插件配置与宿主设置；最后一项指向容器自身的回环端口，用于外网地址
        在宿主内解析不了时兜底。Telegram、微信等渠道由宿主先下载图片字节再上传，
        回环地址同样可用；钉钉、飞书这类由客户端拉图的渠道则不行。
        """
        raw: List[str] = []
        custom = (self._config.get("public_domain") or "").strip()
        if custom:
            raw.append(custom)
        try:
            domain = settings.MP_DOMAIN("") or ""
            if domain:
                raw.append(domain)
        except Exception as error:  # noqa: BLE001 - 宿主未配置域名时降级
            logger.debug(f"每日入库速报：读取外网地址失败：{error}")

        try:
            port = int(getattr(settings, "PORT", 3001) or 3001)
        except Exception:  # noqa: BLE001 - 端口读取失败时用默认值
            port = 3001
        raw.append(f"http://127.0.0.1:{port}")

        candidates: List[str] = []
        for item in raw:
            value = item.strip().rstrip("/")
            if not value:
                continue
            if not value.startswith(("http://", "https://")):
                value = f"http://{value}"
            if value not in candidates:
                candidates.append(value)
        return candidates
