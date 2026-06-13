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
from typing import Callable, Optional

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    Update,
)
from telegram.error import TelegramError
from telegram.ext import ContextTypes

import database as db

from handlers.common import is_admin, is_root_admin, send

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────

# Default OpenRouter fallback chain — the user picked these models.
# An admin can override per-chat with the model submenu, or globally
# with the ``JOKES_MODELS`` env var (comma-separated).
#
# Order rationale: stable instruction-tuned models first, ":alpha"
# / experimental tags last. Owl-Alpha used to lead the chain but
# kept producing the lazy "Сначала X, потом Y" template across all
# our chats — moved to last position 2026-06.
_DEFAULT_JOKE_MODELS: tuple[str, ...] = (
    "google/gemma-4-31b-it:free",
    "nvidia/nemotron-3-super-120b-a12b:free",
    "openrouter/owl-alpha",
)

_OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

# Per-mode (system prompt fragment, temperature). The mode fragment
# describes *tone* only — what kind of joke we want energetically.
# *Structure* (what to anchor on, what shape the joke takes) lives in
# a separate block (``_STRUCTURE_*_RULES`` below) chosen by
# :func:`_build_prompt` based on whether a topic / custom prompt was
# supplied. Mixing tone and structure in one fragment used to break
# topic mode: the mode preset said "цепляйся за реплику чата" and that
# instruction always won over the topic addendum, so ``/joke <тема>``
# produced a chat-anchored joke with the topic word tacked on.
_MODE_PROMPTS: dict[str, tuple[str, float]] = {
    "soft": (
        "Тёплая, добрая шутка с лёгким панчлайном через неожиданный, "
        "но мягкий угол. Без сарказма, без подколок конкретных людей. "
        "Тон — как у доброго друга, а не как у стендапера.",
        0.7,
    ),
    "normal": (
        "Дружеская шутка с чётким панчлайном — наблюдение, где "
        "концовка ломает заданное ожидание неожиданным углом.",
        0.85,
    ),
    "spicy": (
        "Едкая шутка с чёрным юмором, но с панчлайном — не просто "
        "сарказм. Высмеивай конкретное действие, фразу или деталь, "
        "а не человека целиком и не общую «атмосферу».",
        1.0,
    ),
    "savage": (
        "Беспощадный, сухой панчлайн на грани приличий — одно точное "
        "наблюдение, без растекания. Поэтика, а не помои; точность, "
        "а не громкость.",
        1.1,
    ),
    "absurd": (
        "Сюр с логикой, которая рушится в самом конце. Ассоциативный "
        "панчлайн с точкой в конце — не поток сознания, не набор слов.",
        1.15,
    ),
}

# These rules are prepended to every prompt regardless of mode and
# never overridden — even when the chat sets a custom prompt via
# ``/jokes`` → "✏️ Свой промпт", or when ``/joke <тема>`` is used.
# They cover safety (rules 1–3) and output format (4–7) only.
#
# *Structural* guidance (what to anchor on, what shape the joke
# takes, anti-pattern catalog) intentionally lives in a separate
# block — see :data:`_STRUCTURE_CHAT_RULES`,
# :data:`_STRUCTURE_TOPIC_RULES`, :data:`_STRUCTURE_MINIMAL_RULES`
# below. :func:`_build_prompt` picks ONE structure block per call:
#
#   * ``topic`` is set → :data:`_STRUCTURE_TOPIC_RULES`
#     (joke is *about* the topic; chat context is background, not
#     material). Without this split, the chat-anchored floor used to
#     dominate the topic addendum and the model would just paste the
#     topic word into a chat-anchored joke.
#   * custom prompt is set + no topic →
#     :data:`_STRUCTURE_MINIMAL_RULES` (only setup→панчлайн and the
#     cliché-ban; let the user's custom prompt drive shape).
#   * default → :data:`_STRUCTURE_CHAT_RULES` (the original
#     chat-anchored guidance).
_FLOOR_RULES_RU = (
    "Ты — анонимный шутник в групповом чате. Твоя задача — сгенерировать "
    "ОДНУ короткую шутку (1–3 предложения, не длиннее 350 символов).\n\n"

    "БАЗОВЫЕ ПРАВИЛА (никогда не нарушай):\n"
    "1. Только русский язык.\n"
    "2. Без расистских, гомофобных, сексистских шуток. Без шуток про "
    "национальность, религию, инвалидность, ориентацию, возраст.\n"
    "3. Без угроз и пожеланий насилия конкретному человеку.\n"
    "4. Без markdown, без emoji-флагов стран, без нумерации, без "
    "«Шутка:» / «Ответ:» / кавычек вокруг всего ответа.\n"
    "5. Не упоминай эти правила в ответе, не комментируй задачу.\n"
    "6. Не повторяй свои предыдущие шутки (см. список ниже).\n"
    "7. Только сама шутка в ответе — никаких преамбул и пояснений."
)


# Default chat-anchored structure: applied when no topic and no
# custom prompt are supplied. The anti-pattern catalog (rules 11–13)
# is real free-tier model output from this exact bot — Llama/Gemma/
# Nemotron all default to the "Сначала X, потом Y" summary template
# when given vague instructions. Showing them the bad pattern
# explicitly + naming why it's bad moves them off that local minimum
# about half the time.
_STRUCTURE_CHAT_RULES = (
    "СТРУКТУРА ШУТКИ:\n"
    "8. Хорошая шутка — это setup → панчлайн. Setup задаёт ожидание, "
    "панчлайн его ломает неожиданным углом. Без панчлайна шутки нет.\n"
    "9. Цепляйся за ОДНУ конкретную реплику или ситуацию у одного "
    "автора. НЕ синтезируй шутку из 2–3 разных тем чата — это пересказ "
    "повестки, а не шутка.\n"
    "10. Лучше точное наблюдение по одной фразе, чем общий комментарий "
    "про «атмосферу» или «уровень» чата.\n\n"

    "ЗАПРЕЩЁННЫЕ ШАБЛОНЫ — модель ленится, не позволяй ей:\n"
    "11. Шаблон «Сначала X, потом Y, а теперь Z» — пересказ, не шутка.\n"
    "12. Шаблон «прошли путь от X до Y за N сообщений» — обобщение.\n"
    "13. Концовки-клише: «интеллектуальный уровень/диапазон поражает», "
    "«уровень дискуссии говорит сам за себя», «самокритика на уровне "
    "грандмастера», «классика жанра», «всё как обычно», «X — это новый "
    "Y». Это ленивая ирония вместо панча.\n"
    "14. Если в контексте нет смешного материала — лучше отдай короткое "
    "наблюдение про ОДНУ конкретную реплику, чем имитируй абсурд натянуто.\n\n"

    "ПЛОХИЕ ПРИМЕРЫ (так делать НЕ надо):\n"
    "  ✗ «Сначала выясняли кто Биг Мак, потом про Турнир Гвардиолыча. "
    "Интеллектуальный диапазон чата поражает.»\n"
    "    — пересказ двух тем + клише-концовка, нет панчлайна.\n"
    "  ✗ «Прошли путь от фастфуда до киберспорта за десять сообщений.»\n"
    "    — запрещённый шаблон обобщения, не цепляется за конкретику.\n"
    "  ✗ «Самокритика на уровне грандмастера.»\n"
    "    — клише-концовка вместо наблюдения с панчем."
)


# Topic-focused structure: applied when ``/joke <тема>`` (or a
# free-form "шутку про X" trigger) supplies a topic. Inverts rule
# #9 from the chat-anchored block: now the *topic* is the subject,
# and chat lines become contextual background ("who's in this chat,
# what style do they speak in") rather than material to paste a
# panchline onto.
#
# The "плохие примеры" here are the actual symptom users complained
# about: ``/joke Владыка колес`` came back as a joke about Phoenileo's
# рант про контент with "владыка колёс" приклеенным в конце. With
# the chat-anchored floor in place, that's literally what the model
# was instructed to do; this block tells it to do the opposite.
_STRUCTURE_TOPIC_RULES = (
    "СТРУКТУРА ШУТКИ:\n"
    "8. Хорошая шутка — это setup → панчлайн. Setup задаёт ожидание, "
    "панчлайн его ломает неожиданным углом. Без панчлайна шутки нет.\n"
    "9. Шутка строится ВОКРУГ заданной темы. Тема — это её сюжет, "
    "герой и поинт. И setup, и панчлайн оба должны касаться темы.\n"
    "10. Контекст чата ниже — это ФОН, а НЕ материал для шутки. "
    "Используй его, чтобы понять, в каком чате это говорится и кто "
    "там сидит. НЕ строй шутку из конкретных реплик чата.\n"
    "11. Если тема общеизвестна (вещь, явление, мем, бренд, понятие) "
    "— отталкивайся от ассоциаций, фактов, стереотипов про неё. "
    "Если тема непонятна без контекста (никнейм, локальный мем чата, "
    "название проекта) — найди в контексте намёк, что это значит, "
    "и обыграй именно это значение, а не сам контекст.\n"
    "12. Можно упомянуть участника чата по имени, если это органично "
    "вписывается в шутку про тему. Но шутка должна работать и без "
    "имени — герой шутки = тема, а не имя участника.\n\n"

    "ЗАПРЕЩЁННЫЕ ШАБЛОНЫ:\n"
    "13. Главная ошибка: шутка построена про чат, а тема упомянута "
    "одним словом «для галочки». Например, тема — «погода», а шутка "
    "про то, как Phoenileo жалуется на контент, и в конце «он как "
    "погода». Это НЕ шутка про погоду, это шутка про Phoenileo.\n"
    "14. Пересказ нескольких сообщений чата с приклеенной темой "
    "в конце — тоже мимо.\n"
    "15. Концовки-клише: «классика жанра», «всё как обычно», "
    "«X — это новый Y», «уровень дискуссии», «самокритика на уровне "
    "грандмастера». Это ленивая ирония вместо панча.\n\n"

    "ХОРОШИЙ ПРИМЕР:\n"
    "  Тема: «погода».\n"
    "  ✓ «Метеорологи обещали солнечный день — но забыли указать, "
    "на какой именно планете.»\n"
    "    — setup задаёт ожидание (прогноз), панчлайн ломает (другая "
    "планета), всё про тему.\n\n"

    "ПЛОХИЕ ПРИМЕРЫ (так делать НЕ надо):\n"
    "  Тема: «погода».\n"
    "  ✗ «Phoenileo жалуется на жару, но он сам как погода — вечно "
    "меняется.»\n"
    "    — это шутка про Phoenileo, тема упомянута и забыта.\n"
    "  ✗ «Сначала спорили про Биг Мак, потом про турниры, теперь и "
    "погода подъехала.»\n"
    "    — пересказ чата с темой в конце, не шутка про погоду."
)


# Minimal structure: applied when a custom system prompt is set
# (and no topic). The chat owner has explicitly chosen their own
# style guidance — we don't want to override it with our default
# "anchor to one chat line" instruction. So we only enforce the
# basics: setup→панчлайн and the universally-bad cliché endings.
# The custom prompt itself drives everything else.
#
# This is what the ``/jokes`` menu has been promising users all
# along ("базовые правила безопасности и формата применяются всегда")
# — the implementation just wasn't matching the promise before.
_STRUCTURE_MINIMAL_RULES = (
    "СТРУКТУРА ШУТКИ:\n"
    "8. Хорошая шутка — это setup → панчлайн. Setup задаёт ожидание, "
    "панчлайн его ломает неожиданным углом. Без панчлайна шутки нет.\n\n"

    "ЗАПРЕЩЁННЫЕ ШАБЛОНЫ (всегда):\n"
    "9. Концовки-клише: «интеллектуальный уровень/диапазон поражает», "
    "«уровень дискуссии», «самокритика на уровне грандмастера», "
    "«классика жанра», «всё как обычно», «X — это новый Y». "
    "Это ленивая ирония вместо панча.\n"
    "10. Шаблон «Сначала X, потом Y, а теперь Z» и «прошли путь от "
    "X до Y за N сообщений» — пересказ, не шутка."
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
# CoT / meta-leak defense
# ─────────────────────────────────────────────────────────────────────────────
#
# Same class of bug as ``handlers.ai_analysis`` (see PR #40): some
# free-tier reasoning-tuned models dump their chain-of-thought or
# restate the prompt rules into ``message.content`` instead of
# emitting the actual joke. The user gets garbage like:
#
#   "We need to produce a short joke in Russian, 1-3 sentences, max 350
#    characters, no markdown, no emojis, no quoting entire answer, no
#    mention of rules. Must be a setup → punchline, based on ONE
#    specific line from the chat, about theme «Тимоха и черемша»…"
#
# Defense — three layers:
#   1. Strip ``<think>…</think>`` blocks (some models wrap CoT but the
#      real answer follows after).
#   2. Heuristic ``looks_like_meta_leak`` — rejects open-with-CoT-zachin,
#      regurgitates-prompt-rules, or English-only output.
#   3. Wired as a ``validator=`` into ``_call_openrouter_sync``: a leaked
#      response triggers retry on the next model in the fallback chain
#      (CoT-leak is a model-property, not key-property — re-trying with
#      another key on the same model wouldn't help).
#
# The shared helpers live in ``handlers._llm_safety`` so /analyze and
# /jokes don't drift apart on the prefix/wrapper detection.

from handlers._llm_safety import (
    looks_like_meta_leak as _shared_looks_like_meta_leak,
    strip_reasoning_blocks as _strip_reasoning_blocks,
)

# Joke-prompt-specific giveaways. These appear when the model echoes
# the joke prompt rules ("max 350 characters", "no emojis", "setup →
# punchline", "ONE specific line from the chat") back to us instead
# of writing a joke. Universal fragments (no markdown / no preamble /
# in russian / system prompt) live in ``_llm_safety``.
#
# Tuning note: the patterns are intentionally narrow — the joke
# prompt is the only place in the codebase that talks about "setup →
# punchline" or "350 characters" or "based on theme", so these
# substrings have effectively zero false-positive rate on real jokes.
_JOKE_META_SUBSTRINGS: tuple[str, ...] = (
    "350 characters",
    "max 350",
    "produce a joke",
    "produce a short joke",
    "short joke in russian",
    "setup → punchline",
    "setup -> punchline",
    "1-3 sentences",
    "1–3 sentences",
    "1-3 предложен",
    "one specific line",
    "based on theme",
    "no quoting entire",
    "must be a setup",
    "based on one specific",
)


def _validate_joke_content(raw: str) -> str:
    """Validator + cleaner combined.

    Used as ``validator=`` for :func:`_call_openrouter_sync` so a
    leaking model triggers retry on the next model in the chain.

    Returns the fully-cleaned joke text on success, or ``""`` on
    rejection (CoT/meta leak detected). The empty-string return is
    the *signal* — ``_call_openrouter_sync`` skips to the next model
    on a falsy validator result.

    Pipeline:
      1. ``strip_reasoning_blocks`` — peel any ``<think>…</think>`` /
         ``<reasoning>…</reasoning>`` wrapper. If a stray opener has
         no closer, the model ran out of tokens mid-CoT and there's
         no answer to recover.
      2. ``looks_like_meta_leak`` — reject whatever's left if it
         reads as monologue/instructions rather than a joke. Latin
         threshold is more lenient than ``/analyze`` (60 / 3:1
         instead of 30 / 2:1) because legitimate jokes can name-drop
         English-speaking characters or quote movie lines and would
         tip a strict ratio.
      3. ``_clean_joke_text`` — the canonical post-processing
         (label-strip, quote-strip, length cap, mention de-fanging).
         Idempotent on already-cleaned input.
    """
    s = _strip_reasoning_blocks(raw)
    if not s:
        return ""
    if _shared_looks_like_meta_leak(
        s,
        extra_substrings=_JOKE_META_SUBSTRINGS,
        latin_threshold=60,
        latin_ratio=3.0,
    ):
        return ""
    return _clean_joke_text(s)


# ─────────────────────────────────────────────────────────────────────────────
# Free-form joke-request intent detection
# ─────────────────────────────────────────────────────────────────────────────
#
# Two regex patterns matched against user text in ``handle_text``:
#
#   1. WITH topic:  "(давай/расскажи/...) шутку про <X>"
#                   "(давай/расскажи/...) анекдот про <X>"
#                   "(давай/...) шутку на тему <X>"
#                   verb is OPTIONAL — pure "шутку про X" works.
#
#   2. NO topic:    "давай/расскажи/сделай/кинь шутку"
#                   "давай/... анекдот"
#                   verb is REQUIRED to avoid false positives on
#                   conversational uses of the word "шутка"
#                   ("это шутка такая", "ну ты шутник", etc.).
#
# Both are anchored to the whole message — we don't fire if the
# trigger phrase is buried mid-sentence. Cap the source length so
# pasted novellas don't incur a regex cost.

# Imperative-verb prefix the user might use ("давай/те", "сделай",
# "расскажи", "кинь", "выдай", "запили", "подай", "покажи", "сори"…).
# All optional in the WITH-topic variant. Captured non-greedy so
# they don't eat the topic.
_JOKE_VERB = (
    r"(?:давай(?:те)?|сделай(?:те)?|расскажи(?:те)?|подай(?:те)?|"
    r"кинь(?:те)?|покажи(?:те)?|выдай(?:те)?|запили?(?:те)?|"
    r"сори(?:те)?|сочини(?:те)?|придумай(?:те)?|выдумай(?:те)?)"
)
_JOKE_NOUN = r"(?:анекдот[ауы]?|шутк[ауи]|joke|джоук)"
_JOKE_TOPIC_PREP = r"(?:про|об|о|на\s+тему|по\s+теме)"

_JOKE_INTENT_WITH_TOPIC_RE = re.compile(
    rf"^\s*(?:{_JOKE_VERB}\s+)?{_JOKE_NOUN}\s+{_JOKE_TOPIC_PREP}\s+"
    rf"(.{{2,150}}?)\s*[.!?…]*\s*$",
    flags=re.IGNORECASE | re.UNICODE,
)
_JOKE_INTENT_NO_TOPIC_RE = re.compile(
    rf"^\s*{_JOKE_VERB}\s+{_JOKE_NOUN}\s*[.!?…]*\s*$",
    flags=re.IGNORECASE | re.UNICODE,
)

# Topic-noise tokens to strip BEFORE handing the topic to the LLM.
# These show up when a user types `/joke про погоду` (the word
# "про" leaks into ctx.args) or "шутку на тему черемша" (the
# "на тему" prefix). The free-form regex above already strips them
# via the named group, but cmd_joke args go through this path.
_TOPIC_PREFIX_STRIP_RE = re.compile(
    r"^(?:про|об|о|на\s+тему|по\s+теме)\s+",
    flags=re.IGNORECASE,
)


def _normalize_topic(s: str | None) -> str:
    """Trim, strip surrounding quotes/punctuation, drop a leading
    "про/о/об/на тему/по теме", and cap to 150 chars.

    Returns ``""`` when there's nothing useful left — callers treat
    that as "no topic" and fall back to context-only generation.
    """
    if not s:
        return ""
    out = str(s).strip()
    # Strip surrounding quote pairs.
    for opener, closer in (
        ("«", "»"),
        ("\u201c", "\u201d"),
        ("\u2018", "\u2019"),
        ("\"", "\""),
        ("'", "'"),
    ):
        if out.startswith(opener) and out.endswith(closer) and len(out) >= 2:
            out = out[1:-1].strip()
    out = out.strip(" .!?,…")
    out = _TOPIC_PREFIX_STRIP_RE.sub("", out).strip()
    return out[:150]


def detect_joke_intent(text: str | None) -> Optional[str]:
    """Inspect a free-form chat message and decide whether it reads
    as "the user is asking the bot for a joke".

    Returns:
      * ``None``  — not a joke request, leave the message alone.
      * ``""``    — a joke request without a specified topic
                    (e.g. "Давай шутку").
      * ``"X"``   — a joke request with topic ``X``
                    (e.g. "Давай шутку про черемшу" → ``"черемшу"``).

    The two-tier output lets the caller reuse the same downstream
    code path for slash-command and free-form entry points.

    Conservative on purpose: a no-topic match REQUIRES an imperative
    verb prefix so chat lines like "это просто шутка такая" don't
    fire. The topic variant accepts a bare "шутку про X" because the
    "про X" tail is itself a strong signal of intent.
    """
    if not text:
        return None
    s = str(text).strip()
    if not s or len(s) > 400:
        return None
    m = _JOKE_INTENT_WITH_TOPIC_RE.match(s)
    if m:
        return _normalize_topic(m.group(1))
    if _JOKE_INTENT_NO_TOPIC_RE.match(s):
        return ""
    return None


def parse_topic_from_args(args: list[str] | None) -> str:
    """Turn ``ctx.args`` (slash-command tail) into a clean topic
    string. Strips a leading "про/о/об/на тему/по теме" if present —
    users frequently write ``/joke про погоду`` after seeing the
    free-form trigger work.
    """
    if not args:
        return ""
    return _normalize_topic(" ".join(args))


# ─────────────────────────────────────────────────────────────────────────────
# Prompt assembly
# ─────────────────────────────────────────────────────────────────────────────

_CTX_HARD_CAP_CHARS = 6000   # OpenRouter context cap budget for the user msg

# Reaction-emoji scoring vocabulary. We keep this deliberately broad on
# the positive side (Telegram's premium emoji set + common chat reactions)
# and narrow on the negative side (only unambiguously-disapproving
# emojis count against the joke). Anything not listed here scores 0 —
# it's a reaction, but neither a thumbs-up nor a thumbs-down.
#
# These are the emoji *characters* Telegram puts in
# ``ReactionTypeEmoji.emoji``. Custom-emoji reactions
# (``ReactionTypeCustomEmoji``) are uncommon in groups and we ignore
# them — their semantics are chat-specific and we'd need a per-chat
# polarity map, which isn't worth the complexity right now.
_POSITIVE_EMOJIS: frozenset[str] = frozenset({
    "👍", "❤", "❤️", "🔥", "🥰", "👏", "😁", "🎉", "🤩", "💯",
    "🤣", "😂", "🤗", "🤡", "🆒", "❤‍🔥", "❤️‍🔥", "🌚",
    "💋", "🤝", "🦄", "😘", "🏆", "💘", "🤓", "✨",
})
_NEGATIVE_EMOJIS: frozenset[str] = frozenset({
    "👎", "💩", "🤮", "😱", "🤬", "😢", "😨", "🥱", "🥴",
    "😴", "😭", "🤨", "🖕", "☠", "☠️",
})


def _score_emoji(emoji: str) -> int:
    """Map one reaction emoji to ``+1 / -1 / 0``. Unknown emojis are
    neutral — the joke-reaction loop only wants signal where the chat
    *clearly* approved or disapproved.
    """
    if not emoji:
        return 0
    if emoji in _POSITIVE_EMOJIS:
        return 1
    if emoji in _NEGATIVE_EMOJIS:
        return -1
    return 0


def _reactions_to_score(reactions) -> tuple[int, dict[str, int]]:
    """Convert a list of :class:`telegram.ReactionType` (per-user new
    reactions) into ``(net_score, {emoji: 1, ...})``.

    Per-user lists are short (Telegram caps them at one or a few),
    so we always treat each entry as ``count=1``.
    """
    score = 0
    snapshot: dict[str, int] = {}
    for r in reactions or ():
        emoji = getattr(r, "emoji", None)
        if not emoji:
            continue
        score += _score_emoji(emoji)
        snapshot[emoji] = snapshot.get(emoji, 0) + 1
    return score, snapshot


def _reaction_counts_to_score(reaction_counts) -> tuple[int, dict[str, int]]:
    """Convert a list of :class:`telegram.ReactionCount` (chat-wide
    aggregate from ``message_reaction_count``) into
    ``(net_score, {emoji: total_count, ...})``.
    """
    score = 0
    snapshot: dict[str, int] = {}
    for rc in reaction_counts or ():
        rtype = getattr(rc, "type", None)
        emoji = getattr(rtype, "emoji", None) if rtype else None
        try:
            n = int(getattr(rc, "total_count", 0) or 0)
        except (TypeError, ValueError):
            n = 0
        if not emoji or n <= 0:
            continue
        score += _score_emoji(emoji) * n
        snapshot[emoji] = snapshot.get(emoji, 0) + n
    return score, snapshot


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


def _format_top_jokes_for_prompt(top: list[dict], limit: int = 3) -> str:
    """Top-scoring past jokes for this chat — fed into the prompt as
    *style exemplars*, NOT as templates to copy. The model is told
    explicitly to imitate vibe/structure, not subject matter.

    A joke without a score (``score == 0``) is filtered out by the
    DB query already; this helper just renders what survives.
    Returns empty string when there's no signal yet.
    """
    if not top:
        return ""
    bits: list[str] = []
    for h in top[:limit]:
        t = (h.get("text") or "").strip().replace("\n", " ")
        score = h.get("score") or 0
        if not t:
            continue
        if len(t) > 220:
            t = t[:220].rstrip() + "…"
        bits.append(f"- (👍{score}) {t}")
    if not bits:
        return ""
    return (
        "Шутки этого чата, которые получили положительные реакции "
        "(👍/❤️/🔥 и т.п.) — ИМИТИРУЙ их стиль, ритм, длину, тип "
        "панчлайна. НЕ копируй их сюжет и не повторяй формулировки:\n"
        + "\n".join(bits)
    )


def _format_replies_for_prompt(replies: list[dict], limit: int = 8) -> str:
    """Recent chat replies *to* the bot's previous jokes. Each line
    pairs a short joke snippet with the reply, so the LLM can see
    which jokes landed and which ones the chat dunked on.

    Replies are ordered newest-first (DB does that for us) but we
    render oldest-first inside the limit window so the prompt reads
    chronologically.
    """
    if not replies:
        return ""
    window = list(replies[:limit])
    window.reverse()
    bits: list[str] = []
    for r in window:
        joke = (r.get("joke_text") or "").strip().replace("\n", " ")
        reply = (r.get("reply_text") or "").strip().replace("\n", " ")
        if not reply:
            continue
        if len(joke) > 120:
            joke = joke[:120].rstrip() + "…"
        if len(reply) > 160:
            reply = reply[:160].rstrip() + "…"
        author = (r.get("display_name") or r.get("username") or "аноним").strip()
        if len(author) > 30:
            author = author[:30] + "…"
        bits.append(
            f"- шутка: «{joke}» → ответ {author}: «{reply}»"
        )
    if not bits:
        return ""
    return (
        "Реакции чата на твои предыдущие шутки (что ответили текстом). "
        "Учти их вкус — что зашло, что нет — и подстрой стиль:\n"
        + "\n".join(bits)
    )


# Tighter context cap for topic mode. With a topic, the chat
# context is BACKGROUND, not material — we don't want it dominating
# the user message. ~1500 chars (~25–30 short lines) is enough to
# convey style and identify participants, without drowning the
# topic instruction.
_CTX_TOPIC_HARD_CAP_CHARS = 1500


def _trim_context_to_cap(text: str, cap: int) -> str:
    """Drop oldest lines until the rendered context fits under
    ``cap`` characters. Used in topic mode to shrink the chat-context
    block from the default 6000-char budget down to ~1500 so the
    topic stays prominent in the user message.
    """
    if not text or len(text) <= cap:
        return text
    lines = text.split("\n")
    total = sum(len(l) + 1 for l in lines)
    while lines and total > cap:
        dropped = lines.pop(0)
        total -= len(dropped) + 1
    return "\n".join(lines)


def _build_prompt(
    *,
    mode: str,
    context_text: str,
    history_text: str,
    custom_prompt: str | None = None,
    top_text: str = "",
    replies_text: str = "",
    topic: str | None = None,
) -> tuple[str, str, float]:
    """Return ``(system, user, temperature)`` for the OpenRouter call.

    System message is assembled in three parts:

      1. :data:`_FLOOR_RULES_RU` — safety + format. Always applied,
         never overridable.
      2. A structure block — ONE of:

           * :data:`_STRUCTURE_TOPIC_RULES` when ``topic`` is set.
             Inverts chat-anchoring: joke is *about* the topic, chat
             context is background only.
           * :data:`_STRUCTURE_MINIMAL_RULES` when ``custom_prompt``
             is set and there's no topic. Just setup→панчлайн and
             the cliché-ban — lets the custom prompt drive shape.
           * :data:`_STRUCTURE_CHAT_RULES` otherwise (default).

      3. The style fragment — ``custom_prompt`` if set, otherwise the
         mode preset from :data:`_MODE_PROMPTS`. Temperature still
         comes from the mode (custom prompt is the *style*; mode is
         the *energy*).

    User message is also restructured in topic mode: topic at top
    AND bottom, chat context capped to
    :data:`_CTX_TOPIC_HARD_CAP_CHARS` and labelled as "background,
    not material". Without the cap, 6KB of context drowned the
    topic instruction and the model defaulted to chat-anchoring
    even when the system prompt told it to focus on the topic.

    ``top_text`` and ``replies_text`` (feedback-loop blocks) are
    always included when present — style exemplars and reply-vibes
    are useful in either mode.
    """
    mode_prompt, temperature = _MODE_PROMPTS.get(mode, _MODE_PROMPTS["normal"])
    style_prompt = (custom_prompt or "").strip() or mode_prompt
    has_custom_prompt = bool((custom_prompt or "").strip())

    topic_clean = (topic or "").strip()[:150]

    # Pick the structure block. Topic always wins over custom-prompt
    # — when the user supplied a topic they want a topic joke; the
    # custom prompt becomes the "voice" but doesn't override the
    # topic-focused structure.
    if topic_clean:
        structure_block = _STRUCTURE_TOPIC_RULES
    elif has_custom_prompt:
        structure_block = _STRUCTURE_MINIMAL_RULES
    else:
        structure_block = _STRUCTURE_CHAT_RULES

    system_parts = [
        _FLOOR_RULES_RU,
        "",
        structure_block,
        "",
        "СТИЛЬ ЭТОЙ ШУТКИ:",
        style_prompt,
    ]
    if topic_clean:
        # Topic is also restated at the end of the system block as a
        # numbered hard-constraint, so the model sees it as a rule
        # rather than a hint. The "приоритетно" label is deliberate —
        # if anything in the structure block somehow conflicts with
        # the topic, the topic wins.
        system_parts += [
            "",
            "ТЕМА ЭТОЙ ШУТКИ (приоритетно):",
            f"  «{topic_clean}»",
            "  Шутка ОБЯЗАНА быть про эту тему — она её сюжет, "
            "а не декорация. Если какие-то правила выше конфликтуют "
            "с этим — тема важнее.",
        ]
    system = "\n".join(system_parts)

    # ── User message ──────────────────────────────────────────────
    user_parts: list[str] = []
    if topic_clean:
        # Topic mode: lead with topic, render the chat context as
        # background only (with a stricter cap so it doesn't drown
        # the topic), bookend with the topic instruction.
        user_parts.append(f"ТЕМА (главное): «{topic_clean}»")
        user_parts.append("")
        user_parts.append(
            f"Сгенерируй ОДНУ шутку про «{topic_clean}». Тема — это "
            "сюжет шутки, а не декорация."
        )
        user_parts.append("")
        small_ctx = _trim_context_to_cap(
            context_text, _CTX_TOPIC_HARD_CAP_CHARS,
        )
        user_parts.append(
            "Контекст чата ниже — это ФОН (для понимания стиля чата "
            "и его участников), а НЕ материал для шутки. НЕ цитируй "
            "конкретные реплики, НЕ строй шутку про чат с темой "
            "в конце:"
        )
        user_parts.append("")
        user_parts.append(
            small_ctx or "(чат пуст — отталкивайся только от темы)"
        )
    else:
        # Default chat-anchored mode: original layout.
        user_parts += [
            "Ниже — последние сообщения группового чата (в "
            "хронологическом порядке, [Имя]: текст):",
            "",
            context_text or "(чат пуст или ничего смешного)",
        ]

    if top_text:
        user_parts.append("")
        user_parts.append(top_text)
    if replies_text:
        user_parts.append("")
        user_parts.append(replies_text)
    if history_text:
        user_parts.append("")
        user_parts.append(history_text)

    user_parts.append("")
    if topic_clean:
        user_parts.append(
            f"Сгенерируй ровно одну шутку ПРО «{topic_clean}» "
            "(не про чат, а про саму тему). Только саму шутку, "
            "без преамбулы."
        )
    else:
        user_parts.append(
            "Сгенерируй ровно одну шутку. Только саму шутку, "
            "без преамбулы."
        )
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
    validator: Callable[[str], str] | None = None,
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

    Mirrors :func:`tournament_summary._try_openrouter` and
    :func:`handlers.ai_analysis._call_openrouter_sync` so we have
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
        # Reasoning hints. ``effort: low`` keeps any CoT short on
        # supported reasoning models; ``exclude: true`` asks
        # OpenRouter to put the chain-of-thought into the separate
        # ``message.reasoning`` field rather than inside ``content``.
        # Models that don't support these hints silently ignore them
        # — the validator wired in below catches the cases where the
        # model leaks CoT into ``content`` regardless.
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
                    # NB: HTTP headers are latin-1 in stdlib http.client.
                    # Keep this ASCII-only — em-dash here used to crash
                    # every OpenRouter call with UnicodeEncodeError.
                    "X-Title": "FC League Bot - Auto-jokes",
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
                # Defensive validation: some models leak chain-of-thought
                # or English meta-instructions into ``content`` (see
                # ``_validate_joke_content`` for the heuristic). If the
                # validator rejects, it's a model-level problem — don't
                # retry the same model with another key, ``break`` the
                # key loop and let the outer model loop fall through to
                # the next model in the chain.
                if validator is not None:
                    cleaned = validator(content)
                    if not cleaned:
                        attempts.append(
                            f"openrouter {model}: rejected (CoT/meta "
                            f"leak, {len(content)} chars)"
                        )
                        log.info(
                            "jokes: %s rejected (CoT/meta leak in "
                            "content, %d chars)",
                            model, len(content),
                        )
                        break
                    content = cleaned
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
    validator: Callable[[str], str] | None = None,
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
        validator=validator,
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


async def generate_joke_for_chat(
    chat_id: str | int, *, topic: str | None = None,
) -> JokeOutcome:
    """Pull recent messages, build prompt per chat's settings, call
    OpenRouter, and return a :class:`JokeOutcome`. Does NOT write to
    ``jokes_history`` or update ``last_joke_at`` — the caller does
    that after a successful Telegram post (so we don't claim "joke
    sent" when Telegram itself fails).

    ``topic`` (optional) — when provided, the prompt is anchored to
    that subject and an empty chat buffer is no longer a hard error
    (we still produce a topic-only joke). When ``None`` / empty, the
    behaviour is identical to the pre-topic implementation.
    """
    settings = db.get_jokes_settings(chat_id)
    mode = settings.get("jokes_mode") or "normal"
    if mode not in db.JOKES_VALID_MODES:
        mode = "normal"
    context_n = int(settings.get("jokes_context_size") or 100)
    context_n = max(db.JOKES_MIN_CONTEXT, min(db.JOKES_MAX_CONTEXT, context_n))

    topic_clean = _normalize_topic(topic)
    rows = db.recent_chat_messages(chat_id, limit=context_n)
    # An empty buffer is normally a hard fail — without recent chat
    # text the auto-loop has nothing to be funny about. With a
    # user-supplied topic we have a fallback subject, so allow it.
    if not rows and not topic_clean:
        return JokeOutcome(
            text=None, model=None, mode=mode, context_size=0, attempts=[],
            error="empty_log",
        )

    context_text = _build_context_text(rows) if rows else ""
    history = db.list_jokes_history(chat_id, limit=5)
    history_text = _format_recent_jokes_for_prompt(history)

    # Feedback loop: top-scoring past jokes (style exemplars) +
    # recent text replies the chat sent in response to bot jokes.
    # Both are best-effort — if a chat has no signal yet we fall
    # through to the original context-only prompt.
    try:
        top_rated = db.list_top_reacted_jokes(
            chat_id, limit=3, min_score=1, max_age_days=60,
        )
    except Exception:
        log.debug("list_top_reacted_jokes failed for %s", chat_id, exc_info=True)
        top_rated = []
    top_text = _format_top_jokes_for_prompt(top_rated)

    try:
        recent_replies = db.list_recent_replies_for_chat(chat_id, limit=8)
    except Exception:
        log.debug("list_recent_replies_for_chat failed for %s", chat_id, exc_info=True)
        recent_replies = []
    replies_text = _format_replies_for_prompt(recent_replies)

    custom_prompt = (settings.get("jokes_custom_prompt") or "").strip() or None

    system, user, temperature = _build_prompt(
        mode=mode, context_text=context_text, history_text=history_text,
        custom_prompt=custom_prompt,
        top_text=top_text, replies_text=replies_text,
        topic=topic_clean or None,
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
            # Validator runs INSIDE the model loop so a CoT-leaking
            # model gets skipped (we move on to the next fallback)
            # instead of returning bogus output to the user. See the
            # CoT/meta-leak defense block near ``_validate_joke_content``.
            validator=_validate_joke_content,
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

    # Validator already cleaned the content above. Run _clean_joke_text
    # one more time as a defensive idempotent pass — cheap, and means
    # any future change to the call path (e.g. a different validator)
    # still gets the canonical output shape.
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
        if not (
            db.is_jokes_enabled(str(chat.id))
            or db.is_analyze_enabled(str(chat.id))
        ):
            return
        db.log_chat_message(
            str(chat.id),
            message_id=int(msg.message_id) if msg.message_id else None,
            telegram_id=int(user.id) if user and user.id else None,
            username=getattr(user, "username", None) if user else None,
            display_name=(user.full_name if user else None) or None,
            text=text,
        )

        # Feedback loop: if this message is a reply to one of the
        # bot's tracked jokes, also persist it to ``joke_replies``
        # so future prompts can show the LLM "вот как чат ответил
        # на твою предыдущую шутку". We don't gate this on
        # ``jokes_enabled`` separately — getting here already means
        # the chat opted in (jokes or analyze). The reply is logged
        # in BOTH ``chat_messages`` (general context) and
        # ``joke_replies`` (joke-linked feedback) — that's
        # intentional, the two stores serve different prompt slots.
        try:
            reply_to = getattr(msg, "reply_to_message", None)
            if reply_to is not None and getattr(reply_to, "message_id", None):
                joke_row = db.get_joke_by_message(
                    str(chat.id), int(reply_to.message_id)
                )
                if joke_row and joke_row.get("id"):
                    db.add_joke_reply(
                        joke_history_id=int(joke_row["id"]),
                        chat_id=str(chat.id),
                        telegram_id=int(user.id) if user and user.id else None,
                        username=getattr(user, "username", None) if user else None,
                        display_name=(user.full_name if user else None) or None,
                        text=text,
                    )
        except Exception:
            log.debug(
                "log_chat_message: joke-reply detection failed in chat %s",
                getattr(chat, "id", "?"), exc_info=True,
            )
    except Exception:
        log.debug("log_chat_message failed", exc_info=True)


# ─────────────────────────────────────────────────────────────────────────────
# Slash commands & inline menu (/jokes)
# ─────────────────────────────────────────────────────────────────────────────
#
# UX model (since 2026-06): the only admin-facing slash command is
# ``/jokes`` — it opens an inline-keyboard panel where every setting
# is a button. The legacy ``/jokes_on``/``/jokes_off``/``/jokes_*``
# commands were retired; their handlers were removed and only ``/joke``
# (manual trigger) and ``/jokes_history`` (public read) survive at the
# slash-command level.

# Preset values shown as quick-pick buttons in the submenus. Each
# submenu also has an "✏️ Ввести вручную" escape hatch that drops the
# user into a pending-input state consumed by
# :func:`handle_pending_jokes_text`.
_JOKES_INTV_PRESETS: tuple[int, ...] = (30, 60, 120, 240, 360, 720, 1440)
_JOKES_CTX_PRESETS: tuple[int, ...] = (20, 50, 100, 150, 200)
_JOKES_MIN_PRESETS: tuple[int, ...] = (0, 5, 10, 20, 50, 100)

# Pretty labels for mode buttons; the underlying value is the bare
# JOKES_VALID_MODES key.
_JOKES_MODE_LABELS: dict[str, str] = {
    "soft":   "😇 soft",
    "normal": "🙂 normal",
    "spicy":  "🌶 spicy",
    "savage": "🔥 savage",
    "absurd": "🌀 absurd",
}


def _intv_label(minutes: int) -> str:
    """Human-readable interval label for status panels & buttons."""
    if minutes <= 0:
        return "выкл"
    if minutes == 1440:
        return "каждые 1440 мин (сутки)"
    return f"каждые {minutes} мин"


def _jokes_menu_text(chat_id: str | int) -> str:
    """Render the main panel body for ``/jokes`` (and any 'refresh' or
    'back to main' callback). Pure read — touches DB only.
    """
    s = db.get_jokes_settings(chat_id)
    enabled  = bool(s.get("jokes_enabled"))
    interval = int(s.get("jokes_interval_minutes") or 0)
    mode     = s.get("jokes_mode") or "normal"
    context  = int(s.get("jokes_context_size") or 100)
    min_msgs = int(s.get("jokes_min_msgs_since_last") or 20)
    override = (s.get("jokes_model_override") or "").strip()
    custom_prompt = (s.get("jokes_custom_prompt") or "").strip()
    last_at  = s.get("jokes_last_joke_at") or "—"

    log_total = len(db.recent_chat_messages(chat_id, limit=db.JOKES_MAX_CONTEXT))
    since_last = db.count_messages_since(
        chat_id,
        str(last_at) if last_at and last_at != "—" else None,
    )

    state_lbl = "🟢 включено" if enabled else "🔴 выключено"
    if override:
        model_lbl = f"<code>{html.escape(override)}</code>"
    else:
        model_lbl = "<i>дефолтная цепочка</i>"
    if custom_prompt:
        prompt_lbl = f"задан ({len(custom_prompt)} симв.)"
    else:
        prompt_lbl = "<i>—</i>"

    return (
        "🃏 <b>Авто-шутки</b>\n\n"
        f"Состояние: <b>{state_lbl}</b>\n"
        f"Интервал: <b>{html.escape(_intv_label(interval))}</b>\n"
        f"Режим: <b>{html.escape(mode)}</b>\n"
        f"Свой промпт: {prompt_lbl}\n"
        f"Контекст: <b>{context}</b> сообщений\n"
        f"Порог: <b>{min_msgs}</b> новых перед авто-шуткой\n"
        f"Модель: {model_lbl}\n"
        f"Последняя: <b>{html.escape(str(last_at))}</b>\n"
        f"В логе: <b>{log_total}</b> сообщений "
        f"(после последней: <b>{since_last}</b>)"
    )


def _jokes_menu_kb(
    chat_id: str | int, *, enabled: bool, is_root: bool,
) -> InlineKeyboardMarkup:
    """Main panel keyboard. The "🤖 Модель ▸" row is only shown to
    root admins; all other rows are visible to anyone but each
    callback re-checks ``is_admin`` before mutating state.
    """
    cid = str(chat_id)
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton("▶️ Шутку сейчас", callback_data=f"j:run:{cid}")],
        [
            InlineKeyboardButton(
                "🔴 Выключить" if enabled else "🟢 Включить",
                callback_data=f"j:toggle:{cid}",
            ),
            InlineKeyboardButton("📜 История", callback_data=f"j:hist:{cid}"),
        ],
        [InlineKeyboardButton("⏰ Интервал ▸", callback_data=f"j:intv:{cid}")],
        [InlineKeyboardButton("🌶 Режим ▸",   callback_data=f"j:mode:{cid}")],
        [InlineKeyboardButton("✏️ Свой промпт ▸", callback_data=f"j:prompt:{cid}")],
        [InlineKeyboardButton("🧠 Контекст ▸", callback_data=f"j:ctx:{cid}")],
        [InlineKeyboardButton("📊 Порог накопления ▸", callback_data=f"j:min:{cid}")],
    ]
    if is_root:
        rows.append(
            [InlineKeyboardButton("🤖 Модель ▸ (root)", callback_data=f"j:model:{cid}")]
        )
    rows.append([
        InlineKeyboardButton("🗑 Очистить лог", callback_data=f"j:clear:{cid}"),
        InlineKeyboardButton("🔄 Обновить",     callback_data=f"j:menu:{cid}"),
    ])
    return InlineKeyboardMarkup(rows)


def _kb_back_to_menu(chat_id: str | int) -> list[InlineKeyboardButton]:
    """Common single-button row used at the bottom of every submenu."""
    return [InlineKeyboardButton("⬅️ Назад", callback_data=f"j:menu:{chat_id}")]


def _jokes_intv_kb(chat_id: str | int) -> InlineKeyboardMarkup:
    """⏰ Interval submenu — preset minutes + manual entry + 'off'."""
    cid = str(chat_id)
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton("⛔ Выкл", callback_data=f"j:intv:set:{cid}:0")],
    ]
    # Pack presets 3-per-row for a tidy grid.
    row: list[InlineKeyboardButton] = []
    for n in _JOKES_INTV_PRESETS:
        label = "1440 (сутки)" if n == 1440 else str(n)
        row.append(InlineKeyboardButton(label, callback_data=f"j:intv:set:{cid}:{n}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("✏️ Ввести вручную", callback_data=f"j:intv:edit:{cid}")])
    rows.append(_kb_back_to_menu(cid))
    return InlineKeyboardMarkup(rows)


def _jokes_mode_kb(chat_id: str | int) -> InlineKeyboardMarkup:
    """🌶 Mode submenu — fixed list (matches db.JOKES_VALID_MODES)."""
    cid = str(chat_id)
    items = list(db.JOKES_VALID_MODES)
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for m in items:
        label = _JOKES_MODE_LABELS.get(m, m)
        row.append(InlineKeyboardButton(label, callback_data=f"j:mode:set:{cid}:{m}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append(_kb_back_to_menu(cid))
    return InlineKeyboardMarkup(rows)


def _jokes_ctx_kb(chat_id: str | int) -> InlineKeyboardMarkup:
    """🧠 Context-size submenu — preset N + manual entry."""
    cid = str(chat_id)
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for n in _JOKES_CTX_PRESETS:
        row.append(InlineKeyboardButton(str(n), callback_data=f"j:ctx:set:{cid}:{n}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("✏️ Ввести вручную", callback_data=f"j:ctx:edit:{cid}")])
    rows.append(_kb_back_to_menu(cid))
    return InlineKeyboardMarkup(rows)


def _jokes_min_kb(chat_id: str | int) -> InlineKeyboardMarkup:
    """📊 Min-messages-since-last submenu — preset N + manual entry."""
    cid = str(chat_id)
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for n in _JOKES_MIN_PRESETS:
        row.append(InlineKeyboardButton(str(n), callback_data=f"j:min:set:{cid}:{n}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("✏️ Ввести вручную", callback_data=f"j:min:edit:{cid}")])
    rows.append(_kb_back_to_menu(cid))
    return InlineKeyboardMarkup(rows)


def _jokes_model_text(chat_id: str | int) -> str:
    """Body of the 🤖 Модель submenu (root only)."""
    s = db.get_jokes_settings(chat_id)
    override = (s.get("jokes_model_override") or "").strip()
    chain = _joke_models()
    chain_html = "\n".join(f"   → <code>{html.escape(m)}</code>" for m in chain)
    if override:
        cur_lbl = f"<code>{html.escape(override)}</code> (override)"
    else:
        cur_lbl = "<i>дефолтная цепочка</i>"
    return (
        "🤖 <b>Модель этого чата</b>\n\n"
        f"Сейчас: {cur_lbl}\n\n"
        "Дефолтная цепочка fallback:\n"
        f"{chain_html}\n\n"
        "<i>Override идёт первым; если он недоступен, используется цепочка.</i>"
    )


def _jokes_model_kb(chat_id: str | int) -> InlineKeyboardMarkup:
    """🤖 Модель submenu keyboard."""
    cid = str(chat_id)
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ Ввести свою модель", callback_data=f"j:model:edit:{cid}")],
        [InlineKeyboardButton("↺ Сбросить на дефолт",  callback_data=f"j:model:reset:{cid}")],
        _kb_back_to_menu(cid),
    ])


def _jokes_prompt_text(chat_id: str | int) -> str:
    """Body of the ✏️ Свой промпт submenu.

    Shows the chat's current custom prompt verbatim (or notes that it
    isn't set, so the chat falls back to the active mode preset).
    HTML-escapes the prompt text — admins can paste anything in there.
    """
    s = db.get_jokes_settings(chat_id)
    cur = (s.get("jokes_custom_prompt") or "").strip()
    mode = s.get("jokes_mode") or "normal"
    cap = db.JOKES_MAX_CUSTOM_PROMPT
    head = (
        "✏️ <b>Свой системный промпт</b>\n\n"
        "Текст, который заменит preset режима. Базовые правила "
        "безопасности и формата (без markdown, по-русски, "
        "не повторяться, setup→панчлайн) применяются всегда.\n"
        f"Лимит: <b>{cap}</b> символов. "
        "Температуру по-прежнему задаёт режим — переключай "
        "<b>🌶 Режим</b>, если нужна другая «энергия».\n\n"
    )
    if cur:
        body = (
            f"<b>Сейчас задан</b> ({len(cur)} симв.):\n"
            f"<blockquote>{html.escape(cur)}</blockquote>"
        )
    else:
        body = (
            f"<i>Свой промпт не задан — используется preset режима "
            f"<b>{html.escape(mode)}</b>.</i>"
        )
    return head + body


def _jokes_prompt_kb(chat_id: str | int) -> InlineKeyboardMarkup:
    """✏️ Свой промпт submenu keyboard."""
    cid = str(chat_id)
    s = db.get_jokes_settings(cid)
    has_custom = bool((s.get("jokes_custom_prompt") or "").strip())
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(
            "✏️ Изменить" if has_custom else "✏️ Задать",
            callback_data=f"j:prompt:edit:{cid}",
        )],
    ]
    if has_custom:
        rows.append([InlineKeyboardButton(
            "↺ Сбросить на preset режима",
            callback_data=f"j:prompt:reset:{cid}",
        )])
    rows.append(_kb_back_to_menu(cid))
    return InlineKeyboardMarkup(rows)


def _jokes_history_text(chat_id: str | int, *, limit: int = 5) -> str:
    """Body for the 📜 История submenu — last N posted jokes (HTML)."""
    rows = db.list_jokes_history(chat_id, limit=limit)
    if not rows:
        return "📭 В этом чате ещё не было шуток."
    lines = [f"📜 <b>Последние {len(rows)} шуток</b>"]
    for r in rows:
        ts = (r.get("ts") or "—")[:16]
        mode = r.get("mode") or "—"
        src  = "ручная" if r.get("source") == "manual" else "авто"
        body = (r.get("text") or "").strip()
        if len(body) > 300:
            body = body[:300].rstrip() + "…"
        body = _strip_mentions(html.escape(body))
        score = r.get("score") or 0
        score_lbl = (
            f" · <b>{'+' if int(score) > 0 else ''}{int(score)}</b>"
            if score else ""
        )
        lines.append("")
        lines.append(
            f"<i>{html.escape(str(ts))} · {html.escape(mode)} · "
            f"{src}{score_lbl}</i>"
        )
        lines.append(body)
    return "\n".join(lines)


def _jokes_clear_kb(chat_id: str | int) -> InlineKeyboardMarkup:
    """🗑 Confirmation prompt before wiping the rolling chat_messages log."""
    cid = str(chat_id)
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("❌ Нет",          callback_data=f"j:menu:{cid}"),
            InlineKeyboardButton("✅ Да, удалить", callback_data=f"j:clear:yes:{cid}"),
        ],
    ])


async def _send_or_edit(
    query, *, text: str, reply_markup=None,
) -> None:
    """Edit the panel message in place when possible; fall back to a
    fresh ``send_message`` if Telegram refuses (e.g. the message is
    too old, or already has identical content). Errors are logged
    and swallowed — the panel is best-effort UI.
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
        msg = str(e).lower()
        # "Message is not modified" is benign — same content shown.
        if "not modified" in msg:
            return
        log.debug("jokes menu edit failed (%s); falling back to send", e)
    # Fallback: post a fresh panel.
    try:
        await query.message.chat.send_message(
            text,
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=reply_markup,
        )
    except TelegramError:
        log.warning("jokes menu fallback send also failed", exc_info=True)


async def cmd_jokes_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """``/jokes`` (alias ``/jokes_menu``) — admin: open the inline
    settings panel. Replaces the retired
    ``/jokes_on/off/interval/mode/context/minmsgs/setmodel/clear_log/settings``
    family. Body shown is identical for everyone but mutating
    callbacks check ``is_admin`` per-tap.
    """
    user = update.effective_user
    chat = update.effective_chat
    if user is None or chat is None:
        return
    if not is_admin(user.id):
        await send(update, "❌ Только админ.")
        return
    s = db.get_jokes_settings(str(chat.id))
    body = _jokes_menu_text(str(chat.id))
    kb = _jokes_menu_kb(
        str(chat.id),
        enabled=bool(s.get("jokes_enabled")),
        is_root=is_root_admin(user.id),
    )
    await send(update, body, reply_markup=kb)


async def cb_jokes_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Single dispatcher for the ``j:*`` callback namespace.

    Layout::

        j:menu:<cid>                    main panel (refresh / back)
        j:run:<cid>                     ▶️ generate joke now
        j:toggle:<cid>                  🟢/🔴 enable/disable
        j:hist:<cid>                    📜 history view
        j:intv:<cid>                    ⏰ interval submenu
        j:intv:set:<cid>:<n>            apply interval preset
        j:intv:edit:<cid>               manual interval entry → pending
        j:mode:<cid>                    🌶 mode submenu
        j:mode:set:<cid>:<name>         apply mode
        j:ctx:<cid>                     🧠 context submenu
        j:ctx:set:<cid>:<n>             apply context size preset
        j:ctx:edit:<cid>                manual context entry → pending
        j:min:<cid>                     📊 min-msgs submenu
        j:min:set:<cid>:<n>             apply min-msgs preset
        j:min:edit:<cid>                manual min-msgs entry → pending
        j:model:<cid>                   🤖 model submenu (root)
        j:model:edit:<cid>              manual model entry → pending (root)
        j:model:reset:<cid>             clear override → default chain (root)
        j:prompt:<cid>                  ✏️ custom-prompt submenu
        j:prompt:edit:<cid>             manual custom-prompt entry → pending
        j:prompt:reset:<cid>            clear custom prompt → mode preset
        j:clear:<cid>                   🗑 confirm wipe
        j:clear:yes:<cid>               🗑 wipe confirmed
    """
    query = update.callback_query
    if not query or not query.data:
        return
    try:
        await query.answer()
    except TelegramError:
        pass
    user = update.effective_user
    if user is None:
        return
    parts = query.data.split(":")
    # parts[0] == "j"
    if len(parts) < 2:
        return
    action = parts[1]

    # All callbacks except ``hist`` are admin-only. We read the chat
    # id from the callback_data (parts[2:]) rather than the message
    # context so it survives the panel being forwarded etc.
    def _cid_at(idx: int) -> str | None:
        return parts[idx] if len(parts) > idx else None

    is_admin_user = is_admin(user.id)
    is_root_user  = is_root_admin(user.id)

    async def _show_main(cid: str) -> None:
        s = db.get_jokes_settings(cid)
        await _send_or_edit(
            query,
            text=_jokes_menu_text(cid),
            reply_markup=_jokes_menu_kb(
                cid,
                enabled=bool(s.get("jokes_enabled")),
                is_root=is_root_user,
            ),
        )

    # ── Main panel: j:menu:<cid> ───────────────────────────────────────
    if action == "menu":
        cid = _cid_at(2)
        if not cid:
            return
        await _show_main(cid)
        return

    # ── ▶️ Generate now: j:run:<cid> ───────────────────────────────────
    if action == "run":
        cid = _cid_at(2)
        if not cid:
            return
        if not is_admin_user:
            try:
                await query.answer("Только админ.", show_alert=True)
            except TelegramError:
                pass
            return
        # Refuse fast if the chat hasn't enabled logging yet.
        if not db.is_jokes_enabled(cid):
            try:
                await query.answer(
                    "Сначала нажми «🟢 Включить» — нужны логи сообщений.",
                    show_alert=True,
                )
            except TelegramError:
                pass
            return
        cd = _manual_cooldown_remaining(cid)
        if cd > 0:
            try:
                await query.answer(f"⏳ Подожди ещё {cd} сек — анти-спам.", show_alert=True)
            except TelegramError:
                pass
            return
        rows_count = len(db.recent_chat_messages(cid, limit=db.JOKES_MAX_CONTEXT))
        if rows_count < db.JOKES_MIN_CONTEXT:
            try:
                await query.answer(
                    f"📭 Слишком мало сообщений ({rows_count}/{db.JOKES_MIN_CONTEXT}).",
                    show_alert=True,
                )
            except TelegramError:
                pass
            return
        _manual_cooldown_set(cid)
        # Generate + post in the chat (NOT inside the panel — we want
        # the joke to be a normal chat message everyone can react to).
        chat_obj = query.message.chat if query.message else None
        notice = None
        try:
            if chat_obj is not None:
                notice = await chat_obj.send_message("🤖 Думаю…")
        except TelegramError:
            notice = None
        outcome = await generate_joke_for_chat(cid)
        if outcome.text:
            final_text = outcome.text
            parse_mode = None
        else:
            diag = "\n".join(outcome.attempts[-3:]) if outcome.attempts else ""
            final_text = (
                "😶 Шутка не вышла. Все модели вернули пустоту или ошибку.\n"
                + (f"<code>{html.escape(diag[:300])}</code>" if diag else "")
            )
            parse_mode = "HTML"
        posted = False
        # ``posted_message_id`` tracks where the joke text actually
        # ended up — that's the message reactions/replies will
        # target. ``edit_text`` keeps the same id; the fallback
        # ``send_message`` returns a fresh Message we read for its id.
        posted_message_id: int | None = None
        if notice is not None:
            try:
                await notice.edit_text(
                    final_text,
                    parse_mode=parse_mode,
                    disable_web_page_preview=True,
                )
                posted = True
                posted_message_id = int(notice.message_id) if notice.message_id else None
            except TelegramError:
                posted = False
        if not posted and chat_obj is not None:
            try:
                sent = await chat_obj.send_message(
                    final_text,
                    parse_mode=parse_mode,
                    disable_web_page_preview=True,
                )
                posted = True
                posted_message_id = int(sent.message_id) if sent and sent.message_id else None
            except TelegramError as e:
                log.warning("j:run failed to post in chat %s: %s", cid, e)
        if outcome.text and posted:
            try:
                db.add_joke_history(
                    cid,
                    mode=outcome.mode,
                    model=outcome.model,
                    text=outcome.text,
                    context_size=outcome.context_size,
                    source="manual",
                    message_id=posted_message_id,
                )
                db.mark_chat_joke_sent(cid)
            except Exception:
                log.exception("j:run history bookkeeping failed for chat %s", cid)
        # Refresh the panel so "Last joke" / "messages since" reflect reality.
        await _show_main(cid)
        return

    # ── 🟢/🔴 Toggle: j:toggle:<cid> ───────────────────────────────────
    if action == "toggle":
        cid = _cid_at(2)
        if not cid:
            return
        if not is_admin_user:
            try:
                await query.answer("Только админ.", show_alert=True)
            except TelegramError:
                pass
            return
        cur = db.is_jokes_enabled(cid)
        db.set_jokes_enabled(cid, not cur)
        await _show_main(cid)
        return

    # ── 📜 History: j:hist:<cid>  (public) ─────────────────────────────
    if action == "hist":
        cid = _cid_at(2)
        if not cid:
            return
        await _send_or_edit(
            query,
            text=_jokes_history_text(cid, limit=5),
            reply_markup=InlineKeyboardMarkup([_kb_back_to_menu(cid)]),
        )
        return

    # ── ⏰ Interval ────────────────────────────────────────────────────
    if action == "intv":
        sub = _cid_at(2)
        if sub == "set":
            cid = _cid_at(3)
            n_raw = _cid_at(4)
            if not cid or n_raw is None:
                return
            if not is_admin_user:
                try:
                    await query.answer("Только админ.", show_alert=True)
                except TelegramError:
                    pass
                return
            try:
                n = int(n_raw)
            except ValueError:
                return
            db.set_jokes_interval(cid, n)
            await _show_main(cid)
            return
        if sub == "edit":
            cid = _cid_at(3)
            if not cid or not is_admin_user:
                if not is_admin_user:
                    try:
                        await query.answer("Только админ.", show_alert=True)
                    except TelegramError:
                        pass
                return
            ctx.user_data["pending_jokes_input"] = {"kind": "interval", "chat_id": cid}
            try:
                await query.message.reply_text(
                    "✏️ Введи интервал в минутах одним сообщением.\n"
                    f"Допустимо: <b>0</b> (выкл) или "
                    f"<b>{db.JOKES_MIN_INTERVAL_MIN}..{db.JOKES_MAX_INTERVAL_MIN}</b>.\n"
                    "Отмена: <code>отмена</code>.",
                    parse_mode="HTML",
                )
            except TelegramError:
                pass
            return
        # Plain ``j:intv:<cid>`` → submenu.
        cid = sub
        if not cid:
            return
        cur = int(db.get_jokes_settings(cid).get("jokes_interval_minutes") or 0)
        await _send_or_edit(
            query,
            text=(
                "⏰ <b>Интервал авто-шуток</b>\n\n"
                f"Сейчас: <b>{html.escape(_intv_label(cur))}</b>\n\n"
                f"Допустимо: <b>0</b> (выкл) или "
                f"<b>{db.JOKES_MIN_INTERVAL_MIN}..{db.JOKES_MAX_INTERVAL_MIN}</b> мин."
            ),
            reply_markup=_jokes_intv_kb(cid),
        )
        return

    # ── 🌶 Mode ────────────────────────────────────────────────────────
    if action == "mode":
        sub = _cid_at(2)
        if sub == "set":
            cid = _cid_at(3)
            mode = _cid_at(4)
            if not cid or not mode:
                return
            if not is_admin_user:
                try:
                    await query.answer("Только админ.", show_alert=True)
                except TelegramError:
                    pass
                return
            if mode not in db.JOKES_VALID_MODES:
                try:
                    await query.answer("Неизвестный режим.", show_alert=True)
                except TelegramError:
                    pass
                return
            db.set_jokes_mode(cid, mode)
            await _show_main(cid)
            return
        # Plain ``j:mode:<cid>`` → submenu.
        cid = sub
        if not cid:
            return
        cur = db.get_jokes_settings(cid).get("jokes_mode") or "normal"
        await _send_or_edit(
            query,
            text=(
                "🌶 <b>Упоротость</b>\n\n"
                f"Сейчас: <b>{html.escape(cur)}</b>\n\n"
                "<i>soft → normal → spicy → savage → absurd</i>"
            ),
            reply_markup=_jokes_mode_kb(cid),
        )
        return

    # ── 🧠 Context size ────────────────────────────────────────────────
    if action == "ctx":
        sub = _cid_at(2)
        if sub == "set":
            cid = _cid_at(3)
            n_raw = _cid_at(4)
            if not cid or n_raw is None:
                return
            if not is_admin_user:
                try:
                    await query.answer("Только админ.", show_alert=True)
                except TelegramError:
                    pass
                return
            try:
                n = int(n_raw)
            except ValueError:
                return
            db.set_jokes_context_size(cid, n)
            await _show_main(cid)
            return
        if sub == "edit":
            cid = _cid_at(3)
            if not cid:
                return
            if not is_admin_user:
                try:
                    await query.answer("Только админ.", show_alert=True)
                except TelegramError:
                    pass
                return
            ctx.user_data["pending_jokes_input"] = {"kind": "context", "chat_id": cid}
            try:
                await query.message.reply_text(
                    "✏️ Введи размер контекста (число сообщений) одним сообщением.\n"
                    f"Допустимо: <b>{db.JOKES_MIN_CONTEXT}..{db.JOKES_MAX_CONTEXT}</b>.\n"
                    "Отмена: <code>отмена</code>.",
                    parse_mode="HTML",
                )
            except TelegramError:
                pass
            return
        cid = sub
        if not cid:
            return
        cur = int(db.get_jokes_settings(cid).get("jokes_context_size") or 100)
        await _send_or_edit(
            query,
            text=(
                "🧠 <b>Размер контекста</b>\n\n"
                f"Сейчас: <b>{cur}</b> сообщений\n\n"
                f"Допустимо: <b>{db.JOKES_MIN_CONTEXT}..{db.JOKES_MAX_CONTEXT}</b>."
            ),
            reply_markup=_jokes_ctx_kb(cid),
        )
        return

    # ── 📊 Min messages since last ─────────────────────────────────────
    if action == "min":
        sub = _cid_at(2)
        if sub == "set":
            cid = _cid_at(3)
            n_raw = _cid_at(4)
            if not cid or n_raw is None:
                return
            if not is_admin_user:
                try:
                    await query.answer("Только админ.", show_alert=True)
                except TelegramError:
                    pass
                return
            try:
                n = int(n_raw)
            except ValueError:
                return
            db.set_jokes_min_msgs_since_last(cid, n)
            await _show_main(cid)
            return
        if sub == "edit":
            cid = _cid_at(3)
            if not cid:
                return
            if not is_admin_user:
                try:
                    await query.answer("Только админ.", show_alert=True)
                except TelegramError:
                    pass
                return
            ctx.user_data["pending_jokes_input"] = {"kind": "minmsgs", "chat_id": cid}
            try:
                await query.message.reply_text(
                    "✏️ Введи порог накопления (число) одним сообщением.\n"
                    "Допустимо: <b>0</b> и больше (0 = без порога).\n"
                    "Отмена: <code>отмена</code>.",
                    parse_mode="HTML",
                )
            except TelegramError:
                pass
            return
        cid = sub
        if not cid:
            return
        cur = int(db.get_jokes_settings(cid).get("jokes_min_msgs_since_last") or 20)
        await _send_or_edit(
            query,
            text=(
                "📊 <b>Порог накопления</b>\n\n"
                f"Сейчас: <b>{cur}</b> новых сообщений\n\n"
                "<i>Авто-шутка не пойдёт, пока в чате не накопится N "
                "новых сообщений после прошлой шутки.</i>"
            ),
            reply_markup=_jokes_min_kb(cid),
        )
        return

    # ── 🤖 Model (root only) ───────────────────────────────────────────
    if action == "model":
        sub = _cid_at(2)
        if not is_root_user:
            try:
                await query.answer("Только root-админ.", show_alert=True)
            except TelegramError:
                pass
            return
        if sub == "edit":
            cid = _cid_at(3)
            if not cid:
                return
            ctx.user_data["pending_jokes_input"] = {"kind": "model", "chat_id": cid}
            try:
                await query.message.reply_text(
                    "✏️ Введи имя модели OpenRouter одним сообщением "
                    "(например <code>google/gemma-4-31b-it:free</code>).\n"
                    "Сброс на дефолт: <code>reset</code>.\n"
                    "Отмена: <code>отмена</code>.",
                    parse_mode="HTML",
                )
            except TelegramError:
                pass
            return
        if sub == "reset":
            cid = _cid_at(3)
            if not cid:
                return
            db.set_jokes_model_override(cid, None)
            await _show_main(cid)
            return
        # Plain ``j:model:<cid>`` → submenu.
        cid = sub
        if not cid:
            return
        await _send_or_edit(
            query,
            text=_jokes_model_text(cid),
            reply_markup=_jokes_model_kb(cid),
        )
        return

    # ── ✏️ Custom prompt ───────────────────────────────────────────────
    if action == "prompt":
        sub = _cid_at(2)
        if sub == "edit":
            cid = _cid_at(3)
            if not cid:
                return
            if not is_admin_user:
                try:
                    await query.answer("Только админ.", show_alert=True)
                except TelegramError:
                    pass
                return
            ctx.user_data["pending_jokes_input"] = {"kind": "prompt", "chat_id": cid}
            try:
                await query.message.reply_text(
                    "✏️ Отправь следующим сообщением свой системный промпт "
                    "(он заменит preset режима).\n\n"
                    f"Лимит: <b>{db.JOKES_MAX_CUSTOM_PROMPT}</b> символов. "
                    "Базовые правила безопасности/формата всё равно "
                    "применяются.\n"
                    "Сброс на preset: <code>reset</code>.\n"
                    "Отмена: <code>отмена</code>.",
                    parse_mode="HTML",
                )
            except TelegramError:
                pass
            return
        if sub == "reset":
            cid = _cid_at(3)
            if not cid:
                return
            if not is_admin_user:
                try:
                    await query.answer("Только админ.", show_alert=True)
                except TelegramError:
                    pass
                return
            db.set_jokes_custom_prompt(cid, None)
            await _send_or_edit(
                query,
                text=_jokes_prompt_text(cid),
                reply_markup=_jokes_prompt_kb(cid),
            )
            return
        # Plain ``j:prompt:<cid>`` → submenu.
        cid = sub
        if not cid:
            return
        await _send_or_edit(
            query,
            text=_jokes_prompt_text(cid),
            reply_markup=_jokes_prompt_kb(cid),
        )
        return

    # ── 🗑 Clear log ───────────────────────────────────────────────────
    if action == "clear":
        sub = _cid_at(2)
        if sub == "yes":
            cid = _cid_at(3)
            if not cid:
                return
            if not is_admin_user:
                try:
                    await query.answer("Только админ.", show_alert=True)
                except TelegramError:
                    pass
                return
            n = db.clear_chat_messages_log(cid)
            try:
                await query.answer(f"🗑 Удалено: {n}.")
            except TelegramError:
                pass
            await _show_main(cid)
            return
        # Plain ``j:clear:<cid>`` → confirm prompt.
        cid = sub
        if not cid:
            return
        if not is_admin_user:
            try:
                await query.answer("Только админ.", show_alert=True)
            except TelegramError:
                pass
            return
        log_total = len(db.recent_chat_messages(cid, limit=db.JOKES_MAX_CONTEXT))
        await _send_or_edit(
            query,
            text=(
                "🗑 <b>Удалить весь лог сообщений?</b>\n\n"
                f"В чате накоплено: <b>{log_total}</b>.\n"
                "Это <i>не</i> удалит историю шуток и не сбросит настройки."
            ),
            reply_markup=_jokes_clear_kb(cid),
        )
        return

    # Unknown action — silently ignore (defensive: future renames).
    log.debug("cb_jokes_menu: unknown action %r in %r", action, query.data)


async def handle_pending_jokes_text(
    update: Update, ctx: ContextTypes.DEFAULT_TYPE,
) -> bool:
    """Consume the next non-command text message after a user tapped
    "✏️ Ввести вручную" in any of the submenus. Returns ``True`` when
    the message was consumed (so the master text router knows to stop).

    Pending state shape::

        ctx.user_data["pending_jokes_input"] = {
            "kind": "interval" | "context" | "minmsgs" | "model" | "prompt",
            "chat_id": "<cid>",
        }
    """
    pending = ctx.user_data.get("pending_jokes_input")
    if not pending or not isinstance(pending, dict):
        return False
    msg = update.effective_message
    user = update.effective_user
    if msg is None or user is None:
        return False
    txt = (msg.text or "").strip()
    if not txt:
        return False

    kind = pending.get("kind")
    cid = str(pending.get("chat_id") or "")
    if not kind or not cid:
        ctx.user_data.pop("pending_jokes_input", None)
        return False

    # Cancel keyword.
    if txt.lower() in ("отмена", "cancel", "отменить"):
        ctx.user_data.pop("pending_jokes_input", None)
        await send(update, "❌ Отменено.")
        return True

    # All four kinds require admin (and model — root). Re-check
    # because the pending state could outlive a permission change.
    if kind == "model":
        if not is_root_admin(user.id):
            ctx.user_data.pop("pending_jokes_input", None)
            await send(update, "❌ Только root-админ.")
            return True
    else:
        if not is_admin(user.id):
            ctx.user_data.pop("pending_jokes_input", None)
            await send(update, "❌ Только админ.")
            return True

    ctx.user_data.pop("pending_jokes_input", None)

    if kind == "interval":
        if not txt.lstrip("-").isdigit():
            await send(update, "❌ Нужно целое число минут (или <code>отмена</code>).")
            return True
        try:
            n = int(txt)
        except ValueError:
            await send(update, "❌ Некорректное число.")
            return True
        db.set_jokes_interval(cid, n)
        new = int(db.get_jokes_settings(cid).get("jokes_interval_minutes") or 0)
        await send(update, f"✅ Интервал: <b>{html.escape(_intv_label(new))}</b>.")
        return True

    if kind == "context":
        if not txt.lstrip("-").isdigit():
            await send(update, "❌ Нужно целое число (или <code>отмена</code>).")
            return True
        try:
            n = int(txt)
        except ValueError:
            await send(update, "❌ Некорректное число.")
            return True
        db.set_jokes_context_size(cid, n)
        new = int(db.get_jokes_settings(cid).get("jokes_context_size") or 100)
        await send(update, f"✅ Контекст: <b>{new}</b> сообщений.")
        return True

    if kind == "minmsgs":
        if not txt.lstrip("-").isdigit():
            await send(update, "❌ Нужно целое число (или <code>отмена</code>).")
            return True
        try:
            n = int(txt)
        except ValueError:
            await send(update, "❌ Некорректное число.")
            return True
        db.set_jokes_min_msgs_since_last(cid, n)
        new = int(db.get_jokes_settings(cid).get("jokes_min_msgs_since_last") or 20)
        await send(update, f"✅ Порог накопления: <b>{new}</b>.")
        return True

    if kind == "model":
        if txt.lower() in ("reset", "-", "default", "none", "off"):
            db.set_jokes_model_override(cid, None)
            await send(update, "✅ Сброшено на дефолтную цепочку.")
            return True
        # Light validation — OpenRouter ids are usually ``vendor/name[:tag]``.
        if " " in txt or len(txt) > 200:
            await send(update, "❌ Похоже на не-модель (есть пробелы или слишком длинное).")
            return True
        db.set_jokes_model_override(cid, txt)
        await send(
            update,
            f"✅ Модель этого чата: <code>{html.escape(txt)}</code>.\n"
            "Дефолтная цепочка идёт после неё как fallback.",
        )
        return True

    if kind == "prompt":
        # Reset keywords clear the override and fall back to mode preset.
        if txt.lower() in ("reset", "default", "none", "off", "-"):
            db.set_jokes_custom_prompt(cid, None)
            mode = db.get_jokes_settings(cid).get("jokes_mode") or "normal"
            await send(
                update,
                f"✅ Свой промпт сброшен — используется preset режима "
                f"<b>{html.escape(mode)}</b>.",
            )
            return True
        cap = db.JOKES_MAX_CUSTOM_PROMPT
        if len(txt) > cap:
            await send(
                update,
                f"❌ Слишком длинно: <b>{len(txt)}</b> симв., лимит "
                f"<b>{cap}</b>. Сократи и пришли ещё раз "
                "(меню <code>/jokes</code> → ✏️ Свой промпт).",
            )
            return True
        if len(txt) < 5:
            await send(
                update,
                "❌ Слишком коротко — нужно минимум 5 символов "
                "(или <code>отмена</code> / <code>reset</code>).",
            )
            return True
        db.set_jokes_custom_prompt(cid, txt)
        new_len = len((db.get_jokes_settings(cid).get("jokes_custom_prompt") or ""))
        await send(
            update,
            f"✅ Свой промпт сохранён ({new_len} симв.). "
            "Базовые правила безопасности/формата применяются всегда.",
        )
        return True

    # Unknown kind — drop silently.
    return True


async def cmd_joke(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """``/joke [тема]`` — запросить шутку прямо сейчас.

    Поведение:
      * Админ: без дневной квоты, только 60-сек анти-спам на чат.
      * Не-админ: дневная квота 5/чат/сутки + те же 60 сек анти-спам.
        Лимит общий на всех участников чата (это "5 шуток в день
        на чат", а не "на пользователя"); сбрасывается в 00:00 UTC.

    Если в ``ctx.args`` есть текст — он трактуется как тема:
    ``/joke про погоду``, ``/joke черемша``, ``/joke на тему инфляции``.
    Без аргументов — обычная шутка по контексту чата (старое поведение).
    """
    user = update.effective_user
    chat = update.effective_chat
    if user is None or chat is None:
        return
    topic = parse_topic_from_args(list(ctx.args or []))
    await trigger_joke_request(
        update, ctx,
        topic=topic,
        source="slash",
    )


async def trigger_joke_request(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
    *,
    topic: str = "",
    source: str = "slash",
) -> None:
    """Central entry-point for all user-driven joke requests.

    Used by:
      * :func:`cmd_joke` — slash-command entry, ``source='slash'``.
      * The free-form text trigger in ``bot.py``'s ``handle_text``
        when :func:`detect_joke_intent` matches a chat message,
        ``source='freeform'``.

    Performs (in this order):
      1. Privacy gate: refuse if ``jokes_enabled=false`` for this chat.
         Slash-source replies loudly with the ``/jokes_on`` hint;
         freeform-source stays silent (we don't want the bot
         volunteering itself in chats that haven't opted in).
      2. Per-chat-per-day quota for non-admins (``5/chat/day`` shared
         across all participants). Admins skip this entirely.
      3. Anti-spam 60-sec per-chat in-memory cooldown (admins included
         — protects the OpenRouter quota).
      4. Empty-buffer guard: with no topic AND no recent messages,
         there's nothing to joke about; with a topic the buffer
         emptiness is allowed.
      5. Generate via :func:`generate_joke_for_chat`, post the
         result, write to ``jokes_history``, mark ``last_joke_at``.

    The quota counter is consumed BEFORE the OpenRouter call (so an
    OpenRouter failure still costs a daily charge). That's a
    deliberate trade — without the up-front charge a non-admin could
    burn the quota with no effective rate-limit during an outage,
    and the OpenRouter quota itself would be the only backstop.
    Admins bypass the daily charge entirely (we trust them, and the
    60-sec cooldown is enough).
    """
    user = update.effective_user
    chat = update.effective_chat
    if user is None or chat is None:
        return

    cid = str(chat.id)
    is_freeform = source == "freeform"

    # 1. Privacy gate.
    if not db.is_jokes_enabled(cid):
        if is_freeform:
            # Don't volunteer the bot's existence in a chat that
            # hasn't opted in. Silent no-op is the right call.
            return
        await send(
            update,
            "❌ В этом чате авто-шутки выключены.\n"
            "Открой меню <code>/jokes</code> и нажми "
            "«🟢 Включить».",
        )
        return

    # 2. Per-chat-per-day quota for non-admins.
    user_is_admin = is_admin(user.id)
    if not user_is_admin:
        allowed, count, limit = db.bump_jokes_user_daily(cid)
        if not allowed:
            if is_freeform:
                # Soft refusal — quote the limit but don't shout. The
                # quota is shared, so it's not the asker's fault.
                await send(
                    update,
                    f"🃏 Лимит шуток в этом чате на сегодня исчерпан "
                    f"(<b>{count}/{limit}</b>). Сбросится в 00:00 UTC "
                    "(03:00 МСК). Админ может запустить шутку без "
                    "лимита.",
                )
            else:
                await send(
                    update,
                    f"🃏 Лимит шуток в этом чате на сегодня исчерпан: "
                    f"<b>{count}/{limit}</b>.\n"
                    "Сбросится в 00:00 UTC (03:00 МСК). Админ может "
                    "запустить шутку без лимита через <code>/joke</code>.",
                )
            return

    # 3. Anti-spam cooldown — applies to admins too (it's protecting
    # the OpenRouter quota, not the user). On a freeform trigger we
    # softly explain instead of loudly erroring.
    cd = _manual_cooldown_remaining(cid)
    if cd > 0:
        if is_freeform:
            await send(
                update,
                f"⏳ Анти-спам: подожди ещё <b>{cd}</b> сек.",
            )
        else:
            await send(update, f"⏳ Подожди ещё <b>{cd}</b> сек — анти-спам.")
        return

    # 4. Buffer guard. With a topic we tolerate an empty buffer
    # (generate_joke_for_chat will fall back to topic-only). Without
    # a topic, the LLM has nothing to be funny about.
    topic_clean = _normalize_topic(topic)
    if not topic_clean:
        rows_count = len(db.recent_chat_messages(
            cid, limit=db.JOKES_MAX_CONTEXT,
        ))
        if rows_count < db.JOKES_MIN_CONTEXT:
            await send(
                update,
                f"📭 Слишком мало сообщений в логе ({rows_count}/"
                f"{db.JOKES_MIN_CONTEXT}). Нужно ещё немного "
                "пообщаться, либо запроси шутку с темой: "
                "<code>/joke про &lt;тема&gt;</code>.",
            )
            return

    _manual_cooldown_set(cid)

    # 5. Generate & post.
    notice = None
    try:
        progress = "🤖 Думаю…"
        if topic_clean:
            progress = f"🤖 Думаю про «{html.escape(topic_clean)}»…"
        notice = await ctx.bot.send_message(chat_id=chat.id, text=progress)
    except TelegramError:
        notice = None

    outcome = await generate_joke_for_chat(cid, topic=topic_clean or None)

    if outcome.text:
        final_text = outcome.text
    else:
        diag = "\n".join(outcome.attempts[-3:]) if outcome.attempts else ""
        final_text = (
            "😶 Шутка не вышла. Все модели вернули пустоту или ошибку.\n"
            + (f"<code>{html.escape(diag[:300])}</code>" if diag else "")
        )

    posted = False
    posted_message_id: int | None = None
    if notice is not None:
        try:
            await notice.edit_text(
                final_text,
                parse_mode="HTML" if not outcome.text else None,
                disable_web_page_preview=True,
            )
            posted = True
            posted_message_id = (
                int(notice.message_id) if notice.message_id else None
            )
        except TelegramError:
            posted = False
    if not posted:
        try:
            sent = await ctx.bot.send_message(
                chat_id=chat.id,
                text=final_text,
                parse_mode="HTML" if not outcome.text else None,
                disable_web_page_preview=True,
            )
            posted = True
            posted_message_id = (
                int(sent.message_id) if sent and sent.message_id else None
            )
        except TelegramError as e:
            log.warning("/joke failed to post in chat %s: %s", chat.id, e)
            return

    if outcome.text and posted:
        try:
            db.add_joke_history(
                cid,
                mode=outcome.mode,
                model=outcome.model,
                text=outcome.text,
                context_size=outcome.context_size,
                source=("manual_topic" if topic_clean else "manual"),
                message_id=posted_message_id,
            )
            db.mark_chat_joke_sent(cid)
        except Exception:
            log.exception("/joke history bookkeeping failed for chat %s", chat.id)


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
        score = r.get("score") or 0
        score_lbl = (
            f" · <b>{'+' if int(score) > 0 else ''}{int(score)}</b>"
            if score else ""
        )
        lines.append("")
        lines.append(
            f"<i>{html.escape(str(ts))} · {html.escape(mode)} · "
            f"{html.escape(model)} · {src}{score_lbl}</i>"
        )
        lines.append(body)
    await send(update, "\n".join(lines))


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
        sent_msg: Message | None = None
        try:
            sent_msg = await ctx.bot.send_message(
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
                message_id=(
                    int(sent_msg.message_id)
                    if sent_msg and sent_msg.message_id else None
                ),
            )
            db.mark_chat_joke_sent(chat_id)
        except Exception:
            log.exception("job_jokes: bookkeeping failed for %s", chat_id)


# ─────────────────────────────────────────────────────────────────────────────
# Reaction feedback (MessageReactionHandler)
# ─────────────────────────────────────────────────────────────────────────────
#
# Telegram delivers two flavours of reaction event:
#
#   * ``update.message_reaction``       — per-user, requires the bot to
#                                         be a chat administrator. We
#                                         get the user's old + new
#                                         reaction lists.
#   * ``update.message_reaction_count`` — chat-wide aggregate, delivered
#                                         when per-user delivery isn't
#                                         enabled (anonymous chats /
#                                         non-admin bot). We get the
#                                         total counts per emoji.
#
# Both arrive on the *same* ``MessageReactionHandler`` callback when the
# default ``message_reaction_types`` is left at ``MESSAGE_REACTION``
# (which subscribes to both subtypes). We dispatch by checking which
# field of ``update`` is set.
#
# The bot must also list these update types in ``allowed_updates`` —
# see ``run_polling`` in :mod:`bot`. Without that, Telegram silently
# drops the events and this handler never fires.

async def on_message_reaction(
    update: Update, ctx: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Update the score / snapshot for a tracked joke when somebody
    reacts to it. Routes by update sub-type:

    * Per-user (``update.message_reaction``): apply a delta of
      ``score(new) - score(old)`` and merge per-user reaction lists.
    * Chat-wide (``update.message_reaction_count``): overwrite the
      snapshot with the authoritative aggregate counts.

    Untracked messages (anything not in ``jokes_history``) are
    ignored — reactions on quote-loop posts, AI-summary posts,
    bot-command replies etc. don't need to feed back into jokes.
    """
    try:
        # ── per-user reaction event ───────────────────────────────────
        mr = getattr(update, "message_reaction", None)
        if mr is not None:
            chat = getattr(mr, "chat", None)
            if chat is None or not getattr(mr, "message_id", None):
                return
            joke = db.get_joke_by_message(str(chat.id), int(mr.message_id))
            if not joke or not joke.get("id"):
                return
            old_score, _ = _reactions_to_score(getattr(mr, "old_reaction", None) or [])
            new_score, new_snap = _reactions_to_score(getattr(mr, "new_reaction", None) or [])
            delta = new_score - old_score
            # We don't have an authoritative full snapshot from a
            # per-user event (would need to track every user). Pass
            # ``snapshot_json=None`` so the existing snapshot —
            # populated by message_reaction_count if available — is
            # preserved.
            if delta != 0:
                try:
                    new_total = db.apply_joke_reaction_delta(
                        int(joke["id"]), int(delta),
                    )
                    log.info(
                        "joke %s reaction delta=%+d → score=%d (per-user, "
                        "user new=%s)",
                        joke["id"], delta, new_total, new_snap or "{}",
                    )
                except Exception:
                    log.exception(
                        "apply_joke_reaction_delta failed for joke %s",
                        joke.get("id"),
                    )
            return

        # ── chat-wide aggregate event ─────────────────────────────────
        mrc = getattr(update, "message_reaction_count", None)
        if mrc is not None:
            chat = getattr(mrc, "chat", None)
            if chat is None or not getattr(mrc, "message_id", None):
                return
            joke = db.get_joke_by_message(str(chat.id), int(mrc.message_id))
            if not joke or not joke.get("id"):
                return
            score, snap = _reaction_counts_to_score(
                getattr(mrc, "reactions", None) or []
            )
            try:
                db.set_joke_reaction_snapshot(
                    int(joke["id"]),
                    score=score,
                    snapshot_json=json.dumps(snap, ensure_ascii=False) if snap else None,
                )
                log.info(
                    "joke %s reactions snapshot score=%d emojis=%s",
                    joke["id"], score, snap or "{}",
                )
            except Exception:
                log.exception(
                    "set_joke_reaction_snapshot failed for joke %s",
                    joke.get("id"),
                )
            return
    except Exception:
        log.debug("on_message_reaction failed", exc_info=True)


__all__ = [
    # constants
    "_DEFAULT_JOKE_MODELS",
    # public callables
    "log_chat_message",
    "generate_joke_for_chat",
    "job_jokes",
    "cmd_joke",
    "trigger_joke_request",
    "detect_joke_intent",
    "parse_topic_from_args",
    "cmd_jokes_menu",
    "cb_jokes_menu",
    "handle_pending_jokes_text",
    "cmd_jokes_history",
    "on_message_reaction",
    # for tests / smoke
    "JokeOutcome",
]
