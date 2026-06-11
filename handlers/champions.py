"""Hall-of-Fame for past tournaments parsed from the @gvardiolPlay channel.

Three Telegram commands live here:

* ``/champions`` — user-facing inline-keyboard browser. Pick tournament
  type (Гвардиолыч / Фэнтези / VSA), then a view: top by titles,
  chronology (paginated), or per-player. Every date and post link
  jumps to the original channel announcement.
* ``/champion @user`` — combined card for one player across every
  tournament type (titles + final losses + fantasy podium finishes).
* ``/alias`` — admin-only mapping of free-form names ("Феникс",
  declensions like "Антона") to a registered player. Used by the
  importer and any future free-form lookup.
* ``/import_champions`` — admin-only one-shot bulk import of
  ``data/champions_parsed.json`` produced by
  ``scripts/parse_gvardiol_dump.py``. Idempotent.

All callback-query routes are prefixed with ``chmp:`` and dispatched
from ``bot.callback_handler`` via :func:`handle_callback`.
"""

from __future__ import annotations

import html
import json
import logging
import os
import re
import shlex
from datetime import datetime
from pathlib import Path
from typing import Any

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.error import TelegramError
from telegram.ext import ContextTypes

import database as db
from handlers._helpers import _resolve_player_arg
from handlers.common import is_admin, mention, send

log = logging.getLogger("fc_league_bot.handlers.champions")


# ─────────────────────────────────────────────────────────────────────────────
# Tournament-type metadata
# ─────────────────────────────────────────────────────────────────────────────

# Display labels for the three tournament types we recognise.
TYPE_LABELS: dict[str, str] = {
    "main":     "🏆 Турнир Гвардиолыча",
    "fantasy":  "💎 Фэнтези",
    "vsa":      "🔥 VSA",
    "supercup": "🏅 Суперкубок",
}

# Russian word for "trophy" with three case forms (1 / 2-4 / 5+).
def _trophies_word(n: int) -> str:
    n_abs = abs(int(n))
    if n_abs % 100 in (11, 12, 13, 14):
        return "трофеев"
    last = n_abs % 10
    if last == 1:
        return "трофей"
    if last in (2, 3, 4):
        return "трофея"
    return "трофеев"


def _format_date(value: str | None) -> str:
    """``"2024-10-16T18:35:52"`` → ``"16.10.2024"``. Empty/garbage → ``"—"``."""
    if not value:
        return "—"
    s = str(value).strip()
    # Take just the date part — anything after T/space is time we don't show.
    s = re.split(r"[T ]", s, maxsplit=1)[0]
    try:
        dt = datetime.strptime(s, "%Y-%m-%d")
    except ValueError:
        return s
    return dt.strftime("%d.%m.%Y")


def _player_label(p: dict | None, *, fallback: str = "—") -> str:
    """One-line label for a player row used inside chat output.

    Prefers ``game_nickname`` (escaped); falls back to ``@username``;
    finally to ``fallback``. Synthetic ``id_<digits>`` placeholders are
    rendered as plain ``id 12345`` via :func:`mention`.
    """
    if not p:
        return fallback
    nick = (p.get("game_nickname") or "").strip()
    if nick:
        return html.escape(nick)
    return mention(p.get("username"))


def _post_link(url: str | None, label: str) -> str:
    """Wrap ``label`` as an HTML link to the channel post, or fall back
    to plain escaped text when no URL is known.
    """
    if not url:
        return html.escape(label)
    return f'<a href="{html.escape(url, quote=True)}">{html.escape(label)}</a>'


def _winner_summary_line(rec: dict) -> str:
    """One-line summary for a single winner record. Used by the
    chronology view.
    """
    date_label = _format_date(rec.get("tournament_date"))
    date_html = _post_link(rec.get("source_url"), date_label)
    bits: list[str] = [date_html]
    tnum = rec.get("tournament_number")
    if tnum:
        bits.append(f"#{tnum}")
    winner_label = _player_label(
        db.get_player_by_id(rec["winner_player_id"])
        if rec.get("winner_player_id") else None
    )
    line = " · ".join(bits) + f"  👑 {winner_label}"

    # Runner-up (main / vsa) or full podium (fantasy).
    if rec.get("tournament_type") == "fantasy":
        silver = (
            db.get_player_by_id(rec["fantasy_silver_player_id"])
            if rec.get("fantasy_silver_player_id") else None
        )
        bronze = (
            db.get_player_by_id(rec["fantasy_bronze_player_id"])
            if rec.get("fantasy_bronze_player_id") else None
        )
        cup = (
            db.get_player_by_id(rec["fantasy_cup_winner_player_id"])
            if rec.get("fantasy_cup_winner_player_id") else None
        )
        if silver:
            line += f"  🥈 {_player_label(silver)}"
        if bronze:
            line += f"  🥉 {_player_label(bronze)}"
        if cup:
            line += f"  🏅 {_player_label(cup)}"
    else:
        runner = (
            db.get_player_by_id(rec["runner_up_player_id"])
            if rec.get("runner_up_player_id") else None
        )
        if runner:
            line += f"  vs {_player_label(runner)}"
        score = rec.get("final_score")
        if score:
            line += f"  ({html.escape(score)})"
    return line


# ─────────────────────────────────────────────────────────────────────────────
# Inline keyboards
# ─────────────────────────────────────────────────────────────────────────────

def _kb_root() -> InlineKeyboardMarkup:
    """Top-level menu: pick a tournament type."""
    rows = [
        [InlineKeyboardButton(TYPE_LABELS["main"],     callback_data="chmp:type:main")],
        [InlineKeyboardButton(TYPE_LABELS["fantasy"],  callback_data="chmp:type:fantasy")],
        [InlineKeyboardButton(TYPE_LABELS["vsa"],      callback_data="chmp:type:vsa")],
        [InlineKeyboardButton(TYPE_LABELS["supercup"], callback_data="chmp:type:supercup")],
    ]
    return InlineKeyboardMarkup(rows)


def _kb_type(ttype: str) -> InlineKeyboardMarkup:
    """Submenu inside a tournament type — pick a view."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Топ по трофеям", callback_data=f"chmp:top:{ttype}")],
        [InlineKeyboardButton("📅 Хронология",     callback_data=f"chmp:hist:{ttype}:0")],
        [InlineKeyboardButton("🔍 По игроку",      callback_data=f"chmp:byp:{ttype}")],
        [InlineKeyboardButton("⬅️ В меню",         callback_data="chmp:menu")],
    ])


def _kb_back_to_type(ttype: str) -> InlineKeyboardMarkup:
    """Footer "back" button for any view inside a tournament type."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            f"⬅️ {TYPE_LABELS.get(ttype, ttype)}",
            callback_data=f"chmp:type:{ttype}",
        )],
        [InlineKeyboardButton("🏠 В меню", callback_data="chmp:menu")],
    ])


def _kb_history(ttype: str, page: int, total_pages: int) -> InlineKeyboardMarkup:
    """Pagination keyboard for the chronology view."""
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton(
            "◀️ Назад", callback_data=f"chmp:hist:{ttype}:{page - 1}"
        ))
    nav.append(InlineKeyboardButton(
        f"стр. {page + 1}/{total_pages}", callback_data="chmp:noop"
    ))
    if page + 1 < total_pages:
        nav.append(InlineKeyboardButton(
            "Вперёд ▶️", callback_data=f"chmp:hist:{ttype}:{page + 1}"
        ))
    rows: list[list[InlineKeyboardButton]] = [nav]
    rows.append([
        InlineKeyboardButton(
            f"⬅️ {TYPE_LABELS.get(ttype, ttype)}",
            callback_data=f"chmp:type:{ttype}",
        ),
    ])
    return InlineKeyboardMarkup(rows)


def _kb_byplayer_picker(ttype: str, top: list[dict]) -> InlineKeyboardMarkup:
    """For "by player" — show up to 10 buttons with the most-titled players."""
    rows: list[list[InlineKeyboardButton]] = []
    for r in top[:10]:
        pid = r["player_id"]
        label = (r.get("game_nickname") or r.get("username") or f"id={pid}").strip()
        # Telegram allows ≤64 chars in a button label — be defensive.
        if len(label) > 28:
            label = label[:27] + "…"
        rows.append([InlineKeyboardButton(
            f"{label} — {r['titles']} {_trophies_word(r['titles'])}",
            callback_data=f"chmp:byp_p:{ttype}:{pid}",
        )])
    rows.append([InlineKeyboardButton(
        f"⬅️ {TYPE_LABELS.get(ttype, ttype)}",
        callback_data=f"chmp:type:{ttype}",
    )])
    return InlineKeyboardMarkup(rows)


# ─────────────────────────────────────────────────────────────────────────────
# Render: type submenu (with quick stats)
# ─────────────────────────────────────────────────────────────────────────────

def _render_root_text() -> str:
    total = db.count_tournament_winner_records()
    if total == 0:
        return (
            "🏆 <b>Зал славы</b>\n\n"
            "Пока пусто. Админу: запусти <code>/import_champions</code>, "
            "чтобы импортировать чемпионов из дампа канала "
            "(<code>data/champions_parsed.json</code>)."
        )
    return (
        "🏆 <b>Зал славы</b>\n\n"
        f"Всего записей о турнирах: <b>{total}</b>\n\n"
        "Выбери, какой турнир посмотреть:"
    )


def _render_type_text(ttype: str) -> str:
    rows = db.list_tournament_winners(ttype)
    label = TYPE_LABELS.get(ttype, ttype)
    if not rows:
        return (
            f"<b>{label}</b>\n\n"
            "По этому турниру ещё нет данных. Админу: импортируй через "
            "<code>/import_champions</code> или добавь алиасы через "
            "<code>/alias</code> и переимпортируй."
        )
    by_winner: dict[int, int] = {}
    for r in rows:
        by_winner[r["winner_player_id"]] = by_winner.get(r["winner_player_id"], 0) + 1
    leader_id, leader_count = max(by_winner.items(), key=lambda x: x[1])
    leader = db.get_player_by_id(leader_id)
    leader_label = _player_label(leader)
    lines = [
        f"<b>{label}</b>",
        "",
        f"📈 Турниров в базе: <b>{len(rows)}</b>",
        f"👑 Уникальных чемпионов: <b>{len(by_winner)}</b>",
        f"🥇 Лидер по трофеям: <b>{leader_label}</b> "
        f"({leader_count} {_trophies_word(leader_count)})",
        "",
        "Что показать?",
    ]
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# Render: top by titles
# ─────────────────────────────────────────────────────────────────────────────

# How many rows to show in the "top" view (everything past this is just noise).
TOP_LIMIT = 25


def _render_top_text(ttype: str) -> str:
    label = TYPE_LABELS.get(ttype, ttype)
    rows = db.count_titles_by_type(ttype)
    if not rows:
        return f"<b>{label} — Топ чемпионов</b>\n\nПока никто.\n"
    lines = [f"<b>{label} — Топ чемпионов</b>", ""]
    medals = ("🥇", "🥈", "🥉")
    for i, r in enumerate(rows[:TOP_LIMIT], start=1):
        prefix = f"{medals[i - 1]} " if i <= 3 else f"{i:>2}. "
        p = {
            "id": r["player_id"],
            "username": r.get("username"),
            "game_nickname": r.get("game_nickname"),
        }
        name = _player_label(p)
        n = int(r["titles"])
        lines.append(f"{prefix}{name} — <b>{n}</b> {_trophies_word(n)}")
    if len(rows) > TOP_LIMIT:
        lines.append("")
        lines.append(f"… и ещё {len(rows) - TOP_LIMIT}")
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# Render: chronology (paginated)
# ─────────────────────────────────────────────────────────────────────────────

# How many records show up on a single chronology page. Tuned to keep the
# whole message comfortably under Telegram's 4096-char limit even when
# every record has runner-up + score + alias.
HIST_PAGE_SIZE = 10


def _render_history_text(ttype: str, page: int) -> tuple[str, int]:
    label = TYPE_LABELS.get(ttype, ttype)
    rows = db.list_tournament_winners(ttype)
    rows.reverse()  # newest first
    total_pages = max(1, (len(rows) + HIST_PAGE_SIZE - 1) // HIST_PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    if not rows:
        return f"<b>{label} — Хронология</b>\n\nПока никто.\n", total_pages
    start = page * HIST_PAGE_SIZE
    chunk = rows[start:start + HIST_PAGE_SIZE]
    lines = [f"<b>{label} — Хронология</b>", ""]
    for i, rec in enumerate(chunk, start=start + 1):
        lines.append(f"{i}. {_winner_summary_line(rec)}")
    return "\n".join(lines), total_pages


# ─────────────────────────────────────────────────────────────────────────────
# Render: per-player card
# ─────────────────────────────────────────────────────────────────────────────

def _render_player_card(player_id: int, ttype: str | None = None) -> str:
    p = db.get_player_by_id(player_id)
    if not p:
        return "❌ Игрок не найден."
    name = _player_label(p)
    head = f"👑 <b>{name}</b> — карточка чемпиона"
    if ttype:
        head += f" ({TYPE_LABELS.get(ttype, ttype)})"
    sections: list[str] = [head, ""]

    # Per-type breakdown. Always iterate all three so the user sees the
    # complete picture in /champion; the "by player" callback narrows to
    # one type by passing ``ttype``.
    iter_types = (ttype,) if ttype else db.TOURNAMENT_WINNER_TYPES

    any_data = False
    for tt in iter_types:
        wins = db.get_titles_for_player(player_id, tt)
        all_finals = db.get_finals_for_player(player_id, tt)
        non_wins = [r for r in all_finals if r["winner_player_id"] != player_id]

        if not wins and not non_wins:
            continue
        any_data = True
        sections.append(f"<b>{TYPE_LABELS.get(tt, tt)}</b>")

        if wins:
            sections.append(
                f"🏆 Чемпионств: {len(wins)} {_trophies_word(len(wins))}"
            )
            for w in wins:
                sections.append("  • " + _winner_summary_line(w))
        if non_wins:
            sections.append("")
            sections.append(f"🥈 Финалов проиграно / призовых мест: {len(non_wins)}")
            for r in non_wins:
                sections.append("  • " + _winner_summary_line(r))
        sections.append("")

    if not any_data:
        sections.append("Никаких чемпионств / финалов в базе пока нет.")
    return "\n".join(sections).rstrip()


# ─────────────────────────────────────────────────────────────────────────────
# Commands
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_champions(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """``/champions`` — open the inline-keyboard hall-of-fame browser."""
    await send(update, _render_root_text(), reply_markup=_kb_root())


async def cmd_champion(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """``/champion @user`` — combined per-player card across every type."""
    args = list(ctx.args or [])
    target_player: dict | None = None

    msg = update.effective_message
    if msg and msg.reply_to_message and msg.reply_to_message.from_user:
        u = msg.reply_to_message.from_user
        if u.username:
            target_player = db.get_player(u.username)
        if not target_player:
            target_player = db.get_player_by_telegram_id(u.id)

    if not target_player and args:
        target_player = _resolve_player_arg(args[0])

    if not target_player:
        await send(
            update,
            "Использование: <code>/champion @user</code> "
            "(или ответом на сообщение пользователя).\n\n"
            "Покажет все чемпионства и финалы этого игрока во всех турнирах.",
        )
        return
    text = _render_player_card(target_player["id"])
    await send(update, text)


# ─────────────────────────────────────────────────────────────────────────────
# /alias  (admin)
# ─────────────────────────────────────────────────────────────────────────────

def _parse_alias_args(message_text: str) -> list[str]:
    """Split the raw command text honouring quoted multi-word names.

    ``/alias add "Феникс" @phoenileo`` → ``["add", "Феникс", "@phoenileo"]``.
    ``shlex`` already does the right thing with quotes; we only need to
    strip the leading ``/alias`` token and tolerate ``shlex.split``
    raising ``ValueError`` on unmatched quotes.
    """
    text = (message_text or "").strip()
    # Drop the leading ``/alias`` (with optional ``@bot_username`` that
    # Telegram appends in group chats).
    text = re.sub(r"^/[A-Za-z_]\w*(?:@\w+)?\s*", "", text)
    if not text:
        return []
    try:
        return shlex.split(text, posix=True)
    except ValueError:
        # Fall back to whitespace split if quoting is broken — better
        # than crashing the handler.
        return text.split()


async def cmd_alias(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin-only alias management.

    Subcommands:
      ``/alias add "<name>" @user``     — register an alias
      ``/alias remove "<name>"``        — drop an alias
      ``/alias list [@user]``           — show all aliases (or one player's)

    The name MUST be quoted when it contains spaces or punctuation. Single
    words can be passed without quotes.
    """
    if not is_admin(update.effective_user.id):
        await send(update, "❌ Только админ.")
        return

    msg_text = (update.effective_message.text if update.effective_message else "") or ""
    parts = _parse_alias_args(msg_text)
    sub = parts[0].lower() if parts else ""

    if sub in ("add", "set", "+"):
        if len(parts) < 3:
            await send(
                update,
                "Использование: <code>/alias add \"Имя\" @user</code>\n\n"
                "Имя в кавычках, если содержит пробелы. "
                "<code>@user</code> — telegram username игрока, "
                "уже зарегистрированного в боте, или его telegram_id.",
            )
            return
        alias_text = parts[1]
        target = _resolve_player_arg(parts[2])
        if not target:
            await send(
                update,
                f"❌ Игрок {html.escape(parts[2])} не найден в боте. "
                "Сначала зарегистрируй его (или попроси сделать "
                "<code>/register</code>), потом задавай алиас.",
            )
            return
        try:
            inserted = db.add_player_alias(
                alias_text, target["id"],
                granted_by=update.effective_user.id,
            )
        except ValueError as e:
            await send(update, f"❌ {html.escape(str(e))}")
            return
        # Retroactively repoint already-imported tournament_winners rows.
        # When the user added the alias *after* /import_champions had
        # auto-created a placeholder player, the existing records still
        # pointed at that placeholder — leaving the leaderboard split
        # ('Freshl 6' + '@freshl66 4' instead of '@freshl66 10').
        # Consolidate them now so the merge is visible immediately.
        try:
            moved, orphan_ids = db.consolidate_winner_records_for_alias(
                alias_text, target["id"],
            )
        except Exception as e:
            log.warning("alias consolidate failed: %s", e)
            moved, orphan_ids = 0, []

        verb = "добавлен" if inserted else "уже был назначен"
        lines = [
            f"✅ Алиас <b>{html.escape(alias_text)}</b> {verb} → "
            f"{mention(target['username'])} (id={target['id']}).",
        ]
        if moved:
            lines.append("")
            lines.append(
                f"🔁 Переподвязал <b>{moved}</b> запис"
                + ("ь" if moved == 1 else ("и" if 2 <= moved <= 4 else "ей"))
                + f" с плейсхолдер-игрока"
                + ("а" if len(orphan_ids) == 1 else "ов")
                + f" (id="
                + ", ".join(str(i) for i in orphan_ids)
                + f") на {mention(target['username'])}."
            )
            lines.append(
                "ℹ️ Плейсхолдер-игрок остался в БД с 0 трофеями. "
                "Если хочешь полностью слить — <code>/relink_player</code>."
            )
        await send(update, "\n".join(lines))
        return

    if sub in ("remove", "rm", "del", "delete", "-"):
        if len(parts) < 2:
            await send(update, "Использование: <code>/alias remove \"Имя\"</code>")
            return
        alias_text = parts[1]
        if db.remove_player_alias(alias_text):
            await send(
                update,
                f"✅ Алиас <b>{html.escape(alias_text)}</b> удалён.",
            )
        else:
            await send(
                update,
                f"⚠️ Алиас <b>{html.escape(alias_text)}</b> не найден.",
            )
        return

    if sub in ("list", "ls", ""):
        target_player_id: int | None = None
        if len(parts) >= 2:
            target = _resolve_player_arg(parts[1])
            if not target:
                await send(update, f"❌ Игрок {html.escape(parts[1])} не найден.")
                return
            target_player_id = target["id"]
        rows = db.list_player_aliases(target_player_id)
        if not rows:
            if target_player_id:
                await send(update, "У этого игрока нет алиасов.")
            else:
                await send(
                    update,
                    "Алиасов пока нет. Добавь через "
                    "<code>/alias add \"Имя\" @user</code>.",
                )
            return
        lines = [f"📒 Всего алиасов: <b>{len(rows)}</b>", ""]
        # Group by player for readability.
        by_player: dict[int, list[dict]] = {}
        for r in rows:
            by_player.setdefault(r["player_id"], []).append(r)
        for pid, items in by_player.items():
            who = mention(items[0]["username"])
            nick = (items[0].get("game_nickname") or "").strip()
            head = f"<b>{who}</b>"
            if nick:
                head += f" ({html.escape(nick)})"
            lines.append(head)
            for r in items:
                lines.append(f"  • {html.escape(r['alias'])}")
            lines.append("")
        await send(update, "\n".join(lines).rstrip())
        return

    await send(
        update,
        "Использование:\n"
        "  <code>/alias add \"Имя\" @user</code> — добавить алиас\n"
        "  <code>/alias remove \"Имя\"</code> — удалить алиас\n"
        "  <code>/alias list [@user]</code> — список алиасов",
    )


# ─────────────────────────────────────────────────────────────────────────────
# /import_champions  (admin)
# ─────────────────────────────────────────────────────────────────────────────

DEFAULT_CHAMPIONS_JSON = "data/champions_parsed.json"
DEFAULT_ALIASES_JSON = "data/aliases_to_review.json"


def _resolve_name_to_player_id(
    *,
    raw_name: str | None,
    username: str | None,
    alias: str | None = None,
) -> int | None:
    """Best-effort name → ``players.id`` lookup used by the importer.

    Lookup order, cheapest-and-most-precise first:
      1. ``@username`` → ``players.username`` (lower-case exact).
      2. ``raw_name`` / ``alias`` → ``player_aliases.alias``.
      3. ``raw_name`` / ``alias`` → ``players.game_nickname`` (case-insensitive).
      4. ``raw_name`` / ``alias`` → ``players.username`` (rare, but handles
         channel posts that wrote the username without an ``@``).

    Returns the resolved id or ``None``. Never raises — caller treats
    ``None`` as "couldn't resolve".
    """
    if username:
        p = db.get_player(username)
        if p:
            return p["id"]
    for cand in (raw_name, alias):
        if not cand:
            continue
        c = cand.strip()
        if not c:
            continue
        pid = db.resolve_alias_to_player_id(c)
        if pid:
            return pid
        p = db.get_player_by_game_nickname(c)
        if p:
            return p["id"]
        p = db.get_player(c)
        if p:
            return p["id"]
    return None


def _resolve_or_create_player(
    *,
    raw_name: str | None,
    username: str | None,
    alias: str | None = None,
    created_ids: set[int],
) -> int | None:
    """Resolve a name to a ``players.id``; create a placeholder row when
    no match exists.

    Used by ``/import_champions`` so a one-shot import absorbs every
    winner / runner-up / podium reference in the channel dump even if
    those people aren't yet registered in the bot. Auto-created records
    use the name as the synthetic ``players.username`` (lower-cased
    internally by ``upsert_player``) and the original capitalisation as
    ``game_nickname`` for display. The user is expected to merge
    duplicates afterwards via ``/relink_player`` or by mapping aliases
    through ``/alias`` and re-running the import.

    Adds any newly created id to ``created_ids`` so the caller can
    surface a count to chat.
    """
    pid = _resolve_name_to_player_id(
        raw_name=raw_name, username=username, alias=alias,
    )
    if pid is not None:
        return pid
    # Pick what to use as the synthetic key. Prefer the @-handle when
    # present (real Telegram usernames are stable), then the raw name,
    # then the parenthesised alias.
    target = ""
    nick: str | None = None
    if username:
        target = username.strip()
    elif raw_name:
        target = raw_name.strip()
        nick = target  # remember original capitalisation for display
    elif alias:
        target = alias.strip()
        nick = target
    if not target:
        return None
    # ``upsert_player`` lowercases the username internally, so re-creation
    # of e.g. "Антон" / "антон" / "АНТОН" all converge on one row.
    existing = db.get_player(target.lower())
    if existing:
        return existing["id"]
    p = db.upsert_player(target)
    new_id = int(p["id"])
    created_ids.add(new_id)
    if nick and not (p.get("game_nickname") or "").strip():
        try:
            db.set_game_nickname(new_id, nick)
        except Exception:
            log.warning(
                "set_game_nickname failed for new player %s (%r)", new_id, nick,
            )
    return new_id


def _resolve_or_create_simple(
    person: dict | None,
    *,
    created_ids: set[int],
) -> int | None:
    """Wrapper for runner-up / fantasy podium / cup-winner sub-objects."""
    if not person:
        return None
    return _resolve_or_create_player(
        raw_name=person.get("raw_name"),
        username=person.get("username"),
        created_ids=created_ids,
    )


def _resolve_simple(person: dict | None) -> int | None:
    """Resolve-only helper (no creation). Kept for the rare cases where
    we want to know if a person is already registered without creating
    a placeholder.
    """
    if not person:
        return None
    return _resolve_name_to_player_id(
        raw_name=person.get("raw_name"),
        username=person.get("username"),
    )


def _autoapply_aliases_file(path: Path, granted_by: int | None) -> int:
    """Read ``aliases_to_review.json`` and register every alias whose
    ``suggested_username`` field is filled in. Returns the number of
    new aliases that were actually inserted (already-existing identical
    mappings are no-ops).
    """
    if not path.is_file():
        return 0
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        log.warning("aliases JSON unreadable (%s): %s", path, e)
        return 0
    if not isinstance(data, list):
        return 0
    inserted = 0
    for entry in data:
        if not isinstance(entry, dict):
            continue
        alias_name = (entry.get("name") or "").strip()
        suggested = (entry.get("suggested_username") or "").strip()
        if not alias_name or not suggested:
            continue
        target = _resolve_player_arg(suggested)
        if not target:
            log.info(
                "alias autoapply skipped: %r → %r (player not in DB)",
                alias_name, suggested,
            )
            continue
        try:
            if db.add_player_alias(alias_name, target["id"], granted_by=granted_by):
                inserted += 1
        except ValueError as e:
            log.info("alias autoapply skipped: %r — %s", alias_name, e)
    return inserted


async def cmd_import_champions(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """``/import_champions [path]`` — bulk-import the parsed JSON.

    Default path is ``data/champions_parsed.json``. The companion
    ``data/aliases_to_review.json`` is auto-loaded if present, and any
    entry with a filled ``suggested_username`` is registered as an
    alias before the main import pass.

    Idempotent: re-running updates existing rows in place
    (``UNIQUE(tournament_type, source_message_id)``).
    """
    if not is_admin(update.effective_user.id):
        await send(update, "❌ Только админ.")
        return

    args = list(ctx.args or [])
    json_path = Path(args[0] if args else DEFAULT_CHAMPIONS_JSON)
    if not json_path.is_file():
        await send(
            update,
            f"❌ Не нашёл файл <code>{html.escape(str(json_path))}</code>.\n\n"
            "Положи дамп канала в <code>result.json</code> и запусти "
            "<code>python scripts/parse_gvardiol_dump.py</code>, "
            "после этого пробуй снова.",
        )
        return

    aliases_path = json_path.with_name(os.path.basename(DEFAULT_ALIASES_JSON))
    granted_by = update.effective_user.id
    auto_aliases = _autoapply_aliases_file(aliases_path, granted_by=granted_by)

    try:
        with json_path.open("r", encoding="utf-8") as f:
            payload = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        await send(update, f"❌ Не смог прочитать JSON: {html.escape(str(e))}")
        return

    records = payload.get("tournaments") or []
    if not records:
        await send(update, "JSON пустой — нечего импортировать.")
        return

    before = db.count_tournament_winner_records()
    created_player_ids: set[int] = set()

    imported = 0
    updated = 0
    skipped_no_winner: list[dict] = []
    skipped_other: list[dict] = []
    succeeded_partial: list[str] = []  # winner found, runner-up not (legacy resolve-only)

    for rec in records:
        ttype = rec.get("tournament_type")
        if ttype not in db.TOURNAMENT_WINNER_TYPES:
            skipped_other.append({"msg_id": rec.get("msg_id"), "reason": f"unknown type {ttype!r}"})
            continue
        msg_id = rec.get("msg_id")
        url = rec.get("url") or ""
        if msg_id is None or not url:
            skipped_other.append({"msg_id": msg_id, "reason": "missing msg_id / url"})
            continue
        date = rec.get("date") or ""

        if ttype == "fantasy":
            podium = rec.get("podium") or {}
            winner_pid = _resolve_or_create_simple(
                podium.get("winner"), created_ids=created_player_ids,
            )
            if not winner_pid:
                # No name material at all (raw_name + username both empty)
                skipped_no_winner.append(rec)
                continue
            silver_pid = _resolve_or_create_simple(
                podium.get("silver"), created_ids=created_player_ids,
            )
            bronze_pid = _resolve_or_create_simple(
                podium.get("bronze"), created_ids=created_player_ids,
            )
            cup_pid = _resolve_or_create_simple(
                podium.get("cup_winner"), created_ids=created_player_ids,
            )

            existing_id = _existing_winner_id(ttype, msg_id)
            db.add_tournament_winner(
                tournament_type="fantasy",
                tournament_date=date[:10] if date else None,
                tournament_number=None,
                winner_player_id=winner_pid,
                runner_up_player_id=None,
                fantasy_silver_player_id=silver_pid,
                fantasy_bronze_player_id=bronze_pid,
                fantasy_cup_winner_player_id=cup_pid,
                final_score=None,
                championship_count=None,
                source_message_id=int(msg_id),
                source_url=url,
                notes=None,
            )
            if existing_id is not None:
                updated += 1
            else:
                imported += 1
            continue

        # main / vsa
        winner = rec.get("winner") or {}
        runner = rec.get("runner_up") or {}
        winner_pid = _resolve_or_create_player(
            raw_name=winner.get("raw_name"),
            username=winner.get("username"),
            alias=winner.get("alias"),
            created_ids=created_player_ids,
        )
        if not winner_pid:
            # Truly no name material — record can't be saved.
            skipped_no_winner.append(rec)
            continue
        runner_pid = _resolve_or_create_simple(
            runner, created_ids=created_player_ids,
        )
        existing_id = _existing_winner_id(ttype, msg_id)
        db.add_tournament_winner(
            tournament_type=ttype,
            tournament_date=date[:10] if date else None,
            tournament_number=rec.get("tournament_number"),
            winner_player_id=winner_pid,
            runner_up_player_id=runner_pid,
            final_score=rec.get("final_score"),
            championship_count=rec.get("championship_count"),
            source_message_id=int(msg_id),
            source_url=url,
            notes=None,
        )
        if existing_id is not None:
            updated += 1
        else:
            imported += 1

    after = db.count_tournament_winner_records()

    # Build the summary message. Keep it compact — full per-record reasons
    # for skipped entries go to the bot log; chat just shows counts and
    # the first few examples.
    head_lines = [
        "📥 <b>Импорт чемпионов</b>",
        f"Источник: <code>{html.escape(str(json_path))}</code>",
        "",
        f"Алиасов авто-применено:    <b>{auto_aliases}</b>",
        f"Импортировано новых записей: <b>{imported}</b>",
        f"Обновлено существующих:     <b>{updated}</b>",
        f"Создано игроков на лету:    <b>{len(created_player_ids)}</b>",
    ]
    if skipped_no_winner:
        head_lines.append(
            f"Пропущено (нет ни имени, ни @):  <b>{len(skipped_no_winner)}</b>"
        )
    if skipped_other:
        head_lines.append(f"Пропущено по другой причине:  <b>{len(skipped_other)}</b>")
    head_lines.append("")
    head_lines.append(f"Записей в БД: было {before}, стало {after}.")
    if created_player_ids:
        head_lines.append("")
        head_lines.append(
            "ℹ️ Среди новых игроков почти наверняка есть дубли "
            "(одно и то же лицо под разными именами / падежами / "
            "написаниями). Слить дубликаты можно через "
            "<code>/alias add \"Имя\" @realuser</code> + повторный "
            "<code>/import_champions</code>, либо через "
            "<code>/relink_player</code>."
        )

    sections = ["\n".join(head_lines)]

    if skipped_no_winner:
        ex_lines = ["", "<b>Пропущенные (нет имени совсем):</b>"]
        for rec in skipped_no_winner[:10]:
            ttype = rec.get("tournament_type", "?")
            url = rec.get("url", "")
            ex_lines.append(
                f"• {_post_link(url, str(rec.get('msg_id')))} ({ttype})"
            )
        sections.append("\n".join(ex_lines))

    if succeeded_partial:
        sections.append(
            "\n<b>Финалисты не распознаны (записаны без них):</b>\n"
            + "\n".join(f"• {html.escape(p)}" for p in succeeded_partial[:10])
        )

    await send(update, "\n".join(sections))


def _existing_winner_id(ttype: str, msg_id: int | None) -> int | None:
    """Tiny convenience wrapper used only by the importer to decide
    whether the upsert is going to be an INSERT or an UPDATE so the
    summary can show "imported" vs "updated" counts."""
    if msg_id is None:
        return None
    conn = db.get_conn()
    row = conn.execute(
        "SELECT id FROM tournament_winners "
        "WHERE tournament_type=? AND source_message_id=?",
        (ttype, int(msg_id)),
    ).fetchone()
    conn.close()
    if not row:
        return None
    return int(row["id"] if isinstance(row, dict) or hasattr(row, "keys") else row[0])


# ─────────────────────────────────────────────────────────────────────────────
# Callback handler — dispatched from bot.callback_handler on prefix "chmp:"
# ─────────────────────────────────────────────────────────────────────────────

async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Single entry point for every ``chmp:*`` callback.

    Routes:
      ``chmp:menu``                    — root tournament-type picker
      ``chmp:type:<ttype>``            — submenu for a tournament type
      ``chmp:top:<ttype>``             — top-by-titles view
      ``chmp:hist:<ttype>:<page>``     — chronology page
      ``chmp:byp:<ttype>``             — by-player picker (top-10 buttons)
      ``chmp:byp_p:<ttype>:<pid>``     — per-player card scoped to one type
      ``chmp:noop``                    — no-op (used by the page-counter button)
    """
    query = update.callback_query
    if not query:
        return
    data = (query.data or "").strip()
    parts = data.split(":")

    try:
        if data == "chmp:noop":
            return
        if data == "chmp:menu":
            await query.edit_message_text(
                _render_root_text(),
                parse_mode="HTML",
                reply_markup=_kb_root(),
            )
            return
        if len(parts) >= 3 and parts[1] == "type":
            ttype = parts[2]
            if ttype not in db.TOURNAMENT_WINNER_TYPES:
                return
            await query.edit_message_text(
                _render_type_text(ttype),
                parse_mode="HTML",
                reply_markup=_kb_type(ttype),
            )
            return
        if len(parts) >= 3 and parts[1] == "top":
            ttype = parts[2]
            if ttype not in db.TOURNAMENT_WINNER_TYPES:
                return
            await query.edit_message_text(
                _render_top_text(ttype),
                parse_mode="HTML",
                reply_markup=_kb_back_to_type(ttype),
            )
            return
        if len(parts) >= 3 and parts[1] == "hist":
            ttype = parts[2]
            if ttype not in db.TOURNAMENT_WINNER_TYPES:
                return
            page = int(parts[3]) if len(parts) >= 4 and parts[3].isdigit() else 0
            text, total_pages = _render_history_text(ttype, page)
            await query.edit_message_text(
                text,
                parse_mode="HTML",
                reply_markup=_kb_history(ttype, page, total_pages),
            )
            return
        if len(parts) >= 3 and parts[1] == "byp":
            ttype = parts[2]
            if ttype not in db.TOURNAMENT_WINNER_TYPES:
                return
            top = db.count_titles_by_type(ttype)
            label = TYPE_LABELS.get(ttype, ttype)
            if not top:
                await query.edit_message_text(
                    f"<b>{label} — По игроку</b>\n\nВ базе пока никого.",
                    parse_mode="HTML",
                    reply_markup=_kb_back_to_type(ttype),
                )
                return
            await query.edit_message_text(
                f"<b>{label} — По игроку</b>\n\n"
                "Выбери игрока из топ-10 или используй "
                "<code>/champion @user</code> для любого другого.",
                parse_mode="HTML",
                reply_markup=_kb_byplayer_picker(ttype, top),
            )
            return
        if len(parts) >= 4 and parts[1] == "byp_p":
            ttype = parts[2]
            if ttype not in db.TOURNAMENT_WINNER_TYPES:
                return
            try:
                pid = int(parts[3])
            except ValueError:
                return
            text = _render_player_card(pid, ttype=ttype)
            await query.edit_message_text(
                text,
                parse_mode="HTML",
                reply_markup=_kb_back_to_type(ttype),
            )
            return
    except TelegramError as e:
        # "Message is not modified" pops up if the user spams the same
        # button — quietly ignore, anything else is logged.
        if "not modified" in str(e).lower():
            return
        log.warning("champions callback %r failed: %s", data, e)
        return


# ─────────────────────────────────────────────────────────────────────────────
# /rename_champion  /add_trophy  /list_trophies  /remove_trophy   (admin)
# ─────────────────────────────────────────────────────────────────────────────
#
# These four commands let an admin curate the Hall-of-Fame leaderboard
# by hand without re-running the importer:
#
#   * ``/rename_champion <player> <NewNick>`` — fix the displayed name
#     (e.g. an auto-created placeholder like ``Paqrez001`` becomes the
#     person's real in-game tag). Wraps :func:`db.set_game_nickname`.
#
#   * ``/add_trophy <player> [type] [date] [#N] [score]`` — manually
#     award a trophy (creates a fresh ``tournament_winners`` row with a
#     synthetic negative ``source_message_id`` so it doesn't collide
#     with imported rows). Defaults: type=``main``, date=today.
#
#   * ``/list_trophies <player>`` — print the player's trophies with
#     their internal record ids, so the admin knows what to feed
#     ``/remove_trophy``.
#
#   * ``/remove_trophy <id>`` — delete one specific trophy by id.
#
# All four are admin-only. None of them touches `players.elo`, the
# active-tournament tables, or any match data — they live entirely
# inside ``tournament_winners`` + ``players.game_nickname``.

# Tokens admins can pass to spell out today's date.
_TODAY_TOKENS = frozenset({"today", "сегодня", "now"})

# Tournament-type aliases (so admins can type "гвардиолыч" / "fantasy").
_TYPE_ALIASES: dict[str, str] = {
    "main": "main", "g": "main", "guardiola": "main", "гвардиолыч": "main",
    "fantasy": "fantasy", "f": "fantasy", "fant": "fantasy",
    "фэнтези": "fantasy", "фентези": "fantasy", "фэнтэзи": "fantasy",
    "vsa": "vsa",
    "supercup": "supercup", "super": "supercup", "lg": "supercup",
    "суперкубок": "supercup", "lgcup": "supercup", "lg_cup": "supercup",
    "minicup": "supercup", "мини-кубок": "supercup", "мини_кубок": "supercup",
}

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_SCORE_RE = re.compile(r"^\d{1,2}\s*[:\-]\s*\d{1,2}$")
_TNUM_RE = re.compile(r"^#(\d{1,3})$")


def _normalise_score(token: str) -> str:
    """``"3-1"`` → ``"3:1"``; ``"3:1"`` → ``"3:1"``."""
    return token.replace("-", ":").replace(" ", "")


def _next_manual_msg_id(tournament_type: str) -> int:
    """Pick the next available negative ``source_message_id`` for this
    tournament type so manual entries never collide with channel-imported
    ones (which always have positive Telegram message ids).
    """
    conn = db.get_conn()
    row = conn.execute(
        "SELECT MIN(source_message_id) AS min_id FROM tournament_winners "
        "WHERE tournament_type=?",
        (tournament_type,),
    ).fetchone()
    conn.close()
    min_id: int | None = None
    if row is not None:
        try:
            raw = row["min_id"]
        except (KeyError, IndexError, TypeError):
            raw = row[0] if row else None
        if raw is not None:
            try:
                min_id = int(raw)
            except (TypeError, ValueError):
                min_id = None
    base = min_id if (min_id is not None and min_id < 0) else 0
    return base - 1


async def cmd_rename_champion(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """``/rename_champion <player> <NewDisplayName>`` — admin-only.

    Sets ``players.game_nickname`` for the resolved player. The leaderboard
    in ``/champions`` reads ``game_nickname`` first, so the change takes
    effect on the next open. Player is resolved via ``_resolve_player_arg``
    (so ``@user``, numeric Telegram ID, or current display nick all work);
    if you want to look one up by their internal players.id, prefix with
    ``id=`` (e.g. ``id=42``).

    For mass rename (multiple records pointing at the same person),
    consider ``/relink_player`` instead — that *merges* two player rows
    rather than just relabelling one.
    """
    if not is_admin(update.effective_user.id):
        await send(update, "❌ Только админ.")
        return

    args = list(ctx.args or [])
    if len(args) < 2:
        await send(
            update,
            "Использование: <code>/rename_champion &lt;игрок&gt; &lt;Новый ник&gt;</code>\n\n"
            "Меняет отображаемый ник игрока в зале славы.\n\n"
            "Примеры:\n"
            "  <code>/rename_champion @freshl66 Фрешл</code>\n"
            "  <code>/rename_champion Paqrez001 Илья</code>\n"
            "  <code>/rename_champion id=42 Антон</code>\n\n"
            "ℹ️ Объединить два дубля в одного — <code>/relink_player</code>.",
        )
        return

    raw_target = args[0]
    new_nick = " ".join(args[1:]).strip()
    if not new_nick:
        await send(update, "❌ Не указан новый ник.")
        return
    if len(new_nick) > 64:
        await send(update, "❌ Слишком длинный ник (макс. 64 символа).")
        return

    target: dict | None = None
    if raw_target.lower().startswith("id="):
        try:
            pid = int(raw_target.split("=", 1)[1])
        except ValueError:
            pid = -1
        if pid > 0:
            target = db.get_player_by_id(pid)
    if not target:
        target = _resolve_player_arg(raw_target)
    if not target:
        await send(
            update,
            f"❌ Не нашёл игрока «<code>{html.escape(raw_target)}</code>».\n\n"
            "Можно указать @username, числовой Telegram ID, "
            "текущий отображаемый ник, или <code>id=&lt;players.id&gt;</code>.",
        )
        return

    # Don't let one rename collide with another player's already-set nick.
    existing = db.get_player_by_game_nickname(new_nick)
    if existing and int(existing["id"]) != int(target["id"]):
        await send(
            update,
            f"❌ Ник <b>{html.escape(new_nick)}</b> уже занят игроком "
            f"{mention(existing['username'])} (id=<code>{existing['id']}</code>).\n"
            "Если это один и тот же человек — слей записи через "
            "<code>/relink_player</code>.",
        )
        return

    old_nick = (target.get("game_nickname") or "").strip() or "—"
    db.set_game_nickname(int(target["id"]), new_nick)

    await send(
        update,
        "✅ Ник обновлён.\n"
        f"Игрок: {mention(target['username'])} "
        f"(id=<code>{target['id']}</code>)\n"
        f"  было:  <b>{html.escape(old_nick)}</b>\n"
        f"  стало: <b>{html.escape(new_nick)}</b>\n\n"
        "Открой <code>/champions</code> — список обновится с новым ником.",
    )


def _parse_add_trophy_args(rest: list[str]) -> tuple[str, str | None, int | None, str | None, list[str]]:
    """Pull optional ``[type] [date] [#N] [score]`` tokens from ``rest``
    in any order. Returns ``(ttype, date_iso, tnum, score, leftovers)``;
    leftovers go into ``notes`` so admins can leave a free-form comment.
    """
    ttype = "main"
    ttype_set = False
    date_iso: str | None = None
    tnum: int | None = None
    score: str | None = None
    leftovers: list[str] = []

    for tok in rest:
        t = tok.strip()
        if not t:
            continue
        low = t.lower()

        # Tournament type (only the first match wins; rest become notes).
        if not ttype_set and low in _TYPE_ALIASES:
            ttype = _TYPE_ALIASES[low]
            ttype_set = True
            continue

        # Date.
        if date_iso is None and low in _TODAY_TOKENS:
            date_iso = datetime.utcnow().strftime("%Y-%m-%d")
            continue
        if date_iso is None and _DATE_RE.match(t):
            try:
                datetime.strptime(t, "%Y-%m-%d")
                date_iso = t
                continue
            except ValueError:
                pass  # fall through, becomes part of notes

        # Tournament number ``#N``.
        if tnum is None:
            m = _TNUM_RE.match(t)
            if m:
                tnum = int(m.group(1))
                continue

        # Score ``X:Y`` / ``X-Y``.
        if score is None and _SCORE_RE.match(t):
            score = _normalise_score(t)
            continue

        leftovers.append(tok)

    return ttype, date_iso, tnum, score, leftovers


async def cmd_add_trophy(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """``/add_trophy <player> [main|fantasy|vsa|supercup] [YYYY-MM-DD|today] [#N] [X:Y] [notes…]``

    Admin-only. Adds a manual trophy to the Hall of Fame for ``<player>``.
    Defaults: type ``main`` and ``date=today``. Tournament number and
    final score are optional and order-independent. Anything that doesn't
    parse as one of those tokens becomes the row's free-form ``notes``.
    """
    if not is_admin(update.effective_user.id):
        await send(update, "❌ Только админ.")
        return

    args = list(ctx.args or [])
    if not args:
        await send(
            update,
            "Использование: <code>/add_trophy &lt;игрок&gt; "
            "[main|fantasy|vsa|supercup] [YYYY-MM-DD|today] [#N] [X:Y] [заметки]</code>\n\n"
            "Примеры:\n"
            "  <code>/add_trophy @freshl66</code> — main, сегодня\n"
            "  <code>/add_trophy Paqrez001 main 2024-08-27 #3</code>\n"
            "  <code>/add_trophy @nurstw fantasy 2026-05-30</code>\n"
            "  <code>/add_trophy @user main 2025-04-01 #42 3:1 ручная правка</code>\n\n"
            "ℹ️ Записать алиас «Имя → @user» — <code>/alias add</code>.\n"
            "Удалить трофей — <code>/list_trophies @user</code> "
            "+ <code>/remove_trophy &lt;id&gt;</code>.",
        )
        return

    raw_target, *rest = args
    target = _resolve_player_arg(raw_target)
    if not target:
        await send(
            update,
            f"❌ Не нашёл игрока «<code>{html.escape(raw_target)}</code>».\n\n"
            "Можно указать @username, числовой Telegram ID, или "
            "его отображаемый ник.",
        )
        return

    ttype, date_iso, tnum, score, leftovers = _parse_add_trophy_args(rest)
    if date_iso is None:
        date_iso = datetime.utcnow().strftime("%Y-%m-%d")

    by = "@" + (update.effective_user.username or str(update.effective_user.id))
    notes_text = " ".join(leftovers).strip()
    note_full = f"manual: added by {by} on {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}"
    if notes_text:
        note_full += f" — {notes_text}"

    msg_id = _next_manual_msg_id(ttype)
    try:
        new_id = db.add_tournament_winner(
            tournament_type=ttype,
            tournament_date=date_iso,
            tournament_number=tnum,
            winner_player_id=int(target["id"]),
            runner_up_player_id=None,
            final_score=score,
            championship_count=None,
            source_message_id=msg_id,
            source_url="",
            notes=note_full,
        )
    except ValueError as e:
        await send(update, f"❌ {html.escape(str(e))}")
        return

    label = TYPE_LABELS.get(ttype, ttype)
    titles_after = len(db.get_titles_for_player(int(target["id"]), ttype))
    word = _trophies_word(titles_after)

    bits = [
        "🏆 <b>Трофей добавлен</b>",
        "",
        f"Игрок: {_player_label(target)} "
        f"(id=<code>{target['id']}</code>)",
        f"Турнир: {label}",
        f"Дата:   <b>{_format_date(date_iso)}</b>",
    ]
    if tnum:
        bits.append(f"Номер:  #{tnum}")
    if score:
        bits.append(f"Счёт:   <b>{html.escape(score)}</b>")
    bits.append("")
    bits.append(f"Внутренний id записи: <code>{new_id}</code>")
    bits.append(
        f"Всего у игрока в «{label}»: <b>{titles_after}</b> {word}"
    )
    bits.append("")
    bits.append(
        "Удалить эту запись: <code>/remove_trophy "
        f"{new_id}</code>."
    )
    if notes_text:
        bits.append("")
        bits.append(f"Заметка: {html.escape(notes_text)}")
    await send(update, "\n".join(bits))


async def cmd_list_trophies(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """``/list_trophies <player>`` — admin-only debug helper.

    Lists all trophies for ``<player>`` together with their internal
    record ids and tournament types, so the admin can pass the right id
    to ``/remove_trophy``. Unlike ``/champion``, this view *only* shows
    rows where the player is the winner (silver / bronze / runner-up
    placements aren't deletable as "trophies").
    """
    if not is_admin(update.effective_user.id):
        await send(update, "❌ Только админ.")
        return

    args = list(ctx.args or [])
    if not args:
        await send(
            update,
            "Использование: <code>/list_trophies &lt;игрок&gt;</code>\n\n"
            "Покажет все его трофеи с внутренними ID — их можно "
            "скармливать <code>/remove_trophy &lt;id&gt;</code>.",
        )
        return

    target = _resolve_player_arg(args[0])
    if not target:
        await send(
            update,
            f"❌ Не нашёл игрока «<code>{html.escape(args[0])}</code>».",
        )
        return

    rows = db.get_titles_for_player(int(target["id"]))
    if not rows:
        await send(
            update,
            f"У {_player_label(target)} нет трофеев в зале славы.",
        )
        return

    lines = [
        f"🏆 Трофеи: {_player_label(target)} "
        f"(id=<code>{target['id']}</code>) — всего <b>{len(rows)}</b>",
        "",
    ]
    for r in rows:
        rec_id = r.get("id")
        ttype = r.get("tournament_type") or "?"
        label = TYPE_LABELS.get(ttype, ttype)
        date_label = _format_date(r.get("tournament_date"))
        date_html = _post_link(r.get("source_url"), date_label)
        bits: list[str] = [f"#<code>{rec_id}</code>", label, date_html]
        tnum = r.get("tournament_number")
        if tnum:
            bits.append(f"тур #{tnum}")
        score = r.get("final_score")
        if score:
            bits.append(f"({html.escape(str(score))})")
        notes = (r.get("notes") or "").strip()
        if notes.startswith("manual:"):
            bits.append("✏️ ручная")
        lines.append("• " + " · ".join(bits))
    lines.append("")
    lines.append(
        "Удалить запись: <code>/remove_trophy &lt;id&gt;</code> "
        "(id из этого списка)."
    )
    await send(update, "\n".join(lines))


async def cmd_remove_trophy(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """``/remove_trophy <id>`` — admin-only.

    Deletes one ``tournament_winners`` row by primary-key id. The id
    comes from ``/list_trophies`` (or from the success message of
    ``/add_trophy``). The deletion is final — re-running the importer
    later will re-create channel-sourced rows, but manually-added
    trophies have no source post and would have to be re-entered by
    hand.
    """
    if not is_admin(update.effective_user.id):
        await send(update, "❌ Только админ.")
        return

    args = list(ctx.args or [])
    if not args or not args[0].lstrip("-").isdigit():
        await send(
            update,
            "Использование: <code>/remove_trophy &lt;id&gt;</code>\n\n"
            "ID — внутренний id записи из <code>/list_trophies @user</code>.",
        )
        return

    rec_id = int(args[0])
    rec = db.get_tournament_winner_by_id(rec_id)
    if not rec:
        await send(update, f"❌ Записи с id=<code>{rec_id}</code> нет.")
        return

    winner_pid = rec.get("winner_player_id")
    winner = db.get_player_by_id(int(winner_pid)) if winner_pid else None
    ttype = rec.get("tournament_type") or "?"
    label = TYPE_LABELS.get(ttype, ttype)
    date_label = _format_date(rec.get("tournament_date"))

    if not db.delete_tournament_winner(rec_id):
        await send(
            update,
            f"⚠️ Не удалось удалить запись id=<code>{rec_id}</code> "
            "(возможно, удалена параллельно).",
        )
        return

    titles_left = (
        len(db.get_titles_for_player(int(winner_pid), ttype))
        if winner_pid else 0
    )
    word = _trophies_word(titles_left)
    await send(
        update,
        "🗑 <b>Трофей удалён</b>\n\n"
        f"Запись: <code>{rec_id}</code> — {label}, {date_label}\n"
        f"Игрок:  {_player_label(winner)}\n\n"
        f"Осталось у игрока в «{label}»: <b>{titles_left}</b> {word}.",
    )
