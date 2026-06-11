"""
Render the tournament standings table as a PNG image so /table can reply
with a picture instead of a monospaced text block (which Telegram clients
render inconsistently across mobile / desktop / dark-mode).

Public entry points:

* ``render_standings_png(tid) -> bytes`` — full table in a single PNG
  (back-compat).
* ``render_standings_pngs(tid) -> list[bytes]`` — one PNG per group when
  the tournament has multiple groups, so callers can send each group as
  its own Telegram photo. With a single group (or none) the list has
  exactly one element identical to ``render_standings_png``. Splitting
  per-group keeps each upload small enough to clear Telegram's photo
  size limit even when a custom ``/set_tournament_bg`` background is
  attached, and makes the rendered background actually fit one group
  card instead of being stretched across the whole sheet.

Pillow is already a hard requirement of the bot (see requirements.txt /
Dockerfile), so this module adds no new Python deps. It does need a TTF
font with Cyrillic coverage available on the host — see Dockerfile and
nixpacks.toml where ``fonts-dejavu-core`` is installed.
"""
from __future__ import annotations

import re
from io import BytesIO
from typing import Iterable

from PIL import Image, ImageDraw, ImageFont

from bg_helper import make_canvas
from database import get_tournament
from tournament import get_group_standings


# ── palette (Telegram-ish dark theme) ───────────────────────────────────────
BG          = (28, 30, 38)
CARD_BG     = (40, 44, 54)
HEADER_BG   = (54, 88, 144)
HEADER_TXT  = (255, 255, 255)
ROW         = (44, 48, 60)
ROW_ALT     = (50, 54, 66)
ROW_QUALIFY = (52, 92, 64)        # green tint = top-N qualifies for playoff
TEXT        = (235, 238, 245)
MUTED       = (170, 180, 195)
BORDER      = (60, 65, 78)
ACCENT      = (90, 200, 130)

# Medal colours for positions 1/2/3 in the "#" column.
POS_COLORS = {
    1: (255, 215,   0),  # gold
    2: (200, 205, 215),  # silver
    3: (205, 127,  50),  # bronze
}

# Telegram re-encodes photos and shows them at the chat-thumbnail width.
# When the source PNG is too small (~880 px), pinch-zoom on mobile blurs
# heavily because the client has nothing to upscale into. Rendering at 2×
# gives Telegram enough pixels to keep things crisp on both axes.
SCALE = 2


def _s(v: int) -> int:
    return int(v * SCALE)


# (label, width_px, align)
_COLS_RAW: list[tuple[str, int, str]] = [
    ("#",     54,  "center"),
    ("Игрок", 320, "left"),
    ("И",     54,  "center"),
    ("В",     54,  "center"),
    ("Н",     54,  "center"),
    ("П",     54,  "center"),
    ("Мячи",  108, "center"),
    ("РГ",    66,  "center"),
    ("О",     66,  "center"),
]
COLS: list[tuple[str, int, str]] = [(lbl, _s(w), al) for lbl, w, al in _COLS_RAW]

PAD            = _s(24)
ROW_H          = _s(44)
HEADER_H       = _s(50)
GROUP_TITLE_H  = _s(56)
GAP            = _s(18)
TITLE_BLOCK    = _s(110)
LEGEND_BLOCK   = _s(56)


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
def _draw_rect_alpha(
    img: Image.Image,
    box: list | tuple,
    fill: tuple[int, int, int],
    alpha: int = 255,
) -> None:
    """Draw a filled rectangle with variable opacity onto an RGB image.

    When ``alpha == 255`` this is equivalent to a normal draw.rectangle
    (fast path — no compositing). Lower alpha makes the rectangle
    semi-transparent, letting the background image show through.
    """
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
    """Render text inside a cell with horizontal alignment + padding.

    When ``base_img`` is provided AND the text contains color emoji,
    the text is drawn through ``emoji_helper.draw_text_with_emoji`` so
    flags / pictographs render in colour. Otherwise falls back to
    ``draw.text`` for compatibility with sites that haven't been
    updated yet.
    """
    if pad is None:
        pad = _s(12)
    # Always measure with the emoji-aware helper so cells containing
    # 🐐 / 🇩🇪 don't overflow because a regular-font measurement
    # under-counted the wider glyph.
    from emoji_helper import draw_text_with_emoji, measure_text_with_emoji
    tw = measure_text_with_emoji(text, font)
    bbox = draw.textbbox((0, 0), "Hg", font=font)  # cap-height reference
    th = bbox[3] - bbox[1]
    if align == "right":
        tx = x + w - pad - tw
    elif align == "center":
        tx = x + (w - tw) // 2
    else:
        tx = x + pad
    ty = y + (h - th) // 2 - bbox[1]
    if base_img is not None and any(
        ord(ch) > 0x2000 for ch in text  # cheap prefilter
    ):
        draw_text_with_emoji(base_img, (tx, ty), text, font, fill=color)
    else:
        draw.text((tx, ty), text, font=font, fill=color)


def _truncate(
    text: str,
    font: ImageFont.ImageFont,
    max_w: int,
    draw: ImageDraw.ImageDraw,
) -> str:
    """Trim ``text`` with an ellipsis so it fits within ``max_w`` pixels.

    Emoji-aware: uses :func:`emoji_helper.measure_text_with_emoji` so a
    "🐐 GOAT - phoenileo (@p)" string isn't sliced through a flag glyph.
    """
    from emoji_helper import truncate_text_with_emoji
    return truncate_text_with_emoji(text, font, max_w)


def _display_name(p: dict, mode: str = "full") -> str:
    """Prefer the in-game nickname when set, fall back to telegram username.

    Hides the synthetic ``id_<digits>`` placeholder (created for users
    without a public ``@username``) — for those we just render the
    nickname plain, or ``id 12345`` if no nickname is set yet.

    When a per-tournament ``team_tag`` is attached to the row (the
    ``tournament_players`` JOIN propagates it as ``p['team_tag']``),
    it's woven into the rendered name as ``"<nick> - <Team> (@user)"``
    so standings PNGs make team affiliations visible at a glance.

    ``mode`` toggles which fields are surfaced — picked from the
    tournament's "🎨 Оформление" → "🪪 Имена" setting:

    * ``"full"`` (default) — preserve historic behaviour (everything).
    * ``"tag"``  — show only the Telegram ``@username`` (with sane
      fallbacks when the user has no public handle).
    * ``"nick"`` — show only the in-game nickname / team tag; hide the
      ``@-tag`` entirely so brand-centric tournaments look clean.
    """
    nick = (p.get("game_nickname") or "").strip()
    user = (p.get("username") or "").strip()
    tag = (p.get("team_tag") or "").strip()
    is_synthetic = bool(user) and bool(re.match(r"^id_\d+$", user.lower()))
    pretty_user = "" if is_synthetic else user
    synth_label = (
        user.lower().replace("id_", "id ", 1) if is_synthetic else ""
    )

    mode = (mode or "full").lower()
    if mode == "tag":
        # @-tag preferred. Fall back through nick → team tag → "id N"
        # so the row never renders blank for a synthetic-id user.
        if pretty_user:
            return f"@{pretty_user}"
        if nick:
            return nick
        if tag:
            return tag
        if synth_label:
            return synth_label
        return "?"
    if mode == "nick":
        # Nick / team-name preferred. The Telegram handle is hidden
        # entirely; keep nick + tag combined when both are set so the
        # team affiliation is still visible.
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
        return "?"

    # mode == "full" — original behaviour, preserved verbatim.
    if is_synthetic:
        synth = nick or synth_label
        return f"{synth} - {tag}" if tag else synth
    if not user:
        # Last-resort: no username at all. Use the nickname or a dash.
        if not nick:
            return tag or "?"
        return f"{nick} - {tag}" if tag else nick
    # Both nick and username present — prefer "nick - tag (@user)" so a
    # team-tagged player always shows their full identity. Without a
    # tag, drop the nick when it duplicates the username case-
    # insensitively to avoid "phoenileo (@phoenileo)" redundancy.
    if nick:
        if tag:
            return f"{nick} - {tag} (@{user})"
        if nick.lower() == user.lower():
            return f"@{user}"
        return f"{nick} (@{user})"
    if tag:
        return f"{tag} (@{user})"
    return f"@{user}"


def _ru_plural(n: int, forms: tuple[str, str, str]) -> str:
    """
    Russian plural picker. ``forms`` = (singular, few, many),
    e.g. ('игрок', 'игрока', 'игроков').
    """
    n = abs(n) % 100
    n1 = n % 10
    if 10 < n < 20:
        return forms[2]
    if 1 < n1 < 5:
        return forms[1]
    if n1 == 1:
        return forms[0]
    return forms[2]


# ── public API ──────────────────────────────────────────────────────────────
def render_standings_png(tid: int) -> bytes:
    """Single-image renderer (back-compat, used when callers want one PNG)."""
    t = get_tournament(tid) or {}
    standings = get_group_standings(tid)
    qualify_n = max(1, int(t.get("playoff_slots") or 2))
    return _render(t, standings, qualify_n)


def render_standings_pngs(tid: int) -> list[bytes]:
    """Return one PNG per group; falls back to a single image otherwise.

    The bot uses this to send each group as its own Telegram photo —
    keeps each upload small enough that ``/set_tournament_bg`` fans don't
    timeout the way a tall combined sheet does, and makes a custom
    background actually fit one group card instead of being stretched
    across the whole standings sheet.
    """
    t = get_tournament(tid) or {}
    standings = get_group_standings(tid)
    qualify_n = max(1, int(t.get("playoff_slots") or 2))
    if not standings or len(standings) <= 1:
        return [_render(t, standings or {}, qualify_n)]
    out: list[bytes] = []
    for g, players in sorted(standings.items()):
        out.append(_render(t, {g: players}, qualify_n))
    return out


def render_standings_png_for_group(tid: int, group_name: str) -> bytes | None:
    """Render a single group's standings as one PNG.

    Used by ``/table`` when the user picks one specific group from the
    view-selector keyboard. ``group_name`` is matched case-insensitively
    against the keys of :func:`get_group_standings`. Returns ``None`` if
    that group doesn't exist on the tournament so the caller can show a
    friendly error.
    """
    t = get_tournament(tid) or {}
    standings = get_group_standings(tid) or {}
    qualify_n = max(1, int(t.get("playoff_slots") or 2))
    target = (group_name or "").strip().lower()
    matched: dict | None = None
    for g, players in standings.items():
        if str(g).strip().lower() == target:
            matched = {g: players}
            break
    if matched is None:
        return None
    return _render(t, matched, qualify_n)


def list_standings_groups(tid: int) -> list[str]:
    """Return the sorted list of group names for ``tid``.

    Used by the ``/table`` view-selector keyboard to label one button per
    group. Empty list means "tournament has no standings yet" — typically
    pre-roster — and the caller can hide the per-group buttons.
    """
    standings = get_group_standings(tid) or {}
    return [str(g) for g in sorted(standings.keys())]


def _render(t: dict, standings: dict, qualify_n: int) -> bytes:
    """Draw ``standings`` (whole tournament or a single-group slice) as PNG."""
    name_mode = (t.get("name_display_mode") or "full").lower()
    title_font  = _font(_s(34), bold=True)
    sub_font    = _font(_s(20), bold=False)
    group_font  = _font(_s(26), bold=True)
    head_font   = _font(_s(20), bold=True)
    row_font    = _font(_s(22), bold=False)
    row_font_b  = _font(_s(22), bold=True)
    # Slightly smaller font for the player-name column so long
    # "<nick> - <Country> 🇨🇨 (@user)" labels fit without aggressive
    # ellipsing. Numeric columns keep the larger row_font so scores
    # stay easy to scan.
    name_font   = _font(_s(17), bold=False)

    col_total = sum(w for _, w, _ in COLS)
    width = PAD * 2 + col_total

    height = TITLE_BLOCK
    for _, players in sorted(standings.items()):
        height += GAP + GROUP_TITLE_H + HEADER_H + ROW_H * max(1, len(players))
    height += LEGEND_BLOCK + PAD
    if not standings:
        height += GROUP_TITLE_H + ROW_H

    img = make_canvas(
        width, height,
        bg_color=BG,
        bg_image_path=t.get("bg_image_path"),
        bg_image_data=t.get("bg_image_data"),
        overlay_alpha=int(t.get("bg_overlay_alpha") or 165),
    )
    draw = ImageDraw.Draw(img)

    # Row/card background opacity (0=transparent, 255=solid).
    # Controlled via /set_row_alpha <ID> <0-100> (stored as 0-255).
    _row_alpha = int(t.get("row_bg_alpha") or 255)

    # ── title block ────────────────────────────────────────────────────────
    name = (t.get("name") or "Турнир").strip()
    t_type = (t.get("tournament_type") or "").upper()
    scope = "общий" if t.get("is_official", 1) else "локальный"
    # NOTE: don't include emoji in the rendered title — DejaVu doesn't ship
    # emoji glyphs, they show up as a "tofu" box. The trophy emoji lives in
    # the message caption instead, where Telegram renders it natively.
    title_label = name
    sub_bits = []
    if t_type:
        sub_bits.append(t_type)
    sub_bits.append(scope)
    sub_bits.append("Турнирная таблица")
    sub_label = "  ·  ".join(sub_bits)

    # truncate title to fit
    max_title_w = width - PAD * 2
    title_label = _truncate(title_label, title_font, max_title_w, draw)
    draw.text((PAD, PAD), title_label, font=title_font, fill=TEXT)
    draw.text((PAD, PAD + _s(50)), sub_label, font=sub_font, fill=MUTED)

    y = TITLE_BLOCK

    if not standings:
        gx0 = PAD
        _draw_rect_alpha(img, [gx0, y, gx0 + col_total, y + GROUP_TITLE_H], CARD_BG, _row_alpha)
        draw.text(
            (gx0 + _s(16), y + _s(14)),
            "Группы ещё не сформированы",
            font=group_font, fill=MUTED,
        )
        y += GROUP_TITLE_H + GAP

    for g, players in sorted(standings.items()):
        gx0 = PAD
        gw = col_total

        # group title bar
        _draw_rect_alpha(img, [gx0, y, gx0 + gw, y + GROUP_TITLE_H], CARD_BG, _row_alpha)
        # Use custom group name if set (single-group league) — falls
        # back to "Группа A/B/..." for multi-group tournaments.
        custom = (t.get("group_display_name") or "").strip()
        if custom and len(standings) == 1:
            group_label = custom
        else:
            group_label = f"Группа {g}"
        draw.text((gx0 + _s(16), y + _s(14)), group_label, font=group_font, fill=TEXT)
        # tiny right-aligned hint with player count
        hint = f"{len(players)} {_ru_plural(len(players), ('игрок', 'игрока', 'игроков'))}"
        bbox = draw.textbbox((0, 0), hint, font=sub_font)
        hx = gx0 + gw - _s(16) - (bbox[2] - bbox[0])
        draw.text((hx, y + _s(18)), hint, font=sub_font, fill=MUTED)
        y += GROUP_TITLE_H

        # column header strip
        _draw_rect_alpha(img, [gx0, y, gx0 + gw, y + HEADER_H], HEADER_BG, _row_alpha)
        cx = gx0
        for label, w, align in COLS:
            _draw_text_in_cell(
                draw, label, cx, y, w, HEADER_H,
                head_font, HEADER_TXT, align=align,
            )
            cx += w
        y += HEADER_H

        # rows
        for pos, p in enumerate(players, 1):
            played = (
                (p.get("group_wins") or 0)
                + (p.get("group_draws") or 0)
                + (p.get("group_losses") or 0)
            )
            gf = int(p.get("group_gf") or 0)
            ga = int(p.get("group_ga") or 0)

            if pos <= qualify_n:
                row_bg = ROW_QUALIFY
            else:
                row_bg = ROW_ALT if pos % 2 == 0 else ROW

            _draw_rect_alpha(img, [gx0, y, gx0 + gw, y + ROW_H], row_bg, _row_alpha)
            draw.line(
                [gx0, y + ROW_H - 1, gx0 + gw, y + ROW_H - 1],
                fill=BORDER,
            )

            values: list[str] = [
                str(pos),
                _display_name(p, name_mode),
                str(played),
                str(p.get("group_wins") or 0),
                str(p.get("group_draws") or 0),
                str(p.get("group_losses") or 0),
                f"{gf}:{ga}",
                f"{gf - ga:+d}",
                str(p.get("group_points") or 0),
            ]

            cx = gx0
            for (label, w, align), val in zip(COLS, values):
                color = TEXT
                font_use = row_font
                if label == "О":
                    font_use = row_font_b
                if label == "#" and pos in POS_COLORS:
                    color = POS_COLORS[pos]
                    font_use = row_font_b
                if label == "Игрок":
                    font_use = name_font
                    val = _truncate(val, font_use, w - 24, draw)
                # Player-name cells get color emoji rendering (flags,
                # 🐐 etc.); the rest of the columns are pure ASCII so
                # we can skip the emoji branch and save a few cycles.
                _draw_text_in_cell(
                    draw, val, cx, y, w, ROW_H,
                    font_use, color, align=align,
                    base_img=img if label == "Игрок" else None,
                )
                cx += w
            y += ROW_H

        y += GAP

    # ── legend ─────────────────────────────────────────────────────────────
    y_l = y + 2
    sw = 18
    draw.rectangle([PAD, y_l, PAD + sw, y_l + sw], fill=ROW_QUALIFY)
    draw.text(
        (PAD + sw + 8, y_l - 2),
        f"проходят в плей-офф (топ-{qualify_n} из группы)",
        font=sub_font, fill=MUTED,
    )

    buf = BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


__all__ = [
    "render_standings_png",
    "render_standings_pngs",
    "render_standings_png_for_group",
    "list_standings_groups",
]
