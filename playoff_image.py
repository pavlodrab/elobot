"""
Render the playoff bracket as a PNG image so ``/playoff`` answers with a
picture instead of a monospaced text block (mobile clients render those
inconsistently).

Public entry point: ``render_playoff_png(tid) -> bytes`` returning a PNG
ready to pass to ``Bot.send_photo`` / ``Message.reply_photo``.

Layout: each playoff stage (r16 → qf → sf → final) becomes a vertical
column of pair cards, ordered left-to-right. A pair card shows both
players' display names, the per-leg scores (or ``ожидается`` / ``в
работе``) and — once everything is confirmed — the aggregate score with
the winner highlighted.

Design notes:
* Same dark palette as ``standings_image.py`` so the bot's PNGs look
  consistent in chat.
* Renders at 2× scale to dodge Telegram's photo-thumbnail blur on
  mobile.
* If a stage has no matches, the column is skipped — so the bracket
  scales to whatever the tournament actually has (just SF + Final, full
  R16 → Final, etc.).
"""
from __future__ import annotations

import re
from io import BytesIO

from PIL import Image, ImageDraw, ImageFont

from bg_helper import make_canvas
from database import get_player_by_id, get_tournament, get_tournament_matches, get_tournament_players
from tournament import (
    PLAYOFF_STAGES,
    _pair_key,
    _resolve_pair_winner,
    get_stage_config,
)



# ── Per-render team-tag cache ───────────────────────────────────────────────
# Populated by ``_load_tag_map(tid)`` at the start of every public
# ``render_playoff_png(s)`` call and consumed by ``_display_name`` so
# we don't have to plumb a ``tag_by_pid`` dict through every helper
# (``_render_card`` / ``_render_card_dyn`` / ``_resolve_name`` etc).
# Single-threaded asyncio worker → no race conditions in practice.
_TAG_BY_PID: dict[int, str] = {}

# Per-render display-mode cache. Mirrors ``_TAG_BY_PID``: populated at
# the top of every public ``render_playoff_png(s)`` call from
# ``tournaments.name_display_mode`` and consumed by ``_display_name``
# without us having to thread it through the ``_render_card`` chain.
# Single-threaded asyncio worker → no race conditions in practice.
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


# ── palette (matches standings_image.py) ────────────────────────────────────
BG          = (28, 30, 38)
CARD_BG     = (40, 44, 54)
CARD_DONE   = (52, 92, 64)
CARD_LIVE   = (66, 86, 124)
HEADER_BG   = (54, 88, 144)
HEADER_TXT  = (255, 255, 255)
TEXT        = (235, 238, 245)
MUTED       = (170, 180, 195)
BORDER      = (60, 65, 78)
WIN         = (90, 200, 130)
LOSS        = (200, 110, 110)

# Bronze palette — used for the optional 3rd-place fixture so the
# bronze card visually stands out from the main bracket. Tones picked
# to read as "warm bronze" against the dark BG while still preserving
# the WIN/LOSS contrast on confirmed series.
BRONZE_BG     = (62, 50, 38)   # pending / not-yet-played bronze card
BRONZE_DONE   = (140, 90, 50)  # confirmed bronze card
BRONZE_LIVE   = (104, 76, 48)  # mid-state (reported but not confirmed)
BRONZE_BORDER = (122, 84, 50)
BRONZE_LABEL  = (220, 170, 110)  # the "🥉 Матч за 3-е место" sub-header


# ── geometry (1× — multiplied by SCALE on render) ───────────────────────────
# Small brackets (≤8 pairs): rich layout at 2× scale.
# Big brackets (>8 pairs): split into halves or compact layout at 1×
# scale so we fit the Telegram photo limit (width+height ≤ 10000 px).
SCALE         = 2

PAD           = 24
TITLE_BLOCK   = 110

COL_W         = 320       # width of a stage column
COL_GAP       = 28
CARD_H        = 132
CARD_GAP      = 24
STAGE_HEAD_H  = 48

# ── compact geometry (used for big brackets, SCALE=1) ───────────────────────
COMPACT_PAD          = 18
COMPACT_TITLE_BLOCK  = 78
COMPACT_COL_W        = 240
COMPACT_COL_GAP      = 16
COMPACT_CARD_H       = 80
COMPACT_CARD_GAP     = 12
COMPACT_STAGE_HEAD_H = 32


# ── font loading (mirrors standings_image.py) ───────────────────────────────
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


def _s(v: int) -> int:
    return int(v * SCALE)


def _draw_rect_alpha(
    img: Image.Image,
    box: list | tuple,
    fill: tuple[int, int, int],
    alpha: int = 255,
) -> None:
    """Draw a filled rectangle with variable opacity onto an RGB image.

    Mirrors ``standings_image._draw_rect_alpha`` so the playoff bracket
    can respect the same ``row_bg_alpha`` knob (``/set_row_alpha``).
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


def _draw_rounded_rect_alpha(
    img: Image.Image,
    box: list | tuple,
    *,
    radius: int,
    fill: tuple[int, int, int],
    outline: tuple[int, int, int] | None = None,
    width: int = 1,
    alpha: int = 255,
) -> None:
    """Draw a filled rounded rectangle that respects ``alpha``.

    The outline is always opaque so the card edge stays crisp even when
    the body fades out — this matches how the standings rows look when
    ``row_bg_alpha`` is dialled down.
    """
    x0, y0, x1, y1 = int(box[0]), int(box[1]), int(box[2]), int(box[3])
    w = x1 - x0
    h = y1 - y0
    if w <= 0 or h <= 0:
        return
    if alpha >= 255:
        ImageDraw.Draw(img).rounded_rectangle(
            box, radius=radius, fill=fill,
            outline=outline, width=width,
        )
        return
    if alpha <= 0:
        if outline is not None and width > 0:
            ImageDraw.Draw(img).rounded_rectangle(
                box, radius=radius, fill=None,
                outline=outline, width=width,
            )
        return
    overlay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ImageDraw.Draw(overlay).rounded_rectangle(
        [0, 0, w, h], radius=radius, fill=fill + (alpha,),
    )
    img.paste(
        Image.alpha_composite(
            img.crop((x0, y0, x1, y1)).convert("RGBA"),
            overlay,
        ).convert("RGB"),
        (x0, y0),
    )
    if outline is not None and width > 0:
        ImageDraw.Draw(img).rounded_rectangle(
            box, radius=radius, fill=None,
            outline=outline, width=width,
        )


def _truncate(
    text: str,
    font: ImageFont.ImageFont,
    max_w: int,
    draw: ImageDraw.ImageDraw,
) -> str:
    """Trim ``text`` with an ellipsis to fit ``max_w`` pixels.

    Emoji-aware via :mod:`emoji_helper` so we don't slice through a
    flag glyph or miscount the rendered width when team-tagged names
    contain 🇩🇪 / 🐐 etc.
    """
    from emoji_helper import truncate_text_with_emoji
    return truncate_text_with_emoji(text, font, max_w)


def _display_name(p: dict | None, *, fallback: str = "?", team_tag: str = "") -> str:
    """Bracket-card name. Hides synthetic ``id_<digits>`` placeholders so
    we never render an unclickable ``@id_77777`` for a user without a
    public ``@username`` — they show as their game nickname (or
    ``id 77777``) instead.

    When ``team_tag`` is provided (per-tournament tag from
    ``tournament_players``), it's woven into the rendered name as
    ``"<nick> - <Team> (@user)"`` so playoff bracket cards make team
    affiliations visible at a glance. If ``team_tag`` is empty AND
    the module-level ``_TAG_BY_PID`` cache (populated by
    ``render_playoff_png(s)``) has an entry for this player, that
    cached value is used — saves plumbing the tag map through every
    intermediate ``_render_card`` call.

    Honours the per-tournament ``_NAME_MODE`` override (``"full"`` /
    ``"tag"`` / ``"nick"``) so admins can ditch Telegram handles for
    pure-nickname or pure-team-name bracket cards via "🎨 Оформление"
    → "🪪 Имена".
    """
    tag = (team_tag or "").strip()
    if not tag and p is not None:
        pid_local = p.get("id")
        if isinstance(pid_local, int):
            tag = (_TAG_BY_PID.get(pid_local) or "").strip()
    if not p:
        if tag:
            return tag
        return fallback
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

    # mode == "full" — original behaviour, preserved verbatim.
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


def _stage_label(stage: str) -> str:
    return {
        "r512":  "1/256 финала",
        "r256":  "1/128 финала",
        "r128":  "1/64 финала",
        "r64":   "1/32 финала",
        "r32":   "1/16 финала",
        "r16":   "1/8 финала",
        "qf":    "Четвертьфинал",
        "sf":    "Полуфинал",
        "final": "Финал",
        "third": "Матч за 3-е место",
    }.get(stage, stage.upper())


def _collect_third_place(tid: int) -> list[list[dict]]:
    """Return the 3rd-place fixture rows as a single-pair list, or [].

    Mirrors ``_collect_pairs`` (dedup by (pair, leg), keep highest id,
    sort each pair by leg) but only for the ``third`` stage. The result
    is wrapped so callers can feed it directly into ``_render_card``.
    """
    rows = get_tournament_matches(tid, stage="third")
    if not rows:
        return []
    leg_dedup: dict[tuple, dict] = {}
    for m in rows:
        key = (_pair_key(m), int(m.get("leg") or 1))
        cur = leg_dedup.get(key)
        if cur is None or (m.get("id") or 0) > (cur.get("id") or 0):
            leg_dedup[key] = m
    pairs: dict[tuple[int, int], list[dict]] = {}
    for m in leg_dedup.values():
        pairs.setdefault(_pair_key(m), []).append(m)
    return [
        sorted(ms, key=lambda x: int(x.get("leg") or 1))
        for ms in pairs.values()
    ]


def _collect_pairs(tid: int) -> list[tuple[str, list[list[dict]]]]:
    """Group playoff matches by stage → pair → legs (sorted).

    Within each (pair, leg) bucket we keep only the highest-id row, the
    same dedup we apply elsewhere to swallow phantom duplicates.
    """
    out: list[tuple[str, list[list[dict]]]] = []
    for s in PLAYOFF_STAGES:
        matches = get_tournament_matches(tid, stage=s)
        if not matches:
            continue
        # Dedup duplicate legs by (pair, leg).
        leg_dedup: dict[tuple, dict] = {}
        for m in matches:
            key = (_pair_key(m), int(m.get("leg") or 1))
            cur = leg_dedup.get(key)
            if cur is None or (m.get("id") or 0) > (cur.get("id") or 0):
                leg_dedup[key] = m
        pairs: dict[tuple[int, int], list[dict]] = {}
        for m in leg_dedup.values():
            pairs.setdefault(_pair_key(m), []).append(m)
        pair_list: list[list[dict]] = []
        for ms in pairs.values():
            pair_list.append(sorted(ms, key=lambda x: int(x.get("leg") or 1)))
        out.append((s, pair_list))
    return out


def _collect_pairs_full(tid: int) -> list[tuple[str, list[list[dict]]]]:
    """Like ``_collect_pairs`` but normalises every stage to its full
    bracket-slot order and pads missing slots with TBD placeholders.

    Two things this fixes over the raw ``_collect_pairs`` output:

    1. **Stable slot order.** Later stages are spawned incrementally as
       pairs resolve, so their DB order reflects *finish order*, not the
       bracket slot. We rebuild the canonical top → bottom order by
       lineage (a pair fed by previous-stage slots 2k / 2k+1 lands in
       slot k). Without this the mirrored renderer could put the
       lower-half semifinal on the left and the upper-half one on the
       right whenever the lower half happened to finish first.

    2. **Partial-stage padding.** A stage that's only *half* spawned
       (e.g. one semifinal exists because only the top-half quarters are
       done) is padded out to its full slot count with TBD placeholders,
       so the not-yet-created second semifinal is still visible — in its
       correct (right-hand) position — instead of appearing only once
       the last quarterfinal is played. Fully-missing downstream stages
       (incl. the Final) are padded the same way.

    Each TBD pair is a one-element list holding a sentinel match dict
    ``{"_tbd": True, "_slot": k, "_partial_winner_a/b": <pid|None>}``.
    ``_render_card`` / ``_render_card_dyn`` detect the flag and draw an
    empty placeholder card, showing any already-known feeder winner on
    the relevant side (so the Final reads ``@phoenileo vs TBD`` as soon
    as one semifinal closes).
    """
    real = _collect_pairs(tid)
    if not real:
        return real

    # Index real stages by name, preserving order, so we can fill any
    # gap (including a stage that finished but didn't auto-advance).
    real_by_stage: dict[str, list[list[dict]]] = {s: ps for s, ps in real}
    earliest_idx = min(PLAYOFF_STAGES.index(s) for s in real_by_stage)

    # Advance mode + bo-N series length come from the per-stage config
    # so the projected winners match what advance_playoff will spawn
    # for the next stage. Early-stop "wins" mode allows the resolver to
    # declare a winner without every scheduled leg being confirmed.
    t = get_tournament(tid) or {}

    def _pair_winner_or_none(ms: list[dict] | None, stage: str) -> int | None:
        if not ms or ms[0].get("_tbd"):
            return None
        try:
            cfg = get_stage_config(t, stage)
        except Exception:
            cfg = {"len": 1, "mode": (t.get("playoff_advance_mode") or "goals").lower()}
        try:
            return _resolve_pair_winner(
                ms,
                advance_mode=cfg["mode"],
                series_len=cfg["len"],
            )
        except Exception:
            return None

    def _pair_player_ids(ms: list[dict] | None) -> set[int]:
        """Player ids in a real pair (empty for TBD placeholders)."""
        if not ms or ms[0].get("_tbd"):
            return set()
        out_ids: set[int] = set()
        for key in ("player1_id", "player2_id"):
            pid = ms[0].get(key)
            if isinstance(pid, int):
                out_ids.add(pid)
        return out_ids

    def _player_slot_map(slot_pairs: list[list[dict]]) -> dict[int, int]:
        """Map every (real) player id to the bracket slot of its pair."""
        m: dict[int, int] = {}
        for slot, ms in enumerate(slot_pairs):
            for pid in _pair_player_ids(ms):
                m[pid] = slot
        return m

    # ── Canonical bracket ordering ──────────────────────────────────────
    # The earliest populated stage is created by ``generate_playoff`` in
    # standard bracket order (top → bottom) and ``get_tournament_matches``
    # returns rows ``ORDER BY id``, so its pairs are already slot-ordered.
    # Later stages, however, are spawned *incrementally* as pairs resolve
    # — so their DB order reflects which match finished first, NOT the
    # bracket slot. We rebuild the slot order by lineage: the pair whose
    # players advanced from previous-stage slots 2k / 2k+1 belongs to
    # slot k of the current stage. Missing slots are filled with TBD
    # placeholders (projecting the known feeder winners) so a half-played
    # stage shows ALL its slots in the right left/right position instead
    # of collapsing to whatever subset has spawned.
    base_stage = PLAYOFF_STAGES[earliest_idx]
    base_pairs = real_by_stage[base_stage]

    out: list[tuple[str, list[list[dict]]]] = [(base_stage, list(base_pairs))]
    prev_slot_pairs: list[list[dict]] = list(base_pairs)
    prev_player_slot = _player_slot_map(prev_slot_pairs)
    prev_stage = base_stage
    expected_count = len(base_pairs)

    for stage in PLAYOFF_STAGES[earliest_idx + 1:]:
        expected_count = max(1, expected_count // 2)
        real_pairs = real_by_stage.get(stage, [])

        slot_pairs: list[list[dict] | None] = [None] * expected_count
        unplaced: list[list[dict]] = []
        for ms in real_pairs:
            # Slot = previous-stage slot of either participant, halved.
            cand = [
                prev_player_slot[pid] // 2
                for pid in _pair_player_ids(ms)
                if pid in prev_player_slot
            ]
            slot = min(cand) if cand else None
            if (
                slot is not None
                and 0 <= slot < expected_count
                and slot_pairs[slot] is None
            ):
                slot_pairs[slot] = ms
            else:
                unplaced.append(ms)

        # Defensive: any pair we couldn't position by lineage (corrupt
        # data, manual edits) drops into the first free slot so it's at
        # least visible rather than silently lost.
        for ms in unplaced:
            for k in range(expected_count):
                if slot_pairs[k] is None:
                    slot_pairs[k] = ms
                    break

        # Fill the gaps with TBD placeholders, projecting the winners of
        # the two previous-stage slots that feed each empty slot.
        for k in range(expected_count):
            if slot_pairs[k] is not None:
                continue
            w_top = w_bot = None
            if 2 * k < len(prev_slot_pairs):
                w_top = _pair_winner_or_none(prev_slot_pairs[2 * k], prev_stage)
            if 2 * k + 1 < len(prev_slot_pairs):
                w_bot = _pair_winner_or_none(prev_slot_pairs[2 * k + 1], prev_stage)
            slot_pairs[k] = [{
                "_tbd": True,
                "leg": 1,
                "_slot": k,
                "_partial_winner_a": w_top,
                "_partial_winner_b": w_bot,
            }]

        resolved: list[list[dict]] = [ms for ms in slot_pairs if ms is not None]
        out.append((stage, resolved))
        prev_slot_pairs = resolved
        prev_player_slot = _player_slot_map(resolved)
        prev_stage = stage

    return out


def _aggregate_score(ms: list[dict], a_id: int, b_id: int) -> tuple[int, int]:
    a_goals = b_goals = 0
    for m in ms:
        if m.get("score1") is None or m.get("score2") is None:
            continue
        if m["player1_id"] == a_id:
            a_goals += int(m["score1"])
            b_goals += int(m["score2"])
        else:
            a_goals += int(m["score2"])
            b_goals += int(m["score1"])
    return a_goals, b_goals


def _layout_leg_rows(
    draw: ImageDraw.ImageDraw,
    leg_lines: list[str],
    ms: list[dict],
    a_id: int,
    inner_w: int,
    leg_font,
) -> list[str]:
    """Pack per-leg score labels into at most two rows that fit ``inner_w``.

    Layout — column-major, 2 rows max::

        L1: 3:3   L3: 3:3   L5: 2:1
        L2: 3:3   L4: 3:3

    Why columns and not a single wrap-and-flow row? When a series goes 4+
    legs ``…  ·  …  ·  …  ·  …`` overflows the card and the renderer used
    to silently cut the tail with ``…``, hiding the decider leg. Two rows
    grouped by column keep odd legs (L1/L3/L5) above their corresponding
    even legs (L2/L4/—) so the reader sees the chronological pairing of
    the bo2 series at a glance.

    Single-leg series stay on one row (no column grid needed). If even
    the column grid won't fit ``inner_w`` we fall back to truncated
    score-only join as a last resort.
    """
    if len(leg_lines) <= 1:
        return leg_lines or [""]

    # Try the labelled single-row form first — preserves the original
    # appearance for short series (2-3 legs that fit on one line).
    sep_inline = "  ·  "
    inline = sep_inline.join(leg_lines)
    if len(leg_lines) <= 3 and draw.textlength(
        inline, font=leg_font
    ) <= inner_w:
        return [inline]

    # Column-major: leg #1 above leg #2, leg #3 above leg #4, …
    # ``leg_lines`` is already in ascending leg order, so:
    #   col k → (leg_lines[2k], leg_lines[2k+1] if exists)
    columns: list[tuple[str, str]] = []
    for i in range(0, len(leg_lines), 2):
        top = leg_lines[i]
        bot = leg_lines[i + 1] if i + 1 < len(leg_lines) else ""
        columns.append((top, bot))

    col_gap = "   "
    # Each column's cell width = max of its two strings (so they align
    # vertically). Pad each cell to that width before joining columns.
    def _w(s: str) -> float:
        return draw.textlength(s, font=leg_font) if s else 0.0

    gap_w = _w(col_gap)
    total_w = 0.0
    cell_widths: list[float] = []
    for top, bot in columns:
        cw = max(_w(top), _w(bot))
        cell_widths.append(cw)
        total_w += cw
    total_w += gap_w * (len(columns) - 1)

    if total_w <= inner_w:
        # Build padded rows using monospace-style space padding (works
        # fine for digit/punctuation-heavy strings like "L1: 3:3").
        def _pad_to(s: str, target_w: float) -> str:
            if not s:
                return ""
            space_w = _w(" ") or 1.0
            cur_w = _w(s)
            n = max(0, int((target_w - cur_w) / space_w))
            return s + (" " * n)

        top_row = col_gap.join(
            _pad_to(c[0], cell_widths[i]) for i, c in enumerate(columns)
        )
        bot_row = col_gap.join(
            _pad_to(c[1], cell_widths[i]) for i, c in enumerate(columns)
        )
        rows = [top_row.rstrip()]
        if bot_row.strip():
            rows.append(bot_row.rstrip())
        return rows

    # Column grid too wide — fall back to compact score-only form (no
    # "L#:" prefix) on at most two rows.
    short_lines: list[str] = []
    for m in ms:
        leg_no = int(m.get("leg") or 1)
        status = (m.get("status") or "pending").lower()
        if status == "confirmed":
            if m["player1_id"] == a_id:
                short_lines.append(f"{m['score1']}:{m['score2']}")
            else:
                short_lines.append(f"{m['score2']}:{m['score1']}")
        elif status == "reported":
            short_lines.append(f"L{leg_no}·?")
        else:
            short_lines.append(f"L{leg_no}·⏳")

    short_joined = sep_inline.join(short_lines)
    if draw.textlength(short_joined, font=leg_font) <= inner_w:
        return [short_joined]

    rows: list[str] = []
    cur: list[str] = []
    for tok in short_lines:
        candidate = sep_inline.join(cur + [tok]) if cur else tok
        if draw.textlength(candidate, font=leg_font) <= inner_w:
            cur.append(tok)
        else:
            if cur:
                rows.append(sep_inline.join(cur))
            cur = [tok]
    if cur:
        rows.append(sep_inline.join(cur))

    if len(rows) > 2:
        rows = rows[:2]
        rows[-1] = _truncate(rows[-1] + " …", leg_font, inner_w, draw)
    return rows or [_truncate(short_joined, leg_font, inner_w, draw)]


def _render_card(
    draw: ImageDraw.ImageDraw,
    x: int, y: int, w: int, h: int,
    ms: list[dict],
    *,
    name_font, score_font, leg_font, badge_font,
    advance_mode: str = "goals",
    series_len: int | None = None,
):
    # ── TBD placeholder card (no real match yet) ────────────────────────
    if ms and ms[0].get("_tbd"):
        draw.rounded_rectangle(
            [x, y, x + w, y + h],
            radius=_s(10), fill=CARD_BG, outline=BORDER, width=_s(1),
        )
        pad = _s(14)
        inner_w = w - pad * 2
        wa = ms[0].get("_partial_winner_a")
        wb = ms[0].get("_partial_winner_b")

        def _resolve_name(pid: int | None) -> tuple[str, tuple]:
            if pid is None:
                return "TBD", MUTED
            p = get_player_by_id(pid)
            return _display_name(p, fallback=f"id{pid}"), TEXT

        name_a, col_a = _resolve_name(wa)
        name_b, col_b = _resolve_name(wb)
        draw.text(
            (x + pad, y + _s(10)),
            _truncate(name_a, name_font, inner_w, draw),
            font=name_font, fill=col_a,
        )
        draw.text(
            (x + pad, y + _s(34)),
            _truncate(name_b, name_font, inner_w, draw),
            font=name_font, fill=col_b,
        )
        # Hint changes depending on whether anything is decided yet.
        if wa is None and wb is None:
            hint_text = "ожидаем предыдущую стадию"
        elif wa is not None and wb is not None:
            # Both feeders decided but the actual match row not spawned
            # yet (advance_playoff runs on the next confirm). Show a
            # neutral hint.
            hint_text = "ожидаем старт стадии"
        else:
            hint_text = "ждёт соперника"
        draw.text(
            (x + pad, y + _s(64)),
            _truncate(hint_text, leg_font, inner_w, draw),
            font=leg_font, fill=MUTED,
        )
        return

    a_id = ms[0]["player1_id"]
    b_id = ms[0]["player2_id"]
    pa = get_player_by_id(a_id)
    pb = get_player_by_id(b_id)
    name_a = _display_name(pa, fallback=f"id{a_id}")
    name_b = _display_name(pb, fallback=f"id{b_id}")

    all_done = all((m.get("status") or "") == "confirmed" for m in ms)
    fill = CARD_DONE if all_done else (CARD_LIVE if any(
        (m.get("status") or "") == "reported" for m in ms
    ) else CARD_BG)

    draw.rounded_rectangle(
        [x, y, x + w, y + h],
        radius=_s(10), fill=fill, outline=BORDER, width=_s(1),
    )

    # ── leg strip and aggregate ─────────────────────────────────────────
    leg_lines: list[str] = []
    for m in ms:
        leg_no = int(m.get("leg") or 1)
        status = (m.get("status") or "pending").lower()
        if status == "confirmed":
            if m["player1_id"] == a_id:
                leg_lines.append(f"L{leg_no}: {m['score1']}:{m['score2']}")
            else:
                leg_lines.append(f"L{leg_no}: {m['score2']}:{m['score1']}")
        elif status == "reported":
            leg_lines.append(f"L{leg_no}: ожидает подтв.")
        else:
            leg_lines.append(f"L{leg_no}: ⏳ ожидается")

    pad = _s(14)
    inner_w = w - pad * 2

    name_a_clip = _truncate(name_a, name_font, inner_w, draw)
    name_b_clip = _truncate(name_b, name_font, inner_w, draw)

    a_color = TEXT
    b_color = TEXT
    badge = ""
    if all_done:
        winner_id = _resolve_pair_winner(
            ms, advance_mode=advance_mode, series_len=series_len,
        )
        if winner_id == a_id:
            a_color = WIN; b_color = LOSS
            badge = "🏅"
        elif winner_id == b_id:
            a_color = LOSS; b_color = WIN
            badge = "🏅"

    # Top-line: name A
    _em_base = getattr(draw, "_em_base", None)
    if _em_base is not None:
        from emoji_helper import draw_text_with_emoji
        draw_text_with_emoji(
            _em_base, (x + pad, y + _s(10)),
            name_a_clip, name_font, fill=a_color,
        )
        draw_text_with_emoji(
            _em_base, (x + pad, y + _s(34)),
            name_b_clip, name_font, fill=b_color,
        )
    else:
        draw.text((x + pad, y + _s(10)), name_a_clip, font=name_font, fill=a_color)
        draw.text((x + pad, y + _s(34)), name_b_clip, font=name_font, fill=b_color)

    # Aggregate score (right-aligned in upper-right corner) when all legs done.
    if all_done:
        a_g, b_g = _aggregate_score(ms, a_id, b_id)
        agg_text = f"{a_g} : {b_g}"
        agg_w = draw.textlength(agg_text, font=score_font)
        draw.text(
            (x + w - pad - agg_w, y + _s(8)),
            agg_text, font=score_font, fill=HEADER_TXT,
        )

    # Per-leg detail row (under names). Wrap to 2 lines when the
    # labelled form ("L1: 3:3 · L2: …") would overflow — otherwise a
    # 5-leg final-decider series would have its tail truncated to "…"
    # and the user couldn't see the decisive score.
    leg_rows = _layout_leg_rows(draw, leg_lines, ms, a_id, inner_w, leg_font)
    base_y = y + _s(64)
    line_h = _s(18)
    for i, row in enumerate(leg_rows):
        draw.text(
            (x + pad, base_y + i * line_h),
            row, font=leg_font, fill=MUTED,
        )

    # Status pill in bottom-right.
    if badge:
        draw.text(
            (x + w - pad - draw.textlength(badge, font=badge_font),
             y + h - pad - _s(20)),
            badge, font=badge_font, fill=HEADER_TXT,
        )
    else:
        if any((m.get("status") or "") != "confirmed" for m in ms):
            label = "в работе" if any(
                (m.get("status") or "") == "reported" for m in ms
            ) else "ожидается"
            tw = draw.textlength(label, font=leg_font)
            draw.text(
                (x + w - pad - tw, y + h - pad - _s(20)),
                label, font=leg_font, fill=MUTED,
            )


# ── public API ──────────────────────────────────────────────────────────────
# Telegram photo limit is ~10000 px on any side. We split big brackets
# (more than ``_SPLIT_THRESHOLD_PAIRS`` pairs in the first stage) into
# top + bottom halves so each piece fits.
_SPLIT_THRESHOLD_PAIRS = 32   # > 32 pairs in first stage triggers a 2-image cut
_FINAL_STAGE = "final"


def _render_image(
    t: dict,
    stages: list[tuple[str, list[list[dict]]]],
    *,
    half_label: str = "",
    compact: bool = False,
    third_pairs: list[list[dict]] | None = None,
) -> bytes:
    """Render one stage list into a PNG.

    ``half_label`` (e.g. "верхняя половина") is appended to the subtitle
    so the user can tell halves apart. ``compact=True`` switches to the
    smaller card layout used for big brackets (anything that wouldn't
    fit Telegram's photo size limit at the default 2× rendering).

    ``third_pairs`` carries the optional 3rd-place fixture (single pair,
    possibly multiple legs); when non-empty it's rendered as a bronze-
    tinted card directly below the Final card in the same column,
    with a small "🥉 Матч за 3-е место" sub-label between them.
    """
    # NB: we deliberately do NOT append ("third", third_pairs) as an
    # extra column — the bronze fixture is rendered inside the Final
    # column, beneath the Final card, by the loop below.
    if compact:
        # Smaller geometry, SCALE=1.
        scale = 1
        pad         = COMPACT_PAD
        title_block = COMPACT_TITLE_BLOCK
        col_w_1x    = COMPACT_COL_W
        col_gap     = COMPACT_COL_GAP
        card_h      = COMPACT_CARD_H
        card_gap    = COMPACT_CARD_GAP
        stage_head  = COMPACT_STAGE_HEAD_H
        title_font  = ImageFont.truetype(_BOLD_PATHS[0], 26) if _BOLD_PATHS else _font(26, True)
        sub_font    = _font(15, bold=False)
        stage_font  = _font(16, bold=True)
        name_font   = _font(15, bold=True)
        score_font  = _font(15, bold=True)
        leg_font    = _font(12, bold=False)
        badge_font  = _font(15, bold=True)
    else:
        scale = SCALE
        pad         = PAD
        title_block = TITLE_BLOCK
        col_w_1x    = COL_W
        col_gap     = COL_GAP
        card_h      = CARD_H
        card_gap    = CARD_GAP
        stage_head  = STAGE_HEAD_H
        title_font  = _font(_s(34), bold=True)
        sub_font    = _font(_s(20), bold=False)
        stage_font  = _font(_s(22), bold=True)
        name_font   = _font(_s(22), bold=True)
        score_font  = _font(_s(22), bold=True)
        leg_font    = _font(_s(18), bold=False)
        badge_font  = _font(_s(22), bold=True)

    def s(v: int) -> int:
        return int(v * scale)

    n_cols = max(1, len(stages))
    width = s(pad) * 2 + n_cols * s(col_w_1x) + (n_cols - 1) * s(col_gap)
    # Vertical room consumed by the bronze "🥉 Матч за 3-е место"
    # sub-label + the bronze card itself, when a 3rd-place fixture is
    # present. Used both for canvas-height sizing and to position the
    # bronze card inside the Final column further down.
    bronze_label_h = s(20 if compact else 28)
    bronze_block_h = (
        bronze_label_h + s(card_h) + s(card_gap)
    ) if third_pairs else 0
    if not stages:
        height = s(title_block) + s(stage_head) + s(card_h) + s(pad)
    else:
        # Per-column pixel height; max across columns sets the canvas.
        # Final column gets extra room for the bronze sub-label + card
        # when a 3rd-place fixture is being rendered beneath it.
        unit = s(card_h) + s(card_gap)
        max_col_h = 0
        for stage, pairs in stages:
            h_cards = len(pairs) * unit
            if third_pairs and stage == _FINAL_STAGE:
                h_cards += bronze_label_h + unit
            if h_cards > max_col_h:
                max_col_h = h_cards
        col_height = s(stage_head) + max_col_h
        height = s(title_block) + col_height + s(pad)

    img = make_canvas(
        width, height,
        bg_color=BG,
        bg_image_path=t.get("bg_image_path"),
        bg_image_data=t.get("bg_image_data"),
        overlay_alpha=int(t.get("bg_overlay_alpha") or 165),
    )
    draw = ImageDraw.Draw(img)
    # Stash the underlying canvas on the draw object so helpers
    # (e.g. card renderers) can fall back to ``draw_text_with_emoji``
    # for player-name labels containing color emoji / flags. This is
    # cheaper than threading an ``img`` parameter through every
    # ``_render_card*`` callsite and keeps the change local.
    setattr(draw, "_em_base", img)

    # Row/card background opacity (0=transparent, 255=solid). Controlled
    # via /set_row_alpha <ID> <0-100> — same knob that already affects
    # standings_image.py. Applies to the stage header bars and the pair
    # cards so the custom background can show through if the user wants
    # a translucent bracket.
    row_alpha = int(t.get("row_bg_alpha") or 255)

    name = (t.get("name") or "Турнир").strip()
    t_type = (t.get("tournament_type") or "").upper()
    scope = "общий" if t.get("is_official", 1) else "локальный"
    sub_bits = []
    if t_type:
        sub_bits.append(t_type)
    sub_bits.append(scope)
    sub_bits.append("Сетка плей-офф")
    if half_label:
        sub_bits.append(half_label)
    sub_label = "  ·  ".join(sub_bits)

    title_clip = _truncate(name, title_font, width - s(pad) * 2, draw)
    draw.text((s(pad), s(pad)), title_clip, font=title_font, fill=TEXT)
    sub_y_off = 38 if compact else 50
    draw.text((s(pad), s(pad) + s(sub_y_off)), sub_label, font=sub_font, fill=MUTED)

    if not stages:
        _draw_rounded_rect_alpha(
            img,
            [s(pad), s(title_block),
             width - s(pad), s(title_block) + s(stage_head) + s(card_h)],
            radius=s(10), fill=CARD_BG, outline=BORDER, width=s(1),
            alpha=row_alpha,
        )
        draw.text(
            (s(pad) + s(16), s(title_block) + s(20)),
            "Плей-офф ещё не начался",
            font=stage_font, fill=MUTED,
        )
        buf = BytesIO()
        img.save(buf, format="PNG", optimize=True)
        return buf.getvalue()

    adv_mode = (t.get("playoff_advance_mode") or "goals").lower()
    x0 = s(pad)
    y0 = s(title_block)
    col_w = s(col_w_1x)
    for col_idx, (stage, pairs) in enumerate(stages):
        x = x0 + col_idx * (col_w + s(col_gap))
        stage_cfg = get_stage_config(t, stage)
        stage_adv_mode = stage_cfg["mode"]
        stage_series_len = stage_cfg["len"]

        _draw_rect_alpha(
            img,
            [x, y0, x + col_w, y0 + s(stage_head)],
            HEADER_BG, row_alpha,
        )
        draw.text(
            (x + s(10 if compact else 14),
             y0 + s(8 if compact else 14)),
            _stage_label(stage),
            font=stage_font, fill=HEADER_TXT,
        )

        y = y0 + s(stage_head) + s(6 if compact else 8)
        for ms in pairs:
            _render_card_dyn(
                draw, x, y, col_w, s(card_h), ms,
                name_font=name_font,
                score_font=score_font,
                leg_font=leg_font,
                badge_font=badge_font,
                scale=scale,
                compact=compact,
                advance_mode=stage_adv_mode,
                series_len=stage_series_len,
                img=img,
                row_alpha=row_alpha,
            )
            y += s(card_h) + s(card_gap)

        # Bronze match: rendered IN the Final column directly beneath
        # the Final card so it reads as a sibling fixture rather than
        # a separate stage. Uses the warm bronze palette + a small
        # "🥉 Матч за 3-е место" sub-label between the two cards.
        if third_pairs and stage == _FINAL_STAGE:
            third_cfg = get_stage_config(t, "third")
            label_text = "🥉 " + _stage_label("third")
            label_y = y + s(2 if compact else 4)
            draw.text(
                (x + s(2), label_y),
                _truncate(label_text, stage_font, col_w - s(4), draw),
                font=stage_font, fill=BRONZE_LABEL,
            )
            y += bronze_label_h
            for ms in third_pairs:
                _render_card_dyn(
                    draw, x, y, col_w, s(card_h), ms,
                    name_font=name_font,
                    score_font=score_font,
                    leg_font=leg_font,
                    badge_font=badge_font,
                    scale=scale,
                    compact=compact,
                    advance_mode=third_cfg["mode"],
                    series_len=third_cfg["len"],
                    bronze=True,
                    img=img,
                    row_alpha=row_alpha,
                )
                y += s(card_h) + s(card_gap)

    buf = BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def _render_card_dyn(
    draw: ImageDraw.ImageDraw,
    x: int, y: int, w: int, h: int,
    ms: list[dict],
    *,
    name_font, score_font, leg_font, badge_font,
    scale: int,
    compact: bool,
    advance_mode: str = "goals",
    series_len: int | None = None,
    bronze: bool = False,
    img: Image.Image | None = None,
    row_alpha: int = 255,
):
    """Compact-aware version of ``_render_card`` (the original at module
    top is kept for backwards compat with the 2× layout). Internally
    falls through to the same drawing logic but uses scale-aware
    paddings and offsets so the compact layout doesn't clip text.

    ``bronze=True`` swaps the card palette to the warm bronze tones so
    the 3rd-place fixture reads as distinct from the main bracket.

    ``img`` + ``row_alpha`` route the card body through the alpha-aware
    rounded-rect helper so ``/set_row_alpha`` also affects the bracket
    (not just the standings). When ``img`` is omitted we fall back to
    the legacy opaque drawing for backwards compatibility.
    """
    def s(v: int) -> int:
        return int(v * scale)

    pad = s(8 if compact else 14)
    name_y_top = s(6 if compact else 10)
    name_y_bot = s(24 if compact else 34)
    leg_y      = s(48 if compact else 64)

    card_bg_default = BRONZE_BG if bronze else CARD_BG
    card_bg_done    = BRONZE_DONE if bronze else CARD_DONE
    card_bg_live    = BRONZE_LIVE if bronze else CARD_LIVE
    card_border     = BRONZE_BORDER if bronze else BORDER

    def _card_rect(fill_color):
        if img is not None:
            _draw_rounded_rect_alpha(
                img,
                [x, y, x + w, y + h],
                radius=s(8), fill=fill_color,
                outline=card_border, width=s(1),
                alpha=row_alpha,
            )
        else:
            draw.rounded_rectangle(
                [x, y, x + w, y + h],
                radius=s(8), fill=fill_color,
                outline=card_border, width=s(1),
            )

    if ms and ms[0].get("_tbd"):
        _card_rect(card_bg_default)
        inner_w = w - pad * 2
        wa = ms[0].get("_partial_winner_a")
        wb = ms[0].get("_partial_winner_b")

        def _resolve_name(pid: int | None) -> tuple[str, tuple]:
            if pid is None:
                return "TBD", MUTED
            p = get_player_by_id(pid)
            return _display_name(p, fallback=f"id{pid}"), TEXT

        name_a, col_a = _resolve_name(wa)
        name_b, col_b = _resolve_name(wb)
        draw.text(
            (x + pad, y + name_y_top),
            _truncate(name_a, name_font, inner_w, draw),
            font=name_font, fill=col_a,
        )
        draw.text(
            (x + pad, y + name_y_bot),
            _truncate(name_b, name_font, inner_w, draw),
            font=name_font, fill=col_b,
        )
        if wa is None and wb is None:
            hint = "ожидаем предыдущую стадию"
        elif wa is not None and wb is not None:
            hint = "ожидаем старт стадии"
        else:
            hint = "ждёт соперника"
        draw.text(
            (x + pad, y + leg_y),
            _truncate(hint, leg_font, inner_w, draw),
            font=leg_font, fill=MUTED,
        )
        return

    a_id = ms[0]["player1_id"]
    b_id = ms[0]["player2_id"]
    pa = get_player_by_id(a_id)
    pb = get_player_by_id(b_id)
    name_a = _display_name(pa, fallback=f"id{a_id}")
    name_b = _display_name(pb, fallback=f"id{b_id}")

    all_done = all((m.get("status") or "") == "confirmed" for m in ms)
    fill = card_bg_done if all_done else (card_bg_live if any(
        (m.get("status") or "") == "reported" for m in ms
    ) else card_bg_default)

    _card_rect(fill)

    leg_lines: list[str] = []
    for m in ms:
        leg_no = int(m.get("leg") or 1)
        status = (m.get("status") or "pending").lower()
        if status == "confirmed":
            if m["player1_id"] == a_id:
                leg_lines.append(f"L{leg_no}: {m['score1']}:{m['score2']}")
            else:
                leg_lines.append(f"L{leg_no}: {m['score2']}:{m['score1']}")
        elif status == "reported":
            leg_lines.append(f"L{leg_no}: ож.")
        else:
            leg_lines.append(f"L{leg_no}: ⏳")

    inner_w = w - pad * 2
    name_a_clip = _truncate(name_a, name_font, inner_w, draw)
    name_b_clip = _truncate(name_b, name_font, inner_w, draw)

    a_color = TEXT
    b_color = TEXT
    badge = ""
    if all_done:
        winner_id = _resolve_pair_winner(
            ms, advance_mode=advance_mode, series_len=series_len,
        )
        if winner_id == a_id:
            a_color = WIN; b_color = LOSS
            badge = "🏅"
        elif winner_id == b_id:
            a_color = LOSS; b_color = WIN
            badge = "🏅"

    _em_base_dyn = getattr(draw, "_em_base", None)
    if _em_base_dyn is not None:
        from emoji_helper import draw_text_with_emoji
        draw_text_with_emoji(
            _em_base_dyn, (x + pad, y + name_y_top),
            name_a_clip, name_font, fill=a_color,
        )
        draw_text_with_emoji(
            _em_base_dyn, (x + pad, y + name_y_bot),
            name_b_clip, name_font, fill=b_color,
        )
    else:
        draw.text((x + pad, y + name_y_top), name_a_clip, font=name_font, fill=a_color)
        draw.text((x + pad, y + name_y_bot), name_b_clip, font=name_font, fill=b_color)

    if all_done:
        a_g, b_g = _aggregate_score(ms, a_id, b_id)
        agg_text = f"{a_g}:{b_g}"
        agg_w = draw.textlength(agg_text, font=score_font)
        draw.text(
            (x + w - pad - agg_w, y + name_y_top - s(2 if compact else 0)),
            agg_text, font=score_font, fill=HEADER_TXT,
        )

    # Multi-line wrap so a 5-leg series (e.g. 3:3·3:3·3:3·3:3·2:1) is
    # not truncated to "L1 · L2 · L3 …" on the card — mirrors what
    # ``_render_card`` does in the 2× layout.
    leg_rows = _layout_leg_rows(draw, leg_lines, ms, a_id, inner_w, leg_font)
    line_h = s(14 if compact else 18)
    for i, row in enumerate(leg_rows):
        draw.text(
            (x + pad, y + leg_y + i * line_h),
            row, font=leg_font, fill=MUTED,
        )

    if badge:
        draw.text(
            (x + w - pad - draw.textlength(badge, font=badge_font),
             y + h - pad - s(16 if compact else 20)),
            badge, font=badge_font, fill=HEADER_TXT,
        )


def _split_stages_by_half(
    stages: list[tuple[str, list[list[dict]]]], half: str,
) -> list[tuple[str, list[list[dict]]]]:
    """Slice each stage's pair list to the requested half of the bracket.

    Pairs are stored in standard bracket order (top half first, bottom
    half second). For each stage we keep the first or second half of the
    pair list. The Final has only 1 pair — we duplicate it into both
    halves so each picture shows the championship match.
    """
    out: list[tuple[str, list[list[dict]]]] = []
    for stage, pairs in stages:
        if stage == _FINAL_STAGE or len(pairs) <= 1:
            # Always duplicate the final / single-pair stage in both halves.
            out.append((stage, list(pairs)))
            continue
        mid = len(pairs) // 2
        if half == "top":
            out.append((stage, pairs[:mid]))
        else:
            out.append((stage, pairs[mid:]))
    return out


def _split_stages_into_pieces(
    stages: list[tuple[str, list[list[dict]]]],
    n_pieces: int,
) -> list[list[tuple[str, list[list[dict]]]]]:
    """Slice the bracket into ``n_pieces`` non-overlapping subsets.

    Pairs are stored in standard bracket order (top half first, bottom
    half second). For ``n_pieces=2`` this gives top + bottom halves;
    for ``n_pieces=4`` it's quarters, and so on (binary subdivision).

    Stages whose pair count is < ``n_pieces`` (e.g. SF/Final) get
    duplicated across the pieces that "feed" into them, so each picture
    shows the championship match.
    """
    pieces: list[list[tuple[str, list[list[dict]]]]] = [
        [] for _ in range(n_pieces)
    ]
    for stage, pairs in stages:
        nps = len(pairs)
        if nps == 0:
            for p in pieces:
                p.append((stage, []))
            continue
        if nps >= n_pieces:
            chunk = nps // n_pieces
            for i in range(n_pieces):
                lo = i * chunk
                hi = (i + 1) * chunk if i < n_pieces - 1 else nps
                pieces[i].append((stage, pairs[lo:hi]))
        else:
            # Stage has fewer pairs than pieces — figure out which
            # pair each piece feeds into. ``ratio`` pieces share each
            # pair (e.g. for n_pieces=4, len(pairs)=2, ratio=2 → pieces
            # 0,1 see pair 0; pieces 2,3 see pair 1).
            ratio = max(1, n_pieces // max(1, nps))
            for i in range(n_pieces):
                pair_idx = min(nps - 1, i // ratio)
                pieces[i].append((stage, [pairs[pair_idx]]))
    return pieces


def _render_image_mirrored(
    t: dict,
    stages: list[tuple[str, list[list[dict]]]],
    *,
    compact: bool = False,
    half_label: str = "",
    third_pairs: list[list[dict]] | None = None,
) -> bytes:
    """Mirrored bracket render: outermost stage on both sides, Final in
    the middle column. Classic sports-bracket "diamond" — quarters at
    the far left/right, semifinals one step inward, final dead center.

    Falls back to the linear ``_render_image`` layout when there's only
    a single non-final stage with one pair (nothing to mirror).
    """
    if not stages:
        return _render_image(t, stages, compact=compact)

    # Separate the Final from the upstream stages. The 3rd-place
    # fixture is intentionally *excluded* from the mirrored diamond —
    # rendering it as one of the halved upstream stages would yield a
    # "floating" half-empty column. We render it back at the far right
    # of the canvas as a sibling of the final.
    final_pairs: list[list[dict]] = []
    other_stages: list[tuple[str, list[list[dict]]]] = []
    for stage, pairs in stages:
        if stage == _FINAL_STAGE:
            final_pairs = list(pairs)
        elif stage == "third":
            # Defensive: callers should pass via ``third_pairs``, but
            # accept an inline ``"third"`` stage too.
            if not third_pairs:
                third_pairs = list(pairs)
        else:
            other_stages.append((stage, list(pairs)))

    # If we have a single non-final stage with exactly one pair (e.g. a
    # 2-team tournament showing just the Final), there's nothing to
    # mirror — defer to the linear renderer.
    if not other_stages:
        return _render_image(
            t, stages, compact=compact, third_pairs=third_pairs,
        )
    if (
        len(other_stages) == 1
        and len(other_stages[0][1]) <= 1
        and len(final_pairs) <= 1
    ):
        return _render_image(
            t, stages, compact=compact, third_pairs=third_pairs,
        )

    # Split each upstream stage into top + bottom halves.
    halves: list[tuple[str, list[list[dict]]], list[list[dict]]] = []
    for stage, pairs in other_stages:
        if len(pairs) <= 1:
            # Stage with 1 pair (e.g. a single SF that hasn't split into
            # halves): show it once on the LEFT side only — the right
            # side gets an empty placeholder column.
            halves.append((stage, list(pairs), []))
        else:
            mid = len(pairs) // 2
            halves.append((stage, pairs[:mid], pairs[mid:]))

    # Geometry knobs.
    if compact:
        scale = 1
        pad         = COMPACT_PAD
        title_block = COMPACT_TITLE_BLOCK
        col_w_1x    = COMPACT_COL_W
        col_gap     = COMPACT_COL_GAP
        card_h      = COMPACT_CARD_H
        card_gap    = COMPACT_CARD_GAP
        stage_head  = COMPACT_STAGE_HEAD_H
        title_font  = (
            ImageFont.truetype(_BOLD_PATHS[0], 26) if _BOLD_PATHS
            else _font(26, True)
        )
        sub_font    = _font(15, bold=False)
        stage_font  = _font(16, bold=True)
        name_font   = _font(15, bold=True)
        score_font  = _font(15, bold=True)
        leg_font    = _font(12, bold=False)
        badge_font  = _font(15, bold=True)
    else:
        scale = SCALE
        pad         = PAD
        title_block = TITLE_BLOCK
        col_w_1x    = COL_W
        col_gap     = COL_GAP
        card_h      = CARD_H
        card_gap    = CARD_GAP
        stage_head  = STAGE_HEAD_H
        title_font  = _font(_s(34), bold=True)
        sub_font    = _font(_s(20), bold=False)
        stage_font  = _font(_s(22), bold=True)
        name_font   = _font(_s(22), bold=True)
        score_font  = _font(_s(22), bold=True)
        leg_font    = _font(_s(18), bold=False)
        badge_font  = _font(_s(22), bold=True)

    def s(v: int) -> int:
        return int(v * scale)

    UNIT = s(card_h) + s(card_gap)

    # How many "slots" the bracket needs vertically. The outermost
    # stage has the most pairs; split into top + bottom halves.
    n_top = max(len(top) for _, top, _ in halves) if halves else 0
    n_bot = max(len(bot) for _, _, bot in halves) if halves else 0
    n_slots = max(1, n_top + n_bot)

    # The Final sits centered between the top and bottom halves.
    # Add a small gutter between halves so the Final doesn't squeeze
    # right against the SF cards.
    gutter = s(card_gap)
    bracket_height = n_slots * UNIT - s(card_gap)

    # Column layout left → right:
    #   [outermost_top, ..., SF_top, Final, SF_bot, ..., outermost_bot]
    # Width is identical across columns. The 3rd-place fixture, when
    # present, is rendered IN the Final column directly beneath the
    # final card (bronze-tinted) — NOT as an extra column. This keeps
    # the diamond geometry intact and groups the two championship
    # decision matches visually together.
    has_third = bool(third_pairs)
    n_other = len(halves)
    n_cols = 2 * n_other + 1  # left halves + final + right halves
    col_w = s(col_w_1x)
    width = s(pad) * 2 + n_cols * col_w + (n_cols - 1) * s(col_gap)
    # Extra vertical room consumed by the bronze sub-label + card,
    # added below the bracket so the Final column can grow downward.
    bronze_label_h = s(20 if compact else 28)
    bronze_block_h = (
        bronze_label_h + UNIT
    ) if has_third else 0
    height = (
        s(title_block) + s(stage_head) + bracket_height + gutter
        + bronze_block_h + s(pad)
    )

    img = make_canvas(
        width, height,
        bg_color=BG,
        bg_image_path=t.get("bg_image_path"),
        bg_image_data=t.get("bg_image_data"),
        overlay_alpha=int(t.get("bg_overlay_alpha") or 165),
    )
    draw = ImageDraw.Draw(img)
    # Same trick as in _render_image: stash the canvas so helpers can
    # render color emoji onto it for player-name labels.
    setattr(draw, "_em_base", img)

    # Row/card opacity — same /set_row_alpha knob as standings.
    row_alpha = int(t.get("row_bg_alpha") or 255)

    # Title.
    name = (t.get("name") or "Турнир").strip()
    t_type = (t.get("tournament_type") or "").upper()
    scope = "общий" if t.get("is_official", 1) else "локальный"
    sub_bits = []
    if t_type:
        sub_bits.append(t_type)
    sub_bits.append(scope)
    sub_bits.append("Сетка плей-офф")
    if half_label:
        sub_bits.append(half_label)
    sub_label = "  ·  ".join(sub_bits)

    title_clip = _truncate(name, title_font, width - s(pad) * 2, draw)
    draw.text((s(pad), s(pad)), title_clip, font=title_font, fill=TEXT)
    sub_y_off = 38 if compact else 50
    draw.text((s(pad), s(pad) + s(sub_y_off)), sub_label, font=sub_font, fill=MUTED)

    adv_mode = (t.get("playoff_advance_mode") or "goals").lower()
    x0 = s(pad)
    y_head = s(title_block)
    y0 = y_head + s(stage_head) + s(4)

    # ── Helper: compute card y-position for a pair at column depth d ──
    # Outermost stage column has the most pairs; each card occupies one
    # UNIT slot. As we move inward (toward Final), the spacing doubles
    # so each card centers between its two children. Top half occupies
    # the top n_top slots; bottom half occupies the next n_bot slots.
    # Final is centered vertically across both halves.
    def card_y_top_side(depth: int, idx: int, half_count: int) -> int:
        """Y position for a card on the TOP half at depth `depth`
        (0 = outermost), index `idx` within its half. ``half_count`` is
        the number of cards in the outermost stage's top half.
        """
        # Power-of-two depth means card spans 2^depth slots and is
        # centered. For non-power-of-2 brackets (odd splits, byes), we
        # fall back to "spread evenly across n_top slots".
        if half_count <= 0:
            return y0
        slots_per_card = max(1, half_count) // max(1, _cards_in_top_at_depth(
            depth, half_count
        ))
        slot_center = idx * slots_per_card + (slots_per_card - 1) / 2.0
        return y0 + int(slot_center * UNIT)

    def card_y_bot_side(depth: int, idx: int, half_count_bot: int) -> int:
        if half_count_bot <= 0:
            return y0 + n_top * UNIT + gutter
        slots_per_card = max(1, half_count_bot) // max(
            1, _cards_in_top_at_depth(depth, half_count_bot)
        )
        slot_center = idx * slots_per_card + (slots_per_card - 1) / 2.0
        return y0 + (n_top * UNIT + gutter) + int(slot_center * UNIT)

    # Column draw loop. Columns left-to-right:
    #   0..n_other-1: top-half columns, deepest (outermost) first
    #   n_other     : final
    #   n_other+1..: bottom-half columns, innermost (SF) first
    # depth measured from outermost: depth=0 is outermost stage.
    # Sort halves so index 0 = outermost. ``other_stages`` already
    # walks outermost → innermost (because PLAYOFF_STAGES is ordered
    # from largest round to smallest), so halves[0] is outermost.
    # The left side renders outermost first (leftmost), then innermost
    # — which IS halves[0..n-1] in order. The right side renders
    # innermost (right next to Final) first, then outermost — which is
    # halves[n-1..0] (reversed).
    for col_idx in range(n_cols):
        x = x0 + col_idx * (col_w + s(col_gap))

        if col_idx == n_other:
            # Final column.
            stage = _FINAL_STAGE
            pairs_here = final_pairs
            depth_label = _stage_label(stage)
            # Final cards centered between top + bottom halves.
            mid_y = y0 + (n_top * UNIT + gutter // 2) - s(card_h) // 2
            ys = [mid_y]
        elif col_idx < n_other:
            # Left side, top half. col_idx 0 = outermost top.
            half_idx = col_idx  # 0..n_other-1; 0 = outermost
            stage, top_pairs, _ = halves[half_idx]
            pairs_here = top_pairs
            depth_label = _stage_label(stage)
            half_count_top = max(len(h[1]) for h in halves) or 1
            # depth=0 means OUTERMOST (most cards). halves[0] is
            # outermost, so depth equals half_idx.
            depth = half_idx
            ys = [
                card_y_top_side(depth, i, half_count_top)
                for i in range(len(pairs_here))
            ]
        else:
            # Right side, bottom half. col_idx n_other+1 = innermost
            # (SF), col_idx n_cols-1 = outermost.
            half_idx = (col_idx - n_other - 1)  # 0=innermost, n_other-1=outermost
            # Map back to halves index: innermost half is halves[n_other-1].
            real_idx = (n_other - 1) - half_idx
            stage, _, bot_pairs = halves[real_idx]
            pairs_here = bot_pairs
            depth_label = _stage_label(stage)
            half_count_bot = max(len(h[2]) for h in halves) or 1
            # depth=0 means OUTERMOST (most cards). On the right side
            # the OUTERMOST column is the rightmost (half_idx = n_other-1
            # → real_idx = 0), and the innermost SF is closest to the
            # final (half_idx = 0 → real_idx = n_other-1). The depth in
            # halves space equals real_idx.
            depth = real_idx
            ys = [
                card_y_bot_side(depth, i, half_count_bot)
                for i in range(len(pairs_here))
            ]

        # Header rectangle.
        _draw_rect_alpha(
            img,
            [x, y_head, x + col_w, y_head + s(stage_head)],
            HEADER_BG, row_alpha,
        )
        draw.text(
            (x + s(10 if compact else 14),
             y_head + s(8 if compact else 14)),
            depth_label, font=stage_font, fill=HEADER_TXT,
        )

        # Cards.
        stage_cfg = get_stage_config(t, stage)
        stage_adv_mode = stage_cfg["mode"]
        stage_series_len = stage_cfg["len"]
        last_card_bottom = y_head + s(stage_head)
        for i, ms in enumerate(pairs_here):
            y = ys[i] if i < len(ys) else (y0 + i * UNIT)
            _render_card_dyn(
                draw, x, y, col_w, s(card_h), ms,
                name_font=name_font,
                score_font=score_font,
                leg_font=leg_font,
                badge_font=badge_font,
                scale=scale,
                compact=compact,
                advance_mode=stage_adv_mode,
                series_len=stage_series_len,
                img=img,
                row_alpha=row_alpha,
            )
            last_card_bottom = y + s(card_h)

        # Bronze fixture: rendered IN the Final column directly below
        # the final card, with a small "🥉 Матч за 3-е место"
        # sub-label and the warm bronze palette so it reads as a
        # sibling decision match rather than a separate stage.
        if has_third and col_idx == n_other:
            third_cfg = get_stage_config(t, "third")
            label_text = "🥉 " + _stage_label("third")
            label_y = last_card_bottom + s(6 if compact else 10)
            draw.text(
                (x + s(2), label_y),
                _truncate(label_text, stage_font, col_w - s(4), draw),
                font=stage_font, fill=BRONZE_LABEL,
            )
            bronze_y = label_y + bronze_label_h
            for ms in third_pairs:
                _render_card_dyn(
                    draw, x, bronze_y, col_w, s(card_h), ms,
                    name_font=name_font,
                    score_font=score_font,
                    leg_font=leg_font,
                    badge_font=badge_font,
                    scale=scale,
                    compact=compact,
                    advance_mode=third_cfg["mode"],
                    series_len=third_cfg["len"],
                    bronze=True,
                    img=img,
                    row_alpha=row_alpha,
                )
                bronze_y += UNIT

    buf = BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def _cards_in_top_at_depth(depth: int, half_count: int) -> int:
    """How many cards are in the top half of a stage at ``depth`` from
    the outermost stage. depth=0 → ``half_count`` cards; each step
    inward halves that (rounded up so byes don't crash the layout).
    """
    n = half_count
    for _ in range(depth):
        n = max(1, (n + 1) // 2)
    return n


def render_playoff_pngs(tid: int) -> list[bytes]:
    """Render the bracket as one or more PNGs.

    Layout selection (``tournaments.bracket_layout``):

    * ``'mirrored'`` (default) — classic sports-bracket diamond with
      stages converging from both sides toward the Final in the middle.
      Used for small brackets (≤ 16 pairs in the largest stage).
    * ``'linear'`` — single left-to-right column flow (the layout in
      use before 2026-05). Stays available for admins who prefer the
      compact-looking single-column layout.

    Sizing rules apply on top of the layout choice:

    * ≤ 16 pairs in the largest stage → 1 image.
    * 17–32 pairs → 2 images (top + bottom half), rich layout.
    * > 32 pairs → 2+ images in **compact** layout (smaller cards,
      scale 1×) with as many splits as needed so each piece fits the
      Telegram photo size limit (width + height ≤ 10000 px).

    When a 3rd-place fixture exists it's rendered as an extra column
    on the FIRST image only (avoids duplicating the bronze match
    across split halves of huge brackets).
    """
    # Refresh the per-tournament team-tag cache so every nested
    # ``_display_name`` call resolves the right per-tournament tag
    # without us plumbing a tag map through every helper.
    _load_tag_map(tid)
    t = get_tournament(tid) or {}
    _load_name_mode(t)
    stages = _collect_pairs_full(tid)
    third_pairs = _collect_third_place(tid)

    if not stages:
        return [_render_image(t, stages, third_pairs=third_pairs)]

    max_pairs = max(len(pairs) for _, pairs in stages)
    layout = (t.get("bracket_layout") or "mirrored").lower()

    # Tier 1: small bracket — single image, layout from config.
    if max_pairs <= 8:
        if layout == "linear":
            return [_render_image(t, stages, third_pairs=third_pairs)]
        return [_render_image_mirrored(t, stages, third_pairs=third_pairs)]

    # Tier 2: medium bracket — two halves, rich layout still works.
    if max_pairs <= 32:
        pieces = _split_stages_into_pieces(stages, 2)
        labels = ["верхняя половина", "нижняя половина"]
        if layout == "linear":
            return [
                _render_image(
                    t, pieces[i], half_label=labels[i],
                    third_pairs=third_pairs if i == 0 else None,
                )
                for i in range(2)
            ]
        return [
            _render_image_mirrored(
                t, pieces[i], half_label=labels[i],
                third_pairs=third_pairs if i == 0 else None,
            )
            for i in range(2)
        ]

    # Tier 3: big bracket — compact layout, n_pieces chosen so each
    # piece carries ≤ 32 pairs in its largest stage. Always a power of
    # two so the bracket halves divide cleanly.
    n_pieces = 1
    while max_pairs > 32 * n_pieces:
        n_pieces *= 2
    n_pieces = max(2, min(n_pieces, 8))
    pieces = _split_stages_into_pieces(stages, n_pieces)
    return [
        _render_image(
            t, pieces[i],
            half_label=f"часть {i + 1}/{n_pieces}",
            compact=True,
            third_pairs=third_pairs if i == 0 else None,
        )
        for i in range(n_pieces)
    ]


def render_playoff_png(tid: int) -> bytes:
    """Backwards-compat wrapper: returns the first image only.

    For small brackets this is the whole picture; for huge brackets it's
    just the top half (callers that want both should switch to
    ``render_playoff_pngs``).
    """
    return render_playoff_pngs(tid)[0]


__all__ = ["render_playoff_png", "render_playoff_pngs"]
