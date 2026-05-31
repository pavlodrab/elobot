"""Cross-cutting helpers shared by every domain handler module.

These are leaf functions: they read DB rows, do small bits of formatting,
and return values. They never schedule background work, never touch
module-level mutable state in ``bot.py``, and never import from
``bot.py`` (so handler modules can use them without circular imports).

Phase 2 of the bot.py split moved these out of ``bot.py``; ``bot.py``
re-exports them so existing call-sites keep working.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional, Tuple

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.ext import ContextTypes

import database as db
from database import (
    get_active_tournament,
    get_player,
    get_player_by_game_nickname,
    get_player_by_id,
    get_player_by_telegram_id,
    get_tournament,
    get_tournament_by_chat,
    is_tournament_admin,
)

from handlers.common import is_admin, is_root_admin, log, t_full_label


# ── Stage-name localisation (used by /playoff, /my_deadlines, /tlog …) ───────

_STAGE_RU: dict[str, str] = {
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
}


# ── Player resolution ────────────────────────────────────────────────────────

def _player_from_user(user) -> Optional[dict]:
    """
    Resolve the bot's ``players`` row for a Telegram user.

    Looks up by ``@username`` first (cheapest), falls back to numeric
    ``telegram_id`` so users without a public ``@username`` still find
    their record.
    """
    if user is None:
        return None
    if getattr(user, "username", None):
        p = get_player(user.username)
        if p:
            return p
    return get_player_by_telegram_id(getattr(user, "id", None))


def _resolve_player_arg(arg: str) -> Optional[dict]:
    """Resolve a free-form player reference to a ``players`` row.

    Accepts: ``@username``, plain ``username``, numeric Telegram ID, or a
    registered in-game nickname. Returns ``None`` when nothing matches.
    Used by ``/h2h``, ``/walkover``, ``/admin_report`` and friends.

    When the argument starts with ``@``, username lookup is prioritised
    over telegram_id lookup so that players whose username happens to be
    all-digits (e.g. ``@8530008617``) are resolved correctly.
    """
    if not arg:
        return None
    raw = arg.strip()
    had_at = raw.startswith("@")
    s = raw.lstrip("@").lower()
    if not s:
        return None
    # If explicitly prefixed with @, try username first regardless of digits
    if had_at:
        p = get_player(s)
        if p:
            return p
    if s.isdigit():
        p = get_player_by_telegram_id(int(s))
        if p:
            return p
    if not had_at:
        p = get_player(s)
        if p:
            return p
    return get_player_by_game_nickname(s)


# ── Time formatting ──────────────────────────────────────────────────────────

def _format_deadline_countdown(deadline: Optional[str]) -> str:
    """Render a stored (UTC) deadline as ``"через 3ч 12м"`` / ``"просрочено 5ч 4м"``.

    Output is timezone-independent (relative to now), so it does not
    need a TZ label. Falls back to ``"без дедлайна"`` for missing/empty
    input and to the raw string for unparseable values.
    """
    if not deadline:
        return "без дедлайна"
    try:
        dt = datetime.strptime(deadline, "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return str(deadline)
    delta = dt - datetime.utcnow()
    secs = int(delta.total_seconds())
    overdue = secs < 0
    secs = abs(secs)
    days = secs // 86400
    hours = (secs % 86400) // 3600
    mins = (secs % 3600) // 60
    parts: list[str] = []
    if days:
        parts.append(f"{days}д")
    if hours or days:
        parts.append(f"{hours}ч")
    parts.append(f"{mins}м")
    text = " ".join(parts)
    return f"просрочено {text}" if overdue else f"через {text}"


# ── Tournament permission gate ───────────────────────────────────────────────

def _can_manage_tournament(user_id: int | None, t: dict) -> bool:
    """True if ``user_id`` is allowed to administrate this specific tournament.

    Membership is the union of:
      • root admins (env ``ADMIN_IDS``) — global, can do anything;
      • the tournament creator (``tournaments.created_by``);
      • runtime bot admins (``/grant_admin``) — only if they are the
        creator of THIS tournament (not global);
      • per-tournament admins delegated via ``/add_tadmin`` (stored in the
        ``tournament_admins`` table).
    """
    if user_id is None:
        return False
    if is_root_admin(user_id):
        return True
    creator = get_player_by_id(t["created_by"]) if t.get("created_by") else None
    if creator and creator.get("telegram_id") == user_id:
        return True
    tid = t.get("id")
    if tid is None:
        return False
    try:
        return is_tournament_admin(int(tid), user_id)
    except Exception as e:
        log.warning(
            "is_tournament_admin failed for tid=%s uid=%s: %s",
            tid, user_id, e,
        )
        return False


# ── Tournament selection helpers ─────────────────────────────────────────────

def _resolve_tournament_from_args(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
    *,
    type_hint: Optional[str] = None,
    args: Optional[list[str]] = None,
) -> Tuple[Optional[dict], Optional[str]]:
    """Pick the tournament a command should operate on.

    Resolution order: explicit ID arg ``args[0]`` (or ``ctx.args[0]``) →
    tournament bound to the current chat → most recent active tournament
    (optionally of ``type_hint``). Returns ``(tournament, error_message)``
    — exactly one of the two is non-None.

    Pass ``args`` explicitly when the command consumes leading positional
    args before the optional tournament-ID (e.g. ``/replace_player @a @b
    [ID]``).
    """
    eff_args = args if args is not None else (ctx.args or [])
    if eff_args:
        try:
            tid = int(eff_args[0])
        except ValueError:
            tid = None
        if tid is not None:
            t = get_tournament(tid)
            if not t:
                return None, f"❌ Турнир с ID {tid} не найден."
            return t, None
    chat = update.effective_chat
    if chat:
        t = get_tournament_by_chat(chat.id)
        if t:
            return t, None
    t = get_active_tournament(tournament_type=type_hint)
    if t:
        return t, None
    return None, (
        "❌ Не нашёл турнир. Укажи ID командой "
        "<code>/list_players &lt;ID&gt;</code> или "
        "<code>/bind_tournament &lt;ID&gt;</code> в чате."
    )


def _user_active_tournaments(player_id: int) -> list[dict]:
    """Return active (non-finished) tournaments where ``player_id`` is registered.

    Used to power "К какому турниру отнести этот матч?" pickers when the
    user reports a result without specifying a tournament.
    """
    conn = db.get_conn()
    rows = conn.execute(
        """SELECT t.* FROM tournaments t
           JOIN tournament_players tp
             ON tp.tournament_id = t.id
            AND tp.player_id     = ?
           WHERE COALESCE(t.stage, '') != 'finished'
           ORDER BY t.id DESC""",
        (player_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _tournament_picker_kb(
    tournaments: list[dict],
    cb_prefix: str,
    *,
    cancel_cb: str = "ocr_cancel",
    extra_suffix: str = "",
) -> InlineKeyboardMarkup:
    """Inline keyboard listing tournaments for an in-chat picker.

    Each button's ``callback_data`` is ``"<cb_prefix>:<tid><extra_suffix>"``.
    Caps at 10 rows + a Cancel button to stay within Telegram limits.
    """
    rows = [
        [InlineKeyboardButton(
            f"🏆 {t['name']} (ID {t['id']}, {t_full_label(t)})",
            callback_data=f"{cb_prefix}:{t['id']}{extra_suffix}",
        )]
        for t in tournaments[:10]
    ]
    rows.append([InlineKeyboardButton("❌ Отмена", callback_data=cancel_cb)])
    return InlineKeyboardMarkup(rows)


__all__ = [
    "_STAGE_RU",
    "_player_from_user",
    "_resolve_player_arg",
    "_format_deadline_countdown",
    "_can_manage_tournament",
    "_resolve_tournament_from_args",
    "_user_active_tournaments",
    "_tournament_picker_kb",
]
