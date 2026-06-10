"""Self-contained, dependency-light helpers shared by every handler module.

Everything here is intentionally cheap to import: only the standard
library, ``telegram``, and ``database`` (for the runtime-admin check).
No tournament/playoff/match-processing imports — that keeps these
helpers usable from any handler module without circular-import risk.
"""

from __future__ import annotations

import html
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Tuple

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover — Python < 3.9 fallback
    ZoneInfo = None  # type: ignore[assignment]

from telegram import Update
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from database import is_bot_admin_db, is_bot_owner_db


# Synthetic ``@username`` placeholder that ``cmd_register`` / ``upsert_player``
# create for users who don't have a public Telegram handle. Stored in the
# ``players.username`` column as ``id_<numeric_telegram_id>``. Never show this
# to users as ``@id_…`` — see ``mention``/``mention_player``.
_ID_PLACEHOLDER_RE = re.compile(r"^id_(\d+)$")


log = logging.getLogger("fc_league_bot.handlers.common")


# ── Module-level config ──────────────────────────────────────────────────────

# Single source of truth for the env-var "root" admin list. ``bot.py`` and
# every handler module pull ``ADMIN_IDS`` from here so we never read the env
# var twice (and don't get drift between modules).
ADMIN_IDS: list[int] = [
    int(x) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()
]


# ── Display timezone ─────────────────────────────────────────────────────────
#
# Internal storage stays in **naive UTC** (``datetime.utcnow()`` →
# ``"YYYY-MM-DD HH:MM:SS"`` strings). The display layer renders those
# values in the operator's local timezone — by default Moscow time
# (UTC+3, no DST). Override via the ``BOT_DISPLAY_TZ`` env var with any
# IANA name, e.g. ``Europe/Kyiv`` or ``Asia/Yekaterinburg``.

_DISPLAY_TZ_NAME: str = os.environ.get("BOT_DISPLAY_TZ", "Europe/Moscow").strip() \
    or "Europe/Moscow"

# Short user-facing labels for common Russian-speaking zones. Anything
# not in the map falls back to the IANA name's tail (after ``/``).
_TZ_LABELS: dict[str, str] = {
    "Europe/Moscow":       "МСК",
    "Europe/Kaliningrad":  "MSK-1",
    "Europe/Samara":       "MSK+1",
    "Asia/Yekaterinburg":  "MSK+2",
    "Asia/Omsk":           "MSK+3",
    "Asia/Krasnoyarsk":    "MSK+4",
    "Asia/Irkutsk":        "MSK+5",
    "Asia/Yakutsk":        "MSK+6",
    "Asia/Vladivostok":    "MSK+7",
    "Asia/Magadan":        "MSK+8",
    "Asia/Kamchatka":      "MSK+9",
    "Europe/Kyiv":         "Київ",
    "Europe/Kiev":         "Київ",
    "Europe/Minsk":        "Минск",
    "UTC":                 "UTC",
}


def _display_tz():
    """Return the configured display ``tzinfo`` object.

    Falls back to a fixed UTC+3 offset (Moscow time without DST) when
    ``zoneinfo`` is unavailable or the IANA name can't be loaded — so
    the rest of the codebase never has to deal with ``None``.
    """
    if ZoneInfo is not None:
        try:
            return ZoneInfo(_DISPLAY_TZ_NAME)
        except Exception:
            pass
    if _DISPLAY_TZ_NAME in ("Europe/Moscow", "MSK"):
        return timezone(timedelta(hours=3), name="МСК")
    return timezone.utc


def _tz_label() -> str:
    """Short label for the display timezone, e.g. ``"МСК"`` / ``"UTC"``."""
    label = _TZ_LABELS.get(_DISPLAY_TZ_NAME)
    if label:
        return label
    return _DISPLAY_TZ_NAME.rsplit("/", 1)[-1] or _DISPLAY_TZ_NAME


def _utc_to_local(value) -> datetime | None:
    """Interpret ``value`` as a naive UTC timestamp and convert it to
    the display timezone.

    Accepts ``str`` (SQLite return type), naive or tz-aware ``datetime``,
    or ``None``. Returns ``None`` for empty/unparseable input.
    """
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        s = str(value)
        dt = None
        for fmt in (
            "%Y-%m-%d %H:%M:%S.%f%z",
            "%Y-%m-%d %H:%M:%S.%f",
            "%Y-%m-%d %H:%M:%S%z",
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d %H:%M",
            "%Y-%m-%dT%H:%M:%S.%f%z",
            "%Y-%m-%dT%H:%M:%S.%f",
            "%Y-%m-%dT%H:%M:%S%z",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%d",
        ):
            try:
                dt = datetime.strptime(s, fmt)
                break
            except ValueError:
                continue
        if dt is None:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_display_tz())


def _local_to_utc_str(local_naive: datetime) -> str:
    """Treat a naive ``datetime`` as being in the display timezone and
    convert it to a naive UTC ``"YYYY-MM-DD HH:MM:SS"`` string suitable
    for the ``matches.deadline`` / ``tournaments.deadline_at`` columns.
    """
    aware = local_naive.replace(tzinfo=_display_tz())
    return aware.astimezone(timezone.utc).replace(tzinfo=None) \
                .strftime("%Y-%m-%d %H:%M:%S")


# ── Admin checks ─────────────────────────────────────────────────────────────

def is_owner(user_id: int | None) -> bool:
    """True if the user is a bot owner (super-admin level above regular admins).
    Root admins from ADMIN_IDS are automatically considered owners."""
    if user_id is None:
        return False
    if user_id in ADMIN_IDS:
        return True
    try:
        return is_bot_owner_db(user_id)
    except Exception:
        return False


def is_admin(user_id: int | None) -> bool:
    """True if the user has any admin powers — env-var "root" or a runtime grant
    stored in ``bot_admins``. Falls back to env-only on DB error.
    """
    if user_id is None:
        return False
    if user_id in ADMIN_IDS:
        return True
    try:
        if is_bot_owner_db(user_id):
            return True
    except Exception:
        pass
    try:
        return is_bot_admin_db(user_id)
    except Exception as e:
        log.warning("is_bot_admin_db failed (falling back to env-only): %s", e)
        return False


def is_root_admin(user_id: int | None) -> bool:
    """True only for env-var ADMIN_IDS — these can grant/revoke runtime admins."""
    return user_id in ADMIN_IDS


# ── Tournament label helpers ─────────────────────────────────────────────────

def mention(username: str | None) -> str:
    """Render a ``@username`` for chat output.

    Handles the synthetic ``id_<digits>`` placeholder that ``cmd_register``
    creates for accounts without a public Telegram handle: emits ``id 12345``
    (no ``@``) instead of an unclickable ``@id_12345``. Anything else falls
    through to the literal ``@username``.

    For full player rows prefer :func:`mention_player`, which can also build
    a clickable ``tg://user?id=...`` mention from ``game_nickname`` +
    ``telegram_id`` when the player has no public handle.
    """
    if not username:
        return "—"
    s = str(username).strip()
    if not s:
        return "—"
    m = _ID_PLACEHOLDER_RE.match(s.lower())
    if m:
        return f"id {m.group(1)}"
    return f"@{s}"


def _is_id_placeholder(username: str | None) -> bool:
    """True iff ``username`` is the synthetic ``id_<digits>`` placeholder."""
    if not username:
        return False
    return bool(_ID_PLACEHOLDER_RE.match(str(username).strip().lower()))


def mention_player(p: dict | None, *, fallback: str = "—") -> str:
    """Render a clickable / readable mention for a full ``players`` row.

    Resolution order (best → worst):
      1. ``game_nickname`` + ``telegram_id`` → HTML ``tg://user?id=...``
         link wrapping the nickname. Telegram pings the user even when
         they have no public ``@username``.
      2. real ``@username`` (not the synthetic ``id_<digits>``
         placeholder) → ``@username``.
      3. ``game_nickname`` only (no ``telegram_id`` known yet) → plain
         escaped nickname.
      4. ``telegram_id`` only → ``<a href="tg://user?id=...">id 12345</a>``.
      5. nothing usable → ``fallback``.
    """
    if not p:
        return fallback
    nick = (p.get("game_nickname") or "").strip()
    uname = (p.get("username") or "").strip()
    tid = p.get("telegram_id")
    has_real_handle = bool(uname) and not _is_id_placeholder(uname)
    if nick and tid:
        return f'<a href="tg://user?id={int(tid)}">{html.escape(nick)}</a>'
    if has_real_handle:
        return f"@{uname}"
    if nick:
        return html.escape(nick)
    if tid:
        return f'<a href="tg://user?id={int(tid)}">id {int(tid)}</a>'
    if uname:
        # Synthetic id_<digits> placeholder: strip the @ + ``id_`` prefix.
        m = _ID_PLACEHOLDER_RE.match(uname.lower())
        if m:
            return f"id {m.group(1)}"
        return f"@{uname}"
    return fallback


def arrow(delta: int) -> str:
    return f"▲{delta}" if delta >= 0 else f"▼{abs(delta)}"


def t_type_label(t_type: str) -> str:
    return {"vsa": "ВСА", "ri": "РИ"}.get(t_type, t_type.upper())


def t_scope_label(t: dict) -> str:
    """Short scope tag for a tournament: 'общий' or 'локальный'."""
    return "общий" if t.get("is_official", 1) else "локальный"


def t_full_label(t: dict) -> str:
    """e.g. 'ВСА · общий' / 'РИ · локальный'."""
    return f"{t_type_label(t['tournament_type'])} · {t_scope_label(t)}"


# ── Send helper ──────────────────────────────────────────────────────────────

# Telegram's hard per-message text limit. Anything beyond this gets
# rejected with ``BadRequest: Message is too long``, so ``send`` fans
# out into several messages instead of failing.
_TG_MAX_MESSAGE_CHARS = 4096


async def send(update: Update, text: str, **kwargs):
    """Reply to the right place (callback message vs. ordinary message), HTML by default.

    Tolerates updates without a plain ``message`` (edited messages,
    channel posts, business messages) by falling back to
    ``update.effective_message``. If neither is available, the call is
    a no-op rather than a crash.

    Long text is transparently split into multiple Telegram messages
    using ``bot._split_for_telegram`` (which prefers blank-line, then
    single-newline boundaries). When ``reply_markup`` is supplied it is
    attached to the *last* chunk only, so any inline/reply keyboard
    lands on the final visible message — matches the pattern already
    used by ``cmd_help`` / admin help.

    Link previews are suppressed globally via Defaults(link_preview_options).
    """
    kwargs.setdefault("parse_mode", "HTML")

    # Pick the target ``Message`` object once.
    if update.callback_query:
        target = update.callback_query.message
    else:
        target = update.effective_message or update.message
    if target is None:
        return

    # Common case: fits in one message.
    if len(text) <= _TG_MAX_MESSAGE_CHARS:
        await target.reply_text(text, **kwargs)
        return

    # Long text → split. Lazy import: ``handlers.common`` is loaded
    # before ``bot`` finishes initialising, so a top-level import would
    # create a circular dependency. ``profile.py`` uses the same trick.
    from bot import _split_for_telegram  # noqa: WPS433 (intentional cycle break)

    chunks = _split_for_telegram(text, limit=_TG_MAX_MESSAGE_CHARS)

    # Pull reply_markup out of the shared kwargs — we only attach it to
    # the last chunk so the keyboard ends up on the final message.
    reply_markup = kwargs.pop("reply_markup", None)
    last_idx = len(chunks) - 1
    for i, chunk in enumerate(chunks):
        chunk_kwargs = dict(kwargs)
        if reply_markup is not None and i == last_idx:
            chunk_kwargs["reply_markup"] = reply_markup
        await target.reply_text(chunk, **chunk_kwargs)


# ── Argument parsing ─────────────────────────────────────────────────────────

_DURATION_RE = re.compile(r"^(\d+)\s*(d|h|m|ч|д|m)?$", re.IGNORECASE)


def parse_ban_duration(arg: str) -> Tuple[str | None, str]:
    """
    Parse a duration spec like "24" (hours), "7d" (days), "30m" (minutes),
    or "perm"/"бессрочно" (permanent).

    Returns (until_iso_str | None, human_label). until_iso_str=None means permanent.
    """
    a = arg.strip().lower()
    if a in ("perm", "permanent", "бессрочно", "навсегда", "forever", "0"):
        return None, "бессрочно"
    m = _DURATION_RE.match(a)
    if not m:
        try:
            hours = int(a)
        except ValueError:
            raise ValueError("Не понял длительность бана.")
        until_dt = datetime.utcnow() + timedelta(hours=hours)
        return until_dt.strftime("%Y-%m-%d %H:%M:%S"), f"{hours}ч"
    n = int(m.group(1))
    unit = (m.group(2) or "h").lower()
    if unit in ("d", "д"):
        delta = timedelta(days=n)
        label = f"{n}д"
    elif unit in ("m",):
        delta = timedelta(minutes=n)
        label = f"{n}м"
    else:                                               # h, ч (default)
        delta = timedelta(hours=n)
        label = f"{n}ч"
    return (datetime.utcnow() + delta).strftime("%Y-%m-%d %H:%M:%S"), label


def parse_tournament_type_arg(arg: str | None) -> str | None:
    """Accept 'vsa', 'вса', 'ri', 'ри' (case-insensitive)."""
    if not arg:
        return None
    a = arg.strip().lower()
    if a in ("vsa", "вса"):
        return "vsa"
    if a in ("ri", "ри"):
        return "ri"
    return None


# ── Date / time formatting ───────────────────────────────────────────────────

def _fmt_dt(value, fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    """Format a DB timestamp value as ``%Y-%m-%d %H:%M:%S``-style string.

    Accepts ``str`` (SQLite return type), ``datetime`` (psycopg2 return
    type — possibly tz-aware), or ``None``. Returns ``""`` for ``None``
    so callers can safely concatenate without crashing.
    """
    if value is None:
        return ""
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.replace(tzinfo=None)
        return value.strftime(fmt)
    s = str(value)
    try:
        for cand_fmt in (
            "%Y-%m-%d %H:%M:%S.%f%z",
            "%Y-%m-%d %H:%M:%S.%f",
            "%Y-%m-%d %H:%M:%S%z",
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%dT%H:%M:%S.%f%z",
            "%Y-%m-%dT%H:%M:%S.%f",
            "%Y-%m-%dT%H:%M:%S%z",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%d",
        ):
            try:
                parsed = datetime.strptime(s, cand_fmt)
                if parsed.tzinfo is not None:
                    parsed = parsed.replace(tzinfo=None)
                return parsed.strftime(fmt)
            except ValueError:
                continue
    except Exception:
        pass
    return s


def _fmt_date(value) -> str:
    """Short ``YYYY-MM-DD`` form of :func:`_fmt_dt`."""
    return _fmt_dt(value, "%Y-%m-%d")


def _fmt_minute(value) -> str:
    """Minute precision ``YYYY-MM-DD HH:MM`` form of :func:`_fmt_dt`."""
    return _fmt_dt(value, "%Y-%m-%d %H:%M")


def _fmt_dt_local(value, fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    """Like :func:`_fmt_dt`, but converts naive-UTC timestamps to the
    operator's display timezone first.

    Used for user-facing deadline and audit-log strings. Returns ``""``
    for ``None`` / unparseable input so callers can concatenate safely.
    """
    local = _utc_to_local(value)
    if local is None:
        return "" if value in (None, "") else str(value)
    return local.strftime(fmt)


def _fmt_minute_local(value) -> str:
    """Minute precision ``YYYY-MM-DD HH:MM`` form of :func:`_fmt_dt_local`."""
    return _fmt_dt_local(value, "%Y-%m-%d %H:%M")


def _fmt_minute_tz(value) -> str:
    """``"YYYY-MM-DD HH:MM <TZ>"`` — convenience for inline labelling.

    Returns ``""`` for empty input. The TZ suffix is the short label
    from :func:`_tz_label` (e.g. ``"МСК"``).
    """
    s = _fmt_minute_local(value)
    if not s:
        return ""
    return f"{s} {_tz_label()}"


# ── Subscription gate ────────────────────────────────────────────────────────

async def check_required_channel(
    ctx: ContextTypes.DEFAULT_TYPE,
    user_telegram_id: int | None,
    channel: str | None,
) -> Tuple[bool, str]:
    """
    Verify that ``user_telegram_id`` is subscribed to ``channel``.

    Returns ``(ok, message)``. The message is what to show if not ok.
    Channel can be ``"@username"`` or ``"-100xxx"`` (numeric chat id).
    """
    if not channel:
        return True, ""
    if not user_telegram_id:
        return False, (
            "❌ Чтобы проверить подписку, мне нужен твой Telegram-ID. "
            "Сначала /register."
        )
    try:
        m = await ctx.bot.get_chat_member(channel, user_telegram_id)
        if m.status in ("member", "administrator", "creator"):
            return True, ""
        return False, (
            f"❌ Чтобы участвовать в этом турнире, подпишись на канал {channel}, "
            f"затем повтори команду."
        )
    except TelegramError as e:
        log.warning("get_chat_member(%s, %s) failed: %s", channel, user_telegram_id, e)
        return False, (
            f"❌ Не смог проверить подписку на {channel}. "
            f"Убедитесь, что бот добавлен в канал (можно без админ-прав)."
        )


__all__ = [
    "ADMIN_IDS",
    "log",
    "is_admin",
    "is_owner",
    "is_root_admin",
    "mention",
    "mention_player",
    "arrow",
    "t_type_label",
    "t_scope_label",
    "t_full_label",
    "send",
    "parse_ban_duration",
    "parse_tournament_type_arg",
    "_fmt_dt",
    "_fmt_date",
    "_fmt_minute",
    "_fmt_dt_local",
    "_fmt_minute_local",
    "_fmt_minute_tz",
    "_utc_to_local",
    "_local_to_utc_str",
    "_display_tz",
    "_tz_label",
    "check_required_channel",
    "get_random_footer",
    "format_footer_preview",
    "FOOTER_CTX_MATCH",
    "FOOTER_CTX_TABLE",
    "FOOTER_CTX_PLAYOFF",
    "FOOTER_CTX_STAGE",
    "FOOTER_CTX_REMINDER",
    "FOOTER_CTX_BROADCAST",
    "FOOTER_CTX_FINISH",
    "FOOTER_PLACES_ALL",
    "_get_footer_places",
    "entities_to_html",
]


# ── Footer text helpers ──────────────────────────────────────────────────────

import json as _json
import random as _random

# Footer context tags — callers pass one to indicate *where* the message
# is being sent so the footer_places filter can decide whether to include it.
FOOTER_CTX_MATCH = "match"          # match result (report / confirm / admin approve)
FOOTER_CTX_TABLE = "table"          # /table standings image
FOOTER_CTX_PLAYOFF = "playoff"      # /playoff bracket image
FOOTER_CTX_STAGE = "stage"          # stage advance announcement in chat
FOOTER_CTX_REMINDER = "reminder"    # chat reminder (pending matches)
FOOTER_CTX_BROADCAST = "broadcast"  # /broadcast command
FOOTER_CTX_FINISH = "finish"        # tournament finished announcement

# All known place keys with human-readable labels (for the settings UI).
FOOTER_PLACES_ALL: dict[str, str] = {
    "match":     "⚽ Результаты матчей",
    "table":     "📊 Таблица и бомбардиры",
    "playoff":   "⚔️ Сетка плей-офф (/playoff)",
    "stage":     "🚀 Анонс стадий",
    "reminder":  "🔔 Чат-напоминания",
    "broadcast": "📣 Рассылка (/broadcast)",
    "finish":    "🏁 Завершение турнира",
}

# Default: all places enabled.
_FOOTER_PLACES_DEFAULT: dict[str, bool] = {k: True for k in FOOTER_PLACES_ALL}


def _get_footer_places(t: dict | None) -> dict[str, bool]:
    """Parse the ``footer_places`` JSON field into a dict of booleans.

    Falls back to the legacy ``footer_scope`` field for tournaments that
    haven't been migrated yet, and ultimately to "all enabled".
    """
    if not t:
        return dict(_FOOTER_PLACES_DEFAULT)

    raw = (t.get("footer_places") or "").strip()
    if raw:
        try:
            parsed = _json.loads(raw)
            if isinstance(parsed, dict):
                # Merge with defaults so newly-added keys are auto-enabled.
                result = dict(_FOOTER_PLACES_DEFAULT)
                for k, v in parsed.items():
                    if k in result:
                        result[k] = bool(v)
                return result
        except (_json.JSONDecodeError, ValueError):
            pass

    # Legacy fallback: convert old footer_scope to per-place dict.
    scope = (t.get("footer_scope") or "all").strip().lower()
    if scope == "match":
        return {k: (k == "match") for k in FOOTER_PLACES_ALL}
    elif scope == "chat":
        return {k: (k != "reminder") for k in FOOTER_PLACES_ALL}
    # 'all' or unknown → everything on
    return dict(_FOOTER_PLACES_DEFAULT)


# Per-(tournament_id, context) state for footer variant rotation.
#
# Without this, ``random.choice`` will happily pick the same variant
# several messages in a row, which feels broken to admins who set up a
# pool of variants specifically to add variety. We instead maintain a
# shuffled queue per (tid, context) and pop one variant at a time. When
# the queue is exhausted we reshuffle for the next cycle, rotating the
# new queue if needed so the same variant never appears back-to-back
# across cycle boundaries.
#
# Process-local: state is lost on restart, which is fine — variety just
# resets to a fresh shuffle.
#
#   key   = (tournament_id, context)
#   value = {
#       "sig":   tuple(variants)   — to detect when admin edited footer
#       "queue": list[str]         — remaining variants (popped from front)
#       "last":  str | None        — last variant returned (for boundary check)
#   }
_footer_rotation_state: dict[tuple[int, str], dict] = {}


def _next_footer_variant(tid: int | None, context: str,
                         variants: list[str]) -> str:
    """Pick the next footer variant for ``(tid, context)``, with rotation.

    With 2+ variants, guarantees the same variant is never returned
    twice in a row, and that all variants are shown once before any
    repeats. With a single variant, returns it. Falls back to plain
    ``random.choice`` when ``tid`` is missing.
    """
    if not variants:
        return ""
    if len(variants) == 1:
        return variants[0]
    if tid is None:
        return _random.choice(variants)

    key = (tid, context)
    state = _footer_rotation_state.get(key)
    sig = tuple(variants)

    # Rebuild queue if variants changed (admin edited footer) or
    # current queue is empty (full cycle completed).
    if state is None or state.get("sig") != sig or not state.get("queue"):
        new_queue = list(variants)
        _random.shuffle(new_queue)
        last = state.get("last") if state else None
        # Avoid back-to-back repeat across the cycle boundary.
        if last and len(new_queue) > 1 and new_queue[0] == last:
            new_queue.append(new_queue.pop(0))
        state = {"sig": sig, "queue": new_queue, "last": last}
        _footer_rotation_state[key] = state

    chosen = state["queue"].pop(0)
    state["last"] = chosen
    return chosen


def get_random_footer(t: dict | None, context: str = "match") -> str:
    """Pick a footer variant from the tournament's footer_text field.

    ``context`` is one of the ``FOOTER_CTX_*`` constants (match, table,
    playoff, stage, reminder, broadcast, finish). The footer is only
    returned if the tournament's ``footer_places`` has that key enabled.

    ``footer_text`` is stored as either:
    - A JSON array of strings: ``["variant1", "variant2", ...]``
    - A plain string (legacy/single variant): ``"some text"``

    Variants rotate through a shuffled queue per (tournament, context)
    so the same variant never appears in two consecutive messages and
    every variant is shown once before any repeats — see
    ``_next_footer_variant``.

    Returns the chosen variant wrapped in ``<tg-spoiler>`` tags, or empty
    string if no footer is configured or the place is disabled.
    HTML (including ``<a href=...>``) inside variants is preserved as-is.
    """
    if not t:
        return ""
    raw = (t.get("footer_text") or "").strip()
    if not raw:
        return ""
    # Check per-place toggle
    places = _get_footer_places(t)
    if not places.get(context, True):
        return ""
    # Try parsing as JSON array
    variants: list[str] = []
    if raw.startswith("["):
        try:
            parsed = _json.loads(raw)
            if isinstance(parsed, list):
                variants = [str(v).strip() for v in parsed if str(v).strip()]
        except (_json.JSONDecodeError, ValueError):
            pass
    # Fallback: treat as a single plain-text variant
    if not variants:
        variants = [raw]
    chosen = _next_footer_variant(t.get("id"), context, variants)
    if not chosen:
        return ""
    return f"\n\n<tg-spoiler>{chosen}</tg-spoiler>"


def format_footer_preview(t: dict | None) -> str:
    """Format a full preview of all footer variants for the settings panel."""
    if not t:
        return "<i>не задана</i>"
    raw = (t.get("footer_text") or "").strip()
    if not raw:
        return "<i>не задана</i>"
    variants: list[str] = []
    if raw.startswith("["):
        try:
            parsed = _json.loads(raw)
            if isinstance(parsed, list):
                variants = [str(v).strip() for v in parsed if str(v).strip()]
        except (_json.JSONDecodeError, ValueError):
            pass
    if not variants:
        variants = [raw]
    if len(variants) == 1:
        return f"<tg-spoiler>{variants[0]}</tg-spoiler>"
    lines = []
    for i, v in enumerate(variants, 1):
        lines.append(f"  {i}. <tg-spoiler>{v}</tg-spoiler>")
    return "\n".join(lines)



# ── Telegram entity → HTML conversion ────────────────────────────────────────


def entities_to_html(text: str, entities: list | None) -> str:
    """Convert a Telegram message's text + entities into an HTML string.

    Handles: bold, italic, underline, strikethrough, code, pre,
    text_link (hyperlinks), spoiler. Unknown entity types are ignored.

    This lets users type formatted messages (bold, links via Telegram's
    built-in formatting) and the bot stores them as HTML for later use
    in footer_text without requiring raw HTML input.

    If ``entities`` is None or empty, returns ``text`` with HTML special
    chars escaped.
    """
    if not entities:
        return html.escape(text)

    # Build a list of (offset, is_open, priority, tag) tuples.
    # We process text char-by-char (by UTF-16 offset, as Telegram uses).
    # Python strings are UTF-32/UCS-4, but entity offsets are in UTF-16
    # code units. We encode to UTF-16-LE to get correct offsets.
    utf16 = text.encode("utf-16-le")
    # Each UTF-16-LE char is 2 bytes
    length_utf16 = len(utf16) // 2

    # Map: position → list of opening tags, position → list of closing tags
    opens: dict[int, list[str]] = {}
    closes: dict[int, list[str]] = {}

    for ent in entities:
        etype = getattr(ent, "type", None) or (ent.get("type") if isinstance(ent, dict) else None)
        offset = getattr(ent, "offset", None)
        elength = getattr(ent, "length", None)
        if offset is None and isinstance(ent, dict):
            offset = ent.get("offset", 0)
            elength = ent.get("length", 0)
        if offset is None or elength is None:
            continue
        end = offset + elength

        url = getattr(ent, "url", None) or (ent.get("url") if isinstance(ent, dict) else None)

        if etype == "bold":
            opens.setdefault(offset, []).append("<b>")
            closes.setdefault(end, []).insert(0, "</b>")
        elif etype == "italic":
            opens.setdefault(offset, []).append("<i>")
            closes.setdefault(end, []).insert(0, "</i>")
        elif etype == "underline":
            opens.setdefault(offset, []).append("<u>")
            closes.setdefault(end, []).insert(0, "</u>")
        elif etype == "strikethrough":
            opens.setdefault(offset, []).append("<s>")
            closes.setdefault(end, []).insert(0, "</s>")
        elif etype == "code":
            opens.setdefault(offset, []).append("<code>")
            closes.setdefault(end, []).insert(0, "</code>")
        elif etype == "pre":
            opens.setdefault(offset, []).append("<pre>")
            closes.setdefault(end, []).insert(0, "</pre>")
        elif etype == "text_link" and url:
            opens.setdefault(offset, []).append(f'<a href="{html.escape(url)}">')
            closes.setdefault(end, []).insert(0, "</a>")
        elif etype == "spoiler":
            opens.setdefault(offset, []).append("<tg-spoiler>")
            closes.setdefault(end, []).insert(0, "</tg-spoiler>")

    # Build result by iterating UTF-16 positions
    result: list[str] = []
    i = 0
    while i <= length_utf16:
        # Close tags first (proper nesting)
        if i in closes:
            result.extend(closes[i])
        if i in opens:
            result.extend(opens[i])
        if i < length_utf16:
            # Decode one UTF-16 char (may be 2 bytes or 4 bytes for surrogates)
            byte_pos = i * 2
            code_unit = int.from_bytes(utf16[byte_pos:byte_pos + 2], "little")
            if 0xD800 <= code_unit <= 0xDBFF and (byte_pos + 3) < len(utf16):
                # Surrogate pair — 2 UTF-16 units = 1 character
                char = utf16[byte_pos:byte_pos + 4].decode("utf-16-le")
                result.append(html.escape(char))
                i += 2
            else:
                char = utf16[byte_pos:byte_pos + 2].decode("utf-16-le")
                result.append(html.escape(char))
                i += 1
        else:
            i += 1

    return "".join(result)
