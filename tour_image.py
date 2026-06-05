"""
Render tournament tour (тур/gameweek) fixtures as a PNG image.

Public entry points:
  render_tour_png(tid, tour_nums) -> bytes
"""
from __future__ import annotations

from io import BytesIO

from PIL import Image, ImageDraw, ImageFont

from bg_helper import make_canvas
from database import get_tournament, get_matches_by_tour, get_all_tour_nums


# ── Palette (matches standings_image.py dark theme) ───────────────────────
BG         = (28, 30, 38)
CARD_BG    = (40, 44, 54)
HEADER_BG  = (54, 88, 144)
HEADER_TXT = (255, 255, 255)
ROW        = (44, 48, 60)
ROW_ALT    = (50, 54, 66)
TEXT       = (235, 238, 245)
MUTED      = (170, 180, 195)
BORDER     = (60, 65, 78)
ACCENT     = (90, 200, 130)
SCORE_CLR  = (255, 255, 255)
PENDING    = (150, 158, 175)
TOUR_HDR   = (38, 60, 110)

SCALE = 2


def _s(v: int) -> int:
    return int(v * SCALE)


# ── Font helpers ─────────────────────────────────────────────────────────
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


# ── Layout constants ──────────────────────────────────────────────────────
PAD        = _s(20)
ROW_H      = _s(42)
HEADER_H   = _s(56)
TOUR_HDR_H = _s(38)
GAP        = _s(12)

# Column layout: (label, width_px, align)
_COLS_RAW = [
    ("home",  240, "left"),
    ("score",  80, "center"),
    ("away",  240, "right"),
]
COLS = [(lbl, _s(w), al) for lbl, w, al in _COLS_RAW]
TABLE_W = sum(w for _, w, _ in COLS)


def _player_name(match: dict, side: str) -> str:
    """Return a display name for player1 (side='1') or player2 (side='2')."""
    nick = (match.get(f"p{side}_nickname") or "").strip()
    user = (match.get(f"p{side}_username") or "").strip()
    import re
    is_synthetic = bool(user) and bool(re.match(r"^id_\d+$", user.lower()))
    if is_synthetic:
        return nick or user.lower().replace("id_", "id ", 1)
    if nick and user:
        if nick.lower() == user.lower():
            return f"@{user}"
        return nick
    return f"@{user}" if user else nick or "?"


def _score_str(match: dict) -> str:
    """Return '3:1', 'пен 3:1 (4:3)', or '—:—' for pending."""
    if match.get("status") != "confirmed":
        return "—:—"
    s1 = match.get("score1")
    s2 = match.get("score2")
    if s1 is None or s2 is None:
        return "—:—"
    base = f"{s1}:{s2}"
    pen1 = match.get("pen1")
    pen2 = match.get("pen2")
    if pen1 is not None and pen2 is not None:
        return f"{base} ({pen1}:{pen2})"
    return base


def _draw_text_centered(
    draw: ImageDraw.ImageDraw,
    text: str,
    x: int, y: int, w: int, h: int,
    font: ImageFont.ImageFont,
    color: tuple,
) -> None:
    bbox = draw.textbbox((0, 0), text, font=font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    tx = x + (w - tw) // 2
    ty = y + (h - th) // 2 - bbox[1]
    draw.text((tx, ty), text, font=font, fill=color)


def _draw_text_left(
    draw: ImageDraw.ImageDraw,
    text: str,
    x: int, y: int, h: int,
    font: ImageFont.ImageFont,
    color: tuple,
    max_w: int,
) -> None:
    # Truncate if needed
    while text:
        bbox = draw.textbbox((0, 0), text, font=font)
        tw = bbox[2] - bbox[0]
        if tw <= max_w:
            break
        text = text[:-2] + "…"
    bbox = draw.textbbox((0, 0), text, font=font)
    th = bbox[3] - bbox[1]
    ty = y + (h - th) // 2 - bbox[1]
    draw.text((x + _s(8), ty), text, font=font, fill=color)


def _draw_text_right(
    draw: ImageDraw.ImageDraw,
    text: str,
    x: int, y: int, w: int, h: int,
    font: ImageFont.ImageFont,
    color: tuple,
    max_w: int,
) -> None:
    # Truncate if needed
    while text:
        bbox = draw.textbbox((0, 0), text, font=font)
        tw = bbox[2] - bbox[0]
        if tw <= max_w:
            break
        text = text[2:]
        if not text:
            break
        text = "…" + text.lstrip("…")
    bbox = draw.textbbox((0, 0), text, font=font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    tx = x + w - tw - _s(8)
    ty = y + (h - th) // 2 - bbox[1]
    draw.text((tx, ty), text, font=font, fill=color)


def _draw_rect_alpha(
    img: Image.Image,
    box: tuple,
    fill: tuple[int, int, int],
    alpha: int = 255,
) -> None:
    if alpha >= 255:
        ImageDraw.Draw(img).rectangle(box, fill=fill)
        return
    if alpha <= 0:
        return
    x0, y0, x1, y1 = int(box[0]), int(box[1]), int(box[2]), int(box[3])
    w, h = x1 - x0, y1 - y0
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


def _render(
    t: dict,
    tours_data: list[tuple[int, list[dict]]],
    title: str,
) -> bytes:
    """
    Core renderer. ``tours_data`` is a list of (tour_num, matches) pairs.
    """
    font_title  = _font(22, bold=True)
    font_header = _font(16, bold=True)
    font_tour   = _font(15, bold=True)
    font_row    = _font(14)
    font_score  = _font(15, bold=True)

    # Calculate total height
    height = PAD + HEADER_H + GAP
    for _tn, matches in tours_data:
        height += TOUR_HDR_H
        height += ROW_H * max(len(matches), 1)
        height += GAP
    height += PAD

    width = TABLE_W + PAD * 2

    overlay_alpha = int(t.get("bg_overlay_alpha") or 165)
    img = make_canvas(
        width, height,
        bg_color=BG,
        bg_image_path=t.get("bg_image_path"),
        bg_image_data=t.get("bg_image_data"),
        overlay_alpha=overlay_alpha,
    )
    draw = ImageDraw.Draw(img)

    y = PAD

    # ── Main header ──────────────────────────────────────────────────────
    _draw_rect_alpha(img, (PAD, y, PAD + TABLE_W, y + HEADER_H), HEADER_BG)
    t_name = t.get("name") or "Лига"
    # Tournament name (smaller, top)
    _draw_text_centered(
        draw, t_name,
        PAD, y, TABLE_W, HEADER_H // 2,
        _font(14), MUTED,
    )
    # Title (bigger, bottom half)
    _draw_text_centered(
        draw, title,
        PAD, y + HEADER_H // 2, TABLE_W, HEADER_H // 2,
        font_title, HEADER_TXT,
    )
    y += HEADER_H + GAP

    # ── Rows per tour ───────────────────────────────────────────────────
    for tour_num, matches in tours_data:
        # Tour sub-header
        _draw_rect_alpha(img, (PAD, y, PAD + TABLE_W, y + TOUR_HDR_H), TOUR_HDR)
        _draw_text_centered(
            draw, f"🗓 ТУР {tour_num}",
            PAD, y, TABLE_W, TOUR_HDR_H,
            font_tour, HEADER_TXT,
        )
        y += TOUR_HDR_H

        if not matches:
            _draw_rect_alpha(img, (PAD, y, PAD + TABLE_W, y + ROW_H), ROW)
            _draw_text_centered(
                draw, "— нет матчей —",
                PAD, y, TABLE_W, ROW_H,
                font_row, MUTED,
            )
            y += ROW_H
        else:
            col_home_w, col_score_w, col_away_w = [w for _, w, _ in COLS]
            col_home_x  = PAD
            col_score_x = col_home_x + col_home_w
            col_away_x  = col_score_x + col_score_w

            for idx, m in enumerate(matches):
                row_bg = ROW if idx % 2 == 0 else ROW_ALT
                _draw_rect_alpha(img, (PAD, y, PAD + TABLE_W, y + ROW_H), row_bg)

                home_name = _player_name(m, "1")
                away_name = _player_name(m, "2")
                score     = _score_str(m)
                score_clr = SCORE_CLR if m.get("status") == "confirmed" else PENDING

                _draw_text_left(
                    draw, home_name,
                    col_home_x, y, ROW_H, font_row, TEXT, col_home_w - _s(8),
                )
                _draw_text_centered(
                    draw, score,
                    col_score_x, y, col_score_w, ROW_H, font_score, score_clr,
                )
                _draw_text_right(
                    draw, away_name,
                    col_away_x, y, col_away_w, ROW_H, font_row, TEXT, col_away_w - _s(8),
                )

                # Thin border between rows
                draw.line(
                    [(PAD, y + ROW_H - 1), (PAD + TABLE_W, y + ROW_H - 1)],
                    fill=BORDER, width=1,
                )
                y += ROW_H

        y += GAP

    buf = BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


# ── Public API ───────────────────────────────────────────────────────────────

def render_tour_png(tid: int, tour_nums: list[int]) -> bytes:
    """Render one or more tours as a single PNG.

    Args:
        tid:       Tournament ID.
        tour_nums: Sorted list of tour numbers to render (e.g. [1] or [1,2,3]).

    Returns:
        PNG bytes.
    """
    t = get_tournament(tid) or {}

    if len(tour_nums) == 1:
        title = f"ТУР {tour_nums[0]}"
    elif len(tour_nums) == 2 and tour_nums[1] == tour_nums[0] + 1:
        title = f"ТУРЫ {tour_nums[0]}–{tour_nums[-1]}"
    else:
        title = f"ТУРЫ {tour_nums[0]}–{tour_nums[-1]}"

    tours_data: list[tuple[int, list[dict]]] = []
    for tn in tour_nums:
        matches = get_matches_by_tour(tid, tn)
        tours_data.append((tn, matches))

    return _render(t, tours_data, title)
