"""Tests for the penalty-shootout feature.

Covers:
  - DB schema migration adds tournaments.playoff_penalties + matches.pen1/pen2
  - tournaments.playoff_penalties defaults to 0 (off — backwards compatible)
  - update_tournament can flip the toggle
  - update_match accepts pen1/pen2 and they round-trip
  - ocr._ai_post_process gates penalty values (drop on non-draw, tied
    shootout, single value, out-of-range, missing pair)
  - tournament._resolve_pair_winner uses pens as final tiebreaker
  - format_playoff_bracket renders "(пен. X:Y)" for legs that went to pens
"""
import os
import sys
import tempfile

# Use an isolated SQLite file per run so pre-existing schema doesn't
# mask migration regressions.
_db_fd, _db_path = tempfile.mkstemp(prefix="penalty_test_", suffix=".db")
os.close(_db_fd)
os.environ["DB_PATH"] = _db_path

import database as db
from database import (
    get_conn,
    init_db,
    create_tournament,
    update_tournament,
    create_match,
    update_match,
    get_match,
    get_tournament,
    upsert_player,
    get_player,
    add_player_to_tournament,
)
import ocr
import tournament as t_mod


_failures: list[str] = []


def expect(cond: bool, msg: str):
    if cond:
        print(f"  PASS  {msg}")
    else:
        print(f"  FAIL  {msg}")
        _failures.append(msg)


# ── 1) Schema migration ──────────────────────────────────────────────────
init_db()
print("\n=== Schema migration ===")

conn = get_conn()
cur = conn.cursor()
cur.execute("PRAGMA table_info(tournaments)")
tcols = {r[1] for r in cur.fetchall()}
expect("playoff_penalties" in tcols, "tournaments.playoff_penalties column added")

cur.execute("PRAGMA table_info(matches)")
mcols = {r[1] for r in cur.fetchall()}
expect("pen1" in mcols, "matches.pen1 column added")
expect("pen2" in mcols, "matches.pen2 column added")
conn.close()


# ── 2) Default + toggle ──────────────────────────────────────────────────
print("\n=== Toggle + default ===")
upsert_player("admin1", telegram_id=999_001)
admin = get_player("admin1")

tid = create_tournament(
    "Penalty Cup", tournament_type="vsa", created_by=admin["id"],
    is_official=True,
)
t = get_tournament(tid)
expect(int(t.get("playoff_penalties") or 0) == 0,
       "playoff_penalties defaults to 0 (off)")

update_tournament(tid, playoff_penalties=1)
t = get_tournament(tid)
expect(int(t["playoff_penalties"]) == 1,
       "playoff_penalties flips to 1 via update_tournament")


# ── 3) Match round-trip ──────────────────────────────────────────────────
print("\n=== Match pen1/pen2 round-trip ===")
upsert_player("alice", telegram_id=999_002)
upsert_player("bob",   telegram_id=999_003)
a = get_player("alice")
b = get_player("bob")
add_player_to_tournament(tid, a["id"], "A")
add_player_to_tournament(tid, b["id"], "A")

mid = create_match(tid, a["id"], b["id"], stage="final", round_num=1)
update_match(mid, score1=3, score2=3, pen1=3, pen2=1, status="confirmed")
m = get_match(mid)
expect(m["pen1"] == 3 and m["pen2"] == 1,
       f"matches.pen1/pen2 persisted (got {m.get('pen1')}/{m.get('pen2')})")


# ── 4) OCR post-process gate ─────────────────────────────────────────────
print("\n=== OCR _ai_post_process penalty gate ===")
from ocr import _ai_post_process

cases = [
    # (input_pens, regulation, expected_kept, label)
    ((3, 1), (3, 3), True,  "valid 3:3 reg + 3:1 pens kept"),
    ((3, 1), (3, 2), False, "non-draw regulation drops pens"),
    ((3, 3), (1, 1), False, "tied shootout drops pens"),
    ((3, None), (1, 1), False, "single-value pens drop"),
    ((90, 0), (1, 1), False, "timer-shaped pens dropped (out of range)"),
    ((-1, 5), (1, 1), False, "negative pens dropped"),
]
for (p1, p2), (s1, s2), kept, label in cases:
    p = {
        "score1": s1, "score2": s2,
        "pen1": p1, "pen2": p2,
        "team1": "A", "team2": "B",
    }
    _ai_post_process(p)
    if kept:
        ok = p["pen1"] == p1 and p["pen2"] == p2
    else:
        ok = p["pen1"] is None and p["pen2"] is None
    expect(ok, label)


# ── 5) MatchScreenshot dataclass ────────────────────────────────────────
print("\n=== MatchScreenshot pen helpers ===")
ms = ocr.MatchScreenshot()
expect(not ms.has_penalties, "fresh MatchScreenshot has no penalties")
expect(ms.pen_score is None, "pen_score is None when pens unset")
ms.pen1, ms.pen2 = 3, 1
expect(ms.has_penalties, "has_penalties true when both set")
expect(ms.pen_score == "3:1", f"pen_score = '3:1' (got {ms.pen_score!r})")


# ── 6) _resolve_pair_winner with pens ────────────────────────────────────
print("\n=== _resolve_pair_winner penalty tiebreaker ===")
from tournament import _resolve_pair_winner

# Single leg, regulation draw + pens 3:1 → home (player1) wins
leg = {
    "id": 100, "player1_id": 1, "player2_id": 2,
    "score1": 3, "score2": 3, "pen1": 3, "pen2": 1,
    "status": "confirmed",
}
expect(_resolve_pair_winner([leg], advance_mode="goals") == 1,
       "single leg 3:3 (3:1 pens) → player1 wins")

# Same leg with reversed orientation: player1_id=2 means canonical a=2.
# pen1/pen2 belong to player1 (id 2), so a-pens=3, b-pens=1, a wins, a==2.
leg_rev = dict(leg)
leg_rev.update(player1_id=2, player2_id=1)
expect(_resolve_pair_winner([leg_rev], advance_mode="goals") == 2,
       "reversed orientation: pens correctly attributed to player1_id")

# Two legs, aggregate tied + each won one leg → wins tied → pens decide
leg_a = {
    "id": 200, "player1_id": 1, "player2_id": 2,
    "score1": 1, "score2": 2, "status": "confirmed",
}
leg_b = {
    "id": 201, "player1_id": 2, "player2_id": 1,
    "score1": 1, "score2": 2,
    "pen1": 5, "pen2": 4,           # leg's player1=2 → canonical (b=2): a-pens=4, b-pens=5
    "status": "confirmed",
}
expect(_resolve_pair_winner([leg_a, leg_b], advance_mode="goals") == 2,
       "aggregate tied 3:3, wins tied 1-1, pens 4:5 → player2 wins")

# Same scenario but no pens → still tied (None)
leg_b_no_pen = dict(leg_b); leg_b_no_pen.pop("pen1"); leg_b_no_pen.pop("pen2")
expect(_resolve_pair_winner([leg_a, leg_b_no_pen], advance_mode="goals") is None,
       "without pens, true deadlock returns None")

# Multiple legs with pens: latest leg id decides
leg_c = {
    "id": 300, "player1_id": 1, "player2_id": 2,
    "score1": 0, "score2": 0, "pen1": 5, "pen2": 4,  # older
    "status": "confirmed",
}
leg_d = {
    "id": 301, "player1_id": 1, "player2_id": 2,
    "score1": 0, "score2": 0, "pen1": 3, "pen2": 4,  # newer (id=301), b wins on this one
    "status": "confirmed",
}
expect(_resolve_pair_winner([leg_c, leg_d], advance_mode="goals") == 2,
       "multiple shootouts: latest leg by id decides")


# ── 7) format_playoff_bracket renders pens ──────────────────────────────
print("\n=== format_playoff_bracket renders pens ===")
# Mark final match confirmed with pens, then render
update_match(mid, score1=3, score2=3, pen1=3, pen2=1, status="confirmed")
text = t_mod.format_playoff_bracket(tid)
expect("(пен. 3:1)" in text or "(пен. 3:1)".lower() in text.lower(),
       f"bracket text contains penalty marker (excerpt: {text[:300]!r})")


# ── Summary ──────────────────────────────────────────────────────────────
print()
if _failures:
    print(f"FAIL  {len(_failures)} test(s) failed:")
    for f in _failures:
        print(f"  - {f}")
    sys.exit(1)
else:
    print("✅ All penalty-shootout tests passed.")

# Cleanup
try:
    os.unlink(_db_path)
except OSError:
    pass
