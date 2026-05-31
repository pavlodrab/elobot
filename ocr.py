"""
Match-screenshot OCR for the FC Mobile League bot.

Goal: given a screenshot of a match-result screen, extract:
  • final score (e.g. "1:1")
  • home / away team or player names (e.g. "GVL·LOKOMOTIV", "AuraBroAura88888")
  • the league plate ("Лига Гвардиольыча", "VSA", "RI", ...)
    used to classify the match into a tournament type ("vsa" or "ri").

The screen layout we expect (typical FC Mobile post-match recap):

 ┌──────────────────────────────────────────────────────────────────┐
 │ ▣ TEAM_1   ←score→   1   -   1   ←score→   TEAM_2  ▣            │
 │   league_plate                  90:00                            │
 ├──────────────────────────────────────────────────────────────────┤
 │ STATS panel                            GOAL EVENTS               │
 └──────────────────────────────────────────────────────────────────┘

We crop fixed regions defined as ratios of width/height so the same logic
works on different phone resolutions. Each region is OCR'd at multiple
binarisation thresholds and PSMs and we keep the best-looking result.

This module deliberately has **no telegram dependency** so it can be unit-
tested in isolation. The bot calls `parse_match_screenshot(path)` and gets a
structured dict back.
"""
from __future__ import annotations

import io
import logging
import os
import re
from dataclasses import dataclass, field, asdict
from typing import Optional

import base64
import json
import time
import urllib.error
import urllib.request

from PIL import Image, ImageOps

try:
    import pytesseract
    _OCR_AVAILABLE = True
except ImportError:                        # pragma: no cover
    pytesseract = None                     # type: ignore
    _OCR_AVAILABLE = False


# ── AI Vision OCR (Gemini + OpenRouter fallback) ─────────────────────────────
# Inlined here (instead of a separate ai_ocr.py module) to avoid any chance
# of the bot ending up on a host where the auxiliary module isn't on
# sys.path. Everything is pure stdlib; no extra dependencies.

# Google Gemini (primary — 1500 req/day free, no rate-limit 429s)
_GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
_GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
_GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models"

# OpenRouter (fallback) — keys rotated on 429.
# Primary key: env OPENROUTER_API_KEY (with hardcoded fallback below).
# Slots 2 and 3 also have hardcoded fallbacks for backward compat.
# Additional keys (4+): set env vars OPENROUTER_API_KEY_4, _5, ..., up to
# _MAX_OPENROUTER_KEYS — no code changes required to plug in more keys.
_OPENROUTER_DEFAULT_KEY = "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"
_OPENROUTER_API_KEY_2 = os.getenv("OPENROUTER_API_KEY_2", "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").strip()
_OPENROUTER_API_KEY_3 = os.getenv("OPENROUTER_API_KEY_3", "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").strip()
_OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
# Upper bound for how many additional env-var-only keys we scan for.
# Bump this if you ever need more than 20 keys (very unlikely).
_MAX_OPENROUTER_KEYS = 20

# Groq (fast inference, free tier — llama-4-scout has vision)
_GROQ_API_KEY = os.getenv("GROQ_API_KEY", "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").strip()
_GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
_GROQ_MODEL = "meta-llama/llama-4-scout-17b-16e-instruct"
# Groq fallback models (vision-capable, free tier)
_GROQ_FALLBACK_MODELS = [
    "meta-llama/llama-4-scout-17b-16e-instruct",
    "meta-llama/llama-4-maverick-17b-128e-instruct",
]

AI_DEFAULT_MODEL = os.getenv(
    "OPENROUTER_MODEL", "nvidia/nemotron-nano-12b-v2-vl:free"
)
# Free vision-LLMs that can read game screenshots and return JSON.
# Order = retry order: try primary first, then fall through.
# Each panel comes with a 🔄 «Другой моделью» button that explicitly
# rotates through this list, skipping models the user has already
# seen for this screenshot.
#
# Live-tested order (2026-05-07, run #2 — after Baidu was caught
# putting the small-font league line into team1/team2):
#   - google/gemma-4-31b-it:free           — primary (~8s), follows
#                                            instructions, distinguishes
#                                            nickname vs league reliably.
#   - google/gemma-4-26b-a4b-it:free       — sibling (~8s), almost as good,
#                                            sometimes 429.
#   - baidu/qianfan-ocr-fast:free          — fastest (~4s) but pure-OCR,
#                                            doesn't understand context;
#                                            can dump league into team
#                                            fields. Kept as 3rd retry.
#   - nvidia/nemotron-nano-12b-v2-vl:free  — slow (8-45s), reasoning model,
#                                            sometimes emits prose.
#
# Models removed from the free tier (HTTP 404) over time:
#   google/gemma-3-*-it:free (early 2026), google/gemini-2.0-flash-exp:free,
#   qwen/qwen-2.5-vl-72b-instruct:free,
#   meta-llama/llama-3.2-90b-vision-instruct:free (May 2026).
# nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free — re-added as last
#   fallback (May 2026). Sometimes emits prose, but _ai_extract_json_block
#   can usually salvage the JSON from the reasoning trace.
# moonshotai/kimi-k2.6:free — added May 2026, large 1T MoE with vision,
#   262K context. Useful as a fresh fallback when NVIDIA/Gemma chains
#   are saturated.
# Removed (404 / discontinued):
#   baidu/qianfan-ocr-fast:free (May 2026).
# Note: non-VL models (text-only) are NOT suitable for screenshot OCR —
#   they can't accept images. Only include models with vision capability.
AI_FALLBACK_MODELS = (
    "nvidia/nemotron-nano-12b-v2-vl:free",
    "google/gemma-4-31b-it:free",
    "google/gemma-4-26b-a4b-it:free",
    "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free",
    "moonshotai/kimi-k2.6:free",
)
AI_COMPARE_MODELS = (
    "nvidia/nemotron-nano-12b-v2-vl:free",
    "google/gemma-4-31b-it:free",
    "google/gemma-4-26b-a4b-it:free",
    "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free",
    "moonshotai/kimi-k2.6:free",
)

_AI_PROMPT = """You are reading a football game end-screen.
Return ONLY a JSON object, no prose, no markdown fences, no commentary.

Schema:
{
  "score1": int,                    // home goals (left big digit, ignore 90:00 timer)
  "score2": int,                    // away goals (right big digit)
  "pen1":   int|null,               // home penalty-shootout score (see PENALTIES below)
  "pen2":   int|null,               // away penalty-shootout score (see PENALTIES below)
  "team1":  str,                    // LEFT player nickname (see rules below)
  "team2":  str,                    // RIGHT player nickname (see rules below)
  "league": str|null,               // small-font league/affiliation line
  "goals":  [                       // ordered list of every goal scorer
    {
      "name":   str,                // scorer's last name ONLY (see below)
      "minute": int|null,           // minute of the goal (1..120) if visible
      "side":   "home"|"away"|null  // see colour rule below
    },
    ...
  ]
}

PENALTIES (very important — most matches do NOT have penalties):

When a knockout-stage match goes to a penalty shootout, FC Mobile shows
TWO extra numbers in parentheses next to the regular score, on the
SAME row as the big digits. Layout:

      (P1)   S1   -   S2   (P2)
            └big digits┘
       └pen ┘           └pen┘

Examples of what a penalty result looks like on screen:
  "(3) 3 - 3 (1)"   →  score1=3, score2=3, pen1=3, pen2=1
                       (regulation+ET 3:3, home wins on pens 3:1)
  "(2) 1 - 1 (4)"   →  score1=1, score2=1, pen1=2, pen2=4
                       (away wins on pens 4:2)

Rules for pen1 / pen2:
- Output integers ONLY when BOTH parenthesised numbers are clearly
  visible on the screen, sit on the same row as the big score, and the
  regulation score is a draw (score1 == score2). Otherwise BOTH must be null.
- The LEFT parenthesised number is pen1 (home), the RIGHT is pen2 (away).
- Match-clock readings ("90:00", "120:00") are NOT penalty scores —
  ignore the timer.
- If you cannot tell whether the small numbers in parens are penalty
  results, set pen1 and pen2 to null. Do not guess.
- For a normal match without a shootout, set pen1=null and pen2=null.

CRITICAL — distinguishing nickname vs league vs badge:

In each top corner the screen shows TWO stacked text lines next to a
club crest. There is also a small NUMERIC BADGE (level indicator like
"57", "100", "110") rendered in a colored rectangle or circle between
the crest and the nickname. This badge is NOT part of the nickname.

    ┌─ crest ─┐ [57] РД_Aleksfifa         <- big bright nickname (team1/team2)
    │         │      Локомотив Амстердам   <- smaller dimmed league/affiliation
    └─────────┘
         ^badge (level number — IGNORE, never include in team1/team2)

  * team1 / team2 = the BIG, BRIGHT, top-most line in the corner.
    It is the NICKNAME ONLY — without any leading/trailing badge number.
    Examples that ARE valid nicknames:
      "РД_Aleksfifa", "OliverBax", "@user1234", "boze", "YUPII",
      "Zardes-27", "Kaef", "zhbrrrr".
  * The BADGE is a 1-3 digit number (like 57, 100, 110) in a small
    colored box next to the crest. NEVER include it in team1/team2.
    If you see "100 Kaef", the nickname is "Kaef", NOT "100Kaef".
  * league       = the SMALLER, DIMMER, second line below the nickname.
    Examples that ARE leagues / affiliations, NEVER nicknames:
      "Лига Гвардиолыча", "НЕТ ЛИГИ", "Локомотив Амстердам",
      "УДП Украина", "VSA", "ЛК Алексcфира", "Ukraine",
      "The Best", "EA FC LEGENDS".

NEVER put the small-font league text into team1/team2. If you only
see one stacked line in a corner, it is the nickname (league is null).
NEVER include the badge number in team1/team2.

Other rules:
- Copy nicknames and scorer names VERBATIM. Preserve case, digits, dots,
  dashes, underscores, accented and Cyrillic letters. Do NOT invent
  extra letters, spaces, or "translate" Cyrillic into Latin.
- Score is the bigger digits at the top; "90:00" is a timer — ignore it.
- Goal events appear on the right side of the screen, often as
  "<minute>' <lastname> ГОЛ". Each goal has a coloured icon:
    * GREEN icon / left-aligned card  =>  side="home"   (TEAM1 scored)
    * BLUE  icon / right-aligned card =>  side="away"   (TEAM2 scored)
  If you cannot tell the colour, set side to null.
- "90'+1" / "45+2'" stoppage notation: keep the base minute (90, 45),
  drop the "+N".
- The "name" field must contain ONLY the footballer's surname/name.
  NEVER include the word "ГОЛ", "GOAL", "gol" or any variation in "name".
  The screen shows "<minute>' <name> ГОЛ" but you must strip "ГОЛ" —
  e.g. if screen says "23' Mbappé ГОЛ", output "name": "Mbappé".
  Do NOT double letters or alter spelling — copy the name exactly as shown.
- The number of goals in "goals" should equal score1+score2 when possible.
  If you can't detect a goal cleanly, omit it (better fewer than wrong).
"""


def _ai_key() -> str:
    return (os.getenv("OPENROUTER_API_KEY") or _OPENROUTER_DEFAULT_KEY).strip()


def _openrouter_keys() -> list[str]:
    """Return all available OpenRouter keys in rotation order.

    Picks up keys from (in order):
      1. ``OPENROUTER_API_KEY`` (env)  — falls back to the hardcoded primary.
      2. ``OPENROUTER_API_KEY_2``      — falls back to a hardcoded secondary.
      3. ``OPENROUTER_API_KEY_3``      — falls back to a hardcoded tertiary.
      4. ``OPENROUTER_API_KEY_4`` … ``OPENROUTER_API_KEY_{_MAX_OPENROUTER_KEYS}``
         — env-only, no hardcoded fallback. Set as many as you need
         without changing code; absent vars are skipped.

    Duplicates are deduplicated (e.g. accidentally setting the same
    key twice doesn't waste a 429 retry slot).
    """
    keys: list[str] = []
    seen: set[str] = set()

    def _add(raw: str | None) -> None:
        if not raw:
            return
        s = raw.strip()
        if s and s not in seen:
            seen.add(s)
            keys.append(s)

    # Primary + legacy hardcoded slots 2 and 3.
    _add(_ai_key())
    _add(_OPENROUTER_API_KEY_2)
    _add(_OPENROUTER_API_KEY_3)
    # Additional env-only keys (no code change required to plug in more).
    for n in range(4, _MAX_OPENROUTER_KEYS + 1):
        _add(os.getenv(f"OPENROUTER_API_KEY_{n}", ""))

    return keys or [_ai_key()]


def ai_is_available() -> bool:
    return bool(_GEMINI_API_KEY or _GROQ_API_KEY or _ai_key())


def _ai_strip_fences(text: str) -> str:
    s = (text or "").strip()
    s = re.sub(r"^```(?:json|JSON)?\s*", "", s)
    s = re.sub(r"\s*```\s*$", "", s)
    return s.strip()


def _ai_extract_json_block(text: str) -> str | None:
    """Find the first balanced {...} block, ignoring quoted braces.

    Returns None if no balanced object exists.
    """
    s = text or ""
    n = len(s)
    i = 0
    while i < n and s[i] != "{":
        i += 1
    if i >= n:
        return None
    depth = 0
    in_str = False
    esc = False
    j = i
    while j < n:
        ch = s[j]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return s[i:j + 1]
        j += 1
    return None


def _ai_loose_json_loads(blob: str) -> dict | None:
    """json.loads with light repair pass for the kind of slop vision-LLMs
    emit (trailing commas, single quotes, unescaped newlines in strings).
    Returns None on failure.
    """
    if not blob:
        return None
    try:
        return json.loads(blob)
    except Exception:
        pass
    repaired = blob
    # 1) drop trailing commas before } or ]
    repaired = re.sub(r",\s*([}\]])", r"\1", repaired)
    # 2) replace single-quoted strings with double-quoted (best-effort)
    if "'" in repaired and '"' not in repaired:
        repaired = repaired.replace("'", '"')
    # 3) collapse stray bare newlines inside strings
    try:
        return json.loads(repaired)
    except Exception:
        return None


def _ai_coerce_int(v):
    if v is None:
        return None
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def _ai_parse_response(content: str) -> dict:
    raw = content or ""
    blob = _ai_strip_fences(raw)
    obj = _ai_loose_json_loads(blob)
    if obj is None:
        # Try to extract a balanced {...} block first (handles prose
        # before/after JSON like "Here is the result: {...} Hope it helps").
        block = _ai_extract_json_block(blob) or _ai_extract_json_block(raw)
        if block:
            obj = _ai_loose_json_loads(block)
    if obj is None:
        raise ValueError(f"non-JSON response: {raw[:200]!r}")
    if not isinstance(obj, dict):
        raise ValueError(f"non-object JSON: {raw[:200]!r}")
    goals_raw = obj.get("goals") or []
    goals: list[dict] = []
    if isinstance(goals_raw, list):
        for g in goals_raw:
            if not isinstance(g, dict):
                continue
            name = str(g.get("name") or "").strip()
            if not name:
                continue
            side_raw = (g.get("side") or "").strip().lower() or None
            if side_raw not in (None, "home", "away"):
                # tolerate "left"/"right"/"green"/"blue"
                if side_raw in ("left", "green"):
                    side_raw = "home"
                elif side_raw in ("right", "blue"):
                    side_raw = "away"
                else:
                    side_raw = None
            goals.append({
                "name":   name,
                "minute": _ai_coerce_int(g.get("minute")),
                "side":   side_raw,
            })
    return {
        "score1": _ai_coerce_int(obj.get("score1")),
        "score2": _ai_coerce_int(obj.get("score2")),
        "pen1":   _ai_coerce_int(obj.get("pen1")),
        "pen2":   _ai_coerce_int(obj.get("pen2")),
        "team1":  (str(obj.get("team1") or "").strip() or None),
        "team2":  (str(obj.get("team2") or "").strip() or None),
        "league_plate": (str(obj.get("league") or "").strip() or None),
        "goals":  goals,
    }


def _ai_call_one(image_b64: str, model: str, timeout: float = 30.0,
                 api_key: str | None = None):
    """Run a single OpenRouter vision call. Returns (parsed, raw, elapsed).

    ``max_tokens`` is intentionally generous (2000) because reasoning
    models like ``nvidia/nemotron-nano-12b-v2-vl`` consume hundreds of
    tokens in their internal ``reasoning`` channel **before** emitting
    any visible ``content``. With the previous 400-token budget the
    model would run out of room while still thinking and return
    ``content: null`` with ``finish_reason: "length"``.
    """
    key = api_key or _ai_key()
    body = json.dumps({
        "model": model,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": _AI_PROMPT},
                {"type": "image_url",
                 "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}},
            ],
        }],
        "max_tokens": 2000,
        "temperature": 0.1,
    }).encode("utf-8")
    req = urllib.request.Request(
        _OPENROUTER_URL,
        data=body,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/fc-league-bot",
            "X-Title": "FC League Bot",
        },
    )
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body_b = r.read()
    dt = time.time() - t0
    try:
        data = json.loads(body_b)
    except Exception as e:
        raise ValueError(f"non-JSON API response: {body_b[:200]!r}") from e
    if not isinstance(data, dict):
        raise ValueError(f"non-object API response: {str(data)[:200]!r}")
    if data.get("error"):
        raise ValueError(f"API error: {str(data.get('error'))[:200]!r}")
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ValueError(
            f"API returned no choices: {str(data)[:200]!r}"
        )
    first = choices[0]
    if not isinstance(first, dict):
        raise ValueError(f"choice[0] not a dict: {str(first)[:200]!r}")
    message = first.get("message")
    if not isinstance(message, dict):
        # Some providers emit a flat ``"text"`` field instead.
        msg = first.get("text") or ""
        reasoning = ""
    else:
        msg = message.get("content") or ""
        # Reasoning models (e.g. nemotron-nano-12b-v2-vl) put their
        # private chain-of-thought in ``reasoning`` and only emit the
        # visible ``content`` after it. If the model ran out of tokens
        # before flipping to content, ``reasoning`` may still contain a
        # JSON block we can salvage.
        reasoning = message.get("reasoning") or ""
    if not isinstance(msg, str):
        # Vision-LLMs sometimes wrap content in [{"type":"text","text":...}]
        if isinstance(msg, list):
            msg = " ".join(
                str(p.get("text", "")) for p in msg
                if isinstance(p, dict)
            )
        else:
            msg = str(msg)
    if not isinstance(reasoning, str):
        reasoning = str(reasoning) if reasoning else ""
    try:
        parsed = _ai_parse_response(msg)
        return parsed, msg, dt
    except ValueError:
        if reasoning:
            # Try to salvage from the reasoning trace.
            parsed = _ai_parse_response(reasoning)
            return parsed, reasoning, dt
        raise


# Substrings that strongly indicate "this is league/affiliation text,
# NOT a player nickname". Used by ``_ai_post_process`` to repair
# responses where the model dumped the small-font league line into
# team1 or team2 (most often Baidu OCR-Fast, which reads pure pixels
# and doesn't follow the prompt's "big bright = nickname" rule).
_LEAGUE_HINTS = (
    "лиг", "гварди", "vsa", "вса", "удп", "удип",
    "локомотив", "ukraine", "украина", "нет лиги",
    "no league", "лк ",
)


def _looks_like_league(s: str | None) -> bool:
    if not s:
        return False
    low = str(s).strip().lower()
    if not low:
        return False
    return any(h in low for h in _LEAGUE_HINTS)


def _ai_post_process(parsed: dict) -> None:
    """In-place safety net: if the model put the small-font league line
    into team1 / team2, lift it into the league field and blank the
    nickname so the user gets ``—`` (and a 🔄 Другой моделью retry
    button) rather than a fake match against "Локомотив Амстердам".

    Also strips badge/level numbers (57, 100, 110) that the model
    sometimes prepends/appends to the nickname.

    For penalty fields: drops pen1/pen2 unless BOTH are non-null AND
    the regulation score is a draw — vision models love hallucinating
    one or the other from random UI elements.
    """
    if not isinstance(parsed, dict):
        return
    # Strip badge numbers from team names (e.g. "100Kaef" → "Kaef")
    for k in ("team1", "team2"):
        v = parsed.get(k)
        if isinstance(v, str) and v.strip():
            parsed[k] = _strip_badge_number(v.strip())
    league = parsed.get("league")
    for k in ("team1", "team2"):
        v = parsed.get(k)
        if isinstance(v, str) and _looks_like_league(v):
            # Promote to league if we don't already have one.
            if not league or not str(league).strip():
                parsed["league"] = v.strip()
                league = parsed["league"]
            # Wipe the bogus nickname so downstream fuzzy-matching
            # doesn't accidentally find a registered player whose
            # game_nickname happens to overlap with league letters.
            parsed[k] = ""

    # ── Penalty sanity gate ────────────────────────────────────────────
    # Keep pen1/pen2 only when (a) both are integers, (b) regulation is
    # a draw (penalties never happen otherwise), (c) the shootout score
    # is itself NOT a draw (a tied shootout means we read garbage), and
    # (d) the values look plausible (0..30 range — penalty rounds rarely
    # exceed 10 each side; 30 is a generous upper bound to also catch
    # very-extended shootouts without admitting timer leaks like 90/120).
    s1 = parsed.get("score1")
    s2 = parsed.get("score2")
    p1 = parsed.get("pen1")
    p2 = parsed.get("pen2")
    drop_pens = False
    if p1 is None or p2 is None:
        drop_pens = True
    elif not isinstance(p1, int) or not isinstance(p2, int):
        drop_pens = True
    elif p1 < 0 or p2 < 0 or p1 > 30 or p2 > 30:
        drop_pens = True
    elif s1 is None or s2 is None or s1 != s2:
        # Regulation must be a draw for a shootout to occur.
        drop_pens = True
    elif p1 == p2:
        # A shootout can't end level — must have been misread.
        drop_pens = True
    if drop_pens:
        parsed["pen1"] = None
        parsed["pen2"] = None


def _gemini_call_one(image_b64: str, timeout: float = 30.0):
    """Call Google Gemini Vision API. Returns (parsed, raw, elapsed)."""
    url = (f"{_GEMINI_URL}/{_GEMINI_MODEL}:generateContent"
           f"?key={_GEMINI_API_KEY}")
    body = json.dumps({
        "contents": [{"parts": [
            {"text": _AI_PROMPT},
            {"inline_data": {"mime_type": "image/jpeg", "data": image_b64}},
        ]}],
        "generationConfig": {"temperature": 0.1, "maxOutputTokens": 2000},
    }).encode("utf-8")
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/json"},
    )
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        resp_b = r.read()
    dt = time.time() - t0
    data = json.loads(resp_b)
    if data.get("error"):
        raise ValueError(f"Gemini API error: {str(data['error'])[:200]!r}")
    candidates = data.get("candidates")
    if not candidates:
        raise ValueError(f"Gemini returned no candidates: {str(data)[:200]!r}")
    parts = candidates[0].get("content", {}).get("parts", [])
    text = "".join(p.get("text", "") for p in parts)
    parsed = _ai_parse_response(text)
    return parsed, text, dt


def _groq_call_one(image_b64: str, timeout: float = 30.0, model: str | None = None):
    """Call Groq Vision API (llama-4-scout). Returns (parsed, raw, elapsed)."""
    if not _GROQ_API_KEY:
        raise ValueError("GROQ_API_KEY not configured")
    use_model = model or _GROQ_MODEL
    body = json.dumps({
        "model": use_model,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": _AI_PROMPT},
                {"type": "image_url",
                 "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}},
            ],
        }],
        "max_tokens": 2000,
        "temperature": 0.1,
    }).encode("utf-8")
    req = urllib.request.Request(
        _GROQ_URL,
        data=body,
        headers={
            "Authorization": f"Bearer {_GROQ_API_KEY}",
            "Content-Type": "application/json",
        },
    )
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        resp_b = r.read()
    dt = time.time() - t0
    data = json.loads(resp_b)
    if data.get("error"):
        raise ValueError(f"Groq API error: {str(data['error'])[:200]!r}")
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ValueError(f"Groq returned no choices: {str(data)[:200]!r}")
    msg = choices[0].get("message", {}).get("content") or ""
    if not isinstance(msg, str):
        msg = str(msg)
    parsed = _ai_parse_response(msg)
    return parsed, msg, dt


def ai_read_screenshot(image_bytes: bytes, model: str | None = None,
                       timeout: float = 30.0,
                       models_override: tuple[str, ...] | list[str] | None = None):
    """Run AI OCR on a screenshot. None if all configured models fail.

    Tries Google Gemini first (if GEMINI_API_KEY is set), then falls
    through to OpenRouter models.

    If ``models_override`` is given, it replaces the default candidate
    chain entirely (used by the «Другой моделью» retry button so we
    pin a specific model rather than falling through the whole list).
    """
    if not ai_is_available():
        return None
    image_b64 = base64.b64encode(image_bytes).decode("ascii")

    # ── Try Gemini first (1500 req/day free, no 429s) ──
    if _GEMINI_API_KEY and not models_override:
        for attempt in range(2):
            try:
                parsed, raw, dt = _gemini_call_one(image_b64, timeout=timeout)
                parsed["model"] = f"gemini/{_GEMINI_MODEL}"
                parsed["raw"] = raw
                parsed["elapsed_s"] = dt
                _ai_post_process(parsed)
                log.info("Gemini OCR ok via %s in %.1fs", _GEMINI_MODEL, dt)
                return parsed
            except Exception as e:
                log.warning("gemini %s -> %r", _GEMINI_MODEL, e)
                if attempt == 0 and "429" in str(e):
                    time.sleep(2)
                    continue
                # 502/503 — don't retry, fall through to Groq/OpenRouter
                break

    # ── Try Groq (llama-4-scout/maverick vision, fast, free tier) ──
    if _GROQ_API_KEY and not models_override:
        for groq_model in _GROQ_FALLBACK_MODELS:
            for attempt in range(2):
                try:
                    parsed, raw, dt = _groq_call_one(image_b64, timeout=timeout, model=groq_model)
                    parsed["model"] = f"groq/{groq_model}"
                    parsed["raw"] = raw
                    parsed["elapsed_s"] = dt
                    _ai_post_process(parsed)
                    log.info("Groq OCR ok via %s in %.1fs", groq_model, dt)
                    return parsed
                except Exception as e:
                    log.warning("groq %s -> %r", groq_model, e)
                    if attempt == 0 and "429" in str(e):
                        time.sleep(2)
                        continue
                    break  # 403/502/other → try next groq model

    # ── OpenRouter fallback chain ──
    seen: set[str] = set()
    candidates: list[str] = []
    if models_override:
        chain: list[str] = [m for m in models_override if m]
    else:
        chain = [model or AI_DEFAULT_MODEL, *AI_FALLBACK_MODELS]
    for m in chain:
        if m and m not in seen:
            candidates.append(m)
            seen.add(m)
    all_keys = _openrouter_keys()
    last_err: Exception | None = None
    for m in candidates:
        for attempt in range(len(all_keys)):  # rotate through all available keys on 429
            key_to_use = all_keys[attempt % len(all_keys)]
            try:
                parsed, raw, dt = _ai_call_one(image_b64, m, timeout=timeout, api_key=key_to_use)
                parsed["model"] = m
                parsed["raw"] = raw
                parsed["elapsed_s"] = dt
                _ai_post_process(parsed)
                return parsed
            except urllib.error.HTTPError as e:
                try:
                    err_body = e.read()[:200]
                except Exception:
                    err_body = b""
                log.warning("openrouter %s -> HTTP %s: %.200s", m, e.code, err_body)
                last_err = e
                if e.code == 429 and attempt < len(all_keys) - 1:
                    log.warning("openrouter %s -> 429 with key%d, switching to key%d", m, attempt + 1, attempt + 2)
                    continue  # retry with next key
                break  # non-retryable HTTP error (including 502/503), try next model
            except Exception as e:                        # pragma: no cover
                log.warning("openrouter %s -> %r", m, e)
                last_err = e
                # Don't retry on 502/timeout — move to next model immediately
                break
    if last_err:
        log.error("all OpenRouter models failed: %r", last_err)
    return None


def ai_compare_screenshot(image_bytes: bytes,
                          models: tuple[str, ...] = AI_COMPARE_MODELS,
                          timeout: float = 45.0) -> list[dict]:
    image_b64 = base64.b64encode(image_bytes).decode("ascii")
    out: list[dict] = []
    for m in models:
        row: dict = {"model": m, "ok": False, "error": None,
                     "score1": None, "score2": None,
                     "pen1": None, "pen2": None,
                     "team1": None, "team2": None,
                     "league_plate": None, "elapsed_s": 0.0}
        try:
            parsed, raw, dt = _ai_call_one(image_b64, m, timeout=timeout)
            row.update(parsed)
            row["ok"] = True
            row["elapsed_s"] = dt
        except urllib.error.HTTPError as e:
            try:
                row["error"] = f"HTTP {e.code}: {e.read()[:160].decode('utf-8','replace')}"
            except Exception:
                row["error"] = f"HTTP {e.code}"
        except Exception as e:                        # pragma: no cover
            row["error"] = repr(e)
        out.append(row)
    return out

log = logging.getLogger(__name__)


# ── Region map (fractional coords on a normalised canvas) ────────────────────
# Top header band sits roughly in the upper 22% of the screen.
# Tuned for FC Mobile 2024-2026 post-match screen across multiple
# phone resolutions (720p, 1080p, 1440p).
REGIONS: dict[str, tuple[float, float, float, float]] = {
    # ── Top header band layout (FC Mobile match recap) ──────────────────
    # The dark blue header occupies the top ~0.08 → ~0.22 of the image.
    # Empirically (measured by scanning bright text rows across the
    # bundled fixture and 11 user screenshots at 1280×{576,589,591},
    # 2412×1080, and 2556×1179) the nickname text sits at y/H ≈
    # 0.117–0.165, and the sub-line at y/H ≈ 0.16–0.21.  The previous
    # ranges (0.04–0.12) were tuned for the FIXTURE only and missed
    # the actual nickname on every other capture — they accidentally
    # captured the dark header top + crest sliver instead.
    "team1":        (0.18, 0.10, 0.44, 0.155),
    # Sub-line under team1.  Empirically Tesseract reads the small
    # "Лига Гвардиолыча" plate FAR better when it can see the big
    # nickname above it in the same crop (the layout engine then locks
    # onto the correct text scale).  So team1_sub spans the whole
    # nickname-plus-sub band — _clean_team_name only ever consumes the
    # nickname slot from REGIONS["team1"] anyway, and ``_pick_league_plate``
    # scans ALL candidates from this crop for the league hint.
    "team1_sub":    (0.18, 0.10, 0.44, 0.22),
    # Tournament / league plate — narrow left-side crop so it doesn't
    # overlap the score area.  Same y as team1_sub so we don't miss
    # the league when it sits under team1.
    "league_plate": (0.16, 0.10, 0.44, 0.22),
    # Big central score band. The match clock ("90:00") sometimes leaks
    # into the bottom of this crop — _parse_score below rejects clock-shaped
    # digit pairs explicitly, so the score "1 - 1" wins over the timer.
    # Widened vertically (0.08–0.21) to also fit the tall captures
    # (1080/1179 px) where the score sits a bit higher in the band.
    "score":        (0.42, 0.08, 0.60, 0.21),
    # Team 2 name (right side of the top band).  Same y as team1.
    # Widened on the right (0.92) to capture longer nicknames like
    # "AuraBroAura88888" but stops *before* the rightmost crest.
    "team2":        (0.58, 0.10, 0.92, 0.155),
    # Right-side sub band.  Spans the full nickname-plus-sub area so
    # Tesseract reads "Лига Гвардиолыча" reliably (same rationale as
    # team1_sub).
    "team2_sub":    (0.58, 0.10, 0.92, 0.22),
    # The full goal-event list (right pane, ~minute / scorer / "ГОЛ")
    # Extended vertically to catch 5+ goal matches.
    "goals_panel":  (0.49, 0.22, 0.96, 0.90),
}


# ── Tournament-type detection keywords ───────────────────────────────────────
# These are matched against any text we extract (case-insensitive, with
# slack for OCR errors — we use simple substring tests after normalisation).
# These keywords are matched as case-insensitive substrings against a
# normalised blob containing OCR text. They must be SHORT enough to
# survive realistic OCR misreads — e.g. "Гвардиольыча" is frequently
# misread as "Гвардиопыча" / "Гваpдиольыча" (Cyrillic 'л' ↔ 'п', 'р'
# ↔ 'p'), so we don't anchor on the full word.
VSA_KEYWORDS = (
    "vsa",
    "вса",
    "гварди",          # short root — survives 'Гвардиол*'/'Гвардиоп*' misreads
    "guardiol",        # latin spelling
    "guardiola",
    "лига гв",         # 'Лига Гв...' — extra anchor for noisy crops
)
RI_KEYWORDS = (
    " ri ",
    "_ri_",
    "/ri",
    "ри ",
    "real",
    "реал",
    "реальная игра",
    "real image",
)


# ── Result container ─────────────────────────────────────────────────────────
@dataclass
class MatchScreenshot:
    score1: Optional[int] = None
    score2: Optional[int] = None
    # Penalty-shootout scores. Both are ``None`` for matches without a
    # shootout (the overwhelming majority). When set, they ALWAYS come
    # in pairs, the regulation score is a draw, and pen1 != pen2.
    pen1: Optional[int] = None
    pen2: Optional[int] = None
    team1: Optional[str] = None
    team2: Optional[str] = None
    league_plate: Optional[str] = None
    tournament_type: Optional[str] = None     # 'vsa' | 'ri' | None
    # Ordered list of goal events, each: {"name": str, "minute": int|None, "side": "home"|"away"|None}
    goals: list[dict] = field(default_factory=list)
    raw_texts: dict[str, str] = field(default_factory=dict)
    confidence: dict[str, float] = field(default_factory=dict)

    @property
    def score(self) -> Optional[str]:
        if self.score1 is None or self.score2 is None:
            return None
        return f"{self.score1}:{self.score2}"

    @property
    def has_penalties(self) -> bool:
        return self.pen1 is not None and self.pen2 is not None

    @property
    def pen_score(self) -> Optional[str]:
        if not self.has_penalties:
            return None
        return f"{self.pen1}:{self.pen2}"

    def to_dict(self) -> dict:
        return asdict(self)


# ── Image preprocessing helpers ──────────────────────────────────────────────
def _crop_region(img: Image.Image, box: tuple[float, float, float, float]) -> Image.Image:
    W, H = img.size
    x1, y1, x2, y2 = box
    return img.crop((int(x1 * W), int(y1 * H), int(x2 * W), int(y2 * H)))


def _binarize_white_text(im: Image.Image, threshold: int, scale: int = 4) -> Image.Image:
    """
    Most game UIs render team / score text in light colours on a dark band.
    Binarising with a brightness threshold (white→black, dark→white)
    flattens out the gradient background and gives Tesseract clean text.
    """
    g = im.convert("L")
    bw = g.point(lambda p: 0 if p >= threshold else 255)
    bw = bw.resize((bw.width * scale, bw.height * scale), Image.LANCZOS)
    return bw


def _binarize_adaptive(im: Image.Image, scale: int = 4) -> Image.Image:
    """Adaptive local-mean binarization for uneven lighting / gradient
    backgrounds typical in game screenshots.

    Uses a block-based approach: for each pixel, compare to the average
    brightness in a local neighbourhood. This handles the gradient
    header bands much better than a single global threshold.
    """
    try:
        import numpy as np
        g = im.convert("L")
        arr = np.array(g, dtype=np.float32)
        # Block size ~ 1/8 of the width, at least 15px, must be odd.
        block = max(15, (arr.shape[1] // 8) | 1)
        # Integral image for fast local mean computation.
        integral = np.cumsum(np.cumsum(arr, axis=0), axis=1)
        h, w = arr.shape
        pad = block // 2
        # Compute local mean using integral image.
        y1 = np.clip(np.arange(h) - pad, 0, h - 1)
        y2 = np.clip(np.arange(h) + pad, 0, h - 1)
        x1 = np.clip(np.arange(w) - pad, 0, w - 1)
        x2 = np.clip(np.arange(w) + pad, 0, w - 1)
        # Build corner sums.
        A = integral[np.ix_(y1, x1)]
        B = integral[np.ix_(y1, x2)]
        C = integral[np.ix_(y2, x1)]
        D = integral[np.ix_(y2, x2)]
        count = ((y2 - y1 + 1)[:, None]) * ((x2 - x1 + 1)[None, :])
        local_mean = (D - B - C + A) / count.astype(np.float32)
        # Pixels brighter than their neighbourhood are text (white-on-dark).
        # Offset of -15 to require the pixel to be noticeably brighter.
        binary = np.where(arr > local_mean - 15, 0, 255).astype(np.uint8)
        bw = Image.fromarray(binary, mode="L")
    except ImportError:
        # numpy unavailable: fall back to multi-threshold approach.
        g = im.convert("L")
        bw = g.point(lambda p: 0 if p >= 150 else 255)
    bw = bw.resize((bw.width * scale, bw.height * scale), Image.LANCZOS)
    return bw


def _enhance_contrast(im: Image.Image, scale: int = 4) -> Image.Image:
    """High-contrast preprocessing: auto-contrast + sharpen before binarization.

    Useful for faded / low-contrast text on gradient backgrounds where
    simple thresholding fails.
    """
    from PIL import ImageFilter, ImageEnhance
    g = im.convert("L")
    # Auto-level: stretch histogram to full 0-255 range.
    g = ImageOps.autocontrast(g, cutoff=5)
    # Sharpen to make edges crisper.
    g = g.filter(ImageFilter.SHARPEN)
    # Boost contrast further.
    g = ImageEnhance.Contrast(g).enhance(2.0)
    # Final binarize at midpoint.
    bw = g.point(lambda p: 0 if p >= 128 else 255)
    bw = bw.resize((bw.width * scale, bw.height * scale), Image.LANCZOS)
    return bw


def _binarize_dark_text(im: Image.Image, threshold: int, scale: int = 4) -> Image.Image:
    """Inverse: dark text on light background (occasionally needed)."""
    g = im.convert("L")
    bw = g.point(lambda p: 255 if p >= threshold else 0)
    bw = bw.resize((bw.width * scale, bw.height * scale), Image.LANCZOS)
    return bw


# ── Goals-panel color helpers ────────────────────────────────────────────────
# In the FC Mobile post-match screen each goal event row is prefixed by a
# colored ball icon: bright green for the home scorer, bright blue for the
# away scorer. Detecting these bands on a scan of the right pane gives us
# (a) the exact y-rows that contain a goal event and (b) the side without
# having to OCR colored text (tesseract is colour-blind).
def _is_green_ball(rgb: tuple[int, int, int]) -> bool:
    r, g, b = rgb
    return g > 150 and (g - r) > 50 and (g - b) > 30


def _is_blue_ball(rgb: tuple[int, int, int]) -> bool:
    r, g, b = rgb
    return b > 150 and (b - r) > 50 and (b - g) > 10


def _cluster_rows(rows: list[int], gap: int = 5) -> list[tuple[int, int]]:
    """Collapse a sorted list of y-coords into (y_start, y_end) bands."""
    if not rows:
        return []
    out: list[tuple[int, int]] = []
    cur_start = rows[0]
    cur_last = rows[0]
    for y in rows[1:]:
        if y - cur_last <= gap:
            cur_last = y
        else:
            out.append((cur_start, cur_last))
            cur_start = y
            cur_last = y
    out.append((cur_start, cur_last))
    return out


# ── Field parsers (defined before OCR core so it can reference them) ─────────
# Match either dashes (typical match score: "1 - 1") or a colon (some games
# render the score with a colon: "1:1"). We capture the separator so we can
# rank results — dashes are far more reliable as score-only signals because
# the in-game match clock uses ":", which can sneak into the score crop.
_SCORE_RE = re.compile(r"(\d{1,2})\s*([-:—–])\s*(\d{1,2})")


def _is_match_score_pair(s1: int, s2: int, sep: str) -> bool:
    """
    Return True if (s1, s2) plausibly represents a match score rather than
    a clock reading. Football final scores are tiny (≤ ~20 each); clocks
    look like "90:00", "45:30", "0:00", "1:17" (early-game clock with
    seconds 17). We reject obvious clock shapes.
    """
    if not (0 <= s1 <= 30 and 0 <= s2 <= 30):
        return False
    # Dash-separated scores are unambiguous — the in-game clock never
    # uses a dash, so accept everything that survived the range check.
    if sep != ":":
        return True
    # Colon-separated readings are ambiguous: they could be a score
    # ("1:1") or a clock ("0:30", "1:17", "90:00").  Reject anything
    # that has a 2-digit right side: real football scores are almost
    # always single-digit, while the clock seconds are zero-padded
    # 2-digit ("01:17", "00:30"). This is the heuristic that made the
    # user's "1:17" parse fail before — we now refuse colon-pairs whose
    # right-hand side is 10 or above.
    if s2 >= 10:
        return False
    # Anything that looks like a clock reading: colon-separated, large
    # left-side minute, and a "round" right side (00, 30, 15, 45).
    if s1 >= 30 and s2 in (0, 15, 30, 45):
        return False
    # "90:00" and "45:00" are the canonical end-of-half timestamps.
    if (s1, s2) in ((45, 0), (90, 0), (120, 0), (105, 0)):
        return False
    return True


def _parse_score(text: str) -> Optional[tuple[int, int]]:
    """
    Pull a score out of an OCR string. Prefers the clearest, smallest
    reading: dash-separated first (because the timer never uses a dash),
    falling back to colon-separated only if it looks score-shaped.
    """
    if not text:
        return None
    matches = _SCORE_RE.findall(text)
    if not matches:
        return None
    # Pass 1: dash-separated wins outright.
    for s1s, sep, s2s in matches:
        if sep != ":":
            s1, s2 = int(s1s), int(s2s)
            if _is_match_score_pair(s1, s2, sep):
                return s1, s2
    # Pass 2: colon-separated, but only if it doesn't look like a clock.
    for s1s, sep, s2s in matches:
        s1, s2 = int(s1s), int(s2s)
        if _is_match_score_pair(s1, s2, sep):
            return s1, s2
    return None


_TEAM_NAME_CHAR = r"a-zA-Z0-9·\-\.@"   # letters, digits, mid-dot, hyphen, dot, @ — NO underscore

# Badge/level numbers that Tesseract sometimes picks up from the UI.
# These are 1-3 digit numbers (typically 50-200 range) displayed in a
# colored box next to the team crest. When they leak into the OCR
# output they appear as a leading/trailing number glued to the nickname.
_BADGE_RE_LEADING = re.compile(r"^(\d{1,3})([A-Za-zА-Яа-яЁё])")
_BADGE_RE_TRAILING = re.compile(r"([A-Za-zА-Яа-яЁё])(\d{2,3})$")


def _strip_badge_number(name: str) -> str:
    """Remove leading/trailing badge level numbers (like 100, 57) that
    Tesseract picks up from the level indicator next to the crest.

    Only strips when:
      - The number is 2-3 digits (badges are 10-200 range)
      - The remaining text is at least 3 characters (a real nickname)
      - The junction is letter→digit or digit→letter (no separator)
    """
    if not name:
        return name
    # Strip leading badge: "100Kaef" → "Kaef", "57OliverBax" → "OliverBax"
    m = _BADGE_RE_LEADING.match(name)
    if m:
        badge_part = m.group(1)
        if len(badge_part) >= 2:  # 2-3 digit badges only
            remainder = name[len(badge_part):]
            if len(remainder) >= 3:
                name = remainder
    # Strip trailing badge: "Kaef100" → "Kaef"
    m = _BADGE_RE_TRAILING.search(name)
    if m:
        badge_part = m.group(2)
        if len(badge_part) >= 2:
            remainder = name[:-len(badge_part)]
            # Only strip if the remainder looks like a name (has letters)
            if len(remainder) >= 3 and any(c.isalpha() for c in remainder):
                name = remainder
    return name


def _looks_like_gibberish(name: str) -> bool:
    """Heuristic: return True if a Tesseract-produced team name looks like
    OCR noise rather than a real player nickname.

    Indicators of gibberish:
      - Too many case transitions in a short name (e.g. "KaetBO7Z")
      - Very high digit ratio for a short name with no recognisable
        structure (real nicks like "Zardes-27" have a clear letter prefix)
      - Excessive mixing of unrelated character classes

    Real nicknames we must NOT reject:
      - "Zardes-27"        (letters + digits with a separator)
      - "AuraBroAura88888" (CamelCase + trailing digits — common pattern)
      - "OliverBax"        (CamelCase)
      - "zhbrrrr"          (all lowercase)
      - "YUPII"            (all uppercase)
      - "РД_Aleksfifa"     (Cyrillic + Latin mix — intentional)
    """
    if not name or len(name) < 3:
        return True

    letters = sum(1 for c in name if c.isalpha())
    digits = sum(1 for c in name if c.isdigit())
    total = letters + digits

    # If the name is very short (< 3 letters) and has digits mixed in
    # weirdly, it's probably badge+fragment garbage.
    if letters < 2:
        return True

    # Count upper→lower and lower→upper transitions (case flaps).
    # Real CamelCase names have 1-3 transitions; OCR noise has many.
    alpha_chars = [c for c in name if c.isalpha()]
    transitions = 0
    for i in range(1, len(alpha_chars)):
        prev_upper = alpha_chars[i - 1].isupper()
        curr_upper = alpha_chars[i].isupper()
        if prev_upper != curr_upper:
            transitions += 1

    # "KaetBO7Z" has transitions K→a, a→e (no), e→t (no), B→O (no), O→7 (skip), 7→Z (skip)
    # Actually alpha only: K,a,e,t,B,O,Z → K↓a (1), a→e (0), e→t (0), t↑B (2), B→O (0), O↓Z... wait O and Z are both upper
    # Let's count digit-letter alternation instead.
    # A better heuristic: count "digit islands" (runs of digits surrounded by letters).
    digit_islands = 0
    in_digit = False
    for c in name:
        if c.isdigit():
            if not in_digit:
                digit_islands += 1
                in_digit = True
        else:
            in_digit = False

    # Real patterns:
    #   "Zardes-27"      → 1 island (trailing "27") ✓
    #   "AuraBroAura88888" → 1 island (trailing) ✓
    #   "user1234"       → 1 island ✓
    # Gibberish patterns:
    #   "KaetBO7Z"       → 1 island ("7") but short + uppercase/digit mixing
    #   "Ka3tB07Z2"      → 3 islands

    # Multiple digit islands in a short name → likely garbage.
    if digit_islands >= 2 and total <= 10:
        return True

    # High uppercase ratio with embedded single digits in a short name
    # where the digit is surrounded by uppercase letters on both sides,
    # AND the LEFT part (before the digit) has chaotic case mixing
    # (both lowercase and uppercase letters). This catches "KaetBO7Z"
    # (left part "KaetBO" mixes cases) but not "CR7Fan" (left "CR" is
    # all-uppercase) or "R9Legend" (left "R" is single char).
    if total <= 10 and digits >= 1:
        for i, c in enumerate(name):
            if c.isdigit():
                left_upper = (i > 0 and name[i - 1].isalpha()
                              and name[i - 1].isupper())
                right_upper = (i < len(name) - 1
                               and name[i + 1].isalpha()
                               and name[i + 1].isupper())
                if left_upper and right_upper:
                    # Check the portion BEFORE the digit for mixed case.
                    left_part = name[:i]
                    has_lower_left = any(
                        x.islower() for x in left_part if x.isalpha()
                    )
                    has_upper_left = any(
                        x.isupper() for x in left_part if x.isalpha()
                    )
                    # Both lower AND upper on the left = chaotic garble.
                    if has_lower_left and has_upper_left:
                        return True

    return False


def _clean_team_name(text: str) -> Optional[str]:
    """
    OCR output often has stray punctuation / fragments of icons next to the
    actual team name. Pick the longest contiguous run of game-name characters.

    Underscores are commonly produced as OCR artifacts at word boundaries
    (e.g. "AuraBroAura88883_Aeey") — we split on them so the trailing
    "_Aeey" garbage is dropped. We also strip a leading lowercase prefix
    that Tesseract sometimes hallucinates from the league/icon plate
    (e.g. "deokGVL-LOKOMOTIV" → "GVL-LOKOMOTIV").

    Supports both Latin and Cyrillic nicknames (e.g. "Spa_Msk",
    "AuraBroAura88888", "bагpат", "РД_Aleksfifa").
    """
    if not text:
        return None
    # Latin/digit/dot fragments first (handles AuraBroAura88883). If we get
    # nothing, fall back to a permissive Unicode pattern (Cyrillic team names).
    fragments = re.findall(rf"[{_TEAM_NAME_CHAR}]{{3,}}", text)
    if not fragments:
        # Try wider pattern including Cyrillic and underscores for mixed nicks.
        fragments = re.findall(r"[\w·\-.@]{3,}", text, flags=re.UNICODE)
        if not fragments:
            return None

    # Strip a leading run of lowercase letters when followed by an
    # uppercase-led tail at least 3 chars long.
    cleaned: list[str] = []
    for c in fragments:
        m = re.match(r"^[a-z]{1,8}([A-Z][a-zA-Z0-9·\-.]{2,})$", c)
        if m:
            cleaned.append(m.group(1))
        else:
            cleaned.append(c)

    # Score each fragment: prefer the one with the most alphanumeric content
    # relative to its length (reject garbage-heavy fragments).
    def _fragment_quality(s: str) -> tuple[int, int]:
        alnum = sum(1 for c in s if c.isalnum())
        return (alnum, len(s))

    best = max(cleaned, key=_fragment_quality)
    # Strip leading/trailing punctuation/underscores.
    best = best.strip("·-_.")
    if not best:
        return None
    # Strip badge/level numbers that leaked from the UI.
    best = _strip_badge_number(best)
    if not best:
        return None
    # Reject gibberish — Tesseract noise that doesn't resemble a real nickname.
    if _looks_like_gibberish(best):
        return None
    return best


# ── OCR core ─────────────────────────────────────────────────────────────────
def _tess(im: Image.Image, lang: str, psm: int, whitelist: str | None = None) -> str:
    if not _OCR_AVAILABLE:
        return ""
    config = f"--psm {psm}"
    if whitelist:
        # Tesseract requires non-space chars; spaces are always allowed.
        config += f" -c tessedit_char_whitelist={whitelist}"
    try:
        return pytesseract.image_to_string(im, lang=lang, config=config).strip()
    except Exception as e:                  # pragma: no cover
        log.debug("tesseract failed (lang=%s psm=%s): %s", lang, psm, e)
        return ""


# ── Goal-event row OCR (per-row, colour-keyed) ───────────────────────────────
# Strip suffix words tesseract often reads after the scorer name. The game
# UI shows "ГОЛ" (Russian "GOAL") and that's by far the most common artefact;
# we also strip tesseract's mis-readings of the same word.
# Words that tesseract sometimes spits out for the trailing 'ГОЛ' icon-text:
# Г→R/P/F/T, О→0/o/e, Л→N/n/M/A/JI.  Plus the in-game label itself
# ('ГОЛ', 'GOAL', 'gol').  Used by _clean_scorer_name to STOP scanning
# at the first such token.
_GOAL_STOP_WORDS = {
    # 'ГОЛ' / 'gol' / 'goal' literally
    "гол", "гол.", "гoл", "gol", "goal",
    # Common OCR misreads of 'ГОЛ': leading char (Г→R/F/T/P/r/f/t/p),
    # middle (О→0/o/e/O), trailing (Л→N/n/M/m/A/a/J/j/I/i/I)
    "gon", "ron", "ros", "rom", "r0n",
    "fon", "fos", "fom", "f0n", "fan", "foi", "for", "fort",
    "ton", "tos", "tom", "t0n",
    "pon", "pos", "p0n",
    "rоч1", "ра1", "роз", "ron.", "ros.",
}

# Regex that matches a glitched 'ГОЛ' suffix appended to a name when the
# scorer-text crop spilled into the colored 'ГОЛ' badge to the right.
# We require a clear *case shift* (lower→upper) to avoid eating real name
# endings like "Cameron", "Anton", "Aaron" or "Sharon".
#
#   * leading uppercase look-alike of Г   (F R T P Г)
#   * middle 'O/0' look-alike of О         (O 0 О)
#   * trailing look-alike of Л              (N n M m A a I i J j)
#   * up to 2 trailing junk letters
_TOKEN_GOL_GLITCH_SUFFIX = re.compile(
    r"(?<=[a-zа-яё])"                # mid-word case shift (lower→upper)
    r"(?:[FRTPГг][O0oОо][NnMmAaIiJj]"
    r"|[FRTPГг][NnMmAaIiJj])"           # tolerant variant where 'О' got lost
    r"[a-zA-Z]{0,2}$"
)
# Same idea but for a SEPARATE trailing token (no case shift required):
#   'Lamine Yamal Fon' / 'Brahim ros' / 'Rafael Leao FOst'
_STANDALONE_GOL_GLITCH = re.compile(
    r"^[FRTPГг][O0oОо]?[NnMmAaIiJj][a-zA-Z]{0,2}\.?$"
)
_NAME_TOKEN_RE = re.compile(
    r"[A-Za-zА-Яа-яЁё\u00C0-\u024F][A-Za-zА-Яа-яЁё\u00C0-\u024F'’\.]{1,30}"
)


def _is_capitalised_word(tok: str) -> bool:
    """Returns True for tokens that look like a proper noun start
    ('Brahim', 'Leão', 'Дзюба') as opposed to an OCR artefact ('ron',
    '4', '<')."""
    if len(tok) < 2:
        return False
    first = tok[0]
    if not first.isalpha():
        return False
    return first.isupper() or first in "ÀÁÂÃÄÅÆÇÈÉÊËÌÍÎÏÐÑÒÓÔÕÖØÙÚÛÜÝÞА-ЯЁ"


def _clean_scorer_name(text: str) -> Optional[str]:
    """Pull the scorer's name out of the row OCR.

    The OCR output looks like ``"Brahim ron (we)"`` (with various
    glitched reads of 'ГОЛ' / replay-icon noise after the name). Strategy:

    1) Tokenise on whitespace.
    2) Walk left-to-right: keep word-shaped tokens that *aren't* one of
       the known 'ГОЛ' glitch words (``ron``, ``fon``, ``goal`` …).
    3) Stop at the first non-word or stop-word token.

    Names can be Cyrillic ("Дзюба"), Latin ("Bellingham"), or accented
    Latin ("Rafael Leão") — we keep accents.
    """
    if not text:
        return None
    s = text.replace("\n", " ")
    tokens = s.split()
    kept: list[str] = []
    seen_capitalised = False
    for raw in tokens:
        tok = raw.strip(".·-_,:;|`'\"()[]{}")
        if not tok:
            if kept:
                break
            continue
        low = tok.lower()
        # 'ГОЛ' / 'GOAL' or its OCR glitch siblings → stop scanning.
        if low in _GOAL_STOP_WORDS:
            break
        # Standalone 'ГОЛ'-shaped token: 'ГОЛ', 'Fon', 'TOs', 'r0n', 'PON'…
        if _STANDALONE_GOL_GLITCH.match(tok):
            break
        # If the token isn't word-shaped, abort (we've moved into the
        # icon/punctuation tail of the row).
        if not _NAME_TOKEN_RE.fullmatch(tok):
            if kept:
                break
            continue
        # Per-token glitched 'ГОЛ' suffix attached without a space:
        #     'YamalFon' → 'Yamal',  'Brahimron' → 'Brahim',
        #     'AlvarezROn' → 'Alvarez', 'SzobosairOn' → 'Szobosai'.
        stripped = _TOKEN_GOL_GLITCH_SUFFIX.sub("", tok)
        if stripped and stripped != tok and len(stripped) >= 2:
            tok = stripped
        # First token must be capitalised — skips 'a', 'я', 'st', etc.
        # tesseract sometimes prefixes to the actual name.
        if not kept and not _is_capitalised_word(tok):
            continue
        if _is_capitalised_word(tok):
            seen_capitalised = True
        kept.append(tok)
    # Trim trailing short tokens (1–3 chars) that arrive AFTER at least
    # one solid 4+ char token. These are almost always glitch-reads of
    # the goal-icon ('Ce', 'Oe', 'Cy', 'ri', 'tie', 'oF') rather than
    # real name fragments — FC Mobile scorer cards never end on a 1–3
    # char syllable. Common surname prefixes (De/Da/El/Le/Mc/St) only
    # ever appear *between* longer tokens, so this is safe.
    while len(kept) >= 2 and len(kept[-1]) <= 3:
        kept.pop()
    name = " ".join(kept).strip(" .-’'")
    if not seen_capitalised or len(name) < 2:
        return None
    return name


_MINUTE_RE = re.compile(r"(\d{1,3})\s*[’'`´\u2032]?\s*(?:\+\s*(\d{1,2}))?")


def _parse_minute(text: str) -> Optional[int]:
    """Pull the goal minute from an OCR string, e.g. ``"81'"`` → 81 or
    ``"90'+1"`` → 91.

    OCR-misreads of the leading digit are tolerated: ``"SO+1"`` (S misread
    for 9), ``"o0'+1"`` (zero misread). We also strip a leading 'O' /
    'S' / 'B' since these are the most common misreads.
    """
    if not text:
        return None
    # Common OCR substitutions for digits at the start of the minute.
    cleaned = (
        text.replace("\n", " ")
        .replace("S", "9")
        .replace("s", "9")
        .replace("O", "0")
        .replace("o", "0")
        .replace("B", "8")
        .replace("|", "1")
        .replace("l", "1")
        .replace("I", "1")
    )
    m = _MINUTE_RE.search(cleaned)
    if not m:
        return None
    try:
        base = int(m.group(1))
        extra = int(m.group(2)) if m.group(2) else 0
    except ValueError:
        return None
    if not (1 <= base <= 130):
        return None
    return base + extra


def _ocr_goal_row(
    img: Image.Image,
    y_center: int,
    panel_x1: int,
    panel_x2: int,
    ball_x: int,
    row_h: int,
) -> tuple[Optional[int], Optional[str]]:
    """OCR a single goal-event row.

    The row is split into ``minute_strip`` (left of the colored ball) and
    ``name_strip`` (right of the ball, excluding the trailing video-replay
    icon). We try several thresholds + PSMs and pick the best candidate.
    """
    H = img.size[1]
    y1 = max(0, y_center - row_h // 2)
    y2 = min(H, y_center + row_h // 2)
    minute_crop = img.crop((panel_x1, y1, max(panel_x1 + 1, ball_x - 8), y2))
    # Drop the right-most ~70 px (replay-camera icon).
    name_crop = img.crop((min(panel_x2 - 1, ball_x + 22), y1, max(ball_x + 23, panel_x2 - 70), y2))

    minute_text_candidates: list[str] = []
    name_text_candidates: list[str] = []

    # Three thresholds × two PSMs is a good balance for the colored-ball
    # row OCR — much cheaper than the heavy 24-pass sweep used for the
    # team/score regions. Enhanced: also try adaptive and contrast-enhanced
    # preprocessing for gradient backgrounds.
    # Wider threshold sweep (added 130/150/170/200) catches narrow OCR
    # "sweet spots" where the bold digits are crisp on the gradient
    # background. Without 170 in particular the score '59' reads as
    # '99' (the '5' top stroke dies at thr<=140) and our previous
    # max-based vote then picked the noisy 99.
    for thr in (100, 120, 130, 140, 150, 160, 170, 180, 200):
        m_pre = _binarize_white_text(minute_crop, threshold=thr, scale=4)
        n_pre = _binarize_white_text(name_crop, threshold=thr, scale=4)
        for psm in (6, 7, 13):
            # Minute crop: try BOTH digit-whitelist (cleaner on most
            # fonts) AND open mode (whitelist sometimes drops a digit
            # if it's barely visible — 9→nothing). Letter→digit
            # substitutions are applied during parsing so the open-mode
            # reads "SO+1" → 90+1.
            mt_w = _tess(m_pre, lang="eng", psm=psm, whitelist="0123456789+")
            if mt_w:
                minute_text_candidates.append(mt_w)
            mt_o = _tess(m_pre, lang="eng", psm=psm)
            if mt_o:
                minute_text_candidates.append(mt_o)
            for lang in ("eng", "rus+eng"):
                nt = _tess(n_pre, lang=lang, psm=psm)
                if nt:
                    name_text_candidates.append(nt)

    # Additional pass: contrast-enhanced preprocessing for faded text.
    try:
        m_enh = _enhance_contrast(minute_crop, scale=4)
        n_enh = _enhance_contrast(name_crop, scale=4)
        for psm in (6, 7):
            mt_w = _tess(m_enh, lang="eng", psm=psm, whitelist="0123456789+")
            if mt_w:
                minute_text_candidates.append(mt_w)
            for lang in ("eng", "rus+eng"):
                nt = _tess(n_enh, lang=lang, psm=psm)
                if nt:
                    name_text_candidates.append(nt)
    except Exception:
        pass

    # Pick the best minute. We collect plausible (base, extra) pairs and
    # single-base reads from every candidate, vote on them, and combine
    # the winning base + winning extra.
    #
    # Why this is harder than "just take the largest digit pair":
    #  * the OCR sometimes drops the leading '4' → ``'5+2'`` instead of ``'45+2'``;
    #  * the OCR sometimes reads ``'45+2'`` as ``'45+24'`` / ``'5+2 4'`` and
    #    a max-based vote then leaks the stray '24'/'4' into the result;
    #  * the OCR sometimes reads ``'59'`` as ``'99'`` at one threshold and
    #    ``'59'`` at another — we want the *consensus* across thresholds, not
    #    the maximum (which is the noisy one).
    def _digit_normalise(s: str) -> str:
        # Common letter→digit OCR substitutions for the bold UI font.
        return (
            s.replace("S", "9").replace("s", "9")
             .replace("O", "0").replace("o", "0")
             .replace("B", "8")
             .replace("|", "1").replace("l", "1").replace("I", "1")
             .replace("T", "1").replace("i", "1")
        )

    # Patterns reused for both the canonical 'M+N' and bare 'M' reads.
    pair_re = re.compile(r"(?<!\d)(\d{1,2})\s*\+\s*(\d{1,2})(?!\d)")
    isolated_digit_re = re.compile(r"(?<!\d)(\d{1,2})(?!\d)")
    # Capture the FIRST digit after '+' even when noise follows ('+14' is
    # really '+1' with a stray '4'; '+1T' is '+1' with T misread).
    first_extra_re = re.compile(r"\+\s*(\d)")

    from collections import Counter
    pair_votes: "Counter[tuple[int, int]]" = Counter()
    base_votes: "Counter[int]" = Counter()
    extra_votes: "Counter[int]" = Counter()

    for raw in minute_text_candidates:
        c = _digit_normalise(raw)
        canonical = pair_re.search(c)
        if canonical:
            b = int(canonical.group(1))
            e = int(canonical.group(2))
            if 1 <= b <= 95 and 1 <= e <= 9:
                pair_votes[(b, e)] += 1
                base_votes[b] += 1
                extra_votes[e] += 1
            elif 1 <= b <= 95:
                base_votes[b] += 1
        else:
            # Single-number reads (no '+' anywhere).
            nums = [int(d) for d in isolated_digit_re.findall(c)]
            nums = [n for n in nums if 1 <= n <= 95]
            if nums:
                two_digit = [n for n in nums if n >= 10]
                base = two_digit[0] if two_digit else nums[0]
                base_votes[base] += 1

        # Even when the canonical pair didn't pass, the *first* digit
        # after a '+' is usually the real stoppage extra.
        for m_extra in first_extra_re.finditer(c):
            try:
                e = int(m_extra.group(1))
            except ValueError:
                continue
            if 1 <= e <= 9:
                extra_votes[e] += 1

    minute: Optional[int] = None
    if base_votes:
        # Pick the most-voted base. Tiebreak: prefer 2-digit, then the
        # *smaller* value (OCR errors usually *add* noise, so the smaller
        # consensus reading is the correct one for cases like 59 vs 99).
        def _base_key(b: int) -> tuple[int, int, int]:
            return (base_votes[b], 1 if b >= 10 else 0, -b)

        minute = max(base_votes, key=_base_key)
        if extra_votes and minute >= 30:
            # Pick the most-voted extra (mode), tie-break on smaller value.
            best_extra = max(
                extra_votes,
                key=lambda e: (extra_votes[e], -e),
            )
            if 1 <= best_extra <= 9:
                combined = minute + best_extra
                # Football stoppage caps at ~120; reject anything beyond.
                if combined <= 130:
                    minute = combined

    # Pick the cleanest scorer name. Vote-based scoring (analogous to
    # the minute parser): for each unique cleaned candidate, count how
    # many raw OCR variants produced it, then rank by (no glitch tail,
    # no case shift, occurrence count, more tokens, more capitalised
    # tokens, length ≥ 3 floor, longer name).
    #
    # Voting matters here because a real surname like "Brahim" usually
    # falls out of MANY thresholds + PSMs identically, while a noise
    # read like "CE" or "NY" appears once.  Without the count term the
    # tie-breaker can incorrectly pick a 2-letter junk capture.
    glitch_pat = re.compile(r"[a-zа-яё][A-ZА-ЯЁ]")
    glitch_tail = re.compile(r"^[FRTPГг][O0oОо]?[NnMmAaIiJj]\w?$")

    name_votes: "Counter[str]" = Counter()
    for c in name_text_candidates:
        cleaned = _clean_scorer_name(c)
        if cleaned:
            name_votes[cleaned] += 1

    def _name_score(name: str) -> tuple[int, int, int, int, int, int, int]:
        tokens = name.split()
        n_caps = sum(1 for w in tokens if _is_capitalised_word(w))
        n_glitch_tail = sum(1 for w in tokens if glitch_tail.match(w))
        n_case_shifts = sum(1 for w in tokens if glitch_pat.search(w))
        return (
            -n_glitch_tail,        # zero glitch tail tokens win
            -n_case_shifts,        # zero internal case shifts win
            1 if len(name) >= 3 else 0,   # 3+ char names beat "CE"/"NY"
            name_votes[name],      # more occurrences → consensus
            len(tokens),           # more tokens → cleaner separation
            n_caps,                # more capitalised tokens → cleaner
            len(name),             # longer name wins on full tie
        )

    best_name: Optional[str] = None
    if name_votes:
        best_name = max(name_votes, key=_name_score)

    return minute, best_name


def _parse_goals_panel(img: Image.Image) -> list[dict]:
    """Detect colored goal-event balls on the right pane and OCR each row.

    Returns a list of ``{"name": str|None, "minute": int|None,
    "side": "home"|"away"}`` dicts in chronological order (top-down).
    Empty list when no events are detected (e.g. on a 0-0 match or a
    crop that doesn't contain the panel).
    """
    if not _OCR_AVAILABLE:
        return []
    W, H = img.size
    # Scan band: roughly the right half between the header and bottom button.
    # Same area as REGIONS["goals_panel"] but a touch wider so we don't miss
    # a ball that's just outside the original 0.50–0.65 strip.
    scan_x1, scan_x2 = int(0.49 * W), int(0.66 * W)
    scan_y1, scan_y2 = int(0.18 * H), int(0.88 * H)
    # Sample every other column so we stay fast on big screenshots.
    sample_xs = list(range(scan_x1, scan_x2, 2))

    green_rows: list[int] = []
    blue_rows: list[int] = []
    px = img.load()  # PixelAccess is much faster than getpixel for a tight loop
    for y in range(scan_y1, scan_y2):
        g_count = 0
        b_count = 0
        for x in sample_xs:
            r, g, b = px[x, y]
            if _is_green_ball((r, g, b)):
                g_count += 1
            elif _is_blue_ball((r, g, b)):
                b_count += 1
            if g_count >= 5 and b_count >= 5:
                break
        if g_count >= 5:
            green_rows.append(y)
        if b_count >= 5:
            blue_rows.append(y)

    g_bands = _cluster_rows(green_rows)
    b_bands = _cluster_rows(blue_rows)

    # Real goal balls span 30-45 px (a coloured circle ~28-32 px + a few
    # pixels of soft anti-aliased edge).  Anything below ~14 px is almost
    # always a UI artefact — a "PUBLISH" / "CONTINUE" button glow at the
    # very bottom, a sponsor banner pixel, or a JPEG halo around the
    # blue scoreboard.  Calibrate to image height so phone / tablet
    # screenshots use the same threshold.
    min_band_h = max(14, H // 60)
    events: list[tuple[str, int]] = []
    for y1, y2 in g_bands:
        if y2 - y1 >= min_band_h:
            events.append(("home", (y1 + y2) // 2))
    for y1, y2 in b_bands:
        if y2 - y1 >= min_band_h:
            events.append(("away", (y1 + y2) // 2))
    events.sort(key=lambda e: e[1])  # chronological (top-to-bottom)

    panel_x1 = int(0.49 * W)
    panel_x2 = int(0.95 * W)
    ball_x = int((scan_x1 + scan_x2) / 2)  # ~0.575
    # Adaptive row height: the gap between two adjacent bands sets the
    # visible row height. Default ~50 px.
    row_h = 50
    if len(events) >= 2:
        gaps = [events[i + 1][1] - events[i][1] for i in range(len(events) - 1)]
        row_h = max(40, min(100, min(gaps) - 6))

    out: list[dict] = []
    for side, yc in events:
        minute, name = _ocr_goal_row(
            img, yc, panel_x1, panel_x2, ball_x, row_h
        )
        out.append({"name": name, "minute": minute, "side": side})
    return out


# Whitelist for Latin team names — forces tesseract to commit to digits/letters
# and not flip "88888" into Cyrillic look-alikes ("В", "е") under rus+eng.
_LATIN_TEAM_WHITELIST = (
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "abcdefghijklmnopqrstuvwxyz"
    "0123456789"
    "·-_."
)
# Extended whitelist that includes common Cyrillic characters found in
# FC Mobile nicknames (РД, СПАРТАК, etc.)
_CYRILLIC_TEAM_WHITELIST = (
    "АБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯабвгдеёжзийклмнопрстуфхцчшщъыьэюя"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "abcdefghijklmnopqrstuvwxyz"
    "0123456789"
    "·-_. "
)
# Whitelist for the score region — only digits and known separators.
_SCORE_WHITELIST = "0123456789-:"


def _ocr_region(crop: Image.Image, kind: str) -> tuple[str, list[str]]:
    """
    Try several preprocessing variants + PSMs for a region.
    Returns (best_string, all_candidates) — best is the most plausible match
    given the kind of field we're trying to read.

    Enhanced pipeline: uses multiple preprocessing methods (global threshold,
    adaptive local-mean, contrast-enhanced) to handle the gradient backgrounds
    typical of FC Mobile post-match screens.
    """
    candidates: list[str] = []

    whitelist: str | None = None
    if kind == "score":
        thresholds = (110, 130, 150, 170, 190, 210)
        psms = (6, 7, 8, 11, 13)
        langs = ("eng",)
        whitelist = _SCORE_WHITELIST
    elif kind in ("team1", "team2"):
        # Team names are usually Latin in FC Mobile but can be Cyrillic, so
        # try both. eng-only avoids the "АигаВгоАига" transliteration when the
        # original was Latin. The Latin whitelist also forces digits like
        # "88888" to read as digits rather than Cyrillic look-alikes.
        thresholds = (120, 140, 160, 180, 200)
        psms = (6, 7, 8, 11, 13)
        langs = ("eng", "rus+eng")
        whitelist = _LATIN_TEAM_WHITELIST
    else:                                   # league_plate, team2_sub, goals_panel
        thresholds = (90, 110, 130, 150, 170)
        psms = (6, 7, 11, 13)
        langs = ("rus+eng", "eng")

    # Pass 1: Standard global threshold binarization (original approach).
    for thr in thresholds:
        pre = _binarize_white_text(crop, threshold=thr)
        for psm in psms:
            for lang in langs:
                # For team names, run BOTH whitelisted and unconstrained
                # passes so Cyrillic-named teams ("СПАРТАК") still parse —
                # the whitelist is only there to break ties when digits are
                # involved. The whitelisted pass is appended first so it
                # wins the kind-specific scorer below when both yield the
                # same length.
                if kind in ("team1", "team2"):
                    if whitelist and lang == "eng":
                        wtxt = _tess(pre, lang=lang, psm=psm, whitelist=whitelist)
                        if wtxt:
                            candidates.append(wtxt)
                    # Also try Cyrillic whitelist for rus+eng.
                    if lang == "rus+eng":
                        wtxt = _tess(pre, lang=lang, psm=psm,
                                     whitelist=_CYRILLIC_TEAM_WHITELIST)
                        if wtxt:
                            candidates.append(wtxt)
                    txt = _tess(pre, lang=lang, psm=psm)
                else:
                    txt = _tess(pre, lang=lang, psm=psm, whitelist=whitelist)
                if txt:
                    candidates.append(txt)

    # Pass 2: Adaptive local-mean binarization — better on gradient backgrounds.
    try:
        pre_adaptive = _binarize_adaptive(crop)
        for psm in psms[:3]:  # top-3 PSMs to keep it fast
            for lang in langs:
                if kind in ("team1", "team2"):
                    if whitelist and lang == "eng":
                        wtxt = _tess(pre_adaptive, lang=lang, psm=psm,
                                     whitelist=whitelist)
                        if wtxt:
                            candidates.append(wtxt)
                    txt = _tess(pre_adaptive, lang=lang, psm=psm)
                else:
                    txt = _tess(pre_adaptive, lang=lang, psm=psm,
                                whitelist=whitelist)
                if txt:
                    candidates.append(txt)
    except Exception:
        pass  # numpy not available — skip this pass

    # Pass 3: Contrast-enhanced binarization — rescues faded/low-contrast text.
    try:
        pre_contrast = _enhance_contrast(crop)
        for psm in psms[:3]:
            for lang in langs:
                if kind in ("team1", "team2"):
                    if whitelist and lang == "eng":
                        wtxt = _tess(pre_contrast, lang=lang, psm=psm,
                                     whitelist=whitelist)
                        if wtxt:
                            candidates.append(wtxt)
                    txt = _tess(pre_contrast, lang=lang, psm=psm)
                else:
                    txt = _tess(pre_contrast, lang=lang, psm=psm,
                                whitelist=whitelist)
                if txt:
                    candidates.append(txt)
    except Exception:
        pass  # PIL filters unavailable — skip

    if kind == "score":
        # Score voting: tally every candidate that yields a score-shaped
        # pair (passes _parse_score's clock-vs-score heuristic) and pick
        # the mode.  A single accidental "0:00" reading no longer beats
        # 30 consistent "2-1" reads from other threshold passes.
        from collections import Counter as _Counter
        score_votes: "_Counter[tuple[str, tuple[int, int]]]" = _Counter()
        for c in candidates:
            parsed = _parse_score(c)
            if parsed is not None:
                # Prefer the original raw text that ALSO contained a dash
                # (dashes are unambiguous score separators); tie-break on
                # plain string equality so repeated reads accumulate.
                score_votes[(c, parsed)] += 1
        if score_votes:
            # Highest-voted parsed score wins; among ties prefer dash-
            # separated raw texts (less likely to be a clock leak).
            parsed_totals: "_Counter[tuple[int, int]]" = _Counter()
            for (raw, parsed), v in score_votes.items():
                parsed_totals[parsed] += v
            best_parsed = max(
                parsed_totals,
                key=lambda p: (parsed_totals[p], p != (0, 0)),
            )
            # Find the raw text most associated with the best score.
            best_raw = max(
                (k for k in score_votes if k[1] == best_parsed),
                key=lambda kv: (
                    score_votes[kv],
                    1 if any(d in kv[0] for d in "-—–") else 0,
                ),
            )[0]
            return best_raw, candidates
        for c in candidates:
            if _SCORE_RE.search(c):
                return c, candidates
        return "", candidates

    if kind in ("team1", "team2"):
        # Team-name picker.  Tesseract returns 80–120 candidates per crop
        # across the threshold × PSM × language matrix; the correct
        # nickname usually shows up MANY times verbatim while noise
        # variants ("GRyosuraBrofurasessgs") appear once or twice.  So
        # we first reduce candidates to a "core token" (the longest
        # alphanumeric run plus its immediate -_·. punctuation), vote
        # across these cores, then rank by:
        #   1. score-shape sanity (≥3 letters, no 'ГОЛ'/'ГОЯ' debris)
        #   2. occurrence count (consensus across thresholds)
        #   3. number of digits — football nicks often end in numbers
        #   4. CamelCase chunk count (real nicknames are CamelCase)
        #   5. longer wins on ties
        from collections import Counter as _Counter

        def latin_run(s: str) -> int:
            longest = 0
            cur = 0
            for ch in s:
                if ch.isascii() and (ch.isalnum() or ch in "·-_."):
                    cur += 1
                    longest = max(longest, cur)
                else:
                    cur = 0
            return longest

        def cyrillic_run(s: str) -> int:
            longest = 0
            cur = 0
            for ch in s:
                if ('\u0400' <= ch <= '\u04FF') or ch in "·-_. ":
                    cur += 1
                    longest = max(longest, cur)
                else:
                    cur = 0
            return longest

        def extract_core(s: str) -> str:
            """Strip everything except the first long alnum-with-sep run."""
            best_start = best_end = 0
            cur_start = 0
            cur_len = 0
            longest = 0
            for i, ch in enumerate(s):
                ok = (
                    ('\u0400' <= ch <= '\u04FF')
                    or ch.isascii() and (ch.isalnum() or ch in "·-_.")
                )
                if ok:
                    if cur_len == 0:
                        cur_start = i
                    cur_len += 1
                    if cur_len > longest:
                        longest = cur_len
                        best_start = cur_start
                        best_end = i + 1
                else:
                    cur_len = 0
            return s[best_start:best_end]

        def camel_chunks(s: str) -> int:
            """Count CamelCase / digit chunks (real nicks have 2-4)."""
            chunks = 0
            prev = ""
            for ch in s:
                if ch.isupper() and prev and not prev.isupper():
                    chunks += 1
                elif ch.isdigit() and prev and not prev.isdigit():
                    chunks += 1
                prev = ch
            return chunks

        # Build a tally of cleaned cores.
        cores: list[str] = []
        for c in candidates:
            core = extract_core(c)
            if 3 <= len(core) <= 30:  # ignore single-letter noise & ad-text
                cores.append(core)
        core_votes: "_Counter[str]" = _Counter(cores)

        # Total votes is the denominator for "consensus quality".  A core
        # that shows up in ≥1/3 of all candidates is treated as a strong
        # consensus and wins outright over single-shot longer cores.
        total = sum(core_votes.values()) or 1

        def team_score(s: str) -> tuple[int, int, int, int, int, int, int]:
            lat = latin_run(s)
            cyr = cyrillic_run(s)
            run = max(lat, cyr)
            digits = sum(c.isdigit() for c in s)
            chunks = camel_chunks(s)
            votes = core_votes[s]
            strong_consensus = 1 if votes * 3 >= total else 0
            return (
                strong_consensus,  # ≥1/3 of all readings → win
                votes,             # then by occurrence count
                run,               # then by longest alnum/Cyr run
                digits,            # 'AuraBroAura88888' over 'AuraBroAuraeeeBs'
                chunks,            # CamelCase shape
                cyr,               # prefer Cyrillic-rich when above tie
                -len(s),           # shorter wins on full tie
            )

        if core_votes:
            best = max(core_votes, key=team_score)
            return best, candidates
        # Fallback: longest alnum run regardless of script.
        best = max(candidates, key=_score_candidate, default="")
        return best, candidates

    # default — most "useful" content
    best = max(candidates, key=_score_candidate, default="")
    return best, candidates


def _score_candidate(s: str) -> int:
    """Heuristic: prefer strings with letters/digits and reasonable length."""
    if not s:
        return -1
    alnum = sum(c.isalnum() for c in s)
    return alnum * 10 - max(0, len(s) - 60)


# Used to pick the raw-OCR snippet that most plausibly contains a
# league/tournament plate. Patterns are short Cyrillic/Latin roots so
# they survive common OCR misreads — 'Лига' often comes back as
# 'Лиrа' / 'Лиrа.', 'Гвардиольыча' as 'Гвардиопыча' / 'Гваpдиольыча'.
_LEAGUE_HINT_RE = re.compile(
    r"(?ix)"
    r"(?:"
        r"\bлиг"         # 'Лига' — word boundary blocks 'НЕТ\u202fЛИГИ'
        r"|гварди"       # 'Гвардиол*'
        r"|гваpди"       # OCR variant where Cyrillic 'р' got read as Latin 'p'
        r"|вардиол"      # Captured even when leading 'Г' got OCR'd as '|' / ']'
        r"|guardiol"
        r"|\bvsa\b"
        r"|\bвса\b"
        r"|\bri\b"
        r"|\bри\b"
        r"|реальн"       # 'Реальная игра'
    r")"
)


def _pick_league_plate(*candidates: str) -> Optional[str]:
    """Pick the raw OCR string that most plausibly contains the league
    plate text.

    The 'Лига Гвардиолыча' plate (or 'RI'/'VSA' label) appears on
    whichever side belongs to the away player — sometimes under
    ``team1``, sometimes under ``team2``.

    We accept a candidate **only** when it matches a known league hint
    (``лиг`` / ``гварди`` / ``vsa`` / ``ри`` / ``ri``) — otherwise the
    crop usually contains background banners (e.g. holiday
    "С ДНЁМ ПОБЕДЫ", crowd noise, sponsor logos) that the previous
    "longest non-empty" fallback used to leak through as garbage like
    ``OY |e @ СДНЁМ ПОБЕДЫ`` or ``| у | НЕТЛИГИ``.

    Returns ``None`` when no candidate is plausible — that's the
    correct behaviour for the downstream ``Турнир: —`` UI line, and
    leaves the tournament-type detection to fall back to the caption
    or chat binding.
    """
    cleaned = [c.strip() for c in candidates if c and c.strip()]
    if not cleaned:
        return None
    for c in cleaned:
        if _LEAGUE_HINT_RE.search(c):
            return c
    return None


_VSA_FUZZY_RE = re.compile(
    r"(?ix)"
    r"(?:"
        # Russian league name 'Гвардиолыча' often loses its leading 'Г' to
        # OCR (the small 'Г' gets read as '|' / ']' / '1' / 'г' / 'r').  We
        # match the 'вардиол' root irrespective of the prefix so the
        # detection survives that very common misread.
        r"гварди|guardiol|вардиол|вардiол|вардион"
        r"|\bvsa\b|\bвса\b|\bвcа\b"
        # 'Лига Гв...' anchor — tolerates the missing 'г' and a punctuation
        # glyph in its place ('Лига | вардиол…' / 'Лига ] вардиол…').
        r"|лига\s*[|/\\\]1l].?\s*варди"
    r")"
)
# 'RI' / 'РИ' tournaments are denoted by the "Лига РИ" plate.  A bare
# 'ри' / 'ri' in OCR text is unreliable — Tesseract loves to hallucinate
# stray 'ри' inside Cyrillic gibberish ("a д ри hie") — so we only flag
# RI when 'ри'/'ri' sits in a *league* context: either preceded by some
# form of "лиг" / "liga" within ~25 chars, or as part of an explicit
# 'real…' / 'реальн…' phrase.
_RI_FUZZY_RE = re.compile(
    r"(?ix)"
    r"(?:"
        r"лиг[аи]?\s*[\-:]?\s*ри\b"          # 'Лига РИ', 'Лиги РИ'
        r"|лиг[аи]?\s*[\-:]?\s*ri\b"
        r"|liga\s*[\-:]?\s*ri\b"
        r"|реальн|real\s*игр|real\s*ig"
    r")"
)


def detect_tournament_type(*texts: str) -> Optional[str]:
    """Return 'vsa' or 'ri' based on any of the given texts; None if unsure.

    Order matters: VSA wins on ties because the 'Лига Гвардиолыча' plate
    is the dominant signal, and noisy crops often contain stray ' ri '
    or ' ри ' inside garbled Latin (e.g. 'TfapaZtarinesiy al ri').
    """
    blob = " ".join(t for t in texts if t).lower()
    blob = f" {blob} "
    if _VSA_FUZZY_RE.search(blob):
        return "vsa"
    if any(k in blob for k in VSA_KEYWORDS):
        return "vsa"
    if _RI_FUZZY_RE.search(blob):
        return "ri"
    # RI_KEYWORDS contains the loose ' ri ' / 'ри ' fallbacks — we only
    # take them when there's a 'лиг' / 'liga' anchor nearby; otherwise
    # Tesseract noise tokens like 'a д ри hie' false-positive too easily.
    if any(k in blob for k in RI_KEYWORDS) and re.search(
        r"(?i)лиг|liga", blob
    ):
        return "ri"
    return None


def detect_tournament_type_from_caption(caption: str | None) -> Optional[str]:
    """Manual override: user types 'вса' or 'ри' in the photo caption."""
    if not caption:
        return None
    c = caption.lower()
    # Exact word check to avoid e.g. 'vsani' → 'vsa'
    tokens = re.findall(r"[a-zа-яё]+", c, flags=re.UNICODE)
    if any(t in ("vsa", "вса") for t in tokens):
        return "vsa"
    if any(t in ("ri", "ри") for t in tokens):
        return "ri"
    return None


# ── Public entry point ───────────────────────────────────────────────────────
def parse_match_screenshot(
    image_input,
    ai_models: tuple[str, ...] | list[str] | None = None,
    no_tesseract: bool = False,
) -> MatchScreenshot:
    """
    Parse a match-result screenshot.

    image_input: file path, bytes, or PIL.Image.Image.
    ai_models:    optional explicit ordered chain of OpenRouter model
                  IDs to try (used by the «Другой моделью» retry
                  button to skip already-tried models). None →
                  default ``AI_FALLBACK_MODELS``.
    no_tesseract: if True, skip the tesseract fallback entirely.
                  Used when tournament ocr_mode == 'ai_no_tess'.

    Pipeline:
      1. Try OpenRouter Vision LLM. If it returns all required
         fields → use it. Tesseract is bypassed.
      2. Fall back to local tesseract pipeline (preserves the previous
         behaviour for envs without network / API key).
    """
    if isinstance(image_input, Image.Image):
        img = image_input.convert("RGB")
        with io.BytesIO() as buf:
            img.save(buf, format="JPEG")
            jpg_bytes = buf.getvalue()
    elif isinstance(image_input, (bytes, bytearray)):
        jpg_bytes = bytes(image_input)
        img = Image.open(io.BytesIO(jpg_bytes)).convert("RGB")
    else:
        with open(image_input, "rb") as f:
            jpg_bytes = f.read()
        img = Image.open(io.BytesIO(jpg_bytes)).convert("RGB")

    # ── 1) AI Vision OCR ────────────────────────────────────────────────
    ai_disabled = os.getenv("OCR_PROVIDER", "auto").lower() == "tesseract"
    # ``ai_models == ()`` means "we already exhausted the retry chain",
    # we still want the tesseract fallback to run but no AI call.
    if not ai_disabled and ai_models != ():
        try:
            if ai_is_available():
                ai = ai_read_screenshot(jpg_bytes, models_override=ai_models)
                if (
                    ai
                    and ai.get("score1") is not None
                    and ai.get("score2") is not None
                    and ai.get("team1")
                    and ai.get("team2")
                ):
                    r = MatchScreenshot()
                    r.score1 = ai["score1"]
                    r.score2 = ai["score2"]
                    r.pen1 = ai.get("pen1")
                    r.pen2 = ai.get("pen2")
                    r.team1 = (ai.get("team1") or "").strip() or None
                    r.team2 = (ai.get("team2") or "").strip() or None
                    r.league_plate = (ai.get("league_plate") or "").strip() or None
                    r.goals = list(ai.get("goals") or [])
                    pen_str = (
                        f" ({ai['pen1']}-{ai['pen2']} pen)"
                        if r.pen1 is not None and r.pen2 is not None
                        else ""
                    )
                    r.raw_texts = {
                        "score": f"{ai['score1']}:{ai['score2']}{pen_str}",
                        "team1": r.team1 or "",
                        "team2": r.team2 or "",
                        "league_plate": r.league_plate or "",
                        "_ai_model": ai.get("model", ""),
                        "_ai_raw": ai.get("raw", "")[:500],
                    }
                    r.confidence = {"_ai": 1.0}
                    r.tournament_type = detect_tournament_type(
                        r.league_plate or "",
                        r.team1 or "",
                        r.team2 or "",
                        "",
                    )
                    log.info(
                        "AI OCR ok via %s in %.1fs: %s:%s%s %r vs %r",
                        ai.get("model"), ai.get("elapsed_s", 0.0),
                        r.score1, r.score2,
                        f" pen {r.pen1}:{r.pen2}" if r.has_penalties else "",
                        r.team1, r.team2,
                    )
                    return r
                log.info("AI OCR returned partial/None result, falling back to tesseract")
        except Exception as e:
            log.warning("AI OCR error, falling back to tesseract: %r", e)

    # ── 2) Tesseract fallback ───────────────────────────────────────────
    if no_tesseract:
        log.info("Tesseract fallback skipped (no_tesseract=True)")
        return MatchScreenshot()

    result = MatchScreenshot()

    if not _OCR_AVAILABLE:
        log.warning("pytesseract not available; cannot OCR screenshots.")
        return result

    raw: dict[str, str] = {}
    # Keep ALL OCR candidates per region too — the "best" picker prefers
    # high-alnum Latin reads (e.g. ``"Jivrafpapanensiva"``) over the
    # correct-but-shorter Cyrillic read (``"Лига Гвардиолыча"``), so we
    # need every read available to find a league hint.
    all_candidates: dict[str, list[str]] = {}
    for name, box in REGIONS.items():
        crop = _crop_region(img, box)
        best, _all = _ocr_region(crop, kind=name)
        raw[name] = best
        all_candidates[name] = _all
    result.raw_texts = raw

    # Score
    score = _parse_score(raw.get("score", ""))
    if score:
        result.score1, result.score2 = score

    # Team names
    result.team1 = _clean_team_name(raw.get("team1"))
    result.team2 = _clean_team_name(raw.get("team2"))

    # League plate. The "Лига Гвардиолыча" plate sits on whichever side
    # belongs to the away player — sometimes left (under team1), sometimes
    # right (under team2). Scan EVERY raw OCR candidate from the
    # league/sub-line regions and pick the first one that matches a
    # known league hint (лиг/гварди/vsa/ri).
    league_candidates: list[str] = []
    for region_name in ("league_plate", "team2_sub", "team1_sub", "team2", "team1"):
        league_candidates.extend(all_candidates.get(region_name, []))
    # Also include the "best" reads (they may differ from the candidate
    # list when _ocr_region post-processed them).
    for region_name in ("league_plate", "team2_sub", "team1_sub"):
        if raw.get(region_name):
            league_candidates.append(raw[region_name])
    result.league_plate = _pick_league_plate(*league_candidates)

    # Tournament type — feed the picked league_plate plus EVERY raw OCR
    # candidate from the header band.  Scanning all candidates is what
    # rescues 'Лига Гвардиолыча' on crops where the "best" pick was a
    # Latin glitch (e.g. "TfapaZtarinesiy al ri") that would otherwise
    # match the RI_KEYWORDS ' ri ' anchor and mis-classify a vsa match.
    tournament_blobs: list[str] = [
        result.league_plate or "",
    ]
    for region_name in (
        "league_plate", "team2_sub", "team1_sub",
        "team2", "team1",
    ):
        if raw.get(region_name):
            tournament_blobs.append(raw[region_name])
        tournament_blobs.extend(all_candidates.get(region_name, []))
    result.tournament_type = detect_tournament_type(*tournament_blobs)

    # Goal events (right pane). Detected via colored ball icons:
    # green = home goal, blue = away goal. Each row's minute + scorer name
    # is then OCR'd separately, which is far more reliable than parsing
    # the whole panel as one block.
    try:
        result.goals = _parse_goals_panel(img)
    except Exception:                         # pragma: no cover
        log.exception("goals-panel parsing failed (non-fatal)")
        result.goals = []

    return result
