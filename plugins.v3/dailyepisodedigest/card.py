"""
每日入库日报卡片绘制。

把聚合结果渲染成一张深色长图：顶部标题与统计，中部逐部列出海报、标题、季集与入库时间，
底部标注来源。整张图作为通知配图推送，因此每部作品的封面都出现在同一张图里。
"""

from __future__ import annotations

from io import BytesIO
from pathlib import Path
from typing import Any, Optional

from app.sdk.logging import logger

from .digest import Digest, EpisodeGroup, group_detail, short_time
from .fonts import FontBook

# 画布宽度固定为 1080，主流渠道展示时不会被压缩到看不清文字
_WIDTH = 1080
_PAD = 56
# 海报尺寸与圆角
_POSTER_WIDTH = 132
_POSTER_HEIGHT = 198
_POSTER_RADIUS = 14
# 海报与文字之间的间距
_GAP = 32
# 行间距
_ROW_GAP = 30
# 单张图最多列出几部，超出部分在底部汇总提示
_MAX_ROWS = 8
_POSTER_TIMEOUT = 15.0
_MIN_IMAGE_BYTES = 512

# 配色：深色底 + 单一强调色，保证在聊天窗口里不刺眼
_BG_TOP = (18, 20, 26)
_BG_BOTTOM = (27, 31, 39)
_TEXT = (240, 243, 248)
_MUTED = (150, 159, 174)
_DIM = (108, 116, 130)
_ACCENT = (91, 157, 255)
_LINE = (42, 48, 60)


def _lerp(begin: tuple[int, int, int], end: tuple[int, int, int], ratio: float) -> tuple[int, int, int]:
    """
    在两个颜色之间线性插值。
    """
    return tuple(int(begin[index] + (end[index] - begin[index]) * ratio) for index in range(3))  # type: ignore[return-value]


def _draw_gradient(image: Any, height: int) -> None:
    """
    自上而下绘制背景渐变。
    """
    from PIL import ImageDraw

    draw = ImageDraw.Draw(image)
    for y in range(height):
        draw.line([(0, y), (_WIDTH, y)], fill=_lerp(_BG_TOP, _BG_BOTTOM, y / max(1, height - 1)))


def _fetch_poster(url: str) -> Optional[Any]:
    """
    下载海报并转为 RGB 图像，失败返回 None。
    """
    if not url:
        return None
    try:
        import httpx2
        from PIL import Image

        with httpx2.Client(timeout=_POSTER_TIMEOUT, follow_redirects=True) as client:
            response = client.get(url)
        if response.status_code != 200 or len(response.content) < _MIN_IMAGE_BYTES:
            return None
        with Image.open(BytesIO(response.content)) as opened:
            return opened.convert("RGB")
    except Exception as error:  # noqa: BLE001 - 单张海报失败不应中断整张卡片
        logger.debug(f"每日入库速报：海报下载失败 {url}，{error}")
        return None


def _placeholder(title: str) -> Any:
    """
    海报缺失时生成占位图：按标题取色的渐变块加首字，避免卡片出现空洞。
    """
    from PIL import Image, ImageDraw

    seed = sum(ord(char) for char in title) or 1
    base = (40 + seed % 50, 46 + (seed // 7) % 50, 60 + (seed // 13) % 55)
    image = Image.new("RGB", (_POSTER_WIDTH, _POSTER_HEIGHT), base)
    draw = ImageDraw.Draw(image)
    for y in range(_POSTER_HEIGHT):
        draw.line([(0, y), (_POSTER_WIDTH, y)], fill=_lerp(base, _lerp(base, (0, 0, 0), 0.45), y / _POSTER_HEIGHT))
    return image


def _paste_poster(canvas: Any, poster: Any, left: int, top: int) -> None:
    """
    把海报等比裁剪后按圆角贴到画布上。
    """
    from PIL import Image, ImageDraw

    source_width, source_height = poster.size
    scale = max(_POSTER_WIDTH / source_width, _POSTER_HEIGHT / source_height)
    resized = poster.resize(
        (max(1, round(source_width * scale)), max(1, round(source_height * scale))),
        Image.Resampling.LANCZOS,
    )
    crop_left = max(0, (resized.size[0] - _POSTER_WIDTH) // 2)
    crop_top = max(0, (resized.size[1] - _POSTER_HEIGHT) // 2)
    cropped = resized.crop((crop_left, crop_top, crop_left + _POSTER_WIDTH, crop_top + _POSTER_HEIGHT))
    mask = Image.new("L", (_POSTER_WIDTH, _POSTER_HEIGHT), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, _POSTER_WIDTH - 1, _POSTER_HEIGHT - 1), radius=_POSTER_RADIUS, fill=255
    )
    canvas.paste(cropped, (left, top), mask)
    cropped.close()
    resized.close()


def _ellipsize(draw: Any, text: str, font: Any, max_width: int) -> str:
    """
    按可用宽度截断文本并补省略号。
    """
    if draw.textlength(text, font=font) <= max_width:
        return text
    ellipsis = "…"
    trimmed = text
    while trimmed and draw.textlength(trimmed + ellipsis, font=font) > max_width:
        trimmed = trimmed[:-1]
    return (trimmed + ellipsis) if trimmed else ellipsis


def _row_meta(group: EpisodeGroup) -> str:
    """
    单部作品的副标题，如 `S02 E03-E04 · 2 集`。
    """
    return group_detail(group)


def _row_time(group: EpisodeGroup) -> str:
    """
    单部作品的入库时间，如 `入库 10-23 23:02`。
    """
    stamp = short_time(group.last_time)
    return f"入库 {stamp}" if stamp else ""


def render_card(
    digest: Digest,
    font_book: FontBook,
    target: Path,
    max_rows: int = _MAX_ROWS,
) -> Optional[Path]:
    """
    绘制日报卡片并保存为 JPEG。

    :param digest: 聚合后的摘要
    :param font_book: 已解析的字体
    :param target: 输出文件路径
    :param max_rows: 卡片内最多列出几部，超出部分在底部汇总提示
    :return: 成功时返回输出路径，否则返回 None
    """
    try:
        from PIL import Image, ImageDraw
    except Exception as error:  # noqa: BLE001 - 宿主缺少 Pillow 时无法出图
        logger.warning(f"每日入库速报：Pillow 不可用，无法生成图片：{error}")
        return None

    try:
        font_title = font_book.get(46)
        font_date = font_book.get(28)
        font_summary = font_book.get(26)
        font_name = font_book.get(36)
        font_meta = font_book.get(26)
        font_time = font_book.get(22)
        font_foot = font_book.get(20)
    except Exception as error:  # noqa: BLE001 - 字体文件损坏时降级
        logger.error(f"每日入库速报：字体加载失败，无法生成图片：{error}")
        return None

    rows = list(digest.groups[:max_rows])
    hidden = (digest.total_shows - len(rows)) + digest.dropped

    # 先算高度再绘制，避免二次裁切
    header_height = _PAD + 56 + 26 + 30 + 30
    rows_top = header_height + 8
    rows_height = len(rows) * _POSTER_HEIGHT + max(0, len(rows) - 1) * _ROW_GAP
    footer_area = 104 if hidden else 76
    height = rows_top + rows_height + footer_area

    canvas = Image.new("RGB", (_WIDTH, height), _BG_TOP)
    _draw_gradient(canvas, height)
    draw = ImageDraw.Draw(canvas)

    text_left = _PAD + _POSTER_WIDTH + _GAP
    text_width = _WIDTH - text_left - _PAD

    # 标题与日期
    title_y = _PAD + 28
    draw.text((_PAD, title_y), "每日入库速报", font=font_title, fill=_TEXT, anchor="lm", stroke_width=1, stroke_fill=_TEXT)
    draw.text(
        (_WIDTH - _PAD, title_y),
        digest.day_label,
        font=font_date,
        fill=_MUTED,
        anchor="rm",
    )

    # 分隔线
    line_y = title_y + 46
    draw.line([(_PAD, line_y), (_WIDTH - _PAD, line_y)], fill=_LINE, width=2)

    # 统计概要
    summary_y = line_y + 34
    shows = digest.total_shows - digest.total_movies
    parts: list[str] = []
    if shows:
        parts.append(f"{shows} 部剧集")
    if digest.total_episodes:
        parts.append(f"{digest.total_episodes} 集")
    if digest.total_movies:
        parts.append(f"{digest.total_movies} 部电影")
    summary = " · ".join(parts) if parts else "无入库"
    draw.text((_PAD, summary_y), summary, font=font_summary, fill=_ACCENT, anchor="lm")

    # 逐部条目
    row_top = rows_top
    for index, group in enumerate(rows):
        top = row_top + index * (_POSTER_HEIGHT + _ROW_GAP)
        poster = _fetch_poster(group.poster or "") or _placeholder(group.title)
        try:
            _paste_poster(canvas, poster, _PAD, top)
        finally:
            poster.close()

        # 三行文字按海报高度整体居中
        draw.text(
            (text_left, top + 54),
            _ellipsize(draw, group.title, font_name, text_width),
            font=font_name,
            fill=_TEXT,
            anchor="lm",
        )
        draw.text((text_left, top + 104), _row_meta(group), font=font_meta, fill=_MUTED, anchor="lm")
        stamp = _row_time(group)
        if stamp:
            draw.text((text_left, top + 146), stamp, font=font_time, fill=_DIM, anchor="lm")

        if index < len(rows) - 1:
            sep_y = top + _POSTER_HEIGHT + _ROW_GAP // 2
            draw.line([(_PAD, sep_y), (_WIDTH - _PAD, sep_y)], fill=_LINE, width=1)

    # 底部
    if hidden:
        draw.text(
            (_PAD, height - 74),
            f"另有 {hidden} 部未列出",
            font=font_summary,
            fill=_MUTED,
            anchor="lm",
        )
    draw.text(
        (_WIDTH - _PAD, height - 40),
        "MoviePilot · 自动整理入库",
        font=font_foot,
        fill=_DIM,
        anchor="rm",
    )

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        canvas.save(target, format="JPEG", quality=90, optimize=True)
    except Exception as error:  # noqa: BLE001 - 写盘失败按降级处理
        logger.error(f"每日入库速报：卡片保存失败 {target}，{error}")
        return None
    finally:
        canvas.close()
    return target


def prune_old_cards(directory: Path, keep: int = 10) -> None:
    """
    清理历史卡片，只保留最近若干张。

    :param directory: 卡片所在目录
    :param keep: 保留数量
    """
    if not directory.exists():
        return
    files = sorted(
        (item for item in directory.glob("digest_*.jpg") if item.is_file()),
        key=lambda item: item.name,
        reverse=True,
    )
    for stale in files[keep:]:
        try:
            stale.unlink()
        except OSError as error:
            logger.debug(f"每日入库速报：清理旧卡片失败 {stale}，{error}")
