"""
Parse Telegram channel export (result.json) of @gvardiolPlay
into a structured tournament-champions JSON.

This is a *best-effort* parser. Posts in the channel use free-form Russian text
with metaphors, nicknames and varying templates over time. The script:

  1. Filters out obvious non-winner posts (ads, real-football news, announcements).
  2. Classifies each candidate into one of: ``main`` / ``fantasy`` / ``vsa``.
  3. Extracts winner (and runner-up, score, championship count) where it can.
  4. Writes everything (including the raw text and a confidence label) so that
     the result can be manually reviewed before being imported into the bot DB.

Outputs (under ``data/``):
  - ``champions_parsed.json``   parsed tournament records, one per post
  - ``skipped_posts.json``      candidates that triggered keywords but were filtered out
                                (with the reason — useful for tuning)
  - ``aliases_to_review.json``  unique winner / runner-up names that did not come
                                with an ``@username`` and will need a manual alias
                                mapping before DB import

Usage:
  python scripts/parse_gvardiol_dump.py
  python scripts/parse_gvardiol_dump.py --dump result.json --out-dir data
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# --------------------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------------------

CHANNEL_USERNAME = "gvardiolPlay"
POST_URL_TEMPLATE = "https://t.me/{username}/{msg_id}"

# Look at the first N characters of the post to determine its tournament type
CLASSIFY_HEAD_CHARS = 250


# --------------------------------------------------------------------------------------
# Tournament-type classification
# --------------------------------------------------------------------------------------

def classify_tournament_type(text: str) -> str:
    head = text[:CLASSIFY_HEAD_CHARS].lower()
    if re.search(r"\bvsa\b", head):
        return "vsa"
    # Secondary cups — Суперкубок (post-tournament champions cup),
    # LG CUP, мини-кубок. They run alongside the main tournament and
    # crown a separate winner; expose them as their own bucket so they
    # don't inflate main-tournament trophy counts but are still
    # browsable in /champions.
    if re.search(r"победитель\s+суперкубка|обладатель\s+суперкубка|\blg\s+cup\b|мини[-\s]кубк", head):
        return "supercup"
    if any(k in head for k in ("фэнтези", "фентези", "фэнтэзи", "fantasy", "лиги чемпионов")):
        # the basic Гвардиолыч tournament is sometimes called "Лига Чемпионшипа"
        # but never "Лиги Чемпионов" — so this works as a fantasy-LC marker.
        if "лига чемпионшипа" in head:
            return "main"
        return "fantasy"
    # "АПЛ" only with explicit word boundaries — otherwise we hit
    # innocent words like "Аплодисменты" and mis-classify a main-tournament
    # winner post as fantasy.
    if re.search(r"\bапл\b", head):
        return "fantasy"
    return "main"


# --------------------------------------------------------------------------------------
# Negative signals — these posts are NOT winner announcements
# --------------------------------------------------------------------------------------

NEGATIVE_PATTERNS: List[Tuple[str, str]] = [
    # Full-pitch BEASTS FC SHOP ads — only the dedicated "магазин-бот"
    # phrase combined with the brand name is unambiguous. The bare bot
    # handle (`@beastsfcshop_bot`) and the standalone "BEASTS FC SHOP"
    # mention also appear in real winner posts that credit the sponsor
    # at the end (e.g. [401], [420]) so don't kill those.
    (r"магазин-бот\s+beasts\s*fc\s*shop", "ad: BEASTS FC SHOP pitch"),
    (r"если\s+ты\s+покупаешь\s+абонемент", "ad: subscription pitch"),
    (r"приз\s+получит\s+@", "sponsorship prize giveaway"),
    (r"приз\s*[—\-–]\s*абонемент", "raffle / prize giveaway, not a tournament"),
    # Posts that announce the NEXT tournament's prize while only
    # mentioning the previous champion in passing (e.g. [403]).
    (r"в\s+этом\s+турнире\s+мы\s+подарим", "future-tournament prize announcement"),
    (r"тому\s+кто\s+победит", "future-tournament prize announcement"),
    (r"победитель\s+нашего\s+\d+\s+турнира\s+гвардиолыча\s+получит\s+приз", "future-tournament prize announcement"),
    (r"\bмесси\b.*?(?:днём\s+рождения|с\s+днём)", "Messi birthday post"),
    (r"лионель\s+месси", "Messi-related post"),
    (r"манчестер\s+сити.*?(?:обладатель|кубка)", "real football news"),
    (r"кристал\s+пэлас", "real football news (Crystal Palace)"),
    (r"кубка\s+англии", "real football news (FA Cup)"),
    (r"финалисты\s+известны.*?ставки\s+сделаны", "pre-final announcement"),
    (r"\bанонс\b.*?(?:уже\s+совсем\s+скоро|🚀)", "tournament announcement"),
    (r"возвращаемся\s+к\s+сборным", "general post about national teams"),
    # Pre-final announcement: "Уже совсем скоро мы узнаем победителя N турнира"
    (r"уже\s+совсем\s+скоро\s+мы\s+узнаем\s+победителя", "pre-final announcement (winner not yet known)"),
    # Post about a national-teams sub-tournament (different format, user wants only main)
    (r"победой\s+сборной\s+\w+", "national teams sub-tournament"),
    (r"победой\s+в\s+розыгрыше", "raffle / prize giveaway, not a tournament"),
    # Mid-tournament progress posts (semifinal commentary, not final)
    (r"совершил\s+настоящий\s+подвиг.*?в\s+полуфинал", "semifinal progress post"),
    (r"обеспечил\s+себе\s+место\s+в\s+полуфинал", "semifinal progress post"),
    (r"\bвнимание\s+будет\s+приковано\s+к", "tournament status update"),
    # Sub-tournaments outside main+fantasy+vsa.
    # NOTE: Суперкубок / LG CUP / мини-кубок are NOT skipped any more —
    # they are now classified as their own ``supercup`` tournament type.
    # See ``classify_tournament_type`` above.
    # Champions League / inter-league commentary, not a tournament finale.
    (r"плей-офф\s+наш", "inter-league CL commentary"),
    (r"общий\s+счёт\s*[—\-–]?\s*\d+\s*:\s*\d+\s+в\s+пользу", "inter-league CL match commentary"),
    # In-progress status updates that mention a champion in passing.
    (r"уже\s+известен\s+победитель\s+последнего\s+турнира", "between-tournament status post"),
    (r"осталось\s+(?:всего\s+)?несколько\s+игр\s+до\s+окончания", "in-progress status update"),
    (r"пока\s+в\s+плей-?офф", "in-progress status update"),
    (r"турнир\s+окончен,\s+победитель\s+известен\s+и\s+поздравлен", "between-tournament status post"),
    (r"осталось\s+буквально\s+совсем\s+ничего", "in-progress status update"),
    # "Мы начнём новый набор уже на Юбилейный 50-й турнир!" / signup-phase
    # posts that mention an upcoming jubilee number but no actual winner.
    (r"начн[её]м\s+новый\s+набор", "signup announcement, not a winner"),
    (r"новый\s+набор\s+уже\s+на\s+юбилейный", "signup announcement, not a winner"),
    (r"^[^\n]{0,200}только\s+начинаются", "in-progress status update"),
]

# Words that are NOT names (filter out spurious captures)
_NOT_A_NAME = frozenset({
    "он", "она", "новый", "теперь", "этот", "его", "наш", "снова", "сегодня",
    "победителя", "чемпиона", "финалиста", "победитель", "чемпион", "финалист",
    "поздравляем", "победой", "трофеем", "кубком", "опытный", "имя",
    # Russian function words / generic captures that sometimes leak from regex
    "который", "которого", "которому", "котого", "кто", "что", "тот", "та",
    "соперник", "соперника", "сопернику", "опытного", "опытному",
    "также", "конечно", "самый", "именно", "например",
    "вновь", "опять",
})


def negative_reason(text: str) -> Optional[str]:
    low = text.lower()
    for pattern, reason in NEGATIVE_PATTERNS:
        if re.search(pattern, low, re.DOTALL):
            return reason
    return None


# --------------------------------------------------------------------------------------
# Positive signals — keywords that suggest a winner announcement
# --------------------------------------------------------------------------------------

POSITIVE_SIGNALS = [
    r"\bкратн\w+\s+чемпион\w*",         # any case form: чемпион/чемпиона/чемпиону
    r"\bновый\s+чемпион\b",
    r"\b(?:снова|вновь|опять)\s+чемпион\b",
    r"\bвпервые\s+чемпион\b",
    r"\bсамый\s+быстрый\s+чемпион\b",
    r"снова\s+поднял\s+трофей",
    r"поднимает\s+трофей",
    r"забрал\s+трофей",
    r"завоевал\s+(?:трофей|кубок|титул)",
    r"взял\s+\d+",                       # "взял 10-й титул", "взял 8 кубков"
    r"с\s+\w+\s+титулом",                # "с восьмым титулом" / "с 8-м титулом"
    r"новый\s+рекорд\s+установлен",
    r"чемпион\s+среди\s+чемпионов",      # "Чемпион среди чемпионов — Имя"
    r"живая\s+легенда",                  # "Demidrol – живая легенда"
    r"наконец-то\s+он\s+смог",           # post about first-time winner
    r"\bновый\s+герой\b",
    r"поздравляем\s+(?:нового\s+|с\s+)?(?:победител|чемпион)",
    r"поздравляем\s+с\s+(?:[\w]+\s+)?победой",   # [270]: "поздравляем с победой @user"
    r"поздравляем\s+с\s+(?:дублем|трэблом|треблом|квадруплом|пентой)",  # [420] back-to-back winner
    r"поздравляем\s+@\w+\s*[!\.\s🏆🌸🎉]",         # "Поздравляем @user 🏆"
    # ``has_positive_signal`` lower-cases the text before scanning, so
    # capital-letter classes like ``[А-ЯA-Z]`` never match. Use the
    # full-range ``[a-zа-яё]`` here — case-distinction was a misleading
    # safeguard anyway since "Поздравляем" + Имя is a strong enough
    # signal even when the next word is lower-case.
    r"поздравляем\s+[a-zа-яё]\w+\s*[!\.]",         # "Поздравляем Freshl!"
    r"поздравляем\s+[a-zа-яё]\w+\s*[—–\-]",       # "Поздравляем Whitesoho — нового чемпиона"
    r"победител\w+\s+(?:нашего\s+\d*\s*турнира|турнира\s+гвардиолыча|\d+\s+турнира|с\s+отличной)",
    r"кратн\w+\s+обладатель",
    r"очередн\w+\s+(?:победой|трофей|кубок)",
    r"юбилейн\w+\s+победа",
    r"юбилейн\w+\s*,?\s*\d+[\-\u2013\u2014][йя]?\s+турнир",  # "Юбилейный, 40-й турнир"
    r"идеальн\w+\s+кубком",
    r"новый\s+король\s+турнира",
    r"взошедш\w+\s+на\s+вершину",
    r"взял\s+(?:титул|чемпионство|не\s+дешманский\s+кубок|кубок|трофей)",
    r"поднял\s+(?:первый\s+)?(?:трофей|кубок)",
    r"первый\s+турнир\s*[—\-–]\s*и\s+сразу\s+титул",
    # Numeric kratny words
    r"трёхкратн", r"двухкратн", r"двукратн", r"четырёхкратн",
    r"четырехкратн", r"пятикратн", r"шестикратн",
    r"семикратн", r"восьмикратн", r"девятикратн",
    r"десятикратн", r"одиннадцатикратн", r"двенадцатикратн",
    r"\d{1,2}[\-\u2013\u2014\s]?кратн",
    # Fantasy-specific
    r"бронзовый\s+призёр",
    r"серебряный\s+мальчик",
    r"кубок\s+фэнтези",
    r"победу\s+в\s+кубке\s+фэнтези",
    # VSA-specific
    r"турнир\s+по\s+vsa",
    # Supercup-family (Суперкубок / LG CUP / Мини-кубок)
    r"победитель\s+суперкубка",
    r"обладатель\s+суперкубка",
    r"\blg\s+cup\b",
    r"мини[-\s]кубк",
    r"первый\s+победитель\s+мини",
    # "Подводим итоги юбилейного турнира" (50th tournament finale, [1147])
    r"подводим\s+итоги\s+(?:юбилейного\s+)?турнира",
    r"призовые\s+места\s*:",
]


def has_positive_signal(text: str) -> bool:
    low = text.lower()
    return any(re.search(p, low) for p in POSITIVE_SIGNALS)


# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------

def text_of(t: Any) -> str:
    """Telegram dumps put text either as a plain string or as a list of fragments."""
    if isinstance(t, list):
        return "".join(s if isinstance(s, str) else s.get("text", "") for s in t)
    return t or ""


def normalize_dashes(s: str) -> str:
    return s.replace("—", "-").replace("–", "-").replace("−", "-")


_EMOJI_TRAIL_RE = re.compile(
    r"[\U0001F300-\U0001FAFF\U00002600-\U000027BF\u200d\ufe0f]+",
    flags=re.UNICODE,
)


def strip_trailing_emojis(s: str) -> str:
    return _EMOJI_TRAIL_RE.sub("", s).strip()


def clean_name(name: Optional[str]) -> Optional[str]:
    if name is None:
        return None
    name = strip_trailing_emojis(name).strip()
    name = re.sub(r"[!\.,;:?'\"]+$", "", name).strip()
    name = re.sub(r"\s+", " ", name)
    # Reject obviously bogus captures
    if len(name) < 2 or len(name) > 50:
        return None
    if name.lower() in _NOT_A_NAME:
        return None
    return name


def split_name_alias(raw: str) -> Tuple[Optional[str], Optional[str]]:
    """``"Антон (Lokomotive)"`` -> ``("Антон", "Lokomotive")``."""
    if not raw:
        return None, None
    m = re.match(r"^(.+?)\s*\(([^)]+)\)\s*$", raw.strip())
    if m:
        return clean_name(m.group(1)), clean_name(m.group(2))
    return clean_name(raw), None


# --------------------------------------------------------------------------------------
# Championship count
# --------------------------------------------------------------------------------------

KRATNYI_WORDS = {
    "однократный": 1,
    "двукратный": 2, "двухкратный": 2,
    "трёхкратный": 3, "трехкратный": 3, "трёхкратного": 3, "трехкратного": 3,
    "четырёхкратный": 4, "четырехкратный": 4,
    "пятикратный": 5,
    "шестикратный": 6,
    "семикратный": 7,
    "восьмикратный": 8,
    "девятикратный": 9,
    "десятикратный": 10, "десятая": 10,
    "одиннадцатикратный": 11,
    "двенадцатикратный": 12,
}


def parse_championship_count(text: str) -> Optional[int]:
    low = text.lower()
    # Numeric: "8-кратный" / "11-кратный" / "9-й титул" / "11 трофеев"
    m = re.search(r"(\d{1,2})[\s\u00A0\-\u2013\u2014]*кратн", low)
    if m:
        try:
            n = int(m.group(1))
            if 1 <= n <= 99:
                return n
        except ValueError:
            pass
    m = re.search(r"(\d{1,2})\s*[\-\u2013\u2014]?(?:й|го)?\s*титул", low)
    if m:
        try:
            n = int(m.group(1))
            if 1 <= n <= 99:
                return n
        except ValueError:
            pass
    m = re.search(r"теперь\s+это\s+(\d{1,2})\s+трофе", low)
    if m:
        return int(m.group(1))
    m = re.search(r"\bсейчас\s+это\s+(\d{1,2})\s+трофе", low)
    if m:
        return int(m.group(1))
    # Written words
    for word, n in KRATNYI_WORDS.items():
        if word in low:
            return n
    # "Новый чемпион" without a counter = first title
    if re.search(r"\bновый\s+чемпион", low) or re.search(r"первый\s+трофей", low):
        return 1
    if re.search(r"\bпервый\s+титул", low):
        return 1
    return None


# --------------------------------------------------------------------------------------
# Score
# --------------------------------------------------------------------------------------

SCORE_RE = re.compile(
    r"(?:со\s+счёт?ом|победив\s+со\s+счёт?ом)\s+(\d{1,2})\s*[:\-]\s*(\d{1,2})",
    re.IGNORECASE,
)


def parse_score(text: str) -> Optional[str]:
    m = SCORE_RE.search(text)
    if not m:
        return None
    return f"{m.group(1)}:{m.group(2)}"


# --------------------------------------------------------------------------------------
# Tournament number (only for "main" tournament posts)
# --------------------------------------------------------------------------------------

def parse_tournament_number(text: str) -> Optional[int]:
    m = re.search(r"#\s*(\d{1,3})", text)
    if m:
        return int(m.group(1))
    m = re.search(r"\b(\d{1,3})\s+турнира\b", text, re.IGNORECASE)
    if m:
        n = int(m.group(1))
        if 1 <= n <= 999:
            return n
    return None


# --------------------------------------------------------------------------------------
# Username & winner / runner-up extraction
# --------------------------------------------------------------------------------------

USERNAME_RE = re.compile(r"@([A-Za-z][A-Za-z0-9_]{2,32})")

# Single-word name (no spaces, no parens)
_NAME_TOKEN = r"[A-Za-zА-Яа-яЁё][A-Za-zА-Яа-яЁё0-9_'\.]{1,30}"
# Single-word name + optional " (alias)" suffix — only for places where the
# corpus actually uses parenthesised aliases like "Антон (Lokomotive)"
_NAME_WITH_OPT_ALIAS = r"[A-Za-zА-Яа-яЁё][A-Za-zА-Яа-яЁё0-9_'\.]{1,30}(?:\s*\([^)]{1,30}\))?"

# Cyrillic word root for "X-кратный" — must cover BOTH "двукратный" and "двухкратный"
# (real channel uses both spellings).
_KRATNYI_ROOT = (
    r"(?:дву|двух|тре|трё|трех|трёх|четырёх|четырех|"
    r"пяти|шести|семи|восьми|девяти|десяти|"
    r"одиннадцати|двенадцати)кратн\w*"
)
# Numeric form: "8-кратный", "11 кратный", "7️⃣-кратный" (with VS+keycap codepoints)
_KRATNYI_NUMERIC = r"\d{1,2}[\s\u00A0\-\u2013\u2014\ufe0f\u20e3]*кратн\w*"
_KRATNYI_ANY = rf"(?:{_KRATNYI_ROOT}|{_KRATNYI_NUMERIC})"
_DASH = r"[\-\u2013\u2014]"

# Each pattern: (compiled regex, "username" if group 1 is the username, else "name")
WINNER_PATTERNS: List[Tuple[re.Pattern, str]] = [
    # ---- USERNAME-based patterns (highest precision) ----

    # "Поздравляем @user [...] чемпиона/победителя"
    (re.compile(rf"поздравляем\s+@({_NAME_TOKEN})", re.IGNORECASE), "username"),

    # "Поздравляем с (прекрасной/очередной/отличной) победой @user"
    (re.compile(rf"поздравляем\s+с\s+(?:[\w]+\s+)?победой\s+@({_NAME_TOKEN})", re.IGNORECASE), "username"),

    # "И мы поздравляем победителя 17 турнира Гвардиолыча с отличной победой 🥳 @user"
    (re.compile(rf"поздравляем\s+победителя[^@\n]{{0,80}}@({_NAME_TOKEN})", re.IGNORECASE), "username"),

    # ---- "Поздравляем нового X-кратного чемпиона N турнира - Имя" ----

    (re.compile(
        rf"поздравляем\s+(?:нового\s+)?{_KRATNYI_ROOT[:-3]}о?го?\s+чемпиона[^\-—–\n]*?{_DASH}\s+({_NAME_TOKEN})",
        re.IGNORECASE,
    ), "name"),
    (re.compile(
        rf"поздравляем\s+нового\s+победителя[^\-—–\n]*?{_DASH}\s*({_NAME_TOKEN})",
        re.IGNORECASE,
    ), "name"),
    (re.compile(
        rf"поздравляем\s+победителя[^\-—–\n@]*?{_DASH}\s*({_NAME_TOKEN})",
        re.IGNORECASE,
    ), "name"),

    # ---- Header-of-post: "Имя — X-кратный чемпион" ----

    # "Имя — X-кратный чемпион"  /  "Имя - X-кратный чемпион"  /  "Имя – X-кратный чемпион"
    (re.compile(
        rf"(?m)^\s*({_NAME_WITH_OPT_ALIAS})\s*{_DASH}\s*(?:теперь\s+уже\s+)?{_KRATNYI_ANY}\s+чемпион",
        re.IGNORECASE,
    ), "name"),

    # "Имя – снова чемпион"
    (re.compile(
        rf"(?m)^\s*({_NAME_WITH_OPT_ALIAS})\s*{_DASH}\s*(?:снова|вновь)\s+чемпион",
        re.IGNORECASE,
    ), "name"),

    # "Имя снова чемпион" (no dash, e.g. "Dron4ik снова чемпион!")
    (re.compile(
        rf"(?m)^\s*({_NAME_TOKEN})\s+(?:снова|вновь)\s+чемпион",
        re.IGNORECASE,
    ), "name"),

    # ---- "Новый чемпион — Имя!" ----

    (re.compile(
        rf"новый\s+чемпион\s*{_DASH}\s*({_NAME_WITH_OPT_ALIAS}?)\s*[!\.\n]",
        re.IGNORECASE,
    ), "name"),

    # "Новый трёхкратный чемпион — Имя"
    (re.compile(
        rf"новый\s+{_KRATNYI_ANY}\s+чемпион\s*{_DASH}\s*({_NAME_WITH_OPT_ALIAS}?)\s*[!\.\n]",
        re.IGNORECASE,
    ), "name"),

    # ---- "X-кратный чемпион — Имя!" (Demidrol-style) ----

    (re.compile(
        rf"^\s*{_KRATNYI_ANY}\s+чемпион\s*{_DASH}\s*({_NAME_WITH_OPT_ALIAS}?)\s*[!\.\n]",
        re.IGNORECASE | re.MULTILINE,
    ), "name"),

    # "Поздравляем — X-кратный чемпион Имя!"
    (re.compile(
        rf"поздравляем\s*{_DASH}\s*{_KRATNYI_ANY}\s+чемпион\s+({_NAME_WITH_OPT_ALIAS}?)\s*[!\.\n]",
        re.IGNORECASE,
    ), "name"),

    # ---- "И снова он — Имя!" ----

    (re.compile(
        rf"и\s+снова\s+он\s*{_DASH}\s*({_NAME_TOKEN})\s*[!\.]",
        re.IGNORECASE,
    ), "name"),

    # "Легенда вернулась — Имя (alias) снова чемпион"
    (re.compile(
        rf"легенда\s+верн\w+\s*{_DASH}\s*({_NAME_WITH_OPT_ALIAS}?)\s+(?:снова|вновь)\s+чемпион",
        re.IGNORECASE,
    ), "name"),

    # ---- "Юбилейная победа Имя (alias)!" ----

    (re.compile(
        rf"(?:юбилейная|десятая|одиннадцатая|двенадцатая)[^!\n]*?победа\s+({_NAME_WITH_OPT_ALIAS}?)\s*[!\.]",
        re.IGNORECASE,
    ), "name"),

    # ---- "никто иной, как Имя"  /  "...как Имя" (VSA first-winner-style) ----

    (re.compile(
        rf"(?:никто\s+иной[^,\n]*,\s+)?как\s+({_NAME_TOKEN}(?:\s+{_NAME_TOKEN})?)\s*[\U0001F300-\U0001FAFF\n!\.]",
        re.IGNORECASE,
    ), "name"),

    # ---- "Фрешл вновь доказал ..." / "Юра доказал ..." ----

    (re.compile(
        rf"(?m)^\s*({_NAME_TOKEN})\s+(?:вновь|снова)\s+доказал",
        re.IGNORECASE,
    ), "name"),

    # ---- "он же r1f — четырёхкратный чемпион" ----

    (re.compile(
        rf"он\s+же\s+({_NAME_TOKEN})\s*{_DASH}\s*{_KRATNYI_ANY}\s+чемпион",
        re.IGNORECASE,
    ), "name"),

    # ---- "Поздравляем Имя!" + emoji ----

    (re.compile(
        rf"поздравляем\s+({_NAME_TOKEN})\s*[!\.]\s*[\U0001F300-\U0001FAFF]",
        re.IGNORECASE,
    ), "name"),

    # ---- "Его зовут Имя." (rare reveal) ----

    (re.compile(
        rf"его\s+зовут\s+({_NAME_TOKEN})",
        re.IGNORECASE,
    ), "name"),

    # ---- "Имя — живая легенда!" / "Имя (alias) – живая легенда!" ----

    (re.compile(
        rf"(?m)^\s*({_NAME_WITH_OPT_ALIAS})\s*{_DASH}\s*живая\s+легенда",
        re.IGNORECASE,
    ), "name"),

    # ---- "Имя — с восьмым титулом!" ----

    (re.compile(
        rf"(?m)^\s*({_NAME_WITH_OPT_ALIAS})\s*{_DASH}\s*с\s+\w+\s+титулом",
        re.IGNORECASE,
    ), "name"),

    # ---- "Имя, он же Alias — впервые чемпион!" ----

    (re.compile(
        rf"(?m)^\s*({_NAME_TOKEN}),?\s+он\s+же\s+{_NAME_TOKEN}\s*{_DASH}\s*впервые\s+чемпион",
        re.IGNORECASE,
    ), "name"),

    # ---- "Чемпион среди чемпионов — Имя!" ----

    (re.compile(
        rf"чемпион\s+среди\s+чемпионов\s*{_DASH}\s*({_NAME_TOKEN})\s*[!\.\n]",
        re.IGNORECASE,
    ), "name"),

    # ---- "Снова он. Снова Имя." (record-breaker post) ----

    (re.compile(
        rf"снова\s+он\.\s*снова\s+({_NAME_TOKEN})\s*\.",
        re.IGNORECASE,
    ), "name"),

    # ---- "Имя взял N-й титул." (e.g. "Freshl взял 10-й титул.") ----

    (re.compile(
        rf"(?m)^\s*({_NAME_TOKEN})\s+взял\s+\d+(?:[\-\u2013\u2014][йя])?\s*титул",
        re.IGNORECASE,
    ), "name"),

    # ---- "наконец-то он смог!\n\nИмя ждал ..." (first-time winner post) ----

    (re.compile(
        rf"наконец-то\s+он\s+смог[!\.\s🏆🎉]*\n+\s*({_NAME_TOKEN})\s+",
        re.IGNORECASE,
    ), "name"),

    # ---- "Поздравляем Имя — нового чемпиона!" ----

    (re.compile(
        rf"поздравляем\s+({_NAME_TOKEN})\s*{_DASH}\s*нового\s+чемпион",
        re.IGNORECASE,
    ), "name"),

    # ---- Supercup family ----

    # "Победитель Суперкубка — Имя!" / "Победитель Суперкубка — Имя (alias)!"
    (re.compile(
        rf"победитель\s+суперкубка\s*{_DASH}\s*({_NAME_WITH_OPT_ALIAS})",
        re.IGNORECASE,
    ), "name"),

    # "Имя — is a two-time winner LG CUP!" — name in front, English suffix.
    (re.compile(
        rf"(?m)^\s*({_NAME_TOKEN})\s*{_DASH}\s*is\s+a\s+\S+\s*winner\s+lg\s+cup",
        re.IGNORECASE,
    ), "name"),

    # "Первый победитель мини-кубка — Имя!"
    (re.compile(
        rf"(?:первый\s+)?победитель\s+мини[-\s]кубка\s*{_DASH}\s*({_NAME_TOKEN})",
        re.IGNORECASE,
    ), "name"),

    # ---- "ПОДВОДИМ ИТОГИ ... Призовые места: 1. Имя" ([1147]) ----

    (re.compile(
        rf"призовые\s+места\s*:\s*\n+\s*1\s*\.\s*({_NAME_TOKEN})",
        re.IGNORECASE,
    ), "name"),

    # ---- Last-resort fallbacks (anchored to absolute start of post) ----

    # "Поздравляем нового X-кратного чемпиона N турнира\n+Имя"
    (re.compile(
        rf"\Aпоздравляем\s+(?:нового\s+)?(?:дву|двух|трёх|трех|четырёх|четырех|пяти|шести|семи|восьми|девяти|десяти|одиннадцати)?кратного\s+чемпиона[^@\n]*?\n+\s*({_NAME_TOKEN})\b",
        re.IGNORECASE,
    ), "name"),

    # "Поздравляем победителя N турнира [...]\n+Имя"
    (re.compile(
        rf"\Aпоздравляем\s+(?:нашего\s+)?победителя[^@\n]*?\n+\s*({_NAME_TOKEN})\b",
        re.IGNORECASE,
    ), "name"),

    # First-word-of-post + "X-кратный чемпион" within 150 chars
    # (covers "Фрешл вновь доказал своё величие! 🏆🔥 Шестикратный чемпион ...")
    # Anchored at \A so we don't grab names from later paragraphs.
    (re.compile(
        rf"\A({_NAME_TOKEN})[^\n]{{0,200}}?{_KRATNYI_ANY}\s+чемпион",
        re.IGNORECASE,
    ), "name"),
]


def extract_winner(text: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """
    Returns ``(raw_name, username, alias)``.

    ``raw_name`` is what appeared in the post (without ``@``); ``alias`` is the
    optional parenthesised second name (``"Антон (Lokomotive)"`` -> ``alias="Lokomotive"``).
    Either ``username`` or ``raw_name`` is set (sometimes both are absent).
    """
    for rx, kind in WINNER_PATTERNS:
        m = rx.search(text)
        if not m:
            continue
        captured = m.group(1).strip()
        if kind == "username":
            return None, captured, None
        name, alias = split_name_alias(captured)
        if not name:
            continue
        return name, None, alias
    # Fallback: first @username in the head, but only if the head has positive signal
    head = text[:300]
    if any(re.search(p, head, re.IGNORECASE) for p in POSITIVE_SIGNALS):
        m = USERNAME_RE.search(head)
        if m:
            return None, m.group(1), None
    return None, None, None


# --------------------------------------------------------------------------------------
# Runner-up
# --------------------------------------------------------------------------------------

RUNNER_UP_PATTERNS: List[Tuple[re.Pattern, str]] = [
    # "финалиста @user" / "благодарим финалиста @user"
    (re.compile(rf"финалиста?\s+@({_NAME_TOKEN})", re.IGNORECASE), "username"),

    # "о финалисте — Имя" / "не забываем и о финалисте — Имя"
    (re.compile(
        rf"о\s+финалисте\s*{_DASH}\s*({_NAME_WITH_OPT_ALIAS})",
        re.IGNORECASE,
    ), "name"),

    # "Финал против @user" / "Финал против Имя"
    (re.compile(rf"финал\s+против\s+@({_NAME_TOKEN})", re.IGNORECASE), "username"),
    (re.compile(rf"финал\s+против\s+({_NAME_WITH_OPT_ALIAS})\b", re.IGNORECASE), "name"),

    # "победа в финале против @user"
    (re.compile(rf"победа\s+в\s+финале\s+против\s+@({_NAME_TOKEN})", re.IGNORECASE), "username"),
    (re.compile(rf"победа\s+в\s+финале\s+против\s+({_NAME_WITH_OPT_ALIAS})\b", re.IGNORECASE), "name"),

    # "В финале был обыгран @user" / "В финале он не оставил шансов @user" etc.
    (re.compile(
        rf"в\s+финале\s+(?:был\s+|он\s+)?(?:обыгран|повержен|побеждён|обыграл|одолел|пал|разгром\w+|уничтожил|не\s+оставил\s+шансов)[^@\n]{{0,80}}@({_NAME_TOKEN})",
        re.IGNORECASE | re.DOTALL,
    ), "username"),

    # "В финале был обыгран Имя" (no @)
    (re.compile(
        rf"в\s+финале\s+(?:был\s+|он\s+)?(?:обыгран|повержен|побеждён|обыграл|одолел|пал|разгром\w+|уничтожил)\s+(?:не\s+безызвестный\s+|самого\s+|хорошо\s+знакомого\s+(?:всем\s+)?)?({_NAME_WITH_OPT_ALIAS}?)\s*(?:[!\.\n,⚔💔]|\s+[—–\-]\s)",
        re.IGNORECASE,
    ), "name"),

    # "В финале он не оставил шансов Имя" (no @)
    (re.compile(
        rf"в\s+финале\s+он\s+не\s+оставил\s+шансов\s+({_NAME_WITH_OPT_ALIAS}?)\s*[,!\.⚔]",
        re.IGNORECASE,
    ), "name"),

    # "В финале против Имя"
    (re.compile(
        rf"в\s+финале\s+против\s+({_NAME_WITH_OPT_ALIAS}?)\s*[,\.\n!⚔]",
        re.IGNORECASE,
    ), "name"),

    # "В финале он одолел Имя"
    (re.compile(
        rf"в\s+финале\s+он\s+одолел\s+({_NAME_WITH_OPT_ALIAS}?)\s*[,!\.\n⚔]",
        re.IGNORECASE,
    ), "name"),

    # "В финале его соперником был Имя"
    (re.compile(
        rf"в\s+финале\s+(?:его\s+)?соперником\s+был[аи]?\s+({_NAME_WITH_OPT_ALIAS}?)\s*[,!\.\n]",
        re.IGNORECASE,
    ), "name"),

    # "В тяжёлом финале против Имя" / "В тяжёлой ... борьбе он одолел Имя"
    (re.compile(
        rf"в\s+(?:тяжёл\w+|упорн\w+|решающем\s+матче|тяжелейшем\s+финале)[^.\n]{{0,80}}одолел\s+(?:не\s+знакомого\s+|хорошо\s+знакомого\s+)?({_NAME_WITH_OPT_ALIAS}?)\s*[,!\.\n⚔]",
        re.IGNORECASE,
    ), "name"),

    # "Ему противостоял ... Имя" — only catch if a clear name follows.
    # The looser "опытный соперник" was over-matching common words.
    (re.compile(
        rf"ему\s+противостоял\s+@({_NAME_TOKEN})",
        re.IGNORECASE,
    ), "username"),

    # "Финал против Имя стал ..."
    (re.compile(
        rf"финал\s+против\s+({_NAME_WITH_OPT_ALIAS}?)\s+стал",
        re.IGNORECASE,
    ), "name"),

    # "В финале пал Имя"
    (re.compile(
        rf"в\s+финале\s+пал\s+({_NAME_WITH_OPT_ALIAS}?)\s*[!\.\n]",
        re.IGNORECASE,
    ), "name"),

    # Looser: "одолел Имя" / "обыграл Имя" / "разобрал Имя" anywhere
    (re.compile(
        rf"(?:одолел|обыграл|разобрал)\s+(?:не\s+безызвестного\s+|самого\s+|ярко\s+и\s+уверенно\s+)?({_NAME_WITH_OPT_ALIAS}?)\s*[,!\.\n⚔💔]",
        re.IGNORECASE,
    ), "name"),

    # ---- Подиум Призовых мест: "2. Имя" ([1147]) ----

    (re.compile(
        rf"призовые\s+места\s*:[^\n]*\n[^\n]*\n+\s*2\s*\.\s*({_NAME_TOKEN})",
        re.IGNORECASE,
    ), "name"),

    # ---- "А проигравшему Имя" ([1418] мини-кубок) ----

    (re.compile(
        rf"а\s+проигравшему\s+({_NAME_WITH_OPT_ALIAS})",
        re.IGNORECASE,
    ), "name"),
]


def extract_runner_up(text: str) -> Tuple[Optional[str], Optional[str]]:
    """Returns ``(raw_name, username)``."""
    for rx, kind in RUNNER_UP_PATTERNS:
        m = rx.search(text)
        if not m:
            continue
        captured = m.group(1).strip()
        if kind == "username":
            return None, captured
        name, _alias = split_name_alias(captured)
        if not name:
            continue
        return name, None
    return None, None


# --------------------------------------------------------------------------------------
# Fantasy podium parser
# --------------------------------------------------------------------------------------

FANTASY_RE = {
    "winner": re.compile(rf"поздравляем\s+победителя\s*[\-\u2013\u2014]\s*(@?{_NAME_TOKEN})", re.IGNORECASE),
    "silver": re.compile(rf"серебряный\s+мальчик\s*[\-\u2013\u2014]\s*(@?{_NAME_TOKEN})", re.IGNORECASE),
    "bronze": re.compile(rf"бронзовый\s+призёр\s*[\-\u2013\u2014]\s*(@?{_NAME_TOKEN})", re.IGNORECASE),
    "cup_winner": re.compile(rf"победой\s+в\s+кубке\s+фэнтези[^@\n]*?(@?{_NAME_TOKEN})", re.IGNORECASE),
}

FANTASY_CUP_ALT_RE = re.compile(
    rf"поздравляем\s+(@?{_NAME_TOKEN})\s+с\s+победой\s+в\s+кубке\s+фэнтези",
    re.IGNORECASE,
)


def _split_at_username(captured: str) -> Tuple[Optional[str], Optional[str]]:
    """``"@xxx"`` -> ``(None, "xxx")``; ``"Name"`` -> ``("Name", None)``."""
    captured = captured.strip()
    if captured.startswith("@"):
        return None, captured.lstrip("@")
    return clean_name(captured), None


def extract_fantasy_podium(text: str) -> Dict[str, Optional[Dict[str, Optional[str]]]]:
    out: Dict[str, Optional[Dict[str, Optional[str]]]] = {
        "winner": None, "silver": None, "bronze": None, "cup_winner": None,
    }
    for key, rx in FANTASY_RE.items():
        m = rx.search(text)
        if m:
            raw_name, username = _split_at_username(m.group(1))
            out[key] = {"raw_name": raw_name, "username": username}
    if out["cup_winner"] is None:
        m = FANTASY_CUP_ALT_RE.search(text)
        if m:
            raw_name, username = _split_at_username(m.group(1))
            out["cup_winner"] = {"raw_name": raw_name, "username": username}
    return out


# --------------------------------------------------------------------------------------
# Per-post pipeline
# --------------------------------------------------------------------------------------

def parse_post(msg: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """
    Returns ``(parsed_record, skipped_record)`` — exactly one of them is non-None
    (or both None if the post is irrelevant).
    """
    if msg.get("type") != "message":
        return None, None
    text = text_of(msg.get("text"))
    if not text:
        return None, None
    if not has_positive_signal(text):
        return None, None

    msg_id = msg.get("id")
    date = msg.get("date", "")
    url = POST_URL_TEMPLATE.format(username=CHANNEL_USERNAME, msg_id=msg_id)

    neg = negative_reason(text)
    if neg:
        return None, {
            "msg_id": msg_id,
            "date": date,
            "url": url,
            "skip_reason": neg,
            "text_preview": text[:300],
        }

    ttype = classify_tournament_type(text)

    if ttype == "fantasy":
        podium = extract_fantasy_podium(text)
        # Confidence: high if at least the winner is known
        winner = podium.get("winner")
        confidence = "high" if winner and (winner.get("username") or winner.get("raw_name")) else "low"
        return {
            "msg_id": msg_id,
            "date": date,
            "url": url,
            "tournament_type": "fantasy",
            "podium": podium,
            "confidence": confidence,
            "needs_review": confidence != "high",
            "raw_text": text,
        }, None

    # main / vsa share the same shape (winner + runner_up + score)
    raw_name, username, alias = extract_winner(text)
    runner_raw, runner_username = extract_runner_up(text)
    score = parse_score(text)
    count = parse_championship_count(text)
    tournament_number = parse_tournament_number(text) if ttype == "main" else None

    has_winner = bool(username or raw_name)
    has_runner = bool(runner_username or runner_raw)

    if username and has_runner and (score or count):
        confidence = "high"
    elif has_winner and has_runner:
        confidence = "high" if username else "medium"
    elif has_winner:
        confidence = "medium"
    else:
        confidence = "low"

    return {
        "msg_id": msg_id,
        "date": date,
        "url": url,
        "tournament_type": ttype,
        "tournament_number": tournament_number,
        "winner": {
            "raw_name": raw_name,
            "username": username,
            "alias": alias,
        },
        "runner_up": {
            "raw_name": runner_raw,
            "username": runner_username,
        },
        "final_score": score,
        "championship_count": count,
        "confidence": confidence,
        "needs_review": confidence != "high",
        "raw_text": text,
    }, None


# --------------------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------------------

def collect_aliases(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Names that appeared without an ``@username`` and will need manual aliasing."""
    counter: Counter = Counter()
    examples: Dict[str, List[Dict[str, Any]]] = {}

    def note(raw: Optional[str], role: str, rec: Dict[str, Any]) -> None:
        if not raw:
            return
        key = raw
        counter[key] += 1
        examples.setdefault(key, []).append({
            "msg_id": rec["msg_id"],
            "url": rec["url"],
            "date": rec["date"][:10],
            "role": role,
        })

    for rec in records:
        if rec["tournament_type"] == "fantasy":
            for slot, person in (rec.get("podium") or {}).items():
                if person and person.get("raw_name"):
                    note(person["raw_name"], f"fantasy_{slot}", rec)
            continue
        winner = rec.get("winner") or {}
        runner = rec.get("runner_up") or {}
        if winner.get("raw_name"):
            note(winner["raw_name"], "winner", rec)
        if winner.get("alias"):
            note(winner["alias"], "winner_alias", rec)
        if runner.get("raw_name"):
            note(runner["raw_name"], "runner_up", rec)

    return sorted(
        [
            {
                "name": name,
                "occurrences": counter[name],
                "suggested_username": None,
                "examples": examples[name][:5],
            }
            for name in counter
        ],
        key=lambda x: -x["occurrences"],
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dump", default="result.json", help="Path to Telegram export JSON")
    parser.add_argument("--out-dir", default="data", help="Directory for output files")
    args = parser.parse_args()

    dump_path = Path(args.dump)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    with dump_path.open("r", encoding="utf-8") as f:
        dump = json.load(f)

    messages = dump.get("messages", [])
    parsed: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []

    for m in messages:
        rec, skip = parse_post(m)
        if rec is not None:
            parsed.append(rec)
        elif skip is not None:
            skipped.append(skip)

    by_type = Counter(r["tournament_type"] for r in parsed)
    by_conf = Counter(r["confidence"] for r in parsed)

    aliases = collect_aliases(parsed)

    output = {
        "channel": CHANNEL_USERNAME,
        "channel_title": dump.get("name"),
        "parsed_at": datetime.now(timezone.utc).isoformat(),
        "stats": {
            "total_messages": len(messages),
            "parsed_records": len(parsed),
            "skipped_candidates": len(skipped),
            "by_type": dict(by_type),
            "by_confidence": dict(by_conf),
        },
        "tournaments": parsed,
    }

    (out_dir / "champions_parsed.json").write_text(
        json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (out_dir / "skipped_posts.json").write_text(
        json.dumps(skipped, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (out_dir / "aliases_to_review.json").write_text(
        json.dumps(aliases, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(f"Parsed:           {len(parsed)} winner posts")
    print(f"  by type:        {dict(by_type)}")
    print(f"  by confidence:  {dict(by_conf)}")
    print(f"Skipped:          {len(skipped)} ad/news/announcement posts")
    print(f"Aliases to fill:  {len(aliases)} unique non-username names")
    print(f"\nOutput written to {out_dir}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
