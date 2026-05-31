"""Tournament lifecycle, settings and bracket commands (Phase 5 of the
bot.py split).

Owns the full tournament state machine (creation, roster management,
groups draw, group-stage start, /redraw_groups, /start_playoff,
/advance_playoff, /finish_tournament, /simulate, /prune_phantoms,
/bind_tournament + chat binding, the inline ``/settings`` panel, and
all the tournament-aware /set_* commands) plus the picker callbacks
that let users tap a recently-finished tournament to view its bracket
or table.

Re-exported from ``bot`` for backward compatibility — the old
``from bot import cmd_create_tournament, cb_advance_now`` forms keep
working.

A handful of names (``submenu_tournament_settings`` for the inline
settings panel) live in bot.py for now; we lazy-import them inside
function bodies to avoid the bot ↔ handlers.tournament import cycle.
The same pattern (lazy ``from bot import ...``) is used for any
remaining bot.py-only globals these handlers still reach into.
"""

from __future__ import annotations

import asyncio
import html
import io
import logging
import math
import random
import re
from datetime import datetime, timedelta, timezone

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.error import TelegramError
from telegram.ext import ContextTypes

import database as db
from database import (
    add_player_to_tournament,
    count_group_matches_for_pair,
    create_match,
    create_tournament,
    get_active_tournament,
    get_active_tournaments,
    get_player,
    get_player_by_id,
    get_tournament,
    get_tournament_by_chat,
    get_tournament_leaderboard,
    get_tournament_matches,
    get_tournament_players,
    is_player_banned,
    log_tournament_action,
    set_tournament_chat,
    unset_tournament_chat,
    update_match,
    update_tournament,
    upsert_player,
)
from match_processor import apply_result
from playoff_image import render_playoff_png
from standings_image import (
    list_standings_groups,
    render_standings_png,
    render_standings_png_for_group,
    render_standings_pngs,
)
from tournament import (
    GROUP_LETTERS,
    PLAYOFF_STAGES,
    _dedup_playoff_legs,
    _resolve_pair_winner,
    draw_groups,
    format_playoff_bracket,
    format_standings_message,
    generate_group_fixtures,
    generate_playoff,
    get_tournament_podium,
)

from handlers._helpers import (
    _can_manage_tournament,
    _player_from_user,
    _resolve_tournament_from_args,
)
from handlers.common import (
    _fmt_dt,
    _fmt_minute_local,
    _local_to_utc_str,
    _tz_label,
    check_required_channel,
    is_admin,
    is_root_admin,
    mention,
    parse_tournament_type_arg,
    send,
    t_full_label,
    t_type_label,
)
from handlers.match import (
    _announce_stage_advance,
    _current_playoff_stage,
    _maybe_auto_advance,
)

log = logging.getLogger(__name__)



def _bot_submenu_tournament_settings(t):
    """Lazy import of ``bot.submenu_tournament_settings`` to avoid the
    bot ↔ handlers.tournament import cycle."""
    from bot import submenu_tournament_settings
    return submenu_tournament_settings(t)


# Tournament sizing limits — bumped 2026-05 so the bot can host very
# large communities. The lower bound is 1 group of 2 players (always
# valid); the upper bounds are practical caps to keep group fixtures
# and bracket images from becoming unmanageable. ``_GROUP_SIZE_MAX``
# bounds the per-group roster (round-robin grows quadratically — 100
# players in one group is already 4950 matches). ``_GROUPS_COUNT_MAX``
# bounds how many groups can exist (set high so big tournaments can
# split into many small groups instead of a few giant ones).
_GROUP_SIZE_MAX = 100
_GROUPS_COUNT_MAX = 32


# ─────────────────────────────────────────────────────────────────────────────
# /create_tournament  — только для админов
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_create_tournament(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    creator = _player_from_user(user)
    if not creator:
        await send(update, "❌ Сначала зарегистрируйся: /register")
        return

    # Only admins can create tournaments. Custom user-made tournaments
    # were removed — everything is now run by organisers only.
    if not is_admin(user.id):
        await send(
            update,
            "❌ Создавать турниры могут только админы.\n"
            "Если у тебя есть идея турнира — напиши организатору через "
            "<b>🐞 Связь</b> в меню.",
        )
        return

    # Args: <name words...> <type=vsa|ri>
    if not ctx.args:
        await send(
            update,
            "Использование: <code>/create_tournament Название вса</code>\n"
            "или <code>/create_tournament Название ри</code>",
        )
        return

    args = list(ctx.args)

    # ── Auto-tech-loss flag (anywhere in the command line). ─────────────
    # ``тех`` / ``tech`` enables it with the default ``0:3``. A trailing
    # ``X:Y`` score (e.g. ``0:0``) overrides the score AND implicitly
    # enables the flag, so ``/create_tournament Лига вса 0:0`` is enough.
    auto_tech_loss = False
    auto_tech_score: str | None = None
    bracket_only = False
    groups_only = False
    league_mode = False

    def _is_tech_marker(tok: str) -> bool:
        return tok.lower() in (
            "тех", "tech", "techloss", "техпоражение", "tloss",
        )

    def _is_bracket_only_marker(tok: str) -> bool:
        # "плейофф", "playoff", "бракет", "bracket", "ko" — any of these
        # marks the tournament as bracket-only (no group stage).
        return tok.lower() in (
            "плейофф", "плей-офф", "playoff", "play-off",
            "бракет", "bracket", "ko", "knockout",
        )

    def _is_groups_only_marker(tok: str) -> bool:
        # "только_группы", "groups_only", "беsплейофф", etc. mark the
        # tournament as group-stage-only — the playoff is skipped and
        # the winner is whoever tops the (combined) group table when
        # the group stage finishes.
        return tok.lower() in (
            "группы", "групп", "groups", "groupsonly", "groups_only",
            "только_группы", "только-группы", "безплейофф", "без_плейофф",
            "no_playoff", "noplayoff",
        )

    def _is_league_marker(tok: str) -> bool:
        # "лига", "league", "чемпионат", "championship" — creates a
        # single-group round-robin league (everyone plays everyone,
        # no playoff, table determines the winner).
        return tok.lower() in (
            "лига", "league", "чемпионат", "championship",
            "чемп", "champ", "круговой", "круговая",
        )

    def _is_score_token(tok: str) -> bool:
        if ":" not in tok:
            return False
        a, _, b = tok.partition(":")
        return a.isdigit() and b.isdigit() and 0 <= int(a) <= 99 and 0 <= int(b) <= 99

    leftover: list[str] = []
    for a in args:
        if _is_tech_marker(a):
            auto_tech_loss = True
            continue
        if _is_bracket_only_marker(a):
            bracket_only = True
            continue
        if _is_groups_only_marker(a):
            groups_only = True
            continue
        if _is_league_marker(a):
            league_mode = True
            groups_only = True
            continue
        if _is_score_token(a):
            auto_tech_score = a
            auto_tech_loss = True
            continue
        leftover.append(a)
    args = leftover
    # Sanity: groups-only and bracket-only are mutually exclusive
    # markers. If both somehow ended up true, groups-only wins (it's
    # the more conservative choice — no auto-bracket spawning).
    if groups_only and bracket_only:
        bracket_only = False

    # Optional trailing numeric args: groups_count, players_per_group.
    # Pull them off the right side first so the type-arg parser sees
    # the type next.
    explicit_groups = None
    explicit_per_group = None
    while args and args[-1].isdigit():
        n = int(args[-1])
        if explicit_per_group is None:
            explicit_per_group = n
        elif explicit_groups is None:
            explicit_groups = n
        else:
            break
        args = args[:-1]
    # If only one numeric was given, treat it as groups_count (more common).
    if explicit_per_group is not None and explicit_groups is None:
        explicit_groups, explicit_per_group = explicit_per_group, None

    t_type = parse_tournament_type_arg(args[-1]) if args else None
    if t_type:
        args = args[:-1]
    name = " ".join(args).strip() if args else f"Турнир #{datetime.utcnow().strftime('%d%m%y')}"

    if not t_type:
        await send(
            update,
            "❌ Укажи тип турнира: <b>вса</b> или <b>ри</b>.\n"
            "Примеры:\n"
            "  <code>/create_tournament Сезон1 вса</code>\n"
            "  <code>/create_tournament Сезон1 вса 4</code> — 4 группы\n"
            "  <code>/create_tournament Сезон1 вса 4 6</code> — 4 группы по 6",
        )
        return

    # Multiple active tournaments of the same type are allowed. Commands
    # without an explicit ID auto-pick the most recent matching tournament,
    # and chat-bound commands route to the tournament bound to the chat.
    # If you want to disambiguate manually, use ``/bind_tournament <ID>``
    # or pass the tournament ID explicitly (e.g. ``/redraw_groups 7``).

    # Only admins reach this point (gated above), so every tournament here
    # is "official" — matches feed the global ELO/ELO_VSA/ELO_RI pools.

    # If the command is sent in a group/channel, auto-bind that chat to the
    # new tournament so screenshots posted there go straight to it.
    chat = update.effective_chat
    auto_bind_chat_id = None
    if chat is not None and chat.type in ("group", "supergroup", "channel"):
        auto_bind_chat_id = chat.id

    tid = create_tournament(
        name,
        tournament_type=t_type,
        created_by=creator["id"],
        is_official=True,
        chat_id=auto_bind_chat_id,
    )
    # Persist explicit group prefs from CLI args, if any.
    if explicit_groups is not None:
        update_tournament(
            tid,
            groups_count=max(1, min(_GROUPS_COUNT_MAX, explicit_groups)),
        )
    if explicit_per_group is not None:
        update_tournament(
            tid,
            target_group_size=max(2, min(_GROUP_SIZE_MAX, explicit_per_group)),
        )
    if auto_tech_loss:
        update_tournament(
            tid,
            auto_tech_loss_enabled=1,
            auto_tech_loss_score=auto_tech_score or "0:3",
        )
    if bracket_only:
        update_tournament(tid, bracket_only=1)
    if groups_only:
        update_tournament(tid, groups_only=1)
    if league_mode:
        # League = single group, everyone plays everyone
        update_tournament(tid, groups_count=1)
    scope_line = (
        "🌐 Тип лидерборда: <b>общий</b> — матчи влияют на общий ELO "
        f"и ELO {t_type_label(t_type)}."
    )
    bind_line = ""
    if auto_bind_chat_id is not None:
        bind_line = (
            "\n🔗 Чат привязан к турниру — все скрины, отправленные сюда, "
            "будут автоматически засчитаны.\n"
            "(Отвязать: /unbind_tournament)"
        )
    explicit_summary = ""
    if explicit_groups is not None or explicit_per_group is not None:
        parts = []
        if explicit_groups is not None:
            parts.append(f"<b>{explicit_groups}</b> групп")
        if explicit_per_group is not None:
            parts.append(f"по <b>{explicit_per_group}</b> игроков")
        explicit_summary = "\n📐 Жеребьёвка: " + ", ".join(parts) + "."
    if auto_tech_loss:
        explicit_summary += (
            f"\n⏰ Авто-техпоражение при просрочке дедлайна: "
            f"<b>{auto_tech_score or '0:3'}</b>"
        )
    if bracket_only:
        explicit_summary += (
            "\n🏁 Формат: <b>сразу плей-офф</b> (без групп). "
            "Все добавленные игроки попадут в сидованный бракет с байями "
            "по глобальному ELO."
        )
    if groups_only:
        if league_mode:
            explicit_summary += (
                "\n🏅 Формат: <b>лига (чемпионат)</b> — все играют против всех "
                "в одной группе. Победитель определяется по таблице. "
                "Плей-офф не запускается."
            )
        else:
            explicit_summary += (
                "\n📊 Формат: <b>только группы</b> (без плей-офф). "
                "Победителем становится лидер общей группы (по итогам "
                "круга). Плей-офф не запускается."
            )

    start_hint = (
        "Запусти бракет: <code>/start_tournament</code>"
        if bracket_only
        else "Запусти жеребьёвку: <code>/start_tournament</code>"
    )
    await send(
        update,
        f"🏆 Турнир <b>{name}</b> ({t_type_label(t_type)}) создан (ID: {tid})\n"
        f"👤 Создатель: {mention(creator['username'])}\n"
        f"{scope_line}{bind_line}{explicit_summary}\n\n"
        f"Добавляй игроков: <code>/add_player @user1, @user2, ...</code>\n"
        f"{start_hint}",
    )

    # В bracket-only режиме групп нет — пропускаем пикер.
    if explicit_groups is None and not bracket_only and not groups_only:
        rows: list[list[InlineKeyboardButton]] = []
        # Common picks (1..10 each on 2 rows) plus a row of bigger
        # presets — 12, 16, 20, 32 — for huge tournaments. Admins can
        # always type an arbitrary number via the CLI form.
        common = list(range(1, 11)) + [12, 16, 20, 32]
        row: list[InlineKeyboardButton] = []
        for n in common:
            row.append(
                InlineKeyboardButton(str(n), callback_data=f"tcg:groups:{tid}:{n}")
            )
            if len(row) == 5:
                rows.append(row); row = []
        if row:
            rows.append(row)
        rows.append([
            InlineKeyboardButton("⏭ Авто (по числу игроков)", callback_data=f"tcg:groups:{tid}:0"),
        ])
        rows.append([
            InlineKeyboardButton("❌ Отмена", callback_data=f"tcg:cancel:{tid}"),
        ])
        await update.effective_message.reply_text(
            "🏟 Сколько групп должно быть в турнире? "
            "(Можно выбрать сейчас или позже через <code>/redraw_groups</code>.)",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(rows),
        )


# ─────────────────────────────────────────────────────────────────────────────
# /tournaments
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_tournaments(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    actives = get_active_tournaments()
    if not actives:
        await send(update, "Нет активных турниров. Создай: <code>/create_tournament Имя вса</code>")
        return

    lines = ["🏆 <b>Активные турниры</b>\n"]
    user_id = update.effective_user.id if update.effective_user else 0
    finish_buttons: list[list[InlineKeyboardButton]] = []
    for t in actives:
        members = get_tournament_players(t["id"])
        creator = get_player_by_id(t["created_by"]) if t.get("created_by") else None
        creator_str = mention(creator["username"]) if creator else "—"
        scope_icon = "🌐" if t.get("is_official", 1) else "🏠"
        block = [
            f"• <b>{t['name']}</b> [{t_full_label(t)}] {scope_icon} "
            f"— {len(members)} игр., этап: <i>{t['stage']}</i>",
            f"  ID: {t['id']}, создал: {creator_str}",
        ]
        if not t.get("is_official", 1):
            block.append(
                f"  🏠 Локальный лидерборд (общий ELO не задевается). "
                f"Топ: <code>/leaderboard {t['id']}</code>"
            )
        if t.get("description"):
            block.append(f"  📝 {t['description']}")
        if t.get("required_channel"):
            block.append(f"  🔗 Условие: подписка на <b>{t['required_channel']}</b>")
        lines.append("\n".join(block))
        # Add a "Finish" button for users who can manage this tournament.
        # Use the "ask" variant so we show a confirmation prompt (the
        # immediate-finish callback is only used from inside that prompt).
        if _can_manage_tournament(user_id, t):
            finish_buttons.append([InlineKeyboardButton(
                f"🏁 Завершить «{t['name']}» (ID {t['id']})",
                callback_data=f"fin_ask:{t['id']}",
            )])

    text = "\n\n".join(lines)
    kwargs: dict = {"parse_mode": "HTML"}
    if finish_buttons:
        kwargs["reply_markup"] = InlineKeyboardMarkup(finish_buttons)
    if update.callback_query:
        await update.callback_query.message.reply_text(text, **kwargs)
    else:
        await update.message.reply_text(text, **kwargs)


# ─────────────────────────────────────────────────────────────────────────────
# /add_player  — создатель турнира или админ
# ─────────────────────────────────────────────────────────────────────────────

def _parse_add_player_usernames(raw_args: list[str]) -> list[str]:
    """Split ``/add_player`` raw args into normalized usernames.

    Accepts any mix of separators — spaces, commas, semicolons — and
    leading ``@``. Preserves the order of first occurrence and dedupes
    case-insensitively. Returns lowercased usernames, no ``@``.

    Examples::

        ["@a,", "@b", "@c"]          -> ["a", "b", "c"]
        ["@a,@b,@c"]                 -> ["a", "b", "c"]
        ["@A,", "@a"]                -> ["a"]
        ["@a;", "@b @c"]             -> ["a", "b", "c"]
    """
    out: list[str] = []
    seen: set[str] = set()
    for tok in raw_args:
        # Split each raw arg on commas/semicolons/whitespace so ``@a,@b``
        # and ``@a, @b`` both work, regardless of how the user typed it.
        for part in re.split(r"[,\s;]+", tok or ""):
            name = part.strip().lstrip("@").lower()
            if not name:
                continue
            if name in seen:
                continue
            seen.add(name)
            out.append(name)
    return out


async def cmd_add_player(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Add one or more players to the active tournament.

    Usage::

        /add_player @user                       — single
        /add_player @a, @b, @c                  — bulk (commas optional)
        /add_player @a @b @c вса                — bulk + explicit type

    The trailing token is treated as the tournament type (``вса``/``ри``)
    only when it does not look like a username (no ``@`` and not in the
    leftover after comma-splitting). Each username is added independently
    and the bot replies with a per-user summary so partial failures
    (already added, banned, channel-locked) don't abort the whole batch.
    """
    if not ctx.args:
        await send(
            update,
            "Использование: <code>/add_player @user1, @user2, ... [вса|ри]</code>",
        )
        return

    args = list(ctx.args)
    t_type = parse_tournament_type_arg(args[-1]) if len(args) > 1 else None
    if t_type:
        args = args[:-1]

    usernames = _parse_add_player_usernames(args)
    if not usernames:
        await send(
            update,
            "Использование: <code>/add_player @user1, @user2, ... [вса|ри]</code>",
        )
        return

    t = get_active_tournament(tournament_type=t_type)
    if not t:
        await send(update, "❌ Нет активного турнира. Сначала /create_tournament")
        return

    if not _can_manage_tournament(update.effective_user.id, t):
        await send(update, "❌ Управлять турниром может только создатель или админ.")
        return

    existing_ids = {tp["player_id"] for tp in get_tournament_players(t["id"])}

    added: list[str] = []
    already: list[str] = []
    banned: list[tuple[str, str]] = []   # (username, banned_until)
    no_channel: list[str] = []

    for username in usernames:
        p = get_player(username) or upsert_player(username)

        if is_player_banned(p):
            until = _fmt_dt(p.get("banned_until")) or "—"
            banned.append((username, until))
            continue

        if t.get("required_channel"):
            ok, _msg = await check_required_channel(
                ctx, p.get("telegram_id"), t["required_channel"]
            )
            if not ok:
                no_channel.append(username)
                continue

        if p["id"] in existing_ids:
            already.append(username)
            continue

        add_player_to_tournament(t["id"], p["id"], "?")
        existing_ids.add(p["id"])
        added.append(username)
        log_tournament_action(
            t["id"],
            actor_telegram_id=update.effective_user.id,
            actor_username=update.effective_user.username,
            action="add_player",
            details=f"target=@{username}",
        )

    total = len(get_tournament_players(t["id"]))
    label = f"<b>{html.escape(t['name'])}</b> [{t_type_label(t['tournament_type'])}]"

    lines: list[str] = []
    if added:
        lines.append(
            f"✅ Добавлено в {label}: "
            + ", ".join(mention(u) for u in added)
        )
    if already:
        lines.append(
            "⚠️ Уже в турнире: " + ", ".join(mention(u) for u in already)
        )
    if banned:
        lines.append(
            "🚫 В бане (пропущены): "
            + ", ".join(f"{mention(u)} (до {until})" for u, until in banned)
        )
    if no_channel:
        ch = t.get("required_channel") or ""
        lines.append(
            "🔒 Не подписаны на " + ch + " (пропущены): "
            + ", ".join(mention(u) for u in no_channel)
        )
    lines.append(f"\nВсего игроков в турнире: <b>{total}</b>")

    await send(update, "\n".join(lines))


# ─────────────────────────────────────────────────────────────────────────────
# /list_players — show tournament roster (admin + creator + participants)
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_list_players(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """``/list_players [ID]`` — current roster of the tournament.

    Without an ID — uses the chat-bound tournament if present, else the
    most recent active one. Groups players by ``group_name`` (or marks
    them ``Лобби`` if the draw hasn't happened yet) and flags banned
    players inline.
    """
    t, err = _resolve_tournament_from_args(update, ctx)
    if t is None:
        await send(update, err or "❌ Не нашёл турнир.")
        return

    rows = get_tournament_players(t["id"])
    if not rows:
        await send(
            update,
            f"📭 В турнире <b>{html.escape(t['name'])}</b> "
            f"(ID {t['id']}) пока никого нет.",
        )
        return

    # Group by group_name. ``"?"`` / NULL → lobby (draw hasn't run yet).
    by_group: dict[str, list[dict]] = {}
    for r in rows:
        g = (r.get("group_name") or "").strip()
        key = g if g and g != "?" else ""
        by_group.setdefault(key, []).append(r)

    lines: list[str] = [
        f"👥 <b>{html.escape(t['name'])}</b> "
        f"[{t_full_label(t)}] · ID {t['id']}",
        f"Игроков: <b>{len(rows)}</b>",
        "",
    ]

    def _fmt_player(r: dict) -> str:
        u = r.get("username") or ""
        tag = mention(u)
        # Reuse get_player_by_id so banned_until is fresh.
        p = get_player_by_id(r["player_id"])
        if p and is_player_banned(p):
            until = _fmt_dt(p.get("banned_until")) or "?"
            tag += f" 🚫(до {until})"
        if int(r.get("eliminated") or 0):
            tag += " ❌"
        return tag

    if "" in by_group:
        # Pre-draw lobby — flat list.
        roster = by_group.pop("")
        lines.append(
            "<b>В лобби:</b> " + ", ".join(_fmt_player(r) for r in roster)
        )
    for g in sorted(by_group.keys()):
        members = by_group[g]
        lines.append(
            f"<b>Группа {html.escape(g)}:</b> "
            + ", ".join(_fmt_player(r) for r in members)
        )

    await send(update, "\n".join(lines))


# ─────────────────────────────────────────────────────────────────────────────
# /replace_player @old @new — swap a player mid-tournament
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_replace_player(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """``/replace_player @old @new [ID]`` — swap a player in a running tournament.

    Replaces ``@old`` with ``@new`` in:
      • ``tournament_players`` (group + group stats stay with the slot);
      • all pending/reported ``matches`` for that tournament
        (already-confirmed matches keep ``@old`` for accurate history);
      • ``tournament_elo`` row (so isolated-ELO tournaments don't reset).

    Admin and tournament-creator only.
    """
    if len(ctx.args) < 2:
        await send(
            update,
            "Использование: <code>/replace_player @old @new [ID]</code>",
        )
        return

    old_username = ctx.args[0].strip().lstrip("@").lower()
    new_username = ctx.args[1].strip().lstrip("@").lower()
    if not old_username or not new_username:
        await send(update, "❌ Имена не должны быть пустыми.")
        return
    if old_username == new_username:
        await send(update, "❌ Старый и новый игрок должны различаться.")
        return

    # Resolve tournament: 3rd positional arg as ID, else chat/active.
    t, err = _resolve_tournament_from_args(update, ctx, args=list(ctx.args[2:]))
    if t is None:
        await send(update, err or "❌ Турнир не найден.")
        return
    if not _can_manage_tournament(update.effective_user.id, t):
        await send(update, "❌ Только создатель турнира или админ.")
        return

    old_p = get_player(old_username)
    if not old_p:
        await send(update, f"❌ Игрок {mention(old_username)} не найден.")
        return
    new_p = get_player(new_username) or upsert_player(new_username)
    if is_player_banned(new_p):
        until = _fmt_dt(new_p.get("banned_until")) or "?"
        await send(
            update,
            f"❌ {mention(new_username)} в бане до <b>{until}</b>, "
            f"замена невозможна.",
        )
        return

    roster_ids = {r["player_id"] for r in get_tournament_players(t["id"])}
    if old_p["id"] not in roster_ids:
        await send(
            update,
            f"❌ {mention(old_username)} не участвует в турнире "
            f"<b>{html.escape(t['name'])}</b> (ID {t['id']}).",
        )
        return
    if new_p["id"] in roster_ids:
        await send(
            update,
            f"❌ {mention(new_username)} уже в турнире "
            f"<b>{html.escape(t['name'])}</b>. Сначала исключите его, "
            f"если хотите поменять местами.",
        )
        return

    if t.get("required_channel"):
        ok, _msg = await check_required_channel(
            ctx, new_p.get("telegram_id"), t["required_channel"]
        )
        if not ok:
            await send(
                update,
                f"❌ {mention(new_username)} не подписан на "
                f"{t['required_channel']} — не могу заменить.",
            )
            return

    summary = db.replace_tournament_player(t["id"], old_p["id"], new_p["id"])

    lines = [
        f"🔁 Замена в турнире <b>{html.escape(t['name'])}</b> "
        f"[{t_full_label(t)}]",
        f"  {mention(old_username)} → {mention(new_username)}",
        f"  Перенесено матчей: <b>{summary['matches_moved']}</b> "
        f"(подтверждённые остались за {mention(old_username)})",
    ]
    await send(update, "\n".join(lines))

    # Notify the new player.
    if new_p.get("telegram_id"):
        try:
            await ctx.bot.send_message(
                new_p["telegram_id"],
                f"🏆 Тебя добавили в турнир <b>{html.escape(t['name'])}</b> "
                f"вместо {mention(old_username)}.\n"
                f"Используй <code>/matches</code> чтобы увидеть свои матчи.",
                parse_mode="HTML",
            )
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────────────
# /start_tournament
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_start_tournament(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    Start a tournament: draw groups (one-time) and generate group-stage
    fixtures. Idempotent — if the bracket has already been drawn, this
    just shows the existing groups instead of re-rolling. To force a new
    draw, use ``/redraw_groups <ID>``.
    """
    t_type = parse_tournament_type_arg(ctx.args[0]) if ctx.args else None
    t = get_active_tournament(tournament_type=t_type)
    if not t:
        await send(update, "❌ Нет активного турнира.")
        return

    if not _can_manage_tournament(update.effective_user.id, t):
        await send(update, "❌ Только создатель турнира или админ.")
        return

    players = get_tournament_players(t["id"])
    if len(players) < 2:
        await send(update, "❌ Нужно минимум 2 игрока.")
        return

    # ── Bracket-only tournaments: skip the group stage entirely and
    #     build the seeded knockout bracket immediately.
    if int(t.get("bracket_only") or 0):
        # Idempotency: if a bracket already exists, just point the user
        # at it instead of recreating.
        existing_bracket = [
            m for m in db.get_tournament_matches(t["id"])
            if (m.get("stage") or "") in (
                "r512", "r256", "r128", "r64", "r32", "r16",
                "qf", "sf", "final", "third",
            )
        ]
        if existing_bracket:
            await send(
                update,
                f"ℹ️ Бракет для <b>{html.escape(t['name'])}</b> уже создан. "
                f"Посмотри: <code>/playoff {t['id']}</code>.",
            )
            return

        matches_info = generate_playoff(t["id"])
        if not matches_info:
            await send(update, "❌ Нужно минимум 2 игрока для бракета.")
            return
        update_tournament(t["id"], stage="playoff", playoff_started=1)

        n_players = len(players)
        first_stage = matches_info[0]["stage"]
        stage_ru = {
            "r512": "1/256 финала", "r256": "1/128 финала",
            "r128": "1/64 финала", "r64": "1/32 финала",
            "r32": "1/16 финала", "r16": "1/8 финала",
            "qf": "Четвертьфинал", "sf": "Полуфинал", "final": "Финал",
        }.get(first_stage, first_stage.upper())
        bye_count = sum(1 for m in matches_info if m.get("bye"))
        real_count = sum(1 for m in matches_info if not m.get("bye"))

        lines = [
            f"🏆 <b>Плей-офф запущен!</b> [{t_full_label(t)}]",
            f"Турнир <b>{html.escape(t['name'])}</b> идёт без групп — "
            f"сразу сетка.",
            f"\n👥 Игроков: <b>{n_players}</b>",
            f"📐 Стадия старта: <b>{stage_ru}</b>",
        ]
        if bye_count:
            lines.append(
                f"🎟 Bye (топ-сиды без матча): <b>{bye_count}</b>"
            )
        lines.append(f"⚔️ Реальных матчей в первом круге: <b>{real_count}</b>")
        lines.append("\n<b>Пары:</b>")
        seen_pairs: set[tuple[int, int]] = set()
        for mi in matches_info:
            if mi.get("bye"):
                lines.append(f"🎟  {mention(mi['player1'])} → bye")
                continue
            pa = get_player(mi["player1"]) or {}
            pb = get_player(mi["player2"]) or {}
            key = (
                min(pa.get("id") or 0, pb.get("id") or 0),
                max(pa.get("id") or 0, pb.get("id") or 0),
            )
            if key in seen_pairs:
                continue
            seen_pairs.add(key)
            lines.append(
                f"⚔️  {mention(mi['player1'])} vs {mention(mi['player2'])}"
            )
        await send(update, "\n".join(lines))

        # DM the players paired in the first real round.
        for mi in matches_info:
            if mi.get("bye"):
                continue
            for uname in (mi["player1"], mi["player2"]):
                p = get_player(uname)
                if p and p.get("telegram_id"):
                    try:
                        opp = (mi["player2"] if uname == mi["player1"]
                               else mi["player1"])
                        await ctx.bot.send_message(
                            p["telegram_id"],
                            f"⚔️ <b>Твой матч плей-офф!</b>\n\n"
                            f"{mention(uname)} vs {mention(opp)}\n"
                            f"⏳ Срок: 48 часов",
                            parse_mode="HTML",
                        )
                    except Exception:
                        pass
        return

    # ── If the draw already happened, never silently re-roll. ────────────
    existing_group_matches = [
        m for m in db.get_tournament_matches(t["id"])
        if m.get("stage") == "group"
    ]

    # "?" is the lobby sentinel set by /add_player — it does not count
    # as a real assignment. Real group letters (A, B, …) do.
    def _is_real_group(name: str | None) -> bool:
        return bool(name) and name not in ("?", "", "Лобби")

    manual_groups: dict[str, list[int]] = {}
    lobby_pids: list[int] = []
    for p in players:
        g = (p.get("group_name") or "").strip()
        if _is_real_group(g):
            manual_groups.setdefault(g, []).append(p["player_id"])
        else:
            lobby_pids.append(p["player_id"])

    if existing_group_matches:
        groups_disp: dict[str, list[int]] = {}
        for p in players:
            g = p.get("group_name") or "?"
            groups_disp.setdefault(g, []).append(p["player_id"])
        lines = [
            f"ℹ️ Жеребьёвка для <b>{html.escape(t['name'])}</b> уже проведена. "
            f"Текущие группы:\n"
        ]
        for g, pids_g in sorted(groups_disp.items()):
            names = [get_player_by_id(pid)["username"] for pid in pids_g if get_player_by_id(pid)]
            lines.append(f"<b>Группа {g}:</b> {', '.join(mention(u) for u in names)}")
        lines.append(
            f"\nЕсли хочешь перетряхнуть, используй "
            f"<code>/redraw_groups {t['id']}</code>."
        )
        await send(update, "\n".join(lines))
        return

    # ── Manual draw path: admin used /set_group to place players, then
    #     hits /start_tournament. We honor existing assignments instead
    #     of shuffling. If some players are still in lobby ("?"), we
    #     refuse so nobody falls through the cracks.
    if manual_groups:
        if lobby_pids:
            unassigned_names = [
                get_player_by_id(pid)["username"]
                for pid in lobby_pids if get_player_by_id(pid)
            ]
            await send(
                update,
                "❌ Часть игроков ещё в лобби (нет группы): "
                + ", ".join(mention(u) for u in unassigned_names)
                + "\n\n"
                "Раскинь их через "
                f"<code>/set_group {t['id']} A @user1, @user2</code> "
                "или сбрось ручную раздачу командой "
                f"<code>/clear_groups {t['id']}</code> и я раскидаю случайно.",
            )
            return

        groups_count = len(manual_groups)
        update_tournament(t["id"], groups_count=groups_count)
        groups = manual_groups
        mids = generate_group_fixtures(t["id"], groups)

        lines = [
            f"🎯 <b>Ручная раздача принята!</b>\n"
            f"Турнир <b>{html.escape(t['name'])}</b> [{t_full_label(t)}] стартует!\n"
        ]
        for g, pids_g in sorted(groups.items()):
            names = [get_player_by_id(pid)["username"] for pid in pids_g]
            lines.append(f"<b>Группа {g}:</b> {', '.join(mention(u) for u in names)}")
        lines.append(f"\n📅 Создано матчей: <b>{len(mids)}</b>")
        lines.append("Срок каждого матча: <b>48 часов</b>")
        lines.append("\nРепортуй результат: <code>/report 3:2 @opponent</code>\n"
                     "или просто пришли фото скрина — бот распознает.")
        await send(update, "\n".join(lines))

        for pid in [p["player_id"] for p in get_tournament_players(t["id"])]:
            player = get_player_by_id(pid)
            if player and player.get("telegram_id"):
                try:
                    await ctx.bot.send_message(
                        player["telegram_id"],
                        f"🏆 Турнир <b>{html.escape(t['name'])}</b> "
                        f"[{t_full_label(t)}] начался!\n"
                        f"Используй <code>/table</code> чтобы увидеть свою группу.",
                        parse_mode="HTML",
                    )
                except Exception:
                    pass
        return

    n = len(players)
    # Honor explicit preferences set at /create_tournament time:
    #   - groups_count (1..10) — direct.
    #   - target_group_size (2..100) — derive groups_count = ceil(n / size).
    # Otherwise fall back to the legacy auto-heuristic (2..4 groups).
    pref_groups = int(t.get("groups_count") or 0)
    pref_size   = int(t.get("target_group_size") or 0)
    if pref_groups > 0:
        groups_count = max(1, min(_GROUPS_COUNT_MAX, pref_groups))
    elif pref_size > 0:
        groups_count = max(1, -(-n // pref_size))
    else:
        groups_count = max(2, min(n // 3, 4))  # 2–4 groups (legacy default)
    # Don't make more groups than players
    groups_count = max(1, min(groups_count, n))
    update_tournament(t["id"], groups_count=groups_count)

    pids = [p["player_id"] for p in players]
    groups = draw_groups(t["id"], pids, groups_count)
    mids = generate_group_fixtures(t["id"], groups)

    lines = [
        f"🎲 <b>Жеребьёвка завершена!</b>\n"
        f"Турнир <b>{t['name']}</b> [{t_full_label(t)}] стартует!\n"
    ]
    for g, pids_g in sorted(groups.items()):
        names = [get_player_by_id(pid)["username"] for pid in pids_g]
        lines.append(f"<b>Группа {g}:</b> {', '.join(mention(u) for u in names)}")

    lines.append(f"\n📅 Создано матчей: <b>{len(mids)}</b>")
    lines.append("Срок каждого матча: <b>48 часов</b>")
    lines.append("\nРепортуй результат: <code>/report 3:2 @opponent</code>\n"
                 "или просто пришли фото скрина — бот распознает.")

    await send(update, "\n".join(lines))

    for pid in [p["player_id"] for p in get_tournament_players(t["id"])]:
        player = get_player_by_id(pid)
        if player and player.get("telegram_id"):
            try:
                await ctx.bot.send_message(
                    player["telegram_id"],
                    f"🏆 Турнир <b>{t['name']}</b> [{t_full_label(t)}] начался!\n"
                    f"Используй <code>/table</code> чтобы увидеть свою группу.",
                    parse_mode="HTML",
                )
            except Exception:
                pass


# ─────────────────────────────────────────────────────────────────────────────
# /set_group — manual group assignment (admin only, before /start_tournament)
# /clear_groups — wipe manual assignments back to lobby
# ─────────────────────────────────────────────────────────────────────────────

def _resolve_target_tournament_for_group_admin(
    update: Update, ctx: ContextTypes.DEFAULT_TYPE,
    args: list[str],
) -> tuple[dict | None, list[str], str | None]:
    """Resolve which tournament a /set_group / /clear_groups call applies to.

    The first positional arg may be a tournament ID; if so it's stripped
    off and the rest is returned. Otherwise we use the chat-bound active
    tournament if available, else the most-recently created active
    tournament. Returns ``(t, remaining_args, error_msg)`` — when ``t``
    is ``None`` the caller should reply with ``error_msg``.
    """
    remaining = list(args)
    if remaining and remaining[0].isdigit():
        try:
            tid = int(remaining[0])
        except ValueError:
            return None, remaining, "❌ ID должен быть числом."
        t = get_tournament(tid)
        if not t:
            return None, remaining, f"❌ Турнир {tid} не найден."
        return t, remaining[1:], None

    chat = update.effective_chat
    if chat is not None:
        bound = get_tournament_by_chat(chat.id)
        if bound:
            return bound, remaining, None

    t = get_active_tournament()
    if t is None:
        return None, remaining, (
            "❌ Нет активного турнира. Укажи ID: "
            "<code>/set_group &lt;ID&gt; A @user1, @user2</code>"
        )
    return t, remaining, None


async def cmd_set_group(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """``/set_group [ID] <letter|auto> @user1, @user2, ...`` — admin-only.

    Move the listed players into the given group letter (single Cyrillic
    or Latin letter, case-insensitive — stored uppercase). Players must
    already be in the tournament (added via ``/add_player``); we don't
    add them implicitly. ``/set_group [ID] auto`` resets every player's
    group back to "?" so the next ``/start_tournament`` does an
    automatic random draw.

    Refuses if the group-stage fixtures have already been generated —
    use ``/redraw_groups`` to reshape after the draw.
    """
    user = update.effective_user
    if not user or not is_admin(user.id):
        await send(update, "❌ Только админ.")
        return

    if not ctx.args:
        await send(
            update,
            "Использование: <code>/set_group [ID] &lt;A|B|...&gt; "
            "@user1, @user2</code>\n"
            "Сброс ручной раздачи: <code>/set_group [ID] auto</code>",
        )
        return

    t, rest, err = _resolve_target_tournament_for_group_admin(update, ctx, ctx.args)
    if t is None:
        await send(update, err or "❌ Нет активного турнира.")
        return

    if not _can_manage_tournament(user.id, t):
        await send(update, "❌ Только создатель турнира или админ.")
        return

    if int(t.get("bracket_only") or 0):
        await send(
            update,
            "❌ Турнир в формате <b>бракет</b> — групп нет, сидование "
            "идёт автоматически по ELO.",
        )
        return

    existing_group_matches = [
        m for m in db.get_tournament_matches(t["id"])
        if m.get("stage") == "group"
    ]
    if existing_group_matches:
        await send(
            update,
            "❌ Жеребьёвка уже проведена и матчи созданы. "
            f"Сначала <code>/redraw_groups {t['id']}</code>, "
            "затем заново раскинь группы.",
        )
        return

    if not rest:
        await send(update, "❌ Укажи группу: <code>A</code>, <code>B</code>, …")
        return

    # ── Reset path: /set_group auto / случайная / random
    if rest[0].lower() in ("auto", "случайная", "random"):
        for p in get_tournament_players(t["id"]):
            add_player_to_tournament(t["id"], p["player_id"], "?")
        await send(
            update,
            f"🔄 Ручная раздача для <b>{html.escape(t['name'])}</b> сброшена. "
            f"<code>/start_tournament</code> теперь раскинет случайно.",
        )
        return

    raw_letter = rest[0].strip().upper()
    if len(raw_letter) != 1 or not raw_letter.isalpha():
        await send(
            update,
            "❌ Группа — одна буква (A, B, …). Получил: "
            f"<code>{html.escape(rest[0])}</code>",
        )
        return
    # Map to standard letters used by GROUP_LETTERS = "ABCDEFGH".
    if raw_letter not in GROUP_LETTERS:
        await send(
            update,
            f"❌ Поддерживаются только группы A–{GROUP_LETTERS[-1]}. "
            f"Получил: <code>{html.escape(raw_letter)}</code>",
        )
        return
    letter = raw_letter

    usernames = _parse_add_player_usernames(rest[1:])
    if not usernames:
        await send(
            update,
            "❌ Укажи хотя бы одного игрока: "
            "<code>/set_group A @user1, @user2</code>",
        )
        return

    roster = {p["player_id"]: p for p in get_tournament_players(t["id"])}
    moved: list[str] = []
    not_in_tournament: list[str] = []
    for uname in usernames:
        p = get_player(uname)
        if p is None or p["id"] not in roster:
            not_in_tournament.append(uname)
            continue
        add_player_to_tournament(t["id"], p["id"], letter)
        moved.append(uname)

    lines: list[str] = []
    if moved:
        lines.append(
            f"✅ В <b>группу {letter}</b> добавлены: "
            + ", ".join(mention(u) for u in moved)
        )
    if not_in_tournament:
        lines.append(
            "⚠️ Игроки не в турнире (сначала <code>/add_player</code>): "
            + ", ".join(mention(u) for u in not_in_tournament)
        )

    # Show updated overall layout so admin can sanity-check.
    players_now = get_tournament_players(t["id"])
    layout: dict[str, list[str]] = {}
    lobby: list[str] = []
    for p in players_now:
        g = (p.get("group_name") or "").strip()
        uname = (p.get("username") or "").lower()
        if g and g != "?":
            layout.setdefault(g, []).append(uname)
        else:
            lobby.append(uname)
    if layout:
        lines.append("\n<b>Текущая раздача:</b>")
        for g in sorted(layout.keys()):
            lines.append(
                f"  <b>{g}:</b> "
                + ", ".join(mention(u) for u in layout[g])
            )
    if lobby:
        lines.append(
            "\n🏃 В лобби (без группы): "
            + ", ".join(mention(u) for u in lobby)
        )
        lines.append(
            f"\nКогда раскинул всех — <code>/start_tournament</code>."
        )

    await send(update, "\n".join(lines))


async def cmd_clear_groups(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """``/clear_groups [ID]`` — wipe group assignments AND group-stage matches.

    Resets every player's ``group_name`` back to "?" (lobby) and deletes
    all group-stage matches + resets group stats. This is a full reset
    that works even after fixtures have been generated — unlike
    ``/redraw_groups`` which refuses when matches are confirmed.

    Use this when you want to start the group stage from scratch without
    re-drawing automatically.
    """
    user = update.effective_user
    if not user or not is_admin(user.id):
        await send(update, "❌ Только админ.")
        return

    t, _rest, err = _resolve_target_tournament_for_group_admin(
        update, ctx, ctx.args,
    )
    if t is None:
        await send(update, err or "❌ Нет активного турнира.")
        return

    if not _can_manage_tournament(user.id, t):
        await send(update, "❌ Только создатель турнира или админ.")
        return

    tid = t["id"]

    # Delete all group-stage matches and reset group stats
    conn = db.get_conn()
    deleted = conn.execute(
        "DELETE FROM matches WHERE tournament_id=? AND stage='group'",
        (tid,),
    ).rowcount
    conn.execute(
        """UPDATE tournament_players
              SET group_name=NULL,
                  group_points=0,
                  group_gf=0, group_ga=0,
                  group_wins=0, group_draws=0, group_losses=0
            WHERE tournament_id=?""",
        (tid,),
    )
    conn.commit()
    conn.close()

    # Reset all players to lobby
    for p in get_tournament_players(tid):
        add_player_to_tournament(tid, p["player_id"], "?")

    extra = ""
    if deleted:
        extra = f"\n🗑 Удалено матчей группового этапа: <b>{deleted}</b>"

    await send(
        update,
        f"🧹 Группы для <b>{html.escape(t['name'])}</b> полностью сброшены. "
        f"Игроки в лобби.{extra}\n"
        f"Раскидай заново через "
        f"<code>/set_group {tid} A @user1</code> "
        f"или запусти автоматическую жеребьёвку: "
        f"<code>/start_tournament</code>.",
    )


# ─────────────────────────────────────────────────────────────────────────────
# /redraw_groups — explicit re-draw (admin only)
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_redraw_groups(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    /redraw_groups <tournament_id>

    Wipes all current group-stage matches + group assignments and re-draws.
    Admin-only. Refuses if any group-stage match is already confirmed —
    you should /finish_tournament instead in that case.
    """
    if not is_admin(update.effective_user.id):
        await send(update, "❌ Только админ.")
        return

    if not ctx.args:
        await send(update, "Использование: <code>/redraw_groups &lt;ID&gt;</code>")
        return
    try:
        tid = int(ctx.args[0])
    except ValueError:
        await send(update, "❌ ID должен быть числом.")
        return

    t = get_tournament(tid)
    if not t:
        await send(update, f"❌ Турнир {tid} не найден.")
        return
    if not _can_manage_tournament(update.effective_user.id, t):
        await send(update, "❌ Только создатель турнира или админ.")
        return

    matches = db.get_tournament_matches(tid)
    confirmed_group = [
        m for m in matches if m.get("stage") == "group" and m.get("status") == "confirmed"
    ]
    if confirmed_group:
        await send(
            update,
            f"❌ Нельзя перетряхнуть: уже {len(confirmed_group)} матчей группового "
            f"этапа подтверждены. Если хочешь начать заново — заверши турнир "
            f"и создай новый.",
        )
        return

    # Wipe groups + their fixtures, keep tournament_players rows but reset
    # their group_name + group stats.
    conn = db.get_conn()
    conn.execute(
        "DELETE FROM matches WHERE tournament_id=? AND stage='group'",
        (tid,),
    )
    conn.execute(
        """UPDATE tournament_players
              SET group_name=NULL,
                  group_points=0,
                  group_gf=0, group_ga=0,
                  group_wins=0, group_draws=0, group_losses=0
            WHERE tournament_id=?""",
        (tid,),
    )
    conn.commit()
    conn.close()

    players = get_tournament_players(tid)
    if len(players) < 2:
        await send(update, "❌ Нужно минимум 2 игрока.")
        return

    n = len(players)
    pref_groups = int(t.get("groups_count") or 0)
    pref_size   = int(t.get("target_group_size") or 0)
    if pref_groups > 0:
        groups_count = max(1, min(_GROUPS_COUNT_MAX, pref_groups))
    elif pref_size > 0:
        groups_count = max(1, -(-n // pref_size))
    else:
        groups_count = max(2, min(n // 3, 4))
    groups_count = max(1, min(groups_count, n))
    update_tournament(tid, groups_count=groups_count, stage="groups")
    pids = [p["player_id"] for p in players]
    groups = draw_groups(tid, pids, groups_count)
    mids = generate_group_fixtures(tid, groups)

    lines = [
        f"🔄 <b>Перетряхнули жеребьёвку</b> для <b>{html.escape(t['name'])}</b>!\n"
    ]
    for g, pids_g in sorted(groups.items()):
        names = [get_player_by_id(pid)["username"] for pid in pids_g if get_player_by_id(pid)]
        lines.append(f"<b>Группа {g}:</b> {', '.join(mention(u) for u in names)}")
    lines.append(f"\n📅 Создано матчей: <b>{len(mids)}</b>")
    await send(update, "\n".join(lines))




# ─────────────────────────────────────────────────────────────────────────────
# /bind_tournament  /unbind_tournament  — chat ↔ tournament binding
# ─────────────────────────────────────────────────────────────────────────────

def _can_bind_tournament(user_id: int, t: dict) -> bool:
    if is_admin(user_id):
        return True
    creator_id = t.get("created_by")
    if creator_id is None:
        return False
    p = get_player_by_id(creator_id)
    return bool(p and p.get("telegram_id") == user_id)


async def cmd_bind_tournament(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    Bind the current chat (group/channel) to a tournament so screenshots
    posted here are auto-routed to it. Usage: ``/bind_tournament <ID>``.
    Only the tournament's creator or a bot admin can bind.
    """
    chat = update.effective_chat
    if chat is None:
        return
    if chat.type == "private":
        await send(
            update,
            "❌ Эту команду нужно запускать в группе или канале, к которой "
            "ты хочешь привязать турнир.",
        )
        return

    if not ctx.args:
        # Show what's currently bound, if anything.
        bound = get_tournament_by_chat(chat.id)
        if bound:
            await send(
                update,
                f"🔗 Этот чат сейчас привязан к турниру <b>{bound['name']}</b> "
                f"(ID {bound['id']}, {t_full_label(bound)}).\n"
                f"Чтобы перепривязать: <code>/bind_tournament &lt;другой ID&gt;</code>\n"
                f"Чтобы отвязать: /unbind_tournament",
            )
            return
        await send(
            update,
            "Использование: <code>/bind_tournament &lt;ID&gt;</code>\n"
            "Скрины, отправленные в этот чат, будут засчитываться в турнир по ID.",
        )
        return

    try:
        tid = int(ctx.args[0])
    except ValueError:
        await send(update, "❌ Укажи числовой ID турнира.")
        return

    t = get_tournament(tid)
    if not t:
        await send(update, f"❌ Турнир с ID {tid} не найден.")
        return
    if not _can_bind_tournament(update.effective_user.id, t):
        await send(update, "❌ Привязать может только создатель турнира или админ бота.")
        return

    set_tournament_chat(tid, chat.id)
    chat_label = f"@{chat.username}" if chat.username else f"id {chat.id}"
    await send(
        update,
        f"🔗 Чат <b>{chat_label}</b> привязан к турниру <b>{t['name']}</b> "
        f"(ID {tid}, {t_full_label(t)}).\n"
        f"Скрины, отправленные сюда, будут автоматически засчитаны в этот турнир.",
    )


async def cmd_unbind_tournament(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Unbind the current chat from any tournament."""
    chat = update.effective_chat
    if chat is None:
        return
    if chat.type == "private":
        await send(update, "❌ Запускать только в группе/канале.")
        return
    bound = get_tournament_by_chat(chat.id)
    if not bound:
        await send(update, "Этот чат не привязан ни к одному турниру.")
        return
    if not _can_bind_tournament(update.effective_user.id, bound):
        await send(update, "❌ Отвязать может только создатель турнира или админ.")
        return
    unset_tournament_chat(bound["id"])
    await send(
        update,
        f"🔓 Чат отвязан от турнира <b>{bound['name']}</b> (ID {bound['id']}).",
    )




# ─────────────────────────────────────────────────────────────────────────────
# /table  /playoff
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_table_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """``/table_text`` / ``/standings_text`` — shortcut for ``/table text``.

    Renders the standings as plain text (no PNG). Useful in chats where
    images are unwanted (slow networks, accessibility, log archives).
    """
    saved = list(ctx.args or [])
    try:
        ctx.args = ["text"] + saved  # type: ignore[assignment]
        await cmd_table(update, ctx)
    finally:
        ctx.args = saved  # type: ignore[assignment]


async def cmd_playoff_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """``/playoff_text`` / ``/bracket_text`` — shortcut for ``/playoff text``.

    Renders the bracket as plain text instead of the rendered PNG.
    """
    saved = list(ctx.args or [])
    try:
        ctx.args = ["text"] + saved  # type: ignore[assignment]
        await cmd_playoff(update, ctx)
    finally:
        ctx.args = saved  # type: ignore[assignment]


async def cmd_table(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """``/table [tournament_id|вса|ри] [all|split|<group>]`` — турнирная таблица.

    Без аргументов — кнопочный селектор турниров. С первым аргументом
    выбираем турнир (id / тип ``вса`` / ``ри``). Опциональный второй
    аргумент управляет видом картинки:

    * ``all`` — одна общая таблица со всеми группами на одном PNG.
    * ``split`` (или ``groups``) — каждая группа отдельным сообщением.
    * ``A`` / ``B`` / ... — конкретная группа отдельным фото.

    Без второго аргумента: турнир с одной группой рендерится как одна
    картинка; с двумя и более группами бот сначала спросит inline-кнопками,
    как именно показать таблицу.
    """
    if ctx.args:
        first = ctx.args[0]
        # Если первый аргумент — view-токен (для случая «активный турнир один,
        # хочу сразу выбрать вид»), резолвим активный турнир без type_hint.
        if _is_table_view_token(first):
            t, err = _resolve_tournament_from_args(update, ctx, type_hint=None)
            if t is None:
                await send(update, err or "❌ Нет активного турнира.")
                return
            await _render_table_for(update, ctx, t, view=first.lower())
            return
        t_type = parse_tournament_type_arg(first)
        t, err = _resolve_tournament_from_args(update, ctx, type_hint=t_type)
        if t is None:
            await send(update, err or "❌ Нет активного турнира.")
            return
        view = ctx.args[1].lower() if len(ctx.args) > 1 else "auto"
        await _render_table_for(update, ctx, t, view=view)
        return

    await _send_tournament_picker(update, ctx, kind="tbl")


_TABLE_VIEW_KEYWORDS = {
    "all", "одна", "вместе",
    "split", "groups", "разделить", "раздельно",
    "text", "текст", "txt",
}


def _is_table_view_token(tok: str) -> bool:
    """True iff ``tok`` looks like a /table view selector ('all', 'split',
    'groups', or a single-group label like 'A'/'B'/'C').

    Tournament-type tokens (``вса``/``ри``/``vsa``/``ri``) are explicitly
    NOT view tokens — they must keep going through
    ``parse_tournament_type_arg`` so the active-tournament resolver picks
    the right pool.
    """
    s = (tok or "").strip().lower()
    if not s:
        return False
    if parse_tournament_type_arg(s):
        return False
    if s in _TABLE_VIEW_KEYWORDS:
        return True
    if s.lstrip("-").isdigit():
        # Numeric tokens are tournament IDs, not view selectors.
        return False
    # Group label heuristic: a single alpha character (A/B/C/А/Б/В) is
    # almost certainly a group selector. Anything longer that wasn't a
    # type token already → fall through as a tournament-search arg.
    return len(s) == 1 and s.isalpha()


def _build_tournament_picker_kb(
    kind: str, *, include_finished: bool
) -> tuple[InlineKeyboardMarkup, bool, bool]:
    """Build the inline keyboard for the ``/table`` / ``/playoff`` picker.

    Returns ``(markup, has_active, has_finished)``. When
    ``include_finished`` is False, only the active tournaments are shown
    plus a "🏁 Завершённые" expand-button (if any finished tournaments
    exist). When True, the finished tournaments are listed below a
    divider, with a "↩️ Скрыть завершённые" collapse-button.
    """
    actives = get_active_tournaments() or []
    finished_recent = _recent_finished_tournaments(limit=5)
    rows: list[list[InlineKeyboardButton]] = []
    for tt in actives:
        rows.append([InlineKeyboardButton(
            f"{tt['name']} [{t_full_label(tt)}] · ID {tt['id']}",
            callback_data=f"{kind}_pick:{tt['id']}",
        )])
    if finished_recent and include_finished:
        rows.append([InlineKeyboardButton(
            "──── Завершённые ────", callback_data=f"{kind}_noop",
        )])
        for tt in finished_recent:
            rows.append([InlineKeyboardButton(
                f"🏁 {tt['name']} [{t_full_label(tt)}] · ID {tt['id']}",
                callback_data=f"{kind}_pick:{tt['id']}",
            )])
        rows.append([InlineKeyboardButton(
            "↩️ Скрыть завершённые",
            callback_data=f"{kind}_hide_finished",
        )])
    elif finished_recent:
        rows.append([InlineKeyboardButton(
            f"🏁 Завершённые ({len(finished_recent)})",
            callback_data=f"{kind}_show_finished",
        )])
    rows.append([InlineKeyboardButton("❌ Отмена", callback_data=f"{kind}_cancel")])
    return InlineKeyboardMarkup(rows), bool(actives), bool(finished_recent)


async def _send_tournament_picker(
    update: Update, ctx: ContextTypes.DEFAULT_TYPE, *, kind: str
):
    """Render the ``/table`` / ``/playoff`` tournament picker.

    ``kind`` is either ``"tbl"`` or ``"po"`` — that tag is what wires the
    inline-button callbacks to the right handler.

    Завершённые турниры по умолчанию скрыты — над списком активных
    появляется кнопка «🏁 Завершённые», по нажатию список расширяется.
    """
    markup, has_active, has_finished = _build_tournament_picker_kb(
        kind, include_finished=False,
    )
    if not has_active and not has_finished:
        await send(update, "❌ Нет ни одного турнира.")
        return

    title = "Выбери турнир для таблицы:" if kind == "tbl" else "Выбери турнир для плей-офф:"
    msg = update.effective_message
    if msg is None and update.callback_query:
        msg = update.callback_query.message
    if msg:
        await msg.reply_text(title, reply_markup=markup)
    else:
        await send(update, title)


def _recent_finished_tournaments(limit: int = 5) -> list[dict]:
    conn = db.get_conn()
    rows = conn.execute(
        "SELECT * FROM tournaments WHERE stage = 'finished' "
        "ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


async def _render_table_for(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
    t: dict,
    *,
    view: str = "auto",
):
    """Render the standings for ``t`` according to ``view``.

    ``view`` accepts:
      * ``"auto"`` — default. One group → render directly. Two or more
        groups → show the inline view-picker keyboard so the user
        chooses (all-in-one / per-group / single group).
      * ``"all"`` (aliases: ``"одна"``, ``"вместе"``) — single combined
        PNG of every group.
      * ``"split"`` (aliases: ``"groups"``, ``"раздельно"``,
        ``"разделить"``) — one Telegram photo per group, with
        ``(N/M)`` captions.
      * ``"<group>"`` — render only that group's PNG (case-insensitive
        match against the standings keys).
    """
    caption = (
        f"🏆 <b>{html.escape(t['name'])}</b> [{t_full_label(t)}]\n"
        f"📊 Турнирная таблица"
    )
    # Append footer for chat context (table is typically shown in group chat)
    from handlers.common import get_random_footer, FOOTER_CTX_TABLE
    _tbl_footer = get_random_footer(t, FOOTER_CTX_TABLE)
    if _tbl_footer:
        caption += _tbl_footer

    msg = update.effective_message
    chat_id = msg.chat.id if msg else None
    if msg is None and chat_id is None:
        return

    v = (view or "auto").strip().lower()
    groups = list_standings_groups(t["id"])

    # ── Auto: choose the view ourselves, or ask the user. ──────────────────
    if v == "auto":
        if len(groups) <= 1:
            v = "all"  # single-group tournament → just send one PNG
        else:
            await _send_table_view_picker(update, ctx, t, groups, caption)
            return

    # Normalize aliases.
    if v in ("одна", "вместе"):
        v = "all"
    elif v in ("groups", "раздельно", "разделить"):
        v = "split"
    elif v in ("текст", "txt"):
        v = "text"

    # ── text: pure-text standings (no PNG) ─────────────────────────────────
    if v == "text":
        text = format_standings_message(t["id"])
        await send(update, caption + "\n\n" + text)
        return

    # Helper for the per-photo send so we can keep the error handling
    # consistent across branches.
    async def _send_photos(pngs: list[bytes], *, prefix: str):
        n = len(pngs)
        try:
            for idx, png in enumerate(pngs):
                bio = io.BytesIO(png)
                bio.name = f"{prefix}_{idx + 1}of{n}.png"
                cap = caption if n == 1 else f"{caption}\n<i>({idx + 1}/{n})</i>"
                if msg is not None:
                    await msg.reply_photo(
                        photo=bio, caption=cap, parse_mode="HTML",
                        write_timeout=180,
                    )
                else:
                    await ctx.bot.send_photo(
                        chat_id, photo=bio, caption=cap, parse_mode="HTML",
                        write_timeout=180,
                    )
        except TelegramError as e:
            log.warning(
                "reply_photo failed for tid=%s view=%s: %s — falling back to text",
                t["id"], v, e,
            )
            text = format_standings_message(t["id"])
            await send(update, caption + "\n\n" + text)

    # ── all: single combined PNG ───────────────────────────────────────────
    if v == "all":
        try:
            png = await asyncio.to_thread(render_standings_png, t["id"])
        except Exception as e:
            log.exception("render_standings_png failed for tid=%s: %s", t["id"], e)
            text = format_standings_message(t["id"])
            await send(update, caption + "\n\n" + text)
            return
        await _send_photos([png], prefix=f"standings_{t['id']}_all")
        return

    # ── split: one photo per group ─────────────────────────────────────────
    if v == "split":
        try:
            pngs = await asyncio.to_thread(render_standings_pngs, t["id"])
        except Exception as e:
            log.exception("render_standings_pngs failed for tid=%s: %s", t["id"], e)
            text = format_standings_message(t["id"])
            await send(update, caption + "\n\n" + text)
            return
        await _send_photos(pngs, prefix=f"standings_{t['id']}")
        return

    # ── single-group: render only that group ───────────────────────────────
    target_group = view.strip()  # original casing for the error message
    try:
        png = await asyncio.to_thread(
            render_standings_png_for_group, t["id"], target_group,
        )
    except Exception as e:
        log.exception(
            "render_standings_png_for_group failed for tid=%s group=%s: %s",
            t["id"], target_group, e,
        )
        png = None
    if png is None:
        await send(
            update,
            f"❌ В турнире <b>{html.escape(t['name'])}</b> нет группы "
            f"<b>{html.escape(target_group)}</b>. "
            f"Доступные: {', '.join(html.escape(g) for g in groups) or '—'}.",
        )
        return
    group_caption = f"{caption}\n<b>Группа {html.escape(target_group)}</b>"
    try:
        bio = io.BytesIO(png)
        bio.name = f"standings_{t['id']}_group_{target_group}.png"
        if msg is not None:
            await msg.reply_photo(
                photo=bio, caption=group_caption, parse_mode="HTML",
            )
        else:
            await ctx.bot.send_photo(
                chat_id, photo=bio, caption=group_caption, parse_mode="HTML",
            )
    except TelegramError as e:
        log.warning(
            "reply_photo failed for tid=%s group=%s: %s — falling back to text",
            t["id"], target_group, e,
        )
        text = format_standings_message(t["id"])
        await send(update, group_caption + "\n\n" + text)


async def _send_table_view_picker(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
    t: dict,
    groups: list[str],
    caption: str,
):
    """Show inline buttons to pick how the standings table is rendered.

    Triggered from ``/table`` (and from the tournament picker callback)
    only when the tournament has multiple groups, where the choice
    actually changes what the user sees.
    """
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                "📦 Все вместе",
                callback_data=f"tbl_view:{t['id']}:all",
            ),
            InlineKeyboardButton(
                "🗂 Каждая группа отдельно",
                callback_data=f"tbl_view:{t['id']}:split",
            ),
        ],
    ]
    # One button per group — Telegram inline keyboards can wrap, but to
    # keep them compact we emit two buttons per row.
    pair: list[InlineKeyboardButton] = []
    for g in groups:
        pair.append(
            InlineKeyboardButton(
                f"🔹 Группа {g}",
                callback_data=f"tbl_view:{t['id']}:g:{g}",
            )
        )
        if len(pair) == 2:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)
    rows.append([InlineKeyboardButton(
        "❌ Отмена", callback_data="tbl_cancel",
    )])

    text = (
        f"{caption}\n\n"
        f"В этом турнире <b>{len(groups)}</b> групп. "
        "Как показать таблицу?"
    )
    msg = update.effective_message
    if msg is not None:
        await msg.reply_text(
            text, parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(rows),
        )
    else:
        await ctx.bot.send_message(
            update.effective_chat.id if update.effective_chat else None,
            text, parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(rows),
        )


async def cb_table_view(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Callback for the ``📦 / 🗂 / 🔹 Группа …`` view-picker buttons.

    ``callback_data`` shape: ``tbl_view:<tid>:all`` |
    ``tbl_view:<tid>:split`` | ``tbl_view:<tid>:g:<group>``.
    """
    q = update.callback_query
    if not q:
        return
    await q.answer()
    data = q.data or ""
    parts = data.split(":", 3)
    if len(parts) < 3 or parts[0] != "tbl_view":
        return
    try:
        tid = int(parts[1])
    except ValueError:
        return
    mode = parts[2]
    group: str | None = None
    if mode == "g":
        if len(parts) < 4 or not parts[3]:
            return
        group = parts[3]

    t = get_tournament(tid)
    if not t:
        try:
            await q.edit_message_text(f"❌ Турнир ID {tid} не найден.")
        except Exception:
            pass
        return

    # Replace the picker message with a one-liner so the chat doesn't
    # accumulate stale keyboards.
    try:
        if mode == "all":
            label = "📦 Все вместе"
        elif mode == "split":
            label = "🗂 Каждая группа отдельно"
        else:
            label = f"🔹 Группа {group}"
        await q.edit_message_text(
            f"🏆 <b>{html.escape(t['name'])}</b> [{t_full_label(t)}] — {label}",
            parse_mode="HTML",
        )
    except Exception:
        pass

    if mode == "all":
        await _render_table_for(update, ctx, t, view="all")
    elif mode == "split":
        await _render_table_for(update, ctx, t, view="split")
    else:
        await _render_table_for(update, ctx, t, view=group or "")


async def cb_table_pick(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q:
        return
    await q.answer()
    data = q.data or ""
    if data in ("tbl_cancel", "tbl_noop"):
        if data == "tbl_cancel":
            try:
                await q.edit_message_text("Отменено.")
            except Exception:
                pass
        return
    if data in ("tbl_show_finished", "tbl_hide_finished"):
        markup, _, _ = _build_tournament_picker_kb(
            "tbl", include_finished=(data == "tbl_show_finished"),
        )
        try:
            await q.edit_message_reply_markup(reply_markup=markup)
        except Exception:
            pass
        return
    try:
        tid = int(data.split(":", 1)[1])
    except (IndexError, ValueError):
        return
    t = get_tournament(tid)
    if not t:
        try:
            await q.edit_message_text(f"❌ Турнир ID {tid} не найден.")
        except Exception:
            pass
        return
    # Edit out the keyboard, then send the photo as a new message.
    try:
        await q.edit_message_text(
            f"🏆 <b>{html.escape(t['name'])}</b> [{t_full_label(t)}]",
            parse_mode="HTML",
        )
    except Exception:
        pass
    await _render_table_for(update, ctx, t)


_PLAYOFF_VIEW_TEXT_TOKENS = {"text", "текст", "txt", "т"}


def _is_playoff_view_token(tok: str) -> bool:
    """True iff ``tok`` looks like a /playoff view selector (only 'text'
    family for now). Tournament-type tokens like ``вса`` / ``ri`` are
    explicitly NOT view tokens.
    """
    s = (tok or "").strip().lower()
    if not s:
        return False
    if parse_tournament_type_arg(s):
        return False
    return s in _PLAYOFF_VIEW_TEXT_TOKENS


async def cmd_playoff(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """``/playoff [tournament_id|вса|ри] [text]`` — bracket as PNG.

    Без аргумента — кнопочный селектор (тот же UX, что и у ``/table``).
    Опциональный токен ``text`` (а также ``текст`` / ``txt`` / ``т``)
    переключает вывод на чисто-текстовый вариант, удобный для копирования
    и плохого мобильного интернета.
    """
    if ctx.args:
        # First-arg as a view-token shortcut: "/playoff text" → активный
        # турнир + текстовый вывод.
        first = ctx.args[0]
        if _is_playoff_view_token(first):
            t, err = _resolve_tournament_from_args(update, ctx, type_hint=None)
            if t is None:
                await send(update, err or "❌ Нет активного турнира.")
                return
            await _render_playoff_for(update, ctx, t, view="text")
            return
        t_type = parse_tournament_type_arg(first)
        t, err = _resolve_tournament_from_args(update, ctx, type_hint=t_type)
        if t is None:
            await send(update, err or "❌ Нет активного турнира.")
            return
        view = "auto"
        if len(ctx.args) > 1 and _is_playoff_view_token(ctx.args[1]):
            view = "text"
        await _render_playoff_for(update, ctx, t, view=view)
        return

    await _send_tournament_picker(update, ctx, kind="po")


async def _render_playoff_for(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
    t: dict,
    *,
    view: str = "auto",
):
    if t["stage"] not in ("playoff", "finished", "groups_done"):
        await send(update, "⏳ Плей-офф ещё не начался. Дождись окончания группового этапа.")
        return

    caption = (
        f"🏆 <b>{html.escape(t['name'])}</b> [{t_full_label(t)}]\n"
        f"⚔️ Сетка плей-офф"
    )
    # Append footer for chat context
    from handlers.common import get_random_footer, FOOTER_CTX_PLAYOFF
    _po_footer = get_random_footer(t, FOOTER_CTX_PLAYOFF)
    if _po_footer:
        caption += _po_footer
    msg = update.effective_message
    chat_id = msg.chat.id if msg else None

    # ── Pure-text view: skip the PNG renderer entirely. ───────────────────
    if (view or "auto").strip().lower() == "text":
        text = format_playoff_bracket(t["id"])
        await send(update, caption + "\n\n" + text)
        return

    # Manual-advance button: shown only to people who can manage the
    # tournament, and only if it makes sense (not finished, current stage
    # has all confirmed legs, next stage hasn't been generated yet).
    reply_markup = None
    user = update.effective_user
    if (
        user
        and t.get("stage") != "finished"
        and _can_manage_tournament(user.id, t)
        and _can_advance_now(t["id"])
    ):
        reply_markup = InlineKeyboardMarkup([[
            InlineKeyboardButton(
                "🚀 Перейти к следующей стадии",
                callback_data=f"adv_now:{t['id']}",
            ),
        ]])

    try:
        from playoff_image import render_playoff_pngs
        pngs = await asyncio.to_thread(render_playoff_pngs, t["id"])
    except Exception as e:
        log.exception("render_playoff_pngs failed for tid=%s: %s", t["id"], e)
        text = format_playoff_bracket(t["id"])
        await send(update, caption + "\n\n" + text, reply_markup=reply_markup)
        return

    # For big brackets ``render_playoff_pngs`` returns the bracket cut in
    # halves so each piece fits Telegram's photo size limit. Send the
    # halves sequentially with "(1/N)" suffixes; the manual-advance
    # button is attached to the LAST image so it lands at the bottom.
    n = len(pngs)
    try:
        for idx, png in enumerate(pngs):
            bio = io.BytesIO(png)
            bio.name = f"playoff_{t['id']}_{idx + 1}of{n}.png"
            cap = caption if n == 1 else f"{caption}\n<i>({idx + 1}/{n})</i>"
            this_markup = reply_markup if (idx == n - 1) else None
            if msg is not None:
                await msg.reply_photo(
                    photo=bio, caption=cap, parse_mode="HTML",
                    reply_markup=this_markup,
                    write_timeout=180,
                )
            else:
                await ctx.bot.send_photo(
                    chat_id, photo=bio, caption=cap, parse_mode="HTML",
                    reply_markup=this_markup,
                    write_timeout=180,
                )
    except TelegramError as e:
        log.warning("playoff reply_photo failed for tid=%s: %s — fallback to text", t["id"], e)
        text = format_playoff_bracket(t["id"])
        await send(update, caption + "\n\n" + text, reply_markup=reply_markup)


def _can_advance_now(tid: int) -> bool:
    """True iff /advance_playoff would actually move ``tid`` forward.

    Cheap, side-effect-free check: looks at the latest stage with rows
    in the DB and returns True only when **every** confirmed leg is
    locked in for that stage AND the next stage hasn't been generated.
    Used to decide whether to render the manual-advance button.
    """
    from tournament import (
        PLAYOFF_STAGES,
        _dedup_playoff_legs,
        _resolve_pair_winner,
        _pair_key,
        get_stage_config,
    )
    t = get_tournament(tid)
    if not t or t.get("stage") == "finished":
        return False
    for s in PLAYOFF_STAGES:
        ms = _dedup_playoff_legs(get_tournament_matches(tid, stage=s))
        if not ms:
            continue
        stage_cfg = get_stage_config(t, s)
        legs_cfg = stage_cfg["len"]
        adv_mode = stage_cfg["mode"]
        if any(m["status"] != "confirmed" for m in ms):
            return False
        # All legs confirmed at stage `s`. Is there a next stage already?
        idx = PLAYOFF_STAGES.index(s)
        if idx + 1 >= len(PLAYOFF_STAGES):
            # Final completed → finishing the tournament still counts
            # as advancement.
            return t.get("stage") != "finished"
        next_ms = _dedup_playoff_legs(
            get_tournament_matches(tid, stage=PLAYOFF_STAGES[idx + 1])
        )
        if next_ms:
            continue  # already advanced; check the next stage in line
        # Are all pair winners decidable, or do we need an extra match?
        pairs: dict = {}
        for m in ms:
            pairs.setdefault(_pair_key(m), []).append(m)
        for pair_ms in pairs.values():
            ms_sorted = sorted(pair_ms, key=lambda x: x.get("leg") or 1)
            if _resolve_pair_winner(
                ms_sorted, advance_mode=adv_mode, series_len=legs_cfg,
            ) is None:
                if legs_cfg >= 2 and len(ms_sorted) >= legs_cfg:
                    # Aggregate tied — advancing would schedule an extra
                    # match (or another one after a string of consecutive
                    # draws), which is still useful to expose via the
                    # button.
                    return True
                return False
        return True
    return False


async def cb_advance_now(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Inline-button callback: 🚀 Перейти к следующей стадии."""
    q = update.callback_query
    if not q:
        return
    await q.answer()
    data = q.data or ""
    if not data.startswith("adv_now:"):
        return
    try:
        tid = int(data.split(":", 1)[1])
    except ValueError:
        return
    t = get_tournament(tid)
    if not t:
        try:
            await q.edit_message_caption(caption="❌ Турнир не найден.")
        except Exception:
            pass
        return
    if not _can_manage_tournament(update.effective_user.id, t):
        try:
            await q.answer("Только создатель или админ турнира.", show_alert=True)
        except Exception:
            pass
        return
    moved = _maybe_auto_advance(ctx, tid)
    if not moved:
        try:
            await q.answer(
                "Двигать пока нечего — не все матчи стадии подтверждены.",
                show_alert=True,
            )
        except Exception:
            pass
        return
    t2 = get_tournament(tid)
    new_stage = (
        "finished"
        if t2 and t2.get("stage") == "finished"
        else _current_playoff_stage(tid)
    )
    log_tournament_action(
        tid,
        actor_telegram_id=update.effective_user.id,
        actor_username=update.effective_user.username,
        action="advance_playoff",
        details=f"new_stage={new_stage} via=button",
    )
    if new_stage:
        await _announce_stage_advance(ctx, tid, new_stage)
    # Repost the bracket so the user immediately sees the new stage.
    try:
        await q.message.reply_text(
            f"🚀 Стадия продвинута: <b>{html.escape(str(new_stage))}</b>",
            parse_mode="HTML",
        )
    except Exception:
        pass
    try:
        await _render_playoff_for(update, ctx, t2)
    except Exception:
        log.exception("re-render playoff after manual advance failed")


async def cb_playoff_pick(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q:
        return
    await q.answer()
    data = q.data or ""
    if data in ("po_cancel", "po_noop"):
        if data == "po_cancel":
            try:
                await q.edit_message_text("Отменено.")
            except Exception:
                pass
        return
    if data in ("po_show_finished", "po_hide_finished"):
        markup, _, _ = _build_tournament_picker_kb(
            "po", include_finished=(data == "po_show_finished"),
        )
        try:
            await q.edit_message_reply_markup(reply_markup=markup)
        except Exception:
            pass
        return
    try:
        tid = int(data.split(":", 1)[1])
    except (IndexError, ValueError):
        return
    t = get_tournament(tid)
    if not t:
        try:
            await q.edit_message_text(f"❌ Турнир ID {tid} не найден.")
        except Exception:
            pass
        return
    # Strip the keyboard, then send the bracket as a fresh photo message.
    try:
        await q.edit_message_text(
            f"🏆 <b>{html.escape(t['name'])}</b> [{t_full_label(t)}]",
            parse_mode="HTML",
        )
    except Exception:
        pass
    await _render_playoff_for(update, ctx, t)


# ─────────────────────────────────────────────────────────────────────────────
# /close_groups (creator/admin) — lock group stage, no more group matches
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_close_groups(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """``/close_groups [ID|вса|ри]`` — manually lock the group stage.

    After this command no new group-stage matches will be accepted.
    The group table remains viewable via ``/table``.
    """
    t_type = parse_tournament_type_arg(ctx.args[0]) if ctx.args else None
    t, err = _resolve_tournament_from_args(update, ctx, type_hint=t_type)
    if t is None:
        await send(update, err or "❌ Нет активного турнира.")
        return
    if not _can_manage_tournament(update.effective_user.id, t):
        await send(update, "❌ Только создатель или админ.")
        return

    if t.get("playoff_started"):
        await send(
            update,
            f"ℹ️ Групповой этап <b>{html.escape(t['name'])}</b> уже закрыт.",
        )
        return

    update_tournament(t["id"], playoff_started=1)
    log_tournament_action(
        t["id"], update.effective_user.id,
        "close_groups", "Групповой этап закрыт вручную",
    )
    await send(
        update,
        f"🔒 Групповой этап <b>{html.escape(t['name'])}</b> закрыт.\n"
        f"Новые групповые матчи больше не принимаются.\n"
        f"Таблица по-прежнему доступна: <code>/table {t['id']}</code>\n\n"
        f"Чтобы начать плей-офф: <code>/start_playoff {t['id']}</code>",
    )


# ─────────────────────────────────────────────────────────────────────────────
# /start_playoff (creator/admin)
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_start_playoff(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    t_type = parse_tournament_type_arg(ctx.args[0]) if ctx.args else None
    t, err = _resolve_tournament_from_args(update, ctx, type_hint=t_type)
    if t is None:
        await send(update, err or "❌ Нет активного турнира.")
        return
    if not _can_manage_tournament(update.effective_user.id, t):
        await send(update, "❌ Только создатель или админ.")
        return

    # Groups-only tournaments explicitly forbid the playoff — refuse and
    # point the admin at /finish_tournament so the group winner can be
    # crowned without building a bracket.
    if int(t.get("groups_only") or 0):
        await send(
            update,
            f"❌ Турнир <b>{html.escape(t['name'])}</b> создан в формате "
            f"<b>«только группы»</b>. Плей-офф здесь не запускается — "
            f"победителя определи по таблице групп "
            f"(<code>/table {t['id']}</code>) и закрой турнир "
            f"(<code>/finish_tournament {t['id']}</code>).\n\n"
            f"Если нужно всё-таки сыграть плей-офф — поменяй формат в "
            f"⚙️ <i>Настройки турнира → Формат</i> до начала матчей.",
        )
        return

    # Idempotency guard at the command layer: explicit, friendly message
    # if the bracket already exists. ``generate_playoff`` is also
    # idempotent under the hood, but here we want to avoid blasting the
    # chat with a duplicate "Плей-офф начался!" announcement.
    from database import get_real_tournament_matches as _real
    existing_playoff = [
        m for m in _real(t["id"]) if (m.get("stage") or "").lower()
        in ("r512", "r256", "r128", "r64", "r32", "r16",
            "qf", "sf", "final", "third")
    ]
    if existing_playoff:
        await send(
            update,
            f"ℹ️ Плей-офф для <b>{html.escape(t['name'])}</b> уже создан "
            f"({len(existing_playoff)} матч(ей)). Посмотри сетку: "
            f"<code>/playoff {t['id']}</code>.",
        )
        return

    # Honour the per-tournament ``playoff_slots`` setting (default 2).
    # ``generate_playoff`` falls back to ``t.playoff_slots`` when ``None`` is
    # passed, but we pass it explicitly here for clarity.
    slots = max(1, int(t.get("playoff_slots") or 2))
    matches_info = generate_playoff(t["id"], advance_per_group=slots)
    if not matches_info:
        await send(
            update,
            "❌ Не получилось построить сетку плей-офф — нужны минимум 2 группы "
            "с подведёнными итогами. Закончи групповой этап.",
        )
        return
    lines = [f"🏆 <b>Плей-офф начался!</b> [{t_full_label(t)}]\n", "<b>Пары:</b>"]
    seen_pairs: set[tuple[int, int]] = set()
    pretty_info: list[dict] = []
    for mi in matches_info:
        pa = get_player(mi["player1"]) or {}
        pb = get_player(mi["player2"]) or {}
        pid_a = pa.get("id") or 0
        pid_b = pb.get("id") or 0
        key = (min(pid_a, pid_b), max(pid_a, pid_b))
        if key in seen_pairs:
            continue
        seen_pairs.add(key)
        pretty_info.append(mi)
        lines.append(f"⚔️  {mention(mi['player1'])} vs {mention(mi['player2'])}")
    await send(update, "\n".join(lines))
    matches_info = pretty_info

    for mi in matches_info:
        for uname in [mi["player1"], mi["player2"]]:
            p = get_player(uname)
            if p and p.get("telegram_id"):
                try:
                    opp = mi["player2"] if uname == mi["player1"] else mi["player1"]
                    await ctx.bot.send_message(
                        p["telegram_id"],
                        f"⚔️ <b>Твой матч плей-офф!</b>\n\n"
                        f"{mention(uname)} vs {mention(opp)}\n"
                        f"⏳ Срок: 48 часов",
                        parse_mode="HTML",
                    )
                except Exception:
                    pass


# ─────────────────────────────────────────────────────────────────────────────
# /redraw_playoff  — wipe + re-seed an already-drawn bracket
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_redraw_playoff(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    /redraw_playoff <tournament_id>

    Wipes the current bracket and re-seeds it from the current group
    standings using the cross-bracket draw. Useful when the auto-draw
    paired same-group players in the first round.

    Refuses if any real (non-bye) playoff match has been confirmed —
    you can't pull a confirmed result back out of the bracket without
    rewriting history.
    """
    if not is_admin(update.effective_user.id):
        await send(update, "❌ Только админ.")
        return

    t_type = parse_tournament_type_arg(ctx.args[0]) if ctx.args else None
    t, err = _resolve_tournament_from_args(update, ctx, type_hint=t_type)
    if t is None:
        await send(update, err or "❌ Нет активного турнира.")
        return
    if not _can_manage_tournament(update.effective_user.id, t):
        await send(update, "❌ Только создатель турнира или админ.")
        return

    tid = int(t["id"])

    if int(t.get("groups_only") or 0):
        await send(
            update,
            f"❌ Турнир <b>{html.escape(t['name'])}</b> в формате "
            f"<b>«только группы»</b> — плей-офф у него нет.",
        )
        return

    matches = db.get_tournament_matches(tid)
    playoff_stages_set = {s.lower() for s in PLAYOFF_STAGES}
    playoff_stages_set.add("third")
    playoff_matches = [
        m for m in matches
        if (m.get("stage") or "").lower() in playoff_stages_set
    ]
    if not playoff_matches:
        await send(
            update,
            f"ℹ️ Сетка плей-офф у <b>{html.escape(t['name'])}</b> ещё "
            f"не создана — запусти <code>/start_playoff {tid}</code>.",
        )
        return

    # Block redraw once any real match has been confirmed — rewriting
    # history would destroy ELO + leaderboard state. Bye rows (player1
    # == player2, auto-confirmed at 1:0) don't count.
    confirmed_real = [
        m for m in playoff_matches
        if m.get("status") == "confirmed"
        and m.get("player1_id") != m.get("player2_id")
    ]
    if confirmed_real:
        await send(
            update,
            f"❌ Нельзя пересеять: уже <b>{len(confirmed_real)}</b> "
            f"плей-офф матча подтверждены. Если действительно нужно — "
            f"откати их через <code>/edit_match</code> сначала.",
        )
        return

    # Wipe every playoff row (including any byes / unconfirmed legs).
    conn = db.get_conn()
    try:
        ids = [int(m["id"]) for m in playoff_matches]
        conn.execute(
            "DELETE FROM matches WHERE id IN ("
            + ",".join(["?"] * len(ids)) + ")",
            ids,
        )
        conn.commit()
    finally:
        conn.close()

    # Re-run the seeder with the same slots setting as start_playoff.
    slots = max(1, int(t.get("playoff_slots") or 2))
    matches_info = generate_playoff(tid, advance_per_group=slots)
    if not matches_info:
        await send(
            update,
            "❌ Не получилось пересеять сетку — проверь группы.",
        )
        return

    # Pretty-print the new bracket. Collapse byes + leg-2 duplicates
    # so the chat sees one line per pair (matches /start_playoff style).
    lines = [
        f"🔄 <b>Сетка плей-офф пересеяна</b> для "
        f"<b>{html.escape(t['name'])}</b>!\n",
        "<b>Новые пары:</b>",
    ]
    seen_pairs: set[tuple[int, int]] = set()
    bye_lines: list[str] = []
    for mi in matches_info:
        if mi.get("bye"):
            bye_lines.append(f"⏭ {mention(mi['player1'])} (bye)")
            continue
        pa = get_player(mi["player1"]) or {}
        pb = get_player(mi["player2"]) or {}
        pid_a = pa.get("id") or 0
        pid_b = pb.get("id") or 0
        key = (min(pid_a, pid_b), max(pid_a, pid_b))
        if key in seen_pairs:
            continue
        seen_pairs.add(key)
        lines.append(
            f"⚔️  {mention(mi['player1'])} vs {mention(mi['player2'])}"
        )
    lines.extend(bye_lines)
    await send(update, "\n".join(lines))


# ─────────────────────────────────────────────────────────────────────────────
# /ocr_fast / /ocr_full  — quick OCR-mode shortcuts (admin)
# ─────────────────────────────────────────────────────────────────────────────

async def _set_ocr_mode(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
    new_mode: str,
    pretty: str,
) -> None:
    """Shared helper for ``/ocr_fast`` and ``/ocr_full`` commands.

    Validates admin + tournament resolution then flips ``ocr_mode`` on
    the resolved tournament. ``new_mode`` must be one of the allowed
    values (``'ai'``, ``'ai_no_tess'``, ``'score_only'``).
    """
    if not is_admin(update.effective_user.id):
        await send(update, "❌ Только админ.")
        return

    t_type = parse_tournament_type_arg(ctx.args[0]) if ctx.args else None
    t, err = _resolve_tournament_from_args(update, ctx, type_hint=t_type)
    if t is None:
        await send(update, err or "❌ Нет активного турнира.")
        return
    if not _can_manage_tournament(update.effective_user.id, t):
        await send(update, "❌ Только создатель турнира или админ.")
        return

    tid = int(t["id"])
    current = (t.get("ocr_mode") or "ai").lower()
    if current == new_mode:
        await send(
            update,
            f"ℹ️ Режим OCR у <b>{html.escape(t['name'])}</b> уже "
            f"<b>{pretty}</b> — менять нечего.",
        )
        return
    update_tournament(tid, ocr_mode=new_mode)
    await send(
        update,
        f"✅ Режим OCR у <b>{html.escape(t['name'])}</b> переключён на "
        f"<b>{pretty}</b>.\n\n"
        f"<i>Меняется кнопкой «🤖 OCR» / «⚡ Тесеракт» в "
        f"<code>/tournament_settings {tid}</code>.</i>",
    )


async def cmd_ocr_fast(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    /ocr_fast [tid]

    Включить быстрый тесеракт «только счёт» для турнира. AI Vision
    пропускается, читается только полоса счёта (~3s/фото). Ник
    соперника берётся из caption-а ``@user`` к фото.
    """
    await _set_ocr_mode(
        update, ctx,
        new_mode="score_only",
        pretty="⚡ тесеракт (только счёт)",
    )


async def cmd_ocr_full(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    /ocr_full [tid]

    Включить полный режим OCR: AI Vision + тесеракт-фолбек. Самый
    точный, но самый медленный (~10-30s/фото). Команды/лига/голы
    тоже читаются.
    """
    await _set_ocr_mode(
        update, ctx,
        new_mode="ai",
        pretty="🐢 ИИ + тесеракт (всё)",
    )


# ─────────────────────────────────────────────────────────────────────────────
# /finish_tournament  — close out a tournament (creator/admin)
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_finish_tournament(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    Mark a tournament as finished. Accepts an explicit ID
    (``/finish_tournament 27``) or — if omitted — uses the active tournament
    bound to this chat or the single active tournament. Confirmation prompt
    asks before flipping the stage.
    """
    tid: int | None = None
    if ctx.args:
        try:
            tid = int(ctx.args[0])
        except ValueError:
            await send(
                update,
                "Использование: <code>/finish_tournament &lt;ID&gt;</code> "
                "или просто <code>/finish_tournament</code> в чате, "
                "привязанном к турниру.",
            )
            return

    t: dict | None = None
    if tid is not None:
        t = get_tournament(tid)
        if not t:
            await send(update, f"❌ Турнир с ID {tid} не найден.")
            return
    else:
        chat = update.effective_chat
        if chat:
            t = get_tournament_by_chat(chat.id)
        if t is None:
            t = get_active_tournament()
        if t is None:
            await send(
                update,
                "❌ Не нашёл, какой турнир завершать. Укажи ID: "
                "<code>/finish_tournament &lt;ID&gt;</code>.",
            )
            return

    if not _can_manage_tournament(update.effective_user.id, t):
        await send(update, "❌ Завершить турнир может только создатель или админ.")
        return

    if t.get("stage") == "finished":
        await send(
            update,
            f"ℹ️ Турнир <b>{t['name']}</b> (ID {t['id']}) уже завершён.",
        )
        return

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Да, завершить", callback_data=f"fin_t:{t['id']}"),
        InlineKeyboardButton("❌ Отмена",        callback_data="fin_cancel"),
    ]])
    await update.message.reply_text(
        f"⚠️ Завершить турнир <b>{t['name']}</b> (ID {t['id']}, "
        f"{t_full_label(t)})?\n"
        f"После завершения новые матчи в нём приниматься не будут — "
        f"только финальный лидерборд.",
        parse_mode="HTML",
        reply_markup=kb,
    )


def _do_finish_tournament(tid: int) -> dict | None:
    """Flip the tournament's stage to 'finished'. Returns the updated row."""
    update_tournament(tid, stage="finished")
    return get_tournament(tid)


def _player_tag(player_id: int | None) -> str:
    """Render a player id as ``@username`` / nickname / fallback id."""
    if not player_id:
        return "—"
    p = get_player_by_id(player_id)
    if not p:
        return f"id {player_id}"
    return mention(p.get("username") or "") or f"id {player_id}"


def format_tournament_podium(t: dict) -> str:
    """Build the multi-line podium block for the "турнир завершён"
    message. Handles all four shapes:

    * playoff resolved → 🥇 / 🥈 / 🥉 (and optional 4-е).
    * playoff resolved, no bronze played → 🥇 / 🥈 plus a joint
      "🥉 1/2: @a, @b" line for the two SF losers.
    * group-only tournament (no playoff) → top-3 of the leaderboard
      reused as the podium so the message always has *something*.
    * nothing resolvable yet → empty string (caller falls back to the
      generic "/standings" hint).

    Returns the formatted block, or ``""`` if nothing is renderable.
    """
    tid = int(t["id"])
    podium = get_tournament_podium(tid)
    lines: list[str] = []
    if "first" in podium:
        lines.append(f"🥇 1-е место: {_player_tag(podium.get('first'))}")
    if "second" in podium:
        lines.append(f"🥈 2-е место: {_player_tag(podium.get('second'))}")
    if "third" in podium:
        lines.append(f"🥉 3-е место: {_player_tag(podium.get('third'))}")
        if "fourth" in podium:
            lines.append(f"4-е место: {_player_tag(podium.get('fourth'))}")
    elif podium.get("third_tied"):
        tied = ", ".join(_player_tag(pid) for pid in podium["third_tied"])
        lines.append(f"🥉 3-е место (поровну): {tied}")

    if lines:
        return "\n".join(lines)

    # Fallback: no playoff resolution available — use the group ELO
    # leaderboard's top-3 so we still have a podium.
    rows = get_tournament_leaderboard(tid)
    if not rows:
        return ""
    medals = ["🥇 1-е место", "🥈 2-е место", "🥉 3-е место"]
    out: list[str] = []
    for medal, r in zip(medals, rows[:3]):
        p = get_player_by_id(r["player_id"])
        tag = mention(p.get("username") or "") if p else f"id {r['player_id']}"
        out.append(f"{medal}: {tag} — <b>{round(r['elo'])}</b> ELO")
    return "\n".join(out)


# ─────────────────────────────────────────────────────────────────────────────
# /tournament_summary — sends a .txt report + optional AI analysis +
#                       optional Telegra.ph publication.
# ─────────────────────────────────────────────────────────────────────────────


async def _build_and_send_summary(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
    t: dict,
    *,
    want_ai: bool = False,
    want_telegraph: bool = False,
    silent: bool = False,
) -> dict | None:
    """Compute the summary, send a .txt document to the user (and the
    bound chat, if any), optionally enrich with an AI write-up and
    publish a Telegra.ph article. Returns the published Telegra.ph
    payload (or ``None``).

    Heavy work — AI call (~30 s), Telegra.ph publish — runs in a worker
    thread so the Telegram chat stays responsive.
    """
    import tournament_summary as ts  # local import to keep cold paths cheap

    summary = ts.compute_tournament_summary(int(t["id"]))
    if not summary:
        if not silent:
            await send(update, "❌ Не удалось собрать сводку — турнир не найден.")
        return None

    chat = update.effective_chat

    # ── AI analysis (best-effort) ─────────────────────────────────────
    ai_text: str | None = None
    ai_attempts: list[str] = []
    # Telegraph posts always need an AI lead — auto-promote the AI
    # call when the operator asked for a Telegra.ph publish but
    # didn't explicitly request the AI flag. The user explicitly
    # mentioned this requirement: "телеграф постилась иишная версия".
    need_ai = want_ai or want_telegraph
    if need_ai:
        # Tell the user we're working — AI calls can take 30+ s.
        try:
            if chat is not None:
                await ctx.bot.send_chat_action(chat.id, "upload_document")
        except TelegramError:
            pass
        # Build a compact source for the AI. Russian text is multi-byte
        # and we want the model to see all the headlines + at least
        # the bracket + group standings. 16k chars is well within the
        # context window of every model in our fallback chain (most
        # are 128k+).
        source = ts.format_tournament_summary_text(summary)
        if len(source) > 16000:
            source = source[:16000] + "\n…(сводка обрезана)…"
        try:
            ai_text, ai_attempts = await asyncio.to_thread(
                ts.analyze_with_ai, source, "ru",
            )
        except Exception:
            log.exception("tournament_summary: AI analysis crashed")
            ai_text = None
            ai_attempts = ["исключение в analyze_with_ai — см. логи бота"]

    # ── Build the .txt body. ──────────────────────────────────────────
    text_body = ts.format_tournament_summary_text(summary, ai_text=ai_text)
    safe_name = re.sub(r"[^\w\-А-Яа-яЁё]+", "_", summary["name"]).strip("_") or "tournament"
    filename = f"{safe_name}_{summary['id']}_summary.txt"

    # ── Build the hero PNG (best-effort). Failures fall through to the
    # text-only path so the user always gets a usable response. ──────
    photo_bytes: bytes | None = None
    facts_bytes: bytes | None = None
    try:
        from tournament_summary_image import (
            render_tournament_summary_png,
            render_tournament_facts_png,
        )
        photo_bytes = await asyncio.to_thread(
            render_tournament_summary_png, summary, t,
        )
        # Companion "А вы знали?" PNG — only when the tournament has
        # enough drama to populate at least 2 facts.
        if summary.get("facts") and len(summary["facts"]) >= 2:
            facts_bytes = await asyncio.to_thread(
                render_tournament_facts_png, summary, t, 6, None,
            )
    except Exception:
        log.exception(
            "tournament_summary: image rendering failed for tid=%s; "
            "continuing with text-only", summary["id"],
        )
        photo_bytes = None
        facts_bytes = None

    # ── Telegra.ph publish (best-effort) ──────────────────────────────
    published: dict | None = None
    if want_telegraph:
        try:
            if chat is not None:
                await ctx.bot.send_chat_action(chat.id, "upload_document")
        except TelegramError:
            pass
        try:
            published = await asyncio.to_thread(
                ts.publish_to_telegraph,
                summary["name"], summary, ai_text, "GovNL bot",
            )
        except Exception:
            log.exception("tournament_summary: Telegra.ph publish crashed")
            published = None

    # ── Send the .txt file. ───────────────────────────────────────────
    bio = io.BytesIO(text_body.encode("utf-8"))
    bio.name = filename
    caption_lines = [
        f"📄 Сводка турнира <b>{html.escape(summary['name'])}</b> "
        f"(ID {summary['id']}, {summary['type_label']})",
        f"Игроков: <b>{summary['total_players']}</b> · "
        f"матчей: <b>{summary['total_matches']}</b> · "
        f"голов: <b>{summary['total_goals']}</b>",
    ]
    if want_ai:
        if ai_text:
            caption_lines.append("🤖 Анализ ИИ — внутри файла.")
        else:
            # Surface diagnostic so the user can fix the misconfig
            # (most often: model 404, rate-limit on free tier).
            diag = "; ".join(ai_attempts[-3:]) if ai_attempts else "нет попыток"
            caption_lines.append(
                f"🤖 ИИ не ответил: <code>{html.escape(diag[:300])}</code>\n"
                "Подсказка: задай <code>OPENROUTER_API_KEY</code> или "
                "<code>GEMINI_API_KEY</code> в env — фолбэк-ключи из "
                "OCR могут быть лимитированы."
            )
    elif want_telegraph and not ai_text:
        # Telegraph publish silently auto-runs AI — surface the
        # failure so the post explains why the article only has tables.
        diag = "; ".join(ai_attempts[-3:]) if ai_attempts else "нет попыток"
        caption_lines.append(
            f"🤖 ИИ не ответил, статья будет без вступительного текста: "
            f"<code>{html.escape(diag[:200])}</code>"
        )
    if want_telegraph:
        if published and published.get("url"):
            caption_lines.append(
                f"🔗 Telegra.ph: <a href=\"{html.escape(published['url'])}\">"
                f"{html.escape(published['url'])}</a>"
            )
        else:
            caption_lines.append(
                "🔗 Не удалось опубликовать в Telegra.ph — попробуй "
                "позже или вставь содержимое файла вручную."
            )
    caption = "\n".join(caption_lines)

    target_chat = chat.id if chat is not None else None
    # ── Send the hero PNG first (when available) so the user gets the
    # at-a-glance shareable picture before the deep-dive .txt. ───────
    short_caption_lines = [
        f"🏆 <b>{html.escape(summary['name'])}</b> — итоги турнира",
        f"Игроков: <b>{summary['total_players']}</b> · "
        f"матчей: <b>{summary['total_matches']}</b> · "
        f"голов: <b>{summary['total_goals']}</b>",
    ]
    awards = summary.get("awards") or {}
    if awards.get("champion"):
        short_caption_lines.append(
            f"🏆 Чемпион: <b>{html.escape(awards['champion'].get('label', '—'))}</b>"
        )
    short_caption = "\n".join(short_caption_lines)[:1024]

    if photo_bytes:
        photo_io = io.BytesIO(photo_bytes)
        photo_io.name = f"{safe_name}_{summary['id']}_summary.png"
        try:
            if facts_bytes:
                # Two-photo media group: hero + facts. Telegram pins
                # the caption to the first item, and shows them as a
                # carousel — perfect for "summary + extra".
                from telegram import InputMediaPhoto
                facts_io = io.BytesIO(facts_bytes)
                facts_io.name = f"{safe_name}_{summary['id']}_facts.png"
                media = [
                    InputMediaPhoto(media=photo_io, caption=short_caption,
                                     parse_mode="HTML"),
                    InputMediaPhoto(media=facts_io),
                ]
                if target_chat is not None:
                    await ctx.bot.send_media_group(
                        target_chat, media=media, write_timeout=180,
                    )
                # Reroll button — admins only, fast click to refresh
                # the facts image without re-running the whole pipeline.
                if (target_chat is not None
                        and is_admin(update.effective_user.id)
                        and len(summary.get("facts") or []) > 6):
                    try:
                        kb = InlineKeyboardMarkup([[
                            InlineKeyboardButton(
                                "🎲 Ещё факты",
                                callback_data=f"t:facts:{summary['id']}:0",
                            ),
                        ]])
                        await ctx.bot.send_message(
                            target_chat,
                            "Хочешь увидеть другие факты этого турнира?",
                            reply_markup=kb,
                        )
                    except TelegramError:
                        log.warning("failed to send facts reroll button")
            else:
                if update.callback_query and update.callback_query.message:
                    await update.callback_query.message.reply_photo(
                        photo=photo_io, caption=short_caption,
                        parse_mode="HTML", write_timeout=180,
                    )
                elif update.message:
                    await update.message.reply_photo(
                        photo=photo_io, caption=short_caption,
                        parse_mode="HTML", write_timeout=180,
                    )
                elif target_chat is not None:
                    await ctx.bot.send_photo(
                        target_chat, photo=photo_io, caption=short_caption,
                        parse_mode="HTML", write_timeout=180,
                    )
        except TelegramError:
            log.exception("tournament_summary: failed to send hero PNG")

    try:
        if update.callback_query and update.callback_query.message:
            await update.callback_query.message.reply_document(
                document=bio,
                filename=filename,
                caption=caption,
                parse_mode="HTML",
            )
        elif update.message:
            await update.message.reply_document(
                document=bio,
                filename=filename,
                caption=caption,
                parse_mode="HTML",
            )
        elif target_chat is not None:
            await ctx.bot.send_document(
                target_chat,
                document=bio,
                filename=filename,
                caption=caption,
                parse_mode="HTML",
            )
    except TelegramError:
        log.exception("tournament_summary: failed to send .txt document")
        return published

    # Also drop the document into the bound tournament chat (if any and
    # different from where the command was issued) so the whole channel
    # gets the report after /finish_tournament.
    bound_chat = t.get("chat_id")
    if bound_chat and (chat is None or int(bound_chat) != int(chat.id)):
        try:
            if photo_bytes:
                photo_io2 = io.BytesIO(photo_bytes)
                photo_io2.name = f"{safe_name}_{summary['id']}_summary.png"
                await ctx.bot.send_photo(
                    int(bound_chat), photo=photo_io2,
                    caption=short_caption, parse_mode="HTML",
                    write_timeout=180,
                )
        except TelegramError:
            log.warning("tournament_summary: failed to mirror PNG to bound chat %s",
                        bound_chat)
        try:
            bio2 = io.BytesIO(text_body.encode("utf-8"))
            bio2.name = filename
            await ctx.bot.send_document(
                int(bound_chat),
                document=bio2,
                filename=filename,
                caption=caption,
                parse_mode="HTML",
            )
        except TelegramError:
            log.warning("tournament_summary: failed to mirror to bound chat %s",
                        bound_chat)

    return published


async def cmd_tournament_summary(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """``/tournament_summary [ID] [ai] [telegraph]`` — generate a full
    post-tournament report.

    The report is sent as a ``.txt`` document attached to the chat with
    every stat the bot knows: podium, кто на какой стадии вылетел,
    групповые таблицы, плей-офф, бомбардиры, лидерборд по ELO.

    Optional flags (any order, anywhere on the command line):

      • ``ai`` / ``--ai`` / ``ии`` — also includes an AI commentary at
        the end of the file (uses the free OpenRouter models). Adds
        ~30 s to the response.
      • ``telegraph`` / ``--telegraph`` / ``телеграф`` — also publishes
        the report to ``telegra.ph`` and replies with the URL.

    Tournament selection follows the usual rules: explicit ``ID`` arg,
    or chat-bound, or the most recent active tournament.
    """
    raw_args = list(ctx.args or [])
    want_ai = False
    want_telegraph = False

    def _is_ai_flag(tok: str) -> bool:
        return tok.lower() in (
            "ai", "--ai", "-ai", "ии", "--ии", "ai-analysis", "ai_analysis",
            "анализ", "--анализ",
        )

    def _is_tg_flag(tok: str) -> bool:
        return tok.lower() in (
            "telegraph", "--telegraph", "-telegraph", "telegra.ph",
            "телеграф", "--телеграф", "tg", "--tg",
        )

    rest: list[str] = []
    for a in raw_args:
        if _is_ai_flag(a):
            want_ai = True
            continue
        if _is_tg_flag(a):
            want_telegraph = True
            continue
        rest.append(a)

    t, err = _resolve_tournament_from_args(update, ctx, args=rest)
    if t is None:
        await send(update, err or "❌ Не нашёл турнир.")
        return

    if not _can_manage_tournament(update.effective_user.id, t):
        await send(
            update,
            "❌ Сводку может запросить только создатель турнира или админ.",
        )
        return

    # Quick "working on it" ping for slow paths (AI / Telegraph). The
    # actual document follows as a reply once everything is ready.
    if want_ai or want_telegraph:
        await send(
            update,
            "⏳ Собираю сводку…"
            + ("\n🤖 Запрошен анализ ИИ — это может занять до минуты." if want_ai else "")
            + ("\n🔗 Запрошена публикация в Telegra.ph." if want_telegraph else ""),
        )

    await _build_and_send_summary(
        update, ctx, t,
        want_ai=want_ai,
        want_telegraph=want_telegraph,
    )


# ─────────────────────────────────────────────────────────────────────────────
# /past_tournaments + "🏁 Итоги турниров" button — paginated browser of
# finished tournaments. Each row has a 📄 Сводка button that reuses the
# same `_build_and_send_summary` plumbing as /tournament_summary.
# ─────────────────────────────────────────────────────────────────────────────

_FINISHED_PAGE_SIZE = 8


def _list_finished_tournaments(
    offset: int = 0, limit: int = _FINISHED_PAGE_SIZE,
) -> tuple[list[dict], int]:
    """Return ``(rows, total)`` for finished tournaments — newest first.
    Splits offset/limit so the listing paginates without pulling the
    whole history every tap."""
    conn = db.get_conn()
    total_row = conn.execute(
        "SELECT COUNT(*) AS n FROM tournaments WHERE stage = 'finished'"
    ).fetchone()
    total = int(dict(total_row)["n"]) if total_row else 0
    rows = conn.execute(
        "SELECT * FROM tournaments WHERE stage = 'finished' "
        "ORDER BY id DESC LIMIT ? OFFSET ?",
        (int(limit), int(offset)),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows], total


def _format_finished_row(t: dict) -> str:
    """One-line headline for a finished tournament: name, type, podium 1-2-3."""
    bits = [
        f"🏆 <b>{html.escape(t.get('name') or '—')}</b>",
        f"<i>{t_full_label(t)}</i>",
        f"ID <code>{t['id']}</code>",
    ]
    try:
        podium = get_tournament_podium(int(t["id"])) or {}
    except Exception:
        log.exception("podium lookup failed for tid=%s", t.get("id"))
        podium = {}

    medals: list[str] = []
    for key, emoji in (("first", "🥇"), ("second", "🥈"), ("third", "🥉")):
        pid = podium.get(key)
        if not pid:
            continue
        try:
            p = db.get_player_by_id(int(pid)) or {}
        except Exception:
            p = {}
        u = (p.get("username") or "").strip()
        nick = (p.get("game_nickname") or "").strip()
        label = f"@{u}" if u else (nick or f"id{pid}")
        medals.append(f"{emoji} {html.escape(label)}")

    head = " · ".join(bits)
    if medals:
        return head + "\n   " + " ".join(medals)
    return head


async def _send_finished_tournaments_page(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
    *,
    page: int = 0,
    edit: bool = False,
):
    """Render page ``page`` of finished tournaments with one ``📄 Сводка``
    button per row + paginator + a "back" button.

    ``edit=True`` edits the message in place (callback re-render);
    ``edit=False`` posts a fresh message (slash command path).
    """
    page = max(0, int(page))
    offset = page * _FINISHED_PAGE_SIZE
    rows, total = _list_finished_tournaments(offset=offset, limit=_FINISHED_PAGE_SIZE)
    pages_total = max(1, (total + _FINISHED_PAGE_SIZE - 1) // _FINISHED_PAGE_SIZE)
    if not rows and page > 0:
        # Out-of-range page (e.g. an entry was deleted between taps).
        # Fall back to page 0 so the user isn't stranded on an empty view.
        page, offset = 0, 0
        rows, total = _list_finished_tournaments(offset=0, limit=_FINISHED_PAGE_SIZE)
        pages_total = max(1, (total + _FINISHED_PAGE_SIZE - 1) // _FINISHED_PAGE_SIZE)

    if not rows:
        text = (
            "🏁 <b>Итоги турниров</b>\n\n"
            "Завершённых турниров пока нет.\n"
            "Когда хотя бы один будет закрыт через "
            "<code>/finish_tournament</code>, он появится здесь."
        )
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("⬅️ В главное меню", callback_data="menu:home"),
        ]])
        if edit and update.callback_query and update.callback_query.message:
            try:
                await update.callback_query.edit_message_text(
                    text, parse_mode="HTML", reply_markup=kb,
                )
                return
            except TelegramError:
                pass
        await send(update, text, reply_markup=kb)
        return

    lines: list[str] = [
        "🏁 <b>Итоги турниров</b>",
        f"Завершённых: <b>{total}</b> · "
        f"страница <b>{page + 1}/{pages_total}</b>",
        "",
    ]
    kb_rows: list[list[InlineKeyboardButton]] = []
    is_op_admin = is_admin(update.effective_user.id) if update.effective_user else False
    for i, t in enumerate(rows, start=offset + 1):
        lines.append(f"<b>{i}.</b> {_format_finished_row(t)}")
        # One row of action buttons per tournament: quick summary
        # (everyone) + AI / Telegraph (admins only).
        row: list[InlineKeyboardButton] = [
            InlineKeyboardButton(
                f"📄 Сводка #{t['id']}",
                callback_data=f"t:summary:{t['id']}",
            ),
        ]
        if is_op_admin:
            row.append(InlineKeyboardButton(
                "🤖 +ИИ", callback_data=f"t:summaryai:{t['id']}",
            ))
            row.append(InlineKeyboardButton(
                "🔗 +TG", callback_data=f"t:summarytg:{t['id']}",
            ))
        kb_rows.append(row)
        lines.append("")

    # Pagination row.
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️", callback_data=f"t:finished:{page - 1}"))
    nav.append(InlineKeyboardButton(
        f"{page + 1}/{pages_total}", callback_data="t:finished:noop",
    ))
    if (page + 1) < pages_total:
        nav.append(InlineKeyboardButton("▶️", callback_data=f"t:finished:{page + 1}"))
    if len(nav) > 1:
        kb_rows.append(nav)
    kb_rows.append([
        InlineKeyboardButton("⬅️ В главное меню", callback_data="menu:home"),
    ])
    kb = InlineKeyboardMarkup(kb_rows)

    text = "\n".join(lines).rstrip()
    if edit and update.callback_query and update.callback_query.message:
        try:
            await update.callback_query.edit_message_text(
                text, parse_mode="HTML", reply_markup=kb,
            )
            return
        except TelegramError as e:
            log.debug("edit finished-page failed: %s — sending fresh", e)
    await send(update, text, reply_markup=kb)


async def cmd_compare_tournaments(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """``/compare_tournaments`` (aliases: ``/compare``, ``/sravnenie``,
    ``/all_tournaments``) — generate a cross-tournament comparison
    image + .txt file showing the global leaderboards (most titles,
    most appearances, all-time scorers, top ELO) and notable records
    (biggest match, most goal-heavy tournament).
    """
    import tournament_summary as ts
    try:
        from tournament_summary_image import render_all_tournaments_overview_png
    except Exception:
        log.exception("could not import comparison renderer")
        render_all_tournaments_overview_png = None  # type: ignore

    chat = update.effective_chat
    try:
        if chat is not None:
            await ctx.bot.send_chat_action(chat.id, "upload_document")
    except TelegramError:
        pass

    overview = await asyncio.to_thread(ts.compute_all_tournaments_overview)
    if not overview or overview.get("total", 0) == 0:
        await send(
            update,
            "📊 Завершённых турниров пока нет.\n"
            "Закрой хотя бы один через <code>/finish_tournament</code>, "
            "и сравнение появится здесь.",
        )
        return

    # Build the comparison .txt body.
    text_body = ts.format_all_tournaments_text(overview)
    bio = io.BytesIO(text_body.encode("utf-8"))
    bio.name = f"all_tournaments_comparison.txt"

    # Build the PNG (best-effort).
    photo_bytes: bytes | None = None
    if render_all_tournaments_overview_png is not None:
        try:
            photo_bytes = await asyncio.to_thread(
                render_all_tournaments_overview_png, overview,
            )
        except Exception:
            log.exception("comparison PNG rendering failed")
            photo_bytes = None

    totals = overview.get("totals") or {}
    short_caption = (
        "📊 <b>Сравнение всех турниров</b>\n"
        f"Завершено: <b>{overview['total']}</b> турниров · "
        f"Игроков: <b>{totals.get('players', 0)}</b> · "
        f"Матчей: <b>{totals.get('matches', 0)}</b> · "
        f"Голов: <b>{totals.get('goals', 0)}</b>"
    )
    champs = overview.get("champions") or []
    if champs:
        leader = champs[0]
        short_caption += (
            f"\n🏆 Король трофеев: <b>{html.escape(leader['label'])}</b>"
            f" — {leader['titles']} титул(а)"
        )

    caption_full = (
        short_caption + "\n\n📄 Полный список и таблицы — в прикреплённом файле."
    )

    target_chat = chat.id if chat is not None else None

    if photo_bytes:
        try:
            photo_io = io.BytesIO(photo_bytes)
            photo_io.name = "all_tournaments_overview.png"
            if update.message:
                await update.message.reply_photo(
                    photo=photo_io, caption=short_caption[:1024],
                    parse_mode="HTML", write_timeout=180,
                )
            elif target_chat is not None:
                await ctx.bot.send_photo(
                    target_chat, photo=photo_io,
                    caption=short_caption[:1024],
                    parse_mode="HTML", write_timeout=180,
                )
        except TelegramError:
            log.exception("failed to send comparison PNG")

    try:
        if update.message:
            await update.message.reply_document(
                document=bio,
                filename=bio.name,
                caption=caption_full[:1024],
                parse_mode="HTML",
            )
        elif target_chat is not None:
            await ctx.bot.send_document(
                target_chat,
                document=bio,
                filename=bio.name,
                caption=caption_full[:1024],
                parse_mode="HTML",
            )
    except TelegramError:
        log.exception("failed to send comparison .txt")


async def cb_reroll_facts(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Callback ``t:facts:<tid>:<seed>`` — re-render the "А ВЫ ЗНАЛИ?"
    PNG with a different random pick from the lower-tier facts. The
    headline (top-3) facts always stay; only slots 4-6 rotate.

    Telegram callback queries expire after ~15 s, so we ack the
    button **immediately** and tolerate ``BadRequest`` from a stale
    query — the user still gets the regenerated PNG as a fresh
    message even when the spinner toast couldn't be shown.
    """
    query = update.callback_query
    if query is None:
        return

    # Ack the callback up front, before any DB / file work, so the
    # 15 s window doesn't expire while we resolve the tournament.
    # ``BadRequest`` here just means the click was already too old or
    # we already answered — proceed regardless and send the result as
    # a new message.
    async def _ack(text: str | None = None, alert: bool = False) -> None:
        try:
            if text is None:
                await query.answer()
            else:
                await query.answer(text, show_alert=alert)
        except TelegramError:
            log.debug("cb_reroll_facts: query.answer ignored (stale)")

    parts = (query.data or "").split(":")
    if len(parts) < 4:
        await _ack("❌ Битый callback.", alert=True)
        return
    try:
        tid = int(parts[2])
        seed = int(parts[3])
    except ValueError:
        await _ack("❌ Битый callback.", alert=True)
        return

    # Spinner toast first — if Telegram already considers the query
    # too old, ``_ack`` swallows the error and we still continue.
    await _ack("🎲 Кручу колесо…")

    t = await asyncio.to_thread(get_tournament, tid)
    if not t:
        # Can't toast anymore (we already answered); fall back to a
        # normal chat message so the user sees the error.
        if update.effective_chat is not None:
            try:
                await ctx.bot.send_message(
                    update.effective_chat.id,
                    "❌ Турнир не найден.",
                )
            except TelegramError:
                pass
        return

    import tournament_summary as ts
    try:
        from tournament_summary_image import render_tournament_facts_png
    except Exception:
        log.exception("could not import facts renderer")
        return

    summary = await asyncio.to_thread(ts.compute_tournament_summary, tid)
    if not summary or not summary.get("facts"):
        if update.effective_chat is not None:
            try:
                await ctx.bot.send_message(
                    update.effective_chat.id,
                    "🎲 Фактов не нашлось — слишком мало данных по турниру.",
                )
            except TelegramError:
                pass
        return

    new_seed = (seed + 1) * 1009 + tid
    try:
        png = await asyncio.to_thread(
            render_tournament_facts_png, summary, t, 6, new_seed,
        )
    except Exception:
        log.exception("facts re-render failed")
        return
    if not png:
        return

    photo_io = io.BytesIO(png)
    photo_io.name = f"facts_{tid}_{new_seed}.png"
    try:
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton(
                "🎲 Ещё факты",
                callback_data=f"t:facts:{tid}:{seed + 1}",
            ),
        ]])
        if update.effective_chat is not None:
            await ctx.bot.send_photo(
                update.effective_chat.id,
                photo=photo_io,
                caption=(
                    f"🎲 <b>{html.escape(summary.get('name') or '')}</b> — "
                    f"новые факты"
                ),
                parse_mode="HTML",
                reply_markup=kb,
                write_timeout=180,
            )
    except TelegramError:
        log.exception("failed to send rerolled facts PNG")


async def cb_compare_tournaments(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Callback for the "📊 Сравнить турниры" submenu button."""
    query = update.callback_query
    if query is None:
        return
    await query.answer("⏳ Считаю статистику по всем турнирам…")
    await cmd_compare_tournaments(update, ctx)


async def cmd_past_tournaments(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """``/past_tournaments`` (aliases: ``/finished``, ``/itogi``,
    ``/итоги``) — open the paginated list of finished tournaments.
    Each row has a 📄 Сводка button that produces the same .txt
    report as ``/tournament_summary``."""
    page = 0
    if ctx.args:
        try:
            page = max(0, int(ctx.args[0]) - 1)
        except (TypeError, ValueError):
            page = 0
    await _send_finished_tournaments_page(update, ctx, page=page, edit=False)


async def cb_finished_tournaments(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Callback for the "🏁 Итоги турниров" submenu button (and its
    pager arrows). Data: ``t:finished:<page>`` or ``t:finished:noop``."""
    query = update.callback_query
    if query is None:
        return
    await query.answer()
    parts = (query.data or "").split(":")
    if len(parts) >= 3 and parts[2] == "noop":
        return
    page = 0
    if len(parts) >= 3:
        try:
            page = max(0, int(parts[2]))
        except ValueError:
            page = 0
    await _send_finished_tournaments_page(update, ctx, page=page, edit=True)


async def cb_tournament_summary_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Callback for the per-row 📄 Сводка / 🤖 +ИИ / 🔗 +TG buttons.

    Data shapes:
      * ``t:summary:<tid>``    — quick text-only summary (everyone).
      * ``t:summaryai:<tid>``  — quick + AI commentary (admins).
      * ``t:summarytg:<tid>``  — quick + Telegra.ph publish (admins).
    """
    query = update.callback_query
    if query is None:
        return
    data = query.data or ""
    parts = data.split(":")
    if len(parts) < 3:
        await query.answer("❌ Некорректный запрос.", show_alert=True)
        return
    kind = parts[1]
    try:
        tid = int(parts[2])
    except ValueError:
        await query.answer("❌ Некорректный ID турнира.", show_alert=True)
        return

    t = get_tournament(tid)
    if not t:
        await query.answer("❌ Турнир не найден.", show_alert=True)
        return

    want_ai = (kind == "summaryai")
    want_telegraph = (kind == "summarytg")

    # AI / Telegraph variants are admin-only: they consume the
    # OpenRouter free-tier quota and publish a permanent public URL,
    # so a random viewer shouldn't be able to trigger them.
    if (want_ai or want_telegraph) and not is_admin(update.effective_user.id):
        await query.answer(
            "Расширенная сводка доступна только админам.",
            show_alert=True,
        )
        return

    if want_ai or want_telegraph:
        await query.answer("⏳ Готовлю сводку — это может занять до минуты…")
        try:
            await ctx.bot.send_message(
                update.effective_chat.id,
                "⏳ Собираю сводку для турнира "
                f"<b>{html.escape(t.get('name') or '')}</b> (ID {tid})…"
                + ("\n🤖 Запрошен анализ ИИ." if want_ai else "")
                + ("\n🔗 Запрошена публикация в Telegra.ph." if want_telegraph else ""),
                parse_mode="HTML",
            )
        except TelegramError:
            pass
    else:
        await query.answer("📄 Готовлю сводку…")

    await _build_and_send_summary(
        update, ctx, t,
        want_ai=want_ai,
        want_telegraph=want_telegraph,
    )


async def cb_finish_tournament(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Inline-button callback for /finish_tournament confirm/cancel."""
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    if data == "fin_cancel":
        await query.edit_message_text("❌ Отменено.")
        return
    if not data.startswith("fin_t:"):
        return
    try:
        tid = int(data.split(":", 1)[1])
    except ValueError:
        await query.edit_message_text("❌ Невалидный ID.")
        return
    t = get_tournament(tid)
    if not t:
        await query.edit_message_text("❌ Турнир не найден.")
        return
    if not _can_manage_tournament(update.effective_user.id, t):
        await query.edit_message_text("❌ Только создатель или админ.")
        return
    if t.get("stage") == "finished":
        await query.edit_message_text(
            f"ℹ️ Турнир <b>{t['name']}</b> уже был завершён.", parse_mode="HTML"
        )
        return
    _do_finish_tournament(tid)
    log_tournament_action(
        tid,
        actor_telegram_id=update.effective_user.id,
        actor_username=update.effective_user.username,
        action="finish_tournament",
    )
    # Re-read so the formatter sees stage="finished".
    t = get_tournament(tid) or t

    name_esc = html.escape(t["name"])
    head = (
        f"🏁 <b>Турнир {name_esc} завершён</b>\n"
        f"<i>{t_full_label(t)}</i>\n"
    )
    podium_block = format_tournament_podium(t)
    parts: list[str] = [head]
    if podium_block:
        parts.append("\n🏆 <b>Итог:</b>\n" + podium_block + "\n")

    if not t.get("is_official", 1):
        # Local tournament — also include the top-10 ELO leaderboard
        # for the full standings, since these are self-contained.
        rows = get_tournament_leaderboard(tid)
        if rows:
            parts.append("\n📊 <b>Финальный лидерборд</b>:")
            for i, r in enumerate(rows[:10], 1):
                p = get_player_by_id(r["player_id"])
                tag = (
                    mention(p.get("username") or "") if p
                    else f"id {r['player_id']}"
                )
                parts.append(
                    f"{i}. {tag} — <b>{round(r['elo'])}</b> ELO  "
                    f"({r['wins']}W {r['draws']}D {r['losses']}L)"
                )
    else:
        parts.append(
            "\nТоп ELO в этом пуле: /top, /top_vsa, /top_ri"
        )

    text = "\n".join(parts).rstrip()
    try:
        await query.edit_message_text(text, parse_mode="HTML")
    except TelegramError as e:
        # Most common: "Message is not modified" or a stale edit. Fall
        # back to a fresh message so the user still sees the итог.
        log.warning("cb_finish_tournament edit failed: %s — posting fresh", e)
        try:
            chat = update.effective_chat
            if chat is not None:
                await ctx.bot.send_message(
                    chat.id, text, parse_mode="HTML",
                )
        except TelegramError:
            log.exception("cb_finish_tournament fallback send_message failed")

    # Auto-attach the post-tournament summary file. Best-effort —
    # failures here must NOT prevent the tournament from being marked
    # as finished. We don't request AI / Telegraph here so the auto
    # report is fast (no 30+s wait); the operator can re-run
    # ``/tournament_summary <ID> ai telegraph`` to get the rich version.
    try:
        await _build_and_send_summary(
            update, ctx, t,
            want_ai=False,
            want_telegraph=False,
            silent=True,
        )
    except Exception:
        log.exception("cb_finish_tournament: auto summary failed for tid=%s",
                      t.get("id"))


# ─────────────────────────────────────────────────────────────────────────────
# /simulate — admin auto-plays remaining matches in a tournament
# ─────────────────────────────────────────────────────────────────────────────

def _simulated_score(p1: dict, p2: dict) -> tuple[int, int]:
    """
    Pick a plausible football score weighted by ELO. The favourite (higher
    ELO) skews toward winning but every outcome is possible. Goals are drawn
    from a tiny Poisson sampler with the mean tied to each side's expected
    win probability.
    """
    elo1 = p1.get("elo") or 0
    elo2 = p2.get("elo") or 0
    # Expected score 0..1 from ELO formula.
    exp1 = 1.0 / (1.0 + 10 ** ((elo2 - elo1) / 400.0))
    # Stronger side: ~2.1 mean goals; weaker side: ~0.7.
    mean1 = 0.7 + 1.4 * exp1
    mean2 = 0.7 + 1.4 * (1.0 - exp1)
    s1 = _poisson(mean1)
    s2 = _poisson(mean2)
    return min(s1, 7), min(s2, 7)


def _poisson(lam: float) -> int:
    """Tiny, dependency-free Poisson sampler (Knuth's method)."""
    L = math.exp(-lam)
    k = 0
    p = 1.0
    while True:
        k += 1
        p *= random.random()
        if p < L:
            return k - 1


async def cmd_simulate(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    /simulate [tournament_id]

    Admin-only. Auto-plays every remaining `pending` / `reported` match in
    the tournament: picks a score weighted by ELO, marks the match
    confirmed, and runs apply_result so ELO + leaderboards update normally.

    Without an ID — uses the chat-bound tournament if present, else the
    single active tournament. Asks for confirmation before running.
    """
    if not is_admin(update.effective_user.id):
        await send(update, "❌ Только админ.")
        return

    tid: int | None = None
    if ctx.args:
        try:
            tid = int(ctx.args[0])
        except ValueError:
            await send(
                update,
                "Использование: <code>/simulate &lt;ID&gt;</code> или просто "
                "<code>/simulate</code> в чате, привязанном к турниру.",
            )
            return

    t: dict | None = None
    if tid is not None:
        t = get_tournament(tid)
        if not t:
            await send(update, f"❌ Турнир с ID {tid} не найден.")
            return
    else:
        chat = update.effective_chat
        if chat:
            t = get_tournament_by_chat(chat.id)
        if t is None:
            t = get_active_tournament()
        if t is None:
            await send(
                update,
                "❌ Не нашёл активный турнир. Укажи ID: "
                "<code>/simulate &lt;ID&gt;</code>.",
            )
            return

    pending = [
        m for m in db.get_real_tournament_matches(t["id"])
        if m["status"] in ("pending", "reported")
    ]
    if not pending:
        await send(
            update,
            f"ℹ️ В турнире <b>{html.escape(t['name'])}</b> "
            f"нет неотыгранных матчей — симулировать нечего.",
        )
        return

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton(
            f"🎲 Симулировать {len(pending)} матча(ей)",
            callback_data=f"sim_t:{t['id']}",
        ),
        InlineKeyboardButton("❌ Отмена", callback_data="sim_cancel"),
    ]])
    await update.message.reply_text(
        f"🎲 <b>Авто-симуляция турнира</b>\n\n"
        f"Турнир: <b>{html.escape(t['name'])}</b> (ID {t['id']}, {t_full_label(t)})\n"
        f"Будет сыграно матчей: <b>{len(pending)}</b>\n\n"
        f"Счета подбираются с учётом ELO (фаворит чаще выигрывает, "
        f"но возможен любой исход). Все матчи становятся подтверждёнными, "
        f"ELO/W-D-L и лидерборды обновляются как обычно. "
        f"Это действие нельзя отменить.",
        parse_mode="HTML",
        reply_markup=kb,
    )


def _do_simulate_tournament(tid: int, admin_uid: int | None) -> dict:
    """
    Synchronously simulate every pending/reported match in the tournament.
    Returns a stats dict {played, results: [(p1, p2, s1, s2), ...]}.
    """
    pending = [
        m for m in db.get_real_tournament_matches(tid)
        if m["status"] in ("pending", "reported")
    ]
    results: list[tuple[str, str, int, int]] = []
    for m in pending:
        p1 = get_player_by_id(m["player1_id"])
        p2 = get_player_by_id(m["player2_id"])
        if not p1 or not p2:
            continue
        s1, s2 = _simulated_score(p1, p2)
        update_match(
            m["id"],
            score1=s1, score2=s2,
            status="confirmed",
            reported_by=admin_uid,
        )
        try:
            apply_result(m["id"])
        except Exception as e:
            log.warning("apply_result failed for simulated match %s: %s", m["id"], e)
            continue
        results.append((p1["username"], p2["username"], s1, s2))
    return {"played": len(results), "results": results}


async def cb_simulate(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Confirm/cancel callback for /simulate."""
    query = update.callback_query
    data = query.data or ""
    if data == "sim_cancel":
        await query.edit_message_text("❌ Отменено.")
        return
    if not data.startswith("sim_t:"):
        return
    try:
        tid = int(data.split(":", 1)[1])
    except ValueError:
        await query.edit_message_text("❌ Невалидный ID.")
        return
    if not is_admin(update.effective_user.id):
        await query.edit_message_text("❌ Только админ.")
        return
    t = get_tournament(tid)
    if not t:
        await query.edit_message_text("❌ Турнир не найден.")
        return

    await query.edit_message_text(
        f"🎲 Симулирую матчи в <b>{html.escape(t['name'])}</b>…",
        parse_mode="HTML",
    )
    summary = _do_simulate_tournament(tid, update.effective_user.id)

    head = (
        f"✅ Симуляция завершена\n"
        f"Турнир: <b>{html.escape(t['name'])}</b> (ID {tid})\n"
        f"Сыграно матчей: <b>{summary['played']}</b>\n\n"
    )
    # Show first 15 results so the message stays under Telegram's 4096-char limit.
    lines = []
    for u1, u2, s1, s2 in summary["results"][:15]:
        lines.append(f"  {mention(u1)} <b>{s1}:{s2}</b> {mention(u2)}")
    if len(summary["results"]) > 15:
        lines.append(f"  …и ещё {len(summary['results']) - 15} матча(ей)")
    body = "\n".join(lines) if lines else "<i>Ни один матч не был сыгран.</i>"

    await query.message.reply_text(head + body, parse_mode="HTML")

    # After mass-simulation any number of stages can have been completed,
    # not just one. Loop _maybe_auto_advance until it returns False so the
    # bracket walks all the way to the final.
    advanced_any = False
    for _ in range(8):
        try:
            if not _maybe_auto_advance(ctx, tid):
                break
        except Exception:
            log.exception("auto-advance after simulate failed")
            break
        advanced_any = True
    if advanced_any:
        await _announce_stage_advance(
            ctx, tid, _current_playoff_stage(tid),
        )




# ─────────────────────────────────────────────────────────────────────────────
# Button-driven tournament settings panel ("ts:*" callback prefix)
# ─────────────────────────────────────────────────────────────────────────────

def _ts_format_panel_text(t: dict) -> str:
    """Header text shown above the inline tournament-settings keyboard."""
    return (
        f"⚙️ <b>Настройки турнира</b>\n"
        f"<b>{html.escape(t['name'])}</b> "
        f"(ID <code>{t['id']}</code>, {t_full_label(t)})\n\n"
        f"Жми на кнопку, чтобы изменить значение."
    )


async def _ts_show_panel(query, t: dict):
    """(Re)render the settings panel into the same message."""
    await query.edit_message_text(
        _ts_format_panel_text(t),
        parse_mode="HTML",
        reply_markup=_bot_submenu_tournament_settings(t),
    )


async def _handle_tournament_settings_cb(
    update: Update, ctx: ContextTypes.DEFAULT_TYPE, data: str
):
    """
    Handles all "ts:*" callback queries.

    Layout:
      ts:pick                       — pick a tournament
      ts:open:<tid>                 — open settings panel for tournament <tid>
      ts:auto:<tid>                 — toggle auto_confirm
      ts:chat:<tid>                 — toggle reminder_chat_enabled
      ts:third:<tid>                — toggle playoff_third_place
      ts:pen:<tid>                  — toggle playoff_penalties
      ts:slots:<tid>                — show top-N picker
      ts:slots_set:<tid>:<n>        — apply N
      ts:series:<tid>               — show bo-N picker
      ts:series_set:<tid>:<n>       — apply N
      ts:mpg:<tid>                  — show 1/2 picker for group matches/pair
      ts:mpg_set:<tid>:<n>          — apply N
      ts:mpp:<tid>                  — show 1/2 picker for playoff matches/pair
      ts:mpp_set:<tid>:<n>          — apply N
      ts:dm:<tid>                   — show DM reminder hours picker
      ts:dm_set:<tid>:<h>           — apply hours
    """
    query = update.callback_query
    user_id = update.effective_user.id
    parts = data.split(":")
    action = parts[1] if len(parts) > 1 else ""

    if action == "pick":
        # Show a list of tournaments the caller can manage.
        try:
            tournaments = get_active_tournaments()
        except Exception:
            log.exception("ts:pick — failed to list tournaments")
            tournaments = []
        manageable = [t for t in tournaments if _can_manage_tournament(user_id, t)]
        if not manageable:
            await query.edit_message_text(
                "❌ Нет активных турниров, которыми ты можешь управлять.\n"
                "Создай свой через «🏆 Турниры → ➕ Создать турнир».",
            )
            return
        rows = [
            [InlineKeyboardButton(
                f"🏆 {t['name']} (ID {t['id']}, {t_full_label(t)})",
                callback_data=f"ts:open:{t['id']}",
            )]
            for t in manageable
        ]
        rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="menu:home")])
        await query.edit_message_text(
            "⚙️ <b>Выбери турнир</b> для настройки:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(rows),
        )
        return

    # All other actions take a tournament id.
    if len(parts) < 3:
        return
    try:
        tid = int(parts[2])
    except ValueError:
        return
    t = get_tournament(tid)
    if not t:
        await query.edit_message_text("❌ Турнир не найден.")
        return
    if not _can_manage_tournament(user_id, t):
        await query.edit_message_text("❌ Только создатель турнира или админ.")
        return

    # Helper to push an inline picker (list of value buttons) under the panel.
    def _picker(label: str, options: list[tuple[str, str]]) -> InlineKeyboardMarkup:
        rows: list[list[InlineKeyboardButton]] = []
        # Render up to 4 options per row.
        row: list[InlineKeyboardButton] = []
        for txt, cb in options:
            row.append(InlineKeyboardButton(txt, callback_data=cb))
            if len(row) == 4:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        rows.append([InlineKeyboardButton("⬅️ Назад", callback_data=f"ts:open:{tid}")])
        return InlineKeyboardMarkup(rows)

    if action == "open":
        await _ts_show_panel(query, t)
        return

    if action == "auto":
        new_val = 0 if int(t.get("auto_confirm") or 0) else 1
        update_tournament(tid, auto_confirm=new_val)
        t = get_tournament(tid)
        from bot import _submenu_ts_match
        await query.edit_message_text(
            f"⚽ <b>Матчи и OCR</b> — {html.escape(t['name'])} (ID {tid})",
            parse_mode="HTML",
            reply_markup=_submenu_ts_match(t),
        )
        return

    if action == "ocr":
        current = (t.get("ocr_mode") or "ai").lower()
        cycle = {"ai": "ai_no_tess", "ai_no_tess": "score_only", "score_only": "ai"}
        new_val = cycle.get(current, "ai")
        update_tournament(tid, ocr_mode=new_val)
        t = get_tournament(tid)
        from bot import _submenu_ts_match
        await query.edit_message_text(
            f"⚽ <b>Матчи и OCR</b> — {html.escape(t['name'])} (ID {tid})",
            parse_mode="HTML",
            reply_markup=_submenu_ts_match(t),
        )
        return

    if action == "teso":
        current = (t.get("ocr_mode") or "ai").lower()
        new_val = "ai" if current == "score_only" else "score_only"
        update_tournament(tid, ocr_mode=new_val)
        t = get_tournament(tid)
        from bot import _submenu_ts_match
        await query.edit_message_text(
            f"⚽ <b>Матчи и OCR</b> — {html.escape(t['name'])} (ID {tid})",
            parse_mode="HTML",
            reply_markup=_submenu_ts_match(t),
        )
        return

    if action == "third":
        new_val = 0 if int(t.get("playoff_third_place") or 0) else 1
        update_tournament(tid, playoff_third_place=new_val)
        t = get_tournament(tid)
        from bot import _submenu_ts_playoff
        await query.edit_message_text(
            f"🏆 <b>Плей-офф и формат</b> — {html.escape(t['name'])} (ID {tid})",
            parse_mode="HTML",
            reply_markup=_submenu_ts_playoff(t),
        )
        return

    if action == "pen":
        new_val = 0 if int(t.get("playoff_penalties") or 0) else 1
        update_tournament(tid, playoff_penalties=new_val)
        t = get_tournament(tid)
        from bot import _submenu_ts_playoff
        await query.edit_message_text(
            f"🏆 <b>Плей-офф и формат</b> — {html.escape(t['name'])} (ID {tid})",
            parse_mode="HTML",
            reply_markup=_submenu_ts_playoff(t),
        )
        return

    if action == "chat":
        new_val = 0 if int(t.get("reminder_chat_enabled") or 0) else 1
        update_tournament(tid, reminder_chat_enabled=new_val)
        t = get_tournament(tid)
        from bot import _submenu_ts_remind
        await query.edit_message_text(
            f"🔔 <b>Напоминания</b> — {html.escape(t['name'])} (ID {tid})",
            parse_mode="HTML",
            reply_markup=_submenu_ts_remind(t),
        )
        # Telegram only accepts ONE query.answer per callback (we already
        # called it at the top of callback_handler), so we can't reliably
        # show a toast here. If the toggle is "on" but no chat is bound,
        # surface that as a separate plain message so the admin sees it.
        if new_val == 1 and not t.get("chat_id"):
            try:
                await query.message.reply_text(
                    "ℹ️ Чат-напоминания включены, но к этому турниру ещё не "
                    "привязан чат. Зайди в нужный чат и выполни "
                    "<code>/bind_tournament " + str(tid) + "</code>, "
                    "иначе бот не сможет отправлять напоминания.",
                    parse_mode="HTML",
                )
            except TelegramError:
                pass
        return

    if action == "slots":
        opts = [(str(n), f"ts:slots_set:{tid}:{n}") for n in (1, 2, 4, 8)]
        await query.edit_message_text(
            f"🏁 Сколько игроков из каждой группы проходят в плей-офф?\n"
            f"Текущее: <b>{int(t.get('playoff_slots') or 2)}</b>",
            parse_mode="HTML",
            reply_markup=_picker("slots", opts),
        )
        return
    if action == "slots_set":
        n = max(1, min(8, int(parts[3])))
        update_tournament(tid, playoff_slots=n)
        t = get_tournament(tid)
        from bot import _submenu_ts_playoff
        await query.edit_message_text(
            f"🏆 <b>Плей-офф и формат</b> — {html.escape(t['name'])} (ID {tid})",
            parse_mode="HTML",
            reply_markup=_submenu_ts_playoff(t),
        )
        return

    if action == "series":
        opts = [(f"бо-{n}", f"ts:series_set:{tid}:{n}") for n in (1, 3, 5, 7)]
        await query.edit_message_text(
            f"🥊 Длина серии плей-офф (best-of-N).\n"
            f"Текущее: <b>бо-{int(t.get('series_length') or 1) or 1}</b>",
            parse_mode="HTML",
            reply_markup=_picker("series", opts),
        )
        return
    if action == "series_set":
        n = int(parts[3])
        if n not in (1, 3, 5, 7):
            n = 1
        update_tournament(tid, series_length=n)
        t = get_tournament(tid)
        from bot import _submenu_ts_playoff
        await query.edit_message_text(
            f"🏆 <b>Плей-офф и формат</b> — {html.escape(t['name'])} (ID {tid})",
            parse_mode="HTML",
            reply_markup=_submenu_ts_playoff(t),
        )
        return

    if action == "mpg":
        opts = [("1 (одна)", f"ts:mpg_set:{tid}:1"),
                ("2 (дома/в гостях)", f"ts:mpg_set:{tid}:2")]
        await query.edit_message_text(
            f"⚽ Сколько матчей каждый с каждым в группе?\n"
            f"Текущее: <b>{int(t.get('group_matches_per_pair') or 1)}</b>",
            parse_mode="HTML",
            reply_markup=_picker("mpg", opts),
        )
        return
    if action == "mpg_set":
        n = 2 if int(parts[3]) == 2 else 1
        update_tournament(tid, group_matches_per_pair=n)
        t = get_tournament(tid)
        from bot import _submenu_ts_match
        await query.edit_message_text(
            f"⚽ <b>Матчи и OCR</b> — {html.escape(t['name'])} (ID {tid})",
            parse_mode="HTML",
            reply_markup=_submenu_ts_match(t),
        )
        return

    if action == "mpp":
        opts = [("1 (одна)", f"ts:mpp_set:{tid}:1"),
                ("2 (по сумме)", f"ts:mpp_set:{tid}:2")]
        await query.edit_message_text(
            f"🏆 Сколько матчей в паре плей-офф?\n"
            f"  • 1 — одна игра\n"
            f"  • 2 — две, по сумме голов (доп.матч при ничье)\n"
            f"Текущее: <b>{int(t.get('playoff_matches_per_pair') or 1)}</b>",
            parse_mode="HTML",
            reply_markup=_picker("mpp", opts),
        )
        return
    if action == "mpp_set":
        n = 2 if int(parts[3]) == 2 else 1
        update_tournament(tid, playoff_matches_per_pair=n)
        t = get_tournament(tid)
        from bot import _submenu_ts_match
        await query.edit_message_text(
            f"⚽ <b>Матчи и OCR</b> — {html.escape(t['name'])} (ID {tid})",
            parse_mode="HTML",
            reply_markup=_submenu_ts_match(t),
        )
        return

    if action == "dm":
        opts = [("выкл", f"ts:dm_set:{tid}:0"),
                ("6ч",  f"ts:dm_set:{tid}:6"),
                ("12ч", f"ts:dm_set:{tid}:12"),
                ("24ч", f"ts:dm_set:{tid}:24")]
        cur = int(t.get("reminder_dm_hours") or 0)
        cur_lbl = "выкл" if cur <= 0 else f"{cur}ч"
        await query.edit_message_text(
            f"🔔 Как часто напоминать игрокам в личку об их матчах?\n"
            f"Текущее: <b>{cur_lbl}</b>",
            parse_mode="HTML",
            reply_markup=_picker("dm", opts),
        )
        return
    if action == "dm_set":
        h = max(0, min(72, int(parts[3])))
        update_tournament(tid, reminder_dm_hours=h)
        t = get_tournament(tid)
        from bot import _submenu_ts_remind
        await query.edit_message_text(
            f"🔔 <b>Напоминания</b> — {html.escape(t['name'])} (ID {tid})",
            parse_mode="HTML",
            reply_markup=_submenu_ts_remind(t),
        )
        return

    if action == "advmode":
        cur = (t.get("playoff_advance_mode") or "wins").lower()
        opts = [
            ("По победам", f"ts:advmode_set:{tid}:wins"),
            ("По голам",   f"ts:advmode_set:{tid}:goals"),
        ]
        cur_lbl = "по победам" if cur == "wins" else "по голам"
        await query.edit_message_text(
            f"🎯 Как определяется победитель пары в плей-офф?\n"
            f"  • <b>По победам</b> — кто выиграл больше матчей\n"
            f"  • <b>По голам</b> — кто забил больше голов суммарно\n\n"
            f"Текущее: <b>{cur_lbl}</b>",
            parse_mode="HTML",
            reply_markup=_picker("advmode", opts),
        )
        return
    if action == "advmode_set":
        mode = parts[3] if len(parts) > 3 else "wins"
        if mode not in ("wins", "goals"):
            mode = "wins"
        update_tournament(tid, playoff_advance_mode=mode)
        t = get_tournament(tid)
        from bot import _submenu_ts_playoff
        await query.edit_message_text(
            f"🏆 <b>Плей-офф и формат</b> — {html.escape(t['name'])} (ID {tid})",
            parse_mode="HTML",
            reply_markup=_submenu_ts_playoff(t),
        )
        return

    if action == "layout":
        # Pick how the playoff bracket image is drawn — classic
        # "diamond" (mirrored, default) or single-column "linear".
        cur = (t.get("bracket_layout") or "mirrored").lower()
        opts = [
            ("🪞 Симметричная (с двух сторон)", f"ts:layout_set:{tid}:mirrored"),
            ("➡️ Линейная (одна колонка)",      f"ts:layout_set:{tid}:linear"),
        ]
        cur_lbl = "линейная" if cur == "linear" else "симметричная"
        await query.edit_message_text(
            f"🎨 Стиль картинки сетки плей-офф:\n"
            f"  • <b>Симметричная</b> — классическая «бракет-диаграмма», "
            f"стадии сходятся к финалу с обеих сторон.\n"
            f"  • <b>Линейная</b> — все стадии слева направо одной "
            f"колонкой (как было до 2026-05).\n\n"
            f"Текущее: <b>{cur_lbl}</b>",
            parse_mode="HTML",
            reply_markup=_picker("layout", opts),
        )
        return
    if action == "layout_set":
        mode = parts[3] if len(parts) > 3 else "mirrored"
        if mode not in ("mirrored", "linear"):
            mode = "mirrored"
        update_tournament(tid, bracket_layout=mode)
        t = get_tournament(tid)
        from bot import _submenu_ts_style
        await query.edit_message_text(
            f"🎨 <b>Оформление</b> — {html.escape(t['name'])} (ID {tid})",
            parse_mode="HTML",
            reply_markup=_submenu_ts_style(t),
        )
        return

    if action == "overlay":
        # Show overlay percentage picker
        cur_alpha = int(t.get("bg_overlay_alpha") or 165)
        cur_pct = int(round(cur_alpha * 100 / 255))
        opts = [
            ("0% (без затемнения)", f"ts:overlay_set:{tid}:0"),
            ("20%", f"ts:overlay_set:{tid}:20"),
            ("40%", f"ts:overlay_set:{tid}:40"),
            ("65% (по умолчанию)", f"ts:overlay_set:{tid}:65"),
            ("80%", f"ts:overlay_set:{tid}:80"),
            ("100% (фон скрыт)", f"ts:overlay_set:{tid}:100"),
        ]
        await query.edit_message_text(
            f"🌫 <b>Затемнение фона</b> — прозрачность тёмного слоя "
            f"поверх фонового изображения.\n\n"
            f"  • <b>0%</b> — фон полностью виден\n"
            f"  • <b>65%</b> — стандартное затемнение\n"
            f"  • <b>100%</b> — фон полностью закрыт\n\n"
            f"Текущее: <b>{cur_pct}%</b>\n\n"
            f"Для произвольного значения: "
            f"<code>/set_overlay {tid} &lt;0–100&gt;</code>",
            parse_mode="HTML",
            reply_markup=_picker("overlay", opts),
        )
        return
    if action == "overlay_set":
        pct = int(parts[3]) if len(parts) > 3 else 65
        pct = max(0, min(100, pct))
        alpha = int(round(pct * 255 / 100))
        update_tournament(tid, bg_overlay_alpha=alpha)
        t = get_tournament(tid)
        from bot import _submenu_ts_style
        await query.edit_message_text(
            f"🎨 <b>Оформление</b> — {html.escape(t['name'])} (ID {tid})",
            parse_mode="HTML",
            reply_markup=_submenu_ts_style(t),
        )
        return

    if action == "format":
        # Pick whether the tournament runs groups, playoff or both.
        # NOTE: only allowed while the tournament hasn't actually started
        # yet — i.e. no real group or playoff matches have been created.
        # Once matches exist, switching format would orphan them.
        groups_count = sum(
            1 for _ in get_tournament_matches(tid)
        )
        cur_g = int(t.get("groups_only") or 0)
        cur_b = int(t.get("bracket_only") or 0)
        cur_lbl = (
            "только группы" if cur_g
            else "только плей-офф" if cur_b
            else "группы → плей-офф"
        )
        if groups_count > 0:
            # Tournament already running; refuse to switch.
            await query.edit_message_text(
                f"❌ Турнир уже идёт — формат сейчас "
                f"<b>{cur_lbl}</b> и поменять его нельзя "
                f"(уже созданы матчи).",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("⬅️ Назад", callback_data=f"ts:open:{tid}"),
                ]]),
            )
            return
        opts = [
            ("Группы → плей-офф", f"ts:format_set:{tid}:both"),
            ("Только группы",     f"ts:format_set:{tid}:groups"),
            ("🏅 Лига (чемпионат)", f"ts:format_set:{tid}:league"),
            ("Только плей-офф",   f"ts:format_set:{tid}:playoff"),
        ]
        await query.edit_message_text(
            f"📅 Формат турнира:\n"
            f"  • <b>Группы → плей-офф</b> — стандартный: групповой "
            f"этап, лучшие выходят в сетку.\n"
            f"  • <b>Только группы</b> — без плей-офф; по итогам "
            f"групп объявляется победитель (лучший по очкам).\n"
            f"  • <b>🏅 Лига</b> — одна группа, все играют против "
            f"всех (чемпионат). Победитель по таблице.\n"
            f"  • <b>Только плей-офф</b> — сразу сеяная сетка на "
            f"вылет, без групп.\n\n"
            f"Текущее: <b>{cur_lbl}</b>",
            parse_mode="HTML",
            reply_markup=_picker("format", opts),
        )
        return
    if action == "format_set":
        mode = parts[3] if len(parts) > 3 else "both"
        if mode == "groups":
            update_tournament(tid, groups_only=1, bracket_only=0)
        elif mode == "league":
            update_tournament(tid, groups_only=1, bracket_only=0, groups_count=1)
        elif mode == "playoff":
            update_tournament(tid, groups_only=0, bracket_only=1)
        else:
            update_tournament(tid, groups_only=0, bracket_only=0)
        t = get_tournament(tid)
        from bot import _submenu_ts_playoff
        await query.edit_message_text(
            f"🏆 <b>Плей-офф и формат</b> — {html.escape(t['name'])} (ID {tid})",
            parse_mode="HTML",
            reply_markup=_submenu_ts_playoff(t),
        )
        return

    if action == "review":
        # Show all awaiting_admin matches for this tournament with
        # approve/reject inline buttons.
        import database as db
        matches = db.get_tournament_matches(tid)
        awaiting = [
            m for m in matches
            if m.get("status") in ("awaiting_admin", "reported")
        ]
        if not awaiting:
            await query.edit_message_text(
                f"✅ В турнире <b>{html.escape(t['name'])}</b> нет матчей "
                f"на проверку.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("⬅️ Назад", callback_data=f"ts:open:{tid}"),
                ]]),
            )
            return
        lines = [
            f"🛂 <b>Матчи на проверку</b> — {html.escape(t['name'])} "
            f"(ID {tid})\n"
        ]
        kb_rows: list[list] = []
        for m in awaiting[:20]:
            p1 = get_player_by_id(m["player1_id"])
            p2 = get_player_by_id(m["player2_id"])
            u1 = f"@{p1['username']}" if p1 else str(m["player1_id"])
            u2 = f"@{p2['username']}" if p2 else str(m["player2_id"])
            score = ""
            if m.get("score1") is not None and m.get("score2") is not None:
                score = f" <b>{m['score1']}:{m['score2']}</b>"
            lines.append(
                f"  <code>#{m['id']}</code> {u1} vs {u2}{score}"
            )
            kb_rows.append([
                InlineKeyboardButton(
                    f"✅ #{m['id']}",
                    callback_data=f"adm_match:ok:{m['id']}",
                ),
                InlineKeyboardButton(
                    f"❌ #{m['id']}",
                    callback_data=f"adm_match:no:{m['id']}",
                ),
            ])
        if len(awaiting) > 20:
            lines.append(f"\n<i>… и ещё {len(awaiting) - 20}</i>")
        kb_rows.append([
            InlineKeyboardButton("⬅️ Назад", callback_data=f"ts:open:{tid}"),
        ])
        text = "\n".join(lines)
        if len(text) > 4000:
            text = text[:3950] + "\n<i>…обрезано</i>"
        await query.edit_message_text(
            text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(kb_rows),
        )
        return

    if action == "commands":
        # Show full list of tournament management commands
        cmd_text = (
            f"📋 <b>Команды для турнира</b> «{html.escape(t.get('name', ''))}» "
            f"(ID {tid}):\n\n"
            "<b>👥 Управление составом:</b>\n"
            f"<code>/add_player @user1, @user2</code> — добавить игроков\n"
            f"<code>/remove_player @user</code> — удалить игрока\n"
            f"<code>/replace_player @old @new</code> — замена\n"
            f"<code>/list_players</code> — список участников\n\n"
            "<b>📊 Группы и жеребьёвка:</b>\n"
            f"<code>/start_tournament</code> — запустить жеребьёвку\n"
            f"<code>/set_group {tid} A @user1</code> — назначить в группу\n"
            f"<code>/clear_groups {tid}</code> — сброс групп и матчей\n"
            f"<code>/redraw_groups {tid}</code> — перетряхнуть жеребьёвку\n\n"
            "<b>🏆 Плей-офф:</b>\n"
            f"<code>/start_playoff</code> — запустить плей-офф\n"
            f"<code>/advance</code> — продвинуть стадию\n"
            f"<code>/playoff</code> — показать сетку\n"
            f"<code>/prune_phantoms {tid}</code> — убрать фантомы\n\n"
            "<b>⚙️ Настройки:</b>\n"
            f"<code>/set_playoff_slots {tid} N</code> — топ-N в плей-офф\n"
            f"<code>/set_series_length {tid} N</code> — серия бо-N\n"
            f"<code>/set_third_place {tid} on|off</code> — матч за 3-е\n"
            f"<code>/set_overlay {tid} 0-100</code> — прозрачность фона\n"
            f"<code>/set_matches_per_pair {tid} group|playoff N</code>\n"
            f"<code>/set_auto_confirm {tid} on|off</code> — автозачёт\n"
            f"<code>/po_stage_config {tid} sf 3 wins</code> — формат стадии\n"
            f"<code>/set_reminders {tid} dm 24</code> — напоминания\n\n"
            "<b>🎨 Оформление:</b>\n"
            f"<code>/set_tournament_bg {tid}</code> — фон (ответом на фото)\n"
            f"<code>/clear_tournament_bg {tid}</code> — убрать фон\n"
            f"<code>/set_description {tid} текст</code> — описание\n\n"
            "<b>📡 Привязка и трансляция:</b>\n"
            f"<code>/bind_tournament {tid}</code> — привязать чат\n"
            f"<code>/unbind_tournament</code> — отвязать чат\n"
            f"<code>/set_channel {tid} @channel</code> — подписка канала\n"
            f"<code>/broadcast {tid} текст</code> — рассылка\n\n"
            "<b>🔧 Прочее:</b>\n"
            f"<code>/walkover @user</code> — тех. поражение\n"
            f"<code>/admin_report @user1 @user2 X:Y</code> — ручной результат\n"
            f"<code>/simulate {tid}</code> — авто-симуляция\n"
            f"<code>/finish_tournament {tid}</code> — завершить\n"
            f"<code>/add_tadmin {tid} @user</code> — добавить админа турнира\n"
            f"<code>/tlog {tid}</code> — аудит лог"
        )
        await query.edit_message_text(
            cmd_text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⬅️ Назад к настройкам", callback_data=f"ts:open:{tid}"),
            ]]),
        )
        return

    if action == "signup":
        cur = int(t.get("open_signup") or 0)
        update_tournament(tid, open_signup=0 if cur else 1)
        t = get_tournament(tid)
        from bot import _submenu_ts_playoff
        await query.edit_message_text(
            f"🏆 <b>Плей-офф и формат</b> — {html.escape(t['name'])} (ID {tid})",
            parse_mode="HTML",
            reply_markup=_submenu_ts_playoff(t),
        )
        return

    if action == "atl":
        cur = int(t.get("auto_tech_loss_enabled") or 0)
        new_val = 0 if cur else 1
        update_tournament(tid, auto_tech_loss_enabled=new_val)
        t = get_tournament(tid)
        from bot import _submenu_ts_match
        await query.edit_message_text(
            f"⚽ <b>Матчи и OCR</b> — {html.escape(t['name'])} (ID {tid})",
            parse_mode="HTML",
            reply_markup=_submenu_ts_match(t),
        )
        return

    if action == "atl_confirm":
        # Admin confirmed auto-TP for a specific match.
        # Format: ts:atl_confirm:<tid>:<match_id>
        if len(parts) < 4:
            return
        try:
            match_id = int(parts[3])
        except ValueError:
            return
        from match_processor import apply_walkover, apply_result
        from database import get_match, update_match, log_tournament_action
        m = get_match(match_id)
        if not m:
            await query.edit_message_text("❌ Матч не найден.")
            return
        if m.get("played_at") or m.get("status") == "confirmed":
            await query.edit_message_text("ℹ️ Матч уже обработан.")
            return

        score_s = (t.get("auto_tech_loss_score") or "0:3")
        try:
            a_s, _, b_s = score_s.partition(":")
            s1, s2 = int(a_s), int(b_s)
        except ValueError:
            s1, s2 = 0, 3

        p1 = get_player_by_id(m["player1_id"])
        p2 = get_player_by_id(m["player2_id"])

        if (s1, s2) != (0, 3):
            update_match(match_id, score1=s1, score2=s2,
                         status="confirmed", reported_by=None)
            try:
                apply_result(match_id)
            except Exception as e:
                log.warning("apply_result for confirmed tech-loss failed: %s", e)
        else:
            apply_walkover(match_id, m["player1_id"])

        log_tournament_action(
            tid,
            actor_telegram_id=update.effective_user.id,
            actor_username=update.effective_user.username or "[admin]",
            action="auto_tech_loss",
            details=(
                f"match={match_id} "
                f"p1=@{p1['username'] if p1 else m['player1_id']} "
                f"p2=@{p2['username'] if p2 else m['player2_id']} "
                f"score={s1}:{s2} confirmed_by_admin=true"
            ),
        )

        # Notify players
        if p1 and p1.get("telegram_id"):
            try:
                await ctx.bot.send_message(
                    p1["telegram_id"],
                    f"⚠️ Тебе засчитано техническое поражение "
                    f"<b>{s1}:{s2}</b> (матч с {mention(p2['username'])}).",
                    parse_mode="HTML",
                )
            except Exception:
                pass
        if p2 and p2.get("telegram_id"):
            try:
                await ctx.bot.send_message(
                    p2["telegram_id"],
                    f"🏆 Тебе засчитана техническая победа "
                    f"<b>{s2}:{s1}</b> (матч с {mention(p1['username'])}).",
                    parse_mode="HTML",
                )
            except Exception:
                pass

        await query.edit_message_text(
            f"✅ Авто-ТП подтверждено: матч #{match_id}\n"
            f"{mention(p1['username']) if p1 else '?'} vs "
            f"{mention(p2['username']) if p2 else '?'} → "
            f"<b>{s1}:{s2}</b>",
            parse_mode="HTML",
        )

        # Try auto-advance playoff
        from handlers.match import _maybe_auto_advance, _announce_stage_advance, _current_playoff_stage
        try:
            if _maybe_auto_advance(ctx, tid):
                await _announce_stage_advance(
                    ctx, int(tid), _current_playoff_stage(int(tid)),
                )
        except Exception:
            pass
        return

    if action == "atl_skip":
        # Admin declined auto-TP for a specific match.
        # Format: ts:atl_skip:<tid>:<match_id>
        if len(parts) < 4:
            return
        try:
            match_id = int(parts[3])
        except ValueError:
            return
        from database import get_match, log_tournament_action
        m = get_match(match_id)
        if not m:
            await query.edit_message_text("❌ Матч не найден.")
            return

        p1 = get_player_by_id(m["player1_id"])
        p2 = get_player_by_id(m["player2_id"])

        log_tournament_action(
            tid,
            actor_telegram_id=update.effective_user.id,
            actor_username=update.effective_user.username or "[admin]",
            action="auto_tech_loss_skipped",
            details=(
                f"match={match_id} "
                f"p1=@{p1['username'] if p1 else m['player1_id']} "
                f"p2=@{p2['username'] if p2 else m['player2_id']} "
                f"skipped_by_admin=true"
            ),
        )

        await query.edit_message_text(
            f"⏭ Авто-ТП пропущено: матч #{match_id}\n"
            f"{mention(p1['username']) if p1 else '?'} vs "
            f"{mention(p2['username']) if p2 else '?'}\n"
            f"Матч остаётся без изменений — решение за админом.",
            parse_mode="HTML",
        )
        return

    # ── Sub-menu category navigation ─────────────────────────────────────
    if action == "cat_match":
        from bot import _submenu_ts_match
        await query.edit_message_text(
            f"⚽ <b>Матчи и OCR</b> — {html.escape(t['name'])} (ID {tid})",
            parse_mode="HTML",
            reply_markup=_submenu_ts_match(t),
        )
        return

    if action == "cat_playoff":
        from bot import _submenu_ts_playoff
        await query.edit_message_text(
            f"🏆 <b>Плей-офф и формат</b> — {html.escape(t['name'])} (ID {tid})",
            parse_mode="HTML",
            reply_markup=_submenu_ts_playoff(t),
        )
        return

    if action == "cat_style":
        from bot import _submenu_ts_style
        await query.edit_message_text(
            f"🎨 <b>Оформление</b> — {html.escape(t['name'])} (ID {tid})",
            parse_mode="HTML",
            reply_markup=_submenu_ts_style(t),
        )
        return

    if action == "cat_remind":
        from bot import _submenu_ts_remind
        await query.edit_message_text(
            f"🔔 <b>Напоминания</b> — {html.escape(t['name'])} (ID {tid})",
            parse_mode="HTML",
            reply_markup=_submenu_ts_remind(t),
        )
        return

    # ── Footer text (custom signature appended to bot messages) ───────────
    if action == "footer":
        from handlers.common import format_footer_preview, _get_footer_places, FOOTER_PLACES_ALL
        preview = format_footer_preview(t)
        places = _get_footer_places(t)
        enabled_count = sum(1 for v in places.values() if v)
        # Build toggle buttons for each place
        place_rows: list[list[InlineKeyboardButton]] = []
        for key, label in FOOTER_PLACES_ALL.items():
            icon = "✅" if places.get(key, True) else "❌"
            place_rows.append([InlineKeyboardButton(
                f"{icon} {label}",
                callback_data=f"ts:fpl:{tid}:{key}",
            )])
        rows_kb = [
            *place_rows,
            [InlineKeyboardButton("➕ Добавить вариант", callback_data=f"ts:footer_add:{tid}")],
            [InlineKeyboardButton("🗑 Удалить вариант", callback_data=f"ts:footer_del:{tid}"),
             InlineKeyboardButton("🗑 Все", callback_data=f"ts:footer_clear:{tid}")],
            [InlineKeyboardButton("👁 Предпросмотр", callback_data=f"ts:footer_preview:{tid}")],
            [InlineKeyboardButton("⬅️ Назад к настройкам", callback_data=f"ts:open:{tid}")],
        ]
        await query.edit_message_text(
            f"📝 <b>Подпись к сообщениям бота</b>\n\n"
            f"Текст под спойлером в конце сообщений.\n"
            f"Бот выбирает случайный вариант.\n\n"
            f"<b>Где показывать</b> ({enabled_count}/{len(FOOTER_PLACES_ALL)}):\n"
            f"Нажми на тип, чтобы вкл/выкл.\n\n"
            f"<b>Варианты:</b>\n{preview}\n\n"
            f"<code>/set_footer {tid} вариант1 | вариант2</code>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(rows_kb),
        )
        return

    if action == "fpl":
        # Toggle a single footer place: ts:fpl:<tid>:<place_key>
        import json as _json
        from handlers.common import _get_footer_places
        place_key = parts[3] if len(parts) > 3 else ""
        places = _get_footer_places(t)
        if place_key in places:
            places[place_key] = not places[place_key]
            update_tournament(tid, footer_places=_json.dumps(places))
        # Re-render the footer panel
        t = get_tournament(tid)
        # Recursively call the footer action to re-render
        parts_open = ["ts", "footer", str(tid)]
        data_new = ":".join(parts_open)
        await _handle_tournament_settings_cb(update, ctx, data_new)
        return

    if action == "footer_del":
        # Show list of variants with individual delete buttons.
        import json as _json
        raw = (t.get("footer_text") or "").strip()
        variants: list[str] = []
        if raw.startswith("["):
            try:
                parsed = _json.loads(raw)
                if isinstance(parsed, list):
                    variants = [str(v).strip() for v in parsed if str(v).strip()]
            except (_json.JSONDecodeError, ValueError):
                pass
        if not variants and raw:
            variants = [raw]
        if not variants:
            await query.edit_message_text(
                "ℹ️ Нет вариантов для удаления.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("⬅️ Назад", callback_data=f"ts:footer:{tid}"),
                ]]),
            )
            return
        del_rows: list[list[InlineKeyboardButton]] = []
        for i, v in enumerate(variants):
            short = v[:40] + ("…" if len(v) > 40 else "")
            del_rows.append([InlineKeyboardButton(
                f"🗑 {i + 1}. {short}",
                callback_data=f"ts:footer_rm:{tid}:{i}",
            )])
        del_rows.append([InlineKeyboardButton("⬅️ Назад", callback_data=f"ts:footer:{tid}")])
        await query.edit_message_text(
            f"🗑 <b>Удалить вариант подписи</b>\n\n"
            f"Нажми на вариант, чтобы удалить его:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(del_rows),
        )
        return

    if action == "footer_rm":
        # Delete a specific variant by index: ts:footer_rm:<tid>:<index>
        import json as _json
        idx = int(parts[3]) if len(parts) > 3 else -1
        raw = (t.get("footer_text") or "").strip()
        variants: list[str] = []
        if raw.startswith("["):
            try:
                parsed = _json.loads(raw)
                if isinstance(parsed, list):
                    variants = [str(v).strip() for v in parsed if str(v).strip()]
            except (_json.JSONDecodeError, ValueError):
                pass
        if not variants and raw:
            variants = [raw]
        if 0 <= idx < len(variants):
            removed = variants.pop(idx)
            if variants:
                update_tournament(tid, footer_text=_json.dumps(variants, ensure_ascii=False))
            else:
                update_tournament(tid, footer_text="")
        # Re-render footer panel
        t = get_tournament(tid)
        await _handle_tournament_settings_cb(update, ctx, f"ts:footer:{tid}")
        return

    if action == "footer_add":
        # Enter wizard mode — next text message adds a new variant.
        from bot import _wizard_set
        _wizard_set(ctx, "add_footer_variant", {"tid": tid})
        await query.edit_message_text(
            f"✏️ Отправь следующим сообщением новый вариант подписи для "
            f"<b>{html.escape(t['name'])}</b>.\n\n"
            f"HTML разрешён: <code>&lt;b&gt;жирный&lt;/b&gt;</code>, "
            f"<code>&lt;a href=\"https://t.me/channel\"&gt;ссылка&lt;/a&gt;</code>\n\n"
            f"Будет показываться под спойлером. Можно добавить несколько "
            f"вариантов — бот будет выбирать случайный.\n"
            f"Отмена: /cancel",
            parse_mode="HTML",
        )
        return

    if action == "footer_set":
        # Legacy: enter wizard mode to replace all variants with one.
        from bot import _wizard_set
        _wizard_set(ctx, "set_footer", {"tid": tid})
        await query.edit_message_text(
            f"✏️ Отправь следующим сообщением текст подписи для турнира "
            f"<b>{html.escape(t['name'])}</b>.\n\n"
            f"Несколько вариантов — через <code>|</code> (каждый будет рандомным).\n"
            f"HTML разрешён: <code>&lt;b&gt;</code>, <code>&lt;i&gt;</code>, "
            f"<code>&lt;a href=\"...\"&gt;ссылка&lt;/a&gt;</code>\n"
            f"Отмена: /cancel",
            parse_mode="HTML",
        )
        return

    if action == "footer_preview":
        # Show a preview of what the footer looks like in a real message.
        from handlers.common import get_random_footer
        sample_footer = get_random_footer(t)
        if not sample_footer:
            await query.edit_message_text(
                "ℹ️ Подпись не задана — нечего показывать.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("⬅️ Назад", callback_data=f"ts:footer:{tid}"),
                ]]),
            )
            return
        preview_msg = (
            f"⚽ <b>Результат матча</b> [Пример ВСА]\n\n"
            f"@player1 <b>3:1</b> @player2\n\n"
            f"🛂 Отправлено админу на проверку."
            f"{sample_footer}"
        )
        await query.edit_message_text(
            f"👁 <b>Предпросмотр сообщения:</b>\n\n"
            f"{'─' * 30}\n"
            f"{preview_msg}\n"
            f"{'─' * 30}\n\n"
            f"<i>Нажми на спойлер, чтобы раскрыть подпись.</i>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔄 Другой вариант", callback_data=f"ts:footer_preview:{tid}"),
                InlineKeyboardButton("⬅️ Назад", callback_data=f"ts:footer:{tid}"),
            ]]),
        )
        return

    if action == "footer_clear":
        update_tournament(tid, footer_text="")
        await _ts_show_panel(query, get_tournament(tid))
        return


async def cmd_set_footer(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """``/set_footer <tid> <text>`` — set custom footer for a tournament.

    Multiple variants separated by ``|`` — bot picks one randomly per message.
    Example: /set_footer 5 Подписывайтесь на @ch | Ставьте лайк! | Репост
    """
    if not ctx.args or len(ctx.args) < 2:
        await send(
            update,
            "Использование: <code>/set_footer &lt;ID&gt; текст</code>\n\n"
            "Несколько вариантов через <code>|</code>:\n"
            "<code>/set_footer 5 Вариант 1 | Вариант 2 | Вариант 3</code>\n\n"
            "Бот случайно выберет один и покажет под спойлером.\n"
            "Гиперссылки: <code>&lt;a href=\"url\"&gt;текст&lt;/a&gt;</code>",
        )
        return
    try:
        tid = int(ctx.args[0])
    except ValueError:
        await send(update, "❌ Первый аргумент — числовой ID турнира.")
        return
    t = get_tournament(tid)
    if not t:
        await send(update, f"❌ Турнир с ID {tid} не найден.")
        return
    if not _can_manage_tournament(update.effective_user.id, t):
        await send(update, "❌ Только создатель турнира или админ.")
        return
    import json as _json
    text = " ".join(ctx.args[1:]).strip()
    if len(text) > 2000:
        await send(update, "❌ Слишком длинный текст (макс. 2000 символов суммарно).")
        return
    # Split by | into variants
    variants = [v.strip() for v in text.split("|") if v.strip()]
    if not variants:
        await send(update, "❌ Пустой текст.")
        return
    update_tournament(tid, footer_text=_json.dumps(variants, ensure_ascii=False))
    from handlers.common import format_footer_preview
    preview = format_footer_preview(get_tournament(tid))
    await send(
        update,
        f"✅ Подпись для <b>{html.escape(t['name'])}</b> обновлена "
        f"({len(variants)} вар.):\n\n{preview}",
    )


async def cmd_clear_footer(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """``/set_footer <tid>`` (without text) or ``/clear_footer <tid>``."""
    if not ctx.args:
        await send(update, "Использование: <code>/clear_footer &lt;ID&gt;</code>")
        return
    try:
        tid = int(ctx.args[0])
    except ValueError:
        await send(update, "❌ ID должен быть числом.")
        return
    t = get_tournament(tid)
    if not t:
        await send(update, f"❌ Турнир с ID {tid} не найден.")
        return
    if not _can_manage_tournament(update.effective_user.id, t):
        await send(update, "❌ Только создатель турнира или админ.")
        return
    update_tournament(tid, footer_text="")
    await send(update, f"✅ Подпись для <b>{html.escape(t['name'])}</b> удалена.")


async def cmd_set_playoff_slots(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    /set_playoff_slots <tournament_id> <N>

    Sets how many players advance from each group to the playoff stage.
    Defaults to 2 if never set.
    """
    if not is_admin(update.effective_user.id):
        await send(update, "❌ Только админ.")
        return
    if len(ctx.args) < 2:
        await send(
            update,
            "Использование: <code>/set_playoff_slots &lt;ID&gt; &lt;N&gt;</code>\n"
            "Сколько игроков из каждой группы выходит в плей-офф.",
        )
        return
    try:
        tid = int(ctx.args[0])
        n = int(ctx.args[1])
    except ValueError:
        await send(update, "❌ ID и N должны быть числами.")
        return
    if not (1 <= n <= 8):
        await send(update, "❌ N должно быть от 1 до 8.")
        return
    t = get_tournament(tid)
    if not t:
        await send(update, f"❌ Турнир {tid} не найден.")
        return
    update_tournament(tid, playoff_slots=n)
    await send(
        update,
        f"✅ В турнире <b>{html.escape(t['name'])}</b> (ID {tid}) теперь "
        f"в плей-офф из каждой группы выходит <b>{n}</b>.",
    )


async def cmd_set_series_length(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    /set_series_length <tournament_id> <N>

    Sets the best-of-N series length for matches between two players.
    0 or 1 — single-match mode (default behaviour).
    3 — best of 3 (first to 2 wins).
    5 — best of 5 (first to 3 wins) — like the WEEKEND CUP H2H bot.
    7 — best of 7.

    When set to ≥ 2, the bot reports a "X:Y в серии" line after each
    confirmed match and announces "Серия закрыта" when one side reaches
    the required wins.
    """
    if not is_admin(update.effective_user.id):
        await send(update, "❌ Только админ.")
        return
    if len(ctx.args) < 2:
        await send(
            update,
            "Использование: <code>/set_series_length &lt;ID&gt; &lt;N&gt;</code>\n"
            "N: 0/1 — одиночный матч, 3 — бо3, 5 — бо5, 7 — бо7.",
        )
        return
    try:
        tid = int(ctx.args[0])
        n = int(ctx.args[1])
    except ValueError:
        await send(update, "❌ ID и N должны быть числами.")
        return
    if n not in (0, 1, 3, 5, 7):
        await send(update, "❌ N должно быть одним из: 0, 1, 3, 5, 7.")
        return
    t = get_tournament(tid)
    if not t:
        await send(update, f"❌ Турнир {tid} не найден.")
        return
    update_tournament(tid, series_length=n)
    if n <= 1:
        msg = (
            f"✅ В турнире <b>{html.escape(t['name'])}</b> (ID {tid}): "
            f"режим одиночных матчей (без серий)."
        )
    else:
        target = (n + 1) // 2
        msg = (
            f"✅ В турнире <b>{html.escape(t['name'])}</b> (ID {tid}): "
            f"<b>бо{n}</b>, играют до <b>{target}</b> побед."
        )
    await send(update, msg)


def _bool_arg(s: str) -> bool | None:
    """Parse on/off-style flag. Returns None if the string isn't recognised."""
    s = (s or "").strip().lower()
    if s in ("on", "1", "true", "yes", "y", "вкл", "включить", "да"):
        return True
    if s in ("off", "0", "false", "no", "n", "выкл", "выключить", "нет"):
        return False
    return None


async def cmd_set_auto_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    /set_auto_confirm <tournament_id> on|off

    When on, photo-OCR matches are confirmed immediately (no opponent
    button required). Mirrors the WEEKEND CUP H2H bot behaviour.
    """
    if not is_admin(update.effective_user.id):
        await send(update, "❌ Только админ.")
        return
    if len(ctx.args) < 2:
        await send(
            update,
            "Использование: <code>/set_auto_confirm &lt;ID&gt; on|off</code>\n"
            "Если on — матчи по скрину засчитываются сразу, без кнопки соперника.",
        )
        return
    try:
        tid = int(ctx.args[0])
    except ValueError:
        await send(update, "❌ ID должен быть числом.")
        return
    flag = _bool_arg(ctx.args[1])
    if flag is None:
        await send(update, "❌ Используй <code>on</code> или <code>off</code>.")
        return
    t = get_tournament(tid)
    if not t:
        await send(update, f"❌ Турнир {tid} не найден.")
        return
    update_tournament(tid, auto_confirm=1 if flag else 0)
    word = "включён" if flag else "выключен"
    await send(
        update,
        f"✅ Авто-подтверждение в турнире <b>{html.escape(t['name'])}</b> "
        f"(ID {tid}) <b>{word}</b>.",
    )


async def cmd_set_third_place(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    /set_third_place <tournament_id> on|off

    When ``on`` (default for new tournaments), the bot spawns an extra
    "match for 3rd place" between the two semifinal losers as soon as
    the SF is over. It runs in parallel with the final and the
    tournament only flips to ``finished`` after both fixtures are
    confirmed.

    Disabling here AFTER a bronze match has already spawned does NOT
    delete the existing rows — it only prevents new ones. To remove
    already-spawned bronze legs use ``/prune_phantoms`` or edit them
    manually.
    """
    if not is_admin(update.effective_user.id):
        await send(update, "❌ Только админ.")
        return
    if len(ctx.args) < 2:
        await send(
            update,
            "Использование: <code>/set_third_place &lt;ID&gt; on|off</code>\n"
            "Если on — после полуфиналов разыгрывается матч за 3-е место "
            "параллельно с финалом.",
        )
        return
    try:
        tid = int(ctx.args[0])
    except ValueError:
        await send(update, "❌ ID должен быть числом.")
        return
    flag = _bool_arg(ctx.args[1])
    if flag is None:
        await send(update, "❌ Используй <code>on</code> или <code>off</code>.")
        return
    t = get_tournament(tid)
    if not t:
        await send(update, f"❌ Турнир {tid} не найден.")
        return
    update_tournament(tid, playoff_third_place=1 if flag else 0)
    word = "включён" if flag else "выключен"
    await send(
        update,
        f"✅ Матч за 3-е место в турнире <b>{html.escape(t['name'])}</b> "
        f"(ID {tid}) <b>{word}</b>.",
    )


async def cmd_set_penalties(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    /set_penalties <tournament_id> on|off

    When ``on``, OCR is told to also extract the penalty-shootout
    score that FC Mobile shows in parentheses on knockout end-screens
    (e.g. "(3) 3 - 3 (1)" — 3:3 in regulation+ET, home wins on pens
    3:1). The ``pen1``/``pen2`` columns of the match row are populated
    and used as the FINAL tiebreaker in playoff pair resolution when
    the aggregate is level. Group-stage matches are unaffected — a
    drawn group game stays a draw regardless of any shootout shown.

    Default for new tournaments is ``off`` so behaviour is unchanged.
    """
    if not is_admin(update.effective_user.id):
        await send(update, "❌ Только админ.")
        return
    if len(ctx.args) < 2:
        await send(
            update,
            "Использование: <code>/set_penalties &lt;ID&gt; on|off</code>\n"
            "Если on — пенальти из скрина (например, «(3) 3-3 (1)») "
            "распознаются и решают исход пары в плей-офф при ничье "
            "по сумме.",
        )
        return
    try:
        tid = int(ctx.args[0])
    except ValueError:
        await send(update, "❌ ID должен быть числом.")
        return
    flag = _bool_arg(ctx.args[1])
    if flag is None:
        await send(update, "❌ Используй <code>on</code> или <code>off</code>.")
        return
    t = get_tournament(tid)
    if not t:
        await send(update, f"❌ Турнир {tid} не найден.")
        return
    update_tournament(tid, playoff_penalties=1 if flag else 0)
    word = "включены" if flag else "выключены"
    await send(
        update,
        f"✅ Пенальти в плей-офф турнира <b>{html.escape(t['name'])}</b> "
        f"(ID {tid}) <b>{word}</b>.",
    )


async def cmd_set_matches_per_pair(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    /set_matches_per_pair <tournament_id> group|playoff <N>

    N=1 — одна игра между парой (как сейчас).
    N=2 — две игры; в плей-офф с подсчётом по сумме голов и доп.матчем
    при ничье по сумме.
    """
    if not is_admin(update.effective_user.id):
        await send(update, "❌ Только админ.")
        return
    if len(ctx.args) < 3:
        await send(
            update,
            "Использование: <code>/set_matches_per_pair &lt;ID&gt; group|playoff &lt;N&gt;</code>\n"
            "  • group: сколько матчей каждый с каждым в группе (1 или 2)\n"
            "  • playoff: сколько матчей в паре плей-офф (1 = одна игра, 2 = две по сумме голов, доп.матч при ничье)",
        )
        return
    try:
        tid = int(ctx.args[0])
        n = int(ctx.args[2])
    except ValueError:
        await send(update, "❌ ID и N должны быть числами.")
        return
    scope = ctx.args[1].strip().lower()
    if scope not in ("group", "playoff", "groups", "po", "pf"):
        await send(update, "❌ Второй аргумент: <code>group</code> или <code>playoff</code>.")
        return
    if n not in (1, 2):
        await send(update, "❌ N должно быть 1 или 2.")
        return
    t = get_tournament(tid)
    if not t:
        await send(update, f"❌ Турнир {tid} не найден.")
        return
    if scope.startswith("group"):
        update_tournament(tid, group_matches_per_pair=n)
        await send(
            update,
            f"✅ В группе турнира <b>{html.escape(t['name'])}</b> (ID {tid}) "
            f"теперь <b>{n}</b> матч(а) каждый с каждым.",
        )
    else:
        update_tournament(tid, playoff_matches_per_pair=n)
        if n == 2:
            await send(
                update,
                f"✅ В плей-офф <b>{html.escape(t['name'])}</b> (ID {tid}) — "
                f"<b>2 матча</b> в паре по сумме голов; при ничье по сумме — "
                f"<b>доп.матч</b>.",
            )
        else:
            await send(
                update,
                f"✅ В плей-офф <b>{html.escape(t['name'])}</b> (ID {tid}) — "
                f"<b>1 матч</b> в паре.",
            )


async def cmd_set_overlay(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    /set_overlay <tournament_id> <0-100>

    Set the background overlay opacity as a percentage.
    0 = fully transparent (background fully visible through the bracket),
    100 = fully opaque dark overlay (background hidden, text maximally readable).
    Default is ~65%.
    """
    if not is_admin(update.effective_user.id):
        await send(update, "❌ Только админ.")
        return
    if len(ctx.args) < 2:
        await send(
            update,
            "Использование: <code>/set_overlay &lt;ID&gt; &lt;0–100&gt;</code>\n"
            "Прозрачность затемнения фона (в процентах):\n"
            "  • <b>0</b> — фон полностью виден (нет затемнения)\n"
            "  • <b>65</b> — по умолчанию (стандартное затемнение)\n"
            "  • <b>100</b> — фон полностью закрыт",
        )
        return
    try:
        tid = int(ctx.args[0])
    except ValueError:
        await send(update, "❌ ID должен быть числом.")
        return
    try:
        pct = int(ctx.args[1])
    except ValueError:
        await send(update, "❌ Значение должно быть числом от 0 до 100.")
        return
    if pct < 0 or pct > 100:
        await send(update, "❌ Допустимый диапазон: 0–100.")
        return
    t = get_tournament(tid)
    if not t:
        await send(update, f"❌ Турнир {tid} не найден.")
        return
    # Convert percentage (0–100) to alpha (0–255)
    alpha = int(round(pct * 255 / 100))
    update_tournament(tid, bg_overlay_alpha=alpha)
    await send(
        update,
        f"✅ Прозрачность затемнения для <b>{html.escape(t['name'])}</b> "
        f"(ID {tid}) установлена на <b>{pct}%</b>.\n"
        f"Проверь через /table или /playoff.",
    )


async def cmd_set_row_alpha(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    /set_row_alpha <tournament_id> <0-100>

    Прозрачность табличных строк (ники, статистика, хедер группы).
    0 = полностью прозрачные (видно только текст поверх фона),
    100 = полностью непрозрачные (как сейчас по умолчанию).
    Текст (ники, цифры) остаётся всегда 100% видимым.
    """
    if not is_admin(update.effective_user.id):
        await send(update, "❌ Только админ.")
        return
    if len(ctx.args) < 2:
        await send(
            update,
            "Использование: <code>/set_row_alpha &lt;ID&gt; &lt;0–100&gt;</code>\n"
            "Прозрачность строк таблицы (в процентах):\n"
            "  • <b>0</b> — строки полностью прозрачные (виден фон)\n"
            "  • <b>100</b> — по умолчанию (строки непрозрачные)\n"
            "Текст (ники, статистика) всегда виден на 100%.",
        )
        return
    try:
        tid = int(ctx.args[0])
    except ValueError:
        await send(update, "❌ ID должен быть числом.")
        return
    try:
        pct = int(ctx.args[1])
    except ValueError:
        await send(update, "❌ Значение должно быть числом от 0 до 100.")
        return
    if pct < 0 or pct > 100:
        await send(update, "❌ Допустимый диапазон: 0–100.")
        return
    t = get_tournament(tid)
    if not t:
        await send(update, f"❌ Турнир {tid} не найден.")
        return
    alpha = int(round(pct * 255 / 100))
    update_tournament(tid, row_bg_alpha=alpha)
    await send(
        update,
        f"✅ Прозрачность строк таблицы для <b>{html.escape(t['name'])}</b> "
        f"(ID {tid}) установлена на <b>{pct}%</b>.\n"
        f"Проверь через /table или /playoff.",
    )


async def cmd_set_reminders(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    /set_reminders <tournament_id> dm <часы>          — частота лички (0=выкл)
    /set_reminders <tournament_id> chat on|off        — напоминания в чате
    /set_reminders <tournament_id> deadline YYYY-MM-DD HH:MM  — общий дедлайн
    """
    if not is_admin(update.effective_user.id):
        await send(update, "❌ Только админ.")
        return
    if len(ctx.args) < 2:
        await send(
            update,
            "Использование:\n"
            "  <code>/set_reminders &lt;ID&gt; dm &lt;часы&gt;</code> — личка каждые N часов (0=выкл)\n"
            "  <code>/set_reminders &lt;ID&gt; chat on|off</code> — напоминания в чате (расписание escalating)\n"
            "  <code>/set_reminders &lt;ID&gt; deadline YYYY-MM-DD HH:MM</code> — общий дедлайн турнира\n"
            "  <code>/set_reminders &lt;ID&gt; show</code> — показать текущие настройки",
        )
        return
    try:
        tid = int(ctx.args[0])
    except ValueError:
        await send(update, "❌ ID должен быть числом.")
        return
    t = get_tournament(tid)
    if not t:
        await send(update, f"❌ Турнир {tid} не найден.")
        return
    sub = ctx.args[1].strip().lower()
    if sub == "show":
        await send(
            update,
            f"🔔 Напоминания для <b>{html.escape(t['name'])}</b> (ID {tid}):\n"
            f"  • в личку: каждые <b>{t.get('reminder_dm_hours') or 0}</b> ч "
            f"(<i>0 = выкл</i>)\n"
            f"  • в чате: <b>{'ВКЛ' if t.get('reminder_chat_enabled') else 'выкл'}</b>\n"
            f"  • дедлайн турнира: <b>{_fmt_minute_local(t.get('deadline_at')) or '—'}</b>"
            + (f" {_tz_label()}" if t.get('deadline_at') else ""),
        )
        return
    if sub == "dm":
        if len(ctx.args) < 3:
            await send(update, "❌ Укажи количество часов: <code>/set_reminders ID dm 12</code>")
            return
        try:
            hours = int(ctx.args[2])
        except ValueError:
            await send(update, "❌ Часы должны быть числом.")
            return
        if not (0 <= hours <= 168):
            await send(update, "❌ Часы должны быть от 0 до 168 (неделя).")
            return
        update_tournament(tid, reminder_dm_hours=hours)
        if hours == 0:
            await send(update, f"✅ ЛС-напоминания в <b>{html.escape(t['name'])}</b> отключены.")
        else:
            await send(
                update,
                f"✅ ЛС-напоминания в <b>{html.escape(t['name'])}</b> — "
                f"каждые <b>{hours}</b> часов.",
            )
        return
    if sub == "chat":
        if len(ctx.args) < 3:
            await send(update, "❌ <code>/set_reminders ID chat on|off</code>")
            return
        flag = _bool_arg(ctx.args[2])
        if flag is None:
            await send(update, "❌ Используй <code>on</code> или <code>off</code>.")
            return
        update_tournament(tid, reminder_chat_enabled=1 if flag else 0)
        word = "включены" if flag else "выключены"
        if flag and not t.get("chat_id"):
            extra = (
                "\n⚠️ Чат к турниру не привязан — напоминания смогут отправляться "
                "только когда ты сделаешь <code>/bind_tournament</code> в чате."
            )
        else:
            extra = ""
        await send(
            update,
            f"✅ Чат-напоминания в <b>{html.escape(t['name'])}</b> <b>{word}</b>."
            + extra,
        )
        return
    if sub == "deadline":
        if len(ctx.args) < 3:
            await send(
                update,
                "❌ <code>/set_reminders ID deadline YYYY-MM-DD HH:MM</code>",
            )
            return
        raw = " ".join(ctx.args[2:]).strip()
        # Accept "YYYY-MM-DD HH:MM" or just "YYYY-MM-DD".
        from datetime import datetime as _dt
        parsed = None
        for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                parsed = _dt.strptime(raw, fmt)
                break
            except ValueError:
                continue
        if not parsed:
            await send(update, "❌ Не понял дату. Пример: <code>2026-05-12 21:00</code>")
            return
        # `parsed` is naive — interpret it in the operator's display TZ
        # (default МСК) and store the converted UTC string so all
        # downstream code keeps reading UTC the way it always has.
        utc_str = _local_to_utc_str(parsed)
        update_tournament(tid, deadline_at=utc_str)
        await send(
            update,
            f"✅ Дедлайн турнира <b>{html.escape(t['name'])}</b>: "
            f"<b>{parsed.strftime('%Y-%m-%d %H:%M')}</b> {_tz_label()}.",
        )
        return
    await send(update, "❌ Неизвестная подкоманда. См. <code>/set_reminders</code>.")


async def cmd_advance_playoff(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    /advance [tournament_id] — manually nudge a tournament forward
    (build the playoff bracket once the group stage is done, or move
    the bracket to the next round). Allowed for the tournament's creator,
    delegated tournament-admins, and root admins.
    """
    tid: int | None = None
    if ctx.args:
        try:
            tid = int(ctx.args[0])
        except ValueError:
            await send(update, "Использование: <code>/advance &lt;ID&gt;</code>")
            return

    t: dict | None = None
    if tid is not None:
        t = get_tournament(tid)
        if not t:
            await send(update, f"❌ Турнир {tid} не найден.")
            return
    else:
        chat = update.effective_chat
        if chat:
            t = get_tournament_by_chat(chat.id)
        if t is None:
            t = get_active_tournament()
        if t is None:
            await send(update, "❌ Не нашёл активный турнир.")
            return

    if not _can_manage_tournament(update.effective_user.id, t):
        await send(update, "❌ Двигать стадию может только создатель или админ турнира.")
        return

    if _maybe_auto_advance(ctx, t["id"]):
        t2 = get_tournament(t["id"])
        await send(
            update,
            f"🚀 Турнир <b>{html.escape(t['name'])}</b> сдвинут вперёд. "
            f"Текущая стадия: <b>{html.escape(str(t2.get('stage')))}</b>.\n\n"
            f"{format_playoff_bracket(t['id'])}",
        )
        announce_stage_adv = (
            "finished"
            if t2 and t2.get("stage") == "finished"
            else _current_playoff_stage(int(t["id"]))
        )
        log_tournament_action(
            t["id"],
            actor_telegram_id=update.effective_user.id,
            actor_username=update.effective_user.username,
            action="advance_playoff",
            details=f"new_stage={announce_stage_adv}",
        )
        if announce_stage_adv:
            await _announce_stage_advance(
                ctx, int(t["id"]), announce_stage_adv,
            )
    else:
        await send(
            update,
            f"ℹ️ В турнире <b>{html.escape(t['name'])}</b> двигать пока нечего "
            f"(не все матчи стадии сыграны или турнир уже завершён).",
        )


async def cmd_prune_phantoms(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    /prune_phantoms [tournament_id] — admin-only.

    Sweep the matches table for "phantom" pending matches that the bot
    should never have created and delete them. A row is phantom if any
    of these is true:
      • stage='group' but the two players are not in the same group
        (cross-group phantom from /report or admin_report);
      • playoff stage (r16/qf/sf/final) but the same pair already has
        another pending row at the same stage (older duplicates);
      • completely unknown stage.

    Without an argument (or in a chat bound to a tournament), the prune
    scope is that tournament. ``/prune_phantoms all`` sweeps every
    tournament — use with care.
    """
    if not is_admin(update.effective_user.id):
        await send(update, "❌ Только админ.")
        return

    args = ctx.args or []
    scope_all = False
    tid: int | None = None
    if args:
        a = args[0].lower()
        if a in ("all", "*", "все"):
            scope_all = True
        elif a.lstrip("#").isdigit():
            tid = int(a.lstrip("#"))
    if not scope_all and tid is None:
        chat = update.effective_chat
        if chat is not None:
            bound = get_tournament_by_chat(chat.id)
            if bound:
                tid = int(bound["id"])
    if not scope_all and tid is None:
        await send(
            update,
            "Использование: <code>/prune_phantoms &lt;tournament_id&gt;</code> "
            "или <code>/prune_phantoms all</code>.",
        )
        return

    conn = db.get_conn()
    where = "m.status IN ('pending','reported')"
    params: list = []
    if not scope_all:
        where += " AND m.tournament_id = ?"
        params.append(tid)
    rows = conn.execute(
        f"""SELECT m.*,
                   tp1.group_name AS _p1_group,
                   tp2.group_name AS _p2_group
              FROM matches m
         LEFT JOIN tournament_players tp1
                ON tp1.tournament_id = m.tournament_id
               AND tp1.player_id     = m.player1_id
         LEFT JOIN tournament_players tp2
                ON tp2.tournament_id = m.tournament_id
               AND tp2.player_id     = m.player2_id
             WHERE {where}
             ORDER BY m.tournament_id, m.id""",
        params,
    ).fetchall()

    playoff_stages = {"r512", "r256", "r128", "r64", "r32", "r16",
                      "qf", "sf", "final", "third"}
    seen: set[tuple] = set()
    to_delete: list[int] = []
    for r in rows:
        m = dict(r)
        stage = (m.get("stage") or "").lower()
        if stage == "group":
            g1 = m.get("_p1_group"); g2 = m.get("_p2_group")
            if g1 is None or g2 is None or g1 != g2:
                to_delete.append(m["id"])
            continue
        if stage in playoff_stages:
            pair = tuple(sorted((m["player1_id"], m["player2_id"])))
            key = (m.get("tournament_id"), pair, stage, m.get("leg") or 1)
            if key in seen:
                to_delete.append(m["id"])     # duplicate of same pair/stage
            else:
                seen.add(key)
            continue
        # Unknown stage → drop.
        to_delete.append(m["id"])

    if not to_delete:
        conn.close()
        scope_lbl = "all tournaments" if scope_all else f"tournament ID {tid}"
        await send(update, f"✅ Фантомов не найдено в {scope_lbl}.")
        return

    # Delete in batches
    placeholders = ",".join("?" * len(to_delete))
    conn.execute(
        f"DELETE FROM matches WHERE id IN ({placeholders})",
        to_delete,
    )
    conn.commit()
    conn.close()

    scope_lbl = "all tournaments" if scope_all else f"tournament ID {tid}"
    await send(
        update,
        f"🧹 Удалено <b>{len(to_delete)}</b> фантомных pending-матчей в {scope_lbl}.\n"
        f"<i>IDs: {', '.join('#'+str(i) for i in to_delete[:30])}"
        + ("…" if len(to_delete) > 30 else "")
        + "</i>",
    )



# ─────────────────────────────────────────────────────────────────────────────
# /fill_missing_matches — create pending matches for pairs that haven't
# played the full group_matches_per_pair quota yet
# ─────────────────────────────────────────────────────────────────────────────


async def cmd_fill_missing_matches(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/fill_missing_matches [tournament_id]

    Admin-only. Scans each group of the tournament and creates pending
    matches for every pair that has fewer group-stage matches (any
    status) than ``group_matches_per_pair`` requires. Useful when the
    setting was changed after the draw, or when the draw only created
    a single round-robin.

    Without an explicit tournament_id, uses the chat-bound tournament.
    """
    if not is_admin(update.effective_user.id):
        await send(update, "❌ Только админ.")
        return

    tid: int | None = None
    if ctx.args:
        raw = ctx.args[0].lstrip("#")
        if raw.isdigit():
            tid = int(raw)
    if tid is None:
        chat = update.effective_chat
        if chat is not None:
            bound = get_tournament_by_chat(chat.id)
            if bound:
                tid = int(bound["id"])
    if tid is None:
        at = get_active_tournament()
        if at:
            tid = int(at["id"])
    if tid is None:
        await send(update, "❌ Не нашёл турнир. Укажи ID: <code>/fill_missing_matches &lt;id&gt;</code>")
        return

    t = get_tournament(tid)
    if not t:
        await send(update, f"❌ Турнир #{tid} не найден.")
        return

    mpp = max(1, int(t.get("group_matches_per_pair") or 1))
    players = get_tournament_players(tid)

    # Build groups
    groups: dict[str, list[int]] = {}
    for tp in players:
        g = tp.get("group_name") or "?"
        if g == "?":
            continue
        groups.setdefault(g, []).append(tp["player_id"])

    from datetime import datetime, timedelta
    deadline = (datetime.utcnow() + timedelta(hours=48)).strftime("%Y-%m-%d %H:%M:%S")

    created = 0
    for group_name, pids in groups.items():
        for i in range(len(pids)):
            for j in range(i + 1, len(pids)):
                p1, p2 = pids[i], pids[j]
                existing_count = count_group_matches_for_pair(p1, p2, tid)
                need = mpp - existing_count
                if need <= 0:
                    continue
                for leg in range(existing_count + 1, mpp + 1):
                    # Swap home/away for even legs
                    if leg % 2 == 1:
                        a, b = p1, p2
                    else:
                        a, b = p2, p1
                    create_match(
                        tid, a, b,
                        stage="group",
                        round_num=leg,
                        deadline=deadline,
                        leg=leg,
                    )
                    created += 1

    if created == 0:
        await send(
            update,
            f"✅ В турнире <b>{html.escape(t['name'])}</b> (ID {tid}) "
            f"все пары уже имеют по {mpp} матч(ей). Нечего добавлять.",
        )
    else:
        await send(
            update,
            f"✅ Создано <b>{created}</b> новых pending-матчей "
            f"в турнире <b>{html.escape(t['name'])}</b> (ID {tid}).\n"
            f"Теперь каждая пара в группе имеет по {mpp} матч(ей).\n"
            f"Дедлайн: +48ч. Посмотреть: <code>/pending {tid}</code>",
        )


__all__ = [
    'cmd_create_tournament',
    'cmd_tournaments',
    '_parse_add_player_usernames',
    'cmd_add_player',
    'cmd_list_players',
    'cmd_replace_player',
    'cmd_start_tournament',
    'cmd_set_group',
    'cmd_clear_groups',
    'cmd_redraw_groups',
    '_can_bind_tournament',
    'cmd_bind_tournament',
    'cmd_unbind_tournament',
    'cmd_table',
    'cmd_table_text',
    '_send_tournament_picker',
    '_recent_finished_tournaments',
    '_render_table_for',
    'cb_table_pick',
    'cb_table_view',
    'cmd_playoff',
    'cmd_playoff_text',
    '_render_playoff_for',
    '_can_advance_now',
    'cb_advance_now',
    'cb_playoff_pick',
    'cmd_close_groups',
    'cmd_start_playoff',
    'cmd_redraw_playoff',
    'cmd_finish_tournament',
    '_do_finish_tournament',
    'cb_finish_tournament',
    '_simulated_score',
    '_poisson',
    'cmd_simulate',
    '_do_simulate_tournament',
    'cb_simulate',
    '_ts_format_panel_text',
    '_ts_show_panel',
    '_handle_tournament_settings_cb',
    'cmd_set_playoff_slots',
    'cmd_set_series_length',
    '_bool_arg',
    'cmd_set_auto_confirm',
    'cmd_set_third_place',
    'cmd_set_penalties',
    'cmd_set_matches_per_pair',
    'cmd_set_overlay',
    'cmd_set_row_alpha',
    'cmd_set_reminders',
    'cmd_advance_playoff',
    'cmd_prune_phantoms',
    'cmd_fill_missing_matches',
    'cmd_set_footer',
    'cmd_clear_footer',
]




# ─────────────────────────────────────────────────────────────────────────────
# Database export / import — admin-only buttons under "🛠 Админ".
# Lets the operator pull a full DB snapshot from Telegram and push a
# previously-exported snapshot back in. Works for both backends:
#   * SQLite — bonus raw .db file is embedded in the ZIP for one-click
#              restore on a fresh dev box.
#   * Postgres (Railway) — JSON-per-table dump, restored via a single
#                          transactional DELETE + INSERT.
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_export_db(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """``/export_db`` — root-admin only. Sends the current DB as a ZIP
    document. Wraps every table into ``manifest.json`` + per-table JSON
    files; for SQLite a raw ``snapshot.db`` is embedded too."""
    if not is_root_admin(update.effective_user.id):
        await send(update, "❌ Только корневой админ может выгружать БД.")
        return

    chat = update.effective_chat
    try:
        if chat is not None:
            await ctx.bot.send_chat_action(chat.id, "upload_document")
    except TelegramError:
        pass

    try:
        from db_export import export_database
        filename, payload = await asyncio.to_thread(export_database)
    except Exception as e:
        log.exception("export_db failed")
        await send(update, f"❌ Ошибка экспорта: <code>{html.escape(str(e))}</code>")
        return

    bio = io.BytesIO(payload)
    bio.name = filename
    size_kb = len(payload) / 1024
    caption = (
        f"💾 <b>Экспорт БД</b>\n"
        f"Размер: <b>{size_kb:.1f} KB</b>\n\n"
        f"Чтобы восстановить — пришли этот же файл в ответ на кнопку "
        f"«📥 Загрузить БД» (или команду <code>/import_db</code>)."
    )
    try:
        if update.message:
            await update.message.reply_document(
                document=bio, filename=filename,
                caption=caption, parse_mode="HTML",
            )
        elif chat is not None:
            await ctx.bot.send_document(
                chat.id, document=bio, filename=filename,
                caption=caption, parse_mode="HTML",
            )
    except TelegramError:
        log.exception("failed to send DB export")
        await send(update, "❌ Не смог отправить файл.")


async def cmd_import_db(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """``/import_db`` — root-admin only. Arms the bot to expect a DB
    archive as the next document from this admin. The actual restore
    happens in the document handler when the file lands."""
    if not is_root_admin(update.effective_user.id):
        await send(update, "❌ Только корневой админ может загружать БД.")
        return
    ctx.user_data["awaiting_db_import"] = True
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("❌ Отмена", callback_data="db:cancel_import"),
    ]])
    await send(
        update,
        "📥 <b>Загрузка БД</b>\n\n"
        "Пришли мне следующим сообщением файл, который ранее выгрузил "
        "через «💾 Скачать БД» (имя начинается с "
        "<code>govnl_db_export_</code> и заканчивается на "
        "<code>.zip</code>).\n\n"
        "⚠️ <b>Это перезапишет всю текущую БД.</b> Перед загрузкой "
        "обязательно сделай свежий экспорт — на случай отката.",
        reply_markup=kb,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Bot source export — admin grabs the whole code tree as a single ZIP so a
# fresh deploy can be spun up off-Telegram (Railway clone, local replay,
# code review, …). The DB itself goes through /export_db; this is for the
# Python files / Dockerfile / requirements / handlers / assets only.
# ─────────────────────────────────────────────────────────────────────────────

# Top-level directory names whose contents must never be exported. ``.``-
# prefixed dirs (``.git``, ``.venv``, ``.env``, ``.kiro``, …) are pruned
# separately by their leading dot.
_BOT_EXPORT_SKIP_DIRS = frozenset({
    "__pycache__", "venv", "env", ".venv", ".env", "node_modules",
    "tests_fixtures",
})

# File extensions to leave out: bytecode, log files, and the SQLite
# snapshot itself (operators use /export_db for the data).
_BOT_EXPORT_SKIP_EXTS = (
    ".pyc", ".pyo", ".db", ".sqlite", ".sqlite3", ".log",
)

# Hard cap so a stray dump file or fixture image doesn't bloat the
# archive past Telegram's 50 MB document limit.
_BOT_EXPORT_MAX_FILE_BYTES = 5 * 1024 * 1024
_BOT_EXPORT_MAX_TOTAL_BYTES = 45 * 1024 * 1024


def _build_bot_archive() -> tuple[str, bytes]:
    """Walk the project root and pack the source tree into a ZIP.

    Returns ``(filename, zip_bytes)``. Skips bytecode caches, virtualenvs,
    dot-folders, the SQLite snapshot, and oversize binaries. Stops adding
    files once the archive crosses ``_BOT_EXPORT_MAX_TOTAL_BYTES`` so the
    upload never bumps Telegram's 50 MB limit.
    """
    import os
    import zipfile
    from datetime import datetime, timezone

    # ``__file__`` resolves to ``…/govnl/handlers/tournament.py``; two
    # dirname() hops land on the project root regardless of cwd.
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    buf = io.BytesIO()
    total_bytes = 0
    file_count = 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for dirpath, dirnames, filenames in os.walk(project_root):
            # Prune unwanted directories in-place so os.walk skips them.
            dirnames[:] = sorted(
                d for d in dirnames
                if not d.startswith(".") and d not in _BOT_EXPORT_SKIP_DIRS
            )
            for fname in sorted(filenames):
                if fname.startswith("."):
                    continue
                lower = fname.lower()
                if lower.endswith(_BOT_EXPORT_SKIP_EXTS):
                    continue
                full = os.path.join(dirpath, fname)
                rel = os.path.relpath(full, project_root)
                try:
                    sz = os.path.getsize(full)
                except OSError:
                    continue
                if sz > _BOT_EXPORT_MAX_FILE_BYTES:
                    log.info(
                        "export_bot: skipping oversized file %s (%d bytes)",
                        rel, sz,
                    )
                    continue
                if total_bytes + sz > _BOT_EXPORT_MAX_TOTAL_BYTES:
                    log.warning(
                        "export_bot: archive cap hit, stopping at %d files / %d bytes",
                        file_count, total_bytes,
                    )
                    break
                try:
                    zf.write(full, arcname=rel)
                except OSError:
                    log.exception("export_bot: zf.write failed for %s", rel)
                    continue
                total_bytes += sz
                file_count += 1
            else:
                # Inner loop ran to completion → continue walking.
                continue
            # Inner loop hit the size cap → bail out of os.walk.
            break

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    filename = f"govnl_bot_{ts}.zip"
    return filename, buf.getvalue()


async def cmd_export_bot(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """``/export_bot`` — root-admin only. Sends a ZIP archive of the
    bot's source tree (Python modules, ``handlers/``, ``assets/``,
    ``requirements.txt``, Dockerfile, README, …). The database itself
    is not included — use ``/export_db`` for that.

    Aliases: ``/exportbot``, ``/backup_bot``."""
    if not is_root_admin(update.effective_user.id):
        await send(update, "❌ Только корневой админ может выгружать код бота.")
        return

    chat = update.effective_chat
    try:
        if chat is not None:
            await ctx.bot.send_chat_action(chat.id, "upload_document")
    except TelegramError:
        pass

    try:
        filename, payload = await asyncio.to_thread(_build_bot_archive)
    except Exception as e:
        log.exception("export_bot failed")
        await send(
            update,
            f"❌ Ошибка экспорта кода: <code>{html.escape(str(e))}</code>",
        )
        return

    bio = io.BytesIO(payload)
    bio.name = filename
    size_kb = len(payload) / 1024
    caption = (
        f"📦 <b>Экспорт кода бота</b>\n"
        f"Размер: <b>{size_kb:.1f} KB</b>\n\n"
        f"Внутри: исходники Python, <code>handlers/</code>, "
        f"<code>assets/</code>, <code>requirements.txt</code>, "
        f"Dockerfile, README. <i>База данных не вложена — для неё "
        f"/export_db.</i>"
    )
    try:
        if update.message:
            await update.message.reply_document(
                document=bio, filename=filename,
                caption=caption, parse_mode="HTML",
            )
        elif chat is not None:
            await ctx.bot.send_document(
                chat.id, document=bio, filename=filename,
                caption=caption, parse_mode="HTML",
            )
    except TelegramError:
        log.exception("failed to send bot export")
        await send(update, "❌ Не смог отправить файл.")


async def cb_db_buttons(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Dispatch ``db:export``, ``db:import``, ``db:cancel_import``,
    ``db:confirm_import:<token>`` callbacks from the admin panel."""
    query = update.callback_query
    if query is None:
        return
    data = query.data or ""
    if not is_root_admin(update.effective_user.id):
        await query.answer(
            "Доступно только корневому админу.", show_alert=True,
        )
        return
    await query.answer()

    if data == "db:export":
        await cmd_export_db(update, ctx)
        return

    if data == "db:export_bot":
        await cmd_export_bot(update, ctx)
        return

    if data == "db:import":
        await cmd_import_db(update, ctx)
        return

    if data == "db:cancel_import":
        ctx.user_data.pop("awaiting_db_import", None)
        ctx.user_data.pop("pending_db_import", None)
        try:
            await query.edit_message_text("❌ Загрузка БД отменена.")
        except TelegramError:
            pass
        return

    if data.startswith("db:confirm_import:"):
        token = data.split(":", 2)[2]
        pending = ctx.user_data.get("pending_db_import") or {}
        if pending.get("token") != token:
            await query.answer("Подтверждение устарело.", show_alert=True)
            return
        zip_bytes: bytes = pending.get("zip_bytes") or b""
        if not zip_bytes:
            await query.answer("Файл потерян, начни заново.", show_alert=True)
            ctx.user_data.pop("pending_db_import", None)
            return
        try:
            await query.edit_message_text(
                "⏳ Восстанавливаю БД, подожди…", parse_mode="HTML",
            )
        except TelegramError:
            pass
        from db_export import import_database
        result = await asyncio.to_thread(import_database, zip_bytes)
        ctx.user_data.pop("pending_db_import", None)
        ctx.user_data.pop("awaiting_db_import", None)
        if not result.get("ok"):
            err = html.escape(str(result.get("error") or "неизвестная ошибка"))
            await send(
                update,
                f"❌ <b>Импорт не удался</b>\n"
                f"Ошибка: <code>{err}</code>\n"
                f"Транзакция откатилась — текущая БД не пострадала.",
            )
            return
        warns = result.get("warnings") or []
        skipped = result.get("skipped") or []
        msg = [
            "✅ <b>БД восстановлена</b>",
            f"Таблиц: <b>{result['tables_restored']}</b>",
            f"Строк: <b>{result['rows_restored']}</b>",
        ]
        if skipped:
            msg.append(f"Пропущено: <code>{html.escape(', '.join(skipped))}</code>")
        if warns:
            msg.append(
                f"⚠️ Предупреждения ({len(warns)}):\n"
                + "\n".join(f"• <code>{html.escape(w[:200])}</code>"
                            for w in warns[:5])
            )
        await send(update, "\n".join(msg))


async def on_db_import_document(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Document handler that fires when the admin sends a DB ZIP
    after pressing «📥 Загрузить БД» (or running ``/import_db``).

    Accepts ONLY when:
      * the admin previously armed the import via the button/command
        (``ctx.user_data['awaiting_db_import']`` set),
      * the user is a root admin,
      * the document looks like a govnl-db export (filename prefix or
        explicit magic in the manifest).
    """
    if not is_root_admin(update.effective_user.id):
        return
    if not ctx.user_data.get("awaiting_db_import"):
        return  # not in import mode → let other handlers process the doc
    msg = update.message
    if msg is None or msg.document is None:
        return
    doc = msg.document
    fname = (doc.file_name or "").lower()
    # Accept by filename hint or by .zip extension — we'll validate
    # the manifest before applying anyway.
    if not (fname.endswith(".zip") or "govnl_db_export" in fname):
        await msg.reply_text(
            "⚠️ Это не похоже на дамп БД (.zip). Пришли архив, выгруженный "
            "через «💾 Скачать БД», или нажми отмену."
        )
        return
    try:
        tg_file = await ctx.bot.get_file(doc.file_id)
        bio = io.BytesIO()
        await tg_file.download_to_memory(out=bio)
        zip_bytes = bio.getvalue()
    except Exception as e:
        log.exception("import_db: failed to download")
        await msg.reply_text(f"❌ Не смог скачать файл: <code>{html.escape(str(e))}</code>",
                              parse_mode="HTML")
        return

    # Quick sanity-check the manifest before asking for confirmation
    # so the user doesn't burn a tap on a bad file.
    try:
        import zipfile, json as _json
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            manifest = _json.loads(zf.read("manifest.json").decode("utf-8"))
    except Exception as e:
        await msg.reply_text(
            f"❌ Не похоже на дамп бота: <code>{html.escape(str(e))}</code>",
            parse_mode="HTML",
        )
        return

    # Stash the raw bytes in user_data and ask for confirmation.
    import secrets
    token = secrets.token_hex(8)
    ctx.user_data["pending_db_import"] = {
        "token":     token,
        "zip_bytes": zip_bytes,
    }
    tables = manifest.get("tables") or []
    rows_total = sum(int(t.get("rows", 0)) for t in tables)
    backend_lbl = (manifest.get("backend") or "—")
    when = manifest.get("exported_at") or "—"
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Восстановить",
                              callback_data=f"db:confirm_import:{token}")],
        [InlineKeyboardButton("❌ Отмена", callback_data="db:cancel_import")],
    ])
    await msg.reply_text(
        f"📥 <b>Готов восстановить БД из этого файла</b>\n"
        f"Backend дампа: <code>{html.escape(str(backend_lbl))}</code>\n"
        f"Дата экспорта: <code>{html.escape(str(when))}</code>\n"
        f"Таблиц: <b>{len(tables)}</b>, строк: <b>{rows_total}</b>\n\n"
        f"⚠️ <b>Текущая БД будет полностью заменена.</b> Если ещё не "
        f"делал свежий бэкап — нажми «❌ Отмена» и сначала выгрузи "
        f"снимок через «💾 Скачать БД».",
        parse_mode="HTML",
        reply_markup=kb,
    )
