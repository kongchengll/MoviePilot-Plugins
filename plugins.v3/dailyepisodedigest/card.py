"""
每日入库日报卡片绘制。

把聚合结果渲染成一张深色长图：顶部品牌头与统计卡片，中部逐部列出海报、标题、
季集、分类与入库时间，可选追加猫眼实时票房面板，底部标注统计区间与生成时间。
整张图作为通知配图推送，因此所有内容都在同一张图里。

本模块只依赖 Pillow 与宿主日志接口，不访问宿主数据库；布局采用「先算高度、再按区块
绘制」的方式，避免内容增减后出现裁切或行与行重叠。
"""

from __future__ import annotations

from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Any, Optional

from app.sdk.logging import logger

from .boxoffice import BoxOfficeData
from .digest import Digest, EpisodeGroup, format_episodes, short_time
from .fonts import FontBook

# 画布宽度固定为 1080，主流渠道展示时不会被压缩到看不清文字
_WIDTH = 1080
_PAD = 56
_CONTENT_WIDTH = _WIDTH - _PAD * 2
# 海报尺寸与圆角
_POSTER_WIDTH = 148
_POSTER_HEIGHT = 222
_POSTER_RADIUS = 14
# 单部作品卡片
_ROW_PAD = 20
_ROW_HEIGHT = _POSTER_HEIGHT + _ROW_PAD * 2
_ROW_GAP = 18
_PANEL_RADIUS = 22
# 统计卡片
_STAT_HEIGHT = 112
_STAT_GAP = 16
# 票房面板
_BOX_SIDE_PAD = 28
_BOX_TITLE_HEIGHT = 46
_BOX_CELL_HEIGHT = 78
_BOX_ROW_HEIGHT = 66
# 单张图最多列出几部，超出部分在底部汇总提示
_MAX_ROWS = 8
_POSTER_TIMEOUT = 15.0
_MIN_IMAGE_BYTES = 512

# 配色：深色底 + 分层面板 + 多色强调，保证在聊天窗口里清晰但不刺眼
_BG_TOP = (15, 18, 26)
_BG_BOTTOM = (26, 31, 42)
_PANEL = (30, 35, 47)
_PANEL_ALT = (37, 43, 58)
_BORDER = (55, 64, 83)
_TEXT = (242, 245, 251)
_MUTED = (158, 168, 186)
_DIM = (108, 118, 138)
_ACCENT = (94, 158, 255)
_CYAN = (74, 208, 200)
_ORANGE = (255, 183, 94)
_PURPLE = (167, 139, 250)
_LINE = (48, 56, 72)

_WEEKDAYS = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")


def _lerp(begin: tuple[int, int, int], end: tuple[int, int, int], ratio: float) -> tuple[int, int, int]:
    """
    在两个颜色之间线性插值。
    """
    ratio = max(0.0, min(1.0, ratio))
    return tuple(int(begin[index] + (end[index] - begin[index]) * ratio) for index in range(3))  # type: ignore[return-value]


def _tint(color: tuple[int, int, int], factor: float) -> tuple[int, int, int]:
    """
    把强调色压暗，用作标签底色。
    """
    return tuple(int(channel * factor) for channel in color)  # type: ignore[return-value]


def _draw_gradient(image: Any, height: int) -> None:
    """
    自上而下绘制背景渐变。
    """
    from PIL import ImageDraw

    draw = ImageDraw.Draw(image)
    for y in range(height):
        draw.line([(0, y), (_WIDTH, y)], fill=_lerp(_BG_TOP, _BG_BOTTOM, y / max(1, height - 1)))


def _draw_topbar(draw: Any) -> None:
    """
    顶部一条多色渐变装饰条，让长图在聊天窗口里有明确的视觉起点。
    """
    stops = (_ACCENT, _CYAN, _PURPLE, _ORANGE)
    for x in range(_WIDTH):
        position = x / max(1, _WIDTH - 1) * (len(stops) - 1)
        index = min(int(position), len(stops) - 2)
        draw.line([(x, 0), (x, 7)], fill=_lerp(stops[index], stops[index + 1], position - index))


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


def _placeholder(title: str, font_book: FontBook) -> Any:
    """
    海报缺失时生成占位图：按标题取色的渐变块加首字，避免卡片出现空洞。
    """
    from PIL import Image, ImageDraw

    seed = sum(ord(char) for char in title) or 1
    base = (36 + seed % 46, 44 + (seed // 7) % 46, 62 + (seed // 13) % 52)
    image = Image.new("RGB", (_POSTER_WIDTH, _POSTER_HEIGHT), base)
    draw = ImageDraw.Draw(image)
    darker = _lerp(base, (0, 0, 0), 0.48)
    for y in range(_POSTER_HEIGHT):
        draw.line([(0, y), (_POSTER_WIDTH, y)], fill=_lerp(base, darker, y / _POSTER_HEIGHT))
    # 底部压一层暗色，让白色首字更清楚
    for y in range(_POSTER_HEIGHT - 90, _POSTER_HEIGHT):
        alpha = (y - (_POSTER_HEIGHT - 90)) / 90 * 0.55
        draw.line([(0, y), (_POSTER_WIDTH, y)], fill=_lerp(base, (10, 12, 18), alpha))
    if title:
        try:
            font = font_book.get(66)
            draw.text(
                (_POSTER_WIDTH / 2, _POSTER_HEIGHT - 46),
                title[0],
                font=font,
                fill=(238, 242, 250),
                anchor="mm",
                stroke_width=1,
                stroke_fill=(238, 242, 250),
            )
        except Exception:  # noqa: BLE001 - 字体异常时占位图仍可用
            pass
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


def _panel(draw: Any, box: tuple[int, int, int, int], *, fill: tuple[int, int, int] = _PANEL, radius: int = _PANEL_RADIUS) -> None:
    """
    绘制带细边框的圆角面板。
    """
    draw.rounded_rectangle(box, radius=radius, fill=fill, outline=_BORDER, width=2)


def _chip(
    draw: Any,
    left: int,
    top: int,
    text: str,
    font: Any,
    color: tuple[int, int, int],
) -> int:
    """
    绘制一枚胶囊标签并返回它的宽度。
    """
    height = 34
    width = int(draw.textlength(text, font=font)) + 28
    draw.rounded_rectangle((left, top, left + width, top + height), radius=height // 2, fill=_tint(color, 0.24))
    draw.text((left + width / 2, top + height / 2 + 1), text, font=font, fill=color, anchor="mm")
    return width


def _bold(draw: Any, xy: tuple[float, float], text: str, font: Any, fill: tuple[int, int, int], anchor: str = "lm") -> None:
    """
    用轻微描边模拟粗体：插件只下载 Regular 字重。
    """
    draw.text(xy, text, font=font, fill=fill, anchor=anchor, stroke_width=1, stroke_fill=fill)


def _parse_moment(raw: str) -> Optional[datetime]:
    """
    解析宿主时间字符串，失败返回 None。
    """
    try:
        return datetime.strptime((raw or "")[:19], "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None


def _window_text(digest: Digest) -> str:
    """
    生成统计区间文本，如 `11-10 00:00 ~ 11-11 00:00`。
    """
    start, end = short_time(digest.start), short_time(digest.end)
    if start and end:
        return f"统计区间 {start} ~ {end}"
    return ""


def _stat_cards(digest: Digest) -> list[tuple[str, str, str, tuple[int, int, int]]]:
    """
    顶部三张统计卡片的内容：`(数值, 单位, 说明, 强调色)`。
    """
    return [
        (str(digest.total_shows), "部", "新增作品", _ACCENT),
        (str(digest.total_episodes), "集", "剧集更新", _CYAN),
        (str(digest.total_movies), "部", "电影入库", _ORANGE),
    ]


def _boxoffice_height(boxoffice: Optional[BoxOfficeData]) -> int:
    """
    计算票房面板占用的总高度。无效数据返回 0。
    """
    if not boxoffice or not boxoffice.is_valid:
        return 0
    return (
        _BOX_SIDE_PAD
        + _BOX_TITLE_HEIGHT
        + _BOX_CELL_HEIGHT
        + 18
        + len(boxoffice.movies) * _BOX_ROW_HEIGHT
        + 26
    )


def _ratio(raw: str) -> float:
    """
    把票房占比文本转成 0~1 的浮点值，兼容 `35.2%` 与 `0.352`。
    """
    text = (raw or "").strip()
    if not text:
        return 0.0
    try:
        value = float(text.rstrip("%"))
    except ValueError:
        return 0.0
    if "%" in text or value > 1:
        value /= 100
    return max(0.0, min(1.0, value))


def _layout(
    rows: list[EpisodeGroup],
    hidden: int,
    boxoffice: Optional[BoxOfficeData],
) -> tuple[list[tuple[str, int, int]], int]:
    """
    先计算各区块位置与画布总高度，再交给绘制函数按位置渲染。

    :return: `(区块列表, 画布高度)`，区块为 `(名称, 顶部 y, 高度)`
    """
    sections: list[tuple[str, int, int]] = []
    cursor = 0

    def add(name: str, height: int, gap: int = 0) -> None:
        nonlocal cursor
        sections.append((name, cursor, height))
        cursor += height + gap

    rows_height = len(rows) * _ROW_HEIGHT + max(0, len(rows) - 1) * _ROW_GAP
    box_height = _boxoffice_height(boxoffice)

    add("topbar", 8)
    add("header", 150, 18)
    add("stats", _STAT_HEIGHT, 30)
    add("listhead", 40, 16)
    add("rows", rows_height, 22)
    if hidden:
        add("hidden", 40, 10)
    if box_height:
        add("boxoffice", box_height, 24)
    add("footer", 92, 18)
    return sections, cursor


def _draw_header(draw: Any, digest: Digest, top: int, font_book: FontBook) -> None:
    """
    顶部品牌头：栏目标签、主标题、统计区间，右侧日期与星期。
    """
    draw.rounded_rectangle((_PAD, top + 34, _PAD + 6, top + 106), radius=3, fill=_ACCENT)
    draw.text(
        (_PAD + 26, top + 20),
        "MOVIEPILOT   ·   DAILY DIGEST",
        font=font_book.get(18),
        fill=_ACCENT,
        anchor="la",
    )
    _bold(draw, (_PAD + 24, top + 80), "每日入库速报", font_book.get(50), _TEXT, "lm")
    subtitle = _window_text(digest)
    if subtitle:
        draw.text((_PAD + 26, top + 132), subtitle, font=font_book.get(22), fill=_MUTED, anchor="lm")

    moment = _parse_moment(digest.start)
    weekday = _WEEKDAYS[moment.weekday()] if moment else ""
    _bold(draw, (_WIDTH - _PAD, top + 56), digest.day_label, font_book.get(40), _TEXT, "rm")
    right_sub = "  ·  ".join(part for part in (weekday, "媒体库日报") if part)
    draw.text((_WIDTH - _PAD, top + 104), right_sub, font=font_book.get(22), fill=_MUTED, anchor="rm")


def _draw_stats(draw: Any, digest: Digest, top: int, font_book: FontBook) -> None:
    """
    三张统计卡片：新增作品、剧集更新、电影入库。
    """
    card_width = (_CONTENT_WIDTH - _STAT_GAP * 2) // 3
    font_value = font_book.get(46)
    font_unit = font_book.get(22)
    font_label = font_book.get(22)
    for index, (value, unit, label, color) in enumerate(_stat_cards(digest)):
        left = _PAD + index * (card_width + _STAT_GAP)
        _panel(draw, (left, top, left + card_width, top + _STAT_HEIGHT), radius=20)
        draw.rounded_rectangle((left + 20, top + 18, left + 26, top + 52), radius=3, fill=color)
        _bold(draw, (left + 42, top + 42), value, font_value, _TEXT)
        value_width = draw.textlength(value, font=font_value)
        draw.text((left + 42 + value_width + 10, top + 48), unit, font=font_unit, fill=color, anchor="lm")
        draw.text((left + 20, top + 88), label, font=font_label, fill=_MUTED, anchor="lm")


def _draw_listhead(draw: Any, digest: Digest, top: int, font_book: FontBook) -> None:
    """
    「入库明细」小标题与排序说明。
    """
    draw.rounded_rectangle((_PAD, top + 10, _PAD + 6, top + 36), radius=3, fill=_CYAN)
    _bold(draw, (_PAD + 22, top + 23), "入库明细", font_book.get(30), _TEXT)
    draw.text(
        (_WIDTH - _PAD, top + 23),
        f"按入库时间倒序  ·  共 {digest.total_shows} 部",
        font=font_book.get(20),
        fill=_DIM,
        anchor="rm",
    )


def _draw_row(
    draw: Any,
    canvas: Any,
    group: EpisodeGroup,
    index: int,
    top: int,
    font_book: FontBook,
) -> None:
    """
    单行作品卡片：海报、序号、标题、类型/季/集数标签、集号、分类年份、入库时间。
    """
    _panel(draw, (_PAD, top, _WIDTH - _PAD, top + _ROW_HEIGHT))

    poster_x = _PAD + _ROW_PAD
    poster_y = top + _ROW_PAD
    poster = _fetch_poster(group.poster or "") or _placeholder(group.title, font_book)
    try:
        _paste_poster(canvas, poster, poster_x, poster_y)
    finally:
        poster.close()

    # 海报描边与左上角序号，增强立体感
    draw.rounded_rectangle(
        (poster_x, poster_y, poster_x + _POSTER_WIDTH - 1, poster_y + _POSTER_HEIGHT - 1),
        radius=_POSTER_RADIUS,
        outline=(74, 86, 110),
        width=2,
    )
    draw.rounded_rectangle((poster_x + 10, poster_y + 10, poster_x + 46, poster_y + 46), radius=10, fill=(16, 20, 29))
    draw.text((poster_x + 28, poster_y + 28), str(index), font=font_book.get(20), fill=_ACCENT, anchor="mm")

    text_x = poster_x + _POSTER_WIDTH + 26
    text_right = _WIDTH - _PAD - _ROW_PAD
    text_width = text_right - text_x

    font_name = font_book.get(36)
    _bold(
        draw,
        (text_x, top + 48),
        _ellipsize(draw, group.title, font_name, text_width),
        font_name,
        _TEXT,
    )

    # 胶囊标签：类型、季、集数 / 年份
    font_chip = font_book.get(20)
    chips: list[tuple[str, tuple[int, int, int]]] = [
        (("电影" if group.is_movie else "电视剧"), (_ORANGE if group.is_movie else _ACCENT)),
    ]
    season = group.season_label
    if season:
        chips.append((season, _CYAN))
    if group.is_movie:
        chips.append(("单片", _PURPLE))
    else:
        chips.append((f"{group.episode_count} 集", _PURPLE))
    chip_x = text_x
    for label, color in chips:
        chip_x += _chip(draw, chip_x, top + 76, label, font_chip, color) + 10

    # 集号 / 作品详情
    if group.is_movie:
        detail = "本次入库 · 电影长片"
    else:
        episodes = format_episodes(group.episodes)
        detail = f"本次入库 {episodes}" if episodes else "本次入库 · 未识别集号"
    draw.text((text_x, top + 144), _ellipsize(draw, detail, font_book.get(24), text_width), font=font_book.get(24), fill=_MUTED, anchor="lm")

    # 分类与年份
    meta = "  ·  ".join(part for part in (group.category, f"{group.year} 年" if group.year else "") if part)
    if meta:
        draw.text((text_x, top + 182), _ellipsize(draw, meta, font_book.get(22), text_width), font=font_book.get(22), fill=_DIM, anchor="lm")

    # 入库时间
    stamp = short_time(group.last_time)
    if stamp:
        draw.ellipse((text_x, top + 211, text_x + 9, top + 220), fill=_CYAN)
        draw.text((text_x + 20, top + 215), f"入库 {stamp}", font=font_book.get(22), fill=_MUTED, anchor="lm")


def _draw_hidden(draw: Any, hidden: int, top: int, font_book: FontBook) -> None:
    """
    超出图片展示上限时的汇总提示。
    """
    draw.rounded_rectangle((_PAD, top, _WIDTH - _PAD, top + 40), radius=12, fill=_PANEL_ALT)
    font = font_book.get(22)
    note = f"另有 {hidden} 部未列出，可在配置中调高「图片内最多列出部数」"
    draw.text((_WIDTH / 2, top + 20), _ellipsize(draw, note, font, _CONTENT_WIDTH - 40), font=font, fill=_MUTED, anchor="mm")


def _draw_boxoffice(draw: Any, boxoffice: BoxOfficeData, top: int, height: int, font_book: FontBook) -> None:
    """
    票房面板：四格大盘概览 + Top N 榜单（占比条、实时票房、累计票房）。
    """
    _panel(draw, (_PAD, top, _WIDTH - _PAD, top + height), fill=_PANEL_ALT)

    inner_left = _PAD + _BOX_SIDE_PAD
    inner_right = _WIDTH - _PAD - _BOX_SIDE_PAD
    inner_width = inner_right - inner_left
    y = top + _BOX_SIDE_PAD

    _bold(draw, (inner_left, y + 18), "今日猫眼票房", font_book.get(30), _TEXT)
    update = f"实时 {boxoffice.update_time} 更新" if boxoffice.update_time else "实时数据"
    draw.text((inner_right, y + 18), update, font=font_book.get(20), fill=_DIM, anchor="rm")
    y += _BOX_TITLE_HEIGHT

    # 四格概览
    cells: list[tuple[str, str]] = [
        ("大盘", boxoffice.total_box),
        ("分账", boxoffice.split_box),
        ("场次", boxoffice.show_count),
        ("人次", boxoffice.audience_count),
    ]
    cell_gap = 12
    cell_width = (inner_width - cell_gap * 3) // 4
    font_cell_label = font_book.get(20)
    font_cell_value = font_book.get(28)
    for index, (label, value) in enumerate(cells):
        left = inner_left + index * (cell_width + cell_gap)
        draw.rounded_rectangle((left, y, left + cell_width, y + _BOX_CELL_HEIGHT), radius=14, fill=_PANEL)
        draw.text((left + 16, y + 24), label, font=font_cell_label, fill=_DIM, anchor="lm")
        _bold(draw, (left + 16, y + 54), _ellipsize(draw, value or "-", font_cell_value, cell_width - 32), font_cell_value, _TEXT)
    y += _BOX_CELL_HEIGHT + 18

    # 榜单
    font_row_title = font_book.get(24)
    font_row_value = font_book.get(24)
    font_row_sub = font_book.get(18)
    font_rank = font_book.get(18)
    bar_left = inner_left + 46
    bar_width = 220
    for index, movie in enumerate(boxoffice.movies):
        row_y = y + index * _BOX_ROW_HEIGHT
        rank_color = (_ACCENT, _CYAN, _ORANGE)[movie.rank - 1] if 1 <= movie.rank <= 3 else _DIM
        draw.ellipse((inner_left, row_y + 10, inner_left + 30, row_y + 40), fill=rank_color)
        draw.text((inner_left + 15, row_y + 25), str(movie.rank), font=font_rank, fill=(255, 255, 255), anchor="mm")

        title_width = inner_right - bar_left - 210
        draw.text(
            (bar_left, row_y + 20),
            _ellipsize(draw, movie.title, font_row_title, title_width),
            font=font_row_title,
            fill=_TEXT,
            anchor="lm",
        )

        ratio = _ratio(movie.box_rate)
        if ratio > 0:
            draw.rounded_rectangle((bar_left, row_y + 44, bar_left + bar_width, row_y + 50), radius=3, fill=(54, 63, 82))
            draw.rounded_rectangle(
                (bar_left, row_y + 44, bar_left + max(6, int(bar_width * ratio)), row_y + 50),
                radius=3,
                fill=_ACCENT,
            )
            draw.text(
                (bar_left + bar_width + 12, row_y + 47),
                f"票房占比 {movie.box_rate}",
                font=font_row_sub,
                fill=_DIM,
                anchor="lm",
            )

        if movie.realtime_box:
            draw.text((inner_right, row_y + 20), movie.realtime_box, font=font_row_value, fill=_ACCENT, anchor="rm")
        if movie.total_box:
            draw.text(
                (inner_right, row_y + 46),
                f"累计 {movie.total_box}",
                font=font_row_sub,
                fill=_DIM,
                anchor="rm",
            )
        if index < len(boxoffice.movies) - 1:
            draw.line([(inner_left, row_y + _BOX_ROW_HEIGHT), (inner_right, row_y + _BOX_ROW_HEIGHT)], fill=_LINE, width=1)


def _draw_footer(draw: Any, digest: Digest, top: int, font_book: FontBook) -> None:
    """
    底部：统计区间、生成时间与来源水印。
    """
    draw.line([(_PAD, top + 6), (_WIDTH - _PAD, top + 6)], fill=_LINE, width=2)
    font = font_book.get(20)
    window = _window_text(digest)
    if window:
        draw.text((_PAD, top + 34), window, font=font, fill=_DIM, anchor="lm")
    draw.text((_PAD, top + 64), f"生成于 {datetime.now().strftime('%Y-%m-%d %H:%M')}", font=font, fill=_DIM, anchor="lm")
    draw.text((_WIDTH - _PAD, top + 34), "MoviePilot · 自动整理入库", font=font_book.get(22), fill=_MUTED, anchor="rm")
    draw.text((_WIDTH - _PAD, top + 64), "DAILY EPISODE DIGEST", font=font_book.get(18), fill=_DIM, anchor="rm")


def render_card(
    digest: Digest,
    font_book: FontBook,
    target: Path,
    max_rows: int = _MAX_ROWS,
    boxoffice: Optional[BoxOfficeData] = None,
) -> Optional[Path]:
    """
    绘制日报卡片并保存为 JPEG。

    :param digest: 聚合后的摘要
    :param font_book: 已解析的字体
    :param target: 输出文件路径
    :param max_rows: 卡片内最多列出几部，超出部分在底部汇总提示
    :param boxoffice: 可选的猫眼实时票房数据
    :return: 成功时返回输出路径，否则返回 None
    """
    try:
        from PIL import Image, ImageDraw
    except Exception as error:  # noqa: BLE001 - 宿主缺少 Pillow 时无法出图
        logger.warning(f"每日入库速报：Pillow 不可用，无法生成图片：{error}")
        return None

    try:
        # 预热常用字号，字体文件损坏时在此处就降级，避免绘制到一半才失败
        for size in (18, 20, 22, 24, 28, 30, 36, 40, 46, 50, 66):
            font_book.get(size)
    except Exception as error:  # noqa: BLE001 - 字体文件损坏时降级
        logger.error(f"每日入库速报：字体加载失败，无法生成图片：{error}")
        return None

    rows = list(digest.groups[:max_rows])
    hidden = (digest.total_shows - len(rows)) + digest.dropped
    sections, height = _layout(rows, hidden, boxoffice)

    canvas = Image.new("RGB", (_WIDTH, height), _BG_TOP)
    _draw_gradient(canvas, height)
    draw = ImageDraw.Draw(canvas)

    for name, top, section_height in sections:
        if name == "topbar":
            _draw_topbar(draw)
        elif name == "header":
            _draw_header(draw, digest, top, font_book)
        elif name == "stats":
            _draw_stats(draw, digest, top, font_book)
        elif name == "listhead":
            _draw_listhead(draw, digest, top, font_book)
        elif name == "rows":
            for index, group in enumerate(rows):
                _draw_row(draw, canvas, group, index + 1, top + index * (_ROW_HEIGHT + _ROW_GAP), font_book)
        elif name == "hidden":
            _draw_hidden(draw, hidden, top, font_book)
        elif name == "boxoffice" and boxoffice:
            _draw_boxoffice(draw, boxoffice, top, section_height, font_book)
        elif name == "footer":
            _draw_footer(draw, digest, top, font_book)

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        canvas.save(target, format="JPEG", quality=92, optimize=True)
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
