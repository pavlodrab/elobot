"""Quote system: per-chat user-submitted quotations + scheduled rotation.

Three flavours:

* ``/quote <author>: <text>``  — add a new quote (any registered player).
* ``/quote`` (replying to a message) — quote the replied-to message,
  with the replied-to user as the attribution.
* ``/quotes [N]`` — list the most recent quotes in this chat.
* ``/delquote <id>`` — admin-only: remove a quote by id.
* ``/set_quote_interval <minutes>`` — per-chat cadence; 0 disables.

Quotes are kept per ``chat_id`` (string) so groups don't see each
other's content. The background ``job_quotes`` loop in ``bot.py``
reads ``chat_settings`` to decide which chats are due for the next
quote and posts one randomly.
"""

from __future__ import annotations

import html
import logging

from telegram import Update
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


def _format_quote(text: str, author: str | None) -> str:
    """Render a single quote for chat output, HTML-safe."""
    body = html.escape((text or "").strip())
    a = (author or "").strip()
    if a:
        a_safe = html.escape(a)
        return f"💬 «{body}»\n— <b>{a_safe}</b>"
    return f"💬 «{body}»\n— <i>аноним</i>"


async def cmd_quote(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """``/quote`` — add a quote to the current chat.

    Two modes:

    1. **Reply mode:** reply to a message with ``/quote`` (no args).
       The replied message's text becomes the quote, the replied
       user's display becomes the author.
    2. **Inline mode:** ``/quote <author>: <text>`` (any of ``:``,
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

    if raw:
        author, text = _split_author_and_text(raw)
    elif msg.reply_to_message and (
        msg.reply_to_message.text or msg.reply_to_message.caption
    ):
        rep = msg.reply_to_message
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

    if not text:
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
    )
    await send(
        update,
        f"💬 Цитата #{qid} сохранена.\n\n{_format_quote(text, author)}",
    )


async def cmd_quotes(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """``/quotes [N]`` — show the last N quotes in this chat (default 10)."""
    chat = update.effective_chat
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
            "Добавь первую: <code>/quote Pep: Не теряй ритм.</code>",
        )
        return
    lines = [f"💬 <b>Последние цитаты</b> ({len(rows)}):"]
    for r in rows:
        a = (r.get("author") or "").strip() or "аноним"
        body = html.escape((r.get("text") or "").strip())
        lines.append(
            f"<b>#{r['id']}</b>  «{body}» — <i>{html.escape(a)}</i>"
        )
    await send(update, "\n".join(lines))


async def cmd_delete_quote(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """``/delquote <id>`` — delete a quote by id (admin-only)."""
    user = update.effective_user
    if user is None:
        return
    if not is_admin(user.id):
        await send(update, "❌ Только админ.")
        return
    args = list(ctx.args or [])
    if not args or not args[0].lstrip("#").isdigit():
        await send(update, "Использование: <code>/delquote &lt;id&gt;</code>")
        return
    qid = int(args[0].lstrip("#"))
    q = db.get_quote(qid)
    if not q:
        await send(update, f"❌ Цитата #{qid} не найдена.")
        return
    db.delete_quote(qid)
    await send(update, f"🗑 Цитата #{qid} удалена.")


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
            f"Текущее: <b>{cur_lbl}</b>. 0 = выключить.",
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
    "cmd_quote",
    "cmd_quotes",
    "cmd_delete_quote",
    "cmd_set_quote_interval",
]
