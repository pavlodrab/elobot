"""``/analyze`` — short AI summary of the last N chat messages.

The user runs ``/analyze [N]``; the bot replies with an inline
keyboard of preset prompts ("Сводка / Планы / Темы / Настроение").
Tapping a preset feeds the last ``N`` messages from
``chat_messages`` into an OpenRouter model and posts the resulting
summary inside an expandable blockquote. The output length cap
scales with ``N`` (300 chars for tiny N → 1200 chars for the full
500-message buffer; see :func:`_response_cap_for_n`) so a half-day
of group chat doesn't get squeezed into a few sentences.

UX summary:
  * ``/analyze``        — open the panel with default N=200.
  * ``/analyze 350``    — open the panel with N=350 (clamped to
                          ``_MIN_N..MAX_N`` = 20..500). 500 is the
                          physical buffer cap (``JOKES_LOG_CAP``).
  * ``/analyze_on``     — admin: enable the privacy gate so future
                          messages are persisted (independent flag
                          from /jokes_on).
  * ``/analyze_off``    — admin: stop logging new messages. Existing
                          buffer is kept until ``/jokes_clear_log``.

Privacy:
  Messages are persisted into ``chat_messages`` whenever EITHER
  ``jokes_enabled`` OR ``analyze_enabled`` is on for that chat —
  see ``handlers.jokes.log_chat_message``. ``/analyze`` and
  ``/jokes`` share the same rolling 500-row buffer to avoid
  double-storage.

Rate limiting:
  Per-user-per-chat: 1 successful run per hour. Admins (root +
  runtime tournament admins, ``handlers.common.is_admin``) bypass
  the limit entirely. The cooldown is only consumed on a successful
  generation, so a transient OpenRouter failure won't burn an hour.

OpenRouter call mechanics intentionally mirror ``handlers.jokes`` —
same key-rotation pool, same fallback-model chain (configurable via
``ANALYZE_MODELS`` env, defaults to the same chain as jokes), same
``urllib`` + ``asyncio.to_thread`` shape — so we keep one mental
model for LLM call sites in this codebase.

Public entry points:
  * :func:`cmd_analyze`        — ``/analyze [N]`` slash command.
  * :func:`cmd_analyze_on`     — ``/analyze_on`` admin slash command.
  * :func:`cmd_analyze_off`    — ``/analyze_off`` admin slash command.
  * :func:`cb_ai_menu`         — single dispatcher for the ``ai:*``
                                 callback_data namespace.
  * :func:`generate_analysis`  — pure orchestrator (no UI), used by
                                 ``cb_ai_menu`` and reusable in
                                 future hooks.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
import time
import urllib.error
import urllib.request

from typing import Callable, Optional

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.error import TelegramError
from telegram.ext import ContextTypes

import database as db

from handlers.common import is_admin, send

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────

# Same fallback chain as /jokes — instruction-tuned free-tier models
# first, experimental tags last. Override globally via the
# ``ANALYZE_MODELS`` env var (comma-separated). We do NOT fall back to
# ``JOKES_MODELS`` because the two features may diverge later (jokes
# wants creative/temperature, analyze wants concise/factual).
_DEFAULT_ANALYZE_MODELS: tuple[str, ...] = (
    "google/gemma-4-31b-it:free",
    "nvidia/nemotron-3-super-120b-a12b:free",
    "openrouter/owl-alpha",
)

_OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

# N (number of recent messages) bounds.
_MIN_N = 20
_MAX_N = 500             # = db.JOKES_LOG_CAP, the physical buffer cap
_DEFAULT_N = 200

# Quick-pick options shown in the "🔄 Поменять N" submenu.
_N_PRESETS: tuple[int, ...] = (50, 100, 200, 300, 500)

# Hard cap on the model's output (chars). Scales with N — see
# :func:`_response_cap_for_n`. The model is also asked to stay under
# this in the prompt, and we trim defensively. Two anchor points:
#   * ``_RESPONSE_HARD_CHARS_MIN`` — used at the smallest N. A 300-char
#     summary feels right for ~50 messages of group chat.
#   * ``_RESPONSE_HARD_CHARS_MAX`` — used at the largest N (full
#     500-message buffer). 1200 chars ≈ 4-5 dense paragraphs, which
#     is what users want when they hit /analyze 500 (a 300-char
#     squish over half a day of chat felt too compressed — see
#     PR following the original /analyze rollout).
_RESPONSE_HARD_CHARS_MIN = 300
_RESPONSE_HARD_CHARS_MAX = 1200

# Per-user-per-chat rate limit for non-admins. Admins bypass it.
_USER_RATE_LIMIT_SEC = 60 * 60   # 1 hour

# OpenRouter request timeout (seconds). Same as jokes — 30s covers
# the slowest free-tier model on a cold request.
_OPENROUTER_TIMEOUT = 30.0

# Hard cap on the user-message context payload sent to OpenRouter.
# Even at the largest output cap (~1200 chars) the prompt budget has
# plenty of headroom; we still cut from the front (oldest first) if
# a chat dumped 500 huge messages.
_CTX_HARD_CAP_CHARS = 9000


def _response_cap_for_n(n: int) -> int:
    """Output character cap for a given ``N`` (number of messages
    being summarised).

    Linear scale, anchored at ``_RESPONSE_HARD_CHARS_MIN`` for
    ``n <= _MIN_N`` and ``_RESPONSE_HARD_CHARS_MAX`` for
    ``n >= _MAX_N``. Rounded down to the nearest 50 so the number we
    inject into the LLM prompt stays clean (the model copes better
    with "не длиннее 700 символов" than "не длиннее 712 символов" —
    free-tier instruction-tuned models are visibly worse at obeying
    odd-looking limits).

    Concretely::

        n   ≤ 20  →  300
        n   = 50  →  400
        n   = 100 →  500
        n   = 200 →  700
        n   = 300 →  900
        n   = 500 → 1200
    """
    n_clamped = max(_MIN_N, min(_MAX_N, int(n)))
    # 300 baseline + 2 chars per logged message — chosen so that
    # N=500 lands exactly at the 1200 ceiling and N=50 at 400.
    cap = _RESPONSE_HARD_CHARS_MIN + n_clamped * 2
    if cap > _RESPONSE_HARD_CHARS_MAX:
        cap = _RESPONSE_HARD_CHARS_MAX
    if cap < _RESPONSE_HARD_CHARS_MIN:
        cap = _RESPONSE_HARD_CHARS_MIN
    # Round DOWN to nearest 50 so the prompt always shows a clean
    # round number ("≤700 символов", not "≤712 символов").
    return (cap // 50) * 50


# ─────────────────────────────────────────────────────────────────────────────
# Presets — preset_key → (label, system_prompt_fragment, temperature)
#
# Each preset is intentionally written as concrete *structural*
# guidance (what to look for, what to skip) rather than vague vibe
# instructions. Free-tier models default to lazy generic summaries
# without specific structure cues.
# ─────────────────────────────────────────────────────────────────────────────

_PRESETS: dict[str, dict] = {
    "summary": {
        "label": "📋 Сводка",
        "system": (
            "Сделай сжатую сводку самого важного из приведённого фрагмента "
            "чата. Перечисли 1–3 ключевых факта/события (договорённости, "
            "даты, решения, итоги обсуждений) — то, что человек, "
            "пропустивший чат, должен знать. Без воды, без оценок, без "
            "общих слов про «атмосферу»."
        ),
        "temperature": 0.3,
    },
    "plans": {
        "label": "🎯 Планы",
        "system": (
            "Выдели из фрагмента чата ПЛАНЫ И ДОГОВОРЁННОСТИ: что "
            "собираются сделать, кто, когда. Если планов явно нет — "
            "так и напиши одной короткой строкой. Не выдумывай планы, "
            "которых в тексте нет; не додумывай детали."
        ),
        "temperature": 0.3,
    },
    "topics": {
        "label": "🧵 Темы",
        "system": (
            "Перечисли 2–4 основные темы, которые обсуждались в "
            "приведённом фрагменте чата. Каждая тема — короткое "
            "именное словосочетание (2–5 слов), через запятую или "
            "новой строкой. Без «обсуждали то-то и то-то», только "
            "сами темы."
        ),
        "temperature": 0.3,
    },
    "mood": {
        "label": "🌡 Настроение",
        "system": (
            "Опиши общее настроение/тон обсуждения в приведённом "
            "фрагменте чата одной-двумя фразами. Опирайся на "
            "конкретику (споры, шутки, согласие, конфликт), а не на "
            "штампы вроде «дружеская атмосфера»."
        ),
        "temperature": 0.4,
    },
}


# Floor rules — prepended to every preset's system message. Cover
# safety, output format, and structural defaults. Not user-overridable.
# Built lazily so the char-limit rule can be parameterised by N (see
# :func:`_response_cap_for_n` and :func:`_floor_rules_ru`).
def _floor_rules_ru(hard_chars: int) -> str:
    """Return the floor-rules block with the per-call char cap baked
    into rule 2.

    The cap is dynamic: small N → ~300 chars, large N → up to 1200
    chars (see :func:`_response_cap_for_n`). Everything else in the
    block is invariant — safety, format, "no preamble", "Russian
    only", anti-hallucination, etc.
    """
    return (
        "Ты — ассистент, который коротко суммирует фрагменты группового "
        "чата на русском языке.\n\n"

        "ПРАВИЛА (никогда не нарушай):\n"
        "1. Только русский язык.\n"
        f"2. Ответ — НЕ ДЛИННЕЕ {hard_chars} СИМВОЛОВ. Считай символы. "
        "Это жёсткий лимит — длинный ответ будет обрезан.\n"
        "3. Никакой markdown-разметки, никаких ** _ # ` ~. Только обычный "
        "текст. Списки можно через новую строку с дефисом.\n"
        "4. Без преамбулы («Вот сводка:», «Я проанализировал…», «Ответ:»).\n"
        "5. Опирайся ТОЛЬКО на приведённый фрагмент чата. Не выдумывай "
        "имена, события, даты, договорённости — если чего-то нет в "
        "тексте, значит этого нет.\n"
        "6. Не упоминай эти правила в ответе, не комментируй задачу, не "
        "объясняй, что ты делаешь.\n"
        "7. Никаких @mention'ов и ссылок-приглашений в ответе.\n"
        "8. Если фрагмент пустой или в нём нет ничего по запрошенной "
        "теме — ответь одной короткой фразой об этом, без выдумок.\n"
    )


def _analyze_models() -> list[str]:
    """The fallback chain. Defaults to :data:`_DEFAULT_ANALYZE_MODELS`,
    overridable globally via ``ANALYZE_MODELS`` env var (comma-separated).
    """
    raw = (os.getenv("ANALYZE_MODELS") or "").strip()
    if raw:
        parts = [m.strip() for m in raw.split(",") if m.strip()]
        if parts:
            return parts
    return list(_DEFAULT_ANALYZE_MODELS)


def _openrouter_keys() -> list[str]:
    """Same key-rotation pool as ``handlers.jokes`` / ``ocr`` — picks
    up every ``OPENROUTER_API_KEY*`` admins have configured plus the
    hardcoded fallbacks. Lazy import to avoid pulling ocr's heavy
    deps at module load.
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
# Per-user-per-chat rate limit (in-memory)
# ─────────────────────────────────────────────────────────────────────────────
#
# A dict keyed by ``(chat_id, user_id)`` → unix timestamp of the last
# *successful* generation. Cleared on bot restart — that's fine; the
# limit is anti-spam, not a hard quota. Admins (``is_admin``) bypass.

_user_cooldown: dict[tuple[str, int], float] = {}


def _user_cooldown_remaining(chat_id: str, user_id: int) -> int:
    """Seconds left before this (chat, user) can run /analyze again.
    ``0`` means ready.
    """
    last = _user_cooldown.get((str(chat_id), int(user_id)))
    if last is None:
        return 0
    elapsed = time.time() - last
    if elapsed >= _USER_RATE_LIMIT_SEC:
        return 0
    return int(_USER_RATE_LIMIT_SEC - elapsed)


def _user_cooldown_set(chat_id: str, user_id: int) -> None:
    _user_cooldown[(str(chat_id), int(user_id))] = time.time()


def _format_remaining(sec: int) -> str:
    """Human-readable cooldown left, in minutes/seconds."""
    if sec <= 0:
        return "0 сек"
    if sec >= 60:
        return f"{sec // 60} мин"
    return f"{sec} сек"


# ─────────────────────────────────────────────────────────────────────────────
# Output sanitiser
# ─────────────────────────────────────────────────────────────────────────────

_MENTION_RE = re.compile(r"@(?=\w)")
_LEADING_LABEL_RE = re.compile(
    r"^\s*(?:сводка|итог|анализ|ответ|summary|analysis)\s*[:\-—–]\s*",
    flags=re.IGNORECASE,
)
# Strip markdown bold/italic/code wrappers if the model ignores rule 3.
_MD_CHARS_RE = re.compile(r"[*_`~]")

# ── CoT / reasoning-leak defenses ────────────────────────────────────────────
#
# Some OpenRouter free-tier models (gpt-oss-style, deepseek-r1-style,
# nemotron-think) put their chain-of-thought into ``message.content``
# instead of (or in addition to) the separate ``message.reasoning``
# field. If we just trim and post that, the user gets the model's
# inner monologue ("We need to summarize the chat fragment, focusing
# on key facts/events. Must be Russian, ≤300 characters…") instead
# of the actual summary.
#
# We defend in two layers:
#   1. Strip explicit ``<think>…</think>`` / ``<reasoning>…</reasoning>``
#      blocks if present — that gives the real answer back when the
#      model wrapped its CoT properly.
#   2. If what's left still LOOKS like meta-thinking (English phrasing
#      restating the task, reciting the prompt rules, or English-only
#      output when we asked for Russian) — reject it. The orchestrator
#      treats a rejection as a soft failure and retries the next model
#      in the fallback chain.

# All the heavy lifting (regex stripping, prefix lists, latin/cyr
# heuristic) lives in ``handlers._llm_safety`` so ``handlers.jokes``
# can share the same defenses without duplicating code. We keep
# only the analyze-specific giveaways here as ``extra_substrings``.
from handlers._llm_safety import (
    looks_like_meta_leak as _shared_looks_like_meta_leak,
    strip_reasoning_blocks as _strip_reasoning_blocks,
)

# Analyze-specific prompt-rule fragments. The summary prompt asks
# "summarize the chat fragment", "no preamble", "key facts/events" —
# when the model echoes any of these back to us, we know it's
# regurgitating instructions instead of answering.
#
# Note: the original list also included literal char-limit echoes
# ("300 characters", "<=300", "≤300"), but the cap is now dynamic
# (300..1200, see :func:`_response_cap_for_n`), so a literal-string
# match would only fire for the smallest N. Universal fragments
# ("no markdown", "must be russian", …) live in
# ``_llm_safety.META_LEAK_SUBSTRINGS_UNIVERSAL`` so we don't
# duplicate them here. CoT openings in English / Russian are caught
# by ``META_LEAK_PREFIXES``, and an English-heavy head trips the
# Latin/Cyrillic ratio check.
_ANALYZE_META_SUBSTRINGS: tuple[str, ...] = (
    "summarize the chat",
    "summarize the conversation",
    "key facts/events",
    "key facts / events",
)


def _looks_like_meta_leak(text: str) -> bool:
    """``True`` if the model dumped its chain-of-thought / restated
    the task instructions instead of producing the summary.

    Thin wrapper around :func:`handlers._llm_safety.looks_like_meta_leak`
    that supplies analyze-specific extra substrings and the analyze
    Latin/Cyrillic threshold (30 chars, 2:1 ratio — strict, since a
    300-char Russian summary should not contain a wall of English).
    """
    return _shared_looks_like_meta_leak(
        text,
        extra_substrings=_ANALYZE_META_SUBSTRINGS,
        latin_threshold=30,
        latin_ratio=2.0,
    )


def _strip_mentions(s: str) -> str:
    """Insert ZWSP after ``@`` so Telegram doesn't ping anybody when
    the bot posts the summary. Visually identical for humans.
    """
    return _MENTION_RE.sub("@\u200b", s or "")


def _clean_response(raw: str, *, hard_chars: int = _RESPONSE_HARD_CHARS_MIN) -> str:
    """Normalise the LLM output: strip ``<think>`` blocks, reject
    chain-of-thought/meta leaks, trim, drop leading "Сводка:" /
    "Анализ:" labels, strip surrounding quotes, kill leftover
    markdown markers, hard-cap at ``hard_chars``, and de-fang
    @mentions.

    ``hard_chars`` is the per-call output ceiling. Defaults to
    :data:`_RESPONSE_HARD_CHARS_MIN` so callers that don't care about
    the dynamic cap (existing tests, ad-hoc smokes) keep working.
    The orchestrator (:func:`generate_analysis`) computes the real
    cap via :func:`_response_cap_for_n` and threads it through both
    the prompt and this trimmer so the prompt's "≤X chars" promise
    matches what we actually enforce.

    Returns ``""`` (empty) when the input is itself a CoT/meta leak
    rather than a real answer — this is a soft-failure signal the
    orchestrator uses to skip to the next model in the fallback
    chain (see :func:`_call_openrouter_sync`).

    Idempotent: ``_clean_response(_clean_response(x)) == _clean_response(x)``.
    """
    if not raw:
        return ""
    # 1. Strip explicit reasoning wrappers BEFORE anything else — if
    #    the model put its real answer after a `<think>…</think>`
    #    block, this gives us the answer.
    s = _strip_reasoning_blocks(str(raw))
    if not s:
        return ""
    # 2. Reject if what's left is itself the CoT / meta-instruction
    #    dump rather than the summary.
    if _looks_like_meta_leak(s):
        return ""
    s = s.strip()
    s = _LEADING_LABEL_RE.sub("", s).strip()
    # Strip leading/trailing matched quote chars.
    for opener, closer in (
        ("«", "»"),
        ("\u201c", "\u201d"),
        ("\u2018", "\u2019"),
        ("\"", "\""),
        ("'", "'"),
    ):
        if s.startswith(opener) and s.endswith(closer) and len(s) >= 2:
            s = s[1:-1].strip()
    # Strip stray markdown markers — model sometimes ignores rule 3.
    s = _MD_CHARS_RE.sub("", s)
    # Hard cap (dynamic — depends on the current N via the caller).
    if hard_chars > 0 and len(s) > hard_chars:
        # Cut on a word boundary if possible, then add an ellipsis.
        cut = s[:hard_chars - 1]
        if " " in cut:
            cut = cut.rsplit(" ", 1)[0]
        s = cut.rstrip(",.;: \t\n") + "…"
    return _strip_mentions(s)


# ─────────────────────────────────────────────────────────────────────────────
# Prompt assembly
# ─────────────────────────────────────────────────────────────────────────────

def _format_author(row: dict) -> str:
    """Pick the friendliest author label for a single line:
    display_name → @username → ``id_<tg>`` → ``аноним``. Same shape
    as the jokes-module renderer for consistency.
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

    Drops empty rows, trims each line to 400 chars, and prunes from
    the front (oldest first) if the total payload exceeds the hard
    cap. This matches the jokes-module renderer exactly so a future
    refactor can move it to a shared helper.
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
    total = sum(len(l) + 1 for l in lines)
    while lines and total > _CTX_HARD_CAP_CHARS:
        dropped = lines.pop(0)
        total -= len(dropped) + 1
    return "\n".join(lines)


def _build_prompt(
    *, preset_key: str, context_text: str, n: int, hard_chars: int,
) -> tuple[str, str, float]:
    """Return ``(system, user, temperature)`` for the OpenRouter call.

    The system message is :func:`_floor_rules_ru` (with the dynamic
    ``hard_chars`` baked in) + the preset's instructions. The user
    message ships the chat fragment with a short framing line and
    repeats the char limit at the tail — free-tier models obey the
    *last* instruction better than the first, so the reminder helps.
    """
    preset = _PRESETS.get(preset_key) or _PRESETS["summary"]
    system = _floor_rules_ru(hard_chars) + "\n" + str(preset["system"])
    user_parts = [
        f"Ниже — последние {n} сообщений группового чата "
        "(в хронологическом порядке, [Имя]: текст):",
        "",
        context_text or "(чат пуст)",
        "",
        "Сделай ответ согласно инструкции в системном промпте. "
        f"Не длиннее {hard_chars} символов. Только сам ответ, без преамбулы.",
    ]
    return system, "\n".join(user_parts), float(preset["temperature"])


# ─────────────────────────────────────────────────────────────────────────────
# OpenRouter call (blocking → wrapped via asyncio.to_thread)
# ─────────────────────────────────────────────────────────────────────────────

def _call_openrouter_sync(
    *,
    system: str,
    user: str,
    temperature: float,
    models: list[str],
    timeout: float = _OPENROUTER_TIMEOUT,
    attempts: list[str] | None = None,
    validator: Callable[[str], str] | None = None,
    max_tokens: int = 400,
) -> tuple[Optional[str], Optional[str]]:
    """Try every (model × key) combo until one returns a non-empty
    content that ``validator`` accepts. Returns ``(text, model_used)``
    or ``(None, None)``.

    ``validator``: optional callable that takes the raw model content
    and returns the cleaned final string, or ``""`` to reject. A
    rejection is treated as a *model-level* failure (CoT/meta leak,
    English-only output, etc.) — we ``break`` out of the key loop
    and move to the next model in the fallback chain, since trying
    the same broken model with a different key won't help.

    ``max_tokens``: per-call output budget. Defaults to 400 (enough
    for the legacy 300-char summary). The orchestrator scales this
    up with N (see :func:`generate_analysis` — ``hard_chars * 2``)
    so longer summaries don't get truncated mid-sentence at the
    OpenRouter side.

    Mirrors :func:`handlers.jokes._call_openrouter_sync` so we keep
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
        # Per-call budget. Cyrillic tokenizes at roughly 1 token/char
        # for newer models, so ``max_tokens`` is set generously above
        # the char cap on the caller side; we hard-cap output on our
        # side anyway.
        "max_tokens": int(max_tokens),
        "temperature": float(temperature),
        # Reasoning hints. ``effort: low`` keeps the CoT short on
        # supported reasoning models; ``exclude: true`` asks
        # OpenRouter to put any chain-of-thought into the separate
        # ``message.reasoning`` field rather than inside ``content``.
        # Models that don't support these hints silently ignore them
        # — and our validator (see ``validator=`` below) catches the
        # cases where the model leaks CoT into ``content`` anyway.
        "reasoning": {"effort": "low", "exclude": True},
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
                    # ASCII-only — the stdlib http.client header
                    # encoder is latin-1 and an em-dash here once
                    # crashed every OpenRouter call (see jokes.py).
                    "X-Title": "FC League Bot - Analyze",
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
                # 401/403/429 — bad key or rate-limited, try next key.
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
                # Defensive validation: some models leak chain-of-thought
                # or English meta-instructions into ``content`` (see
                # ``_clean_response`` / ``_looks_like_meta_leak``). If
                # the validator rejects, it's a model-level problem —
                # don't retry the same model with another key, ``break``
                # the key loop and let the outer model loop fall through
                # to the next model in the chain.
                if validator is not None:
                    cleaned = validator(content)
                    if not cleaned:
                        attempts.append(
                            f"openrouter {model}: rejected (CoT/meta "
                            f"leak, {len(content)} chars)"
                        )
                        log.info(
                            "analyze: %s rejected (CoT/meta leak in "
                            "content, %d chars)",
                            model, len(content),
                        )
                        break
                    content = cleaned
                attempts.append(
                    f"openrouter {model}: OK ({len(content)} chars, {dt:.1f}s)"
                )
                log.info("analyze: %s OK in %.1fs (%d chars)", model, dt, len(content))
                return content, model
            attempts.append(f"openrouter {model}: empty content")
    return None, None


async def _call_openrouter(
    *,
    system: str,
    user: str,
    temperature: float,
    models: list[str],
    timeout: float = _OPENROUTER_TIMEOUT,
    attempts: list[str] | None = None,
    validator: Callable[[str], str] | None = None,
    max_tokens: int = 400,
) -> tuple[Optional[str], Optional[str]]:
    """Async wrapper around the blocking OpenRouter call so the bot
    event loop isn't blocked while we wait on a 30-second HTTP
    request.
    """
    return await asyncio.to_thread(
        _call_openrouter_sync,
        system=system,
        user=user,
        temperature=temperature,
        models=models,
        timeout=timeout,
        attempts=attempts,
        validator=validator,
        max_tokens=max_tokens,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Orchestrator
# ─────────────────────────────────────────────────────────────────────────────

class AnalyzeOutcome:
    """Lightweight result wrapper. Mirrors ``JokeOutcome`` so callers
    have a uniform shape across LLM features.
    """

    __slots__ = ("text", "model", "preset", "n", "context_size", "attempts", "error")

    def __init__(
        self,
        *,
        text: Optional[str],
        model: Optional[str],
        preset: str,
        n: int,
        context_size: int,
        attempts: list[str],
        error: Optional[str] = None,
    ) -> None:
        self.text = text
        self.model = model
        self.preset = preset
        self.n = n
        self.context_size = context_size
        self.attempts = attempts
        self.error = error


async def generate_analysis(
    chat_id: str | int, *, preset_key: str, n: int,
) -> AnalyzeOutcome:
    """Pull the last ``n`` messages, build the preset prompt, call
    OpenRouter, and return an :class:`AnalyzeOutcome`.

    No side effects — does NOT consume the cooldown, does NOT post
    anything. The caller (``cb_ai_menu``) is responsible for both.
    """
    if preset_key not in _PRESETS:
        preset_key = "summary"
    n_clamped = max(_MIN_N, min(_MAX_N, int(n)))
    # Per-call output budget — scales with N. A big N gets a longer
    # cap so we don't squeeze 500 messages of group chat into 300
    # chars (users' actual complaint with the original fixed cap).
    hard_chars = _response_cap_for_n(n_clamped)

    rows = db.recent_chat_messages(chat_id, limit=n_clamped)
    if not rows:
        return AnalyzeOutcome(
            text=None, model=None, preset=preset_key, n=n_clamped,
            context_size=0, attempts=[], error="empty_log",
        )

    context_text = _build_context_text(rows)

    system, user_msg, temperature = _build_prompt(
        preset_key=preset_key, context_text=context_text, n=len(rows),
        hard_chars=hard_chars,
    )

    attempts: list[str] = []
    try:
        raw, model = await _call_openrouter(
            system=system,
            user=user_msg,
            temperature=temperature,
            models=_analyze_models(),
            timeout=_OPENROUTER_TIMEOUT,
            attempts=attempts,
            # max_tokens generously above the char cap — Cyrillic
            # tokenizes at roughly 1 token/char for newer models, so
            # 2× headroom keeps us safe from being truncated mid-
            # sentence; we hard-cap on our side anyway.
            max_tokens=max(400, hard_chars * 2),
            # Validator runs INSIDE the model loop so a CoT-leaking
            # model gets skipped (we move on to the next fallback)
            # instead of returning bogus output to the user. Closure
            # over hard_chars so the validator's hard cap matches
            # what we promised the model in the prompt.
            validator=lambda c: _clean_response(c, hard_chars=hard_chars),
        )
    except Exception as e:
        log.exception("generate_analysis(%s) crashed", chat_id)
        return AnalyzeOutcome(
            text=None, model=None, preset=preset_key, n=n_clamped,
            context_size=len(rows), attempts=attempts,
            error=f"crash: {type(e).__name__}: {e}",
        )

    if not raw:
        return AnalyzeOutcome(
            text=None, model=None, preset=preset_key, n=n_clamped,
            context_size=len(rows), attempts=attempts,
            error="all_models_failed",
        )

    # Validator already cleaned the content above. Run _clean_response
    # one more time as a defensive idempotent pass — cheap, and means
    # any future change to the call path (e.g. a different validator)
    # still gets the canonical output shape.
    cleaned = _clean_response(raw, hard_chars=hard_chars)
    if not cleaned:
        return AnalyzeOutcome(
            text=None, model=model, preset=preset_key, n=n_clamped,
            context_size=len(rows), attempts=attempts,
            error="empty_after_clean",
        )

    return AnalyzeOutcome(
        text=cleaned, model=model, preset=preset_key, n=n_clamped,
        context_size=len(rows), attempts=attempts, error=None,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Render helpers
# ─────────────────────────────────────────────────────────────────────────────

def _render_result(outcome: AnalyzeOutcome) -> str:
    """Final HTML message: header + expandable blockquote with the
    summary. The blockquote is the "hidden quote" the user asked for —
    Telegram renders it collapsed with a "Read more" expander when
    its content is long enough; for ≤300-char results it shows fully
    but still in the visually distinct quote block, which is what the
    user wanted.
    """
    preset_label = (
        _PRESETS.get(outcome.preset, {}).get("label") or outcome.preset
    )
    head = (
        f"🤖 <b>AI-анализ</b> · {html.escape(preset_label)} "
        f"<i>(N={outcome.n})</i>"
    )
    body = html.escape(outcome.text or "")
    # ``<blockquote expandable>`` — Bot API 7.0+ HTML tag, supported
    # by python-telegram-bot 21.x with parse_mode="HTML".
    return f"{head}\n<blockquote expandable>{body}</blockquote>"


def _render_failure(outcome: AnalyzeOutcome) -> str:
    """Fallback message when the LLM call returned nothing. Includes
    a tiny diagnostic tail (last 3 attempts) so admins can debug
    OpenRouter quota/key issues without trawling the bot logs.
    """
    if outcome.error == "empty_log":
        return (
            "📭 В буфере чата ещё нет сообщений для анализа.\n"
            "Включи модуль через <code>/analyze_on</code> и подожди, "
            "пока в чате накопится контекст."
        )
    diag = "\n".join(outcome.attempts[-3:]) if outcome.attempts else ""
    msg = "😶 Не получилось. Все модели вернули пустоту или ошибку."
    if diag:
        msg += f"\n<code>{html.escape(diag[:300])}</code>"
    return msg


# ─────────────────────────────────────────────────────────────────────────────
# Inline keyboard
# ─────────────────────────────────────────────────────────────────────────────
#
# callback_data layout (kept short — Telegram caps at 64 bytes):
#
#   ai:m:<cid>:<n>            — main menu (re-render)
#   ai:p:<cid>:<n>:<preset>   — pick preset → run analysis
#   ai:nm:<cid>:<n>           — open the "change N" submenu
#   ai:ns:<cid>:<n>           — set N (back to main with new N)
#
# All payloads carry the chat_id so the panel survives forwarding /
# being read by the wrong update.

def _main_menu_text(n: int) -> str:
    cap = _response_cap_for_n(n)
    return (
        "🧠 <b>AI-анализ чата</b>\n"
        f"Беру последние <b>N={n}</b> сообщений и делаю короткую "
        f"сводку (≤{cap} символов). Ответ — раскрывающейся цитатой.\n\n"
        "Выбери, что нужно:"
    )


def _main_menu_kb(chat_id: str, n: int) -> InlineKeyboardMarkup:
    """Two rows of preset buttons + a "change N" row. Order matches
    the order they're declared in :data:`_PRESETS` for predictability.
    """
    keys = list(_PRESETS.keys())
    # Pair them up for the keyboard.
    rows: list[list[InlineKeyboardButton]] = []
    pair: list[InlineKeyboardButton] = []
    for k in keys:
        label = _PRESETS[k]["label"]
        pair.append(InlineKeyboardButton(
            label, callback_data=f"ai:p:{chat_id}:{n}:{k}",
        ))
        if len(pair) == 2:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)
    rows.append([
        InlineKeyboardButton(
            "🔄 Поменять N",
            callback_data=f"ai:nm:{chat_id}:{n}",
        ),
    ])
    return InlineKeyboardMarkup(rows)


def _pick_n_kb(chat_id: str, current_n: int) -> InlineKeyboardMarkup:
    """Quick-pick keyboard for N. Marks the current value with a dot
    so the user knows where they are.
    """
    rows: list[list[InlineKeyboardButton]] = []
    pair: list[InlineKeyboardButton] = []
    for value in _N_PRESETS:
        label = f"• {value}" if value == current_n else str(value)
        pair.append(InlineKeyboardButton(
            label, callback_data=f"ai:ns:{chat_id}:{value}",
        ))
        if len(pair) == 3:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)
    rows.append([
        InlineKeyboardButton(
            "↩ Назад",
            callback_data=f"ai:m:{chat_id}:{current_n}",
        ),
    ])
    return InlineKeyboardMarkup(rows)


async def _send_or_edit(
    query, *, text: str, reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    """Edit the panel message in place when possible; fall back to a
    fresh send if Telegram refuses (e.g. message too old, identical
    content). Errors are logged and swallowed — panel UI is best-effort.
    """
    try:
        await query.edit_message_text(
            text,
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=reply_markup,
        )
        return
    except TelegramError as e:
        if "not modified" in str(e).lower():
            return
        log.debug("ai-analyze menu edit failed (%s); falling back", e)
    try:
        if query.message is not None:
            await query.message.chat.send_message(
                text,
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=reply_markup,
            )
    except TelegramError:
        log.warning("ai-analyze menu fallback send also failed", exc_info=True)


# ─────────────────────────────────────────────────────────────────────────────
# Slash commands
# ─────────────────────────────────────────────────────────────────────────────

def _parse_n_arg(args: list[str] | None) -> int:
    """Parse the optional N argument from ``/analyze [N]``. Falls
    back to :data:`_DEFAULT_N` on missing/garbage input. Clamped to
    ``[_MIN_N .. _MAX_N]``.
    """
    if not args:
        return _DEFAULT_N
    raw = args[0].strip().lstrip("-+")
    if not raw.isdigit():
        return _DEFAULT_N
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return _DEFAULT_N
    return max(_MIN_N, min(_MAX_N, n))


async def cmd_analyze(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """``/analyze [N]`` — public.

    Opens the inline preset-picker. If ``analyze_enabled=false`` for
    this chat, prompts an admin to run ``/analyze_on`` first. We do
    NOT auto-enable on the first ``/analyze`` call — that would make
    the privacy gate meaningless.
    """
    chat = update.effective_chat
    user = update.effective_user
    if chat is None or user is None:
        return

    # Refuse in DMs — chat_messages is groups-only by design (privacy
    # + nothing useful to summarise from a 1-on-1 with a bot).
    if getattr(chat, "type", None) == "private":
        await send(
            update,
            "❌ <b>/analyze</b> работает только в групповых чатах.",
        )
        return

    n = _parse_n_arg(list(ctx.args or []))
    user_n_was_clamped = (
        ctx.args and ctx.args[0].strip().lstrip("-+").isdigit()
        and int(ctx.args[0].strip().lstrip("-+")) > _MAX_N
    )

    if not db.is_analyze_enabled(str(chat.id)):
        await send(
            update,
            "❌ Модуль <b>/analyze</b> в этом чате выключен.\n"
            "Админ может включить командой <code>/analyze_on</code>.",
        )
        return

    body = _main_menu_text(n)
    if user_n_was_clamped:
        body += (
            f"\n\n<i>Запрошенный N урезан до {_MAX_N} — "
            "это физический потолок буфера сообщений чата.</i>"
        )
    await send(
        update,
        body,
        reply_markup=_main_menu_kb(str(chat.id), n),
    )


async def cmd_analyze_on(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """``/analyze_on`` — admin: enable per-chat /analyze opt-in.

    Turning this on starts persisting incoming text messages into
    ``chat_messages`` (the same buffer used by /jokes). The buffer
    is capped at ``JOKES_LOG_CAP`` (500 rows) per chat with FIFO
    eviction — we never store more than the rolling window.
    """
    chat = update.effective_chat
    user = update.effective_user
    if chat is None or user is None:
        return
    if not is_admin(user.id):
        await send(update, "❌ Только админ.")
        return
    if getattr(chat, "type", None) == "private":
        await send(
            update,
            "❌ <b>/analyze</b> работает только в групповых чатах.",
        )
        return
    if db.is_analyze_enabled(str(chat.id)):
        await send(update, "ℹ️ Модуль <b>/analyze</b> уже включён в этом чате.")
        return
    db.set_analyze_enabled(str(chat.id), True)
    await send(
        update,
        "✅ Модуль <b>/analyze</b> включён.\n"
        "Бот начал сохранять текстовые сообщения этого чата в "
        "буфер для AI-анализа (до 500 последних).\n"
        "Запусти анализ командой <code>/analyze [N]</code>.",
    )


async def cmd_analyze_off(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """``/analyze_off`` — admin: stop logging new messages.

    The existing buffer is NOT wiped — admins can still run /analyze
    over what was already collected, and ``/jokes_clear_log`` (admin)
    is the way to wipe on demand. Auto-jokes logging is independent;
    if /jokes_on is also active, messages keep flowing into the
    buffer for that feature.
    """
    chat = update.effective_chat
    user = update.effective_user
    if chat is None or user is None:
        return
    if not is_admin(user.id):
        await send(update, "❌ Только админ.")
        return
    if getattr(chat, "type", None) == "private":
        await send(
            update,
            "❌ <b>/analyze</b> работает только в групповых чатах.",
        )
        return
    if not db.is_analyze_enabled(str(chat.id)):
        await send(update, "ℹ️ Модуль <b>/analyze</b> уже выключен.")
        return
    db.set_analyze_enabled(str(chat.id), False)
    await send(
        update,
        "🔴 Модуль <b>/analyze</b> выключен.\n"
        "Новые сообщения этого чата больше не логируются "
        "для AI-анализа. Накопленный буфер не удалён — "
        "очистить можно через <code>/jokes_clear_log</code>.",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Callback dispatcher (ai:* namespace)
# ─────────────────────────────────────────────────────────────────────────────

async def cb_ai_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Single dispatcher for the ``ai:*`` callback_data namespace.

    Layout::

        ai:m:<cid>:<n>            re-render main menu
        ai:p:<cid>:<n>:<preset>   pick preset → run + post result
        ai:nm:<cid>:<n>           open "change N" submenu
        ai:ns:<cid>:<n>           set N → back to main menu
    """
    query = update.callback_query
    if not query or not query.data:
        return
    try:
        await query.answer()
    except TelegramError:
        pass
    user = update.effective_user
    chat = update.effective_chat
    if user is None or chat is None:
        return

    parts = query.data.split(":")
    # parts[0] == "ai"
    if len(parts) < 2:
        return
    action = parts[1]

    def _cid_at(idx: int) -> str | None:
        return parts[idx] if len(parts) > idx else None

    def _int_at(idx: int, default: int) -> int:
        v = _cid_at(idx)
        if v is None:
            return default
        try:
            return int(v)
        except (TypeError, ValueError):
            return default

    # ── ai:m:<cid>:<n> — main menu refresh ────────────────────────────
    if action == "m":
        cid = _cid_at(2)
        n = max(_MIN_N, min(_MAX_N, _int_at(3, _DEFAULT_N)))
        if not cid:
            return
        await _send_or_edit(
            query,
            text=_main_menu_text(n),
            reply_markup=_main_menu_kb(cid, n),
        )
        return

    # ── ai:nm:<cid>:<n> — open change-N submenu ───────────────────────
    if action == "nm":
        cid = _cid_at(2)
        n = max(_MIN_N, min(_MAX_N, _int_at(3, _DEFAULT_N)))
        if not cid:
            return
        await _send_or_edit(
            query,
            text=(
                "🧠 <b>Сколько сообщений анализировать?</b>\n"
                f"Сейчас: <b>N={n}</b>. Лимит — {_MAX_N} "
                "(размер буфера чата)."
            ),
            reply_markup=_pick_n_kb(cid, n),
        )
        return

    # ── ai:ns:<cid>:<n> — apply N preset → back to main ────────────────
    if action == "ns":
        cid = _cid_at(2)
        n = max(_MIN_N, min(_MAX_N, _int_at(3, _DEFAULT_N)))
        if not cid:
            return
        await _send_or_edit(
            query,
            text=_main_menu_text(n),
            reply_markup=_main_menu_kb(cid, n),
        )
        return

    # ── ai:p:<cid>:<n>:<preset> — pick & run ──────────────────────────
    if action == "p":
        cid = _cid_at(2)
        n = max(_MIN_N, min(_MAX_N, _int_at(3, _DEFAULT_N)))
        preset_key = _cid_at(4) or "summary"
        if not cid:
            return
        if preset_key not in _PRESETS:
            preset_key = "summary"

        # Re-check the privacy gate at click time — admin may have
        # toggled /analyze_off between opening the panel and clicking.
        if not db.is_analyze_enabled(cid):
            try:
                await query.answer(
                    "Модуль /analyze в этом чате выключен.",
                    show_alert=True,
                )
            except TelegramError:
                pass
            return

        # Per-user-per-chat rate limit (admins bypass).
        if not is_admin(user.id):
            cd = _user_cooldown_remaining(cid, user.id)
            if cd > 0:
                try:
                    await query.answer(
                        f"⏳ Лимит — раз в час. Подожди ещё "
                        f"{_format_remaining(cd)}.",
                        show_alert=True,
                    )
                except TelegramError:
                    pass
                return

        # Defensive: refuse if buffer is empty (can happen if
        # /analyze_on was just enabled and nobody has typed yet).
        rows_count = len(db.recent_chat_messages(cid, limit=_MAX_N))
        if rows_count == 0:
            try:
                await query.answer(
                    "📭 Буфер пуст. Подожди, пока в чате "
                    "появятся сообщения.",
                    show_alert=True,
                )
            except TelegramError:
                pass
            return

        # Show progress in the chat itself (NOT inside the panel — we
        # want the result to be a normal chat message everyone sees).
        chat_obj = query.message.chat if query.message else None
        notice = None
        try:
            if chat_obj is not None:
                preset_label = _PRESETS[preset_key]["label"]
                notice = await chat_obj.send_message(
                    f"🤖 Анализирую {n} сообщений · {preset_label}…",
                )
        except TelegramError:
            notice = None

        outcome = await generate_analysis(
            cid, preset_key=preset_key, n=n,
        )

        if outcome.text:
            final_text = _render_result(outcome)
            ok = True
        else:
            final_text = _render_failure(outcome)
            ok = False

        # Edit the "Анализирую…" notice into the final result.
        posted = False
        if notice is not None:
            try:
                await notice.edit_text(
                    final_text,
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
                posted = True
            except TelegramError:
                posted = False
        if not posted and chat_obj is not None:
            try:
                await chat_obj.send_message(
                    final_text,
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
                posted = True
            except TelegramError as e:
                log.warning("/analyze post failed in %s: %s", cid, e)

        # Consume the user's cooldown only on success — a transient
        # OpenRouter failure shouldn't burn an hour.
        if ok and posted and not is_admin(user.id):
            _user_cooldown_set(cid, user.id)
        return

    # Unknown action — silently ignore. We don't want to spam errors
    # at users when callback_data drifts between bot versions.
    return


__all__ = [
    # constants
    "_DEFAULT_ANALYZE_MODELS",
    "_MIN_N",
    "_MAX_N",
    "_DEFAULT_N",
    "_RESPONSE_HARD_CHARS_MIN",
    "_RESPONSE_HARD_CHARS_MAX",
    "_USER_RATE_LIMIT_SEC",
    # public callables
    "cmd_analyze",
    "cmd_analyze_on",
    "cmd_analyze_off",
    "cb_ai_menu",
    "generate_analysis",
    # for tests / smoke
    "AnalyzeOutcome",
]
