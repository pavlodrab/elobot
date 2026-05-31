"""
Regression test for the ``parse_mode="HTML"`` "unsupported start tag"
crash class.

Background
----------
``handlers/common.py::send`` sets ``parse_mode="HTML"`` by default.
Telegram's HTML-mode parser only accepts a tiny allowlist of tags::

    b, strong, i, em, u, ins, s, strike, del, a, code, pre, tg-spoiler

Anything else - in particular Cyrillic placeholder words like
``<ник в игре>`` or ``<текст>`` - is rejected with::

    telegram.error.BadRequest: Can't parse entities:
        unsupported start tag "ник" at byte offset 82

Three real instances of this bug shipped to production simultaneously
(``bot.py:1587``, ``handlers/admin.py:958-959``).  Each one looks
innocent in code review but blows up the moment a user hits the
unhappy-path code branch.

Strategy
--------
Walk every ``.py`` source file under ``fc_league_bot/`` with ``ast``,
extract every string literal that is NOT a module/class/function
docstring, and assert that no string contains a ``<word>`` token
whose tag name is outside the Telegram allowlist.  This catches both
the original three bugs and any future reintroductions of the same
shape.

Run directly (``python test_html_placeholders.py``) or via pytest.
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parent

# Telegram Bot API HTML mode - exact tag list documented at
# https://core.telegram.org/bots/api#html-style
ALLOWED_TAGS: frozenset[str] = frozenset({
    "b", "strong",
    "i", "em",
    "u", "ins",
    "s", "strike", "del",
    "a",
    "code", "pre",
    "tg-spoiler",
    "blockquote",
    "span",            # used by tg for tg-spoiler/expandable blockquote
})

# Capture ``<name`` (open) and ``</name`` (close).  We don't care about
# attributes - we just need the tag name.
_TAG_RE = re.compile(r"<\s*/?\s*([^\s<>/!?][^\s<>/]*)")


def _strip_doc(node: ast.AST) -> None:
    """Drop the docstring (first stmt) from any module/class/function
    body so we never flag user-internal text."""
    if hasattr(node, "body") and isinstance(node.body, list) and node.body:
        first = node.body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            node.body[0] = ast.Pass()  # neutralise the docstring


def _strip_all_docs(tree: ast.AST) -> None:
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            _strip_doc(node)


def _all_strings(tree: ast.AST) -> Iterable[tuple[ast.AST, str]]:
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield node, node.value


def _bad_tags(text: str) -> list[tuple[int, str]]:
    """Return ``[(offset, tag_name), ...]`` for every disallowed tag."""
    bad: list[tuple[int, str]] = []
    for m in _TAG_RE.finditer(text):
        # Strip leading '/' (close tag) and any attributes after a space.
        raw = m.group(1).lstrip("/").strip().lower()
        name = raw.split()[0] if raw.split() else raw
        if not name:
            continue
        # Heuristic: skip pure-punctuation matches (e.g. ``</br>`` -> 'br'
        # is fine to keep flagged; we want to flag those too if they ever
        # appear).  Numbers-only or things like ``<3`` we never want to
        # flag.
        if name.isdigit() or not re.match(r"^[A-Za-z\u00C0-\uFFFF]", name):
            continue
        if name in ALLOWED_TAGS:
            continue
        bad.append((m.start(), name))
    return bad


# Files that send messages to users.  These are the only files where a
# string literal can plausibly reach Telegram's HTML parser.  Other
# modules (``database.py`` SQL comments, ``ocr.py`` AI prompt templates,
# ``standings_image.py`` PIL labels, etc.) legitimately contain
# placeholders like ``<player_id>`` or ``<minute>`` that never go
# anywhere near Telegram, so we don't lint them.
_USER_FACING_FILES = (
    "bot.py",
    "handlers/admin.py",
    "handlers/common.py",
    "handlers/leaderboard.py",
    "handlers/match.py",
    "handlers/profile.py",
    "handlers/queries.py",
    "handlers/tournament.py",
)


def _iter_source_files() -> list[Path]:
    out: list[Path] = []
    for rel in _USER_FACING_FILES:
        p = ROOT / rel
        if p.exists():
            out.append(p)
    return out


def lint_file(path: Path) -> list[tuple[int, str, str]]:
    """Return ``[(line, tag_name, snippet), ...]`` for every disallowed
    tag found in a non-docstring string literal of ``path``."""
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src, filename=str(path))
    _strip_all_docs(tree)
    findings: list[tuple[int, str, str]] = []
    for node, text in _all_strings(tree):
        for offset, name in _bad_tags(text):
            snippet = text[max(0, offset - 25): offset + 25].replace("\n", "\\n")
            findings.append((getattr(node, "lineno", 0), name, snippet))
    return findings


def test_no_invalid_html_tags_in_user_strings() -> None:
    """The actual pytest entrypoint."""
    all_findings: dict[str, list[tuple[int, str, str]]] = {}
    for f in _iter_source_files():
        bad = lint_file(f)
        if bad:
            all_findings[str(f.relative_to(ROOT))] = bad

    if all_findings:
        msg_lines = [
            "Found user-facing string literals containing literal '<word>' tokens",
            "outside Telegram's HTML allowlist (b/i/u/s/em/strong/ins/strike/del/",
            "a/code/pre/tg-spoiler/blockquote/span).",
            "",
            "Telegram will reject these messages with",
            "  telegram.error.BadRequest: Can't parse entities: unsupported start tag",
            "Either escape with &lt;/&gt; or pass parse_mode=None.",
            "",
        ]
        for f, items in all_findings.items():
            msg_lines.append(f"  {f}:")
            for line, name, snippet in items:
                msg_lines.append(f"    line {line}: <{name}>  …{snippet}…")
        raise AssertionError("\n".join(msg_lines))


if __name__ == "__main__":
    try:
        test_no_invalid_html_tags_in_user_strings()
    except AssertionError as exc:
        print("FAIL")
        print(exc)
        sys.exit(1)
    print("OK - no disallowed HTML tag names in user-facing strings.")
