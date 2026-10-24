"""
中文字体解析：为图片渲染准备可用的字体文件。

MoviePilot 官方镜像没有安装任何中文字体，`ImageFont.load_default()` 也不含 CJK 字形，
因此必须自带或下载一份字体。解析顺序（前一个可用即返回）：

1. 配置里指定的字体文件路径
2. 插件源码目录 `fonts/` 下用户自行放入的字体
3. 插件数据目录 `fonts/` 下的缓存
4. 宿主系统的常见中文字体路径
5. 从固定地址下载并缓存到插件数据目录

全部失败时返回 None，调用方降级为纯文本推送。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from app.sdk.logging import logger

# 只下载 Regular，标题加粗用描边模拟，避免再多下载 16MB 的 Bold 字重
_FONT_FILENAME = "NotoSansCJKsc-Regular.otf"
_FONT_URLS = (
    "https://cdn.jsdelivr.net/gh/notofonts/noto-cjk@Sans2.004/Sans/OTF/SimplifiedChinese/NotoSansCJKsc-Regular.otf",
    "https://raw.githubusercontent.com/notofonts/noto-cjk/Sans2.004/Sans/OTF/SimplifiedChinese/NotoSansCJKsc-Regular.otf",
)
# 完整字体约 16MB，明显偏小的文件说明下载到了错误页
_MIN_FONT_BYTES = 1024 * 1024
_DOWNLOAD_TIMEOUT = 120.0

# 宿主系统的常见中文字体位置，覆盖 Linux 发行版、macOS 与 Windows 桌面运行
_SYSTEM_FONTS = (
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/truetype/arphic/uming.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/msyhbd.ttc",
    "C:/Windows/Fonts/simhei.ttf",
)

_FONT_SUFFIXES = (".otf", ".ttf", ".ttc")


class FontBook:
    """
    字体文件与按字号缓存的字体对象。

    图片渲染会反复取用不同字号的同一份字体，缓存字体对象可避免重复解析大字体文件。
    """

    def __init__(self, path: Path) -> None:
        """
        记录字体文件路径并初始化字号缓存。

        :param path: 可用的字体文件路径
        """
        self.path = path
        self._cache: dict[int, Any] = {}

    def get(self, size: int) -> Any:
        """
        取指定字号的字体对象，失败时抛出异常由调用方降级。

        :param size: 字号，单位像素
        :return: PIL 字体对象
        """
        from PIL import ImageFont

        cached = self._cache.get(size)
        if cached is None:
            cached = ImageFont.truetype(str(self.path), size)
            self._cache[size] = cached
        return cached


def _pick_from_dir(directory: Path) -> Optional[Path]:
    """
    在目录中挑一个可用的字体文件，优先文件名含 Regular 的条目。
    """
    if not directory.is_dir():
        return None
    candidates = sorted(
        (item for item in directory.iterdir() if item.is_file() and item.suffix.lower() in _FONT_SUFFIXES),
        key=lambda item: (("regular" not in item.name.lower()), item.name),
    )
    return candidates[0] if candidates else None


def _download_font(target: Path) -> Optional[Path]:
    """
    下载字体到缓存路径，全部镜像失败时返回 None。
    """
    if target.exists() and target.stat().st_size >= _MIN_FONT_BYTES:
        return target
    try:
        import httpx2
    except Exception as error:  # noqa: BLE001 - 宿主缺少 httpx2 时无法下载
        logger.warning(f"每日入库速报：httpx2 不可用，无法下载字体：{error}")
        return None

    target.parent.mkdir(parents=True, exist_ok=True)
    for url in _FONT_URLS:
        try:
            logger.info(f"每日入库速报：正在下载中文字体（约 16MB，仅首次需要）{url}")
            with httpx2.Client(timeout=_DOWNLOAD_TIMEOUT, follow_redirects=True) as client:
                response = client.get(url)
            if response.status_code != 200:
                logger.warning(f"每日入库速报：字体下载返回 HTTP {response.status_code}")
                continue
            payload = response.content
            if len(payload) < _MIN_FONT_BYTES:
                logger.warning(f"每日入库速报：字体文件异常偏小（{len(payload)} 字节），已忽略")
                continue
            temp = target.with_suffix(target.suffix + ".part")
            temp.write_bytes(payload)
            temp.replace(target)
            logger.info(f"每日入库速报：中文字体已缓存到 {target}")
            return target
        except Exception as error:  # noqa: BLE001 - 换下一个镜像重试
            logger.warning(f"每日入库速报：字体下载失败 {url}，{error}")
    return None


def resolve_font(data_dir: Path, custom_path: str = "") -> Optional[FontBook]:
    """
    按优先级解析可用字体。

    :param data_dir: 插件数据目录，用于存放下载缓存
    :param custom_path: 用户在配置中指定的字体文件路径
    :return: 可用字体；找不到任何中文字体时返回 None
    """
    if custom_path:
        custom = Path(custom_path).expanduser()
        if custom.is_file() and custom.suffix.lower() in _FONT_SUFFIXES:
            return FontBook(custom)
        logger.warning(f"每日入库速报：配置的字体文件不存在或格式不支持：{custom_path}")

    plugin_dir = Path(__file__).resolve().parent
    bundled = _pick_from_dir(plugin_dir / "fonts")
    if bundled:
        return FontBook(bundled)

    cache_dir = data_dir / "fonts"
    cached = cache_dir / _FONT_FILENAME
    if cached.is_file() and cached.stat().st_size >= _MIN_FONT_BYTES:
        return FontBook(cached)

    for system_path in _SYSTEM_FONTS:
        candidate = Path(system_path)
        if candidate.is_file():
            logger.info(f"每日入库速报：使用系统字体 {system_path}")
            return FontBook(candidate)

    downloaded = _download_font(cached)
    if downloaded:
        return FontBook(downloaded)

    logger.error("每日入库速报：未找到可用的中文字体，图片推送将降级为文本推送")
    return None
