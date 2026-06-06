"""
Reproduction + regression test for the mirrored playoff bracket bug
reported on Турнир Гвардиолыча №76:

  * the right-hand semifinal was rendered on the LEFT (and vice-versa),
  * the second semifinal only became visible once the LAST quarterfinal
    was played.

Both stem from ``_collect_pairs_full`` returning later-stage pairs in DB
insertion (finish) order instead of canonical bracket-slot order, and
from not padding a half-spawned stage with TBD placeholders.

Run:  BOT_TOKEN=dummy ADMIN_IDS=111 python test_bracket_slot_order.py
"""
import os
import sys
import tempfile

os.environ.setdefault("BOT_TOKEN", "dummy")
os.environ.setdefault("ADMIN_IDS", "111")

_tmp_db = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
_tmp_db.close()
os.environ["DB_PATH"] = _tmp_db.name


def expect(cond, msg):
    if cond:
        print(f"  ok  | {msg}")
    else:
        print(f"  FAIL| {msg}")
        sys.exit(1)


def _pids(pair):
    """Set of player ids in a pair-legs list (empty for TBD)."""
    if not pair or pair[0].get("_tbd"):
        return set()
    return {pair[0]["player1_id"], pair[0]["player2_id"]}


def main():
    import database as db
    db.init_db()

    from tournament import generate_playoff, advance_playoff
    from playoff_image import _collect_pairs_full

    # ── Build an 8-player bracket (straight QF → SF → Final). ───────────
    tid = db.create_tournament("SlotOrder", tournament_type="vsa")
    db.update_tournament(
        tid, bracket_only=1, playoff_matches_per_pair=1,
        playoff_third_place=0,
    )
    players = []
    for i in range(8):
        p = db.upsert_player(f"so_{i}")
        # Distinct, descending ELO so seeding is deterministic.
        db.update_player_stats(p["id"], elo_vsa=1800 - i * 10)
        db.add_player_to_tournament(tid, p["id"], "?")
        players.append(p)

    generate_playoff(tid)
    qf = db.get_tournament_matches(tid, stage="qf")
    expect(len(qf) == 4, f"8-player bracket → 4 QF matches (got {len(qf)})")

    # QF rows are stored in bracket-slot order (id order). Slots 0,1 are
    # the TOP half; slots 2,3 the BOTTOM half.
    qf_slot_players = [{m["player1_id"], m["player2_id"]} for m in qf]
    top_qf = qf_slot_players[0] | qf_slot_players[1]
    bot_qf = qf_slot_players[2] | qf_slot_players[3]

    # ── Resolve the BOTTOM-half QFs FIRST (slots 2 & 3). ───────────────
    # This is what triggered the bug: the bottom SF spawns before the top
    # SF, so DB/insertion order puts it first → mirrored layout drew it on
    # the left.
    for m in (qf[2], qf[3]):
        db.update_match(m["id"], score1=3, score2=0, status="confirmed")
    advance_playoff(tid)

    sf_after_partial = db.get_tournament_matches(tid, stage="sf")
    expect(
        len(sf_after_partial) == 1,
        f"only the bottom SF has spawned after 2/4 QF (got "
        f"{len(sf_after_partial)})",
    )

    # ── KEY ASSERTIONS on the partial state. ───────────────────────────
    full = dict(_collect_pairs_full(tid))
    expect("sf" in full, "_collect_pairs_full includes the sf stage")
    sf_pairs = full["sf"]
    expect(
        len(sf_pairs) == 2,
        f"sf padded to its full 2 slots even though only 1 spawned (got "
        f"{len(sf_pairs)})",
    )

    # Slot 0 (LEFT, top half) must be the TBD placeholder; slot 1 (RIGHT,
    # bottom half) must hold the real spawned SF between the bottom-QF
    # winners. Before the fix this was reversed.
    expect(
        sf_pairs[0][0].get("_tbd") is True,
        "sf slot 0 (left/top half) is a TBD placeholder",
    )
    expect(
        not sf_pairs[1][0].get("_tbd"),
        "sf slot 1 (right/bottom half) holds the real spawned match",
    )
    expect(
        _pids(sf_pairs[1]) <= bot_qf,
        "the spawned SF sits in the BOTTOM-half slot (its players came "
        "from the bottom-half QFs)",
    )

    # The TOP-half TBD SF should also be visible right away (the second
    # semifinal no longer waits for the last QF), and since the top QFs
    # are still pending it projects no feeder winners yet.
    expect(
        sf_pairs[0][0].get("_partial_winner_a") is None
        and sf_pairs[0][0].get("_partial_winner_b") is None,
        "top-half TBD SF shows no projected winners while its QFs pend",
    )

    # ── Now resolve the TOP-half QFs too. ──────────────────────────────
    for m in (qf[0], qf[1]):
        db.update_match(m["id"], score1=2, score2=1, status="confirmed")
    advance_playoff(tid)

    full2 = dict(_collect_pairs_full(tid))
    sf_pairs2 = full2["sf"]
    expect(len(sf_pairs2) == 2, "sf still has 2 slots once both spawned")
    expect(
        all(not p[0].get("_tbd") for p in sf_pairs2),
        "both SF slots are now real matches",
    )
    # Slot 0 must be fed by the TOP-half QFs, slot 1 by the BOTTOM half —
    # regardless of which finished first.
    expect(
        _pids(sf_pairs2[0]) <= top_qf,
        "sf slot 0 stays fed by the TOP-half QF winners",
    )
    expect(
        _pids(sf_pairs2[1]) <= bot_qf,
        "sf slot 1 stays fed by the BOTTOM-half QF winners",
    )

    # Final is padded as a single TBD pair projecting both SF winners.
    final_pairs = full2["final"]
    expect(len(final_pairs) == 1, "final padded to a single pair")
    expect(final_pairs[0][0].get("_tbd") is True, "final pair is TBD")

    # ── Smoke-test the actual mirrored render (no exceptions / valid PNG).
    from playoff_image import render_playoff_pngs
    pngs = render_playoff_pngs(tid)
    expect(len(pngs) >= 1, "render_playoff_pngs returns at least one image")
    expect(pngs[0][:8] == b"\x89PNG\r\n\x1a\n", "first image is a valid PNG")

    print("\nALL BRACKET-SLOT-ORDER TESTS PASSED.")


if __name__ == "__main__":
    main()
