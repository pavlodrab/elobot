"""User identity / profile commands (Phase 5 of the bot.py split).

This module owns the player-side, low-coupling commands that don't
depend on the wider tournament state machine:

* ``/register``, ``/setnick`` — onboarding.
* ``/profile`` — full ELO/streak/match-history overview for a player.
* ``/matches`` — last-N personal match log.
* ``/myid`` — surface telegram user/chat IDs and how each one is used.
* ``/keyboard``, ``/show_keyboard``, ``/hide_keyboard`` — DM-only toggle
  for the bottom reply keyboard.
* ``/admincmd`` (alias ``/adminhelp``) — admin command reference.

Anything that needs the menu-system helpers (``main_menu_kb``,
``_menu_kb_for``, ``admin_help_text``) imports them lazily from
``bot`` at call time — this avoids the import cycle that would
otherwise form because ``bot`` imports this module at module load.

Re-exported from ``bot`` for backward compatibility.
"""

from __future__ import annotations

import html
import logging

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.error import TelegramError
from telegram.ext import ContextTypes

import database as db
from database import (
    get_player,
    get_player_by_game_nickname,
    get_player_by_id,
    get_player_by_telegram_id,
    get_player_matches,
    get_tournament,
    get_tournament_by_chat,
    is_player_banned,
    set_game_nickname,
    upsert_player,
)
from elo import rank_label

from handlers._helpers import _player_from_user
from handlers.common import (
    ADMIN_IDS,
    _fmt_date,
    _fmt_dt,
    is_admin,
    mention,
    send,
    t_full_label,
    t_type_label,
)

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# /admincmd  /adminhelp — admin command reference
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_admincmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    msg = update.effective_message
    if msg is None:
        return
    if not is_admin(user_id):
        await msg.reply_text(
            "❌ Этот список — только для админов. Игроцкие команды: /help",
        )
        return
    # Lazy import to avoid the bot ↔ handlers.profile import cycle.
    from bot import _menu_kb_for, _split_for_telegram, admin_help_text
    chunks = _split_for_telegram(admin_help_text())
    last_idx = len(chunks) - 1
    for i, chunk in enumerate(chunks):
        kb = _menu_kb_for(update, user_id) if i == last_idx else None
        try:
            await msg.reply_text(
                chunk,
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=kb,
            )
        except TelegramError:
            await msg.reply_text(
                chunk,
                disable_web_page_preview=True,
                reply_markup=kb,
            )


# ─────────────────────────────────────────────────────────────────────────────
# /hide_keyboard  /show_keyboard  /keyboard — bottom panel toggle (DM only)
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_hide_keyboard(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    p = _player_from_user(user) if user else None
    if p:
        try:
            db.set_no_keyboard_preference(p["id"], True)
        except Exception:
            log.exception("set_no_keyboard_preference(True) failed")
    msg = update.effective_message
    if msg is None:
        return
    try:
        await msg.reply_text(
            "🫥 Нижняя панель скрыта. Используй обычные команды (/help, /report, "
            "/profile и т.д.). Чтобы вернуть — /show_keyboard или /keyboard.",
            reply_markup=ReplyKeyboardRemove(),
        )
    except Exception:
        log.exception("cmd_hide_keyboard reply failed")
        # Fallback: at least confirm without trying to remove the keyboard.
        try:
            await msg.reply_text("🫥 Нижняя панель скрыта.")
        except Exception:
            pass


async def cmd_show_keyboard(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    p = _player_from_user(user) if user else None
    if p:
        try:
            db.set_no_keyboard_preference(p["id"], False)
        except Exception:
            log.exception("set_no_keyboard_preference(False) failed")
    chat = update.effective_chat
    msg = update.effective_message
    if msg is None:
        return
    if chat and chat.type in ("group", "supergroup", "channel"):
        await msg.reply_text(
            "ℹ️ В групповых чатах нижняя панель не отображается — "
            "только slash-команды. Открой DM с ботом, чтобы пользоваться меню.",
        )
        return
    from bot import main_menu_kb  # lazy: avoid bot ↔ profile import cycle
    try:
        await msg.reply_text(
            "✅ Нижняя панель снова видна.",
            reply_markup=main_menu_kb(user.id if user else None),
        )
    except Exception:
        log.exception("cmd_show_keyboard reply failed")
        try:
            await msg.reply_text("✅ Нижняя панель снова видна.")
        except Exception:
            pass


async def cmd_keyboard(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """``/keyboard`` — inline-toggle for the bottom reply keyboard.

    Useful when the user no longer has the reply keyboard active in their
    chat (Telegram doesn't show the toggle-icon when no keyboard is
    set), so ``/hide_keyboard`` would have nothing to interact with.
    This sends an inline button that swaps the preference with one tap.
    """
    user = update.effective_user
    chat = update.effective_chat
    msg = update.effective_message
    if msg is None:
        return
    if chat and chat.type in ("group", "supergroup", "channel"):
        await msg.reply_text(
            "ℹ️ В групповых чатах нижняя панель не отображается — "
            "только slash-команды. Открой DM с ботом, чтобы пользоваться меню.",
        )
        return

    p = _player_from_user(user) if user else None
    is_hidden = False
    if p:
        try:
            is_hidden = db.get_no_keyboard_preference(p["id"])
        except Exception:
            is_hidden = False

    if is_hidden:
        text = (
            "📋 Нижняя панель сейчас <b>скрыта</b>.\n"
            "Тапни ниже, чтобы вернуть её."
        )
        btn_label = "📋 Показать нижнюю панель"
        cb = "kb:show"
    else:
        text = (
            "📋 Нижняя панель сейчас <b>видна</b>.\n"
            "Тапни ниже, чтобы спрятать (останутся только slash-команды)."
        )
        btn_label = "🫥 Скрыть нижнюю панель"
        cb = "kb:hide"
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(btn_label, callback_data=cb)]])
    await msg.reply_text(text, parse_mode="HTML", reply_markup=kb)


# ─────────────────────────────────────────────────────────────────────────────
# /myid — surface telegram user/chat IDs and how each one is used
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_myid(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat

    user_id = user.id if user else None
    user_name = user.full_name if user else "—"
    user_at = f"@{user.username}" if (user and user.username) else "—"

    chat_id = chat.id if chat else None
    chat_type = chat.type if chat else "—"
    chat_title = (chat.title or chat.full_name or "") if chat else ""

    is_in_env_admins = bool(user_id is not None and user_id in ADMIN_IDS)

    # Active tournament bound to this chat (if any) — useful context
    bound_t = None
    if chat is not None:
        try:
            bound_t = get_tournament_by_chat(chat.id)
        except Exception:
            bound_t = None

    lines = ["🆔 <b>Твои ID</b>", ""]
    lines.append(f"• <b>user_id</b> (твой Telegram ID): <code>{user_id}</code>")
    if user_at != "—":
        lines.append(f"  └ {html.escape(user_name)} ({user_at})")
    else:
        lines.append(f"  └ {html.escape(user_name)}")
    lines.append("")
    lines.append(f"• <b>chat_id</b>: <code>{chat_id}</code>")
    lines.append(f"  └ тип: <code>{chat_type}</code>"
                 + (f", «{html.escape(chat_title)}»" if chat_title else ""))

    if bound_t:
        lines.append("")
        lines.append(
            f"🔗 Этот чат привязан к турниру "
            f"<b>{html.escape(bound_t['name'])}</b> "
            f"(ID <code>{bound_t['id']}</code>, {t_full_label(bound_t)})."
        )

    lines.append("")
    lines.append("<b>Куда какой ID</b>")
    lines.append(
        "• <code>ADMIN_IDS</code> (env-переменная бота) — твой <b>user_id</b>. "
        "Делает тебя root-админом, которого нельзя снять через /revoke_admin."
    )
    if is_in_env_admins:
        lines.append("  ✅ Ты сейчас в ADMIN_IDS.")
    lines.append(
        "• <code>/grant_admin</code>, <code>/revoke_admin</code> — "
        "тоже про <b>user_id</b> (но удобнее по @username или ответом)."
    )
    lines.append(
        "• <code>/bind_tournament &lt;ID турнира&gt;</code> — "
        "запусти в групповом чате; бот возьмёт <b>chat_id</b> сам и "
        "будет автоматически засчитывать сюда скрины. ID турнира берётся "
        "из <code>/tournaments</code>."
    )
    lines.append(
        "• <code>/set_auto_confirm &lt;ID турнира&gt; on|off</code> — "
        "<b>ID турнира</b> (а не user_id). Включает мгновенный зачёт матча "
        "по скрину без подтверждения соперника."
    )
    lines.append(
        "• <code>/set_playoff_slots</code>, <code>/set_series_length</code>, "
        "<code>/set_matches_per_pair</code>, <code>/set_reminders</code> — "
        "первый аргумент тоже <b>ID турнира</b>."
    )

    await update.effective_message.reply_text(
        "\n".join(lines),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


# ─────────────────────────────────────────────────────────────────────────────
# /register — onboarding
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_register(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    # If we already have this Telegram user under an old @username, just
    # update the username on the existing row instead of creating a duplicate.
    # Important: also handle the case where the user *removed* their
    # public @username — sync the row to the synthetic ``id_<tid>``
    # placeholder so other handlers stop trying to mention them as
    # @oldhandle (which no longer pings them).
    existing = get_player_by_telegram_id(user.id)
    new_uname = (user.username or f"id_{user.id}").lower()
    if existing and existing["username"] != new_uname:
        from database import update_player_username
        update_player_username(existing["id"], new_uname)
    elif not existing:
        upsert_player(new_uname, user.id)
    label = f"@{user.username}" if user.username else f"id {user.id}"
    await send(
        update,
        f"✅ <b>{html.escape(label)}</b> зарегистрирован(а) в лиге!\n"
        f"🏅 Стартовый ELO: <b>0</b>\n\n"
        f"Дальше: укажи свой игровой ник через <code>/setnick MyInGameNickname</code>\n"
        f"чтобы бот мог распознавать твои скрины матчей автоматически.\n\n"
        f"/help — список команд.",
    )


# ─────────────────────────────────────────────────────────────────────────────
# /setnick — bind / change in-game nickname
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_setnick(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    p = _player_from_user(user)
    if not p:
        # Auto-register so /setnick can be the user's first command.
        upsert_player(user.username or f"id_{user.id}", user.id)
        p = _player_from_user(user)
        if not p:
            await send(update, "❌ Не удалось зарегистрировать тебя. Попробуй /register.")
            return
    if not ctx.args:
        cur = p.get("game_nickname") or "—"
        await send(
            update,
            "Использование: <code>/setnick InGameNickname</code>\n\n"
            f"Твой текущий игровой ник: <b>{html.escape(cur)}</b>",
        )
        return

    new_nick = " ".join(ctx.args).strip()
    if len(new_nick) > 64:
        await send(update, "❌ Слишком длинный ник (макс. 64 символа).")
        return

    # Make sure it isn't already taken by another player
    existing = get_player_by_game_nickname(new_nick)
    if existing and existing["id"] != p["id"]:
        await send(
            update,
            f"❌ Ник <b>{html.escape(new_nick)}</b> уже занят игроком "
            f"{mention(existing['username'])}.",
        )
        return

    set_game_nickname(p["id"], new_nick)
    await send(
        update,
        f"✅ Игровой ник для {mention(p['username'])} установлен: "
        f"<b>{html.escape(new_nick)}</b>",
    )


# ─────────────────────────────────────────────────────────────────────────────
# /profile — full ELO/streak/match-history overview
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_profile(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if ctx.args:
        uname = ctx.args[0].lstrip("@").lower()
        p = get_player(uname)
    else:
        user = update.effective_user
        p = _player_from_user(user)

    if not p:
        await send(update, "❌ Игрок не найден.")
        return

    total = p["wins"] + p["losses"] + p["draws"]
    winrate = f"{p['wins']/total*100:.0f}%" if total else "—"
    avg_gf = f"{p['goals_scored']/total:.1f}" if total else "—"
    avg_ga = f"{p['goals_conceded']/total:.1f}" if total else "—"
    nick_line = (
        f"🎮 Ник в игре: <b>{p['game_nickname']}</b>\n"
        if p.get("game_nickname") else
        "🎮 Ник в игре: <i>не указан</i> — задай через /setnick\n"
    )

    ban_line = ""
    if is_player_banned(p):
        ban_line = (
            f"🚫 <b>В бане до {_fmt_dt(p['banned_until'])}</b>"
            + (f"\nПричина: {p['banned_reason']}" if p.get("banned_reason") else "")
            + "\n\n"
        )

    last_adj_line = ""
    if p.get("last_elo_adjust"):
        last_adj_line = f"⚖️ Последняя ручная правка ELO: <i>{p['last_elo_adjust']}</i>\n"

    elo_vsa = round(p.get("elo_vsa") or 0)
    elo_ri  = round(p.get("elo_ri") or 0)

    # Local ELO across all player-created (isolated) tournaments this player has joined.
    local_lines = []
    conn = db.get_conn()
    rows = conn.execute(
        """SELECT t.id, t.name, t.tournament_type, t.is_official, t.stage, te.elo
           FROM tournament_players tp
           JOIN tournaments t   ON t.id = tp.tournament_id
           LEFT JOIN tournament_elo te
                  ON te.tournament_id = tp.tournament_id
                 AND te.player_id     = tp.player_id
           WHERE tp.player_id = ? AND COALESCE(t.is_official, 1) = 0
           ORDER BY t.id DESC""",
        (p["id"],),
    ).fetchall()
    conn.close()
    for r in rows:
        local_lines.append(
            f"  • <b>{r['name']}</b> [{t_type_label(r['tournament_type'])}] "
            f"(ID: {r['id']}): <b>{round(r['elo'] or 0)}</b> "
            f"<i>({r['stage']})</i>"
        )
    local_block = ""
    if local_lines:
        local_block = (
            "\n🏠 <b>Локальные ELO в турнирах игроков</b>\n"
            + "\n".join(local_lines)
            + "\n"
        )

    text = (
        f"👤 <b>{mention(p['username'])}</b>\n"
        f"{'─'*30}\n"
        f"{ban_line}"
        f"{nick_line}"
        f"🏅 ELO (общий пул): <b>{round(p['elo'])}</b>  {rank_label(p['elo'])}\n"
        f"   ⚽ ВСА: <b>{elo_vsa}</b>   🎮 РИ: <b>{elo_ri}</b>\n"
        f"   <i>Только официальные турниры. Турниры игроков — отдельно ниже.</i>\n"
        f"{local_block}"
        f"{last_adj_line}\n"
        f"📊 <b>Статистика</b>\n"
        f"  Матчей: {total}  |  Винрейт: {winrate}\n"
        f"  В: {p['wins']}  Н: {p['draws']}  П: {p['losses']}\n\n"
        f"⚽ <b>Голы</b>\n"
        f"  Забито: {p['goals_scored']}  (avg {avg_gf})\n"
        f"  Пропущено: {p['goals_conceded']}  (avg {avg_ga})\n"
        f"  Сухие матчи: {p['clean_sheets']}\n\n"
        f"🔥 <b>Серия побед</b>: {p['win_streak']}  (рекорд: {p['best_streak']})\n"
    )

    # Profile-only: an inline shortcut to the player's open matches.
    # Falls back to plain text on any markup error.
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton(
            "📅 Мои матчи / дедлайны", callback_data="my_deadlines",
        ),
        InlineKeyboardButton(
            "📋 История матчей", callback_data="my_matches",
        ),
    ]])
    try:
        await send(update, text, reply_markup=kb)
    except Exception:
        await send(update, text)


# ─────────────────────────────────────────────────────────────────────────────
# /matches — last-N personal match log
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_matches(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    p = _player_from_user(user)
    if not p:
        await send(update, "❌ Сначала зарегистрируйся: /register")
        return

    history = get_player_matches(p["id"], limit=8)
    if not history:
        await send(update, "У тебя ещё нет сыгранных матчей.")
        return

    lines = [f"📋 <b>Последние матчи {mention(p['username'])}</b>\n"]
    for m in history:
        p1 = get_player_by_id(m["player1_id"])
        p2 = get_player_by_id(m["player2_id"])
        s1, s2 = m["score1"], m["score2"]
        is_p1 = p["id"] == m["player1_id"]
        my_score = s1 if is_p1 else s2
        opp_score = s2 if is_p1 else s1
        opp_name = p2["username"] if is_p1 else p1["username"]

        if my_score > opp_score:
            result = "✅ Победа"
        elif my_score < opp_score:
            result = "❌ Поражение"
        else:
            result = "🤝 Ничья"

        date = _fmt_date(m.get("played_at") or m.get("created_at"))
        # Tournament tag
        tt = ""
        if m.get("tournament_id"):
            t = get_tournament(m["tournament_id"])
            if t:
                tt = f" [{t_type_label(t['tournament_type'])}]"
        lines.append(
            f"{result}  <b>{my_score}:{opp_score}</b>  vs {mention(opp_name)}  "
            f"<i>{date}</i>{tt}  <code>#{m['id']}</code>"
        )
    lines.append("")
    lines.append(
        "<i>ID матча — в конце каждой строки. Админ может пересчитать "
        "его как ТП: <code>/walkover #ID @loser</code>.</i>"
    )

    await send(update, "\n".join(lines))


__all__ = [
    "cmd_admincmd",
    "cmd_hide_keyboard",
    "cmd_show_keyboard",
    "cmd_keyboard",
    "cmd_myid",
    "cmd_register",
    "cmd_setnick",
    "cmd_profile",
    "cmd_matches",
]
