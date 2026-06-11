"""Admin command handlers (Phase 2 of the bot.py split).

Two flavors of admin live here, side by side:

* **Bot admins** (root via ``ADMIN_IDS`` env, or runtime via
  ``/grant_admin``) — global powers across the whole bot:
  ``/grant_admin``, ``/revoke_admin``, ``/admins``, ``/ban``, ``/unban``,
  ``/banned``, ``/elo``, ``/setelo``, ``/admin_setnick``.

* **Tournament admins** (creator + delegated via ``/add_tadmin``) —
  scoped to a single tournament: ``/add_tadmin``, ``/remove_tadmin``,
  ``/tadmins``, ``/broadcast``, ``/set_description``, ``/set_channel``,
  ``/clear_channel``, ``/set_tournament_bg``, ``/clear_tournament_bg``.

Re-exported from ``bot.py`` for backward compatibility.
"""

from __future__ import annotations

import base64
import html
import os
import re

from telegram import Update
from telegram.error import TelegramError
from telegram.ext import ContextTypes

import database as db
from database import (
    add_tournament_admin,
    adjust_player_elo,
    ban_player,
    get_active_tournament,
    get_player,
    get_player_by_game_nickname,
    get_player_by_id,
    get_player_by_telegram_id,
    get_tournament,
    get_tournament_players,
    grant_bot_admin,
    grant_bot_owner,
    is_bot_owner_db,
    is_player_banned,
    list_bot_admins,
    list_bot_owners,
    list_tournament_admins,
    log_tournament_action,
    remove_tournament_admin,
    revoke_bot_admin,
    revoke_bot_owner,
    set_game_nickname,
    set_player_elo,
    unban_player,
    upsert_player,
)

from handlers._helpers import (
    _can_manage_tournament,
    _resolve_player_arg,
    _resolve_tournament_from_args,
)
from handlers.common import (
    ADMIN_IDS,
    _fmt_dt,
    _fmt_minute_local,
    _tz_label,
    arrow,
    is_admin,
    is_owner,
    is_root_admin,
    log,
    mention,
    parse_ban_duration,
    parse_tournament_type_arg,
    send,
    t_full_label,
    t_type_label,
)


# ─────────────────────────────────────────────────────────────────────────────
# /admin_setnick — admin sets/changes another player's game nickname
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_admin_setnick(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Usage:
        /admin_setnick @user <InGameNickname>
        /admin_setnick <id> <InGameNickname>
        or reply to a user's message with /admin_setnick <InGameNickname>

    Admin-only. If the target isn't yet in the players table they are
    created (so the nickname is reserved before they /register).
    """
    if not is_admin(update.effective_user.id):
        await send(update, "❌ Только админ может задавать игровые ники.")
        return

    msg = update.effective_message
    args = list(ctx.args or [])

    target_player: dict | None = None
    target_label: str = ""

    # 1) Reply-to-message form: /admin_setnick <nick>
    if msg and msg.reply_to_message and msg.reply_to_message.from_user:
        u = msg.reply_to_message.from_user
        target_player = get_player(u.username or "") if u.username else None
        if not target_player:
            target_player = upsert_player(
                u.username or f"id_{u.id}",
                telegram_id=u.id,
            )
        target_label = f"@{u.username}" if u.username else f"id {u.id}"
        nick_parts = args
    elif args:
        # 2) /admin_setnick @user <nick>  or  /admin_setnick <id> <nick>
        first = args[0].lstrip("@").strip()
        nick_parts = args[1:]
        if first.isdigit():
            tid = int(first)
            target_player = get_player_by_telegram_id(tid)
            if not target_player:
                # Auto-create a player row for this telegram_id so the
                # admin can pre-register users without @username.
                target_player = upsert_player(f"id_{tid}", telegram_id=tid)
            target_label = f"id {tid}"
        else:
            target_player = get_player(first.lower())
            if not target_player:
                # Reserve nickname against an unregistered username — they
                # link their Telegram account on first /register call.
                target_player = upsert_player(first.lower())
            target_label = f"@{first}"
    else:
        await send(
            update,
            "Использование: <code>/admin_setnick @user InGameNickname</code> "
            "или ответом на сообщение пользователя.\n"
            "Также можно по числовому ID: "
            "<code>/admin_setnick 123456789 InGameNickname</code>.",
        )
        return

    new_nick = " ".join(nick_parts).strip()
    if not new_nick:
        await send(
            update,
            "❌ Не указан новый ник. Пример: <code>/admin_setnick @user MyNick</code>",
        )
        return
    if len(new_nick) > 64:
        await send(update, "❌ Слишком длинный ник (макс. 64 символа).")
        return

    # Don't let one player steal another's nickname.
    existing = get_player_by_game_nickname(new_nick)
    if existing and existing["id"] != target_player["id"]:
        await send(
            update,
            f"❌ Ник <b>{html.escape(new_nick)}</b> уже занят игроком "
            f"{mention(existing['username'])}.",
        )
        return

    old_nick = target_player.get("game_nickname") or "—"
    set_game_nickname(target_player["id"], new_nick)
    await send(
        update,
        f"✅ Игровой ник для {target_label} "
        f"({mention(target_player['username'])}) обновлён:\n"
        f"  было: <b>{html.escape(old_nick)}</b>\n"
        f"  стало: <b>{html.escape(new_nick)}</b>",
    )

    # Best-effort notify the target so they know.
    if target_player.get("telegram_id"):
        try:
            await ctx.bot.send_message(
                target_player["telegram_id"],
                f"ℹ️ Админ установил твой игровой ник: <b>{html.escape(new_nick)}</b>",
                parse_mode="HTML",
            )
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────────────
# /admin_addplayer — register a player by Telegram ID + display name
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_admin_addplayer(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Usage:
        /admin_addplayer <telegram_id> <DisplayName>

    Admin-only. Creates (or updates) a player record for a person who has
    no public @username. The synthetic ``id_<telegram_id>`` placeholder is
    stored in the ``username`` column, and ``game_nickname`` is set to the
    provided display name so the bot can show a human-readable name in
    leaderboards, match reports, and mentions (via ``mention_player``).

    If the player already exists (by telegram_id), just updates their
    game_nickname (display name).

    Aliases: ``/addplayer``, ``/admin_add_player``.
    """
    if not is_admin(update.effective_user.id):
        await send(update, "❌ Только админ может добавлять игроков.")
        return

    args = list(ctx.args or [])
    if len(args) < 2:
        await send(
            update,
            "Использование: <code>/admin_addplayer &lt;telegram_id&gt; &lt;Ник&gt;</code>\n\n"
            "Пример: <code>/admin_addplayer 123456789 Вася Пупкин</code>\n\n"
            "Регистрирует игрока без @username по его Telegram ID и задаёт "
            "ему отображаемое имя (ник). Если игрок уже зарегистрирован — "
            "обновляет ник.",
        )
        return

    if not args[0].isdigit():
        await send(
            update,
            f"❌ Первый аргумент должен быть числовым Telegram ID, "
            f"получил: <code>{html.escape(args[0])}</code>",
        )
        return

    tid = int(args[0])
    display_name = " ".join(args[1:]).strip()

    if not display_name:
        await send(update, "❌ Не указан ник (отображаемое имя).")
        return
    if len(display_name) > 64:
        await send(update, "❌ Слишком длинный ник (макс. 64 символа).")
        return

    # Check if nick is already taken by another player
    existing_nick = get_player_by_game_nickname(display_name)
    existing_player = get_player_by_telegram_id(tid)

    if existing_nick and (not existing_player or existing_nick["id"] != existing_player["id"]):
        await send(
            update,
            f"❌ Ник <b>{html.escape(display_name)}</b> уже занят игроком "
            f"{mention(existing_nick['username'])}.",
        )
        return

    if existing_player:
        # Player already registered — just update game_nickname
        old_nick = existing_player.get("game_nickname") or "—"
        set_game_nickname(existing_player["id"], display_name)
        await send(
            update,
            f"✅ Игрок с Telegram ID <code>{tid}</code> уже зарегистрирован.\n"
            f"Обновил ник: <b>{html.escape(old_nick)}</b> → <b>{html.escape(display_name)}</b>",
        )
    else:
        # Create new player with synthetic username
        synthetic_username = f"id_{tid}"
        player = upsert_player(synthetic_username, telegram_id=tid)
        set_game_nickname(player["id"], display_name)
        await send(
            update,
            f"✅ Игрок зарегистрирован!\n"
            f"  Telegram ID: <code>{tid}</code>\n"
            f"  Ник: <b>{html.escape(display_name)}</b>\n\n"
            f"Теперь его можно добавлять в турниры по ID: "
            f"<code>/add_player {tid}</code> или <code>/admin_addplayer_late &lt;tid_турнира&gt; {tid}</code>",
        )

    # Notify the user (best-effort)
    try:
        await ctx.bot.send_message(
            tid,
            f"ℹ️ Админ зарегистрировал тебя в лиге с ником: "
            f"<b>{html.escape(display_name)}</b>\n\n"
            f"Ты можешь изменить ник командой /setnick или написать /help "
            f"для списка команд.",
            parse_mode="HTML",
        )
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# /relink_player — merge an old @handle row into the current id_<X> row
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_relink_player(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Usage:
        /relink_player @oldhandle <telegram_id>
        /relink_player @oldhandle <new_player_id>
        /relink_player <old_player_id> <telegram_id>

    Admin-only. Merges two ``players`` rows that refer to the same human
    being — typically when an admin pre-registered ``@oldhandle`` (with
    ``/admin_setnick`` etc.), the user later removed their public
    ``@username``, then DM'd the bot which created a second row under
    the synthetic ``id_<telegram_id>`` placeholder.

    First argument identifies the **old** row (the one we want to drop).
    Second argument identifies the **new** row to keep (numeric Telegram
    ID, or its internal ``players.id``). The new row's ``telegram_id``
    wins; the old row's ``@handle``, ``game_nickname``, lifetime stats
    sums, max ELO, and tournament participation/match history are all
    preserved on the kept row.

    Aliases: ``/relink``, ``/merge_player``, ``/mergeplayer``.
    """
    if not is_admin(update.effective_user.id):
        await send(update, "❌ Только админ может объединять записи игроков.")
        return

    args = list(ctx.args or [])
    if len(args) < 2:
        await send(
            update,
            "Использование:\n"
            "<code>/relink_player @oldhandle 77777</code> "
            "(объединить старую @oldhandle с записью пользователя tid=77777)\n"
            "<code>/relink_player @oldhandle 42</code> "
            "(второй аргумент — внутренний player id)\n"
            "<code>/relink_player 17 77777</code> "
            "(оба по player id / telegram_id)\n\n"
            "Команда переносит все турниры, матчи и ELO со старой записи "
            "на новую, складывает накопительные счётчики и удаляет дубликат.",
        )
        return

    def _resolve(tok: str) -> dict | None:
        s = tok.strip().lstrip("@")
        if not s:
            return None
        # Numeric: try internal player.id first, then telegram_id.
        if s.isdigit():
            n = int(s)
            p = get_player_by_id(n)
            if p:
                return p
            return get_player_by_telegram_id(n)
        return get_player(s.lower())

    old_p = _resolve(args[0])
    new_p = _resolve(args[1])
    if not old_p:
        await send(update, f"❌ Не нашёл старую запись по «<code>{html.escape(args[0])}</code>».")
        return
    if not new_p:
        await send(update, f"❌ Не нашёл новую запись по «<code>{html.escape(args[1])}</code>».")
        return
    if int(old_p["id"]) == int(new_p["id"]):
        await send(
            update,
            "❌ Это одна и та же запись — объединять нечего.\n"
            f"player id={old_p['id']}, username={mention(old_p['username'])}.",
        )
        return

    # Pre-flight summary so the admin knows exactly what's about to happen.
    old_label = (
        f"player id=<code>{old_p['id']}</code> "
        f"({mention(old_p['username'])}, "
        f"tid={old_p.get('telegram_id') or '—'}, "
        f"ник={html.escape(old_p.get('game_nickname') or '—')})"
    )
    new_label = (
        f"player id=<code>{new_p['id']}</code> "
        f"({mention(new_p['username'])}, "
        f"tid={new_p.get('telegram_id') or '—'}, "
        f"ник={html.escape(new_p.get('game_nickname') or '—')})"
    )

    try:
        counters = db.merge_players(
            keep_id=int(new_p["id"]),
            drop_id=int(old_p["id"]),
        )
    except (ValueError, LookupError) as e:
        await send(update, f"❌ Не получилось объединить: {html.escape(str(e))}.")
        return
    except Exception as e:
        log.exception("merge_players failed: %s", e)
        await send(update, f"❌ Внутренняя ошибка при объединении: {html.escape(str(e))}.")
        return

    # Re-fetch the kept row so we can show the resulting state.
    kept = get_player_by_id(int(new_p["id"])) or new_p
    await send(
        update,
        "✅ <b>Записи объединены.</b>\n\n"
        f"Дроп: {old_label}\n"
        f"Кип:  {new_label}\n\n"
        f"Перенесено матчей: <b>{counters['matches_moved']}</b>\n"
        f"Турниров с пересечением (стат-сумма): <b>{counters['tp_overlap']}</b>\n"
        f"Изолированных ELO с пересечением: <b>{counters['elo_overlap']}</b>\n"
        f"Голов перепривязано: <b>{counters['goals_moved']}</b>\n\n"
        "Итог по объединённой записи:\n"
        f"  username: {mention(kept['username'])}\n"
        f"  telegram_id: <code>{kept.get('telegram_id') or '—'}</code>\n"
        f"  игровой ник: <b>{html.escape(kept.get('game_nickname') or '—')}</b>\n"
        f"  ELO: <b>{int(kept.get('elo') or 0)}</b>",
    )


# ─────────────────────────────────────────────────────────────────────────────
# /ban  /unban  /banned (admin only)
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_ban(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await send(update, "❌ Только админ.")
        return
    if not ctx.args:
        await send(
            update,
            "Использование: <code>/ban @user [длительность] [причина]</code>\n"
            "Длительность: <b>24</b> (часы), <b>7d</b> (дни), <b>30m</b> (минуты), "
            "<b>perm</b> (бессрочно). По умолчанию — <b>24ч</b>.",
        )
        return

    uname = ctx.args[0].lstrip("@").lower()
    target = get_player(uname)
    if not target:
        await send(update, f"❌ Игрок {mention(uname)} не найден.")
        return

    duration_arg = ctx.args[1] if len(ctx.args) > 1 else "24"
    try:
        until_iso, label = parse_ban_duration(duration_arg)
    except ValueError as e:
        await send(update, f"❌ {e}")
        return

    reason = " ".join(ctx.args[2:]).strip() or None
    ban_player(target["id"], until_iso, reason)

    until_str = until_iso or "бессрочно"
    msg = (
        f"🚫 {mention(uname)} забанен(а) ({label}).\n"
        f"До: <b>{until_str}</b>\n"
        f"Причина: {reason or '—'}"
    )
    await send(update, msg)
    if target.get("telegram_id"):
        try:
            await ctx.bot.send_message(
                target["telegram_id"],
                f"🚫 Тебе выдан бан ({label}).\nДо: <b>{until_str}</b>\nПричина: {reason or '—'}",
                parse_mode="HTML",
            )
        except Exception:
            pass


async def cmd_unban(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await send(update, "❌ Только админ.")
        return
    if not ctx.args:
        await send(update, "Использование: <code>/unban @user</code>")
        return
    uname = ctx.args[0].lstrip("@").lower()
    target = get_player(uname)
    if not target:
        await send(update, f"❌ Игрок {mention(uname)} не найден.")
        return
    if not is_player_banned(target):
        await send(update, f"⚠️ {mention(uname)} не в бане.")
        return
    unban_player(target["id"])
    await send(update, f"✅ Бан с {mention(uname)} снят.")
    if target.get("telegram_id"):
        try:
            await ctx.bot.send_message(
                target["telegram_id"],
                "✅ С тебя сняли бан. Можешь снова участвовать в турнирах.",
            )
        except Exception:
            pass


async def cmd_banned(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Show currently-banned players (anyone can call)."""
    conn = db.get_conn()
    rows = conn.execute(
        "SELECT * FROM players WHERE banned_until IS NOT NULL"
    ).fetchall()
    conn.close()
    banned = [dict(r) for r in rows if is_player_banned(dict(r))]
    if not banned:
        await send(update, "✅ Никого нет в бане.")
        return
    lines = ["🚫 <b>Забаненные игроки</b>\n"]
    for p in banned:
        lines.append(
            f"• {mention(p['username'])} — до <b>{_fmt_dt(p['banned_until'])}</b>\n"
            f"  Причина: {p.get('banned_reason') or '—'}"
        )
    await send(update, "\n".join(lines))


# ─────────────────────────────────────────────────────────────────────────────
# /elo  /setelo  (admin only — manual ELO changes)
# ─────────────────────────────────────────────────────────────────────────────

_ELO_DELTA_RE = re.compile(r"^([+-]?)(\d+(?:\.\d+)?)$")


async def cmd_elo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await send(update, "❌ Только админ.")
        return
    if len(ctx.args) < 2:
        await send(
            update,
            "Использование: <code>/elo @user +50</code>  или  <code>/elo @user -100 причина</code>",
        )
        return

    uname = ctx.args[0].lstrip("@").lower()
    delta_str = ctx.args[1]
    note = " ".join(ctx.args[2:]).strip()

    target = get_player(uname)
    if not target:
        await send(update, f"❌ Игрок {mention(uname)} не найден.")
        return

    m = _ELO_DELTA_RE.match(delta_str)
    if not m:
        await send(
            update,
            "❌ Дельта должна быть числом, например <code>+50</code> или <code>-100</code>.",
        )
        return
    sign, num = m.group(1) or "+", m.group(2)
    delta = float(num) if sign != "-" else -float(num)

    by = "@" + (update.effective_user.username or str(update.effective_user.id))
    new_elo = adjust_player_elo(target["id"], delta, by_user=by, note=note)
    arrow_str = arrow(int(round(delta)))
    await send(
        update,
        f"📈 ELO {mention(uname)}: {round(target['elo'])} → <b>{round(new_elo)}</b> ({arrow_str})\n"
        f"Изменил(а): {by}{(' — ' + note) if note else ''}",
    )
    if target.get("telegram_id"):
        try:
            await ctx.bot.send_message(
                target["telegram_id"],
                f"⚖️ Админ изменил твой ELO: {round(target['elo'])} → "
                f"<b>{round(new_elo)}</b> ({arrow_str})"
                + (f"\nПричина: {note}" if note else ""),
                parse_mode="HTML",
            )
        except Exception:
            pass


async def cmd_setelo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await send(update, "❌ Только админ.")
        return
    if len(ctx.args) < 2:
        await send(update, "Использование: <code>/setelo @user 200 [причина]</code>")
        return

    uname = ctx.args[0].lstrip("@").lower()
    try:
        new_value = float(ctx.args[1])
    except ValueError:
        await send(update, "❌ Значение ELO должно быть числом.")
        return
    note = " ".join(ctx.args[2:]).strip()

    target = get_player(uname)
    if not target:
        await send(update, f"❌ Игрок {mention(uname)} не найден.")
        return

    by = "@" + (update.effective_user.username or str(update.effective_user.id))
    new_elo = set_player_elo(target["id"], new_value, by_user=by, note=note)
    delta = new_elo - target["elo"]
    arrow_str = arrow(int(round(delta)))
    await send(
        update,
        f"📈 ELO {mention(uname)}: {round(target['elo'])} → <b>{round(new_elo)}</b> ({arrow_str})\n"
        f"Установил(а): {by}{(' — ' + note) if note else ''}",
    )
    if target.get("telegram_id"):
        try:
            await ctx.bot.send_message(
                target["telegram_id"],
                f"⚖️ Админ задал твой ELO: <b>{round(new_elo)}</b> ({arrow_str})"
                + (f"\nПричина: {note}" if note else ""),
                parse_mode="HTML",
            )
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────────────
# /grant_admin  /revoke_admin  /admins  — runtime bot-admin promotion
# ─────────────────────────────────────────────────────────────────────────────

async def _resolve_admin_target(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Resolve the target user from /grant_admin or /revoke_admin args.

    Accepts: a numeric Telegram ID, an @username, or a reply to that user's
    message. Returns ``(telegram_id, label)`` or ``(None, error_text)``.
    """
    msg = update.effective_message

    if msg and msg.reply_to_message and msg.reply_to_message.from_user:
        u = msg.reply_to_message.from_user
        label = f"@{u.username}" if u.username else f"id {u.id}"
        return u.id, label

    if not ctx.args:
        return None, (
            "Использование: <code>/grant_admin @user</code>, "
            "<code>/grant_admin 123456789</code>, или ответом на сообщение "
            "пользователя."
        )

    arg = ctx.args[0].lstrip("@").strip()
    if arg.isdigit():
        tid = int(arg)
        return tid, f"id {tid}"

    target = get_player(arg.lower())
    if not target:
        return None, (
            f"❌ Игрок @{arg} не найден в базе. "
            f"Попроси его сначала зарегистрироваться <code>/register</code>, "
            f"или укажи числовой Telegram ID: <code>/grant_admin &lt;id&gt;</code>."
        )
    if not target.get("telegram_id"):
        return None, (
            f"❌ У @{arg} ещё нет привязанного Telegram ID. "
            f"Попроси его написать боту в личку любую команду — после этого "
            f"telegram_id привяжется."
        )
    return int(target["telegram_id"]), f"@{arg}"


async def cmd_grant_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Promote a Telegram user to bot admin (runtime grant)."""
    if not is_admin(update.effective_user.id):
        await send(update, "❌ Только текущий админ может выдавать админку.")
        return

    target_id, label = await _resolve_admin_target(update, ctx)
    if target_id is None:
        await send(update, label, parse_mode="HTML")
        return

    if target_id in ADMIN_IDS:
        await send(
            update,
            f"ℹ️ {label} уже root-админ (через ADMIN_IDS env). Дополнительная "
            f"запись в БД не требуется.",
        )
        return

    note = " ".join(ctx.args[1:]).strip() or None
    grant_bot_admin(target_id, granted_by=update.effective_user.id, note=note)
    await send(
        update,
        f"✅ {label} теперь админ бота. Может создавать официальные турниры, "
        f"редактировать ELO, апрувить матчи и всё остальное."
        + (f"\nКомментарий: {note}" if note else ""),
    )
    try:
        await ctx.bot.send_message(
            target_id,
            "🎉 Тебе выдали админку бота. Теперь ты можешь создавать "
            "официальные турниры через /create_tournament, апрувить матчи "
            "и пользоваться всеми админ-командами. /help",
        )
    except Exception:
        pass


async def cmd_revoke_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Revoke a previously-granted runtime admin."""
    if not is_admin(update.effective_user.id):
        await send(update, "❌ Только админ может отзывать админку.")
        return

    target_id, label = await _resolve_admin_target(update, ctx)
    if target_id is None:
        await send(update, label, parse_mode="HTML")
        return

    if target_id in ADMIN_IDS:
        await send(
            update,
            f"❌ {label} — root-админ (ADMIN_IDS env). Снять только удалением "
            f"из переменной окружения и рестартом бота.",
        )
        return

    if revoke_bot_admin(target_id):
        await send(update, f"🔓 Админка снята: {label}")
        try:
            await ctx.bot.send_message(target_id, "ℹ️ С тебя сняли админку бота.")
        except Exception:
            pass
    else:
        await send(update, f"ℹ️ {label} и так не был админом.")


async def cmd_admins(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Show the current list of bot admins (root + runtime)."""
    lines: list[str] = []
    lines.append("👮 <b>Админы бота</b>\n")
    if ADMIN_IDS:
        lines.append("<b>Root</b> (через ADMIN_IDS env, снять нельзя):")
        for aid in ADMIN_IDS:
            lines.append(f"  • <code>{aid}</code>")
    else:
        lines.append("<i>Root-админы не настроены (env ADMIN_IDS пустой).</i>")
    runtime = list_bot_admins()
    if runtime:
        lines.append("\n<b>Runtime</b> (выданы через /grant_admin):")
        for r in runtime:
            tail = ""
            if r.get("note"):
                tail = f" — {r['note']}"
            lines.append(f"  • <code>{r['telegram_id']}</code>{tail}")
    else:
        lines.append("\n<i>Runtime-админов нет.</i>")
    lines.append(
        "\nДобавить: <code>/grant_admin @user</code> или ответом на сообщение."
        "\nАдмины конкретного турнира — <code>/tadmins [ID]</code>."
    )
    await send(update, "\n".join(lines))


# ─────────────────────────────────────────────────────────────────────────────
# /add_tadmin  /remove_tadmin  /tadmins  — per-tournament admin delegation
# ─────────────────────────────────────────────────────────────────────────────

def _resolve_tadmin_target(
    update: Update, ctx: ContextTypes.DEFAULT_TYPE,
    *, args: list[str],
) -> tuple[int | None, str]:
    """Resolve the per-tournament-admin target.

    Same shape as ``_resolve_admin_target`` but works on a positional args
    slice so ``/add_tadmin`` can take an optional tournament ID first.
    Returns ``(telegram_id, label)`` or ``(None, error_text)``.
    """
    msg = update.effective_message
    if msg and msg.reply_to_message and msg.reply_to_message.from_user:
        u = msg.reply_to_message.from_user
        label = f"@{u.username}" if u.username else f"id {u.id}"
        return u.id, label

    if not args:
        return None, (
            "Не указан пользователь. Используй "
            "<code>@username</code>, числовой Telegram ID, или ответь на "
            "сообщение нужного пользователя."
        )
    raw_arg = args[0].strip()
    had_at = raw_arg.startswith("@")
    arg = raw_arg.lstrip("@").strip()
    # If prefixed with @, prioritise username lookup even for all-digit names
    if had_at:
        target = get_player(arg.lower())
        if target and target.get("telegram_id"):
            return int(target["telegram_id"]), f"@{target['username']}"
    if arg.isdigit():
        tid = int(arg)
        return tid, f"id {tid}"
    target = get_player(arg.lower())
    if not target or not target.get("telegram_id"):
        return None, (
            f"❌ Не нашёл пользователя <code>{html.escape(arg)}</code>. "
            f"Он должен быть зарегистрирован в боте, или укажи числовой "
            f"Telegram ID."
        )
    return int(target["telegram_id"]), f"@{target['username']}"


def _split_tadmin_args(
    update: Update, ctx: ContextTypes.DEFAULT_TYPE,
) -> tuple[dict | None, list[str], str | None]:
    """Pull an optional leading numeric tournament ID off ``ctx.args``.

    Resolve it to a tournament dict. If no leading numeric arg is present,
    fall back to the active tournament binding. Returns ``(tournament,
    remaining_args, err_text)`` — exactly one of ``tournament``/``err`` set.
    """
    args = list(ctx.args or [])
    if args and args[0].isdigit():
        tid = int(args[0])
        t = get_tournament(tid)
        if not t:
            return None, args, f"❌ Турнир с ID {tid} не найден."
        return t, args[1:], None

    t, err = _resolve_tournament_from_args(update, ctx, args=args)
    if t is None:
        return None, args, err or "❌ Нет активного турнира."
    return t, args, None


async def cmd_add_tadmin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """``/add_tadmin [ID] @user [note]`` — назначить админа турнира.

    Доступно создателю турнира и root-админу. Назначенный админ получает
    те же права на этот конкретный турнир, что и создатель.
    """
    t, args, err = _split_tadmin_args(update, ctx)
    if t is None:
        await send(update, err or "❌ Нет активного турнира.")
        return

    user_id = update.effective_user.id
    creator = get_player_by_id(t["created_by"]) if t.get("created_by") else None
    is_creator = bool(creator and creator.get("telegram_id") == user_id)
    if not (is_root_admin(user_id) or is_creator):
        await send(
            update,
            "❌ Назначать админов турнира может только создатель турнира "
            "или главный админ бота.",
        )
        return

    target_id, label = _resolve_tadmin_target(update, ctx, args=args)
    if target_id is None:
        await send(update, label, parse_mode="HTML")
        return

    if creator and creator.get("telegram_id") == target_id:
        await send(
            update,
            f"ℹ️ {label} — создатель турнира, у него уже есть полные права.",
        )
        return
    if is_root_admin(target_id):
        await send(
            update,
            f"ℹ️ {label} — главный админ бота, у него уже есть полные права.",
        )
        return

    note = " ".join(args[1:]).strip() or None
    if note and note.startswith("@"):
        note = " ".join(args[2:]).strip() or None
    add_tournament_admin(t["id"], target_id, granted_by=user_id, note=note)
    log_tournament_action(
        t["id"],
        actor_telegram_id=user_id,
        actor_username=update.effective_user.username,
        action="add_tadmin",
        details=f"target={label}" + (f" note={note}" if note else ""),
    )

    label_safe = html.escape(label)
    t_label = f"<b>{html.escape(t['name'])}</b> [{t_full_label(t)}]"
    await send(
        update,
        f"✅ {label_safe} теперь админ турнира {t_label}. "
        f"Может управлять составом, продвигать стадии и подтверждать матчи "
        f"в этом турнире."
        + (f"\nКомментарий: {html.escape(note)}" if note else ""),
    )
    try:
        await ctx.bot.send_message(
            target_id,
            f"🎉 Тебя сделали админом турнира "
            f"«{html.escape(t['name'])}» [{t_full_label(t)}]. "
            f"Теперь ты можешь добавлять/убирать игроков, подтверждать "
            f"матчи и продвигать стадии — в рамках этого турнира.",
            parse_mode="HTML",
        )
    except Exception:
        pass


async def cmd_remove_tadmin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """``/remove_tadmin [ID] @user`` — снять админа с турнира.

    Доступно создателю турнира и root-админу.
    """
    t, args, err = _split_tadmin_args(update, ctx)
    if t is None:
        await send(update, err or "❌ Нет активного турнира.")
        return

    user_id = update.effective_user.id
    creator = get_player_by_id(t["created_by"]) if t.get("created_by") else None
    is_creator = bool(creator and creator.get("telegram_id") == user_id)
    if not (is_root_admin(user_id) or is_creator):
        await send(
            update,
            "❌ Снимать админов турнира может только создатель турнира "
            "или главный админ бота.",
        )
        return

    target_id, label = _resolve_tadmin_target(update, ctx, args=args)
    if target_id is None:
        await send(update, label, parse_mode="HTML")
        return

    if remove_tournament_admin(t["id"], target_id):
        log_tournament_action(
            t["id"],
            actor_telegram_id=user_id,
            actor_username=update.effective_user.username,
            action="remove_tadmin",
            details=f"target={label}",
        )
        t_label = f"<b>{html.escape(t['name'])}</b> [{t_full_label(t)}]"
        await send(
            update,
            f"🔓 Снял {html.escape(label)} с админов турнира {t_label}.",
        )
        try:
            await ctx.bot.send_message(
                target_id,
                f"ℹ️ С тебя сняли админку турнира "
                f"«{html.escape(t['name'])}» [{t_full_label(t)}].",
                parse_mode="HTML",
            )
        except Exception:
            pass
    else:
        await send(
            update,
            f"ℹ️ {html.escape(label)} и так не был админом этого турнира.",
        )


async def cmd_tadmins(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """``/tadmins [ID]`` — посмотреть админов турнира."""
    t, _args, err = _split_tadmin_args(update, ctx)
    if t is None:
        await send(update, err or "❌ Нет активного турнира.")
        return

    creator = get_player_by_id(t["created_by"]) if t.get("created_by") else None
    delegated = list_tournament_admins(t["id"])

    lines = [
        f"👮 <b>Админы турнира</b> "
        f"<b>{html.escape(t['name'])}</b> [{t_full_label(t)}]\n"
    ]
    if creator:
        creator_label = f"@{creator['username']}"
        if creator.get("telegram_id"):
            creator_label += f" (id {creator['telegram_id']})"
        lines.append(f"<b>Создатель:</b> {creator_label}")
    else:
        lines.append("<i>Создатель не указан.</i>")

    if delegated:
        lines.append("\n<b>Делегированные админы</b> (выданы через /add_tadmin):")
        for r in delegated:
            tail = ""
            if r.get("note"):
                tail = f" — {html.escape(r['note'])}"
            lines.append(f"  • <code>{r['telegram_id']}</code>{tail}")
    else:
        lines.append("\n<i>Делегированных админов нет.</i>")

    if ADMIN_IDS:
        lines.append(
            "\n<i>Главный админ бота (root, ADMIN_IDS) тоже может управлять "
            "любым турниром.</i>"
        )
    lines.append(
        "\nНазначить: <code>/add_tadmin [ID] @user</code>; "
        "снять: <code>/remove_tadmin [ID] @user</code>."
    )
    await send(update, "\n".join(lines))


# ─────────────────────────────────────────────────────────────────────────────
# /broadcast  — DM all participants of a tournament
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_broadcast(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """``/broadcast [ID] <текст>`` — рассылка всем участникам турнира.

    Текст уходит в личку каждому участнику с активным статусом.
    Доступно создателю турнира / root / делегированным админам.
    """
    t, args, err = _split_tadmin_args(update, ctx)
    if t is None:
        await send(update, err or "❌ Нет активного турнира.")
        return
    user_id = update.effective_user.id
    if not _can_manage_tournament(user_id, t):
        await send(
            update,
            "❌ Рассылку могут делать только админы этого турнира.",
        )
        return
    text = " ".join(args).strip()
    msg = update.effective_message
    if not text and msg and msg.reply_to_message and msg.reply_to_message.text:
        text = msg.reply_to_message.text
    if not text:
        await send(
            update,
            "Использование: <code>/broadcast [ID] &lt;текст&gt;</code>"
            "\nИли ответь этой командой на сообщение, которое нужно "
            "разослать.",
        )
        return
    participants = get_tournament_players(int(t["id"]))
    if not participants:
        await send(update, "ℹ️ В турнире нет участников.")
        return

    body = (
        f"📣 <b>Анонс турнира</b> "
        f"<b>{html.escape(t['name'])}</b> [{t_full_label(t)}]\n\n"
        f"{html.escape(text)}"
    )
    # Append footer for broadcast context
    from handlers.common import get_random_footer, FOOTER_CTX_BROADCAST
    _bc_footer = get_random_footer(t, FOOTER_CTX_BROADCAST)
    if _bc_footer:
        body += _bc_footer
    sent = 0
    failed = 0
    for tp in participants:
        pid = tp.get("player_id")
        p = get_player_by_id(pid) if pid else None
        if not p or not p.get("telegram_id"):
            failed += 1
            continue
        try:
            await ctx.bot.send_message(
                int(p["telegram_id"]), body, parse_mode="HTML",
            )
            sent += 1
        except Exception as e:
            log.warning("broadcast to %s failed: %s", p.get("telegram_id"), e)
            failed += 1

    log_tournament_action(
        int(t["id"]),
        actor_telegram_id=user_id,
        actor_username=update.effective_user.username,
        action="broadcast",
        details=f"sent={sent} failed={failed} chars={len(text)}",
    )
    await send(
        update,
        f"📣 Рассылка отправлена: ✅ {sent}, ⚠️ не доставлено {failed}.",
    )


# ─────────────────────────────────────────────────────────────────────────────
# /set_description  /set_channel  /clear_channel  (creator/admin)
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_set_description(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await send(
            update,
            "Использование: <code>/set_description &lt;текст&gt;</code>\n"
            "Можно указать тип в начале: <code>/set_description вса &lt;текст&gt;</code>",
        )
        return

    args = list(ctx.args)
    t_type = parse_tournament_type_arg(args[0])
    if t_type:
        args = args[1:]
    text = " ".join(args).strip()
    if not text:
        await send(update, "❌ Описание пустое.")
        return
    if len(text) > 1000:
        await send(update, "❌ Слишком длинное описание (макс. 1000 символов).")
        return

    t = get_active_tournament(tournament_type=t_type)
    if not t:
        await send(update, "❌ Нет активного турнира.")
        return
    if not _can_manage_tournament(update.effective_user.id, t):
        await send(update, "❌ Описание турнира меняет создатель или админ.")
        return

    conn = db.get_conn()
    conn.execute("UPDATE tournaments SET description=? WHERE id=?", (text, t["id"]))
    conn.commit()
    conn.close()
    log_tournament_action(
        t["id"],
        actor_telegram_id=update.effective_user.id,
        actor_username=update.effective_user.username,
        action="set_description",
        details=f"len={len(text)}",
    )
    await send(
        update,
        f"📝 Описание турнира <b>{t['name']}</b> [{t_type_label(t['tournament_type'])}] обновлено:\n\n{text}",
    )


async def cmd_set_channel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await send(
            update,
            "Использование: <code>/set_channel @my_channel</code>\n"
            "Или числовой ID: <code>/set_channel -1001234567890</code>\n"
            "<i>Бот должен быть участником канала, чтобы проверять подписки.</i>",
        )
        return

    args = list(ctx.args)
    t_type = parse_tournament_type_arg(args[0])
    if t_type:
        args = args[1:]
    if not args:
        await send(update, "❌ Укажи канал.")
        return
    channel = args[0]
    if not (channel.startswith("@") or channel.startswith("-100") or channel.lstrip("-").isdigit()):
        channel = "@" + channel.lstrip("@")

    t = get_active_tournament(tournament_type=t_type)
    if not t:
        await send(update, "❌ Нет активного турнира.")
        return
    if not _can_manage_tournament(update.effective_user.id, t):
        await send(update, "❌ Только создатель или админ.")
        return

    # Best-effort sanity check: try to fetch the chat as the bot.
    try:
        chat = await ctx.bot.get_chat(channel)
        channel_disp = f"@{chat.username}" if chat.username else str(chat.id)
    except TelegramError as e:
        await send(
            update,
            f"⚠️ Не смог получить канал {channel}: {e}\n"
            f"Канал всё равно записан, но проверка подписок не будет работать "
            f"пока бота не добавят в канал.",
        )
        channel_disp = channel

    conn = db.get_conn()
    conn.execute(
        "UPDATE tournaments SET required_channel=? WHERE id=?",
        (channel, t["id"]),
    )
    conn.commit()
    conn.close()
    log_tournament_action(
        t["id"],
        actor_telegram_id=update.effective_user.id,
        actor_username=update.effective_user.username,
        action="set_channel",
        details=f"channel={channel}",
    )
    await send(
        update,
        f"🔗 Турнир <b>{t['name']}</b> [{t_type_label(t['tournament_type'])}]: "
        f"теперь требует подписку на <b>{channel_disp}</b>.\n"
        f"Чтобы это работало — добавь бота в канал (можно без админ-прав).",
    )


async def cmd_clear_channel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    t_type = parse_tournament_type_arg(ctx.args[0]) if ctx.args else None
    t = get_active_tournament(tournament_type=t_type)
    if not t:
        await send(update, "❌ Нет активного турнира.")
        return
    if not _can_manage_tournament(update.effective_user.id, t):
        await send(update, "❌ Только создатель или админ.")
        return
    conn = db.get_conn()
    conn.execute(
        "UPDATE tournaments SET required_channel=NULL WHERE id=?", (t["id"],)
    )
    conn.commit()
    conn.close()
    log_tournament_action(
        t["id"],
        actor_telegram_id=update.effective_user.id,
        actor_username=update.effective_user.username,
        action="clear_channel",
    )
    await send(
        update,
        f"✅ Снято условие подписки для турнира <b>{t['name']}</b>.",
    )


# ─────────────────────────────────────────────────────────────────────────────
# /set_tournament_bg  /clear_tournament_bg  — custom PNG background (v13)
# ─────────────────────────────────────────────────────────────────────────────

# Module path used in tests, kept absolute so it works regardless of CWD.
TOURNAMENT_BG_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..",
    "tournament_bg",
)
TOURNAMENT_BG_DIR = os.path.normpath(TOURNAMENT_BG_DIR)


def _tournament_bg_path(tid: int) -> str:
    """Disk path where this tournament's background image lives."""
    return os.path.join(TOURNAMENT_BG_DIR, f"{tid}.jpg")


async def cmd_set_tournament_bg(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """``/set_tournament_bg [ID]`` — set a custom PNG background.

    Two ways to send the photo:
      1. Reply to a photo message with ``/set_tournament_bg`` (optionally
         followed by a tournament ID).
      2. Send a photo with ``/set_tournament_bg`` as the caption (again,
         optionally followed by a tournament ID).
    Permissions: creator, delegated tournament-admin, or root.

    Stores both the on-disk file (hot cache for renderer) and a base64
    copy in the DB (``tournaments.bg_image_data``) so the background
    survives Railway/Heroku/Docker redeploys.
    """
    msg = update.effective_message
    photo_msg = (
        msg.reply_to_message if (msg and msg.reply_to_message and msg.reply_to_message.photo)
        else msg
    )
    photos = (photo_msg.photo if photo_msg else None) or []
    if not photos:
        await send(
            update,
            "📸 Пришли фото с подписью <code>/set_tournament_bg [ID]</code> "
            "или ответь этой командой на любое фото в чате.",
        )
        return

    t, err = _resolve_tournament_from_args(update, ctx)
    if not t:
        await send(update, err or "❌ Не нашёл турнир.")
        return
    if not _can_manage_tournament(update.effective_user.id, t):
        await send(update, "❌ Менять фон может только создатель или админ турнира.")
        return

    file = await ctx.bot.get_file(photos[-1].file_id)
    os.makedirs(TOURNAMENT_BG_DIR, exist_ok=True)
    dst = _tournament_bg_path(t["id"])
    try:
        await file.download_to_drive(dst)
    except Exception as exc:
        log.exception("download tournament bg failed")
        await send(update, f"❌ Не удалось скачать фото: {html.escape(str(exc))}")
        return

    # Persist the bytes in the DB too so the bg survives container
    # redeploys (Railway / Heroku / Docker rebuild the FS clean each push).
    try:
        with open(dst, "rb") as fh:
            bg_b64 = base64.b64encode(fh.read()).decode("ascii")
    except Exception as exc:
        log.warning("read freshly-downloaded bg failed: %s", exc)
        bg_b64 = None

    db.update_tournament(t["id"], bg_image_path=dst, bg_image_data=bg_b64)
    log_tournament_action(
        t["id"],
        actor_telegram_id=update.effective_user.id,
        actor_username=update.effective_user.username,
        action="set_tournament_bg",
    )
    await send(
        update,
        f"✅ Фон для турнира <b>{html.escape(t['name'])}</b> сохранён. "
        f"Теперь /standings и /playoff будут на этом фоне.",
    )


async def cmd_clear_tournament_bg(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """``/clear_tournament_bg [ID]`` — вернуть стандартный фон."""
    t, err = _resolve_tournament_from_args(update, ctx)
    if not t:
        await send(update, err or "❌ Не нашёл турнир.")
        return
    if not _can_manage_tournament(update.effective_user.id, t):
        await send(update, "❌ Только создатель или админ.")
        return
    db.update_tournament(t["id"], bg_image_path=None, bg_image_data=None)
    try:
        os.unlink(_tournament_bg_path(t["id"]))
    except OSError:
        pass
    log_tournament_action(
        t["id"],
        actor_telegram_id=update.effective_user.id,
        actor_username=update.effective_user.username,
        action="clear_tournament_bg",
    )
    await send(
        update,
        f"✅ Фон сброшен. /standings и /playoff турнира "
        f"<b>{html.escape(t['name'])}</b> снова на стандартном фоне.",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Goal point-edits — /admin_matchgoals, /admin_addgoal, /admin_delgoal,
# /admin_setgoalauthor.
#
# These commands let an admin surgically edit a single goal event in
# `match_goals` without touching the match score / status. They're the
# precision counterpart to `/edit_goals`, which always replaces the
# full goal list.
#
# After any change, `/tablebomb` recomputes from `match_goals` on the
# fly — there's nothing to invalidate.
# ─────────────────────────────────────────────────────────────────────────────

def _parse_match_id_token(tok: str) -> int | None:
    """Accept ``142`` / ``#142`` / ``m142`` / ``M142`` and return 142."""
    if not tok:
        return None
    s = tok.strip().lstrip("#").lstrip("mM").strip()
    return int(s) if s.isdigit() else None


def _parse_goal_id_token(tok: str) -> int | None:
    """Accept ``17`` / ``g17`` / ``#17`` and return 17."""
    if not tok:
        return None
    s = tok.strip().lstrip("#").lstrip("gG").strip()
    return int(s) if s.isdigit() else None


def _side_of_player_in_match(m: dict, pid: int) -> str | None:
    """``'home'`` / ``'away'`` / ``None`` if pid isn't a participant."""
    if pid == m.get("player1_id"):
        return "home"
    if pid == m.get("player2_id"):
        return "away"
    return None


async def cmd_admin_matchgoals(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """``/admin_matchgoals <match_id>`` — список голов матча.

    Без него не разобраться, какой goal_id править через
    /admin_delgoal / /admin_setgoalauthor.
    """
    if not is_admin(update.effective_user.id):
        await send(update, "❌ Только админ.")
        return
    args = ctx.args or []
    if not args:
        await send(
            update,
            "Использование: <code>/admin_matchgoals &lt;match_id&gt;</code>",
        )
        return
    mid = _parse_match_id_token(args[0])
    if mid is None:
        await send(update, f"❌ Не пойму ID матча: {html.escape(args[0])}")
        return
    m = db.get_match(mid)
    if not m:
        await send(update, f"❌ Матч #{mid} не найден.")
        return
    p1 = get_player_by_id(m["player1_id"])
    p2 = get_player_by_id(m["player2_id"])
    p1n = mention(p1["username"]) if p1 else "—"
    p2n = mention(p2["username"]) if p2 else "—"
    score = f"{m.get('score1', '?')}:{m.get('score2', '?')}"

    goals = db.get_match_goals(mid)
    lines = [
        f"⚽ <b>Голы матча #{mid}</b>",
        f"{p1n} 🟢 vs 🔵 {p2n}  ({score}, {m.get('status', '?')})",
        "",
    ]
    if not goals:
        lines.append("<i>Голов не записано.</i>")
    else:
        for g in goals:
            side = (g.get("side") or "").lower()
            tag = "🟢" if side == "home" else "🔵" if side == "away" else "⚪"
            league_p = (
                get_player_by_id(g["player_id"])
                if g.get("player_id") else None
            )
            league_str = (
                mention(league_p["username"]) if league_p else "—"
            )
            raw = g.get("raw_name") or "—"
            minute = g.get("minute")
            min_str = f"{minute}'" if minute is not None else "—"
            lines.append(
                f"#{g['id']} {tag} {min_str:>4}  "
                f"<i>{html.escape(str(raw))}</i> → {league_str}"
            )
    lines.append("")
    lines.append(
        "Изменить: <code>/admin_addgoal &lt;match_id&gt; "
        "[@user|home|away] [home|away] [мин] [name:&lt;имя&gt;]</code>, "
        "<code>/admin_delgoal &lt;goal_id&gt;</code>, "
        "<code>/admin_setgoalauthor &lt;goal_id&gt; @user</code>, "
        "<code>/admin_setgoalname &lt;goal_id&gt; &lt;имя&gt;</code>."
    )
    await send(update, "\n".join(lines))


async def cmd_admin_addgoal(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """``/admin_addgoal <match_id> [@user|home|away] [home|away] [минута] [name:<имя>]``.

    Три режима записи гола:

    1. **С участником матча** (старый сценарий):
       ``/admin_addgoal 481 @vasya 12``
       Сторона выводится из позиции игрока в матче (player1 → home,
       player2 → away). ``raw_name`` = ``game_nickname`` игрока (или
       его username, если ник не задан).

    2. **С участником и кастомным именем футболиста**:
       ``/admin_addgoal 481 @vasya 12 name:Mbappe``
       То же, что (1), но ``raw_name`` берётся из ``name:`` —
       полезно, когда у игрока в составе несколько футболистов.

    3. **Без юзера, только сторона + имя** — для матчей, занесённых
       просто счётом (без OCR-скрина):
       ``/admin_addgoal 481 home name:Mbappe 12``
       ``player_id`` сохраняется как ``NULL``. ``home``/``away`` и
       ``name:<имя>`` обязательны. В ``/tablebomb`` гол всё равно
       попадёт под этим именем, т.к. таблица бомбардиров считает
       по ``raw_name`` + стороне матча.

    Всё после ``name:`` до конца строки считается именем
    футболиста — поддерживаются пробелы (``name:Cristiano Ronaldo``).
    """
    if not is_admin(update.effective_user.id):
        await send(update, "❌ Только админ.")
        return
    args = list(ctx.args or [])

    # Reply-to-message resolves the player when no @user is given.
    msg = update.effective_message
    reply_target = (
        msg.reply_to_message.from_user
        if msg and msg.reply_to_message and msg.reply_to_message.from_user
        else None
    )

    usage = (
        "Использование: <code>/admin_addgoal &lt;match_id&gt; "
        "[@user|home|away] [home|away] [минута] [name:&lt;имя&gt;]</code>\n"
        "• Со ссылкой на игрока: <code>/admin_addgoal 481 @vasya 12</code>\n"
        "• С кастомным именем: <code>/admin_addgoal 481 @vasya 12 name:Mbappe</code>\n"
        "• Без юзера (матч занесён счётом): "
        "<code>/admin_addgoal 481 home name:Mbappe 12</code>\n"
        "Можно также ответом на сообщение игрока вместо @user."
    )

    if not args:
        await send(update, usage)
        return

    mid = _parse_match_id_token(args[0])
    if mid is None:
        await send(update, f"❌ Не пойму ID матча: {html.escape(args[0])}")
        return
    m = db.get_match(mid)
    if not m:
        await send(update, f"❌ Матч #{mid} не найден.")
        return
    p1 = get_player_by_id(m["player1_id"])
    p2 = get_player_by_id(m["player2_id"])

    rest = args[1:]

    # ── Step 1: extract `name:<...>` first — everything after that
    # token (incl. the suffix in the token itself) is the footballer
    # name, joined with spaces. This must happen BEFORE positional
    # parsing so multi-word names don't trip the keyword scanner.
    raw_name_override: str | None = None
    for i, tok in enumerate(rest):
        if tok.lower().startswith("name:"):
            head = tok[len("name:"):]
            tail = rest[i + 1:]
            parts = ([head] if head else []) + list(tail)
            raw_name_override = " ".join(parts).strip() or None
            rest = rest[:i]
            break
    if raw_name_override is not None and len(raw_name_override) > 64:
        await send(update, "❌ Слишком длинное имя футболиста (макс. 64 символа).")
        return

    # ── Step 2: optional player ref. If the first remaining token
    # isn't a side keyword and looks like a player ref, consume it.
    side_keywords = {
        "home", "h", "дома", "д",
        "away", "a", "гости", "г",
    }
    target: dict | None = None
    if rest and rest[0].lower() not in side_keywords and not rest[0].isdigit():
        tok = rest[0]
        target = _resolve_player_arg(tok)
        if not target:
            await send(update, f"❌ Игрок не найден: {html.escape(tok)}")
            return
        rest = rest[1:]
    elif rest and rest[0].isdigit() and not reply_target:
        # Pure-numeric first token can be either a telegram-id or a
        # minute. Disambiguate: if it resolves to a registered player,
        # treat as player ref; otherwise leave it for the minute slot.
        cand = _resolve_player_arg(rest[0])
        if cand:
            target = cand
            rest = rest[1:]
    elif reply_target:
        from handlers._helpers import _player_from_user
        target = _player_from_user(reply_target)
        if not target:
            await send(
                update,
                "❌ Адресат reply-а не зарегистрирован в боте.",
            )
            return

    # ── Step 3: parse side / minute from the remaining tokens.
    side: str | None = None
    minute: int | None = None
    for tok in rest:
        s = tok.strip().lower()
        if s in ("home", "h", "дома", "д"):
            side = "home"
        elif s in ("away", "a", "гости", "г"):
            side = "away"
        elif s.isdigit():
            minute = int(s)
        else:
            await send(update, f"❌ Не понял аргумент: {html.escape(tok)}")
            return

    # ── Step 4: derive side / raw_name based on whether we have a
    # target player or not.
    sanity = ""
    if target is not None:
        auto_side = _side_of_player_in_match(m, target["id"])
        if side is None:
            side = auto_side
        if side is None:
            # Player not in match and no manual side given.
            p1n = mention(p1["username"]) if p1 else "—"
            p2n = mention(p2["username"]) if p2 else "—"
            await send(
                update,
                f"❌ {mention(target['username'])} не участвует в матче "
                f"#{mid} ({p1n} vs {p2n}). Участникам сторона ставится "
                "автоматически; для голов «в свои» добавь "
                "<code>home</code>/<code>away</code> явно.",
            )
            return
        # Sanity warning if admin's manual side disagrees with the
        # player's actual seat (allowed — own-goals exist — but worth
        # flagging).
        if auto_side and side != auto_side:
            sanity = (
                f"\n⚠️ Указана сторона <b>{side}</b>, но "
                f"{mention(target['username'])} играет с другой стороны "
                f"(<b>{auto_side}</b>) — учту как автогол / спецслучай."
            )
        raw_name = (
            raw_name_override
            or target.get("game_nickname")
            or target.get("username")
        )
        player_id_for_db: int | None = int(target["id"])
    else:
        # No player reference at all — bare-side mode for matches that
        # were entered by score only.
        if side is None:
            await send(
                update,
                "❌ Без указания игрока нужны явные сторона и имя:\n"
                "<code>/admin_addgoal &lt;match_id&gt; home|away "
                "name:&lt;имя&gt; [минута]</code>\n"
                "Либо укажи участника матча: "
                "<code>@user</code> / telegram-id / reply.",
            )
            return
        if not raw_name_override:
            await send(
                update,
                "❌ Без <code>@user</code> нужно явно задать имя "
                "футболиста через <code>name:&lt;имя&gt;</code>. Без "
                "имени гол не попадёт в <code>/tablebomb</code>.",
            )
            return
        raw_name = raw_name_override
        player_id_for_db = None

    gid = db.add_match_goal(
        match_id=mid,
        player_id=player_id_for_db,
        raw_name=raw_name,
        minute=minute,
        side=side,
    )
    log_tournament_action(
        m.get("tournament_id"),
        actor_telegram_id=update.effective_user.id,
        actor_username=update.effective_user.username,
        action="admin_addgoal",
        details=(
            f"match=#{mid} goal=#{gid} "
            f"player_id={player_id_for_db if player_id_for_db is not None else 'NULL'} "
            f"side={side} raw_name={raw_name!r}"
        ),
    )
    side_tag = "🟢" if side == "home" else "🔵"
    min_str = f"{minute}'" if minute is not None else "—"
    if target is not None:
        who = (
            f"{mention(target['username'])} "
            f"<i>({html.escape(str(raw_name))})</i>"
        )
    else:
        who = f"<i>{html.escape(str(raw_name))}</i> (без юзера)"
    await send(
        update,
        f"✅ Записан гол #{gid} в матче #{mid}: "
        f"{side_tag} {who}, минута {min_str}."
        + sanity,
    )


async def cmd_admin_delgoal(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """``/admin_delgoal <goal_id>`` — удалить один гол по id."""
    if not is_admin(update.effective_user.id):
        await send(update, "❌ Только админ.")
        return
    args = ctx.args or []
    if not args:
        await send(
            update,
            "Использование: <code>/admin_delgoal &lt;goal_id&gt;</code>\n"
            "ID гола можно посмотреть в <code>/admin_matchgoals &lt;match_id&gt;</code>.",
        )
        return
    gid = _parse_goal_id_token(args[0])
    if gid is None:
        await send(update, f"❌ Не пойму ID гола: {html.escape(args[0])}")
        return
    g = db.get_match_goal(gid)
    if not g:
        await send(update, f"❌ Гол #{gid} не найден.")
        return
    ok = db.delete_match_goal(gid)
    if not ok:
        await send(update, f"❌ Не получилось удалить гол #{gid}.")
        return
    log_tournament_action(
        g.get("tournament_id"),
        actor_telegram_id=update.effective_user.id,
        actor_username=update.effective_user.username,
        action="admin_delgoal",
        details=f"match=#{g.get('match_id')} goal=#{gid} side={g.get('side')}",
    )
    await send(
        update,
        f"🗑 Гол #{gid} (матч #{g.get('match_id')}, "
        f"side={g.get('side') or '—'}) удалён.",
    )


async def cmd_admin_setgoalauthor(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """``/admin_setgoalauthor <goal_id> <@user|id>`` — переназначить автора.

    Сторона (home/away) пересчитывается автоматически из позиции
    нового автора в матче. Если новый автор не участвует в матче —
    отказ с подсказкой.
    """
    if not is_admin(update.effective_user.id):
        await send(update, "❌ Только админ.")
        return
    args = list(ctx.args or [])
    msg = update.effective_message
    reply_target = (
        msg.reply_to_message.from_user
        if msg and msg.reply_to_message and msg.reply_to_message.from_user
        else None
    )

    if len(args) < 1 or (len(args) < 2 and not reply_target):
        await send(
            update,
            "Использование: <code>/admin_setgoalauthor &lt;goal_id&gt; "
            "&lt;@user|id&gt;</code>\n"
            "Можно ответом на сообщение игрока (тогда второй аргумент не нужен).",
        )
        return

    gid = _parse_goal_id_token(args[0])
    if gid is None:
        await send(update, f"❌ Не пойму ID гола: {html.escape(args[0])}")
        return
    g = db.get_match_goal(gid)
    if not g:
        await send(update, f"❌ Гол #{gid} не найден.")
        return
    m = db.get_match(int(g["match_id"]))
    if not m:
        await send(update, "❌ Матч этого гола не найден в БД.")
        return
    p1 = get_player_by_id(m["player1_id"])
    p2 = get_player_by_id(m["player2_id"])

    if len(args) >= 2:
        target = _resolve_player_arg(args[1])
        if not target:
            await send(update, f"❌ Игрок не найден: {html.escape(args[1])}")
            return
    else:
        from handlers._helpers import _player_from_user
        target = _player_from_user(reply_target)
        if not target:
            await send(
                update,
                "❌ Адресат reply-а не зарегистрирован в боте.",
            )
            return

    new_side = _side_of_player_in_match(m, int(target["id"]))
    if new_side is None:
        p1n = mention(p1["username"]) if p1 else "—"
        p2n = mention(p2["username"]) if p2 else "—"
        await send(
            update,
            f"❌ {mention(target['username'])} не участвует в матче "
            f"#{m['id']} ({p1n} vs {p2n}). Сначала перепиши состав или "
            "используй <code>/edit_goals</code> для свободной правки.",
        )
        return

    # Don't overwrite raw_name — it's the footballer name from OCR (e.g. "Pirlo"),
    # not the player's username/nickname. Only player_id and side change.
    ok = db.update_match_goal_author(
        goal_id=gid,
        player_id=int(target["id"]),
        side=new_side,
    )
    if not ok:
        await send(update, f"❌ Не получилось обновить гол #{gid}.")
        return
    log_tournament_action(
        m.get("tournament_id"),
        actor_telegram_id=update.effective_user.id,
        actor_username=update.effective_user.username,
        action="admin_setgoalauthor",
        details=(
            f"match=#{m['id']} goal=#{gid} new_player_id={target['id']} "
            f"side={new_side}"
        ),
    )
    side_tag = "🟢" if new_side == "home" else "🔵"
    await send(
        update,
        f"✏️ Гол #{gid} (матч #{m['id']}) переназначен на "
        f"{side_tag} {mention(target['username'])}.",
    )


# ─────────────────────────────────────────────────────────────────────────────
# /admin_setgoalname — rename the footballer (raw_name) on a goal
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_admin_setgoalname(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """``/admin_setgoalname <goal_id> <имя_футболиста>`` — изменить имя футболиста.

    Меняет только поле ``raw_name`` (имя, считанное OCR-ом) — автор
    (player_id) и сторона (side) остаются прежними.

    Пример:
        /admin_setgoalname 324 Pirlo
    """
    if not is_admin(update.effective_user.id):
        await send(update, "❌ Только админ.")
        return
    args = list(ctx.args or [])
    if len(args) < 2:
        await send(
            update,
            "Использование: <code>/admin_setgoalname &lt;goal_id&gt; "
            "&lt;имя футболиста&gt;</code>\n"
            "Пример: <code>/admin_setgoalname 324 Pirlo</code>",
        )
        return

    gid = _parse_goal_id_token(args[0])
    if gid is None:
        await send(update, f"❌ Не пойму ID гола: {html.escape(args[0])}")
        return
    g = db.get_match_goal(gid)
    if not g:
        await send(update, f"❌ Гол #{gid} не найден.")
        return

    new_name = " ".join(args[1:]).strip()
    if not new_name:
        await send(update, "❌ Укажи имя футболиста.")
        return
    if len(new_name) > 64:
        await send(update, "❌ Слишком длинное имя (макс. 64 символа).")
        return

    old_name = g.get("raw_name") or "—"
    ok = db.update_match_goal_author(
        goal_id=gid,
        player_id=g.get("player_id"),
        raw_name=new_name,
    )
    if not ok:
        await send(update, f"❌ Не получилось обновить гол #{gid}.")
        return

    m = db.get_match(int(g["match_id"])) if g.get("match_id") else None
    log_tournament_action(
        (m or {}).get("tournament_id"),
        actor_telegram_id=update.effective_user.id,
        actor_username=update.effective_user.username,
        action="admin_setgoalname",
        details=f"goal=#{gid} old={old_name} new={new_name}",
    )
    await send(
        update,
        f"✏️ Гол #{gid} (матч #{g.get('match_id', '?')}): "
        f"имя изменено <b>{html.escape(old_name)}</b> → "
        f"<b>{html.escape(new_name)}</b>.",
    )


# ─────────────────────────────────────────────────────────────────────────────
# /admin_addplayer_late — drop a player into a running tournament
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_admin_addplayer_late(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """``/admin_addplayer_late <tournament_id> <@user|id> [группа]``.

    Добавляет игрока в указанную (или единственную) группу уже
    идущего турнира. Для каждого участника группы, с которым у
    новичка ещё нет матча, создаётся ``matches(stage='group',
    status='pending', deadline=NOW+48h)``. ``group_matches_per_pair``
    турнира соблюдается.

    Турнир должен быть в групповой стадии. ``tournament_elo``
    инициализируется средним по группе.
    """
    from datetime import datetime, timedelta
    from tournament import MATCH_DEADLINE_HOURS

    if not is_admin(update.effective_user.id):
        await send(update, "❌ Только админ.")
        return
    args = list(ctx.args or [])
    msg = update.effective_message
    reply_target = (
        msg.reply_to_message.from_user
        if msg and msg.reply_to_message and msg.reply_to_message.from_user
        else None
    )

    if len(args) < 1 or (len(args) < 2 and not reply_target):
        await send(
            update,
            "Использование: <code>/admin_addplayer_late "
            "&lt;tournament_id&gt; &lt;@user|id&gt; [группа]</code>\n"
            "Можно также ответом на сообщение игрока (тогда @user "
            "не нужен).",
        )
        return

    if not args[0].isdigit():
        await send(
            update, f"❌ Не пойму ID турнира: {html.escape(args[0])}"
        )
        return
    tid = int(args[0])
    t = get_tournament(tid)
    if not t:
        await send(update, f"❌ Турнир #{tid} не найден.")
        return
    if not _can_manage_tournament(update.effective_user.id, t):
        await send(update, "❌ Только создатель турнира или root-админ.")
        return
    stage = (t.get("stage") or "").lower()
    if stage not in ("groups", "group", "groups_done"):
        await send(
            update,
            f"❌ Турнир не в групповой стадии (сейчас: <code>{stage or '—'}</code>). "
            "Добавление в плей-офф/завершённый турнир не поддерживается.",
        )
        return

    # Layout: args[0]=tid, args[1]=player ref, args[2]=optional group.
    # Reply-to-message lets the admin skip args[1].
    rest = args[1:]
    target: dict | None = None
    if rest:
        target = _resolve_player_arg(rest[0])
        if target:
            rest = rest[1:]
        elif reply_target:
            # First arg after tid wasn't a known player → treat it as the
            # group, resolve target from the reply.
            from handlers._helpers import _player_from_user
            target = _player_from_user(reply_target)
        else:
            await send(
                update,
                f"❌ Игрок не найден: {html.escape(rest[0])}",
            )
            return
    elif reply_target:
        from handlers._helpers import _player_from_user
        target = _player_from_user(reply_target)

    if target is None:
        await send(
            update,
            "❌ Не нашёл игрока. Укажи <code>@username</code>, "
            "telegram-id или ответь на его сообщение.",
        )
        return

    # Detect group: explicit arg → that group; otherwise auto-detect
    # if the tournament has only one group.
    rosters = get_tournament_players(tid) or []
    groups_existing: dict[str, list[dict]] = {}
    for tp in rosters:
        g = (tp.get("group_name") or "").strip() or "?"
        groups_existing.setdefault(g, []).append(tp)
    real_groups = {g: v for g, v in groups_existing.items() if g and g != "?"}

    group_arg: str | None = None
    if rest:
        group_arg = rest[0].strip()

    if group_arg:
        # Normalize: single letter → uppercase; allow "Group A", "A", "а" (cyrillic).
        norm = group_arg.upper().replace("ГРУППА", "").strip()
        # Cyrillic A/B/C lookalikes → latin equivalents.
        cyr_to_lat = {"А": "A", "В": "B", "С": "C", "Е": "E", "К": "K",
                      "М": "M", "Н": "H", "О": "O", "Р": "P", "Т": "T",
                      "Х": "X"}
        norm = "".join(cyr_to_lat.get(ch, ch) for ch in norm)
        if not norm:
            await send(update, f"❌ Не пойму группу: {html.escape(group_arg)}")
            return
        # Match against real_groups keys case-insensitive.
        match_g = next(
            (k for k in real_groups if k.upper() == norm), None
        )
        if not match_g:
            await send(
                update,
                f"❌ Группа <code>{html.escape(group_arg)}</code> в турнире "
                f"#{tid} не найдена. Доступны: "
                + (", ".join(sorted(real_groups.keys())) or "—"),
            )
            return
        group_name = match_g
    else:
        if len(real_groups) == 1:
            group_name = next(iter(real_groups))
        elif len(real_groups) == 0:
            await send(
                update,
                "❌ В турнире ещё нет групп — нечего пополнять. "
                "Сначала <code>/start_tournament</code>.",
            )
            return
        else:
            await send(
                update,
                "❌ В турнире несколько групп — укажи третью аргументом "
                "буквой группы (например, <code>A</code>). Доступны: "
                + ", ".join(sorted(real_groups.keys())),
            )
            return

    if is_player_banned(target):
        await send(update, f"❌ Игрок {mention(target['username'])} в бане.")
        return

    deadline = (
        datetime.utcnow() + timedelta(hours=MATCH_DEADLINE_HOURS)
    ).strftime("%Y-%m-%d %H:%M:%S")
    try:
        result = db.late_join_tournament_group(
            tid, int(target["id"]), group_name, deadline=deadline,
        )
    except ValueError as e:
        await send(update, f"❌ {html.escape(str(e))}")
        return
    except Exception as e:
        log.exception("late_join_tournament_group failed: %s", e)
        await send(update, f"❌ Внутренняя ошибка: {html.escape(str(e))}")
        return

    log_tournament_action(
        tid,
        actor_telegram_id=update.effective_user.id,
        actor_username=update.effective_user.username,
        action="admin_addplayer_late",
        details=(
            f"player_id={target['id']} group={group_name} "
            f"created={len(result['created_match_ids'])} "
            f"skipped={len(result['skipped_opponents'])} "
            f"init_elo={result['init_elo']:.0f}"
        ),
    )

    lines = [
        f"✅ {mention(target['username'])} добавлен(а) в группу "
        f"<b>{html.escape(group_name)}</b> турнира "
        f"<b>{html.escape(t['name'])}</b> [{t_full_label(t)}].",
        f"Стартовый ELO в турнире: <b>{int(round(result['init_elo']))}</b> "
        "(средний по группе).",
        f"Создано матчей: <b>{len(result['created_match_ids'])}</b>.",
    ]
    if result["skipped_opponents"]:
        skipped_names = []
        for opp_pid in result["skipped_opponents"]:
            opp = get_player_by_id(opp_pid)
            skipped_names.append(mention(opp["username"]) if opp else f"id{opp_pid}")
        lines.append(
            "⚠️ Уже был(и) матч(и) с: " + ", ".join(skipped_names)
            + " — пропущено."
        )
    lines.append(
        f"Дедлайн новых матчей: через {MATCH_DEADLINE_HOURS}ч "
        f"(<code>{_fmt_minute_local(deadline)} {_tz_label()}</code>)."
    )
    await send(update, "\n".join(lines))


# ─────────────────────────────────────────────────────────────────────────────
# /give_owner — transfer tournament ownership to another user
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_give_owner(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """``/give_owner [ID] @user`` — передать владение турниром другому.

    Доступно текущему создателю турнира или root-админу бота.
    Меняет ``tournaments.created_by`` на нового игрока.
    """
    args = list(ctx.args or [])

    # Resolve tournament (optional leading numeric ID)
    t, remaining_args, err = _split_tadmin_args(update, ctx)
    if t is None:
        await send(update, err or "❌ Нет активного турнира.")
        return

    # Permission: only current owner or root admin
    user_id = update.effective_user.id
    creator = get_player_by_id(t["created_by"]) if t.get("created_by") else None
    is_creator = bool(creator and creator.get("telegram_id") == user_id)
    if not (is_root_admin(user_id) or is_creator):
        await send(
            update,
            "❌ Передать владение турниром может только текущий создатель "
            "турнира или главный админ бота.",
        )
        return

    # Resolve target user
    target_id, label = _resolve_tadmin_target(update, ctx, args=remaining_args)
    if target_id is None:
        await send(update, label, parse_mode="HTML")
        return

    # Don't transfer to self
    if creator and creator.get("telegram_id") == target_id:
        await send(update, f"ℹ️ {label} уже владелец этого турнира.")
        return

    # Resolve target's player row (must exist)
    target_player = get_player_by_telegram_id(target_id)
    if not target_player:
        await send(
            update,
            f"❌ Пользователь {html.escape(label)} не зарегистрирован в боте. "
            f"Сначала пусть напишет /register.",
        )
        return

    # Transfer ownership
    db.update_tournament(t["id"], created_by=target_player["id"])

    # Keep old owner as tournament admin so they don't lose access
    if creator and creator.get("telegram_id"):
        from database import add_tournament_admin as _add_ta
        _add_ta(
            t["id"],
            int(creator["telegram_id"]),
            granted_by=user_id,
            note="auto: бывший создатель",
        )

    log_tournament_action(
        t["id"],
        actor_telegram_id=user_id,
        actor_username=update.effective_user.username,
        action="give_owner",
        details=f"new_owner={label} (player_id={target_player['id']})",
    )

    t_label = f"<b>{html.escape(t['name'])}</b> [{t_full_label(t)}]"
    await send(
        update,
        f"✅ Владение турниром {t_label} передано: {html.escape(label)}.\n"
        f"Теперь {html.escape(label)} — создатель этого турнира.\n"
        f"Старый создатель остаётся админом турнира.",
    )

    # Notify new owner
    try:
        await ctx.bot.send_message(
            target_id,
            f"🏆 Тебе передали владение турниром "
            f"«{html.escape(t['name'])}» [{t_full_label(t)}].\n"
            f"Теперь ты создатель — полные права управления.",
            parse_mode="HTML",
        )
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# /owner, /setowner, /set_owner — assign bot owner (super-admin)
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_set_owner(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """``/owner @user`` -- assign a user as bot owner (super-admin).

    Only existing owners or root admins (ADMIN_IDS) can use this command.
    Aliases: /setowner, /set_owner.
    """
    user_id = update.effective_user.id
    if not is_owner(user_id):
        await send(update, "❌ Только владельцы бота (owner) или root-админы (ADMIN_IDS) могут назначать владельцев.")
        return

    target_id, label = await _resolve_admin_target(update, ctx)
    if target_id is None:
        await send(update, label, parse_mode="HTML")
        return

    if target_id in ADMIN_IDS:
        await send(update, f"ℹ️ {label} уже root-админ (ADMIN_IDS). Он автоматически является владельцем.")
        return

    if is_bot_owner_db(target_id):
        await send(update, f"ℹ️ {label} уже владелец бота.")
        return

    note = " ".join((ctx.args or [])[1:]).strip() or None
    grant_bot_owner(target_id, granted_by=user_id, note=note)
    await send(
        update,
        f"✅ {label} теперь владелец бота (super-admin). "
        f"Имеет доступ ко ВСЕМ функциям бота, выше обычных админов."
        + (f"\nКомментарий: {note}" if note else ""),
    )
    try:
        await ctx.bot.send_message(
            target_id,
            "👑 Тебя назначили владельцем бота (super-admin). "
            "У тебя теперь полный доступ ко всем функциям.",
        )
    except Exception as e:
        log.debug("notify owner target failed: %s", e)


# ─────────────────────────────────────────────────────────────────────────────
# /revoke_owner — revoke bot owner status
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_revoke_owner(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """``/revoke_owner @user`` -- revoke bot owner (super-admin) status.

    Only existing owners or root admins (ADMIN_IDS) can use this command.
    Aliases: /revokeowner.
    """
    user_id = update.effective_user.id
    if not is_owner(user_id):
        await send(update, "❌ Только владельцы бота (owner) или root-админы (ADMIN_IDS) могут снимать владельцев.")
        return

    target_id, label = await _resolve_admin_target(update, ctx)
    if target_id is None:
        await send(update, label, parse_mode="HTML")
        return

    if target_id in ADMIN_IDS:
        await send(
            update,
            f"❌ {label} — root-админ (ADMIN_IDS env). Снять только удалением "
            f"из переменной окружения и рестартом бота.",
        )
        return

    if revoke_bot_owner(target_id):
        await send(update, f"🔓 Статус владельца снят: {label}")
        try:
            await ctx.bot.send_message(target_id, "ℹ️ С тебя сняли статус владельца бота.")
        except Exception:
            pass
    else:
        await send(update, f"ℹ️ {label} и так не был владельцем.")


# ─────────────────────────────────────────────────────────────────────────────
# /owners — list bot owners
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_owners(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Show the current list of bot owners (root + runtime)."""
    lines: list[str] = []
    lines.append("👑 <b>Владельцы бота</b>\n")
    if ADMIN_IDS:
        lines.append("<b>Root</b> (через ADMIN_IDS env, автоматически владельцы):")
        for aid in ADMIN_IDS:
            lines.append(f"  • <code>{aid}</code>")
    else:
        lines.append("<i>Root-админы не настроены (env ADMIN_IDS пустой).</i>")
    runtime = list_bot_owners()
    if runtime:
        lines.append("\n<b>Назначенные</b> (выданы через /owner):")
        for r in runtime:
            tail = ""
            if r.get("note"):
                tail = f" — {r['note']}"
            lines.append(f"  • <code>{r['telegram_id']}</code>{tail}")
    else:
        lines.append("\n<i>Назначенных владельцев нет.</i>")
    lines.append(
        "\nДобавить: <code>/owner @user</code>."
        "\nСнять: <code>/revoke_owner @user</code>."
    )
    await send(update, "\n".join(lines))


__all__ = [
    "cmd_admin_addplayer",
    "cmd_admin_setnick",
    "cmd_admins",
    "cmd_add_tadmin",
    "cmd_admin_addgoal",
    "cmd_admin_addplayer_late",
    "cmd_admin_delgoal",
    "cmd_admin_matchgoals",
    "cmd_admin_setgoalauthor",
    "cmd_admin_setgoalname",
    "cmd_ban",
    "cmd_relink_player",
    "cmd_banned",
    "cmd_broadcast",
    "cmd_clear_channel",
    "cmd_clear_tournament_bg",
    "cmd_elo",
    "cmd_give_owner",
    "cmd_grant_admin",
    "cmd_owners",
    "cmd_remove_tadmin",
    "cmd_revoke_admin",
    "cmd_revoke_owner",
    "cmd_set_channel",
    "cmd_set_description",
    "cmd_set_owner",
    "cmd_set_tournament_bg",
    "cmd_setelo",
    "cmd_tadmins",
    "cmd_unban",
]




# ─────────────────────────────────────────────────────────────────────────────
# Player titles / awards
#
# Free-form titles attached to a player (e.g. "🐐 GOAT", "Чемпион №76").
# Multiple titles per player. Visible in /profile, /table_text and
# /tablebomb. Admins / owners only.
# ─────────────────────────────────────────────────────────────────────────────


async def cmd_award(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """``/award @user <титул>`` — выдать игроку титул.

    Title can be any free-form text (emoji allowed). Multiple titles
    per player are allowed — re-running with the same title creates
    a duplicate row, which the renderer dedupes when displaying.

    Admin-only (bot admin or tournament admin of any tournament).
    """
    user = update.effective_user
    if user is None:
        return
    from handlers.common import is_admin, mention, send

    if not is_admin(user.id):
        # Allow tournament admins to award titles too (their tournament's
        # players); they often run sub-leagues and want to hand out
        # custom titles without bothering bot admins.
        if not db.list_tournament_admin_for_user(user.id):
            await send(update, "❌ Только админ.")
            return

    args = list(ctx.args or [])
    if len(args) < 2:
        await send(
            update,
            "Использование: <code>/award @user &lt;титул&gt;</code>\n"
            "Например: <code>/award @phoenileo 🐐 GOAT</code>",
        )
        return

    target = _resolve_player_arg(args[0])
    if not target:
        await send(
            update,
            f"❌ Игрок <code>{html.escape(args[0])}</code> не найден.",
        )
        return

    title = " ".join(args[1:]).strip()
    if not title:
        await send(update, "❌ Не указан титул.")
        return
    if len(title) > 120:
        title = title[:120].rstrip()

    new_id = db.add_player_title(
        target["id"], title,
        granted_by=user.id,
    )
    await send(
        update,
        f"🏅 Титул <b>{html.escape(title)}</b> выдан игроку "
        f"{mention(target.get('username') or '?')} (id титула: {new_id}).",
    )


async def cmd_revoke_award(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """``/revoke_award @user <титул>`` — снять у игрока титул.

    Match is case-insensitive on the title text. Removes every row
    that matches (so duplicates are cleared in one shot). Admins only.
    """
    user = update.effective_user
    if user is None:
        return
    from handlers.common import is_admin, mention, send

    if not is_admin(user.id):
        if not db.list_tournament_admin_for_user(user.id):
            await send(update, "❌ Только админ.")
            return

    args = list(ctx.args or [])
    if len(args) < 2:
        await send(
            update,
            "Использование: <code>/revoke_award @user &lt;титул&gt;</code>",
        )
        return
    target = _resolve_player_arg(args[0])
    if not target:
        await send(
            update,
            f"❌ Игрок <code>{html.escape(args[0])}</code> не найден.",
        )
        return
    title = " ".join(args[1:]).strip()
    if not title:
        await send(update, "❌ Не указан титул.")
        return
    removed = db.remove_player_title_by_text(target["id"], title)
    if removed:
        await send(
            update,
            f"🗑 У игрока {mention(target.get('username') or '?')} снят "
            f"титул <b>{html.escape(title)}</b> (удалено записей: "
            f"{removed}).",
        )
    else:
        await send(
            update,
            f"ℹ️ У игрока {mention(target.get('username') or '?')} "
            f"нет титула <b>{html.escape(title)}</b>.",
        )


async def cmd_awards(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """``/awards [@user]`` — показать титулы игрока (свои если без аргумента)."""
    from handlers.common import mention, send
    from handlers._helpers import _player_from_user

    args = list(ctx.args or [])
    target = None
    if args:
        target = _resolve_player_arg(args[0])
        if not target:
            await send(
                update,
                f"❌ Игрок <code>{html.escape(args[0])}</code> не найден.",
            )
            return
    else:
        target = _player_from_user(update.effective_user)
        if not target:
            await send(update, "❌ Сначала зарегистрируйся: /register")
            return

    titles = db.list_player_titles(target["id"])
    name = mention(target.get("username") or "?")
    if not titles:
        await send(update, f"🤷 У {name} пока нет титулов.")
        return
    lines = [f"🏅 <b>Титулы</b> {name}:"]
    for t in titles:
        note = (t.get("note") or "").strip()
        # Format: "• 🐐 GOAT — за победу" (note optional)
        if note:
            lines.append(
                f"• {html.escape(t['title'])} — "
                f"<i>{html.escape(note)}</i>"
            )
        else:
            lines.append(f"• {html.escape(t['title'])}")
    await send(update, "\n".join(lines))


__all__.extend([
    "cmd_award",
    "cmd_revoke_award",
    "cmd_awards",
])



# ─────────────────────────────────────────────────────────────────────────────
# /cl_spawn_cups — spawn the two follow-up cups for a Champions League (32) league
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_cl_spawn_cups(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Usage:
        /cl_spawn_cups <league_tournament_id> [main_size] [consolation_size]

    After a 32-player Champions-League-style league has finished,
    spawn the two follow-up cups:

      * **Основной кубок** — places 1..``main_size`` (default 24) of the
        league enter a bracket-only cup. Bracket size is the next power
        of two ≥ ``main_size`` (24 → 32-bracket); the top
        ``2^k - main_size`` seeds receive byes in the first round.
      * **Лига Конфети** — places ``main_size+1``..``main_size+consolation_size``
        (default 25..32) enter an 8-player cup. Standard QF → SF → Final.

    Both cups are seeded by **league finishing position** (not by global
    ELO), all ties are played in **two legs** with aggregate-goal
    advancement, and there's no third-place match.

    Admin-only. Ask the league creator or a bot admin to run it.
    """
    if not is_admin(update.effective_user.id):
        await send(update, "❌ Только админ может запускать /cl_spawn_cups.")
        return

    args = list(ctx.args or [])
    if not args or not args[0].lstrip("-").isdigit():
        await send(
            update,
            "Использование:\n"
            "<code>/cl_spawn_cups &lt;league_id&gt; [main_size] [consolation_size]</code>\n\n"
            "По умолчанию main_size=24, consolation_size = всё, что осталось "
            "после топ-24 (так что то же самое работает и на 32, и на 34, и "
            "на 36 игроков).",
        )
        return

    league_tid = int(args[0])
    main_size = int(args[1]) if len(args) >= 2 and args[1].isdigit() else 24
    cons_size: int | None = (
        int(args[2]) if len(args) >= 3 and args[2].isdigit() else None
    )

    # Lazy import to avoid circular dependency at module import time.
    from tournament import spawn_cl_followup_cups

    try:
        result = spawn_cl_followup_cups(
            league_tid,
            main_size=main_size,
            consolation_size=cons_size,  # None = "all remaining past main_size"
            legs_per_pair=2,
        )
    except ValueError as e:
        await send(update, f"❌ {html.escape(str(e))}.")
        return
    except Exception as e:
        log.exception("spawn_cl_followup_cups failed: %s", e)
        await send(update, f"❌ Внутренняя ошибка: {html.escape(str(e))}.")
        return

    main_real = sum(1 for m in result["main_matches"] if not m.get("bye"))
    main_byes = sum(1 for m in result["main_matches"] if m.get("bye"))
    cons_real = sum(1 for m in result["consolation_matches"] if not m.get("bye"))
    cons_byes = sum(1 for m in result["consolation_matches"] if m.get("bye"))

    cons_size_actual = (
        sum(1 for m in result["consolation_matches"] if not m.get("bye")) * 2
        + cons_byes
    )

    msg = [
        "✅ <b>Follow-up кубки созданы.</b>",
        "",
        f"🏆 <b>Основной кубок</b> — id <code>{result['main_tid']}</code>",
        f"   Игроков: <b>{main_size}</b> (1-{main_size} место лиги)",
        f"   Первый раунд: <b>{main_real}</b> матч(а), баев: <b>{main_byes}</b>",
        f"   Пары играются в 2 матча, проход по сумме голов.",
        "",
    ]
    if result.get("consolation_tid"):
        msg.extend([
            f"🥉 <b>Лига Конфети</b> — id <code>{result['consolation_tid']}</code>",
            f"   Игроков: <b>{cons_size_actual}</b> "
            f"({main_size + 1}-{main_size + cons_size_actual} место лиги)",
            f"   Первый раунд: <b>{cons_real}</b> матч(а), баев: <b>{cons_byes}</b>",
            f"   Пары играются в 2 матча, проход по сумме голов.",
            "",
        ])
    else:
        msg.extend([
            "🥉 Утешительный кубок не создан — на местах 25+ не осталось "
            "хотя бы 2 игроков.",
            "",
        ])
    msg.append("Чтобы посмотреть сетку: <code>/bracket &lt;id&gt;</code>.")
    await send(update, "\n".join(msg))


__all__.append("cmd_cl_spawn_cups")
