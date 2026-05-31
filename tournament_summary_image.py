"""
Render a single hero PNG that summarises the tournament: champion,
runner-up, bronze, лучшая атака / оборона / бомбардир, лучший % побед,
самый яркий матч, и плашка с общими цифрами.

Public entry point::

    render_tournament_summary_png(summary, tournament=None) -> bytes

``summary`` is the dict returned by ``tournament_summary.compute_tournament_summary``
(must contain at minimum ``name``, ``type_label``, ``awards``, and the
counters). ``tournament`` is the raw row from ``database.get_tournament``
— used only to look up the optional custom background via
``bg_helper.make_canvas``; passing ``None`` falls back to the flat dark
palette shared with the other PNGs.

Layout (1× units; multiplied by ``SCALE`` on render):

    ┌── title block ──────────────────────────────────────┐
    ┌── champion hero card (full width, gold accents) ────┐
    ┌── 2-up: runner-up │ bronze ─────────────────────────┐
    ┌── 2-up: best attack │ best defense ─────────────────┐
    ┌── 2-up: top scorer │ best win-rate ─────────────────┐
    ┌── spectacle match strip (full width) ───────────────┐
    ┌── totals strip (full width, footer) ────────────────┐

Cards collapse gracefully — categories with no winner (e.g. no bronze
in a four-team bracket without a third-place fixture, or no top-scorer
data when the tournament was reported without OCR) are simply hidden,
with the layout reflowing to keep the image tight.
"""
from __future__ import annotations

from io import BytesIO
from typing import Optional

from PIL import Image, ImageDraw, ImageFont

from bg_helper import make_canvas


# ── palette (matches standings_image / playoff_image / tablebomb_image) ─────
BG          = (28, 30, 38)
CARD_BG     = (40, 44, 54)
CARD_DEEP   = (32, 36, 46)
HEADER_BG   = (54, 88, 144)
HEADER_TXT  = (255, 255, 255)
TEXT        = (235, 238, 245)
MUTED       = (170, 180, 195)
BORDER      = (60, 65, 78)
ACCENT      = (90, 200, 130)

GOLD        = (255, 215,   0)
SILVER      = (200, 205, 215)
BRONZE      = (205, 127,  50)

# Per-card accent colours.
ATTACK_BG   = (60,  44,  44)   # warm red
ATTACK_FG   = (240, 130, 110)
DEFENSE_BG  = (40,  56,  72)   # cool blue
DEFENSE_FG  = (130, 190, 230)
SCORER_BG   = (60,  56,  36)   # warm yellow
SCORER_FG   = (240, 200, 110)
WINRATE_BG  = (44,  60,  48)   # green
WINRATE_FG  = (130, 220, 150)
SPECT_BG    = (62,  44,  72)   # purple
SPECT_FG    = (210, 160, 240)


SCALE = 2


def _s(v: int) -> int:
    return int(v * SCALE)


# ── geometry (1× — multiplied by SCALE on render) ──────────────────────────
WIDTH         = 540
PAD           = 20
GAP           = 14

TITLE_BLOCK_H = 96
HERO_H        = 165
SUB_H         = 110
AWARD_H       = 122
SPECT_H       = 96
TOTALS_H      = 70


# ── font loading (mirrors playoff_image.py) ────────────────────────────────
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
def _truncate(text: str, font: ImageFont.ImageFont, max_w: int,
              draw: ImageDraw.ImageDraw) -> str:
    """Binary-search ellipsis truncation so long names never spill the card."""
    if not text:
        return ""
    if draw.textlength(text, font=font) <= max_w:
        return text
    ell = "…"
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if draw.textlength(text[:mid] + ell, font=font) <= max_w:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo] + ell


def _fit_text_size(
    text: str,
    base_size: int,
    min_size: int,
    max_w: int,
    draw: ImageDraw.ImageDraw,
    *,
    bold: bool = False,
) -> tuple[str, ImageFont.ImageFont]:
    """Pick the LARGEST font size <= ``base_size`` (and >= ``min_size``)
    at which ``text`` fits inside ``max_w``. Falls back to ellipsis
    truncation at ``min_size`` if even the smallest size can't fit.

    Used for headline lines (winner @usernames, big number values)
    where shrinking is preferable to truncating — readers care about
    seeing the full name, even if a font size smaller."""
    if not text:
        return "", _font(_s(base_size), bold=bold)
    # Step in 1px increments down from base — for our 12-22 pt range
    # this is at most 10 attempts and cheap.
    for size in range(base_size, min_size - 1, -1):
        f = _font(_s(size), bold=bold)
        if draw.textlength(text, font=f) <= max_w:
            return text, f
    f = _font(_s(min_size), bold=bold)
    return _truncate(text, f, max_w, draw), f


def _wrap_lines(
    text: str,
    font: ImageFont.ImageFont,
    max_w: int,
    draw: ImageDraw.ImageDraw,
    *,
    max_lines: int = 2,
) -> list[str]:
    """Greedy word-wrap. Returns up to ``max_lines`` lines; the final
    line is ellipsis-truncated if more text remains. Middle-dot
    separators (``·``) become legal break points so values like
    ``в 12 матчах · РГ +15`` wrap cleanly."""
    if not text:
        return []
    words = text.replace("\u00b7", " \u00b7 ").split()
    if not words:
        return []
    lines: list[str] = []
    current = ""
    for w in words:
        candidate = (current + " " + w).strip() if current else w
        if draw.textlength(candidate, font=font) <= max_w:
            current = candidate
        else:
            if current:
                lines.append(current)
                if len(lines) >= max_lines:
                    current = w
                    break
                current = w
            else:
                # single word longer than the card — hard-truncate
                lines.append(_truncate(w, font, max_w, draw))
                current = ""
                if len(lines) >= max_lines:
                    break
    if current and len(lines) < max_lines:
        lines.append(current)
    elif current and len(lines) >= max_lines:
        # Fold leftover into the last line and ellipsis-truncate.
        last = lines[-1]
        lines[-1] = _truncate(last + " " + current, font, max_w, draw)
    return lines


def _rounded(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int],
             *, radius: int, fill, outline=None, width: int = 0) -> None:
    """Pillow's `rounded_rectangle` exists ≥ 8.2 — fall back to a plain
    rectangle on truly ancient builds without crashing."""
    try:
        draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=width)
    except (AttributeError, TypeError):
        draw.rectangle(box, fill=fill, outline=outline, width=width)


def _accent_bar(img: Image.Image, x: int, y: int, h: int, color: tuple) -> None:
    """Vertical 4 px accent stripe along the left edge of a card."""
    draw = ImageDraw.Draw(img)
    draw.rectangle([x, y, x + _s(4), y + h], fill=color)


def _draw_card_shell(img: Image.Image, box: tuple[int, int, int, int],
                     *, fill=CARD_BG, accent: Optional[tuple] = None) -> None:
    """Outer card body + 1 px border + optional left accent stripe."""
    draw = ImageDraw.Draw(img)
    _rounded(draw, box, radius=_s(14), fill=fill, outline=BORDER, width=_s(1))
    if accent is not None:
        x0, y0, _, y1 = box
        _accent_bar(img, x0 + _s(1), y0 + _s(8), (y1 - y0) - _s(16), accent)


def _draw_award_card(
    img: Image.Image,
    box: tuple[int, int, int, int],
    *,
    icon: str,
    title: str,
    name: str,
    value: str,
    sub: str,
    accent: tuple[int, int, int],
    name_color: tuple[int, int, int] = TEXT,
) -> None:
    """Generic award card — icon + section header on top, bold winner
    name in the middle, value/subtitle at the bottom. Falls back to
    "—" cleanly when ``name`` is empty (caller already filtered, but
    we double-check defensively).

    Long usernames auto-shrink (down to 17px) instead of being clipped
    to ``@vadimvadi…`` — the operator cares about seeing the full
    @handle. The ``sub`` line word-wraps to 2 lines if needed so
    "в 4 матчах · РГ +10" doesn't get cut mid-phrase."""
    draw = ImageDraw.Draw(img)
    _draw_card_shell(img, box, accent=accent)
    x0, y0, x1, y1 = box
    inner_x = x0 + _s(20)
    inner_w = (x1 - x0) - _s(40)

    # Section header — title is short and bounded, simple truncation.
    f_label = _font(_s(13), bold=True)
    header = f"{icon}  {title.upper()}"
    header = _truncate(header, f_label, inner_w, draw)
    draw.text((inner_x, y0 + _s(12)), header, font=f_label, fill=accent)

    # Winner name — auto-shrink instead of ellipsis, and 2-line word-
    # wrap when even the smallest size won't fit. Long @usernames and
    # "X vs Y" pair labels (closest_pair, goalfest "битва нулей") stay
    # whole instead of being chopped to '@vadimvadimvadi…' or
    # '@phonileo vs @mi…'. Lower bound is 14px — anything smaller
    # would be unreadable in a Telegram thumbnail.
    name = name or "—"
    f_name_min = _font(_s(14), bold=True)
    name_y = y0 + _s(34)
    name_extra = 0  # extra vertical px taken by a wrapped name
    if draw.textlength(name, font=f_name_min) > inner_w:
        # Won't fit even at min size as one line — wrap.
        name_lines = _wrap_lines(name, f_name_min, inner_w, draw, max_lines=2)
        for i, line in enumerate(name_lines):
            draw.text(
                (inner_x, name_y + i * _s(17)),
                line, font=f_name_min, fill=name_color,
            )
        if len(name_lines) > 1:
            name_extra = _s(17)
    else:
        name_str, f_name = _fit_text_size(
            name, base_size=22, min_size=14,
            max_w=inner_w, draw=draw, bold=True,
        )
        draw.text((inner_x, name_y), name_str, font=f_name, fill=name_color)

    # Value — also auto-shrinks. "пропущено 10 в 12 матчах · РГ +15"
    # is long but fits at 13px. If even at min size it'd be ellipsis-
    # truncated, fall back to wrapping into 2 lines so '20 матчей без
    # поражений · 15 побед, 5 ничьих' shows whole. ``name_extra`` is
    # added to every y so a 2-line name doesn't overlap the value.
    value_y = y0 + _s(64) + name_extra
    sub_base_y = _s(84) + name_extra
    value_extra = 0  # extra vertical px from a 2-line value
    if value:
        f_val_min = _font(_s(12), bold=True)
        if draw.textlength(value, font=f_val_min) > inner_w:
            # Single line at min won't fit — wrap to 2 lines at min size.
            value_lines = _wrap_lines(value, f_val_min, inner_w, draw, max_lines=2)
            for i, line in enumerate(value_lines):
                draw.text(
                    (inner_x, value_y + i * _s(15)),
                    line, font=f_val_min, fill=TEXT,
                )
            if len(value_lines) > 1:
                value_extra = _s(12)
        else:
            value_str, f_val = _fit_text_size(
                value, base_size=15, min_size=12,
                max_w=inner_w, draw=draw, bold=True,
            )
            draw.text((inner_x, value_y), value_str, font=f_val, fill=TEXT)
    sub_y_offset = sub_base_y + value_extra

    # Sub — wraps to 2 lines so longer commentary survives intact.
    if sub:
        f_sub = _font(_s(12), bold=False)
        sub_lines = _wrap_lines(sub, f_sub, inner_w, draw, max_lines=2)
        # When the name OR the value already burned a second line, drop
        # the second sub line silently rather than overflowing the card.
        if name_extra > 0 or value_extra > 0:
            sub_lines = sub_lines[:1]
        for i, line in enumerate(sub_lines):
            draw.text(
                (inner_x, y0 + sub_y_offset + i * _s(14)),
                line, font=f_sub, fill=MUTED,
            )


def _draw_hero_champion(img: Image.Image, box: tuple[int, int, int, int],
                        champion: Optional[dict], extra_sub: str) -> None:
    """Top hero card with bold "🏆 ЧЕМПИОН" + the winner's name in gold.

    ``extra_sub`` is the small subtitle slot (e.g. final wins/draws/losses
    breakdown). Empty when the champion has no real played-match stats.
    Long @usernames auto-shrink down to 28px before being truncated."""
    draw = ImageDraw.Draw(img)
    _rounded(draw, box, radius=_s(18), fill=(48, 42, 24),
             outline=GOLD, width=_s(2))
    x0, y0, x1, _ = box
    inner_x = x0 + _s(24)
    inner_w = (x1 - x0) - _s(48)

    f_label = _font(_s(15), bold=True)
    draw.text((inner_x, y0 + _s(14)), "🏆  ЧЕМПИОН ТУРНИРА",
              font=f_label, fill=GOLD)

    name = (champion or {}).get("label") or "—"
    name, f_name = _fit_text_size(
        name, base_size=40, min_size=28,
        max_w=inner_w, draw=draw, bold=True,
    )
    draw.text((inner_x, y0 + _s(38)), name, font=f_name, fill=GOLD)

    if extra_sub:
        f_sub = _font(_s(14), bold=False)
        # Two lines for the path summary on the hero — covers the
        # "20-0-0 в 20 матчах · 51:15 · РГ +36" case that used to wrap
        # off-card.
        sub_lines = _wrap_lines(extra_sub, f_sub, inner_w, draw, max_lines=2)
        for i, line in enumerate(sub_lines):
            draw.text(
                (inner_x, y0 + _s(102) + i * _s(16)),
                line, font=f_sub, fill=(220, 200, 140),
            )


def _draw_medal_card(img: Image.Image, box: tuple[int, int, int, int],
                     icon: str, title: str, person: Optional[dict],
                     accent: tuple[int, int, int]) -> None:
    """Smaller card for runner-up / bronze. ``person`` may be ``None``
    — we still draw the card with an "—" placeholder so the silver/
    bronze row stays visually balanced. Long @usernames auto-shrink."""
    draw = ImageDraw.Draw(img)
    _draw_card_shell(img, box, accent=accent)
    x0, y0, x1, _ = box
    inner_x = x0 + _s(20)
    inner_w = (x1 - x0) - _s(40)

    f_label = _font(_s(12), bold=True)
    header = f"{icon}  {title.upper()}"
    header = _truncate(header, f_label, inner_w, draw)
    draw.text((inner_x, y0 + _s(12)), header, font=f_label, fill=accent)

    name = (person or {}).get("label") or "—"
    name, f_name = _fit_text_size(
        name, base_size=24, min_size=18,
        max_w=inner_w, draw=draw, bold=True,
    )
    draw.text((inner_x, y0 + _s(32)), name, font=f_name, fill=TEXT)


def _draw_spectacle(img: Image.Image, box: tuple[int, int, int, int],
                    spect: Optional[dict]) -> None:
    """Full-width strip for the most spectacular match. The label is
    a 'team1 5:0 team2' string that can get long with two long
    @usernames — auto-shrinks instead of cutting one of the names."""
    if not spect:
        return
    draw = ImageDraw.Draw(img)
    _draw_card_shell(img, box, accent=SPECT_FG, fill=SPECT_BG)
    x0, y0, x1, _ = box
    inner_x = x0 + _s(20)
    inner_w = (x1 - x0) - _s(40)

    f_label = _font(_s(13), bold=True)
    draw.text((inner_x, y0 + _s(10)),
              "💥  САМЫЙ ЯРКИЙ МАТЧ",
              font=f_label, fill=SPECT_FG)

    label = spect.get("label") or ""
    label, f_name = _fit_text_size(
        label, base_size=22, min_size=15,
        max_w=inner_w, draw=draw, bold=True,
    )
    draw.text((inner_x, y0 + _s(32)), label, font=f_name, fill=TEXT)

    sub = spect.get("sub") or ""
    if sub:
        f_sub = _font(_s(13), bold=False)
        sub_lines = _wrap_lines(sub, f_sub, inner_w, draw, max_lines=1)
        for i, line in enumerate(sub_lines):
            draw.text(
                (inner_x, y0 + _s(64) + i * _s(15)),
                line, font=f_sub, fill=MUTED,
            )


def _draw_totals(img: Image.Image, box: tuple[int, int, int, int],
                 summary: dict, totals: dict) -> None:
    """Footer strip with players / matches / goals / avg-per-match."""
    draw = ImageDraw.Draw(img)
    _draw_card_shell(img, box, fill=CARD_DEEP)
    x0, y0, x1, y1 = box
    width = x1 - x0
    height = y1 - y0
    cells = [
        ("Игроков",  str(summary.get("total_players", 0))),
        ("Матчей",   str(totals.get("matches", 0))),
        ("Голов",    str(totals.get("goals", 0))),
        ("Ср. голы", f"{totals.get('avg', 0.0):.2f}"),
    ]
    cell_w = width / len(cells)
    f_lbl = _font(_s(11), bold=True)
    f_val = _font(_s(22), bold=True)
    for i, (lbl, val) in enumerate(cells):
        cx = x0 + cell_w * (i + 0.5)
        draw.text((cx, y0 + _s(10)), lbl, font=f_lbl,
                  fill=MUTED, anchor="mt")
        draw.text((cx, y0 + _s(28)), val, font=f_val,
                  fill=TEXT, anchor="mt")
        # Vertical separator (skip on the last cell).
        if i < len(cells) - 1:
            sep_x = int(x0 + cell_w * (i + 1))
            draw.line(
                [(sep_x, y0 + _s(14)), (sep_x, y1 - _s(14))],
                fill=BORDER, width=_s(1),
            )


# ── public entry point ──────────────────────────────────────────────────────
def render_tournament_summary_png(
    summary: dict,
    tournament: Optional[dict] = None,
) -> bytes:
    """Render the hero PNG. Returns PNG bytes."""
    awards = (summary.get("awards") or {}) if summary else {}
    totals = awards.get("totals") or {}

    # ── compute layout height bottom-up. Every band's `*_h` is in 1×
    # units; we know which bands appear depending on data presence. ──
    has_runner = bool(awards.get("runner_up"))
    has_bronze = bool(awards.get("bronze"))
    medal_row_present = has_runner or has_bronze

    # Award bands. We keep best-attack + best-defense always (they
    # collapse to "—" when zero data, but tournaments with confirmed
    # matches will always populate them). Skip slots that are None.
    award_slots: list[tuple[str, Optional[dict]]] = [
        ("attack",   awards.get("best_attack")),
        ("defense",  awards.get("best_defense")),
        ("scorer",   awards.get("top_scorer")),
        ("winrate",  awards.get("win_rate")),
    ]
    award_slots_present = [(k, v) for k, v in award_slots if v]
    # Pair up into rows of 2.
    award_rows = (len(award_slots_present) + 1) // 2

    has_spectacle = bool(awards.get("spectacle"))

    width_px = _s(WIDTH)
    pad = _s(PAD)
    gap = _s(GAP)

    # Stack heights.
    title_h   = _s(TITLE_BLOCK_H)
    hero_h    = _s(HERO_H)
    sub_h     = _s(SUB_H) if medal_row_present else 0
    award_h_each = _s(AWARD_H)
    awards_block_h = (award_h_each * award_rows
                      + gap * max(0, award_rows - 1)) if award_rows else 0
    spect_h   = _s(SPECT_H) if has_spectacle else 0
    totals_h  = _s(TOTALS_H)

    # Build a list of (band_height, gap_after?) so we can compute
    # total height and y offsets in one pass.
    bands: list[tuple[str, int]] = [("title", title_h), ("hero", hero_h)]
    if sub_h:           bands.append(("sub",       sub_h))
    if awards_block_h:  bands.append(("awards",    awards_block_h))
    if spect_h:         bands.append(("spectacle", spect_h))
    bands.append(("totals", totals_h))

    height_px = pad * 2 + sum(h for _, h in bands) + gap * (len(bands) - 1)

    # Canvas — picks up the tournament's custom background if attached.
    bg_image_path = (tournament or {}).get("bg_image_path") if tournament else None
    bg_image_data = (tournament or {}).get("bg_image_data") if tournament else None
    overlay_alpha = int((tournament or {}).get("bg_overlay_alpha") or 200)
    img = make_canvas(
        width_px, height_px,
        bg_color=BG,
        bg_image_path=bg_image_path,
        bg_image_data=bg_image_data,
        overlay_alpha=overlay_alpha,
    )
    draw = ImageDraw.Draw(img)

    # ── walk bands top-to-bottom. ──
    cy = pad

    for kind, h in bands:
        x0 = pad
        x1 = width_px - pad

        if kind == "title":
            # Two lines: 🏆 Итоги турнира + tournament name + type.
            f_top = _font(_s(15), bold=True)
            draw.text((x0, cy), "🏆  ИТОГИ ТУРНИРА",
                      font=f_top, fill=ACCENT)
            f_name = _font(_s(28), bold=True)
            full = summary.get("name") or "—"
            full = _truncate(full, f_name, x1 - x0, draw)
            draw.text((x0, cy + _s(20)), full, font=f_name, fill=TEXT)
            f_meta = _font(_s(13), bold=False)
            meta_bits = []
            if summary.get("type_label"):
                meta_bits.append(summary["type_label"])
            if summary.get("format_label"):
                meta_bits.append(summary["format_label"])
            if summary.get("id"):
                meta_bits.append(f"ID {summary['id']}")
            meta = " · ".join(meta_bits)
            if meta:
                meta = _truncate(meta, f_meta, x1 - x0, draw)
                draw.text((x0, cy + _s(60)), meta, font=f_meta, fill=MUTED)
            # Thin divider under the title block.
            draw.line(
                [(x0, cy + h - _s(4)), (x1, cy + h - _s(4))],
                fill=BORDER, width=_s(1),
            )

        elif kind == "hero":
            # Champion hero. ``extra_sub`` shows the champion's path —
            # we use the player_stats row that matches the champion.
            champ = awards.get("champion")
            extra_sub = ""
            if champ:
                ch_user = (champ.get("username") or "").lower()
                for r in summary.get("player_stats") or []:
                    if (r.get("username") or "").lower() == ch_user:
                        extra_sub = (
                            f"{r['wins']}-{r['draws']}-{r['losses']} в "
                            f"{r['played']} матчах · "
                            f"{r['gf']}:{r['ga']} · РГ {r['gf']-r['ga']:+d}"
                        )
                        break
            _draw_hero_champion(img, (x0, cy, x1, cy + h), champ, extra_sub)

        elif kind == "sub":
            # Two-up: silver | bronze. If only one is present, span half.
            mid = (x0 + x1) // 2
            if has_runner:
                _draw_medal_card(
                    img, (x0, cy, mid - gap // 2, cy + h),
                    "🥈", "2-е место (финалист)",
                    awards.get("runner_up"), SILVER,
                )
            if has_bronze:
                _draw_medal_card(
                    img, (mid + gap // 2 if has_runner else x0, cy,
                          x1, cy + h),
                    "🥉", "3-е место",
                    awards.get("bronze"), BRONZE,
                )

        elif kind == "awards":
            # 2-up grid of award cards.
            mid = (x0 + x1) // 2
            for idx, (slot, data) in enumerate(award_slots_present):
                row = idx // 2
                col = idx % 2
                ay0 = cy + row * (award_h_each + gap)
                ay1 = ay0 + award_h_each
                ax0 = x0 if col == 0 else mid + gap // 2
                ax1 = mid - gap // 2 if col == 0 else x1
                meta = _AWARD_META[slot]
                _draw_award_card(
                    img, (ax0, ay0, ax1, ay1),
                    icon=meta["icon"],
                    title=meta["title"],
                    name=(data or {}).get("label") or "—",
                    value=(data or {}).get("value") or "",
                    sub=(data or {}).get("sub") or "",
                    accent=meta["fg"],
                    name_color=meta["fg"],
                )

        elif kind == "spectacle":
            _draw_spectacle(img, (x0, cy, x1, cy + h), awards.get("spectacle"))

        elif kind == "totals":
            _draw_totals(img, (x0, cy, x1, cy + h), summary, totals)

        cy += h + gap

    out = BytesIO()
    img.save(out, format="PNG", optimize=True)
    return out.getvalue()


_AWARD_META: dict[str, dict] = {
    "attack":  {"icon": "⚽", "title": "Лучшая атака",   "fg": ATTACK_FG},
    "defense": {"icon": "🛡", "title": "Лучшая оборона", "fg": DEFENSE_FG},
    "scorer":  {"icon": "🥅", "title": "Бомбардир",      "fg": SCORER_FG},
    "winrate": {"icon": "🎯", "title": "Лучший % побед", "fg": WINRATE_FG},
}


__all__ = ["render_tournament_summary_png", "render_all_tournaments_overview_png"]




# ─────────────────────────────────────────────────────────────────────────────
# Cross-tournament comparison PNG: one image showing the global leaders
# across every finished tournament. Uses the same visual language as the
# per-tournament hero PNG.
# ─────────────────────────────────────────────────────────────────────────────

def _draw_leaderboard_card(
    img: Image.Image,
    box: tuple[int, int, int, int],
    *,
    icon: str,
    title: str,
    rows: list[tuple[str, str]],
    accent: tuple[int, int, int],
) -> None:
    """Generic ranked-list card: ``rows`` is a list of ``(left, right)``
    tuples like ``("@alice", "3 титула")``. Renders up to 5 entries —
    truncates long labels to fit the card. The right-aligned value
    auto-shrinks if it wouldn't otherwise leave room for the label."""
    draw = ImageDraw.Draw(img)
    _draw_card_shell(img, box, accent=accent)
    x0, y0, x1, y1 = box
    inner_x = x0 + _s(20)
    inner_w = (x1 - x0) - _s(40)

    f_label = _font(_s(13), bold=True)
    draw.text((inner_x, y0 + _s(12)),
              f"{icon}  {title.upper()}",
              font=f_label, fill=accent)

    f_row = _font(_s(15), bold=False)
    f_pos = _font(_s(15), bold=True)
    line_h = _s(22)
    base_y = y0 + _s(38)
    max_rows = max(0, ((y1 - y0) - _s(48)) // line_h)
    for i, (left, right) in enumerate(rows[:max_rows], 1):
        y = base_y + (i - 1) * line_h
        # Position prefix.
        draw.text((inner_x, y), f"{i}.", font=f_pos, fill=MUTED)
        # Right-anchored value — auto-shrink if value alone is wide.
        right_str, f_val = _fit_text_size(
            right, base_size=15, min_size=11,
            max_w=inner_w - _s(40), draw=draw, bold=True,
        )
        right_w = draw.textlength(right_str, font=f_val)
        draw.text((x1 - _s(20) - right_w, y), right_str, font=f_val, fill=TEXT)
        # Left-aligned label — auto-shrink so long @usernames stay
        # readable instead of being cut to "@vadimvadi…".
        avail = inner_w - _s(28) - right_w - _s(8)
        left_str, f_left = _fit_text_size(
            left, base_size=15, min_size=11,
            max_w=avail, draw=draw,
        )
        draw.text((inner_x + _s(28), y), left_str, font=f_left, fill=TEXT)


def render_all_tournaments_overview_png(overview: dict) -> bytes:
    """Render the comparison hero PNG. Returns PNG bytes."""
    width_px = _s(640)
    pad = _s(PAD)
    gap = _s(GAP)

    title_h = _s(120)
    summary_h = _s(96)
    leaderboard_h = _s(220)   # 5 rows visible
    notable_h = _s(96)
    footer_h = _s(60)

    # Decide which leaderboards have data.
    have_champs = bool(overview.get("champions"))
    have_apps = bool(overview.get("appearances"))
    have_scorers = bool(overview.get("scorers"))
    have_elo = bool(overview.get("elo"))
    have_biggest = bool(overview.get("biggest"))
    have_avg = bool(overview.get("highest_avg"))

    # Two columns × two rows of leaderboard cards (when all four exist).
    leaderboards: list[tuple[str, str, list[tuple[str, str]], tuple]] = []
    if have_champs:
        leaderboards.append((
            "🏆", "Больше всего титулов",
            [(c["label"], f"{c['titles']} титул(а)")
             for c in overview["champions"][:5]],
            GOLD,
        ))
    if have_apps:
        leaderboards.append((
            "🎯", "Больше всего участий",
            [(c["label"], f"{c['tournaments']} турнир(а)")
             for c in overview["appearances"][:5]],
            WINRATE_FG,
        ))
    if have_scorers:
        leaderboards.append((
            "⚽", "Бомбардиры всех времён",
            [(s["label"], f"{s['goals']} голов")
             for s in overview["scorers"][:5]],
            ATTACK_FG,
        ))
    if have_elo:
        leaderboards.append((
            "📈", "Топ ELO",
            [(e["label"], f"{e['elo']}")
             for e in overview["elo"][:5]],
            DEFENSE_FG,
        ))

    rows_count = (len(leaderboards) + 1) // 2
    leaderboard_block_h = (
        rows_count * leaderboard_h + max(0, rows_count - 1) * gap
    ) if leaderboards else 0

    notable_count = sum(1 for x in (have_biggest, have_avg) if x)
    notable_block_h = notable_h if notable_count else 0

    height_px = (
        pad * 2
        + title_h + gap
        + summary_h + gap
        + leaderboard_block_h
        + (gap + notable_block_h if notable_block_h else 0)
        + gap + footer_h
    )

    img = make_canvas(width_px, height_px, bg_color=BG)
    draw = ImageDraw.Draw(img)
    cy = pad

    # Title block.
    f_top = _font(_s(15), bold=True)
    draw.text((pad, cy), "📊  СРАВНЕНИЕ ВСЕХ ТУРНИРОВ",
              font=f_top, fill=ACCENT)
    f_name = _font(_s(28), bold=True)
    draw.text((pad, cy + _s(22)),
              f"{overview.get('total', 0)} завершённых турниров",
              font=f_name, fill=TEXT)
    f_meta = _font(_s(13), bold=False)
    by_type = overview.get("by_type") or {}
    if by_type:
        type_str = "  ·  ".join(
            f"{k.upper()}: {v}" for k, v in sorted(by_type.items())
        )
        draw.text((pad, cy + _s(60)), type_str, font=f_meta, fill=MUTED)
    draw.line(
        [(pad, cy + title_h - _s(8)), (width_px - pad, cy + title_h - _s(8))],
        fill=BORDER, width=_s(1),
    )
    cy += title_h + gap

    # Summary numbers strip.
    totals = overview.get("totals") or {}
    avg = (totals.get("goals") / totals.get("matches")
           if totals.get("matches") else 0.0)
    cells = [
        ("Игроков",  str(totals.get("players", 0))),
        ("Матчей",   str(totals.get("matches", 0))),
        ("Голов",    str(totals.get("goals", 0))),
        ("Ср. голы", f"{avg:.2f}"),
    ]
    box = (pad, cy, width_px - pad, cy + summary_h)
    _draw_card_shell(img, box, fill=CARD_DEEP)
    cw = (width_px - 2 * pad) / len(cells)
    f_lbl = _font(_s(11), bold=True)
    f_val = _font(_s(28), bold=True)
    for i, (lbl, val) in enumerate(cells):
        cx = pad + cw * (i + 0.5)
        draw.text((cx, cy + _s(14)), lbl, font=f_lbl,
                  fill=MUTED, anchor="mt")
        draw.text((cx, cy + _s(36)), val, font=f_val,
                  fill=TEXT, anchor="mt")
        if i < len(cells) - 1:
            sep_x = int(pad + cw * (i + 1))
            draw.line(
                [(sep_x, cy + _s(14)), (sep_x, cy + summary_h - _s(14))],
                fill=BORDER, width=_s(1),
            )
    cy += summary_h + gap

    # Leaderboard 2-up grid.
    mid = (pad + width_px - pad) // 2
    for idx, (icon, title, rows, accent) in enumerate(leaderboards):
        row = idx // 2
        col = idx % 2
        ay0 = cy + row * (leaderboard_h + gap)
        ay1 = ay0 + leaderboard_h
        ax0 = pad if col == 0 else mid + gap // 2
        ax1 = mid - gap // 2 if col == 0 else width_px - pad
        _draw_leaderboard_card(
            img, (ax0, ay0, ax1, ay1),
            icon=icon, title=title, rows=rows, accent=accent,
        )
    cy += leaderboard_block_h + (gap if leaderboard_block_h else 0)

    # Notable strip — biggest match + highest avg, side by side.
    if notable_block_h:
        items: list[tuple[str, str, str, tuple]] = []
        if have_biggest:
            big = overview["biggest"]
            items.append((
                "💥", "Крупнейший матч",
                f"{big['a']} {big['score']} {big['b']} · {big['tournament']}",
                SPECT_FG,
            ))
        if have_avg:
            avg_t = overview["highest_avg"]
            items.append((
                "🔥", "Самый голевой турнир",
                f"{avg_t['name']} — {avg_t['avg']:.2f} гола/матч",
                SCORER_FG,
            ))
        if len(items) == 1:
            box = (pad, cy, width_px - pad, cy + notable_h)
            icon, title, val, accent = items[0]
            _draw_card_shell(img, box, accent=accent, fill=CARD_BG)
            f_lbl = _font(_s(13), bold=True)
            f_val = _font(_s(20), bold=True)
            draw.text((pad + _s(20), cy + _s(14)),
                      f"{icon}  {title.upper()}",
                      font=f_lbl, fill=accent)
            draw.text((pad + _s(20), cy + _s(40)),
                      _truncate(val, f_val, width_px - 2 * pad - _s(40), draw),
                      font=f_val, fill=TEXT)
        else:
            for i, (icon, title, val, accent) in enumerate(items):
                ax0 = pad if i == 0 else mid + gap // 2
                ax1 = mid - gap // 2 if i == 0 else width_px - pad
                box = (ax0, cy, ax1, cy + notable_h)
                _draw_card_shell(img, box, accent=accent, fill=CARD_BG)
                f_lbl = _font(_s(13), bold=True)
                f_val = _font(_s(18), bold=True)
                draw.text((ax0 + _s(20), cy + _s(14)),
                          f"{icon}  {title.upper()}",
                          font=f_lbl, fill=accent)
                draw.text((ax0 + _s(20), cy + _s(40)),
                          _truncate(val, f_val, ax1 - ax0 - _s(40), draw),
                          font=f_val, fill=TEXT)
        cy += notable_block_h + gap

    # Footer.
    f_foot = _font(_s(11), bold=False)
    draw.text(
        (pad, cy + _s(20)),
        "Все цифры посчитаны по завершённым турнирам. "
        "ELO — глобальный рейтинг игроков.",
        font=f_foot, fill=MUTED,
    )

    out = BytesIO()
    img.save(out, format="PNG", optimize=True)
    return out.getvalue()




# ─────────────────────────────────────────────────────────────────────────────
# "А ВЫ ЗНАЛИ?" PNG — companion image with the top tournament facts.
# Sent alongside the hero PNG as a Telegram media-group so the chat
# always sees both at once.
# ─────────────────────────────────────────────────────────────────────────────


def _fact_accent(kind: str) -> tuple[int, int, int]:
    """Map a fact kind to a thematic accent colour. Unknown kinds fall
    back to the neutral teal accent."""
    palette: dict[str, tuple[int, int, int]] = {
        "scoring_streak":    (240, 130, 110),  # warm red
        "win_streak":        GOLD,
        "unbeaten":          (130, 220, 150),  # green
        "winless":           (160, 130, 130),  # muted brown
        "clean_sheet_king":  (130, 190, 230),  # cool blue
        "group_of_death":    (210, 160, 240),  # purple
        "goal_avalanche":    (240, 180, 110),  # orange
        "closest_pair":      (210, 160, 240),
        "hat_trick":         (255, 200, 90),   # gold-yellow
        "doubles":           (240, 200, 110),
        "goalfest":          (240, 130, 110),
        "conversion_king":   (130, 220, 150),
        "draw_specialist":   (170, 180, 195),
        "contribution_king": (255, 200, 90),
        "group_dominator":   (220, 160, 90),
        "low_scoring":       (130, 150, 170),
        # New (extended pool)
        "spectator_player":  (255, 170, 200),  # pink
        "no_goal_streak":    (140, 200, 230),
        "sniper":            (255, 130, 130),
        "underdog":          (180, 160, 130),
        "darkhorse":         (180, 220, 130),
        "ironman":           (220, 180, 100),
        "fast_start":        (130, 230, 200),
        "mood_swings":       (200, 140, 230),
        "lucky_one":         (140, 230, 160),
        "opener":            (200, 200, 220),
        "rivalry":           (240, 150, 150),
        "draw_group":        (190, 195, 210),
        "revenge":           (255, 170, 130),
        "close_runnerup":    (200, 210, 230),
        "tight_final":       (240, 220, 130),
        # Round 3 (extended pool — 12 more)
        "clean_win_king":         (255, 215, 100),  # gold-yellow
        "group_unbeaten":         (170, 230, 170),  # bright green
        "worst_group_defense":    (200, 130, 130),  # dim red
        "playoff_attacker":       (255, 140, 100),  # bright orange
        "playoff_defender":       (140, 200, 235),  # cool blue
        "playoff_blowout":        (240, 130, 110),  # warm red
        "multistage_scorer":      (180, 230, 200),  # mint
        "stability_king":         (200, 220, 170),  # olive
        "closer":                 (210, 200, 230),  # light purple
        "ascending_intensity":    (160, 230, 130),  # lime
        "descending_intensity":   (180, 200, 220),  # cool grey
        "defensive_rivalry":      (170, 210, 230),  # ice blue
        "tournament_duration":    (200, 200, 200),  # neutral
    }
    return palette.get(kind, ACCENT)


def render_tournament_facts_png(
    summary: dict,
    tournament: Optional[dict] = None,
    top: int = 6,
    seed: Optional[int] = None,
) -> Optional[bytes]:
    """Render the companion "🎲 А ВЫ ЗНАЛИ?" PNG with the top ``top``
    facts. Returns ``None`` when ``summary['facts']`` is empty (caller
    just sends the hero alone in that case).

    ``seed`` lets the "🎲 Ещё факты" button reshuffle which subset is
    drawn while keeping the highest-scoring facts always visible.
    Behaviour: facts above the rank-3 quality floor are always shown
    in their natural order; the remaining slots are filled from the
    next tier with weighted-random sampling so reshuffling brings new
    surprises without dropping the headline numbers.
    """
    import random

    facts = list(summary.get("facts") or [])
    if not facts:
        return None

    # Use the shared diversity selector so the image and the .txt
    # digest both honour the same "no fact-spam from one player" rule.
    # Champion is already shown on the hero PNG, so cap them tightly
    # in the secondary panel.
    from tournament_summary import select_top_facts
    champion_label = ((summary.get("podium") or {}).get("first") or {}).get("label")
    chosen = select_top_facts(
        facts,
        n=top,
        max_per_subject=1,
        champion_label=champion_label,
        champion_max=2,
        seed=seed,
    )

    width_px = _s(WIDTH)
    pad = _s(PAD)
    gap = _s(GAP)

    title_h = _s(96)
    fact_h = _s(122)
    rows_count = (len(chosen) + 1) // 2
    grid_h = rows_count * fact_h + max(0, rows_count - 1) * gap
    footer_h = _s(64)

    height_px = pad * 2 + title_h + gap + grid_h + gap + footer_h

    bg_image_path = (tournament or {}).get("bg_image_path") if tournament else None
    bg_image_data = (tournament or {}).get("bg_image_data") if tournament else None
    overlay_alpha = int((tournament or {}).get("bg_overlay_alpha") or 200)
    img = make_canvas(
        width_px, height_px,
        bg_color=BG,
        bg_image_path=bg_image_path,
        bg_image_data=bg_image_data,
        overlay_alpha=overlay_alpha,
    )
    draw = ImageDraw.Draw(img)
    cy = pad

    # Title block.
    f_top = _font(_s(15), bold=True)
    draw.text((pad, cy), "🎲  А ВЫ ЗНАЛИ?", font=f_top, fill=ACCENT)
    f_name = _font(_s(28), bold=True)
    draw.text(
        (pad, cy + _s(22)),
        _truncate(summary.get("name") or "—", f_name, width_px - 2 * pad, draw),
        font=f_name, fill=TEXT,
    )
    f_meta = _font(_s(13), bold=False)
    draw.text(
        (pad, cy + _s(60)),
        f"Самые любопытные цифры этого турнира · "
        f"{summary.get('total_players', 0)} игроков · "
        f"{summary.get('total_matches', 0)} матчей",
        font=f_meta, fill=MUTED,
    )
    draw.line(
        [(pad, cy + title_h - _s(6)), (width_px - pad, cy + title_h - _s(6))],
        fill=BORDER, width=_s(1),
    )
    cy += title_h + gap

    # 2-up grid of facts.
    mid = (pad + width_px - pad) // 2
    for idx, fact in enumerate(chosen):
        row = idx // 2
        col = idx % 2
        ay0 = cy + row * (fact_h + gap)
        ay1 = ay0 + fact_h
        ax0 = pad if col == 0 else mid + gap // 2
        ax1 = mid - gap // 2 if col == 0 else width_px - pad
        accent = _fact_accent(fact.get("kind") or "")
        _draw_award_card(
            img, (ax0, ay0, ax1, ay1),
            icon=fact.get("icon") or "•",
            title=fact.get("title") or "",
            name=fact.get("label") or "—",
            value=fact.get("value") or "",
            sub=fact.get("sub") or "",
            accent=accent,
            name_color=accent,
        )
    cy += grid_h + gap

    # Footer with reshuffle hint.
    f_foot = _font(_s(11), bold=False)
    total_facts = len(facts)
    if total_facts > top:
        hint = (f"Из {total_facts} найденных фактов показаны {len(chosen)}. "
                f"Нажми «🎲 Ещё факты», чтобы покрутить колесо.")
    else:
        hint = f"Все {total_facts} интересных факта на этом турнире."
    draw.text((pad, cy + _s(20)), hint, font=f_foot, fill=MUTED)

    out = BytesIO()
    img.save(out, format="PNG", optimize=True)
    return out.getvalue()
