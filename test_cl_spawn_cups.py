"""End-to-end test for the Champions League (32) follow-up cup spawning.

Builds a 32-player single-leg league, runs all matches with synthetic
results so each player ends up at a deterministic position, then calls
``spawn_cl_followup_cups`` and verifies:

* Two new tournaments are created (main + consolation).
* Main cup has 24 registered players, builds a 32-bracket with 8 byes
  for league top-8, and 8 real first-round (r32) matches between
  league seeds 9-24.
* Consolation cup has 8 registered players, builds an 8-bracket with
  no byes, and 4 real first-round (qf) matches.
* All real first-round matches have ``leg=1`` and ``leg=2`` rows
  (two-leg ties).
* Bye matches are auto-confirmed.
* Players are seeded by **league finishing position**, not global ELO.
"""
from __future__ import annotations

import os
import sys
import tempfile


def _setup():
    fd, path = tempfile.mkstemp(suffix=".db", prefix="cl_spawn_test_")
    os.close(fd)
    os.environ["DB_PATH"] = path
    for mod in [m for m in list(sys.modules) if m == "database" or m.startswith("database.")]:
        del sys.modules[mod]
    for mod in [m for m in list(sys.modules) if m == "tournament" or m.startswith("tournament.")]:
        del sys.modules[mod]
    import database as db
    import tournament as t
    db.init_db()
    return db, t, path


def _build_league_with_known_finish(db, t, n: int = 32) -> tuple[int, list[int]]:
    """Create a 32-player single-leg league and play it out so finishing
    order is exactly the registration order (player at index 0 wins
    everything → finishes 1st, etc.). Returns (league_tid, ordered_pids).
    """
    # Create players. Higher index = lower league finish.
    pids = []
    for i in range(n):
        # Spread the global ELO in REVERSE so it doesn't accidentally
        # match the league finish — that way we can prove the spawned
        # cups seed by league position, not by ELO.
        p = db.upsert_player(f"p{i:02d}", telegram_id=10000 + i)
        # bottom-of-the-table players have HIGH global ELO; top players LOW.
        db.set_player_elo(p["id"], 2000 - i, by_user="test")
        pids.append(p["id"])

    # Create league: 32 players, single round-robin, single group.
    creator = pids[0]
    league_tid = db.create_tournament(
        "CL Test", tournament_type="vsa", created_by=creator,
    )
    db.update_tournament(
        league_tid,
        groups_only=1,
        groups_count=1,
        group_matches_per_pair=1,
        playoff_third_place=0,
    )
    for pid in pids:
        db.add_player_to_tournament(league_tid, pid, "A")

    # Generate all round-robin matches (single circle).
    t.generate_group_fixtures(league_tid, {"A": pids})

    # Play out the league deterministically: lower index always wins 3:0.
    matches = db.get_tournament_matches(league_tid, stage="group")
    for m in matches:
        # If player1 has lower index (= higher seed), they win 3:0.
        i1 = pids.index(m["player1_id"])
        i2 = pids.index(m["player2_id"])
        if i1 < i2:
            db.update_match(m["id"], score1=3, score2=0, status="confirmed")
        else:
            db.update_match(m["id"], score1=0, score2=3, status="confirmed")

    # Recompute group standings — let's verify finishing order.
    standings = t.get_group_standings(league_tid)
    finishing = [p["player_id"] for p in standings["A"]]
    assert finishing == pids, (
        f"expected finish order {pids[:5]}... got {finishing[:5]}..."
    )
    return league_tid, pids


def test_spawn_creates_two_cups_with_correct_sizes():
    db, t, path = _setup()
    try:
        league_tid, pids = _build_league_with_known_finish(db, t, n=32)
        result = t.spawn_cl_followup_cups(league_tid)

        assert "main_tid" in result and "consolation_tid" in result
        main_tid = result["main_tid"]
        cons_tid = result["consolation_tid"]
        assert main_tid != cons_tid != league_tid

        # Main cup: 24 registered players.
        main_players = db.get_tournament_players(main_tid)
        assert len(main_players) == 24, f"main cup should have 24 players, got {len(main_players)}"
        # Exactly the league top-24.
        main_pids = {p["player_id"] for p in main_players}
        assert main_pids == set(pids[:24])

        # Consolation cup: 8 registered players (places 25-32).
        cons_players = db.get_tournament_players(cons_tid)
        assert len(cons_players) == 8, f"cons cup should have 8 players, got {len(cons_players)}"
        cons_pids = {p["player_id"] for p in cons_players}
        assert cons_pids == set(pids[24:32])
    finally:
        os.unlink(path)


def test_main_cup_has_8_byes_and_8_real_r32_matches_two_legs():
    db, t, path = _setup()
    try:
        league_tid, pids = _build_league_with_known_finish(db, t, n=32)
        result = t.spawn_cl_followup_cups(league_tid)
        main_tid = result["main_tid"]

        # All r32 matches in the main cup.
        r32 = db.get_tournament_matches(main_tid, stage="r32")
        # 8 byes (single auto-confirmed leg) + 8 real pairs × 2 legs = 24 rows.
        byes = [m for m in r32 if m["player1_id"] == m["player2_id"]]
        real = [m for m in r32 if m["player1_id"] != m["player2_id"]]
        assert len(byes) == 8, f"expected 8 byes, got {len(byes)}"
        assert len(real) == 16, f"expected 16 real legs (8 pairs × 2), got {len(real)}"

        # Byes go to the league top-8 (NOT the global-ELO top-8 — those
        # were the BOTTOM of the league per our setup).
        byed_pids = {m["player1_id"] for m in byes}
        assert byed_pids == set(pids[:8]), (
            f"byes should go to league top-8 ({pids[:8]}), got {sorted(byed_pids)}"
        )

        # Each real pair plays leg 1 and leg 2.
        from collections import Counter
        pair_keys = Counter()
        for m in real:
            key = tuple(sorted([m["player1_id"], m["player2_id"]]))
            pair_keys[key] += 1
        assert all(v == 2 for v in pair_keys.values()), (
            f"every real pair must have 2 legs, got {dict(pair_keys)}"
        )
        assert len(pair_keys) == 8, "expected 8 distinct real pairs"

        # Real matches are between league seeds 9-16 vs 17-24 (standard
        # bracket order). All players involved must be from positions 9-24.
        real_pids = {pid for m in real for pid in (m["player1_id"], m["player2_id"])}
        assert real_pids == set(pids[8:24])

        # Bye legs are auto-confirmed; real legs are pending.
        assert all(m["status"] == "confirmed" for m in byes)
        assert all(m["status"] == "pending" for m in real)
    finally:
        os.unlink(path)


def test_consolation_cup_has_no_byes_and_4_qf_two_legs():
    db, t, path = _setup()
    try:
        league_tid, pids = _build_league_with_known_finish(db, t, n=32)
        result = t.spawn_cl_followup_cups(league_tid)
        cons_tid = result["consolation_tid"]

        qf = db.get_tournament_matches(cons_tid, stage="qf")
        byes = [m for m in qf if m["player1_id"] == m["player2_id"]]
        real = [m for m in qf if m["player1_id"] != m["player2_id"]]
        assert len(byes) == 0, "8-player bracket must have no byes"
        assert len(real) == 8, f"expected 4 pairs × 2 legs = 8, got {len(real)}"

        # All cons cup players are the league bottom-8.
        real_pids = {pid for m in real for pid in (m["player1_id"], m["player2_id"])}
        assert real_pids == set(pids[24:32])

        # No bronze match for cons cup.
        cons_t = db.get_tournament(cons_tid)
        assert int(cons_t.get("playoff_third_place") or 0) == 0
        assert int(cons_t.get("playoff_matches_per_pair") or 1) == 2
        assert (cons_t.get("playoff_advance_mode") or "").lower() == "goals"
    finally:
        os.unlink(path)


def test_spawn_rejects_unfinished_league():
    db, t, path = _setup()
    try:
        # Build an incomplete league.
        pids = [db.upsert_player(f"q{i:02d}", telegram_id=20000 + i)["id"] for i in range(32)]
        creator = pids[0]
        league_tid = db.create_tournament("Half-done CL", tournament_type="vsa", created_by=creator)
        db.update_tournament(league_tid, groups_only=1, groups_count=1, group_matches_per_pair=1)
        for pid in pids:
            db.add_player_to_tournament(league_tid, pid, "A")
        t.generate_group_fixtures(league_tid, {"A": pids})

        # Confirm only one match.
        ms = db.get_tournament_matches(league_tid, stage="group")
        db.update_match(ms[0]["id"], score1=1, score2=0, status="confirmed")

        try:
            t.spawn_cl_followup_cups(league_tid)
        except ValueError:
            return
        raise AssertionError("expected ValueError for unfinished league")
    finally:
        os.unlink(path)


def test_spawn_rejects_too_few_players():
    db, t, path = _setup()
    try:
        # Only 16 players — cannot build 24+8.
        pids = [db.upsert_player(f"r{i:02d}", telegram_id=30000 + i)["id"] for i in range(16)]
        creator = pids[0]
        league_tid = db.create_tournament("Tiny CL", tournament_type="vsa", created_by=creator)
        db.update_tournament(league_tid, groups_only=1, groups_count=1, group_matches_per_pair=1)
        for pid in pids:
            db.add_player_to_tournament(league_tid, pid, "A")
        t.generate_group_fixtures(league_tid, {"A": pids})
        for m in db.get_tournament_matches(league_tid, stage="group"):
            db.update_match(m["id"], score1=1, score2=0, status="confirmed")

        try:
            t.spawn_cl_followup_cups(league_tid)
        except ValueError as e:
            assert "16" in str(e) or "32" in str(e)
            return
        raise AssertionError("expected ValueError for short roster")
    finally:
        os.unlink(path)



def test_spawn_handles_34_player_league_default_consolation():
    """The CL template no longer hard-codes 32 — same template + spawn
    must handle a 34-player league: top 24 → main cup, remaining 10 →
    consolation cup with byes for the top 6 cons seeds.
    """
    db, t, path = _setup()
    try:
        # 34 players, single-leg league.
        pids = []
        for i in range(34):
            p = db.upsert_player(f"r{i:02d}", telegram_id=70000 + i)
            db.set_player_elo(p["id"], 2000 - i, by_user="test")
            pids.append(p["id"])
        creator = pids[0]
        league_tid = db.create_tournament(
            "CL-34", tournament_type="vsa", created_by=creator,
        )
        db.update_tournament(
            league_tid,
            groups_only=1,
            groups_count=1,
            group_matches_per_pair=1,
            playoff_third_place=0,
        )
        for pid in pids:
            db.add_player_to_tournament(league_tid, pid, "A")
        t.generate_group_fixtures(league_tid, {"A": pids})
        for m in db.get_tournament_matches(league_tid, stage="group"):
            i1 = pids.index(m["player1_id"])
            i2 = pids.index(m["player2_id"])
            if i1 < i2:
                db.update_match(m["id"], score1=3, score2=0, status="confirmed")
            else:
                db.update_match(m["id"], score1=0, score2=3, status="confirmed")

        # Default call (no consolation_size): cons takes everyone past 24.
        result = t.spawn_cl_followup_cups(league_tid)
        main_tid = result["main_tid"]
        cons_tid = result["consolation_tid"]
        assert cons_tid is not None, "34 players must produce a consolation cup"

        # Main cup unchanged: 24 players, 32-bracket, 8 byes.
        main_players = db.get_tournament_players(main_tid)
        assert {p["player_id"] for p in main_players} == set(pids[:24])

        # Consolation: 10 players (places 25-34), 16-bracket, 6 byes.
        cons_players = db.get_tournament_players(cons_tid)
        assert {p["player_id"] for p in cons_players} == set(pids[24:34])
        # 10 players → 16-bracket → first stage = r16 → 8 pairs total.
        # 6 byes for top 6 cons seeds (= league places 25-30), 2 real
        # pairs × 2 legs = 4 real legs.
        r16 = db.get_tournament_matches(cons_tid, stage="r16")
        cons_byes = [m for m in r16 if m["player1_id"] == m["player2_id"]]
        cons_real = [m for m in r16 if m["player1_id"] != m["player2_id"]]
        assert len(cons_byes) == 6, (
            f"34-player league cons cup should have 6 byes, got {len(cons_byes)}"
        )
        assert len(cons_real) == 4, (
            f"cons cup should have 2 pairs × 2 legs = 4 real rows, got {len(cons_real)}"
        )
        # Byes go to league places 25-30 (top 6 of cons), real pairs
        # are between places 31-32 vs 33-34.
        bye_pids = {m["player1_id"] for m in cons_byes}
        assert bye_pids == set(pids[24:30])
        real_pids = {pid for m in cons_real for pid in (m["player1_id"], m["player2_id"])}
        assert real_pids == set(pids[30:34])
    finally:
        os.unlink(path)


def test_spawn_skips_consolation_when_only_one_extra_player():
    """League of 25: top 24 go to main cup, one player would be alone
    in the cons cup → cons must be skipped, not built as a 1-player
    bracket.
    """
    db, t, path = _setup()
    try:
        pids = []
        for i in range(25):
            p = db.upsert_player(f"s{i:02d}", telegram_id=60000 + i)
            db.set_player_elo(p["id"], 2000 - i, by_user="test")
            pids.append(p["id"])
        creator = pids[0]
        league_tid = db.create_tournament("CL-25", tournament_type="vsa", created_by=creator)
        db.update_tournament(
            league_tid, groups_only=1, groups_count=1, group_matches_per_pair=1,
        )
        for pid in pids:
            db.add_player_to_tournament(league_tid, pid, "A")
        t.generate_group_fixtures(league_tid, {"A": pids})
        for m in db.get_tournament_matches(league_tid, stage="group"):
            i1 = pids.index(m["player1_id"]); i2 = pids.index(m["player2_id"])
            if i1 < i2:
                db.update_match(m["id"], score1=3, score2=0, status="confirmed")
            else:
                db.update_match(m["id"], score1=0, score2=3, status="confirmed")

        result = t.spawn_cl_followup_cups(league_tid)
        assert result["main_tid"]
        assert result["consolation_tid"] is None
        assert result["consolation_matches"] == []
    finally:
        os.unlink(path)



def main() -> int:
    failures: list[str] = []
    for fn in [
        test_spawn_creates_two_cups_with_correct_sizes,
        test_main_cup_has_8_byes_and_8_real_r32_matches_two_legs,
        test_consolation_cup_has_no_byes_and_4_qf_two_legs,
        test_spawn_rejects_unfinished_league,
        test_spawn_rejects_too_few_players,
        test_spawn_handles_34_player_league_default_consolation,
        test_spawn_skips_consolation_when_only_one_extra_player,
    ]:
        try:
            fn()
            print(f"  PASS  {fn.__name__}")
        except AssertionError as e:
            print(f"  FAIL  {fn.__name__}: {e}")
            failures.append(fn.__name__)
        except Exception as e:
            import traceback
            print(f"  ERROR {fn.__name__}: {type(e).__name__}: {e}")
            traceback.print_exc()
            failures.append(fn.__name__)
    if failures:
        print(f"\n{len(failures)} test(s) failed.")
        return 1
    print("\nAll spawn_cl_followup_cups tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
