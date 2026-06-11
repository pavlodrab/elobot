"""
Render tournament tour (round) matches as a PNG image so /tours can reply
with a picture showing one or several rounds at a glance.

Public entry point:

* ``render_tour_png(tid, tour_numbers) -> bytes``
"""

from __future__ import annotations

import re
from io import BytesIO
from typing import Iterable

from PIL import Image, ImageDraw, ImageFont

from bg_helper import make_canvas
from database import (
    get_player_by_id,
    get_tournament,
    get_tournament_players,
    get_tour_matches,
)


# ── Per-render team-tag and name-mode caches ────────────────────────────────
# Mirror the same approach used in ``playoff_image.py``: populated by
# ``render_tour_png`` from ``tournaments.name_display_mode`` /
# ``tournament_players.team_tag`` and consumed by ``_display_name`` so
# we don't have to thread either through every cell-render call.
# Single-threaded asyncio worker → no race conditions in practice.
_TAG_BY_PID: dict[int, str] = {}
_NAME_MODE: str = "full"


def _load_tag_map(tid: int) -> None:
    """Refresh ``_TAG_BY_PID`` with the per-tournament team tags."""
    _TAG_BY_PID.clear()
    try:
        rows = get_tournament_players(tid)
    except Exception:
        return
    for r in rows:
        pid = r.get("player_id")
        tag = (r.get("team_tag") or "").strip()
        if isinstance(pid, int) and tag:
            _TAG_BY_PID[pid] = tag


def _load_name_mode(t: dict | None) -> None:
    """Refresh module-level ``_NAME_MODE`` from the tournament row."""
    global _NAME_MODE
    raw = ((t or {}).get("name_display_mode") or "full")
    mode = str(raw).strip().lower()
    if mode not in ("full", "tag", "nick"):
        mode = "full"
    _NAME_MODE = mode


def _display_name(p: dict | None, *, fallback: str = "?") -> str:
    """Pick the right participant label for the active ``_NAME_MODE``.

    Mirrors the logic in ``playoff_image._display_name`` so /tours and
    /playoff stay visually consistent — including the synthetic
    ``id_<digits>`` placeholder handling and the per-tournament team
    tag fallback.
    """
    if not p:
        return fallback
    pid_local = p.get("id")
    tag = ""
    if isinstance(pid_local, int):
        tag = (_TAG_BY_PID.get(pid_local) or "").strip()
    nick = (p.get("game_nickname") or "").strip()
    user = (p.get("username") or "").strip()
    is_synthetic = bool(user) and bool(re.match(r"^id_\d+$", user.lower()))
    pretty_user = "" if is_synthetic else user
    synth_label = (
        user.lower().replace("id_", "id ", 1) if is_synthetic else ""
    )

    mode = _NAME_MODE
    if mode == "tag":
        if pretty_user:
            return f"@{pretty_user}"
        if nick:
            return nick
        if tag:
            return tag
        if synth_label:
            return synth_label
        return fallback
    if mode == "nick":
        if nick and tag:
            return f"{nick} - {tag}"
        if nick:
            return nick
        if tag:
            return tag
        if pretty_user:
            return f"@{pretty_user}"
        if synth_label:
            return synth_label
        return fallback

    # mode == "full" — historic behaviour, preserved verbatim.
    if is_synthetic:
        synth = nick or synth_label
        return f"{synth} - {tag}" if tag else synth
    if user and nick:
        if tag:
            return f"{nick} - {tag} (@{user})"
        if nick.lower() == user.lower():
            return f"@{user}"
        return f"{nick} (@{user})"
    if user:
        if tag:
            return f"{tag} (@{user})"
        return f"@{user}"
    return f"{nick} - {tag}" if (nick and tag) else (nick or tag or fallback)


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
    base_img: "Image.Image | None" = None,
) -> None:
    if pad is None:
        pad = _s(12)
    from emoji_helper import draw_text_with_emoji, measure_text_with_emoji
    # Always measure with the emoji-aware helper so cells containing
    # 📅 / ✅ don't overflow because a regular-font measurement
    # under-counted the wider glyph.
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
    if base_img is not None and any(
        ord(ch) > 0x2000 for ch in text  # cheap prefilter for emoji
    ):
        draw_text_with_emoji(base_img, (tx, ty), text, font, fill=color)
    else:
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

    # Refresh caches once per render so every nested ``_display_name``
    # call resolves the right tag / mode without us threading them
    # through ``_draw_text_in_cell``.
    _load_tag_map(tid)
    _load_name_mode(t)

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
    from emoji_helper import draw_text_with_emoji
    # Title (name) — draw with emoji support in case the admin put
    # an emoji in the tournament name.
    if any(ord(c) > 0x2000 for c in name):
        draw_text_with_emoji(img, (PAD, PAD), name, title_font, fill=TEXT)
    else:
        draw.text((PAD, PAD), name, font=title_font, fill=TEXT)
    draw.text((PAD, PAD + _s(50)), sub, font=sub_font, fill=MUTED)

    y = TITLE_BLK

    for tn, matches in blocks:
        x0 = PAD
        xw = _COL_TOTAL

        # Tour title bar
        title = f"📅 Тур {tn}"
        _draw_rect_alpha(img, [x0, y, x0 + xw, y + TOUR_H], CARD_BG, _row_alpha)
        # Tour title contains a calendar emoji — render through
        # draw_text_with_emoji so it doesn't fall back to tofu.
        draw_text_with_emoji(img, (x0 + _s(16), y + _s(14)), title, tour_font, fill=TEXT)
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
            _draw_text_in_cell(
                draw, label, cx, y, col_w, HEADER_H, head_font, HEADER_TXT,
                align=align, base_img=img,
            )
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
            # Honour the per-tournament name-display mode (full /
            # @tag / nick) and trim long ``"<nick> - <Team> (@user)"``
            # labels with an ellipsis so they don't bleed into the
            # neighbouring "Счёт" column. Inner padding here matches
            # ``_draw_text_in_cell``'s default (``_s(12)`` on each
            # side, hence ``_s(24)`` total).
            from emoji_helper import truncate_text_with_emoji
            host_w  = _COLS_RAW[1][1] - _s(24)
            guest_w = _COLS_RAW[3][1] - _s(24)
            n1 = truncate_text_with_emoji(_display_name(p1), name_font, host_w)
            n2 = truncate_text_with_emoji(_display_name(p2), name_font, guest_w)

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
                    base_img=img,
                )
                cx += col_w
            y += ROW_H

        y += GAP

    buf = BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


__all__ = ["render_tour_png"]
