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

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

import database as db
from database import (
    get_active_tournaments,
    get_all_players,
    get_all_players_by_elo_field,
    get_player_by_id,
    get_tournament,
    get_tournament_leaderboard,
    get_tournament_players,
)
from elo import rank_label

from handlers._helpers import _player_from_user
from handlers.common import (
    ADMIN_IDS,
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

async def cmd_table_bomb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/tablebomb [ID|вса|ри] — таблица бомбардиров одного турнира.

    Считается по таблице ``match_goals``: каждый гол засчитывается
    участнику лиги, чья сторона забила (зелёный = домашний игрок
    матча, синий = гость). Под каждым игроком выводятся его
    футболисты (in-game имена) с количеством голов.

    Без аргументов — берёт активный турнир, в котором ты участвуешь
    (предпочтительно кастомный, как и /leaderboard); при двусмыс­ленности
    нужен явный ID.
    """
    user = update.effective_user
    requester = _player_from_user(user)

    arg = ctx.args[0] if ctx.args else None
    t = _resolve_leaderboard_tournament(arg, requester)
    if not t:
        await send(
            update,
            "❌ Не нашёл турнир. Укажи ID: <code>/tablebomb 7</code> "
            "или <code>/tablebomb вса</code>.",
        )
        return

    rows = db.get_top_scorers_by_side_for_tournament(t["id"], limit=50)
    footballer_rows = db.get_footballer_scorers_for_tournament(t["id"], limit=200)

    # Build a mapping: player_id → list of (raw_name, total_goals)
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
        "<i>🟢 — голы дома, 🔵 — голы в гостях.</i>",
    ]

    if not rows:
        header.append(
            "\nПока нет голов. Голы попадают сюда автоматически после "
            "распознавания скрина матча (нужны цветные события — "
            "зелёные/синие)."
        )
        await send(update, "\n".join(header))
        return

    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    lines = list(header) + [""]
    for i, r in enumerate(rows, 1):
        m = medals.get(i, f"{i}.")
        nick = (
            f" <i>({html.escape(r['game_nickname'])})</i>"
            if r.get("game_nickname") else ""
        )
        lines.append(
            f"{m} {mention(r['username'])}{nick} — "
            f"<b>{r['total_goals']}</b> ⚽  "
            f"({r['home_goals']}🟢 / {r['away_goals']}🔵)"
        )
        # Show footballer breakdown under this player
        flist = footballers_by_player.get(r["player_id"], [])
        for fname, fgoals in flist:
            lines.append(f"    ⚽ {html.escape(fname)} — {fgoals}")
    await send(update, "\n".join(lines))


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


async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cancelled = False
    if ctx.user_data.pop("awaiting_feedback", False):
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
    "cmd_top",
    "cmd_top_vsa",
    "cmd_top_ri",
    "cmd_leaderboard",
    "cmd_top_scorers",
    "cmd_table_bomb",
    "cmd_feedback",
    "cmd_cancel",
]
