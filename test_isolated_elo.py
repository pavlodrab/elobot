"""
Sanity test for the per-tournament isolated ELO feature.

Exercises:
- DB migration adds the `is_official` column and `tournament_elo` table.
- create_tournament(is_official=False) is reflected in tournaments row.
- A confirmed match in an isolated tournament:
    * does NOT change players.elo / elo_vsa / elo_ri
    * DOES update tournament_elo for both players
    * DOES update global wins/losses/goals/streaks
- A confirmed match in an official tournament still updates global elo.
- get_tournament_leaderboard returns entries for joined players sorted by ELO.

Standalone — no telegram or external deps. Uses an in-memory-ish temp DB.
"""
import os
import sqlite3
import sys
import tempfile

# Force isolated DB BEFORE importing the bot modules.
TMPDIR = tempfile.mkdtemp(prefix="fc_elo_test_")
DB_PATH = os.path.join(TMPDIR, "league.db")
os.environ["DB_PATH"] = DB_PATH

import database as db  # noqa: E402
from match_processor import apply_result  # noqa: E402

FAILED = []


def expect(cond, msg):
    if cond:
        print(f"  PASS  {msg}")
    else:
        print(f"  FAIL  {msg}")
        FAILED.append(msg)


def setup_match(tid, p1_id, p2_id, s1, s2):
    """Create a 'confirmed' match ready for apply_result to process."""
    mid = db.create_match(tid, p1_id, p2_id, stage="group")
    db.update_match(mid, score1=s1, score2=s2, status="confirmed")
    return mid


def main():
    print(f"Using temp DB: {DB_PATH}")
    db.init_db()

    # ── Migration sanity ────────────────────────────────────────────────────
    conn = db.get_conn()
    cols = {r[1] for r in conn.execute("PRAGMA table_info(tournaments)").fetchall()}
    expect("is_official" in cols, "tournaments has is_official column")
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    expect("tournament_elo" in tables, "tournament_elo table exists")
    conn.close()

    # ── Players ──────────────────────────────────────────────────────────────
    alice = db.upsert_player("alice", telegram_id=1)
    bob   = db.upsert_player("bob",   telegram_id=2)
    expect(alice["elo"] == 0, "Alice starts at ELO 0")
    expect(bob["elo"]   == 0, "Bob starts at ELO 0")

    # ── 1) Isolated tournament — global ELO must stay at 0 ──────────────────
    iso_tid = db.create_tournament(
        "Player Cup", tournament_type="vsa",
        created_by=alice["id"], is_official=False,
    )
    db.add_player_to_tournament(iso_tid, alice["id"], "A")
    db.add_player_to_tournament(iso_tid, bob["id"],   "A")

    iso_t = db.get_tournament(iso_tid)
    expect(iso_t["is_official"] == 0, "Isolated tournament stored as is_official=0")

    mid_iso = setup_match(iso_tid, alice["id"], bob["id"], 4, 1)
    summary_iso = apply_result(mid_iso)

    alice_after = db.get_player_by_id(alice["id"])
    bob_after   = db.get_player_by_id(bob["id"])

    expect(alice_after["elo"] == 0, "Alice global ELO untouched after isolated win")
    expect(bob_after["elo"]   == 0, "Bob   global ELO untouched after isolated loss")
    expect((alice_after["elo_vsa"] or 0) == 0, "Alice elo_vsa untouched")
    expect((bob_after["elo_vsa"]   or 0) == 0, "Bob   elo_vsa untouched")
    expect(alice_after["wins"] == 1, "Alice global wins +1 (stat is still tracked)")
    expect(bob_after["losses"] == 1, "Bob   global losses +1")
    expect(alice_after["goals_scored"] == 4, "Alice goals_scored +4")
    expect(bob_after["goals_conceded"] == 4, "Bob   goals_conceded +4")

    a_local = db.get_tournament_elo(iso_tid, alice["id"])
    b_local = db.get_tournament_elo(iso_tid, bob["id"])
    expect(a_local["elo"] > 0, f"Alice local ELO went up (got {a_local['elo']})")
    expect(b_local["elo"] < 0, f"Bob   local ELO went down (got {b_local['elo']})")
    expect(a_local["wins"] == 1 and b_local["losses"] == 1,
           "Local W/L counters incremented")

    expect(summary_iso["is_official"] is False, "Summary marks match as non-official")
    expect(summary_iso["elo_scope"] == "local", "Summary elo_scope=='local'")
    expect(summary_iso["p1_typed_after"] is None, "No per-type mirror in isolated mode")

    lb = db.get_tournament_leaderboard(iso_tid)
    expect(len(lb) == 2, "Leaderboard has both joined players")
    expect(lb[0]["username"] == "alice", "Leaderboard ordered by ELO desc (alice on top)")

    # ── 2) Official tournament — global ELO must move ───────────────────────
    off_tid = db.create_tournament(
        "Admin Season 1", tournament_type="vsa",
        created_by=alice["id"], is_official=True,
    )
    db.add_player_to_tournament(off_tid, alice["id"], "A")
    db.add_player_to_tournament(off_tid, bob["id"],   "A")
    off_t = db.get_tournament(off_tid)
    expect(off_t["is_official"] == 1, "Official tournament stored as is_official=1")

    alice_pre  = db.get_player_by_id(alice["id"])["elo"]
    bob_pre    = db.get_player_by_id(bob["id"])["elo"]
    mid_off = setup_match(off_tid, alice["id"], bob["id"], 3, 0)
    summary_off = apply_result(mid_off)

    alice_post = db.get_player_by_id(alice["id"])
    bob_post   = db.get_player_by_id(bob["id"])
    expect(alice_post["elo"] > alice_pre, "Alice global ELO went UP after official win")
    expect(bob_post["elo"]   < bob_pre,   "Bob   global ELO went DOWN after official loss")
    expect((alice_post["elo_vsa"] or 0) > 0, "Alice elo_vsa updated (per-type mirror)")
    expect(summary_off["is_official"] is True, "Summary marks match as official")
    expect(summary_off["elo_scope"] == "global", "Summary elo_scope=='global'")

    # The isolated leaderboard for the OTHER tournament must be unchanged.
    a_local2 = db.get_tournament_elo(iso_tid, alice["id"])
    expect(a_local2["elo"] == a_local["elo"],
           "Official match did not leak into isolated tournament's local ELO")

    # ── 3) Migration of legacy data (no is_official column) ─────────────────
    # Simulate by dropping the column on a fresh DB and re-running init_db.
    legacy_dir = tempfile.mkdtemp(prefix="fc_elo_legacy_")
    legacy_db = os.path.join(legacy_dir, "league.db")
    conn = sqlite3.connect(legacy_db)
    conn.executescript(
        """
        CREATE TABLE players (id INTEGER PRIMARY KEY, username TEXT UNIQUE, elo REAL DEFAULT 0);
        CREATE TABLE tournaments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            tournament_type TEXT NOT NULL DEFAULT 'vsa',
            stage TEXT DEFAULT 'groups',
            groups_count INTEGER DEFAULT 2,
            playoff_started INTEGER DEFAULT 0,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        INSERT INTO tournaments (name) VALUES ('Legacy Season');
        """
    )
    conn.commit()
    conn.close()
    # Point database.py at the legacy DB and re-init.
    db.DB_PATH = legacy_db
    os.environ["DB_PATH"] = legacy_db
    db.init_db()
    conn = db.get_conn()
    legacy_row = conn.execute("SELECT is_official FROM tournaments WHERE name='Legacy Season'").fetchone()
    conn.close()
    expect(legacy_row["is_official"] == 1, "Legacy tournament backfilled to is_official=1")

    print()
    if FAILED:
        print(f"❌ {len(FAILED)} test(s) FAILED:")
        for m in FAILED:
            print(f"   - {m}")
        sys.exit(1)
    print("✅ All tests passed.")


if __name__ == "__main__":
    main()
