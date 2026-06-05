"""
Render tournament tour (round) matches as a PNG image so /tours can reply
with a picture showing one or several rounds at a glance.

Public entry point:

* ``render_tour_png(tid, tour_numbers) -> bytes``
"""

from __future__ import annotations

from io import BytesIO
from typing import Iterable

from PIL import Image, ImageDraw, ImageFont

from bg_helper import make_canvas
from database import get_player_by_id, get_tournament, get_tour_matches


# ── palette (matches standings_image.py) ──────────────────────────────────────
BG          = (28, 30, 38)
CARD_BG     = (40, 44, 54)
HEADER_BG   = (54, 88, 144)
HEADER_TXT  = (255, 255, 255)
ROW         = (44, 48, 60)
ROW_ALT     = (50, 54, 66)
TEXT        = (235, 238, 245)
MUTED       = (170, 180, 195)
BORDER      = (60, 65, 78)
ACCENT      = (90, 200, 130)

SCALE = 2


def _s(v: int) -> int:
    return int(v * SCALE)


_COLS_RAW: list[tuple[str, int, str]] = [
    ("№",     _s(50),  "center"),
    ("Хозяин", _s(280), "left"),
    ("Счёт",   _s(90),  "center"),
    ("Гость",  _s(280), "left"),
    ("Статус", _s(100), "center"),
]

PAD       = _s(24)
ROW_H     = _s(44)
HEADER_H  = _s(50)
TOUR_H    = _s(56)
GAP       = _s(18)
TITLE_BLK = _s(110)

_COL_TOTAL = sum(w for _, w, _ in _COLS_RAW)


# ── font cache ────────────────────────────────────────────────────────────────
_FONT_CACHE: dict[tuple[int, bool], ImageFont.ImageFont] = {}

_BOLD_PATHS = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
)
_REG_PATHS = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
)


def _font(size: int, bold: bool = False) -> ImageFont.ImageFont:
    key = (size, bold)
    cached = _FONT_CACHE.get(key)
    if cached is not None:
        return cached
    for path in (_BOLD_PATHS if bold else _REG_PATHS):
        try:
            f = ImageFont.truetype(path, size)
            _FONT_CACHE[key] = f
            return f
        except (OSError, IOError):
            continue
    f = ImageFont.load_default()
    _FONT_CACHE[key] = f
    return f


def _draw_rect_alpha(
    img: Image.Image,
    box: list | tuple,
    fill: tuple[int, int, int],
    alpha: int = 255,
) -> None:
    if alpha >= 255:
        ImageDraw.Draw(img).rectangle(box, fill=fill)
        return
    if alpha <= 0:
        return
    x0, y0, x1, y1 = int(box[0]), int(box[1]), int(box[2]), int(box[3])
    w = x1 - x0
    h = y1 - y0
    if w <= 0 or h <= 0:
        return
    overlay = Image.new("RGBA", (w, h), fill + (alpha,))
    img.paste(
        Image.alpha_composite(
            img.crop((x0, y0, x1, y1)).convert("RGBA"),
            overlay,
        ).convert("RGB"),
        (x0, y0),
    )


def _draw_text_in_cell(
    draw: ImageDraw.ImageDraw,
    text: str,
    x: int, y: int, w: int, h: int,
    font: ImageFont.ImageFont,
    color: tuple,
    *,
    align: str = "left",
    pad: int | None = None,
) -> None:
    if pad is None:
        pad = _s(12)
    from emoji_helper import measure_text_with_emoji
    tw = measure_text_with_emoji(text, font)
    bbox = draw.textbbox((0, 0), "Hg", font=font)
    th = bbox[3] - bbox[1]
    if align == "right":
        tx = x + w - pad - tw
    elif align == "center":
        tx = x + (w - tw) // 2
    else:
        tx = x + pad
    ty = y + (h - th) // 2 - bbox[1]
    draw.text((tx, ty), text, font=font, fill=color)


def _status_icon(status: str) -> str:
    return {
        "pending": "⏳",
        "reported": "🟡",
        "awaiting_admin": "🟡",
        "confirmed": "✅",
    }.get(status, "⏳")


# ── public API ────────────────────────────────────────────────────────────────
def render_tour_png(tid: int, tour_numbers: Iterable[int]) -> bytes:
    """Render one or more tours as a PNG image.

    Each tour becomes a separate block with a title bar, column headers,
    and match rows. Status is shown with icons: ⏳ pending, 🟡 reported,
    ✅ confirmed.
    """
    t = get_tournament(tid) or {}
    name = (t.get("name") or "Турнир").strip()

    title_font = _font(_s(34), bold=True)
    sub_font   = _font(_s(20), bold=False)
    tour_font  = _font(_s(26), bold=True)
    head_font  = _font(_s(20), bold=True)
    row_font   = _font(_s(22), bold=False)
    name_font  = _font(_s(18), bold=False)

    w = PAD * 2 + _COL_TOTAL

    # First pass: collect match data and compute height
    blocks: list[tuple[int, list[dict]]] = []
    for tn in tour_numbers:
        matches = get_tour_matches(tid, tn)
        if matches:
            blocks.append((tn, matches))

    if not blocks:
        # Single block with "no matches"
        blocks.append((next(iter(tour_numbers), 0), []))

    height = TITLE_BLK
    for tn, matches in blocks:
        height += TOUR_H + HEADER_H
        height += ROW_H * max(1, len(matches))
        height += GAP

    img = make_canvas(
        w, height,
        bg_color=BG,
        bg_image_path=t.get("bg_image_path"),
        bg_image_data=t.get("bg_image_data"),
        overlay_alpha=int(t.get("bg_overlay_alpha") or 165),
    )
    draw = ImageDraw.Draw(img)
    _row_alpha = int(t.get("row_bg_alpha") or 255)

    # Title
    t_type = (t.get("tournament_type") or "").upper()
    rt = ("лига" if int(t.get("groups_only") or 0) else "группы")
    sub = "  ·  ".join(filter(None, [t_type, rt, "Туры"]))
    draw.text((PAD, PAD), name, font=title_font, fill=TEXT)
    draw.text((PAD, PAD + _s(50)), sub, font=sub_font, fill=MUTED)

    y = TITLE_BLK

    for tn, matches in blocks:
        x0 = PAD
        xw = _COL_TOTAL

        # Tour title bar
        title = f"📅 Тур {tn}" if len(blocks) == 1 else f"📅 Тур {tn}"
        _draw_rect_alpha(img, [x0, y, x0 + xw, y + TOUR_H], CARD_BG, _row_alpha)
        draw.text((x0 + _s(16), y + _s(14)), title, font=tour_font, fill=TEXT)
        if matches:
            hint = f"{len(matches)} матчей"
            bbox = draw.textbbox((0, 0), hint, font=sub_font)
            hx = x0 + xw - _s(16) - (bbox[2] - bbox[0])
            draw.text((hx, y + _s(18)), hint, font=sub_font, fill=MUTED)
        y += TOUR_H

        # Column headers
        _draw_rect_alpha(img, [x0, y, x0 + xw, y + HEADER_H], HEADER_BG, _row_alpha)
        cx = x0
        for label, col_w, align in _COLS_RAW:
            _draw_text_in_cell(draw, label, cx, y, col_w, HEADER_H, head_font, HEADER_TXT, align=align)
            cx += col_w
        y += HEADER_H

        if not matches:
            _draw_rect_alpha(img, [x0, y, x0 + xw, y + ROW_H], ROW_ALT, _row_alpha)
            draw.text((x0 + _s(16), y + _s(12)), "Нет матчей в этом туре", font=row_font, fill=MUTED)
            y += ROW_H
            y += GAP
            continue

        for pos, m in enumerate(matches, 1):
            row_bg = ROW_ALT if pos % 2 == 0 else ROW
            _draw_rect_alpha(img, [x0, y, x0 + xw, y + ROW_H], row_bg, _row_alpha)
            draw.line(
                [x0, y + ROW_H - 1, x0 + xw, y + ROW_H - 1],
                fill=BORDER,
            )

            p1 = get_player_by_id(m["player1_id"])
            p2 = get_player_by_id(m["player2_id"])
            n1 = (p1 or {}).get("username", "?")
            n2 = (p2 or {}).get("username", "?")

            s1 = m.get("score1")
            s2 = m.get("score2")
            is_confirmed = m.get("status") == "confirmed"
            score = f"{s1}:{s2}" if (is_confirmed and s1 is not None and s2 is not None) else "–:–"

            icon = _status_icon(m.get("status", ""))

            vals = [str(pos), n1, score, n2, icon]
            cx = x0
            for (label, col_w, align), val in zip(_COLS_RAW, vals):
                col_font = name_font if label in ("Хозяин", "Гость") else row_font
                col_color = TEXT
                if label == "Счёт" and is_confirmed:
                    col_color = ACCENT
                _draw_text_in_cell(
                    draw, val, cx, y, col_w, ROW_H,
                    col_font, col_color, align=align,
                )
                cx += col_w
            y += ROW_H

        y += GAP

    buf = BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


__all__ = ["render_tour_png"]
