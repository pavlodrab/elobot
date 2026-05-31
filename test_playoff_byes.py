"""
Tests for the seeded playoff bracket with byes.

Covers the new behaviour added on top of the original two-shape
``generate_playoff`` (R16/QF cross-paring + 2-group SF):

* helper ``_bracket_seed_order`` matches standard tournament bracket;
* qualifiers are seeded globally by (group rank, points, GD, GF);
* with ``n`` qualifiers the bracket size is the next power of two ≥ n
  (capped at 16), and the surplus slots become byes for the top seeds;
* bye rows in ``matches`` are auto-confirmed with score 1:0 so
  ``advance_playoff`` cascades them into the next round naturally.

Run as a script (no pytest dependency):

    BOT_TOKEN=dummy ADMIN_IDS=111 python test_playoff_byes.py
"""
import os
import sys
import tempfile

os.environ.setdefault("BOT_TOKEN", "dummy")
os.environ.setdefault("ADMIN_IDS", "111")

# Use a fresh on-disk DB so tests don't collide with anything else.
_tmp_db = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
_tmp_db.close()
os.environ["DB_PATH"] = _tmp_db.name


def expect(cond, msg):
    if cond:
        print(f"  ok  | {msg}")
    else:
        print(f"  FAIL| {msg}")
        sys.exit(1)


def main():
    import database as db
    db.init_db()

    from tournament import (
        _bracket_seed_order,
        _next_pow2,
        _seed_qualifiers,
        _build_bracket_pairs,
        compute_playoff_preview,
        generate_playoff,
        advance_playoff,
        get_group_standings,
    )

    print("\n=== _next_pow2 ===")
    expect(_next_pow2(1) == 1, "_next_pow2(1) == 1")
    expect(_next_pow2(2) == 2, "_next_pow2(2) == 2")
    expect(_next_pow2(3) == 4, "_next_pow2(3) == 4")
    expect(_next_pow2(8) == 8, "_next_pow2(8) == 8")
    expect(_next_pow2(9) == 16, "_next_pow2(9) == 16")
    expect(_next_pow2(14) == 16, "_next_pow2(14) == 16")
    expect(_next_pow2(16) == 16, "_next_pow2(16) == 16")

    print("\n=== _bracket_seed_order ===")
    expect(_bracket_seed_order(2) == [1, 2],
           "size=2 → [1,2]")
    expect(_bracket_seed_order(4) == [1, 4, 2, 3],
           "size=4 → [1,4,2,3]")
    expect(_bracket_seed_order(8) == [1, 8, 4, 5, 2, 7, 3, 6],
           "size=8 → [1,8,4,5,2,7,3,6]")
    expect(_bracket_seed_order(16) ==
           [1, 16, 8, 9, 4, 13, 5, 12, 2, 15, 7, 10, 3, 14, 6, 11],
           "size=16 → standard 16-bracket order")

    # ---------------------------------------------------------------
    # 14 qualifiers (7 groups × top 2): 16-bracket, top 2 seeds get bye.
    # ---------------------------------------------------------------
    print("\n=== 14 qualifiers → 16-bracket with 2 byes ===")
    tid = db.create_tournament("FourteenCup", tournament_type="vsa")
    # Disable the optional 3rd-place fixture so the existing
    # "advance_playoff returns 'finished' after the final" assertion
    # still holds (the bronze match would otherwise gate the
    # transition until both fixtures are confirmed). A dedicated test
    # case at the end of this file exercises the on-by-default path.
    db.update_tournament(
        tid, playoff_slots=2, playoff_matches_per_pair=1,
        playoff_third_place=0,
    )

    # 7 groups × 2 players = 14 qualifiers. We add a 3rd player per group
    # so each group ranks correctly (1st > 2nd > 3rd) and the 3rd player
    # is excluded from the playoff.
    groups = "ABCDEFG"
    pp = []
    for g_idx, g in enumerate(groups):
        for pos in range(3):
            p = db.upsert_player(f"p_{g}_{pos}")
            pp.append((p, g, pos))
            db.add_player_to_tournament(tid, p["id"], g)
            # Force standings: 1st place = 9 pts (with stronger GD for
            # earlier groups), 2nd = 6 pts, 3rd = 0.
            base_pts = {0: 9, 1: 6, 2: 0}[pos]
            # Stronger group A > B > ... so seeding is deterministic.
            gd = (10 - g_idx) if pos == 0 else (5 - g_idx) if pos == 1 else 0
            gf = max(0, gd) + 1
            ga = max(0, -gd) + 1 if gd < 0 else 1
            db.update_tournament_player(
                tid, p["id"],
                group_points=base_pts,
                group_gf=gf,
                group_ga=ga,
                group_wins=base_pts // 3,
                group_draws=0,
                group_losses=0,
            )

    # Sanity: standings
    standings = get_group_standings(tid)
    expect(len(standings) == 7, "7 groups created")
    expect(all(len(standings[g]) == 3 for g in groups),
           "each group has 3 players in standings")

    # Preview before generating
    preview = compute_playoff_preview(tid)
    expect(preview["stage"] == "r16",
           f"preview stage is 'r16' (got {preview['stage']})")
    expect(preview.get("bracket_size") == 16,
           f"preview bracket_size is 16 (got {preview.get('bracket_size')})")
    bye_pairs = [p for p in preview["pairs"] if p["bye"]]
    real_pairs = [p for p in preview["pairs"] if not p["bye"]]
    expect(len(bye_pairs) == 2,
           f"2 bye pairs in preview (got {len(bye_pairs)})")
    expect(len(real_pairs) == 6,
           f"6 real pairs in preview (got {len(real_pairs)})")
    # Top 2 seeds (group A 1st place + group B 1st place) must be the byes.
    bye_seeds = sorted(p["a"]["username"] for p in bye_pairs)
    # ``upsert_player`` lower-cases usernames.
    expect(bye_seeds == ["p_a_0", "p_b_0"],
           f"top 2 seeds (1A, 1B) get the byes (got {bye_seeds})")

    # Generate the bracket
    bracket = generate_playoff(tid)
    r16_rows = [m for m in bracket if m["stage"] == "r16"]
    expect(len(r16_rows) == 8,
           f"first stage has 8 rows (6 real + 2 bye) (got {len(r16_rows)})")
    bye_rows = [m for m in r16_rows if m["bye"]]
    real_rows = [m for m in r16_rows if not m["bye"]]
    expect(len(bye_rows) == 2,
           f"2 bye rows (got {len(bye_rows)})")
    expect(len(real_rows) == 6,
           f"6 real first-round matches (got {len(real_rows)})")

    # In the DB the bye rows must already be confirmed at 1:0 with the
    # same player on both sides.
    db_r16 = db.get_tournament_matches(tid, stage="r16")
    db_byes = [m for m in db_r16 if m["player1_id"] == m["player2_id"]]
    expect(len(db_byes) == 2,
           f"DB has 2 bye rows (player1_id == player2_id) (got {len(db_byes)})")
    for m in db_byes:
        expect(m["status"] == "confirmed",
               f"bye match #{m['id']} is auto-confirmed")
        expect(m["score1"] == 1 and m["score2"] == 0,
               f"bye match #{m['id']} is 1:0")

    # ---------------------------------------------------------------
    # Now play out the real first-round matches and verify advance_playoff
    # promotes 8 winners into QF (real winners + 2 byed seeds).
    # ---------------------------------------------------------------
    print("\n=== advance_playoff after first round (with byes) ===")
    db_real = [m for m in db_r16 if m["player1_id"] != m["player2_id"]]
    for m in db_real:
        # Higher seed (= player with more group points) wins 2:0.
        p1 = db.get_player_by_id(m["player1_id"])
        p2 = db.get_player_by_id(m["player2_id"])
        # We don't have group_points on the players directly, so just
        # let player1 win — they were placed first in the seed order
        # and that's deterministic.
        db.update_match(m["id"], score1=2, score2=0, status="confirmed")

    next_stage = advance_playoff(tid)
    expect(next_stage == "qf",
           f"after r16 (incl. byes) advance_playoff promotes to 'qf' "
           f"(got {next_stage})")

    qf_rows = db.get_tournament_matches(tid, stage="qf")
    expect(len(qf_rows) == 4,
           f"QF has 4 matches (got {len(qf_rows)})")

    # The 2 byed seeds must each appear in exactly one QF match.
    byed_seed_ids = {m["player1_id"] for m in db_byes}
    qf_player_ids = set()
    for m in qf_rows:
        qf_player_ids.add(m["player1_id"])
        qf_player_ids.add(m["player2_id"])
    for sid in byed_seed_ids:
        expect(sid in qf_player_ids,
               f"byed seed (player_id={sid}) is in the QF lineup")

    # ---------------------------------------------------------------
    # Cascade: play QF → SF → Final and verify champion is decided.
    # ---------------------------------------------------------------
    print("\n=== full cascade (QF → SF → Final) for the 14-player bracket ===")
    db_qf = db.get_tournament_matches(tid, stage="qf")
    for m in db_qf:
        db.update_match(m["id"], score1=2, score2=1, status="confirmed")
    nxt = advance_playoff(tid)
    expect(nxt == "sf", f"after QF, advance_playoff returns 'sf' (got {nxt})")
    db_sf = db.get_tournament_matches(tid, stage="sf")
    expect(len(db_sf) == 2, f"SF has 2 matches (got {len(db_sf)})")
    for m in db_sf:
        db.update_match(m["id"], score1=2, score2=1, status="confirmed")
    nxt = advance_playoff(tid)
    expect(nxt == "final", f"after SF, advance_playoff returns 'final' (got {nxt})")
    db_final = db.get_tournament_matches(tid, stage="final")
    expect(len(db_final) == 1, f"Final has 1 match (got {len(db_final)})")
    db.update_match(db_final[0]["id"], score1=2, score2=1, status="confirmed")
    nxt = advance_playoff(tid)
    expect(nxt == "finished",
           f"after Final, advance_playoff returns 'finished' (got {nxt})")

    # ---------------------------------------------------------------
    # 8 qualifiers (4 groups × top 2) — straight QF, no byes.
    # ---------------------------------------------------------------
    print("\n=== 8 qualifiers → straight QF, no byes ===")
    tid8 = db.create_tournament("EightCup", tournament_type="vsa")
    db.update_tournament(tid8, playoff_slots=2, playoff_matches_per_pair=1)
    for g_idx, g in enumerate("ABCD"):
        for pos in range(2):
            p = db.upsert_player(f"e_{g}_{pos}")
            db.add_player_to_tournament(tid8, p["id"], g)
            base_pts = 9 if pos == 0 else 6
            gd = (10 - g_idx) if pos == 0 else (5 - g_idx)
            gf = max(0, gd) + 1
            ga = 1
            db.update_tournament_player(
                tid8, p["id"], group_points=base_pts,
                group_gf=gf, group_ga=ga,
            )
    bracket8 = generate_playoff(tid8)
    qf_count = len([m for m in bracket8 if m["stage"] == "qf"])
    expect(qf_count == 4,
           f"8 qualifiers → 4 QF matches (got {qf_count})")
    bye_count = len([m for m in bracket8 if m.get("bye")])
    expect(bye_count == 0,
           f"8 qualifiers → 0 byes (got {bye_count})")

    # ---------------------------------------------------------------
    # 4 qualifiers (2 groups × top 2) — straight SF, like before.
    # Backwards-compat with the old generate_playoff behaviour for the
    # 2-group case.
    # ---------------------------------------------------------------
    print("\n=== 4 qualifiers → straight SF, no byes ===")
    tid4 = db.create_tournament("FourCup", tournament_type="vsa")
    db.update_tournament(tid4, playoff_slots=2, playoff_matches_per_pair=1)
    for g_idx, g in enumerate("AB"):
        for pos in range(2):
            p = db.upsert_player(f"f_{g}_{pos}")
            db.add_player_to_tournament(tid4, p["id"], g)
            base_pts = 9 if pos == 0 else 6
            gd = (10 - g_idx) if pos == 0 else (5 - g_idx)
            gf = max(0, gd) + 1
            ga = 1
            db.update_tournament_player(
                tid4, p["id"], group_points=base_pts,
                group_gf=gf, group_ga=ga,
            )
    bracket4 = generate_playoff(tid4)
    sf_count = len([m for m in bracket4 if m["stage"] == "sf"])
    expect(sf_count == 2,
           f"4 qualifiers → 2 SF matches (got {sf_count})")
    bye_count4 = len([m for m in bracket4 if m.get("bye")])
    expect(bye_count4 == 0,
           f"4 qualifiers → 0 byes (got {bye_count4})")

    # ---------------------------------------------------------------
    # 18 qualifiers (9 groups × top 2): 32-bracket. Top 14 seeds skip
    # straight to R16; bottom 4 fight in 2 R32 matches → 2 winners
    # join the 14 byes for a clean 16-player R16.
    # ---------------------------------------------------------------
    print("\n=== 18 qualifiers → 32-bracket: 14 byes + 4 in R32 ===")
    tid18 = db.create_tournament("EighteenCup", tournament_type="vsa")
    db.update_tournament(
        tid18, playoff_slots=2, playoff_matches_per_pair=1,
        playoff_third_place=0,
    )
    for g_idx, g in enumerate("ABCDEFGHI"):  # 9 groups
        for pos in range(2):
            p = db.upsert_player(f"x_{g}_{pos}")
            db.add_player_to_tournament(tid18, p["id"], g)
            base_pts = 9 if pos == 0 else 6
            gd = (10 - g_idx) if pos == 0 else (5 - g_idx)
            gf = max(0, gd) + 1
            ga = 1
            db.update_tournament_player(
                tid18, p["id"], group_points=base_pts,
                group_gf=gf, group_ga=ga,
            )
    bracket18 = generate_playoff(tid18)

    r32_rows = [m for m in bracket18 if m["stage"] == "r32"]
    expect(len(r32_rows) == 16,
           f"first stage 'r32' has 16 rows (2 real + 14 bye) (got {len(r32_rows)})")
    bye_rows18 = [m for m in r32_rows if m.get("bye")]
    real_rows18 = [m for m in r32_rows if not m.get("bye")]
    expect(len(bye_rows18) == 14,
           f"14 byes (top seeds skip straight to R16) (got {len(bye_rows18)})")
    expect(len(real_rows18) == 2,
           f"2 real R32 matches (4 lowest seeds play in) (got {len(real_rows18)})")

    # Cascade R32 → R16 → QF → SF → Final.
    db_r32 = db.get_tournament_matches(tid18, stage="r32")
    db_real_r32 = [m for m in db_r32 if m["player1_id"] != m["player2_id"]]
    expect(len(db_real_r32) == 2,
           f"DB has 2 real R32 matches (got {len(db_real_r32)})")
    for m in db_real_r32:
        db.update_match(m["id"], score1=2, score2=0, status="confirmed")
    nxt = advance_playoff(tid18)
    expect(nxt == "r16", f"after R32, advance_playoff promotes to 'r16' "
                         f"(got {nxt})")
    db_r16_18 = db.get_tournament_matches(tid18, stage="r16")
    expect(len(db_r16_18) == 8,
           f"R16 has 8 matches after the 14 byes + 2 R32 winners merge "
           f"(got {len(db_r16_18)})")

    # Walk all the way to a champion.
    for m in db_r16_18:
        db.update_match(m["id"], score1=2, score2=1, status="confirmed")
    nxt = advance_playoff(tid18)
    expect(nxt == "qf", f"after R16, advance_playoff promotes to 'qf' "
                        f"(got {nxt})")
    for m in db.get_tournament_matches(tid18, stage="qf"):
        db.update_match(m["id"], score1=2, score2=1, status="confirmed")
    nxt = advance_playoff(tid18)
    expect(nxt == "sf", f"after QF, advance_playoff promotes to 'sf' "
                        f"(got {nxt})")
    for m in db.get_tournament_matches(tid18, stage="sf"):
        db.update_match(m["id"], score1=2, score2=1, status="confirmed")
    nxt = advance_playoff(tid18)
    expect(nxt == "final", f"after SF, advance_playoff promotes to 'final' "
                           f"(got {nxt})")
    fin = db.get_tournament_matches(tid18, stage="final")
    expect(len(fin) == 1, f"Final has 1 match (got {len(fin)})")
    db.update_match(fin[0]["id"], score1=2, score2=0, status="confirmed")
    nxt = advance_playoff(tid18)
    expect(nxt == "finished",
           f"after Final, advance_playoff returns 'finished' "
           f"(got {nxt})")

    # ---------------------------------------------------------------
    # 32 qualifiers — full R32, no byes, all players play first round.
    # ---------------------------------------------------------------
    print("\n=== 32 qualifiers → straight R32, no byes ===")
    tid32 = db.create_tournament("ThirtyTwoCup", tournament_type="vsa")
    db.update_tournament(tid32, playoff_slots=2, playoff_matches_per_pair=1)
    # 16 groups × top 2 = 32 qualifiers. We don't have 16 group letters
    # (GROUP_LETTERS = "ABCDEFGH"), so use 8 groups × top 4 instead and
    # set playoff_slots=4.
    db.update_tournament(tid32, playoff_slots=4)
    for g_idx, g in enumerate("ABCDEFGH"):  # 8 groups × 4 = 32
        for pos in range(4):
            p = db.upsert_player(f"y_{g}_{pos}")
            db.add_player_to_tournament(tid32, p["id"], g)
            base_pts = {0: 12, 1: 9, 2: 6, 3: 3}[pos]
            gd = (10 - g_idx) - pos
            gf = max(0, gd) + 1
            ga = 1
            db.update_tournament_player(
                tid32, p["id"], group_points=base_pts,
                group_gf=gf, group_ga=ga,
            )
    bracket32 = generate_playoff(tid32)
    r32_rows32 = [m for m in bracket32 if m["stage"] == "r32"]
    bye_count32 = len([m for m in r32_rows32 if m.get("bye")])
    expect(len(r32_rows32) == 16,
           f"32 qualifiers → 16 R32 matches (got {len(r32_rows32)})")
    expect(bye_count32 == 0,
           f"32 qualifiers → 0 byes (got {bye_count32})")

    # ---------------------------------------------------------------
    # 6 qualifiers (3 groups × top 2) — 8-bracket with 2 byes for top seeds.
    # Previously code created an irregular 3-pair "QF" — now it's a
    # proper 4-pair QF where the top 2 seeds skip into SF.
    # ---------------------------------------------------------------
    print("\n=== 6 qualifiers → 8-bracket with 2 byes ===")
    tid6 = db.create_tournament("SixCup", tournament_type="vsa")
    db.update_tournament(tid6, playoff_slots=2, playoff_matches_per_pair=1)
    for g_idx, g in enumerate("ABC"):
        for pos in range(2):
            p = db.upsert_player(f"s_{g}_{pos}")
            db.add_player_to_tournament(tid6, p["id"], g)
            base_pts = 9 if pos == 0 else 6
            gd = (10 - g_idx) if pos == 0 else (5 - g_idx)
            gf = max(0, gd) + 1
            ga = 1
            db.update_tournament_player(
                tid6, p["id"], group_points=base_pts,
                group_gf=gf, group_ga=ga,
            )
    bracket6 = generate_playoff(tid6)
    qf_count6 = len([m for m in bracket6 if m["stage"] == "qf"])
    bye_count6 = len([m for m in bracket6 if m.get("bye")])
    expect(qf_count6 == 4,
           f"6 qualifiers → 4 QF matches (2 real + 2 bye) (got {qf_count6})")
    expect(bye_count6 == 2,
           f"6 qualifiers → 2 byes (got {bye_count6})")

    # ---------------------------------------------------------------
    # Bracket-only tournament: no group stage, seeded by global ELO.
    # 6 players → 8-bracket → QF with 2 byes.
    # ---------------------------------------------------------------
    print("\n=== Bracket-only tournament: 6 players → 8-bracket ===")
    tid_bo = db.create_tournament("BracketOnly", tournament_type="vsa")
    db.update_tournament(tid_bo, bracket_only=1)
    # Six players with distinct vsa-ELO: 1700, 1600, 1500, 1400, 1300, 1200.
    bo_players = []
    for i, elo in enumerate([1700, 1600, 1500, 1400, 1300, 1200]):
        p = db.upsert_player(f"bo_{i}")
        db.update_player_stats(p["id"], elo_vsa=elo)
        db.add_player_to_tournament(tid_bo, p["id"], "?")
        bo_players.append((p, elo))
    bracket_bo = generate_playoff(tid_bo)
    qf_rows_bo = [m for m in bracket_bo if m["stage"] == "qf"]
    bye_rows_bo = [m for m in bracket_bo if m.get("bye")]
    expect(len(qf_rows_bo) == 4,
           f"bracket-only 6 → 4 QF matches (2 real + 2 bye) "
           f"(got {len(qf_rows_bo)})")
    expect(len(bye_rows_bo) == 2,
           f"bracket-only 6 → 2 byes for top-2 seeds (got {len(bye_rows_bo)})")
    # The byes must go to the two top-ELO players (1700 + 1600).
    bye_unames = sorted(m["player1"] for m in bye_rows_bo)
    expect(bye_unames == ["bo_0", "bo_1"],
           f"bracket-only byes go to top-2 ELO seeds (got {bye_unames})")

    # ---------------------------------------------------------------
    # Bracket-only with 18 players: should produce a 32-bracket with
    # 14 byes in R32 (top-14 ELO) and 2 real R32 matches between
    # seeds 15-18. After playing the real R32 matches → R16 cascade.
    # ---------------------------------------------------------------
    print("\n=== Bracket-only: 18 players → 32-bracket, 14 byes ===")
    tid_bo18 = db.create_tournament("BO18", tournament_type="vsa")
    db.update_tournament(tid_bo18, bracket_only=1)
    for i in range(18):
        p = db.upsert_player(f"bo18_{i:02d}")
        db.update_player_stats(p["id"], elo_vsa=2000 - i * 10)
        db.add_player_to_tournament(tid_bo18, p["id"], "?")
    bracket_bo18 = generate_playoff(tid_bo18)
    r32_bo18 = [m for m in bracket_bo18 if m["stage"] == "r32"]
    bye_bo18 = [m for m in r32_bo18 if m.get("bye")]
    real_bo18 = [m for m in r32_bo18 if not m.get("bye")]
    expect(len(r32_bo18) == 16,
           f"bracket-only 18 → 16 R32 slots (got {len(r32_bo18)})")
    expect(len(bye_bo18) == 14,
           f"bracket-only 18 → 14 byes (got {len(bye_bo18)})")
    expect(len(real_bo18) == 2,
           f"bracket-only 18 → 2 real R32 matches (got {len(real_bo18)})")

    # ---------------------------------------------------------------
    # Large bracket sanity: 100 qualifiers → 128-bracket, 28 byes
    # (top-28 seeds skip R128, bottom 72 fight in 36 real R128 matches).
    # ---------------------------------------------------------------
    print("\n=== 100 qualifiers (bracket-only) → 128-bracket ===")
    tid_big = db.create_tournament("BigBracket", tournament_type="vsa")
    db.update_tournament(tid_big, bracket_only=1)
    for i in range(100):
        p = db.upsert_player(f"big_{i:03d}")
        db.update_player_stats(p["id"], elo_vsa=3000 - i)
        db.add_player_to_tournament(tid_big, p["id"], "?")
    bracket_big = generate_playoff(tid_big)
    r128 = [m for m in bracket_big if m["stage"] == "r128"]
    byes_big = [m for m in r128 if m.get("bye")]
    real_big = [m for m in r128 if not m.get("bye")]
    expect(len(r128) == 64,
           f"100 → 64 R128 slots in bracket (got {len(r128)})")
    expect(len(byes_big) == 28,
           f"100 → 28 byes in R128 (got {len(byes_big)})")
    expect(len(real_big) == 36,
           f"100 → 36 real R128 matches (got {len(real_big)})")

    # ---------------------------------------------------------------
    # 3rd-place fixture: with 4 qualifiers and third-place ENABLED
    # (default), advance_playoff after the SF must spawn BOTH the
    # final and a bronze match between the two SF losers. Closing
    # only the final must NOT mark the tournament finished — the
    # bronze match has to be played too. Closing both flips the
    # tournament to ``finished``.
    # ---------------------------------------------------------------
    print("\n=== 4 qualifiers + 3rd-place enabled: bronze fixture ===")
    tid_b = db.create_tournament("BronzeCup", tournament_type="vsa")
    db.update_tournament(
        tid_b, playoff_slots=2, playoff_matches_per_pair=1,
        playoff_third_place=1,
    )
    bronze_players: dict[str, int] = {}
    for g_idx, g in enumerate("AB"):
        for pos in range(2):
            uname = f"br_{g}_{pos}"
            p = db.upsert_player(uname)
            bronze_players[uname] = p["id"]
            db.add_player_to_tournament(tid_b, p["id"], g)
            base_pts = 9 if pos == 0 else 6
            gd = (10 - g_idx) if pos == 0 else (5 - g_idx)
            gf = max(0, gd) + 1
            ga = 1
            db.update_tournament_player(
                tid_b, p["id"], group_points=base_pts,
                group_gf=gf, group_ga=ga,
            )
    generate_playoff(tid_b)
    sf_b = db.get_tournament_matches(tid_b, stage="sf")
    expect(len(sf_b) == 2, f"4-qualifier bracket spawns 2 SF (got {len(sf_b)})")

    # Pre-SF: no bronze rows yet.
    pre_third = db.get_tournament_matches(tid_b, stage="third")
    expect(
        len(pre_third) == 0,
        f"no 3rd-place rows before SF completes (got {len(pre_third)})",
    )

    # Close both SF matches: player1 of each row wins 2:0. Track
    # winners + losers so we can verify the bronze pair.
    sf_losers: set[int] = set()
    sf_winners: set[int] = set()
    for m in sf_b:
        db.update_match(m["id"], score1=2, score2=0, status="confirmed")
        sf_winners.add(m["player1_id"])
        sf_losers.add(m["player2_id"])
    nxt = advance_playoff(tid_b)
    expect(
        nxt == "final",
        f"after SF, advance_playoff promotes to 'final' (got {nxt})",
    )

    # Final must exist with the two SF winners.
    fin_b = db.get_tournament_matches(tid_b, stage="final")
    expect(len(fin_b) == 1, f"Final has 1 match (got {len(fin_b)})")
    fin_ids = {fin_b[0]["player1_id"], fin_b[0]["player2_id"]}
    expect(
        fin_ids == sf_winners,
        f"Final pair = the two SF winners (got {fin_ids}, "
        f"expected {sf_winners})",
    )

    # Bronze must exist with the two SF losers.
    bronze = db.get_tournament_matches(tid_b, stage="third")
    expect(len(bronze) == 1, f"3rd-place fixture has 1 match (got {len(bronze)})")
    bronze_ids = {bronze[0]["player1_id"], bronze[0]["player2_id"]}
    expect(
        bronze_ids == sf_losers,
        f"3rd-place pair = the two SF losers (got {bronze_ids}, "
        f"expected {sf_losers})",
    )
    expect(
        (bronze[0].get("status") or "pending") == "pending",
        f"3rd-place fixture is pending until played "
        f"(got {bronze[0].get('status')})",
    )

    # Closing only the final must NOT mark the tournament finished.
    db.update_match(fin_b[0]["id"], score1=2, score2=1, status="confirmed")
    nxt = advance_playoff(tid_b)
    expect(
        nxt is None,
        f"final closed but bronze pending → advance_playoff returns None "
        f"(got {nxt!r})",
    )
    t_state = db.get_tournament(tid_b)
    expect(
        (t_state.get("stage") or "") != "finished",
        f"tournament stage stays != 'finished' until bronze is played "
        f"(got {t_state.get('stage')!r})",
    )

    # Closing the bronze match flips the tournament to finished.
    db.update_match(bronze[0]["id"], score1=1, score2=0, status="confirmed")
    nxt = advance_playoff(tid_b)
    expect(
        nxt == "finished",
        f"final + bronze both confirmed → advance_playoff returns "
        f"'finished' (got {nxt!r})",
    )
    t_state = db.get_tournament(tid_b)
    expect(
        (t_state.get("stage") or "") == "finished",
        f"tournament stage flips to 'finished' (got {t_state.get('stage')!r})",
    )

    # ---------------------------------------------------------------
    # With 3rd-place enabled but only ONE SF pair (e.g. a 2-team
    # tournament), no bronze fixture must be spawned.
    # ---------------------------------------------------------------
    print("\n=== 3rd-place enabled with only 1 SF pair: no bronze ===")
    tid_b2 = db.create_tournament("BronzeNoBye", tournament_type="vsa")
    db.update_tournament(
        tid_b2, playoff_slots=1, playoff_matches_per_pair=1,
        playoff_third_place=1,
    )
    # 2 players in 1 group → straight to a single Final pair via the
    # standard generator. Verify no bronze fixture is created.
    for pos in range(2):
        p = db.upsert_player(f"b2_{pos}")
        db.add_player_to_tournament(tid_b2, p["id"], "A")
        db.update_tournament_player(
            tid_b2, p["id"],
            group_points=9 if pos == 0 else 0,
            group_gf=1, group_ga=0,
        )
    generate_playoff(tid_b2)
    # The single-pair tournament jumps straight to 'final' with no SF
    # at all, so close the final and verify the tournament finishes
    # without ever spawning a 3rd-place row.
    fin2 = db.get_tournament_matches(tid_b2, stage="final")
    if fin2:
        db.update_match(fin2[0]["id"], score1=2, score2=0, status="confirmed")
        nxt = advance_playoff(tid_b2)
        # "finished" is the expected terminal state; no bronze rows.
        no_bronze = not db.get_tournament_matches(tid_b2, stage="third")
        expect(
            no_bronze,
            "no 3rd-place rows when bracket has no SF stage",
        )

    # ---------------------------------------------------------------
    # Podium computation for a fully-resolved 4-team bracket with the
    # 3rd-place fixture played. Verifies the get_tournament_podium()
    # helper that powers the "🏆 турнир завершён" чат-объявление.
    # ---------------------------------------------------------------
    print("\n=== podium for finished 4-team bracket WITH bronze ===")
    from tournament import get_tournament_podium
    tid_pod = db.create_tournament("PodTest", tournament_type="vsa")
    db.update_tournament(
        tid_pod, playoff_slots=2, playoff_matches_per_pair=1,
        playoff_third_place=1,
    )
    p_ids = []
    for gi, g in enumerate("AB"):
        for pos in range(2):
            p = db.upsert_player(f"pod_{gi}_{pos}")
            p_ids.append(p["id"])
            db.add_player_to_tournament(tid_pod, p["id"], g)
            db.update_tournament_player(
                tid_pod, p["id"],
                group_points=9 if pos == 0 else 6,
                group_gf=10 - gi if pos == 0 else 6,
                group_ga=1,
            )
    generate_playoff(tid_pod)
    for m in db.get_tournament_matches(tid_pod, stage="sf"):
        db.update_match(m["id"], score1=2, score2=0, status="confirmed")
    advance_playoff(tid_pod)
    fin = db.get_tournament_matches(tid_pod, stage="final")[0]
    db.update_match(fin["id"], score1=3, score2=1, status="confirmed")
    pod = get_tournament_podium(tid_pod)
    expect("first" in pod and "second" in pod,
           f"1st/2nd present after final (got {pod!r})")
    expect("third" not in pod,
           f"3rd absent while bronze is still pending (got {pod!r})")
    br = db.get_tournament_matches(tid_pod, stage="third")[0]
    db.update_match(br["id"], score1=4, score2=2, status="confirmed")
    advance_playoff(tid_pod)
    pod = get_tournament_podium(tid_pod)
    expect(all(k in pod for k in ("first", "second", "third", "fourth")),
           f"full podium after bronze confirmed (got {pod!r})")
    expect(pod["first"] != pod["third"],
           "1st and 3rd are different players")
    expect(pod["second"] != pod["fourth"],
           "2nd and 4th are different players")
    t_state = db.get_tournament(tid_pod)
    expect(t_state.get("stage") == "finished",
           f"stage flips to finished (got {t_state.get('stage')!r})")

    # ---------------------------------------------------------------
    # Same scenario but bronze DISABLED → both SF losers must end up
    # in 'third_tied'.
    # ---------------------------------------------------------------
    print("\n=== podium for finished bracket WITHOUT bronze (tied 3rd) ===")
    tid_pod2 = db.create_tournament("PodTestNoBronze", tournament_type="vsa")
    db.update_tournament(
        tid_pod2, playoff_slots=2, playoff_matches_per_pair=1,
        playoff_third_place=0,
    )
    for gi, g in enumerate("AB"):
        for pos in range(2):
            p = db.upsert_player(f"pod2_{gi}_{pos}")
            db.add_player_to_tournament(tid_pod2, p["id"], g)
            db.update_tournament_player(
                tid_pod2, p["id"],
                group_points=9 if pos == 0 else 6,
                group_gf=10 - gi if pos == 0 else 6,
                group_ga=1,
            )
    generate_playoff(tid_pod2)
    for m in db.get_tournament_matches(tid_pod2, stage="sf"):
        db.update_match(m["id"], score1=2, score2=0, status="confirmed")
    advance_playoff(tid_pod2)
    fin2 = db.get_tournament_matches(tid_pod2, stage="final")[0]
    db.update_match(fin2["id"], score1=3, score2=1, status="confirmed")
    advance_playoff(tid_pod2)
    pod = get_tournament_podium(tid_pod2)
    expect("third_tied" in pod and len(pod["third_tied"]) == 2,
           f"third_tied lists 2 SF losers (got {pod!r})")
    expect("third" not in pod and "fourth" not in pod,
           f"no explicit 3rd/4th when bronze disabled (got {pod!r})")

    # ---------------------------------------------------------------
    # 2-group cross-bracket draw: 4 from each group must NEVER face
    # the same group in the first round. Reproduces the bug
    # "@oliverbax vs @nazar_54321 (both group A)" from real data.
    # ---------------------------------------------------------------
    print("\n=== 2-group QF cross-bracket draw (no same-group in R1) ===")
    tid_cb = db.create_tournament("CrossBracketCup", tournament_type="ri")
    db.update_tournament(
        tid_cb, playoff_slots=4, playoff_matches_per_pair=1,
        playoff_third_place=0,
    )
    # Exactly the standings the user reported on 2026-05-20:
    # Group A (descending pts): A1=28, A2=25, A3=16, A4=13
    # Group B (descending pts): B1=20, B2=16, B3=15, B4=15  ← note B4 > A4
    real = [
        ("A", 0, 28, 28, 5),
        ("A", 1, 25, 22, 6),
        ("A", 2, 16, 21, 15),
        ("A", 3, 13, 15, 12),
        ("A", 4, 6, 6, 24),
        ("A", 5, 0, 0, 30),
        ("B", 0, 20, 19, 12),
        ("B", 1, 16, 15, 19),
        ("B", 2, 15, 16, 10),
        ("B", 3, 15, 18, 13),
        ("B", 4, 9, 13, 19),
        ("B", 5, 7, 10, 18),
    ]
    for g, pos, pts, gf, ga in real:
        p = db.upsert_player(f"xb_{g}_{pos}")
        db.add_player_to_tournament(tid_cb, p["id"], g)
        db.update_tournament_player(
            tid_cb, p["id"],
            group_points=pts, group_gf=gf, group_ga=ga,
        )

    bracket = generate_playoff(tid_cb)
    qf_real = [m for m in bracket if m["stage"] == "qf" and not m["bye"]]
    expect(len(qf_real) == 4,
           f"4 QF pairs created (got {len(qf_real)})")

    # Username pattern: xb_<GROUP>_<POSITION>. Every real first-round
    # pair must have players from different groups (different letter
    # at position 3 of the username).
    def _grp(uname):  # "xb_A_0" → "A"
        return uname.split("_")[1]

    pairs = [(_grp(m["player1"]), _grp(m["player2"])) for m in qf_real]
    same_group_pairs = [p for p in pairs if p[0] == p[1]]
    expect(
        not same_group_pairs,
        f"no same-group QF pairs (got {pairs!r}, bad={same_group_pairs!r})",
    )

    # Match the textbook "крест": A1×B4, B2×A3, B1×A4, A2×B3
    # The actual set of pair-keys must be exactly the cross set
    # (unordered within each pair, unordered between pairs).
    seen = {frozenset({m["player1"], m["player2"]}) for m in qf_real}
    expected = {
        frozenset({"xb_a_0", "xb_b_3"}),  # A1 × B4
        frozenset({"xb_b_1", "xb_a_2"}),  # B2 × A3
        frozenset({"xb_b_0", "xb_a_3"}),  # B1 × A4
        frozenset({"xb_a_1", "xb_b_2"}),  # A2 × B3
    }
    expect(seen == expected,
           f"QF pairs match cross-bracket spec\n  got {seen!r}\n  want {expected!r}")

    # ---------------------------------------------------------------
    # Incremental playoff advancement: confirming only a subset of QF
    # pairs that feed the same SF slot should immediately produce the
    # corresponding SF match without waiting for the full QF to finish.
    # ---------------------------------------------------------------
    print("\n=== Incremental advance: partial QF -> partial SF ===")
    tid_inc = db.create_tournament("IncrementalCup", tournament_type="ri")
    db.update_tournament(
        tid_inc, playoff_slots=8, playoff_matches_per_pair=1,
        playoff_third_place=0,
    )
    # Create 8 players in a single group with descending standings.
    inc_players = []
    for i in range(8):
        p = db.upsert_player(f"inc_p{i}")
        db.add_player_to_tournament(tid_inc, p["id"], "A")
        db.update_tournament_player(
            tid_inc, p["id"],
            group_points=30 - i * 3,
            group_gf=20 - i,
            group_ga=5,
        )
        inc_players.append(p["id"])

    generate_playoff(tid_inc)

    # Bracket seed order for size 8: [1,8,4,5,2,7,3,6]
    # QF pairs (by seed): (1v8), (4v5), (2v7), (3v6)
    # In next round: winner(pair0) vs winner(pair1), winner(pair2) vs winner(pair3)
    # So confirming pairs 0 and 1 should produce 1 SF match.
    qf_matches = db.get_tournament_matches(tid_inc, stage="qf")
    # Filter out byes (player1_id == player2_id)
    qf_real_inc = [m for m in qf_matches if m["player1_id"] != m["player2_id"]]
    expect(len(qf_real_inc) == 4,
           f"8-player bracket has 4 real QF matches (got {len(qf_real_inc)})")

    # Sort QF matches in bracket pair order to identify pair indices.
    # _pair_key gives (min_id, max_id); pairs are ordered by their
    # first occurrence in the bracket so we rely on iteration order
    # matching bracket seeding (dict insertion order).
    from tournament import _pair_key, _dedup_playoff_legs
    qf_deduped = _dedup_playoff_legs(qf_matches)
    qf_pairs_dict: dict = {}
    for m in qf_deduped:
        qf_pairs_dict.setdefault(_pair_key(m), []).append(m)
    qf_pair_keys = list(qf_pairs_dict.keys())
    expect(len(qf_pair_keys) == 4,
           f"4 QF pair keys (got {len(qf_pair_keys)})")

    # Confirm only pair 0 and pair 1 (feed the same SF slot).
    for pk in qf_pair_keys[:2]:
        for m in qf_pairs_dict[pk]:
            if m["player1_id"] == m["player2_id"]:
                continue  # bye, already confirmed
            db.update_match(m["id"], score1=2, score2=0, status="confirmed")

    # Call advance_playoff - should create exactly 1 SF match.
    result1 = advance_playoff(tid_inc)
    expect(result1 == "sf",
           f"advance_playoff returns 'sf' after partial QF (got {result1!r})")

    sf_matches_1 = db.get_tournament_matches(tid_inc, stage="sf")
    sf_real_1 = [m for m in sf_matches_1 if m["player1_id"] != m["player2_id"]]
    expect(len(sf_real_1) == 1,
           f"exactly 1 SF match created after 2/4 QF resolved (got {len(sf_real_1)})")

    # Idempotency: calling again should not create duplicates.
    result1b = advance_playoff(tid_inc)
    sf_matches_1b = db.get_tournament_matches(tid_inc, stage="sf")
    sf_real_1b = [m for m in sf_matches_1b if m["player1_id"] != m["player2_id"]]
    expect(len(sf_real_1b) == 1,
           f"idempotent: still 1 SF match after repeat call (got {len(sf_real_1b)})")

    # Verify the other 2 QF pairs are still pending (no extra SF yet).
    for pk in qf_pair_keys[2:]:
        for m in qf_pairs_dict[pk]:
            if m["player1_id"] == m["player2_id"]:
                continue
            fresh = db.get_match(m["id"])
            expect(fresh["status"] != "confirmed",
                   f"QF pair {pk} still pending")
            break  # only check one match per pair

    # Now confirm QF pairs 2 and 3.
    for pk in qf_pair_keys[2:]:
        for m in qf_pairs_dict[pk]:
            if m["player1_id"] == m["player2_id"]:
                continue
            db.update_match(m["id"], score1=1, score2=0, status="confirmed")

    # Call advance_playoff again - should create the 2nd SF match.
    result2 = advance_playoff(tid_inc)
    expect(result2 == "sf",
           f"advance_playoff returns 'sf' after remaining QF done (got {result2!r})")

    sf_matches_2 = db.get_tournament_matches(tid_inc, stage="sf")
    sf_real_2 = [m for m in sf_matches_2 if m["player1_id"] != m["player2_id"]]
    expect(len(sf_real_2) == 2,
           f"2 total SF matches after all QF resolved (got {len(sf_real_2)})")

    print("\nALL PLAYOFF-BYE TESTS PASSED.")


if __name__ == "__main__":
    main()
