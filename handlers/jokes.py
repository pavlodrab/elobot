"""Auto-jokes module: bot reads recent chat text and asks an LLM
(OpenRouter free-tier fallback chain) to write a one-liner joke.

Two ways to fire:
  * Manually — admin runs ``/joke`` in any chat where ``/jokes_on``
    is active. Throttled by a tiny per-chat in-memory cooldown.
  * Automatically — ``job_jokes`` (registered in ``bot.py``) runs
    every ~5 minutes and posts a joke in chats whose interval has
    elapsed AND that have collected at least ``jokes_min_msgs_since_last``
    new messages since the previous joke.

Privacy model: ``chat_messages`` (rolling 500-row buffer) is only
written when ``jokes_enabled=true`` for that chat. ``/jokes_off``
keeps existing logged rows by default; ``/jokes_clear_log`` wipes
them on demand.

Per-chat config lives in ``chat_settings`` (cf. database.py — same
table as the existing /quote loop). Modes (``soft / normal / spicy
/ savage / absurd``) are vibe presets that pair a system-prompt
fragment with a sampling temperature. Floor rules are constant
across all modes — no slurs, no threats, no PII leakage.

Public entry points:
  * :func:`log_chat_message`         — group=-1 MessageHandler hook
  * :func:`generate_joke_for_chat`   — used by ``/joke`` and ``job_jokes``
  * :func:`job_jokes`                — scheduled by ``bot.py``
  * ``cmd_joke`` and friends         — slash commands

OpenRouter call mechanics (urllib + key rotation) intentionally
mirror :mod:`tournament_summary` so we have one mental model for
LLM calls in the codebase.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import random
import re
import time
import urllib.error
import urllib.request

from datetime import datetime
from typing import Optional

from telegram import Update
from telegram.error import TelegramError
from telegram.ext import ContextTypes

import database as db

from handlers.common import is_admin, is_root_admin, send

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────

# Default OpenRouter fallback chain — the user picked these models.
# An admin can override per-chat with ``/jokes_setmodel``, or globally
# with the ``JOKES_MODELS`` env var (comma-separated).
_DEFAULT_JOKE_MODELS: tuple[str, ...] = (
    "openrouter/owl-alpha",
    "google/gemma-4-31b-it:free",
    "nvidia/nemotron-3-super-120b-a12b:free",
)

_OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

# Per-mode (system prompt fragment, temperature). Floor rules are
# prepended to every prompt regardless of mode — they never bend.
_MODE_PROMPTS: dict[str, tuple[str, float]] = {
    "soft": (
        "Сделай одну добрую, мягкую, безобидную шутку про этот чат. "
        "Тёплая ирония, без сарказма, без чёрного юмора, без подколов в "
        "адрес конкретных людей. Если поводов для шутки в контексте нет "
        "— нейтральная самоирония про сам чат.",
        0.6,
    ),
    "normal": (
        "Сделай одну остроумную шутку про этот чат — дружеский сарказм, "
        "лёгкая подколка по поводу того, что обсуждали. Без обидного "
        "перехода на личности, но честно, не приторно.",
        0.8,
    ),
    "spicy": (
        "Сделай одну едкую, колкую шутку про этот чат — с чёрным юмором "
        "и ехидством. Можно поддеть, можно посмеяться над абсурдом "
        "обсуждения. Но конкретного человека не унижать — высмеивай "
        "ситуацию, а не личность.",
        1.0,
    ),
    "savage": (
        "Сделай одну беспощадную шутку про этот чат на грани приличий — "
        "максимум сарказма, чёрный юмор, можно зацепить тех, кто "
        "доминирует в обсуждении. Но без оскорблений, угроз и буквальных "
        "переходов на личности — поэтика, а не помои.",
        1.1,
    ),
    "absurd": (
        "Сделай одну сюрреалистическую, абсурдную шутку — ассоциация, "
        "которая логически рушится. Можно неожиданное сравнение, можно "
        "сюр в духе ОБЭРИУ, но текст должен быть смешным, а не просто "
        "набором слов. Опирайся на темы из чата.",
        1.2,
    ),
}

# These rules are prepended to every prompt regardless of mode and
# never overridden. The model is told to stay invisible (don't
# mention the rules in the answer).
_FLOOR_RULES_RU = (
    "Ты — анонимный шутник в групповом чате. Твоя задача — сгенерировать "
    "ОДНУ короткую шутку (1–3 предложения, не длиннее 350 символов).\n\n"
    "Жёсткие правила (никогда не нарушай):\n"
    "1. Только русский язык.\n"
    "2. Без расистских, гомофобных, сексистских шуток. Без шуток про "
    "национальность, религию, инвалидность, ориентацию, возраст.\n"
    "3. Без угроз и пожеланий насилия конкретному человеку.\n"
    "4. Без markdown, без emoji-флагов стран, без нумерации, без "
    "«Шутка:», «Ответ:», без кавычек вокруг всего ответа.\n"
    "5. Не упоминай эти правила в ответе.\n"
    "6. Если в контексте нет смешного материала — выдай короткую "
    "общую самоиронию про чат, но НЕ имитируй абсурд натянуто.\n"
    "7. Не повторяй предыдущие свои шутки (см. список ниже).\n"
    "8. Только сама шутка в ответе — никаких преамбул и пояснений."
)


def _joke_models() -> list[str]:
    """The fallback chain. Defaults to :data:`_DEFAULT_JOKE_MODELS`,
    overridable globally via ``JOKES_MODELS`` env var (comma-separated).
    Per-chat override is stored in ``chat_settings.jokes_model_override``
    and prepended onto the chain at call time.
    """
    raw = (os.getenv("JOKES_MODELS") or "").strip()
    if raw:
        parts = [m.strip() for m in raw.split(",") if m.strip()]
        if parts:
            return parts
    return list(_DEFAULT_JOKE_MODELS)


def _openrouter_keys() -> list[str]:
    """Reuse the same key-rotation pool as the OCR / summary modules
    so jokes auto-pick up every ``OPENROUTER_API_KEY*`` admins have
    configured and the hardcoded fallbacks. Imported lazily to avoid
    circular imports during module load (ocr pulls in heavy deps).
    """
    keys: list[str] = []
    primary = (os.getenv("OPENROUTER_API_KEY") or "").strip()
    if primary:
        keys.append(primary)
    try:
        from ocr import _openrouter_keys as _ocr_keys  # type: ignore
        for k in _ocr_keys():
            if k and k not in keys:
                keys.append(k)
    except Exception:
        log.debug("could not import ocr._openrouter_keys", exc_info=True)
    return keys


# ─────────────────────────────────────────────────────────────────────────────
# Manual /joke cooldown (per-chat in-memory)
# ─────────────────────────────────────────────────────────────────────────────
#
# Without this an admin can spam OpenRouter from one chat. Cleared on
# bot restart — that's fine; the throttle is just an anti-spam measure.

_MANUAL_COOLDOWN_SEC = 60
_manual_cooldown: dict[str, float] = {}


def _manual_cooldown_remaining(chat_id: str) -> int:
    """Seconds left before this chat can run /joke again. ``0`` = ready."""
    last = _manual_cooldown.get(str(chat_id))
    if last is None:
        return 0
    elapsed = time.time() - last
    if elapsed >= _MANUAL_COOLDOWN_SEC:
        return 0
    return int(_MANUAL_COOLDOWN_SEC - elapsed)


def _manual_cooldown_set(chat_id: str) -> None:
    _manual_cooldown[str(chat_id)] = time.time()


# ─────────────────────────────────────────────────────────────────────────────
# Output sanitiser — same ZWSP trick used by /quote
# ─────────────────────────────────────────────────────────────────────────────

_MENTION_RE = re.compile(r"@(?=\w)")
_LEADING_LABEL_RE = re.compile(
    r"^\s*(?:шутка|анекдот|joke|ответ)\s*[:\-—–]\s*",
    flags=re.IGNORECASE,
)


def _strip_mentions(s: str) -> str:
    """Insert ZWSP after ``@`` so Telegram doesn't ping anybody when
    the bot posts the joke. Visually identical for humans.
    """
    return _MENTION_RE.sub("@\u200b", s or "")


def _clean_joke_text(raw: str) -> str:
    """Normalise the LLM output: trim, drop common ``Шутка:`` /
    ``Ответ:`` prefixes, strip surrounding quotes, cap length, and
    de-fang @mentions so we don't accidentally notify users.
    """
    if not raw:
        return ""
    s = str(raw).strip()
    s = _LEADING_LABEL_RE.sub("", s).strip()
    # Strip leading/trailing matched quote chars.
    for opener, closer in (("«", "»"), ("\u201c", "\u201d"), ("\u2018", "\u2019"),
                           ("\"", "\""), ("'", "'")):
        if s.startswith(opener) and s.endswith(closer) and len(s) >= 2:
            s = s[1:-1].strip()
    # Cap length — model sometimes ignores the 350-char rule on
    # sustained output. We keep a generous 800 ceiling to allow
    # "absurd" mode poetic riffs without runaway essays.
    if len(s) > 800:
        s = s[:800].rsplit(" ", 1)[0].rstrip(",.;") + "…"
    return _strip_mentions(s)


# ─────────────────────────────────────────────────────────────────────────────
# Prompt assembly
# ─────────────────────────────────────────────────────────────────────────────

_CTX_HARD_CAP_CHARS = 6000   # OpenRouter context cap budget for the user msg


def _format_author(row: dict) -> str:
    """Pick the friendliest author label we have for a single line:
    display_name → username → ``id_<tg>`` → ``аноним``.
    """
    name = (row.get("display_name") or "").strip()
    if name:
        return name
    user = (row.get("username") or "").strip()
    if user:
        return f"@{user}"
    tid = row.get("telegram_id")
    if tid:
        return f"id_{tid}"
    return "аноним"


def _build_context_text(rows: list[dict]) -> str:
    """Render last-N messages as ``[Имя]: текст`` lines, oldest first.

    Drops empty rows, trims each line to ~400 chars, and prunes from
    the front if the total payload would exceed the hard cap.
    """
    lines: list[str] = []
    for r in rows:
        txt = (r.get("text") or "").strip()
        if not txt:
            continue
        if len(txt) > 400:
            txt = txt[:400].rstrip() + "…"
        author = _format_author(r)
        lines.append(f"[{author}]: {txt}")
    if not lines:
        return ""
    # Prune oldest until under cap.
    total = sum(len(l) + 1 for l in lines)
    while lines and total > _CTX_HARD_CAP_CHARS:
        dropped = lines.pop(0)
        total -= len(dropped) + 1
    return "\n".join(lines)


def _format_recent_jokes_for_prompt(history: list[dict], limit: int = 5) -> str:
    """Compact list of the bot's last few jokes — fed into the prompt
    so the model has an "avoid repeating yourself" hint. Returns
    empty string when there's no history.
    """
    if not history:
        return ""
    bits: list[str] = []
    for h in history[:limit]:
        t = (h.get("text") or "").strip().replace("\n", " ")
        if t:
            if len(t) > 200:
                t = t[:200].rstrip() + "…"
            bits.append(f"- {t}")
    if not bits:
        return ""
    return (
        "Твои последние шутки в этом чате (НЕ повторяй ни одну из них, "
        "ни по форме, ни по поводу):\n" + "\n".join(bits)
    )


def _build_prompt(
    *,
    mode: str,
    context_text: str,
    history_text: str,
) -> tuple[str, str, float]:
    """Return ``(system, user, temperature)`` for the OpenRouter call."""
    mode_prompt, temperature = _MODE_PROMPTS.get(mode, _MODE_PROMPTS["normal"])
    system = _FLOOR_RULES_RU + "\n\n" + mode_prompt
    user_parts = [
        "Ниже — последние сообщения группового чата (в хронологическом "
        "порядке, [Имя]: текст):",
        "",
        context_text or "(чат пуст или ничего смешного)",
    ]
    if history_text:
        user_parts.append("")
        user_parts.append(history_text)
    user_parts.append("")
    user_parts.append("Сгенерируй ровно одну шутку. Только саму шутку, "
                      "без преамбулы.")
    return system, "\n".join(user_parts), float(temperature)


# ─────────────────────────────────────────────────────────────────────────────
# OpenRouter call (blocking → wrapped via asyncio.to_thread)
# ─────────────────────────────────────────────────────────────────────────────

def _call_openrouter_sync(
    *,
    system: str,
    user: str,
    temperature: float,
    models: list[str],
    timeout: float = 30.0,
    attempts: list[str] | None = None,
) -> tuple[Optional[str], Optional[str]]:
    """Try every (model × key) combo until one returns non-empty
    content. Returns ``(text, model_used)`` or ``(None, None)``.

    Mirrors :func:`tournament_summary._try_openrouter` so we have
    consistent error handling across LLM call sites.
    """
    if attempts is None:
        attempts = []
    keys = _openrouter_keys()
    if not keys:
        attempts.append("openrouter: нет ключей (OPENROUTER_API_KEY)")
        return None, None
    if not models:
        attempts.append("openrouter: пустой список моделей")
        return None, None

    body_template = {
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_tokens": 600,        # one joke is short; this is generous
        "temperature": float(temperature),
        # Reasoning-effort hint for nemotron / similar — fine to ignore.
        "reasoning": {"effort": "low"},
    }

    for model in models:
        body = {**body_template, "model": model}
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        for key in keys:
            req = urllib.request.Request(
                _OPENROUTER_URL,
                data=payload,
                headers={
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                    "HTTP-Referer": "https://github.com/fc-league-bot",
                    "X-Title": "FC League Bot — Auto-jokes",
                },
            )
            t0 = time.time()
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    raw = r.read()
            except urllib.error.HTTPError as e:
                err_body = ""
                try:
                    err_body = e.read().decode("utf-8", errors="replace")[:200]
                except Exception:
                    pass
                attempts.append(
                    f"openrouter {model} key…{key[-6:]}: HTTP {e.code} "
                    f"{err_body[:80]}"
                )
                # 401/403 — bad key, try next key.
                # 429 — rate-limited, try next key.
                # Anything else (404, 500, …) — give up on this model
                # and move on. Don't burn keys on a permanently broken
                # model name.
                if e.code in (401, 403, 429):
                    continue
                break
            except Exception as e:
                attempts.append(
                    f"openrouter {model} key…{key[-6:]}: "
                    f"{type(e).__name__} {e}"
                )
                continue
            dt = time.time() - t0
            try:
                data = json.loads(raw)
            except Exception:
                attempts.append(f"openrouter {model}: non-JSON {raw[:80]!r}")
                continue
            err = data.get("error") if isinstance(data, dict) else None
            if err:
                attempts.append(
                    f"openrouter {model}: API error {str(err)[:120]}"
                )
                continue
            choices = (data.get("choices") or []) if isinstance(data, dict) else []
            if not choices:
                attempts.append(f"openrouter {model}: empty choices")
                continue
            msg = choices[0].get("message") if isinstance(choices[0], dict) else None
            content = (msg.get("content") if isinstance(msg, dict) else None) or ""
            content = content.strip()
            if not content:
                # Some reasoning models put the answer in `reasoning`.
                content = (msg.get("reasoning") if isinstance(msg, dict) else "") or ""
                content = content.strip()
            if content:
                attempts.append(
                    f"openrouter {model}: OK ({len(content)} chars, {dt:.1f}s)"
                )
                log.info("jokes: %s OK in %.1fs (%d chars)", model, dt, len(content))
                return content, model
            attempts.append(f"openrouter {model}: empty content")
    return None, None


async def _call_openrouter(
    *,
    system: str,
    user: str,
    temperature: float,
    models: list[str],
    timeout: float = 30.0,
    attempts: list[str] | None = None,
) -> tuple[Optional[str], Optional[str]]:
    """Async wrapper around the blocking OpenRouter call so the bot
    event loop isn't blocked while we wait on a 30s HTTP request.
    """
    return await asyncio.to_thread(
        _call_openrouter_sync,
        system=system,
        user=user,
        temperature=temperature,
        models=models,
        timeout=timeout,
        attempts=attempts,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Joke generation orchestrator
# ─────────────────────────────────────────────────────────────────────────────

class JokeOutcome:
    """Lightweight result wrapper. We use a class instead of a tuple
    so callers reference fields by name in long log lines.
    """

    __slots__ = ("text", "model", "mode", "context_size", "attempts", "error")

    def __init__(
        self,
        *,
        text: Optional[str],
        model: Optional[str],
        mode: str,
        context_size: int,
        attempts: list[str],
        error: Optional[str] = None,
    ) -> None:
        self.text = text
        self.model = model
        self.mode = mode
        self.context_size = context_size
        self.attempts = attempts
        self.error = error


async def generate_joke_for_chat(chat_id: str | int) -> JokeOutcome:
    """Pull recent messages, build prompt per chat's settings, call
    OpenRouter, and return a :class:`JokeOutcome`. Does NOT write to
    ``jokes_history`` or update ``last_joke_at`` — the caller does
    that after a successful Telegram post (so we don't claim "joke
    sent" when Telegram itself fails).
    """
    settings = db.get_jokes_settings(chat_id)
    mode = settings.get("jokes_mode") or "normal"
    if mode not in db.JOKES_VALID_MODES:
        mode = "normal"
    context_n = int(settings.get("jokes_context_size") or 100)
    context_n = max(db.JOKES_MIN_CONTEXT, min(db.JOKES_MAX_CONTEXT, context_n))

    rows = db.recent_chat_messages(chat_id, limit=context_n)
    if not rows:
        return JokeOutcome(
            text=None, model=None, mode=mode, context_size=0, attempts=[],
            error="empty_log",
        )

    context_text = _build_context_text(rows)
    history = db.list_jokes_history(chat_id, limit=5)
    history_text = _format_recent_jokes_for_prompt(history)

    system, user, temperature = _build_prompt(
        mode=mode, context_text=context_text, history_text=history_text,
    )

    # Build the model fallback list: per-chat override goes first,
    # then default chain (deduped, preserving order).
    chain: list[str] = []
    override = (settings.get("jokes_model_override") or "").strip()
    if override:
        chain.append(override)
    for m in _joke_models():
        if m not in chain:
            chain.append(m)

    attempts: list[str] = []
    try:
        raw, model = await _call_openrouter(
            system=system,
            user=user,
            temperature=temperature,
            models=chain,
            timeout=30.0,
            attempts=attempts,
        )
    except Exception as e:
        log.exception("generate_joke_for_chat(%s) crashed", chat_id)
        return JokeOutcome(
            text=None, model=None, mode=mode, context_size=len(rows),
            attempts=attempts, error=f"crash: {type(e).__name__}: {e}",
        )

    if not raw:
        return JokeOutcome(
            text=None, model=None, mode=mode, context_size=len(rows),
            attempts=attempts, error="all_models_failed",
        )

    cleaned = _clean_joke_text(raw)
    if not cleaned:
        return JokeOutcome(
            text=None, model=model, mode=mode, context_size=len(rows),
            attempts=attempts, error="empty_after_clean",
        )

    return JokeOutcome(
        text=cleaned, model=model, mode=mode, context_size=len(rows),
        attempts=attempts,
    )


# ─────────────────────────────────────────────────────────────────────────────
# log_chat_message — group=-1 MessageHandler (always-runs first)
# ─────────────────────────────────────────────────────────────────────────────

async def log_chat_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Append the incoming text message to ``chat_messages`` if and
    only if ``jokes_enabled=true`` for this chat. Never raises —
    on any error we just skip logging and let downstream handlers run.

    Filtered out:
      * non-text or empty messages,
      * messages from the bot itself or any bot,
      * messages whose first character is ``/`` (slash-commands —
        no comedic value, plus they leak handler internals),
      * private 1-on-1 chats with the bot (not interesting + privacy).
    """
    try:
        msg = update.effective_message
        chat = update.effective_chat
        user = update.effective_user
        if msg is None or chat is None:
            return
        # We only persist group / supergroup / channel chatter, never DMs.
        if getattr(chat, "type", None) == "private":
            return
        if user is not None and getattr(user, "is_bot", False):
            return
        text = (msg.text or msg.caption or "").strip()
        if not text:
            return
        if text.startswith("/"):
            return
        if not db.is_jokes_enabled(str(chat.id)):
            return
        db.log_chat_message(
            str(chat.id),
            message_id=int(msg.message_id) if msg.message_id else None,
            telegram_id=int(user.id) if user and user.id else None,
            username=getattr(user, "username", None) if user else None,
            display_name=(user.full_name if user else None) or None,
            text=text,
        )
    except Exception:
        log.debug("log_chat_message failed", exc_info=True)


# ─────────────────────────────────────────────────────────────────────────────
# Slash commands
# ─────────────────────────────────────────────────────────────────────────────

def _format_jokes_settings(settings: dict, *, in_chat_id: str | int) -> str:
    """Pretty-print current per-chat config + status for /jokes_settings."""
    enabled = bool(settings.get("jokes_enabled"))
    interval = int(settings.get("jokes_interval_minutes") or 0)
    mode = settings.get("jokes_mode") or "normal"
    context = int(settings.get("jokes_context_size") or 100)
    min_msgs = int(settings.get("jokes_min_msgs_since_last") or 20)
    override = (settings.get("jokes_model_override") or "").strip()
    last_at = settings.get("jokes_last_joke_at") or "—"

    last_at_label = str(last_at) if last_at and last_at != "—" else "—"
    # Count how many messages logged since last joke.
    since_last = db.count_messages_since(
        in_chat_id, str(last_at) if last_at and last_at != "—" else None,
    )
    log_total = len(db.recent_chat_messages(in_chat_id, limit=db.JOKES_MAX_CONTEXT))

    state_lbl = "🟢 включено" if enabled else "🔴 выключено"
    interval_lbl = "выкл" if interval <= 0 else f"каждые {interval} мин"
    mode_lbl = mode

    chain = _joke_models()
    if override:
        chain_lbl = f"<code>{html.escape(override)}</code> → " + " → ".join(
            f"<code>{html.escape(m)}</code>" for m in chain
        )
    else:
        chain_lbl = " → ".join(f"<code>{html.escape(m)}</code>" for m in chain)

    return (
        "🃏 <b>Настройки авто-шуток</b>\n\n"
        f"Состояние: {state_lbl}\n"
        f"Интервал авто-шуток: <b>{interval_lbl}</b>\n"
        f"Режим: <b>{html.escape(mode_lbl)}</b>\n"
        f"Контекст для шутки: <b>{context}</b> сообщений\n"
        f"Мин. новых сообщений после последней шутки: <b>{min_msgs}</b>\n\n"
        f"Последняя шутка: <b>{html.escape(last_at_label)}</b>\n"
        f"Сообщений в логе сейчас: <b>{log_total}</b> "
        f"(после последней шутки: <b>{since_last}</b>)\n\n"
        f"Модель (цепочка fallback): {chain_lbl}\n\n"
        "<i>Команды:</i>\n"
        "  /jokes_on, /jokes_off — вкл/выкл логирование и авто-шутки\n"
        "  /jokes_interval &lt;минуты&gt; — частота (0 = только вручную)\n"
        f"  /jokes_mode &lt;{ '|'.join(db.JOKES_VALID_MODES) }&gt;\n"
        "  /jokes_context &lt;N&gt; — сколько сообщений в промпт "
        f"({db.JOKES_MIN_CONTEXT}..{db.JOKES_MAX_CONTEXT})\n"
        "  /jokes_minmsgs &lt;N&gt; — порог накопления для авто-шутки\n"
        "  /jokes_setmodel &lt;model|reset&gt; — модель этого чата (root)\n"
        "  /jokes_clear_log — очистить лог сообщений\n"
        "  /joke — выдать шутку сейчас\n"
        "  /jokes_history [N] — последние N шуток (всем)"
    )


async def cmd_joke(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """``/joke`` — admin-only, manually trigger one joke right now.

    Cooldown: ``_MANUAL_COOLDOWN_SEC`` per chat (anti-spam for the
    OpenRouter quota). Posts the joke into the chat and writes it to
    ``jokes_history`` with ``source='manual'``. Updates
    ``jokes_last_joke_at`` so the auto-loop's interval window
    restarts from now.
    """
    user = update.effective_user
    chat = update.effective_chat
    if user is None or chat is None:
        return
    if not is_admin(user.id):
        await send(update, "❌ Только админ.")
        return
    if not db.is_jokes_enabled(str(chat.id)):
        await send(
            update,
            "❌ В этом чате авто-шутки выключены.\n"
            "Включи логирование и шутки командой <code>/jokes_on</code>.",
        )
        return

    cd = _manual_cooldown_remaining(str(chat.id))
    if cd > 0:
        await send(update, f"⏳ Подожди ещё <b>{cd}</b> сек — анти-спам.")
        return

    # Verify there's enough material to actually joke about.
    rows_count = len(db.recent_chat_messages(str(chat.id), limit=db.JOKES_MAX_CONTEXT))
    if rows_count < db.JOKES_MIN_CONTEXT:
        await send(
            update,
            f"📭 Слишком мало сообщений в логе ({rows_count}/"
            f"{db.JOKES_MIN_CONTEXT}). Нужно ещё немного пообщаться "
            "после <code>/jokes_on</code>.",
        )
        return

    _manual_cooldown_set(str(chat.id))

    # Show the user we're working — long calls can take 10–20 sec.
    notice = None
    try:
        notice = await ctx.bot.send_message(
            chat_id=chat.id,
            text="🤖 Думаю…",
        )
    except TelegramError:
        notice = None

    outcome = await generate_joke_for_chat(str(chat.id))

    # Replace the "Думаю…" notice with the actual joke (or an error).
    final_text: str
    if outcome.text:
        final_text = outcome.text
    else:
        diag = "\n".join(outcome.attempts[-3:]) if outcome.attempts else ""
        final_text = (
            "😶 Шутка не вышла. Все модели вернули пустоту или ошибку.\n"
            + (f"<code>{html.escape(diag[:300])}</code>" if diag else "")
        )

    posted = False
    if notice is not None:
        try:
            await notice.edit_text(
                final_text,
                parse_mode="HTML" if not outcome.text else None,
                disable_web_page_preview=True,
            )
            posted = True
        except TelegramError:
            posted = False
    if not posted:
        try:
            await ctx.bot.send_message(
                chat_id=chat.id,
                text=final_text,
                parse_mode="HTML" if not outcome.text else None,
                disable_web_page_preview=True,
            )
            posted = True
        except TelegramError as e:
            log.warning("/joke failed to post in chat %s: %s", chat.id, e)
            return

    if outcome.text and posted:
        try:
            db.add_joke_history(
                str(chat.id),
                mode=outcome.mode,
                model=outcome.model,
                text=outcome.text,
                context_size=outcome.context_size,
                source="manual",
            )
            db.mark_chat_joke_sent(str(chat.id))
        except Exception:
            log.exception("/joke history bookkeeping failed for chat %s", chat.id)


async def cmd_jokes_on(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """``/jokes_on`` — admin: enable lazy logging + allow ``/joke``.
    Does NOT change the auto-interval; admin sets that separately.
    """
    user = update.effective_user
    chat = update.effective_chat
    if user is None or chat is None:
        return
    if not is_admin(user.id):
        await send(update, "❌ Только админ.")
        return
    db.set_jokes_enabled(str(chat.id), True)
    settings = db.get_jokes_settings(str(chat.id))
    interval = int(settings.get("jokes_interval_minutes") or 0)
    interval_lbl = "выкл (только вручную через /joke)" if interval <= 0 else f"каждые {interval} мин"
    await send(
        update,
        "✅ <b>Авто-шутки включены</b> в этом чате.\n\n"
        "Бот начал логировать сообщения для контекста.\n"
        f"Текущий авто-интервал: <b>{interval_lbl}</b>.\n\n"
        "Запустить шутку прямо сейчас: <code>/joke</code>\n"
        "Изменить настройки: <code>/jokes_settings</code>.",
    )


async def cmd_jokes_off(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """``/jokes_off`` — admin: stop logging and silence auto-loop.
    Existing logged rows stay until ``/jokes_clear_log`` is called.
    """
    user = update.effective_user
    chat = update.effective_chat
    if user is None or chat is None:
        return
    if not is_admin(user.id):
        await send(update, "❌ Только админ.")
        return
    db.set_jokes_enabled(str(chat.id), False)
    await send(
        update,
        "🔴 <b>Авто-шутки выключены</b> в этом чате.\n\n"
        "Логирование сообщений остановлено. Существующий лог сохранён "
        "(стереть — <code>/jokes_clear_log</code>).",
    )


async def cmd_jokes_interval(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """``/jokes_interval <minutes>`` — admin. ``0`` disables auto-loop
    (manual /joke still works). Clamped to ``[JOKES_MIN_INTERVAL_MIN,
    JOKES_MAX_INTERVAL_MIN]`` when positive.
    """
    user = update.effective_user
    chat = update.effective_chat
    if user is None or chat is None:
        return
    if not is_admin(user.id):
        await send(update, "❌ Только админ.")
        return
    args = list(ctx.args or [])
    if not args or not args[0].lstrip("-").isdigit():
        cur = db.get_jokes_settings(str(chat.id))
        cur_int = int(cur.get("jokes_interval_minutes") or 0)
        cur_lbl = "выкл" if cur_int <= 0 else f"{cur_int} мин"
        await send(
            update,
            "Использование: <code>/jokes_interval &lt;минуты&gt;</code>\n\n"
            f"Сейчас: <b>{cur_lbl}</b>\n"
            "Поставь <code>0</code>, чтобы выключить авто-петлю "
            "(ручной <code>/joke</code> остаётся).\n"
            f"Допустимый диапазон при положительном значении: "
            f"<b>{db.JOKES_MIN_INTERVAL_MIN}..{db.JOKES_MAX_INTERVAL_MIN}</b> мин.",
        )
        return
    try:
        minutes = int(args[0])
    except ValueError:
        await send(update, "❌ Минуты — целым числом.")
        return
    db.set_jokes_interval(str(chat.id), minutes)
    new = int(db.get_jokes_settings(str(chat.id)).get("jokes_interval_minutes") or 0)
    if new <= 0:
        msg = "✅ Авто-интервал выключен. Ручной <code>/joke</code> работает."
    else:
        msg = f"✅ Авто-интервал: <b>каждые {new} мин</b>."
    await send(update, msg)


async def cmd_jokes_mode(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """``/jokes_mode <soft|normal|spicy|savage|absurd>`` — admin."""
    user = update.effective_user
    chat = update.effective_chat
    if user is None or chat is None:
        return
    if not is_admin(user.id):
        await send(update, "❌ Только админ.")
        return
    args = list(ctx.args or [])
    if not args:
        cur = db.get_jokes_settings(str(chat.id)).get("jokes_mode") or "normal"
        await send(
            update,
            "Использование: <code>/jokes_mode &lt;режим&gt;</code>\n\n"
            f"Сейчас: <b>{html.escape(cur)}</b>\n"
            f"Доступные: <code>{ '</code>, <code>'.join(db.JOKES_VALID_MODES) }</code>\n\n"
            "<i>Шкала упоротости:</i>\n"
            "  soft — мягкая, без сарказма\n"
            "  normal — лёгкий сарказм (дефолт)\n"
            "  spicy — едкие шутки, чёрный юмор\n"
            "  savage — на грани приличий\n"
            "  absurd — сюрреализм, абсурд",
        )
        return
    mode = args[0].lower().strip()
    if mode not in db.JOKES_VALID_MODES:
        await send(
            update,
            f"❌ Неизвестный режим. Доступно: "
            f"<code>{ '</code>, <code>'.join(db.JOKES_VALID_MODES) }</code>",
        )
        return
    db.set_jokes_mode(str(chat.id), mode)
    await send(update, f"✅ Режим: <b>{html.escape(mode)}</b>.")


async def cmd_jokes_context(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """``/jokes_context <N>`` — admin: how many messages to feed the LLM."""
    user = update.effective_user
    chat = update.effective_chat
    if user is None or chat is None:
        return
    if not is_admin(user.id):
        await send(update, "❌ Только админ.")
        return
    args = list(ctx.args or [])
    if not args or not args[0].lstrip("-").isdigit():
        cur = int(db.get_jokes_settings(str(chat.id)).get("jokes_context_size") or 100)
        await send(
            update,
            "Использование: <code>/jokes_context &lt;N&gt;</code>\n\n"
            f"Сейчас: <b>{cur}</b>\n"
            f"Допустимо: <b>{db.JOKES_MIN_CONTEXT}..{db.JOKES_MAX_CONTEXT}</b>.",
        )
        return
    try:
        n = int(args[0])
    except ValueError:
        await send(update, "❌ Число.")
        return
    db.set_jokes_context_size(str(chat.id), n)
    new = int(db.get_jokes_settings(str(chat.id)).get("jokes_context_size") or 100)
    await send(update, f"✅ Контекст для шутки: <b>{new}</b> сообщений.")


async def cmd_jokes_minmsgs(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """``/jokes_minmsgs <N>`` — admin. Auto-loop floor: how many new
    messages must accumulate before the next auto-joke fires.
    """
    user = update.effective_user
    chat = update.effective_chat
    if user is None or chat is None:
        return
    if not is_admin(user.id):
        await send(update, "❌ Только админ.")
        return
    args = list(ctx.args or [])
    if not args or not args[0].lstrip("-").isdigit():
        cur = int(db.get_jokes_settings(str(chat.id)).get("jokes_min_msgs_since_last") or 20)
        await send(
            update,
            "Использование: <code>/jokes_minmsgs &lt;N&gt;</code>\n\n"
            f"Сейчас: <b>{cur}</b>\n"
            "Это нижняя граница для авто-шутки — не шутить, пока в чате "
            "не накопилось хотя бы N новых сообщений после прошлой шутки.",
        )
        return
    try:
        n = max(0, int(args[0]))
    except ValueError:
        await send(update, "❌ Число.")
        return
    db.set_jokes_min_msgs_since_last(str(chat.id), n)
    await send(update, f"✅ Порог накопления: <b>{n}</b>.")


async def cmd_jokes_setmodel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """``/jokes_setmodel <model>`` — root admin only. Pin one
    OpenRouter model id for this chat. ``reset`` / ``-`` / empty
    restores the default fallback chain.
    """
    user = update.effective_user
    chat = update.effective_chat
    if user is None or chat is None:
        return
    if not is_root_admin(user.id):
        await send(update, "❌ Только root-админ (env ADMIN_IDS).")
        return
    args = list(ctx.args or [])
    if not args:
        cur = (db.get_jokes_settings(str(chat.id)).get("jokes_model_override") or "").strip()
        chain = _joke_models()
        cur_lbl = (
            f"<code>{html.escape(cur)}</code>" if cur
            else "<i>нет — используется дефолтная цепочка</i>"
        )
        await send(
            update,
            "Использование: <code>/jokes_setmodel &lt;model|reset&gt;</code>\n\n"
            f"Сейчас: {cur_lbl}\n"
            f"Дефолтная цепочка: " + " → ".join(
                f"<code>{html.escape(m)}</code>" for m in chain
            ),
        )
        return
    val = args[0].strip()
    if val.lower() in ("reset", "-", "default", "none", "off"):
        db.set_jokes_model_override(str(chat.id), None)
        await send(update, "✅ Сброшено на дефолтную цепочку.")
        return
    db.set_jokes_model_override(str(chat.id), val)
    await send(
        update,
        f"✅ Модель этого чата: <code>{html.escape(val)}</code>.\n"
        "Дефолтная цепочка идёт после неё как fallback.",
    )


async def cmd_jokes_clear_log(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """``/jokes_clear_log`` — admin: drop accumulated chat_messages
    rows for this chat. Doesn't change settings, doesn't touch
    ``jokes_history``.
    """
    user = update.effective_user
    chat = update.effective_chat
    if user is None or chat is None:
        return
    if not is_admin(user.id):
        await send(update, "❌ Только админ.")
        return
    n = db.clear_chat_messages_log(str(chat.id))
    await send(update, f"🗑 Лог сообщений очищен. Удалено: <b>{n}</b>.")


async def cmd_jokes_history(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """``/jokes_history [N]`` — public. Show the last N posted jokes
    for this chat (default 5, max 20).
    """
    chat = update.effective_chat
    if chat is None:
        return
    args = list(ctx.args or [])
    n = 5
    if args and args[0].lstrip("-").isdigit():
        try:
            n = max(1, min(20, int(args[0])))
        except ValueError:
            n = 5
    rows = db.list_jokes_history(str(chat.id), limit=n)
    if not rows:
        await send(update, "📭 В этом чате ещё не было шуток.")
        return
    lines = [f"🃏 <b>Последние шутки</b> (последние {len(rows)}):"]
    for r in rows:
        ts = (r.get("ts") or "—")[:16]
        mode = r.get("mode") or "—"
        model = (r.get("model") or "—").split("/")[-1].split(":")[0]
        src = "ручная" if r.get("source") == "manual" else "авто"
        body = (r.get("text") or "").strip()
        if len(body) > 300:
            body = body[:300].rstrip() + "…"
        body = _strip_mentions(html.escape(body))
        lines.append("")
        lines.append(
            f"<i>{html.escape(str(ts))} · {html.escape(mode)} · "
            f"{html.escape(model)} · {src}</i>"
        )
        lines.append(body)
    await send(update, "\n".join(lines))


async def cmd_jokes_settings(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """``/jokes_settings`` — admin: print current per-chat config
    plus log/queue stats. Static text — change via dedicated commands.
    """
    user = update.effective_user
    chat = update.effective_chat
    if user is None or chat is None:
        return
    if not is_admin(user.id):
        await send(update, "❌ Только админ.")
        return
    settings = db.get_jokes_settings(str(chat.id))
    await send(update, _format_jokes_settings(settings, in_chat_id=str(chat.id)))


# ─────────────────────────────────────────────────────────────────────────────
# Background job — invoked from bot.py via app.job_queue.run_repeating
# ─────────────────────────────────────────────────────────────────────────────

async def job_jokes(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Per-chat auto-joke loop — runs every ~5 min from ``bot.py``.

    For every chat with ``jokes_enabled=true`` AND
    ``jokes_interval_minutes > 0``:
      * skip if interval window hasn't elapsed since ``last_joke_at``,
      * skip if fewer than ``jokes_min_msgs_since_last`` new messages
        have arrived since ``last_joke_at`` (no joking on a dead chat),
      * otherwise: generate, post, update ``last_joke_at``, write
        ``jokes_history``.

    Each chat is independent — failures in one don't block the rest.
    """
    try:
        chats = db.list_chats_with_jokes_enabled()
    except Exception:
        log.exception("job_jokes: list chats failed")
        return
    if not chats:
        return

    now = datetime.utcnow()
    for c in chats:
        chat_id = c.get("chat_id")
        if not chat_id:
            continue
        try:
            interval_min = int(c.get("jokes_interval_minutes") or 0)
        except (TypeError, ValueError):
            interval_min = 0
        if interval_min <= 0:
            continue

        # Time since last joke.
        last_raw = c.get("jokes_last_joke_at")
        last = None
        if last_raw:
            try:
                last = datetime.strptime(str(last_raw), "%Y-%m-%d %H:%M:%S")
            except (TypeError, ValueError):
                last = None
        if last and (now - last).total_seconds() < interval_min * 60:
            continue

        # Min-messages-since-last gate.
        try:
            min_new = int(c.get("jokes_min_msgs_since_last") or 0)
        except (TypeError, ValueError):
            min_new = 0
        if min_new > 0:
            try:
                fresh = db.count_messages_since(
                    chat_id,
                    str(last_raw) if last_raw else None,
                )
            except Exception:
                log.exception("job_jokes: count_messages_since(%s)", chat_id)
                continue
            if fresh < min_new:
                continue

        # Defensive ceiling: skip if log is too thin to write a joke
        # at all (could happen if /jokes_clear_log was just run).
        try:
            log_total = len(db.recent_chat_messages(
                chat_id, limit=db.JOKES_MIN_CONTEXT,
            ))
        except Exception:
            log.exception("job_jokes: recent_chat_messages(%s)", chat_id)
            continue
        if log_total < db.JOKES_MIN_CONTEXT:
            continue

        # Generate.
        try:
            outcome = await generate_joke_for_chat(chat_id)
        except Exception:
            log.exception("job_jokes: generate failed for %s", chat_id)
            continue
        if not outcome.text:
            log.info(
                "job_jokes: no joke for %s (mode=%s err=%s attempts=%s)",
                chat_id, outcome.mode, outcome.error,
                "; ".join(outcome.attempts[-3:]) if outcome.attempts else "—",
            )
            # Still mark as sent so we don't hammer OpenRouter every
            # 5 min when a chat keeps failing — the next attempt waits
            # one full interval.
            try:
                db.mark_chat_joke_sent(chat_id)
            except Exception:
                log.exception("job_jokes: mark_chat_joke_sent(%s) failed", chat_id)
            continue

        # Post.
        try:
            await ctx.bot.send_message(
                chat_id=int(chat_id) if str(chat_id).lstrip("-").isdigit() else chat_id,
                text=outcome.text,
                disable_web_page_preview=True,
            )
        except TelegramError as e:
            log.warning("job_jokes: send to %s failed: %s", chat_id, e)
            # Don't update last_at — let the next tick retry.
            continue
        except Exception:
            log.exception("job_jokes: unexpected send failure for %s", chat_id)
            continue

        # Bookkeeping.
        try:
            db.add_joke_history(
                chat_id,
                mode=outcome.mode,
                model=outcome.model,
                text=outcome.text,
                context_size=outcome.context_size,
                source="auto",
            )
            db.mark_chat_joke_sent(chat_id)
        except Exception:
            log.exception("job_jokes: bookkeeping failed for %s", chat_id)


__all__ = [
    # constants
    "_DEFAULT_JOKE_MODELS",
    # public callables
    "log_chat_message",
    "generate_joke_for_chat",
    "job_jokes",
    "cmd_joke",
    "cmd_jokes_on",
    "cmd_jokes_off",
    "cmd_jokes_interval",
    "cmd_jokes_mode",
    "cmd_jokes_context",
    "cmd_jokes_minmsgs",
    "cmd_jokes_setmodel",
    "cmd_jokes_clear_log",
    "cmd_jokes_history",
    "cmd_jokes_settings",
    # for tests / smoke
    "JokeOutcome",
]
