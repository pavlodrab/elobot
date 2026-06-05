"""Tour (тур / gameweek) commands — Phase: tours.

Commands exposed:
  /turs  [tid] <N>        — render tour N as image
  /turs  [tid] <N-M>      — render tours N through M as image (max 5)
  /turstext [tid] <N>     — same but plain text
  /turstext [tid] <N-M>   — same but plain text (range, max 10)
  /tours [tid]            — list all available tour numbers

The tournament is resolved in this order:
  1. First numeric argument that is a valid tournament id.
  2. Active tournament bound to the current chat.
  3. Any single active tournament.
"""
from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import ContextTypes

from database import (
    get_active_tournament,
    get_all_tour_nums,
    get_matches_by_tour,
    get_tournament,
    get_tournament_by_chat,
)
from tour_image import render_tour_png
from handlers.common import send

log = logging.getLogger(__name__)

_MAX_RANGE_IMAGE = 5   # max tours in one /turs  image call
_MAX_RANGE_TEXT  = 10  # max tours in one /turstext call


# ── helpers ──────────────────────────────────────────────────────────────────

def _resolve_tournament(args: list[str], chat_id: int | None):
    """Return (tournament_dict_or_None, remaining_args)."""
    if args and args[0].isdigit():
        t = get_tournament(int(args[0]))
        if t:
            return t, list(args[1:])
    # Try chat-bound tournament
    if chat_id:
        t = get_tournament_by_chat(str(chat_id))
        if t:
            return t, list(args)
    # Fall back to any active tournament
    t = get_active_tournament()
    return t, list(args)


def _parse_range(token: str) -> tuple[int, int] | None:
    """Parse 'N' or 'N-M' into (start, end). Returns None on bad input."""
    token = token.strip()
    if "-" in token:
        parts = token.split("-", 1)
        if parts[0].isdigit() and parts[1].isdigit():
            a, b = int(parts[0]), int(parts[1])
            return (a, b) if a <= b else (b, a)
    elif token.isdigit():
        n = int(token)
        return (n, n)
    return None


def _player_name(match: dict, side: str) -> str:
    nick = (match.get(f"p{side}_nickname") or "").strip()
    user = (match.get(f"p{side}_username") or "").strip()
    if nick and user and nick.lower() != user.lower():
        return nick
    return f"@{user}" if user else nick or "?"


def _score_str(match: dict) -> str:
    if match.get("status") != "confirmed":
        return "--:--"
    s1 = match.get("score1")
    s2 = match.get("score2")
    if s1 is None or s2 is None:
        return "--:--"
    base = f"{s1}:{s2}"
    pen1, pen2 = match.get("pen1"), match.get("pen2")
    if pen1 is not None and pen2 is not None:
        return f"{base} ({pen1}:{pen2})"
    return base


# -- /turs -- image -----------------------------------------------------------

async def cmd_turs(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Отправить картинку с матчами тура(ов).

    Использование:
      /turs 3          -- тур 3
      /turs 1-4        -- туры 1-4 (максимум 5)
      /turs 42 3       -- тур 3 турнира #42
      /turs 42 1-4     -- туры 1-4 турнира #42
    """
    args = list(ctx.args or [])
    chat_id = update.effective_chat.id if update.effective_chat else None

    t, args = _resolve_tournament(args, chat_id)
    if not t:
        await send(update, "Не нашёл активный турнир. Укажи ID: <code>/turs 42 1</code>")
        return

    if not args:
        tour_nums = get_all_tour_nums(t["id"])
        if not tour_nums:
            await send(update, f"В турнире <b>{t['name']}</b> ещё нет туров.")
            return
        token = str(tour_nums[-1])
    else:
        token = args[0]

    parsed = _parse_range(token)
    if not parsed:
        await send(
            update,
            "Неверный формат. Примеры: <code>/turs 1</code>, <code>/turs 2-5</code>",
        )
        return

    start, end = parsed
    if end - start + 1 > _MAX_RANGE_IMAGE:
        end = start + _MAX_RANGE_IMAGE - 1
        await send(
            update,
            f"Показываю максимум {_MAX_RANGE_IMAGE} туров: {start}-{end}.",
        )

    available = set(get_all_tour_nums(t["id"]))
    tour_list = [tn for tn in range(start, end + 1) if tn in available]
    if not tour_list:
        await send(
            update,
            f"Туры {start}-{end} не найдены в турнире <b>{t['name']}</b>.\n"
            f"Доступные туры: {', '.join(str(x) for x in sorted(available)) or 'нет'}",
        )
        return

    try:
        png = render_tour_png(t["id"], tour_list)
    except Exception as exc:
        log.exception("render_tour_png failed: %s", exc)
        await send(update, "Не удалось сгенерировать изображение. Попробуй /turstext.")
        return

    if len(tour_list) == 1:
        caption = f"<b>{t['name']}</b> - Тур {tour_list[0]}"
    else:
        caption = f"<b>{t['name']}</b> - Туры {tour_list[0]}-{tour_list[-1]}"

    await update.effective_message.reply_photo(
        photo=png,
        caption=caption,
        parse_mode="HTML",
    )


# -- /turstext -- plain text --------------------------------------------------

async def cmd_turstext(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Текстовый список матчей тура(ов).

    Использование:
      /turstext 3          -- тур 3
      /turstext 1-15       -- туры 1-15 (максимум 10)
      /turstext 42 3       -- тур 3 турнира #42
    """
    args = list(ctx.args or [])
    chat_id = update.effective_chat.id if update.effective_chat else None

    t, args = _resolve_tournament(args, chat_id)
    if not t:
        await send(update, "Не нашёл активный турнир. Укажи ID: <code>/turstext 42 1</code>")
        return

    if not args:
        tour_nums = get_all_tour_nums(t["id"])
        if not tour_nums:
            await send(update, f"В турнире <b>{t['name']}</b> ещё нет туров.")
            return
        token = str(tour_nums[-1])
    else:
        token = args[0]

    parsed = _parse_range(token)
    if not parsed:
        await send(
            update,
            "Неверный формат. Примеры: <code>/turstext 1</code>, <code>/turstext 1-15</code>",
        )
        return

    start, end = parsed
    if end - start + 1 > _MAX_RANGE_TEXT:
        end = start + _MAX_RANGE_TEXT - 1

    available = set(get_all_tour_nums(t["id"]))
    tour_list = [tn for tn in range(start, end + 1) if tn in available]
    if not tour_list:
        await send(
            update,
            f"Туры {start}-{end} не найдены в турнире <b>{t['name']}</b>.\n"
            f"Доступные туры: {', '.join(str(x) for x in sorted(available)) or 'нет'}",
        )
        return

    lines = [f"<b>{t['name']}</b>"]
    for tn in tour_list:
        matches = get_matches_by_tour(t["id"], tn)
        lines.append(f"\n<b>Тур {tn}</b>")
        if not matches:
            lines.append("  -- нет матчей --")
            continue
        for m in matches:
            home = _player_name(m, "1")
            away = _player_name(m, "2")
            score = _score_str(m)
            icon = "ok" if m.get("status") == "confirmed" else "..."
            lines.append(f"  [{icon}] {home}  {score}  {away}")

    await send(update, "\n".join(lines))


# -- /tours -- list available tour numbers ------------------------------------

async def cmd_tours_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Список доступных туров турнира.

    Использование:
      /tours         -- для текущего / активного турнира
      /tours 42      -- для турнира #42
    """
    args = list(ctx.args or [])
    chat_id = update.effective_chat.id if update.effective_chat else None

    t, _ = _resolve_tournament(args, chat_id)
    if not t:
        await send(update, "Не нашёл активный турнир.")
        return

    tour_nums = get_all_tour_nums(t["id"])
    if not tour_nums:
        await send(update, f"В турнире <b>{t['name']}</b> ещё нет туров.")
        return

    lines = [f"<b>{t['name']}</b> -- туры:\n"]
    for tn in tour_nums:
        matches = get_matches_by_tour(t["id"], tn)
        done  = sum(1 for m in matches if m.get("status") == "confirmed")
        total = len(matches)
        if done == total and total > 0:
            bar = f"сыграно все ({total})"
        else:
            bar = f"{done}/{total} матчей"
        lines.append(f"  Тур {tn} -- {bar}")

    lines.append(
        f"\n<code>/turs {tour_nums[0]}</code> -- картинка  |  "
        f"<code>/turstext {tour_nums[0]}</code> -- текст"
    )
    await send(update, "\n".join(lines))
