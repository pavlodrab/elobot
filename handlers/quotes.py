"""Quote system: per-chat user-submitted quotations + scheduled rotation.

Four flavours:

* ``/quote <author>: <text>``  — add a new quote (any registered player).
* ``/quote`` (replying to a message) — quote the replied-to message,
  with the replied-to user as the attribution.
* ``/quote`` (replying to a **voice** message) — quote the voice
  message; the bot will re-send the audio later.
* ``/quotes [N]`` — list the most recent quotes in this chat.
* ``/delquote <id>`` — admin-only: remove a quote by id.
* ``/set_quote_interval <minutes>`` — per-chat cadence; 0 disables.
* ``/quote_settings`` — inline button menu for cadence + quick stats.

Quotes are kept per ``chat_id`` (string) so groups don't see each
other's content. The background ``job_quotes`` loop in ``bot.py``
reads ``chat_settings`` to decide which chats are due for the next
quote and posts one randomly.
"""

from __future__ import annotations

import html
import logging
import re

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.error import TelegramError
from telegram.ext import ContextTypes

import database as db

from handlers.common import is_admin, mention, send

log = logging.getLogger(__name__)


_AUTHOR_SEPARATORS = (":", "—", "–", "-", "|")


def _split_author_and_text(raw: str) -> tuple[str, str]:
    """Parse the freeform ``/quote`` argument into ``(author, text)``.

    Accepts several shapes:

    * ``"Pep: Не теряй ритм."``           → ("Pep", "Не теряй ритм.")
    * ``"Pep — Не теряй ритм."``          → ("Pep", "Не теряй ритм.")
    * ``"Pep | Не теряй ритм."``          → ("Pep", "Не теряй ритм.")
    * ``"Не теряй ритм."`` (no separator) → ("", "Не теряй ритм.")

    The first separator wins. We trim whitespace on both halves and
    require ``text`` to be non-empty; an empty author is allowed
    (the quote is just attributed to "—").
    """
    s = (raw or "").strip()
    if not s:
        return "", ""
    # Try each separator in order; pick the earliest one (smallest
    # left-half length) so "Pep: Klopp - says" parses with ":".
    best_idx = -1
    best_sep = ""
    for sep in _AUTHOR_SEPARATORS:
        i = s.find(sep)
        if i <= 0:
            continue
        # Heuristic: keep only short authors (<= 50 chars). Otherwise
        # we'd carve off half of a prose-only quote that has a colon.
        if i > 50:
            continue
        if best_idx < 0 or i < best_idx:
            best_idx = i
            best_sep = sep
    if best_idx < 0:
        return "", s
    author = s[:best_idx].strip().strip("«»\"'\u201c\u201d")
    text = s[best_idx + len(best_sep):].strip().strip("«»\"'\u201c\u201d")
    return author, text or s


def _strip_mentions(s: str) -> str:
    """Break ``@username`` patterns so Telegram doesn't send notifications.

    Inserts a zero-width space (U+200B) right after every ``@`` that is
    followed by a word character, which keeps the visual appearance but
    prevents Telegram from recognising the token as a mention.
    """
    return re.sub(r"@(?=\w)", "@\u200b", s)


def _format_quote(text: str, author: str | None) -> str:
    """Render a single quote for chat output, HTML-safe."""
    body = html.escape((text or "").strip())
    body = _strip_mentions(body)
    a = (author or "").strip()
    if a:
        a_safe = html.escape(a)
        a_safe = _strip_mentions(a_safe)
        return f"💬 «{body}»\n— <b>{a_safe}</b>"
    return f"💬 «{body}»\n— <i>аноним</i>"


def _format_voice_caption(author: str | None) -> str:
    """Caption for a voice-message quote."""
    a = (author or "").strip()
    if a:
        a_safe = html.escape(a)
        a_safe = _strip_mentions(a_safe)
        return f"🎵 — <b>{a_safe}</b>"
    return "🎵 — <i>аноним</i>"


async def cmd_quote(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """``/quote`` — add a quote to the current chat.

    Three modes:

    1. **Reply mode (text):** reply to a text message with ``/quote``
       (no args). The replied message's text becomes the quote, the
       replied user's display becomes the author.
    2. **Reply mode (voice):** reply to a **voice** message with
       ``/quote`` (no args). The bot saves the voice file so it can
       re-send it later as a quote.
    3. **Inline mode:** ``/quote <author>: <text>`` (any of ``:``,
       ``—``, ``-``, ``|`` as separator).
    """
    user = update.effective_user
    msg = update.effective_message
    if user is None or msg is None:
        return
    chat = update.effective_chat
    if chat is None:
        await send(update, "❌ Команда работает только в чате.")
        return
    chat_id = str(chat.id)

    raw = " ".join(ctx.args or []).strip()
    text = ""
    author = ""
    voice_file_id = None

    if raw:
        author, text = _split_author_and_text(raw)
    elif msg.reply_to_message and (
        msg.reply_to_message.text
        or msg.reply_to_message.caption
        or msg.reply_to_message.voice
    ):
        rep = msg.reply_to_message
        if rep.voice:
            text = "🎵"
            voice_file_id = rep.voice.file_id
        else:
            text = (rep.text or rep.caption or "").strip()
        # Attribution: prefer @username, then full name, then "id N".
        ru = rep.from_user
        if ru:
            if getattr(ru, "username", None):
                author = f"@{ru.username}"
            else:
                full = " ".join(
                    x for x in (ru.first_name, ru.last_name) if x
                )
                author = full.strip() or f"id {ru.id}"
    else:
        await send(
            update,
            "Использование:\n"
            "  • <code>/quote &lt;автор&gt;: &lt;текст&gt;</code>\n"
            "  • Или ответом на сообщение: <code>/quote</code> — "
            "тогда автор и текст возьмутся из сообщения.",
        )
        return

    if not text and not voice_file_id:
        await send(
            update,
            "❌ Не понял текст цитаты.\nПример: "
            "<code>/quote Pep: Не теряй свой ритм.</code>",
        )
        return
    if len(text) > 1000:
        text = text[:1000].rstrip()

    # Resolve added_by — a registered player_id is nice-to-have for
    # audit; missing registration doesn't block the add.
    added_by = None
    try:
        from handlers._helpers import _player_from_user
        p = _player_from_user(user)
        if p:
            added_by = int(p["id"])
    except Exception:
        added_by = None

    qid = db.add_quote(
        text, author=author or None,
        chat_id=chat_id, added_by=added_by,
        voice_file_id=voice_file_id,
    )

    if voice_file_id:
        await send(
            update,
            f"💬 Цитата #{qid} сохранена.\n\n{_format_voice_caption(author)}",
        )
    else:
        await send(
            update,
            f"💬 Цитата #{qid} сохранена.\n\n{_format_quote(text, author)}",
        )


async def cmd_quotes(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """``/quotes [N]`` — show the last N quotes in this chat (default 10).

    Each quote is shown with its <code>#id</code> so admins can use
    ``/delquote &lt;id&gt;``. For admins, the panel also has inline
    🗑 buttons for one-tap deletion.
    """
    chat = update.effective_chat
    user = update.effective_user
    if chat is None:
        return
    args = list(ctx.args or [])
    limit = 10
    if args and args[0].isdigit():
        limit = max(1, min(50, int(args[0])))
    rows = db.list_quotes(chat_id=str(chat.id), limit=limit)
    if not rows:
        await send(
            update,
            "🤷 В этом чате пока нет цитат.\n"
            "Добавь первую: <code>/quote Pep: Не теряй ритм.</code>\n"
            "Полный гайд: <code>/quote_help</code>",
        )
        return
    lines = [f"💬 <b>Последние цитаты</b> ({len(rows)}):"]
    for r in rows:
        a = (r.get("author") or "").strip() or "аноним"
        if r.get("voice_file_id"):
            lines.append(
                f"<b>#{r['id']}</b>  🎵 <i>Голосовое сообщение</i> — "
                f"<i>{_strip_mentions(html.escape(a))}</i>"
            )
        else:
            body = _strip_mentions(html.escape((r.get("text") or "").strip()))
            lines.append(
                f"<b>#{r['id']}</b>  «{body}» — <i>{_strip_mentions(html.escape(a))}</i>"
            )
    lines.append("")
    is_a = bool(user and is_admin(user.id))
    if is_a:
        lines.append(
            "🗑 Удалить: <code>/delquote &lt;id&gt;</code> "
            "(или нажми кнопку под цитатой ниже)."
        )
    else:
        lines.append(
            "🗑 Удалить может только админ — <code>/delquote &lt;id&gt;</code>."
        )
    lines.append("📖 Полный гайд: <code>/quote_help</code>")

    kb = None
    kb_rows: list[list[InlineKeyboardButton]] = []
    for r in rows[:10]:
        qid = int(r["id"])
        row: list[InlineKeyboardButton] = []
        if r.get("voice_file_id"):
            row.append(InlineKeyboardButton(
                f"▶️ #{qid}",
                callback_data=f"qs:play:{qid}",
            ))
        if is_a:
            if r.get("voice_file_id"):
                preview = "🎵"
            else:
                preview = (r.get("text") or "").strip()
                if len(preview) > 28:
                    preview = preview[:26] + "…"
            row.append(InlineKeyboardButton(
                f"🗑 #{qid} — {preview}",
                callback_data=f"qs:del:{qid}",
            ))
        if row:
            kb_rows.append(row)
    if kb_rows:
        kb = InlineKeyboardMarkup(kb_rows)
    await send(update, "\n".join(lines), reply_markup=kb)


async def cmd_delete_quote(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """``/delquote <id>`` — delete a quote by id (admin-only).

    Friendly UX — when called without an id, prints exactly how to find
    one (use ``/quotes``) and reminds that only admins can delete.
    """
    user = update.effective_user
    if user is None:
        return
    if not is_admin(user.id):
        await send(
            update,
            "❌ Удалять цитаты может только админ.\n"
            "Если ты админ — добавь свой telegram-id в env "
            "<code>ADMIN_IDS</code> (либо попроси действующего админа "
            "выполнить <code>/grant_admin @ты</code>).",
        )
        return
    args = list(ctx.args or [])
    if not args or not args[0].lstrip("#").isdigit():
        await send(
            update,
            "🗑 <b>Удаление цитаты</b>\n\n"
            "Формат: <code>/delquote &lt;id&gt;</code>\n\n"
            "Где взять id:\n"
            "• <code>/quotes</code> — покажет список с номерами\n"
            "• Под каждой строкой указан <b>#id</b>",
        )
        return
    qid = int(args[0].lstrip("#"))
    q = db.get_quote(qid)
    if not q:
        await send(update, f"❌ Цитата #{qid} не найдена.")
        return
    db.delete_quote(qid)
    await send(update, f"🗑 Цитата #{qid} удалена.")


# ─────────────────────────────────────────────────────────────────────────────
# /quote_help — full guide (one place to learn the whole system).
# ─────────────────────────────────────────────────────────────────────────────


_QUOTE_GUIDE_HTML = (
    "💬 <b>Гайд по цитатам</b>\n\n"

    "<b>1. Добавить цитату</b> — любой пользователь:\n"
    "  • <code>/quote Автор: Текст цитаты</code>\n"
    "  • Разделители: <code>:</code> <code>—</code> <code>-</code> "
    "<code>|</code>\n"
    "  • Можно ответом на сообщение: пишешь <code>/quote</code> в реплай — "
    "бот возьмёт текст и автора из цитируемого сообщения.\n"
    "  • Можно ответом на <b>голосове повідомлення</b>: "
    "<code>/quote</code> в реплай на voice — "
    "бот сохранит аудио и будет пересылать его как цитату.\n"
    "  • Алиасы команды: <code>/addquote</code>, <code>/add_quote</code>, "
    "<code>/quto</code> (опечатка тоже работает).\n\n"

    "<b>2. Посмотреть цитаты</b>:\n"
    "  • <code>/quotes</code> — последние 10 цитат этого чата с их id.\n"
    "  • <code>/quotes 30</code> — последние 30.\n\n"

    "<b>3. Удалить цитату</b> (только админ):\n"
    "  • <code>/delquote &lt;id&gt;</code>\n"
    "  • id берётся из <code>/quotes</code> — там у каждой строки в начале "
    "стоит <code>#&lt;id&gt;</code>.\n"
    "  • Также под цитатами в <code>/quotes</code> есть инлайн-кнопки "
    "🗑 для удаления одним тапом.\n\n"

    "<b>4. Авто-рассылка цитат</b>:\n"
    "  • <code>/quote_settings</code> (или <code>/quotemenu</code>) — "
    "панель управления для этого чата.\n"
    "  • Кнопкой выбираешь интервал: 30 мин / 1 ч / 3 ч / 6 ч / 12 ч / "
    "24 ч / Выкл.\n"
    "  • Точное значение в минутах: "
    "<code>/set_quote_interval &lt;минут&gt;</code> (макс 10080 = 1 неделя).\n"
    "  • <b>Менять интервал может только админ чата или админ бота.</b>\n\n"

    "<b>5. Тихие часы (по умолчанию 23:00–12:00 МСК)</b>:\n"
    "  • Ночью бот не шлёт цитаты — настройка по умолчанию не пингует "
    "в 3 ночи.\n"
    "  • В <code>/quote_settings</code> кнопка «🌙 Тихие часы» — выбор "
    "пресета или отключение.\n"
    "  • Если <i>start = end</i>, тихих часов нет (24/7 цитаты).\n\n"

    "<b>6. Что появляется в чате</b>:\n"
    "<code>💬 «Текст цитаты»\n— Автор</code>\n"
)


async def cmd_quote_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """``/quote_help`` — полный гайд по системе цитат."""
    await send(update, _QUOTE_GUIDE_HTML)


async def cmd_set_quote_interval(
    update: Update, ctx: ContextTypes.DEFAULT_TYPE,
):
    """``/set_quote_interval <minutes>`` — set the chat's quote cadence.

    0 disables. Per chat — admins of one group don't affect another.
    Telegram admins of the chat OR bot admins can change it.
    """
    user = update.effective_user
    chat = update.effective_chat
    if user is None or chat is None:
        return
    args = list(ctx.args or [])
    if not args or not args[0].lstrip("-").isdigit():
        cur = db.get_chat_settings(chat.id)
        cur_min = int(cur.get("quote_interval_minutes") or 0)
        cur_lbl = "выкл" if cur_min <= 0 else f"{cur_min} мин"
        await send(
            update,
            "Использование: "
            "<code>/set_quote_interval &lt;минут&gt;</code>\n"
            f"Текущее: <b>{cur_lbl}</b>. 0 = выключить.\n"
            "<i>(только админ чата или бота)</i>",
        )
        return
    minutes = int(args[0])
    minutes = max(0, min(10080, minutes))

    # Allow bot admins always; for non-admins require they be a chat
    # administrator (Telegram-side). Best-effort — if the API call
    # fails, fall back to allowing any user (groups without get_me
    # permissions hit this).
    if not is_admin(user.id):
        try:
            member = await ctx.bot.get_chat_member(chat.id, user.id)
            if member.status not in ("creator", "administrator"):
                await send(
                    update,
                    "❌ Менять цитаты в чате может только админ чата "
                    "или админ бота.",
                )
                return
        except Exception:
            # Best-effort; let it through rather than blocking valid
            # admins on permission errors.
            pass

    db.set_chat_quote_interval(chat.id, minutes)
    if minutes == 0:
        await send(update, "🔕 Цитаты в этом чате отключены.")
    else:
        await send(
            update,
            f"💬 Цитаты в этом чате будут отправляться каждые "
            f"<b>{minutes}</b> мин.\nДобавить цитату: "
            f"<code>/quote &lt;автор&gt;: &lt;текст&gt;</code>",
        )


__all__ = [
    "_format_quote",
    "_format_voice_caption",
    "cmd_quote",
    "cmd_quotes",
    "cmd_delete_quote",
    "cmd_set_quote_interval",
    "cmd_quote_settings",
    "cmd_quote_help",
    "cb_quote_settings",
]


# ─────────────────────────────────────────────────────────────────────────────
# /quote_settings — inline button menu for cadence + quick stats.
#
# Per-chat (because ``chat_settings`` is per chat). Shows current
# cadence, total quote count, and a 1-tap picker for common intervals.
# Designed to be the easy-mode entry-point — admins who prefer typing
# can still use ``/set_quote_interval``.
# ─────────────────────────────────────────────────────────────────────────────


def _format_hour(h: int) -> str:
    """Format an hour-of-day (0..23) as ``HH:00``."""
    return f"{int(h) % 24:02d}:00"


def _quote_settings_kb(tid_chat: str | int) -> InlineKeyboardMarkup:
    """Build the cadence + quiet-hour picker keyboard for a chat."""
    cid = str(tid_chat)
    rows = [
        [
            InlineKeyboardButton("🔕 Выкл", callback_data=f"qs:set:{cid}:0"),
            InlineKeyboardButton("30 мин",  callback_data=f"qs:set:{cid}:30"),
        ],
        [
            InlineKeyboardButton("1 ч",   callback_data=f"qs:set:{cid}:60"),
            InlineKeyboardButton("3 ч",   callback_data=f"qs:set:{cid}:180"),
        ],
        [
            InlineKeyboardButton("6 ч",   callback_data=f"qs:set:{cid}:360"),
            InlineKeyboardButton("12 ч",  callback_data=f"qs:set:{cid}:720"),
        ],
        [InlineKeyboardButton("24 ч",  callback_data=f"qs:set:{cid}:1440")],
        # Quiet-hour presets — admin-only, applies a (start, end) pair.
        # Format: ``qs:quiet:<chat_id>:<start>:<end>``.
        [
            InlineKeyboardButton(
                "🌙 Тихие 23–12 (МСК)",
                callback_data=f"qs:quiet:{cid}:23:12",
            ),
        ],
        [
            InlineKeyboardButton(
                "🌙 22–10",
                callback_data=f"qs:quiet:{cid}:22:10",
            ),
            InlineKeyboardButton(
                "🌙 00–09",
                callback_data=f"qs:quiet:{cid}:0:9",
            ),
            InlineKeyboardButton(
                "☀️ Без тихих",
                callback_data=f"qs:quiet:{cid}:0:0",
            ),
        ],
        [
            InlineKeyboardButton(
                "📖 Гайд", callback_data=f"qs:guide:{cid}",
            ),
            InlineKeyboardButton("✖️ Закрыть", callback_data="qs:close"),
        ],
    ]
    return InlineKeyboardMarkup(rows)


def _quote_settings_text(chat_id: str | int) -> str:
    """Render the settings panel body — current cadence, quote count,
    quiet-hour window and a one-line hint."""
    settings = db.get_chat_settings(chat_id)
    cur_min = int(settings.get("quote_interval_minutes") or 0)
    cur_lbl = "выкл" if cur_min <= 0 else f"каждые {cur_min} мин"
    try:
        total = len(db.list_quotes(chat_id=str(chat_id), limit=1000))
    except Exception:
        total = 0
    qs = int(settings.get("quiet_start_hour") or 23)
    qe = int(settings.get("quiet_end_hour") or 12)
    if qs == qe:
        quiet_lbl = "выкл (24/7)"
    else:
        quiet_lbl = (
            f"{_format_hour(qs)} → {_format_hour(qe)} "
            f"(в это время цитаты не отправляются)"
        )
    last_raw = settings.get("last_quote_at")
    last_str = ""
    if last_raw:
        last_str = (
            f"\n   Последняя цитата отправлена: "
            f"<code>{html.escape(str(last_raw))}</code> UTC"
        )
    return (
        "💬 <b>Настройки цитат</b>\n\n"
        f"⏱ Частота: <b>{cur_lbl}</b>\n"
        f"🌙 Тихие часы: <b>{quiet_lbl}</b>\n"
        f"📜 Цитат в чате: <b>{total}</b>{last_str}\n\n"
        "Жми кнопки ниже, чтобы поменять.\n"
        "<i>Менять может только админ чата или админ бота.</i>\n\n"
        "📥 Добавить: <code>/quote Автор: текст</code>\n"
        "📋 Список: <code>/quotes</code>\n"
        "🗑 Удалить (только админ): <code>/delquote &lt;id&gt;</code>\n"
        "📖 Гайд: <code>/quote_help</code>"
    )


async def _user_can_change_quotes(
    update: Update, ctx: ContextTypes.DEFAULT_TYPE,
) -> bool:
    """Allow bot admins always; in groups, also allow Telegram chat
    administrators. DM with the bot — only the user themselves (and
    bot admins). Best-effort: API failures default to **denying** the
    change so we don't accidentally give random users in big groups
    the power to mess with cadence.
    """
    user = update.effective_user
    chat = update.effective_chat
    if user is None or chat is None:
        return False
    if is_admin(user.id):
        return True
    if chat.type in ("group", "supergroup"):
        try:
            member = await ctx.bot.get_chat_member(chat.id, user.id)
            return member.status in ("creator", "administrator")
        except Exception:
            return False  # safer default: deny on API error
    return True  # private chats: only one user can ever interact


async def cmd_quote_settings(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """``/quote_settings`` — open the inline cadence picker for this chat."""
    chat = update.effective_chat
    if chat is None:
        return
    body = _quote_settings_text(chat.id)
    kb = _quote_settings_kb(chat.id)
    await send(update, body, reply_markup=kb)


async def cb_quote_settings(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Inline callback handler for the ``qs:*`` namespace.

    * ``qs:set:<chat_id>:<minutes>``         — apply a new cadence.
    * ``qs:quiet:<chat_id>:<start>:<end>``   — apply quiet-hour window.
    * ``qs:guide:<chat_id>``                 — show /quote_help inline.
    * ``qs:del:<quote_id>``                  — admin one-tap delete.
    * ``qs:close``                           — drop the panel.
    """
    query = update.callback_query
    if not query or not query.data:
        return
    await query.answer()
    data = query.data
    parts = data.split(":")
    action = parts[1] if len(parts) > 1 else ""

    if action == "close":
        try:
            await query.edit_message_text("💬 Закрыто.")
        except TelegramError:
            pass
        return

    if action == "guide":
        try:
            await query.edit_message_text(
                _QUOTE_GUIDE_HTML,
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton(
                        "⬅️ Назад",
                        callback_data=(
                            f"qs:back:{parts[2]}" if len(parts) > 2 else "qs:close"
                        ),
                    ),
                ]]),
            )
        except TelegramError:
            pass
        return

    if action == "back" and len(parts) > 2:
        chat_id_back = parts[2]
        try:
            await query.edit_message_text(
                _quote_settings_text(chat_id_back),
                parse_mode="HTML",
                reply_markup=_quote_settings_kb(chat_id_back),
            )
        except TelegramError:
            pass
        return

    # ── Play voice quote from /quotes list ────────────────────────────
    if action == "play":
        if len(parts) < 3:
            return
        try:
            qid = int(parts[2])
        except ValueError:
            return
        q = db.get_quote(qid)
        if not q or not q.get("voice_file_id"):
            try:
                await query.message.reply_text(
                    "❌ Цитата не найдена или не содержит голосовое сообщение.",
                )
            except TelegramError:
                pass
            return
        try:
            await query.message.reply_voice(
                voice=q["voice_file_id"],
                caption=_format_voice_caption(q.get("author") or ""),
                parse_mode="HTML",
            )
        except TelegramError:
            try:
                await query.message.reply_text(
                    "❌ Не удалось отправить голосовое сообщение.",
                )
            except TelegramError:
                pass
        return

    # ── One-tap delete from /quotes admin keyboard ────────────────────
    if action == "del":
        if len(parts) < 3:
            return
        try:
            qid = int(parts[2])
        except ValueError:
            return
        user = update.effective_user
        if user is None or not is_admin(user.id):
            try:
                await query.message.reply_text(
                    "❌ Удалять цитаты может только админ.",
                )
            except TelegramError:
                pass
            return
        try:
            ok = db.delete_quote(qid)
        except Exception:
            log.exception("qs:del: persist failed for #%s", qid)
            ok = False
        try:
            await query.message.reply_text(
                f"🗑 Цитата #{qid} удалена."
                if ok else f"❌ Цитата #{qid} не найдена.",
                parse_mode="HTML",
            )
        except TelegramError:
            pass
        return

    # ── Cadence + quiet-hour mutators (admin-only) ────────────────────
    if action in ("set", "quiet"):
        if not await _user_can_change_quotes(update, ctx):
            try:
                await query.message.reply_text(
                    "❌ Менять настройки цитат может только админ чата "
                    "или админ бота.",
                )
            except TelegramError:
                pass
            return
        if len(parts) < 3:
            return
        chat_id_raw = parts[2]
        try:
            if action == "set":
                minutes = max(0, min(10080, int(parts[3])))
                db.set_chat_quote_interval(chat_id_raw, minutes)
            else:  # quiet
                start_h = int(parts[3])
                end_h = int(parts[4])
                db.set_chat_quote_quiet_hours(chat_id_raw, start_h, end_h)
        except Exception:
            log.exception("qs:%s: persist failed for chat=%s", action, chat_id_raw)
            try:
                await query.message.reply_text(
                    "❌ Не удалось сохранить. Попробуй ещё раз.",
                )
            except TelegramError:
                pass
            return
        # Refresh the panel.
        try:
            await query.edit_message_text(
                _quote_settings_text(chat_id_raw),
                parse_mode="HTML",
                reply_markup=_quote_settings_kb(chat_id_raw),
            )
        except TelegramError:
            pass
        return
