"""
End-to-end test against a real Postgres instance.

Skipped automatically if DATABASE_URL is not set or psycopg2 is not installed.

Mirrors test_isolated_elo.py but on Postgres, so we know the abstraction layer
works on the actual deploy target (Railway).
"""
from __future__ import annotations

import os
import sys


def main():
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        print("DATABASE_URL is not set — skipping Postgres e2e test.")
        return 0
    try:
        import psycopg2  # noqa: F401
    except ImportError:
        print("psycopg2 not installed — skipping Postgres e2e test.")
        return 0

    # Reset the schema so each run starts clean.
    import psycopg2 as pg
    raw = pg.connect(url.replace("postgres://", "postgresql://", 1))
    raw.autocommit = True
    cur = raw.cursor()
    cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    cur.close()
    raw.close()

    import database as db
    from match_processor import apply_result

    failed = []

    def expect(cond, msg):
        if cond:
            print(f"  PASS  {msg}")
        else:
            print(f"  FAIL  {msg}")
            failed.append(msg)

    db.init_db()

    # ── DB layer sanity ─────────────────────────────────────────────────────
    conn = db.get_conn()
    cols = conn.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name='tournaments'"
    ).fetchall()
    col_names = {r["column_name"] for r in cols}
    expect("is_official" in col_names, "tournaments.is_official exists on Postgres")
    expect("chat_id" in col_names, "tournaments.chat_id exists on Postgres")
    tables = conn.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema='public'"
    ).fetchall()
    table_names = {r["table_name"] for r in tables}
    expect("tournament_elo" in table_names, "tournament_elo table exists on Postgres")
    conn.close()

    # ── Players ─────────────────────────────────────────────────────────────
    alice = db.upsert_player("alice", telegram_id=1)
    bob = db.upsert_player("bob", telegram_id=2)
    expect(int(alice["elo"]) == 0, "Alice starts at ELO 0")
    expect(int(bob["elo"]) == 0, "Bob   starts at ELO 0")

    # ── Isolated tournament ─────────────────────────────────────────────────
    iso_tid = db.create_tournament(
        "Player Cup", tournament_type="vsa",
        created_by=alice["id"], is_official=False,
    )
    db.add_player_to_tournament(iso_tid, alice["id"], "A")
    db.add_player_to_tournament(iso_tid, bob["id"], "A")

    iso_t = db.get_tournament(iso_tid)
    expect(int(iso_t["is_official"]) == 0, "Isolated tournament stored as is_official=0")

    mid = db.create_match(iso_tid, alice["id"], bob["id"], stage="group")
    db.update_match(mid, score1=4, score2=1, status="confirmed")
    summary = apply_result(mid)

    alice_post = db.get_player_by_id(alice["id"])
    bob_post = db.get_player_by_id(bob["id"])
    expect(int(alice_post["elo"]) == 0, "Alice global ELO untouched")
    expect(int(bob_post["elo"]) == 0, "Bob   global ELO untouched")
    expect(alice_post["wins"] == 1, "Alice global wins +1")
    expect(bob_post["losses"] == 1, "Bob   global losses +1")

    a_local = db.get_tournament_elo(iso_tid, alice["id"])
    b_local = db.get_tournament_elo(iso_tid, bob["id"])
    expect(a_local["elo"] > 0, f"Alice local ELO went up (got {a_local['elo']})")
    expect(b_local["elo"] < 0, f"Bob   local ELO went down (got {b_local['elo']})")

    expect(summary["is_official"] is False, "Summary marks match as non-official")
    expect(summary["elo_scope"] == "local", "Summary elo_scope=='local'")

    lb = db.get_tournament_leaderboard(iso_tid)
    expect(len(lb) == 2, "Leaderboard has 2 entries")
    expect(lb[0]["username"] == "alice", "Leaderboard sorted with alice on top")

    # ── Chat binding ────────────────────────────────────────────────────────
    db.set_tournament_chat(iso_tid, "-1001234567890")
    by_chat = db.get_tournament_by_chat("-1001234567890")
    expect(by_chat is not None and by_chat["id"] == iso_tid, "set_tournament_chat / get_tournament_by_chat round-trip")

    found = db.find_tournaments_by_name_substring("player cu")
    expect(any(t["id"] == iso_tid for t in found),
           "find_tournaments_by_name_substring matches Player Cup")

    # ── Official tournament — global ELO must move ──────────────────────────
    off_tid = db.create_tournament(
        "Admin Season", tournament_type="vsa",
        created_by=alice["id"], is_official=True,
    )
    db.add_player_to_tournament(off_tid, alice["id"], "A")
    db.add_player_to_tournament(off_tid, bob["id"], "A")

    alice_pre_elo = db.get_player_by_id(alice["id"])["elo"]
    mid2 = db.create_match(off_tid, alice["id"], bob["id"], stage="group")
    db.update_match(mid2, score1=3, score2=0, status="confirmed")
    summary2 = apply_result(mid2)
    alice_post2 = db.get_player_by_id(alice["id"])
    expect(alice_post2["elo"] > alice_pre_elo, "Alice global ELO went UP after official win")
    expect(summary2["elo_scope"] == "global", "Official summary elo_scope=='global'")

    if failed:
        print()
        print(f"FAIL  {len(failed)} test(s) failed:")
        for m in failed:
            print(f"   - {m}")
        return 1
    print()
    print("All Postgres e2e tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
