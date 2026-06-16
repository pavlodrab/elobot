"""Leaderboard / Top-N / feedback commands (Phase 5 of the bot.py split).

This module owns the read-mostly ranking commands plus the /feedback
flow:

* ``/top``, ``/top_vsa``, ``/top_ri`` — global ELO top-15 by field.
* ``/leaderboard`` — per-tournament leaderboard (used by player-created
  tournaments; for official ones it shows tournament-local W/D/L view).
* ``/top_scorers`` — goal-scorers ranking (official / custom / all /
  per-tournament).
* ``/feedback`` — bug/idea reporter; forwards to all root admins via
  ``_send_feedback_to_admins``.
* ``/cancel`` — cancel an in-flight ``awaiting_feedback`` state.

Everything is re-exported from ``bot`` for backward compatibility
(``from bot import cmd_top, cmd_top_scorers, _send_feedback_to_admins``
keeps working).
"""

from __future__ import annotations

import html
import logging
from io import BytesIO

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

import database as db
from database import (
    get_active_tournaments,
    get_all_players,
    get_all_players_by_elo_field,
    get_goals_vs_opponents_for_tournament,
    get_player_by_id,
    get_tournament,
    get_tournament_leaderboard,
    get_tournament_players,
)
from elo import rank_label
from tablebomb_image import render_tablebomb_png

from handlers._helpers import _player_from_user
from handlers.common import (
    ADMIN_IDS,
    FOOTER_CTX_TABLE,
    get_random_footer,
    mention,
    parse_tournament_type_arg,
    send,
    t_full_label,
)

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# /top  /top_vsa  /top_ri — global ELO ranking
# ─────────────────────────────────────────────────────────────────────────────

async def _send_top_by_field(update: Update, ctx: ContextTypes.DEFAULT_TYPE, field: str):
    """Render Top-15 sorted by ``elo`` / ``elo_vsa`` / ``elo_ri``."""
    players = get_all_players_by_elo_field(field)
    if not players:
        await send(update, "Нет зарегистрированных игроков.")
        return

    headers = {
        "elo":     ("🏆 <b>Общий рейтинг лиги (ELO)</b>",
                    "<i>Один пул — только матчи официальных турниров. "
                    "Турниры игроков считаются отдельно: /leaderboard</i>"),
        "elo_vsa": ("⚽ <b>Топ ВСА</b>",
                    "<i>Только официальные матчи в турнирах ВСА.</i>"),
        "elo_ri":  ("🎮 <b>Топ РИ</b>",
                    "<i>Только официальные матчи в турнирах РИ.</i>"),
    }
    h_title, h_sub = headers.get(field, headers["elo"])
    lines = [h_title, h_sub + "\n"]

    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    for i, p in enumerate(players[:15], 1):
        total = p["wins"] + p["losses"] + p["draws"]
        m = medals.get(i, f"{i}.")
        elo_val = round(p.get(field) or 0)
        rank = rank_label(elo_val)
        nick = f" <i>({p['game_nickname']})</i>" if p.get("game_nickname") else ""
        lines.append(
            f"{m} {mention(p['username'])}{nick} — <b>{elo_val}</b> ELO  {rank}\n"
            f"   W{p['wins']} D{p['draws']} L{p['losses']} ({total} матчей)"
        )
    await send(update, "\n".join(lines))


async def cmd_top(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _send_top_by_field(update, ctx, "elo")


async def cmd_top_vsa(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _send_top_by_field(update, ctx, "elo_vsa")


async def cmd_top_ri(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _send_top_by_field(update, ctx, "elo_ri")


# ─────────────────────────────────────────────────────────────────────────────
# /leaderboard — per-tournament leaderboard
# ─────────────────────────────────────────────────────────────────────────────

def _resolve_leaderboard_tournament(
    arg: str | None,
    requester: dict | None,
):
    """Pick which tournament's local leaderboard to show.

    Priority:
      1. If arg is a numeric tournament id, use that tournament directly
         (works for finished tournaments too).
      2. If arg is a type ('вса' / 'ри'), prefer an active tournament of
         that type that the requester participates in; else any active.
      3. Else: any active tournament the requester participates in;
         prefer non-official ones (since /leaderboard is mostly meant
         for those).
    Returns the tournament dict or None.
    """
    if arg:
        a = arg.strip()
        if a.isdigit():
            return get_tournament(int(a))
        t_type = parse_tournament_type_arg(a)
        if t_type:
            actives = [t for t in get_active_tournaments()
                       if t["tournament_type"] == t_type]
            if requester:
                for t in actives:
                    members = get_tournament_players(t["id"])
                    if any(m["player_id"] == requester["id"] for m in members):
                        return t
            return actives[0] if actives else None

    actives = get_active_tournaments()
    if requester:
        own = []
        for t in actives:
            members = get_tournament_players(t["id"])
            if any(m["player_id"] == requester["id"] for m in members):
                own.append(t)
        # Prefer the player's local tournaments first.
        own.sort(key=lambda t: (1 if t.get("is_official", 1) else 0, -t["id"]))
        if own:
            return own[0]
    if actives:
        return actives[0]
    return None


def _build_official_local_view(t: dict) -> list[dict]:
    """For *official* tournaments we don't keep an isolated rating, but
    we still want /leaderboard to show useful per-tournament stats.
    Aggregate confirmed matches from this tournament and rank players by
    W/D/L/goal-diff.
    """
    matches = db.get_real_tournament_matches(t["id"])
    members = get_tournament_players(t["id"])

    by_pid: dict[int, dict] = {}
    for mp in members:
        by_pid[mp["player_id"]] = {
            "player_id": mp["player_id"],
            "username": mp["username"],
            "game_nickname": mp.get("game_nickname"),
            "telegram_id": mp.get("telegram_id"),
            "elo": mp.get("elo") or 0,   # show current global ELO so it stays meaningful
            "games": 0,
            "wins": 0,
            "draws": 0,
            "losses": 0,
            "goals_for": 0,
            "goals_against": 0,
        }

    for m in matches:
        if m["status"] != "confirmed":
            continue
        p1 = by_pid.get(m["player1_id"])
        p2 = by_pid.get(m["player2_id"])
        if not p1 or not p2:
            continue
        s1, s2 = m["score1"] or 0, m["score2"] or 0
        for p, gf, ga in [(p1, s1, s2), (p2, s2, s1)]:
            p["games"] += 1
            p["goals_for"] += gf
            p["goals_against"] += ga
        if s1 > s2:
            p1["wins"] += 1
            p2["losses"] += 1
        elif s2 > s1:
            p2["wins"] += 1
            p1["losses"] += 1
        else:
            p1["draws"] += 1
            p2["draws"] += 1

    rows = list(by_pid.values())
    rows.sort(
        key=lambda r: (r["elo"], r["wins"] - r["losses"],
                       r["goals_for"] - r["goals_against"], r["goals_for"]),
        reverse=True,
    )
    return rows


async def cmd_leaderboard(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Локальный лидерборд турнира.

    Использование:
      /leaderboard           — авто-выбор твоего активного турнира
      /leaderboard 7         — по ID турнира
      /leaderboard вса       — активный ВСА турнир
      /leaderboard ри        — активный РИ турнир
    """
    user = update.effective_user
    requester = _player_from_user(user)

    arg = ctx.args[0] if ctx.args else None
    t = _resolve_leaderboard_tournament(arg, requester)
    if not t:
        await send(
            update,
            "❌ Не нашёл турнир. Укажи ID: <code>/leaderboard 7</code> или "
            "<code>/leaderboard вса</code>.",
        )
        return

    is_official = bool(t.get("is_official", 1))
    rows = (
        get_tournament_leaderboard(t["id"])
        if not is_official
        else _build_official_local_view(t)
    )

    creator = get_player_by_id(t["created_by"]) if t.get("created_by") else None
    creator_str = mention(creator["username"]) if creator else "—"

    header = [
        f"🏆 <b>Лидерборд турнира</b>",
        f"<b>{t['name']}</b> [{t_full_label(t)}]  (ID: {t['id']})",
        f"Создал: {creator_str}",
    ]
    if is_official:
        header.append(
            "<i>Официальный турнир: ELO считается из общего пула. "
            "Сводка ниже — только матчи в этом турнире.</i>"
        )
    else:
        header.append(
            "<i>Турнир игрока: изолированный ELO. Общий ELO/ВСА/РИ "
            "не задеваются.</i>"
        )

    if not rows:
        header.append("\nПока нет данных. Сыграйте хотя бы один матч.")
        await send(update, "\n".join(header))
        return

    lines = list(header) + [""]
    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    for i, r in enumerate(rows, 1):
        m = medals.get(i, f"{i}.")
        elo_val = round(r.get("elo") or 0)
        nick = f" <i>({r['game_nickname']})</i>" if r.get("game_nickname") else ""
        gd = (r.get("goals_for") or 0) - (r.get("goals_against") or 0)
        gd_str = f"+{gd}" if gd > 0 else str(gd)
        lines.append(
            f"{m} {mention(r['username'])}{nick} — <b>{elo_val}</b> ELO\n"
            f"   W{r.get('wins', 0)} D{r.get('draws', 0)} L{r.get('losses', 0)}  "
            f"⚽ {r.get('goals_for', 0)}:{r.get('goals_against', 0)} ({gd_str})  "
            f"матчей: {r.get('games', 0)}"
        )
    await send(update, "\n".join(lines))


# ─────────────────────────────────────────────────────────────────────────────
# /top_scorers — goal-scorers ranking
# ─────────────────────────────────────────────────────────────────────────────

_TABLEBOMB_TEXT_TOKENS = {"text", "текст", "txt", "т"}


def _normalize_footballer_name(name: str) -> str:
    """Normalize an OCR footballer name for deduplication.

    Handles common OCR variations:
    - Strip whitespace and trailing dots/periods
    - Strip trailing 'ГОЛ'/'GOAL' suffix (if AI OCR left it in)
    - Lowercase for comparison
    - Remove diacritics differences (é→e, š→s, etc.)
    - Remove common suffixes like "Jr.", "Jr", "Sr."
    - Collapse consecutive duplicate letters (Mbaappé → Mbape, Oliise → Olise)
    - Normalize common OCR character confusions
    - Collapse multiple spaces
    """
    import re
    import unicodedata
    s = (name or "").strip().rstrip(".")

    # Strip trailing 'ГОЛ'/'GOAL' suffix (space-separated or glued)
    s = re.sub(r"\s*(?:ГОЛ|GOAL|Гол|gol|GOL|гол)\.?\s*$", "", s, flags=re.IGNORECASE)

    # Decompose unicode and strip combining marks (diacritics)
    nfkd = unicodedata.normalize("NFKD", s)
    stripped = "".join(c for c in nfkd if not unicodedata.combining(c))
    # Lowercase
    stripped = stripped.lower()
    # Remove common suffixes
    for suffix in (" jr.", " jr", " sr.", " sr", " iii", " ii"):
        if stripped.endswith(suffix):
            stripped = stripped[: -len(suffix)]
            break
    # Collapse spaces
    stripped = " ".join(stripped.split())

    # Collapse consecutive duplicate letters:
    # "mbappe" → "mbape", "oliise" → "olise", "bellingham" stays (ll is common)
    # Strategy: collapse runs of 3+ same char to 1, and runs of 2 same char
    # to 1 as well (since footballer names rarely have intentional doubles
    # that matter for matching — "Bellingham" vs "Belingham" should match).
    stripped = re.sub(r"(.)\1+", r"\1", stripped)

    # Normalize common OCR character confusions in Latin:
    # 'rn' ↔ 'm', but that's risky. Instead just keep the collapsed form.
    # The main win is already from diacritics + dedup above.

    return stripped


def _last_token_match(a: str, b: str) -> bool:
    """Return True if one of the normalized names is a single token that
    equals the last (surname) token of the other.

    This catches the very common OCR / game-UI pattern where the same
    real footballer is rendered both as full name and as surname only:

        ("wiliams", "nico wiliams")  → True   (Nico Williams)
        ("yamal",   "lamine yamal")  → True   (Lamine Yamal)
        ("torres",  "ferran torres") → True   (Ferran Torres)
        ("ronaldo", "c. ronaldo")    → True   (Cristiano Ronaldo)
        ("vini",    "vinicius")      → False  (different first names)
        ("torres",  "ferran")        → False  (no shared token)

    Both names are expected to be already normalized via
    ``_normalize_footballer_name`` (lowercased, diacritics stripped,
    spaces collapsed).

    Caller restricts merging to a single ``player_id``, which makes
    surname-only collisions (two different footballers nicknamed
    "Williams" scoring for the same player) extremely unlikely in
    practice.
    """
    a_toks = a.split()
    b_toks = b.split()
    if not a_toks or not b_toks:
        return False
    if len(a_toks) == 1 and len(b_toks) >= 2:
        return a_toks[0] == b_toks[-1]
    if len(b_toks) == 1 and len(a_toks) >= 2:
        return b_toks[0] == a_toks[-1]
    return False


def _merge_footballer_rows(rows: list[dict]) -> list[dict]:
    """Merge footballer rows with similar names (OCR variations).

    Three-pass strategy:
    1) Exact match on normalized name + player_id (diacritics, doubles, ГОЛ).
    2) Fuzzy pass: within the same player_id, merge names with
       SequenceMatcher.ratio() above threshold (catches single-char
       typos like Mbarpé→Mbappé) **or** the surname-token rule
       (Williams ↔ Nico Williams, Yamal ↔ Lamine Yamal, …).
    3) For each merged group prefer the more specific (multi-token)
       spelling as display name, regardless of which spelling has more
       goals — so "Nico Williams" wins over "Williams" even when they're
       6:5.

    Returns merged rows sorted by total_goals desc.
    """
    from collections import defaultdict
    from difflib import SequenceMatcher

    # Pass 1: exact normalized merge
    # Key: (normalized_name, player_id) → list of original rows
    groups: dict[tuple[str, int], list[dict]] = defaultdict(list)
    for r in rows:
        key = (_normalize_footballer_name(r.get("raw_name", "")), r["player_id"])
        groups[key].append(r)

    # Aggregate each exact-norm group into a single row
    pass1: list[dict] = []
    for (_norm, pid), group in groups.items():
        total = sum(r.get("total_goals", 0) for r in group)
        home = sum(r.get("home_goals", 0) for r in group)
        away = sum(r.get("away_goals", 0) for r in group)
        best_row = max(group, key=lambda r: r.get("total_goals", 0))
        pass1.append({
            "raw_name":      best_row.get("raw_name"),
            "_norm":         _norm,
            "player_id":     pid,
            "username":      best_row.get("username"),
            "game_nickname": best_row.get("game_nickname"),
            "telegram_id":   best_row.get("telegram_id"),
            "home_goals":    home,
            "away_goals":    away,
            "total_goals":   total,
        })

    # Pass 2: fuzzy merge within same player_id
    # Group pass1 rows by player_id, then merge entries with similarity >= 0.75
    by_player: dict[int, list[dict]] = defaultdict(list)
    for r in pass1:
        by_player[r["player_id"]].append(r)

    merged: list[dict] = []
    for pid, player_rows in by_player.items():
        # Sort by total_goals desc so the "canonical" name (most goals) is first
        player_rows.sort(key=lambda r: r["total_goals"], reverse=True)
        used = [False] * len(player_rows)
        for i in range(len(player_rows)):
            if used[i]:
                continue
            # This is the "anchor" row — the dominant spelling
            anchor = player_rows[i]
            anchor_norm = anchor["_norm"]
            for j in range(i + 1, len(player_rows)):
                if used[j]:
                    continue
                other_norm = player_rows[j]["_norm"]
                # Check similarity: SequenceMatcher ratio >= 0.75
                # For short names (len ≤ 6) require >= 0.8
                ratio = SequenceMatcher(None, anchor_norm, other_norm).ratio()
                min_len = min(len(anchor_norm), len(other_norm))
                threshold = 0.80 if min_len <= 6 else 0.75
                if ratio >= threshold or _last_token_match(anchor_norm, other_norm):
                    # Merge into anchor
                    anchor["total_goals"] += player_rows[j]["total_goals"]
                    anchor["home_goals"] += player_rows[j]["home_goals"]
                    anchor["away_goals"] += player_rows[j]["away_goals"]
                    # Prefer the more specific (multi-token) spelling as
                    # the display name. Without this, "Williams" 5 +
                    # "Nico Williams" 6 would surface as "Nico Williams"
                    # only because it's the anchor (more goals); but if
                    # the surname-only spelling were ahead the merged
                    # row would read "Williams 11", losing information.
                    other_tok_count = len(other_norm.split())
                    anchor_tok_count = len(anchor_norm.split())
                    if other_tok_count > anchor_tok_count:
                        anchor["raw_name"] = player_rows[j]["raw_name"]
                        anchor["_norm"] = other_norm
                        anchor_norm = other_norm
                    used[j] = True
            # Remove internal key before output
            row_out = {k: v for k, v in anchor.items() if k != "_norm"}
            merged.append(row_out)

    merged.sort(key=lambda r: r["total_goals"], reverse=True)
    return merged


# ─────────────────────────────────────────────────────────────────────────────
# Telegraph publishing for detailed bombardiers (long stats + "кому забил")
# ─────────────────────────────────────────────────────────────────────────────

def _build_bombardier_telegraph_nodes(
    t: dict,
    rows: list[dict],
    footballer_rows: list[dict],
    footballers_by_player: dict[int, list[tuple[str, int]]],
    vs_data: list[dict],
) -> list[dict]:
    """Build Telegra.ph Node[] for the full bombardier stats page.

    Sections:
    1. Header with tournament info
    2. Player bombardier ranking (goals per player with footballer breakdown)
    3. "Кому забил" — who scored against whom (grouped by scorer)
    """
    from collections import defaultdict
    from datetime import datetime

    nodes: list[dict] = []

    # Header
    t_name = t.get("name") or "Турнир"
    nodes.append({"tag": "h3", "children": [f"⚽ Бомбардиры — {t_name}"]})
    nodes.append({"tag": "p", "children": [
        f"ID турнира: {t['id']} · Тип: {t.get('tournament_type', '—')}"
    ]})

    # Section 1: Player ranking
    if rows:
        nodes.append({"tag": "h3", "children": ["🏆 Рейтинг бомбардиров"]})
        medals = {1: "🥇", 2: "🥈", 3: "🥉"}
        for i, r in enumerate(rows, 1):
            m = medals.get(i, f"{i}.")
            uname = f"@{r['username']}" if r.get("username") else "—"
            nick = f" ({r['game_nickname']})" if r.get("game_nickname") else ""
            # Player header line
            nodes.append({"tag": "p", "children": [
                {"tag": "b", "children": [
                    f"{m} {uname}{nick} — {r['total_goals']} ⚽ "
                    f"({r['home_goals']}🟢 / {r['away_goals']}🔵)"
                ]}
            ]})
            # Footballer sub-list for this player
            flist = footballers_by_player.get(r["player_id"], [])
            if flist:
                fb_sub: list[dict] = []
                for fname, fgoals in flist:
                    fb_sub.append({"tag": "li", "children": [
                        f"⚽ {fname} — {fgoals}"
                    ]})
                nodes.append({"tag": "ul", "children": fb_sub})

    # Section 2: Footballer overall ranking
    if footballer_rows:
        nodes.append({"tag": "h3", "children": ["⚽ Топ футболистов (по голам)"]})
        fb_items: list[dict] = []
        for idx, fr in enumerate(footballer_rows[:30], 1):
            uname = f"@{fr['username']}" if fr.get("username") else "—"
            fb_items.append({"tag": "li", "children": [
                f"{idx}. {fr['raw_name']} — {fr['total_goals']} гол(а) ({uname})"
            ]})
        nodes.append({"tag": "ul", "children": fb_items})

    # Section 3: "Кому забил" — goals against specific opponents
    if vs_data:
        nodes.append({"tag": "hr"})
        nodes.append({"tag": "h3", "children": ["🎯 Кому забил (голы по соперникам)"]})
        nodes.append({"tag": "p", "children": [
            "Подробная разбивка: какой футболист забивал конкретному "
            "сопернику в турнире."
        ]})

        # Group by scorer
        by_scorer: dict[int, list[dict]] = defaultdict(list)
        for row in vs_data:
            by_scorer[row["scorer_id"]].append(row)

        # Sort scorers by total goals (sum across all opponents)
        scorer_totals = sorted(
            by_scorer.items(),
            key=lambda kv: sum(r["goals"] for r in kv[1]),
            reverse=True,
        )

        for scorer_id, opp_rows in scorer_totals:
            scorer_name = opp_rows[0]["scorer_username"] or "—"
            total = sum(r["goals"] for r in opp_rows)
            nodes.append({"tag": "h4", "children": [
                f"@{scorer_name} — {total} гол(а)"
            ]})

            # Group by opponent within this scorer
            by_opp: dict[int, list[dict]] = defaultdict(list)
            for r in opp_rows:
                by_opp[r["opponent_id"]].append(r)

            opp_items: list[dict] = []
            for oid, goals_list in sorted(
                by_opp.items(),
                key=lambda kv: sum(r["goals"] for r in kv[1]),
                reverse=True,
            ):
                opp_name = goals_list[0]["opponent_username"] or "—"
                opp_total = sum(r["goals"] for r in goals_list)
                # List footballers that scored against this opponent
                fb_details = ", ".join(
                    f"{r['raw_name']}×{r['goals']}" if r["goals"] > 1
                    else r["raw_name"]
                    for r in sorted(goals_list, key=lambda x: -x["goals"])
                )
                opp_items.append({"tag": "li", "children": [
                    f"vs @{opp_name} — {opp_total} гол(а): {fb_details}"
                ]})
            nodes.append({"tag": "ul", "children": opp_items})

    # Footer
    nodes.append({"tag": "hr"})
    nodes.append({"tag": "p", "children": [
        f"Сгенерировано ботом GovNL · "
        f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}"
    ]})
    return nodes


def _publish_bombardiers_telegraph(
    t: dict,
    rows: list[dict],
    footballer_rows: list[dict],
    footballers_by_player: dict[int, list[tuple[str, int]]],
    vs_data: list[dict],
) -> str | None:
    """Publish full bombardier stats to Telegraph. Returns URL or None."""
    from tournament_summary import _telegraph_account_token, _telegraph_call

    token = _telegraph_account_token()
    if not token:
        log.warning("Telegraph: no token available for bombardiers publish")
        return None

    nodes = _build_bombardier_telegraph_nodes(
        t, rows, footballer_rows, footballers_by_player, vs_data
    )
    if not nodes:
        return None

    title = f"⚽ Бомбардиры — {(t.get('name') or 'Турнир').strip()}"[:256]
    res = _telegraph_call("createPage", {
        "access_token": token,
        "title":        title,
        "author_name":  "GovNL bot",
        "author_url":   "",
        "content":      nodes,
        "return_content": "false",
    })
    if not res:
        return None
    return res.get("url")


async def cmd_table_bomb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/tablebomb [ID|вса|ри] [text] — таблица бомбардиров одного турнира.

    Считается по таблице ``match_goals``: каждый гол засчитывается
    участнику лиги, чья сторона забила (зелёный = домашний игрок
    матча, синий = гость).

    Рендерит PNG с топ-7 **футболистами** (in-game имена из OCR)
    по количеству голов. Справа — теневой силуэт игрока с эффектом
    энергии/молний.

    Опция ``text`` (или ``текст`` / ``txt``) — только текстовая
    версия без картинки.

    Без аргументов — берёт активный турнир, в котором ты участвуешь.
    Фон берётся из настроек турнира (/set_tournament_bg).
    """
    user = update.effective_user
    requester = _player_from_user(user)

    # Parse args: detect text-mode token and tournament selector
    text_mode = False
    tid_arg = None
    for a in (ctx.args or []):
        if a.lower() in _TABLEBOMB_TEXT_TOKENS:
            text_mode = True
        else:
            tid_arg = a

    t = _resolve_leaderboard_tournament(tid_arg, requester)
    if not t:
        await send(
            update,
            "❌ Не нашёл турнир. Укажи ID: <code>/tablebomb 7</code> "
            "или <code>/tablebomb вса</code>.",
        )
        return

    # Get footballer-level scorers (raw_name based) and merge duplicates
    footballer_rows_raw = db.get_footballer_scorers_for_tournament(t["id"], limit=200)
    footballer_rows = _merge_footballer_rows(footballer_rows_raw)
    # Also get player-level scorers for the text breakdown
    rows = db.get_top_scorers_by_side_for_tournament(t["id"], limit=50)

    # Build a mapping: player_id → list of (raw_name, total_goals) — merged
    footballers_by_player: dict[int, list[tuple[str, int]]] = {}
    for fr in footballer_rows:
        pid = fr["player_id"]
        footballers_by_player.setdefault(pid, []).append(
            (fr["raw_name"], fr["total_goals"])
        )

    creator = get_player_by_id(t["created_by"]) if t.get("created_by") else None
    creator_str = mention(creator["username"]) if creator else "—"

    header = [
        "⚽ <b>Бомбардиры</b>",
        f"<b>{html.escape(t['name'])}</b> [{t_full_label(t)}]  (ID: {t['id']})",
        f"Создал: {creator_str}",
    ]

    if not footballer_rows and not rows:
        header.append(
            "\nПока нет голов. Голы попадают сюда автоматически после "
            "распознавания скрина матча (нужны цветные события — "
            "зелёные/синие)."
        )
        await send(update, "\n".join(header))
        return

    # ── Build text details (expandable blockquote) ────────────────────
    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    detail_lines: list[str] = []

    if rows:
        for i, r in enumerate(rows, 1):
            m = medals.get(i, f"{i}.")
            nick = (
                f" <i>({html.escape(r['game_nickname'])})</i>"
                if r.get("game_nickname") else ""
            )
            # Per-player titles are appended after the goal counts so
            # the bombardier list reads "🥇 @user [🐐 GOAT, Чемпион №76]
            # — N ⚽". Failures here are non-fatal; we just skip the
            # badge and keep the row.
            titles_blob = ""
            try:
                tts = db.player_title_strings(r["player_id"])
                if tts:
                    seen_t: set[str] = set()
                    uniq_t: list[str] = []
                    for t_str in tts:
                        k = (t_str or "").strip().lower()
                        if k in seen_t:
                            continue
                        seen_t.add(k)
                        uniq_t.append(t_str)
                    titles_blob = (
                        " 🏅 "
                        + " • ".join(html.escape(t_str) for t_str in uniq_t)
                    )
            except Exception:
                titles_blob = ""
            detail_lines.append(
                f"{m} {mention(r['username'])}{nick}{titles_blob} — "
                f"<b>{r['total_goals']}</b> ⚽  "
                f"({r['home_goals']}🟢 / {r['away_goals']}🔵)"
            )
            flist = footballers_by_player.get(r["player_id"], [])
            for fname, fgoals in flist:
                detail_lines.append(f"    ⚽ {html.escape(fname)} — {fgoals}")

    # ── Send as single message: photo + caption with expandable text ────
    if not text_mode and footballer_rows:
        try:
            png_bytes = render_tablebomb_png(footballer_rows, tournament=t)
            photo = BytesIO(png_bytes)
            photo.name = "tablebomb.png"

            # Caption: header + expandable blockquote with full details
            caption_parts = list(header)
            if detail_lines:
                details_text = "\n".join(detail_lines)
                caption_parts.append(
                    f"\n<blockquote expandable>{details_text}</blockquote>"
                )
            # Append footer
            _tb_footer = get_random_footer(t, FOOTER_CTX_TABLE)
            if _tb_footer:
                caption_parts.append(_tb_footer)
            caption_text = "\n".join(caption_parts)

            # Telegram caption limit is 1024 chars — if over, truncate
            # the blockquote content and fall back to separate message.
            if len(caption_text) <= 1024:
                await update.effective_message.reply_photo(
                    photo=photo,
                    caption=caption_text,
                    parse_mode="HTML",
                )
                return
            else:
                # Caption too long — send photo with short caption,
                # then text with details separately.
                short_caption = "\n".join(header)[:1024]
                await update.effective_message.reply_photo(
                    photo=photo,
                    caption=short_caption,
                    parse_mode="HTML",
                )
                # Build full details text
                full_lines = list(header)
                if detail_lines:
                    full_lines.append(
                        f"\n<blockquote expandable>"
                        f"{chr(10).join(detail_lines)}</blockquote>"
                    )
                _tb_footer3 = get_random_footer(t, FOOTER_CTX_TABLE)
                if _tb_footer3:
                    full_lines.append(_tb_footer3)
                full_text = "\n".join(full_lines)

                # If even the separate message is too long for Telegram
                # (4096 char limit), publish full stats to Telegraph
                if len(full_text) > 4000:
                    vs_data = get_goals_vs_opponents_for_tournament(t["id"])
                    tg_url = _publish_bombardiers_telegraph(
                        t, rows, footballer_rows,
                        footballers_by_player, vs_data,
                    )
                    if tg_url:
                        short_msg = list(header)
                        # Include top-5 in Telegram as a teaser
                        if detail_lines:
                            teaser = "\n".join(detail_lines[:5])
                            short_msg.append(
                                f"\n<blockquote expandable>"
                                f"{teaser}\n…</blockquote>"
                            )
                        short_msg.append(
                            f'\n📊 <a href="{tg_url}">Полная статистика '
                            f'бомбардиров + кому забил</a>'
                        )
                        if _tb_footer3:
                            short_msg.append(_tb_footer3)
                        await send(update, "\n".join(short_msg))
                        return

                await send(update, full_text)
                return
        except Exception as exc:
            log.warning("tablebomb image generation failed: %s", exc)

    # ── Text-only fallback (text_mode or image failed) ──────────────────
    lines = list(header)
    if detail_lines:
        details_text = "\n".join(detail_lines)
        lines.append(
            f"\n<blockquote expandable>{details_text}</blockquote>"
        )
    _tb_footer2 = get_random_footer(t, FOOTER_CTX_TABLE)
    if _tb_footer2:
        lines.append(_tb_footer2)
    lines.append("<i>Для текстовой версии: /tablebomb текст</i>")
    full_text = "\n".join(lines)

    # If text exceeds Telegram limit, publish to Telegraph
    if len(full_text) > 4000:
        vs_data = get_goals_vs_opponents_for_tournament(t["id"])
        tg_url = _publish_bombardiers_telegraph(
            t, rows, footballer_rows,
            footballers_by_player, vs_data,
        )
        if tg_url:
            short_msg = list(header)
            if detail_lines:
                teaser = "\n".join(detail_lines[:5])
                short_msg.append(
                    f"\n<blockquote expandable>{teaser}\n…</blockquote>"
                )
            short_msg.append(
                f'\n📊 <a href="{tg_url}">Полная статистика '
                f'бомбардиров + кому забил</a>'
            )
            if _tb_footer2:
                short_msg.append(_tb_footer2)
            try:
                await send(update, "\n".join(short_msg))
            except Exception:
                pass
            return

    try:
        await send(update, full_text)
    except Exception:
        pass  # original message may have been deleted


async def cmd_top_scorers(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/top_scorers                — официальные турниры (по умолчанию).
    /top_scorers custom         — кастомные (созданные игроками) турниры.
    /top_scorers all            — все турниры вместе.
    /top_scorers <tournament_id>— конкретный турнир.

    Считается по таблице ``match_goals`` (распознаётся OCR-ом из скрина:
    зелёный сегмент = домашний бомбардир, синий = выездной). Если у
    матча нет goal-events, он не учитывается тут — для общего ELO см.
    /leaderboard.
    """
    args = ctx.args or []
    mode = "official"
    tid_arg: int | None = None
    if args:
        a = args[0].lstrip("@").lower()
        if a in ("custom", "кастом", "private", "user"):
            mode = "custom"
        elif a in ("all", "все", "вс"):
            mode = "all"
        elif a in ("official", "офиц", "main"):
            mode = "official"
        elif a.isdigit():
            mode = "tournament"
            tid_arg = int(a)
        # No tournament-name fallback: arg must be a known mode keyword
        # or a numeric ID.

    if mode == "tournament" and tid_arg is not None:
        t = get_tournament(tid_arg)
        if not t:
            await send(update, f"❌ Турнир ID {tid_arg} не найден.")
            return
        rows = db.get_top_scorers_for_tournament(tid_arg, limit=20)
        title = (
            f"⚽ <b>Бомбардиры — {html.escape(t['name'])}</b> "
            f"<i>(ID {tid_arg})</i>"
        )
    elif mode == "custom":
        rows = db.get_top_scorers_custom(limit=20)
        title = "⚽ <b>Бомбардиры — кастомные турниры</b>"
    elif mode == "all":
        rows = db.get_top_scorers_global(limit=20, only_official=False)
        title = "⚽ <b>Бомбардиры — все турниры</b>"
    else:
        rows = db.get_top_scorers_global(limit=20, only_official=True)
        title = "⚽ <b>Бомбардиры — официальные турниры</b>"

    if not rows:
        # Fallback to legacy goals_scored counter so the command isn't empty
        # right after deploy (when match_goals is still empty).
        legacy = sorted(get_all_players(), key=lambda p: p.get("goals_scored", 0), reverse=True)
        legacy = [p for p in legacy if p.get("goals_scored", 0) > 0]
        if not legacy:
            await send(
                update,
                f"{title}\n\n"
                "Пока нет распознанных голов из скринов.\n"
                "<i>Бомбардиры считаются автоматически после каждого "
                "подтверждённого матча — нужны скрины с цветными "
                "событиями голов (зел/син).</i>",
            )
            return
        lines = [title + "  <i>(legacy: счёт по матчам)</i>", ""]
        medals = {1: "🥇", 2: "🥈", 3: "🥉"}
        for i, p in enumerate(legacy[:10], 1):
            tot = p["wins"] + p["losses"] + p["draws"]
            avg = f"{p['goals_scored']/tot:.1f}" if tot else "—"
            m = medals.get(i, f"{i}.")
            lines.append(
                f"{m} {mention(p['username'])} — <b>{p['goals_scored']}</b> ⚽  "
                f"(avg {avg}/матч)"
            )
        await send(update, "\n".join(lines))
        return

    lines = [title, ""]
    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    for i, r in enumerate(rows, 1):
        m = medals.get(i, f"{i}.")
        lines.append(
            f"{m} {mention(r['username'])} — <b>{r['goals']}</b> ⚽"
        )
    lines.append("")
    lines.append(
        "<i>Раздельные таблицы: /top_scorers · /top_scorers custom · "
        "/top_scorers all · /top_scorers &lt;ID&gt;</i>"
    )
    await send(update, "\n".join(lines))


# ─────────────────────────────────────────────────────────────────────────────
# /feedback — bug/idea reporter (forward to root admins)
# ─────────────────────────────────────────────────────────────────────────────

async def _send_feedback_to_admins(
    ctx: ContextTypes.DEFAULT_TYPE,
    user,
    text: str | None,
    photo_file_id: str | None = None,
):
    """Send a feedback message to every admin DM. Returns count delivered."""
    if not ADMIN_IDS:
        return 0

    user_tag = f"@{user.username}" if user.username else f"id {user.id}"
    name = user.full_name or "—"
    header = (
        f"📬 <b>Фидбек</b>\n"
        f"От: {user_tag} (<i>{html.escape(name)}</i>, tg_id={user.id})\n"
        f"{'─'*30}\n"
    )
    body = (text or "").strip() or "<i>(без текста)</i>"
    payload = header + body

    reply_kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(
            "💬 Ответить",
            callback_data=f"fb_reply:{user.id}",
        )],
    ])

    delivered = 0
    for admin_id in ADMIN_IDS:
        try:
            if photo_file_id:
                await ctx.bot.send_photo(
                    admin_id,
                    photo=photo_file_id,
                    caption=payload[:1000],
                    parse_mode="HTML",
                    reply_markup=reply_kb,
                )
            else:
                await ctx.bot.send_message(
                    admin_id, payload,
                    parse_mode="HTML",
                    reply_markup=reply_kb,
                )
            delivered += 1
        except Exception as e:
            log.warning("feedback delivery to admin %s failed: %s", admin_id, e)
    return delivered


async def cmd_feedback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """One-shot feedback: ``/feedback <text>``. If no text, enter
    ``awaiting_feedback`` mode."""
    text = " ".join(ctx.args).strip()
    if not text:
        ctx.user_data["awaiting_feedback"] = True
        await send(
            update,
            "🐞💡 Напиши свой баг или предложение одним сообщением (можно с фото).\n"
            "Чтобы отменить — /cancel.",
        )
        return

    if not ADMIN_IDS:
        await send(update, "❌ Админы не настроены — некому отправить фидбек.")
        return

    delivered = await _send_feedback_to_admins(ctx, update.effective_user, text)
    if delivered:
        await send(update, f"✅ Спасибо! Доставлено {delivered}/{len(ADMIN_IDS)} админ(ам).")
    else:
        await send(update, "⚠️ Не удалось доставить ни одному админу.")


# ─────────────────────────────────────────────────────────────────────────────
# /bug — bug-only reporter (separate flow from /feedback so admins can
# triage by header, but the underlying delivery is the same).
# ─────────────────────────────────────────────────────────────────────────────


async def _send_bug_to_admins(
    ctx: ContextTypes.DEFAULT_TYPE,
    user,
    text: str | None,
    photo_file_id: str | None = None,
):
    """Send a bug report to every admin DM. Returns count delivered.

    Wire-compatible with ``_send_feedback_to_admins`` (same callback
    flow for the 'Ответить' button) but stamps the message with a
    🐞 ``BUG`` header so admins can spot bug reports immediately in
    their feed.
    """
    if not ADMIN_IDS:
        return 0

    user_tag = f"@{user.username}" if user.username else f"id {user.id}"
    name = user.full_name or "—"
    header = (
        f"🐞 <b>BUG REPORT</b>\n"
        f"От: {user_tag} (<i>{html.escape(name)}</i>, tg_id={user.id})\n"
        f"{'─'*30}\n"
    )
    body = (text or "").strip() or "<i>(без описания)</i>"
    payload = header + body

    reply_kb = InlineKeyboardMarkup([[
        InlineKeyboardButton(
            "💬 Ответить",
            callback_data=f"fb_reply:{user.id}",
        ),
    ]])

    delivered = 0
    for admin_id in ADMIN_IDS:
        try:
            if photo_file_id:
                await ctx.bot.send_photo(
                    admin_id,
                    photo=photo_file_id,
                    caption=payload[:1000],
                    parse_mode="HTML",
                    reply_markup=reply_kb,
                )
            else:
                await ctx.bot.send_message(
                    admin_id, payload,
                    parse_mode="HTML",
                    reply_markup=reply_kb,
                )
            delivered += 1
        except Exception as e:
            log.warning("bug-report delivery to admin %s failed: %s", admin_id, e)
    return delivered


async def cmd_bug(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """``/bug [текст]`` — отправить багрепорт админам.

    Без аргументов — переходит в режим ``awaiting_bug``, и следующее
    сообщение (текст или фото) уходит админам с заголовком
    «🐞 BUG REPORT». Это отдельный слот от ``awaiting_feedback`` так
    что параллельные сессии fb / bug не мешают друг другу.
    """
    user = update.effective_user
    if user is None:
        return

    text = " ".join(ctx.args).strip()
    if not text:
        ctx.user_data["awaiting_bug"] = True
        await send(
            update,
            "🐞 Опиши баг одним сообщением (можно прикрепить скриншот).\n"
            "Что было — что ожидал — что получилось.\n"
            "Отменить: /cancel.",
        )
        return

    if not ADMIN_IDS:
        await send(update, "❌ Админы не настроены — некому отправить отчёт.")
        return

    delivered = await _send_bug_to_admins(ctx, user, text)
    if delivered:
        await send(
            update,
            f"✅ Багрепорт отправлен ({delivered}/{len(ADMIN_IDS)} админ(ам)). "
            f"Спасибо!",
        )
    else:
        await send(update, "⚠️ Не удалось доставить ни одному админу.")


async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cancelled = False
    if ctx.user_data.pop("awaiting_feedback", False):
        cancelled = True
    if ctx.user_data.pop("awaiting_bug", False):
        cancelled = True
    if ctx.user_data.pop("awaiting_fb_reply_to", None) is not None:
        cancelled = True
    if cancelled:
        await send(update, "Окей, отменено.")
    else:
        await send(update, "Нечего отменять.")


__all__ = [
    "_send_top_by_field",
    "_resolve_leaderboard_tournament",
    "_build_official_local_view",
    "_send_feedback_to_admins",
    "_send_bug_to_admins",
    "cmd_top",
    "cmd_top_vsa",
    "cmd_top_ri",
    "cmd_leaderboard",
    "cmd_top_scorers",
    "cmd_table_bomb",
    "cmd_feedback",
    "cmd_bug",
    "cmd_cancel",
]
