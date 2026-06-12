"""Shared OpenRouter output-safety helpers.

Some free-tier reasoning-tuned models (gpt-oss-style, deepseek-r1-style,
nemotron-think) leak their chain-of-thought into ``message.content``
instead of (or in addition to) the dedicated ``message.reasoning``
field. If we just trim and post that, the user gets the model's inner
monologue ("We need to produce a short joke in Russian, 1-3 sentences,
max 350 characters, no markdown…") instead of the actual answer.

This module centralises the two-layer defense used by every
OpenRouter-backed feature in the bot (so far: ``handlers.ai_analysis``
and ``handlers.jokes``):

  1. :func:`strip_reasoning_blocks` — removes ``<think>…</think>`` /
     ``<reasoning>…</reasoning>`` wrappers. If a stray opener appears
     with no closer (the model ran out of tokens mid-CoT), drop
     everything from the opener onward — it's all monologue, no answer
     follows.

  2. :func:`looks_like_meta_leak` — flags the cleaned content as a
     CoT/meta-instruction leak when:
       * it opens with a known thinking zachin
         (:data:`META_LEAK_PREFIXES`);
       * it contains a known prompt-rule regurgitation substring (a
         small universal baseline + feature-specific extras);
       * its head is heavily Latin while the system prompt mandates
         Russian (parameterised threshold + ratio).

Each feature module wires both into its OpenRouter validator so that
a leaking model triggers a retry on the next model in the fallback
chain rather than returning bogus output to the user.

Why this is worth its own module:
  * The exact same bug class hit ``/analyze`` (PR #40) and then
    ``/joke`` separately. The third feature that calls OpenRouter
    will benefit by importing this instead of re-implementing it.
  * The detection is intentionally conservative — false positives
    would silently hide legitimate answers. Keeping the heuristics
    in one place means there is one place to tune them.
"""

from __future__ import annotations

import re

# ─── reasoning-block stripping ────────────────────────────────────────────────

_THINK_BLOCK_RE = re.compile(
    r"<\s*think(?:ing)?\s*>.*?<\s*/\s*think(?:ing)?\s*>",
    flags=re.IGNORECASE | re.DOTALL,
)
_REASONING_BLOCK_RE = re.compile(
    r"<\s*reasoning\s*>.*?<\s*/\s*reasoning\s*>",
    flags=re.IGNORECASE | re.DOTALL,
)
_OPEN_THINK_RE = re.compile(
    r"<\s*think(?:ing)?\s*>", flags=re.IGNORECASE,
)


def strip_reasoning_blocks(s: str | None) -> str:
    """Remove ``<think>…</think>`` / ``<reasoning>…</reasoning>``
    wrappers from a raw model response.

    If a stray ``<think>`` opener appears with no closer (the model
    hit ``max_tokens`` mid-CoT), drop everything from the opener
    onward — it's all monologue, the answer never started.
    """
    if not s:
        return s or ""
    s = _THINK_BLOCK_RE.sub("", s)
    s = _REASONING_BLOCK_RE.sub("", s)
    m = _OPEN_THINK_RE.search(s)
    if m:
        s = s[: m.start()]
    return s.strip()


# ─── meta-leak heuristic ──────────────────────────────────────────────────────
#
# Phrases that, when they OPEN the response, mean the model is dumping
# its plan / restating the task instead of producing the answer.
# Matched lower-cased against the leading whitespace-stripped text.

META_LEAK_PREFIXES: tuple[str, ...] = (
    # English CoT openings (the common case for free-tier reasoning
    # models that leak their inner monologue).
    "we need to",
    "we should",
    "we have to",
    "we'll",
    "we will",
    "we want",
    "let me",
    "let's",
    "let us",
    "i need to",
    "i'll",
    "i will",
    "i'm going to",
    "i should",
    "i must",
    "i have to",
    "okay,",
    "ok,",
    "alright,",
    "sure,",
    "first,",
    "now,",
    "the user",
    "the task",
    "the assistant",
    "the chat",
    "the goal is",
    "the system prompt",
    "looking at",
    "based on the",
    "according to the",
    "identify ",
    "focus on",
    "must be ",
    "must produce",
    "produce a ",
    "task:",
    "instruction:",
    "instructions:",
    "goal:",
    # Russian CoT openings (rarer but seen on bilingual reasoning models).
    "итак, мне нужно",
    "так, мне нужно",
    "хорошо, мне нужно",
    "понятно, нужно сделать",
    "сейчас сделаю",
    "сейчас составлю",
    "сейчас проанализирую",
    "проанализирую и составлю",
    "сначала разберём",
    "сначала разберем",
    "разберём задачу",
    "разберем задачу",
)


# Universal substrings that, anywhere in the answer, strongly hint
# the model is regurgitating the prompt rules rather than answering.
# Lower-cased match. Keep this list short and high-signal; feature-
# specific giveaways belong in the caller's ``extra_substrings``.
META_LEAK_SUBSTRINGS_UNIVERSAL: tuple[str, ...] = (
    "no markdown",
    "no preamble",
    "no emojis",
    "no quoting",
    "no mention of rules",
    "without preamble",
    "system prompt",
    "must be russian",
    "in russian",
)


def looks_like_meta_leak(
    text: str | None,
    *,
    extra_substrings: tuple[str, ...] | list[str] = (),
    latin_threshold: int = 30,
    latin_ratio: float = 2.0,
) -> bool:
    """``True`` if ``text`` reads as the model's chain-of-thought or
    a restatement of the prompt rules rather than the actual answer.

    Heuristics (any one fires → return True):

      * The text opens with a known meta-thinking prefix
        (:data:`META_LEAK_PREFIXES`, case-insensitive).
      * The text contains one of the universal prompt-rule
        regurgitation substrings (:data:`META_LEAK_SUBSTRINGS_UNIVERSAL`)
        OR one of the feature-specific ``extra_substrings``.
      * The first 200 chars are heavily Latin (``latin_threshold``
        Latin letters AND Latin > Cyrillic × ``latin_ratio``) — our
        prompts mandate Russian, so a Latin-dominated head means
        the model either answered in English or leaked CoT in
        English.

    Tuning knobs:
      * ``latin_threshold`` — minimum Latin-letter count in the first
        200 chars before the ratio check kicks in. Default ``30``.
      * ``latin_ratio`` — how many times more Latin than Cyrillic
        triggers a rejection. Default ``2``. Raise it (3, 4) for
        features whose legitimate output may include lots of foreign
        names/quotes (e.g. jokes that name-drop English-speaking
        characters or quote movie lines).

    The detector is intentionally conservative: a Russian summary
    that legitimately contains Latin player nicknames (Phoenileo,
    Oliver Queen, Fragment) sits well below the default 2× threshold.
    """
    if not text:
        return False
    head = text.strip().lower()
    if not head:
        return False
    for prefix in META_LEAK_PREFIXES:
        if head.startswith(prefix):
            return True
    needles = list(META_LEAK_SUBSTRINGS_UNIVERSAL) + [
        s.lower() for s in (extra_substrings or ())
    ]
    for needle in needles:
        if needle and needle in head:
            return True
    sample = text.strip()[:200]
    latin = sum(1 for c in sample if "a" <= c.lower() <= "z")
    cyr = sum(1 for c in sample if "\u0400" <= c <= "\u04ff")
    if latin >= latin_threshold and latin > cyr * float(latin_ratio):
        return True
    return False


__all__ = [
    "strip_reasoning_blocks",
    "looks_like_meta_leak",
    "META_LEAK_PREFIXES",
    "META_LEAK_SUBSTRINGS_UNIVERSAL",
]
