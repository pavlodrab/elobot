"""
Render the top-scorers ("bombardiry") table as a PNG image for /tablebomb.

Displays the top-7 **footballers** (in-game names, not real players) with:
* Gold border (#1), Silver border (#2), Bronze border (#3)
* A footballer silhouette PNG on the right side (replaceable asset)
* Dark theme consistent with standings_image.py / playoff_image.py
* Uses the same tournament background as /table and /playoff

Public entry point: ``render_tablebomb_png(rows, tournament) -> bytes``
"""
from __future__ import annotations

import os as _os
import re
from io import BytesIO

from PIL import Image, ImageDraw, ImageFont

from bg_helper import make_canvas


# ── palette (matches standings/playoff dark theme) ──────────────────────────
BG          = (28, 30, 38)
CARD_BG     = (40, 44, 54)
HEADER_BG   = (54, 88, 144)
HEADER_TXT  = (255, 255, 255)
TEXT        = (235, 238, 245)
MUTED       = (170, 180, 195)
BORDER      = (60, 65, 78)

# Medal border colors
GOLD        = (255, 215, 0)
SILVER      = (192, 192, 192)
BRONZE      = (205, 127, 50)

# Row backgrounds
ROW_BG      = (44, 48, 60)
ROW_ALT     = (50, 54, 66)

SCALE = 2

# Path to the built-in footballer silhouette PNG (transparent background).
# Users can replace this file with their own PNG to customise the look.
_PLAYER_ASSET_PATH = _os.path.join(
    _os.path.dirname(_os.path.abspath(__file__)), "assets", "tablebomb_player.png"
)


def _s(v: int) -> int:
    return int(v * SCALE)


# ── font loading ────────────────────────────────────────────────────────────
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


# ── helpers ─────────────────────────────────────────────────────────────────
def _truncate(
    text: str,
    font: ImageFont.ImageFont,
    max_w: int,
    draw: ImageDraw.ImageDraw,
) -> str:
    """Trim ``text`` with an ellipsis to fit ``max_w`` pixels.

    Emoji-aware via :mod:`emoji_helper` so we don't slice through a
    flag glyph or miscount the rendered width when names contain emoji.
    """
    from emoji_helper import truncate_text_with_emoji
    return truncate_text_with_emoji(text, font, max_w, suffix="...")


def _paste_player_asset(img: Image.Image, x: int, y: int, h: int) -> None:
    """Paste the footballer silhouette PNG onto the image.

    The asset is loaded from ``assets/tablebomb_player.png`` (a transparent
    PNG with lightning effects). It's resized to fit ``h`` pixels tall and
    centered horizontally at ``x``.

    If the asset file is missing or unreadable, silently skips.
    """
    try:
        asset = Image.open(_PLAYER_ASSET_PATH).convert("RGBA")
    except (OSError, IOError):
        return

    aw, ah = asset.size
    if ah <= 0:
        return
    scale = h / ah
    new_w = int(aw * scale)
    new_h = h
    asset = asset.resize((new_w, new_h), Image.LANCZOS)

    paste_x = x - new_w // 2
    paste_y = y

    # Alpha-composite onto the RGB canvas
    region = img.crop((paste_x, paste_y, paste_x + new_w, paste_y + new_h))
    region_rgba = region.convert("RGBA")
    composited = Image.alpha_composite(region_rgba, asset)
    img.paste(composited.convert("RGB"), (paste_x, paste_y))


def _draw_medal_card(
    img: Image.Image,
    draw: ImageDraw.ImageDraw,
    x: int, y: int, w: int, h: int,
    position: int,
    row_data: dict,
    *,
    name_font: ImageFont.ImageFont,
    goals_font: ImageFont.ImageFont,
    detail_font: ImageFont.ImageFont,
    row_alpha: int = 255,
    name_mode: str = "full",
) -> None:
    """Draw a single scorer card with optional medal border."""
    border_color = BORDER
    border_width = _s(1)
    if position == 1:
        border_color = GOLD
        border_width = _s(3)
    elif position == 2:
        border_color = SILVER
        border_width = _s(3)
    elif position == 3:
        border_color = BRONZE
        border_width = _s(3)

    radius = _s(12)
    card_fill = ROW_ALT if position % 2 == 0 else ROW_BG

    _draw_rounded_rect(img, draw, x, y, w, h,
                       radius=radius, fill=card_fill,
                       outline=border_color, width=border_width,
                       alpha=row_alpha)

    pad = _s(16)
    inner_w = w - pad * 2
    pos_label = str(position)

    # Position circle
    circle_r = _s(18)
    circle_cx = x + pad + circle_r
    circle_cy = y + h // 2
    circle_color = GOLD if position == 1 else (
        SILVER if position == 2 else (BRONZE if position == 3 else MUTED))
    draw.ellipse(
        [circle_cx - circle_r, circle_cy - circle_r,
         circle_cx + circle_r, circle_cy + circle_r],
        fill=circle_color if position <= 3 else CARD_BG,
        outline=circle_color, width=_s(2))
    pos_font = _font(_s(18), bold=True)
    bbox = draw.textbbox((0, 0), pos_label, font=pos_font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    pos_text_color = (30, 30, 30) if position <= 3 else TEXT
    draw.text((circle_cx - tw // 2, circle_cy - th // 2 - bbox[1]),
              pos_label, font=pos_font, fill=pos_text_color)

    # Footballer name
    name_x = circle_cx + circle_r + _s(14)
    raw_name = (row_data.get("raw_name") or "").strip()
    if not raw_name:
        nick = (row_data.get("game_nickname") or "").strip()
        user = (row_data.get("username") or "").strip()
        raw_name = nick or user or "?"
    raw_name = _truncate(raw_name, name_font, inner_w - (name_x - x - pad) - _s(100), draw)
    _em_base = getattr(draw, "_em_base", None)
    if _em_base is not None:
        from emoji_helper import draw_text_with_emoji
        draw_text_with_emoji(
            _em_base, (name_x, y + _s(10)),
            raw_name, name_font, fill=TEXT,
        )
    else:
        draw.text((name_x, y + _s(10)), raw_name, font=name_font, fill=TEXT)

    # Owner (the league player who scored). Honours the per-tournament
    # ``name_display_mode``: ``"tag"`` shows ``@user`` only,
    # ``"nick"`` shows the in-game nickname only (or the team_tag if
    # the row carries one — the GROUP BY in ``get_top_scorers_*``
    # leaves it on ``row_data``), and ``"full"`` keeps the legacy
    # ``@user``-or-nickname fallback.
    owner = (row_data.get("username") or "").strip()
    nick = (row_data.get("game_nickname") or "").strip()
    team = (row_data.get("team_tag") or "").strip()
    mode = (name_mode or "full").lower()
    if mode == "nick":
        owner_label = nick or team or (f"@{owner}" if owner else "")
    elif mode == "tag":
        owner_label = f"@{owner}" if owner else (nick or team)
    else:
        owner_label = f"@{owner}" if owner else nick
    if owner_label:
        owner_label = _truncate(owner_label, detail_font,
                                inner_w - (name_x - x - pad) - _s(100), draw)
        draw.text((name_x, y + h - _s(10) - _s(16)), owner_label,
                  font=detail_font, fill=MUTED)

    # Goals count
    goals_text = str(row_data.get("total_goals", 0))
    gw = draw.textlength(goals_text, font=goals_font)
    goals_x = x + w - pad - int(gw) - _s(30)
    goals_y = y + (h - _s(30)) // 2
    draw.text((goals_x, goals_y), goals_text, font=goals_font, fill=HEADER_TXT)
    gol_font = _font(_s(14), bold=False)
    draw.text((goals_x + int(gw) + _s(6), goals_y + _s(8)),
              "гол.", font=gol_font, fill=MUTED)


def _draw_rounded_rect(
    img: Image.Image, draw: ImageDraw.ImageDraw,
    x: int, y: int, w: int, h: int, *,
    radius: int, fill: tuple, outline: tuple | None = None,
    width: int = 1, alpha: int = 255,
) -> None:
    if alpha >= 255:
        draw.rounded_rectangle([x, y, x + w, y + h], radius=radius,
                               fill=fill, outline=outline, width=width)
        return
    if alpha <= 0:
        if outline and width > 0:
            draw.rounded_rectangle([x, y, x + w, y + h], radius=radius,
                                   fill=None, outline=outline, width=width)
        return
    overlay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ImageDraw.Draw(overlay).rounded_rectangle(
        [0, 0, w, h], radius=radius, fill=fill + (alpha,))
    img.paste(
        Image.alpha_composite(
            img.crop((x, y, x + w, y + h)).convert("RGBA"), overlay
        ).convert("RGB"), (x, y))
    if outline and width > 0:
        draw.rounded_rectangle([x, y, x + w, y + h], radius=radius,
                               fill=None, outline=outline, width=width)


# ── public API ──────────────────────────────────────────────────────────────
def render_tablebomb_png(
    rows: list[dict],
    tournament: dict | None = None,
) -> bytes:
    """Render the top-7 footballers (by goals) as a PNG image.

    Returns PNG image bytes ready for Bot.send_photo.
    """
    t = tournament or {}
    top = rows[:7]

    if not top:
        img = Image.new("RGB", (_s(400), _s(100)), BG)
        draw = ImageDraw.Draw(img)
        draw.text((_s(20), _s(30)), "Нет голов", font=_font(_s(20)), fill=MUTED)
        buf = BytesIO()
        img.save(buf, format="PNG", optimize=True)
        return buf.getvalue()

    title_font = _font(_s(32), bold=True)
    sub_font = _font(_s(18), bold=False)
    name_font = _font(_s(20), bold=True)
    goals_font = _font(_s(26), bold=True)
    detail_font = _font(_s(14), bold=False)

    pad = _s(28)
    title_h = _s(90)
    card_h = _s(72)
    card_gap = _s(12)
    silhouette_w = _s(160)

    cards_w = _s(440)
    total_w = pad + cards_w + _s(20) + silhouette_w + pad
    total_h = title_h + len(top) * (card_h + card_gap) + pad

    img = make_canvas(
        total_w, total_h, bg_color=BG,
        bg_image_path=t.get("bg_image_path"),
        bg_image_data=t.get("bg_image_data"),
        overlay_alpha=int(t.get("bg_overlay_alpha") or 180))
    draw = ImageDraw.Draw(img)
    setattr(draw, "_em_base", img)
    row_alpha = int(t.get("row_bg_alpha") or 230)

    # Title
    t_name = (t.get("name") or "").strip()
    draw.text((pad, _s(16)), _truncate("Бомбардиры", title_font, total_w - pad * 2, draw),
              font=title_font, fill=TEXT)
    sub_parts = []
    if t_name:
        sub_parts.append(t_name)
    t_type = (t.get("tournament_type") or "").upper()
    if t_type:
        sub_parts.append(t_type)
    sub_parts.append("Топ-7 по голам")
    sub_text = _truncate("  |  ".join(sub_parts), sub_font, total_w - pad * 2, draw)
    draw.text((pad, _s(54)), sub_text, font=sub_font, fill=MUTED)

    # Player asset on the right (behind cards layer for transparency)
    silhouette_x = pad + cards_w + _s(20) + silhouette_w // 2
    silhouette_y = title_h
    silhouette_h = total_h - title_h - pad
    _paste_player_asset(img, silhouette_x, silhouette_y, silhouette_h)

    # Re-create draw after asset paste
    draw = ImageDraw.Draw(img)
    setattr(draw, "_em_base", img)

    # Scorer cards
    y = title_h
    name_mode = (t.get("name_display_mode") or "full").lower()
    for i, row_data in enumerate(top, 1):
        _draw_medal_card(img, draw, x=pad, y=y, w=cards_w, h=card_h,
                         position=i, row_data=row_data,
                         name_font=name_font, goals_font=goals_font,
                         detail_font=detail_font, row_alpha=row_alpha,
                         name_mode=name_mode)
        y += card_h + card_gap

    buf = BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


__all__ = ["render_tablebomb_png"]
