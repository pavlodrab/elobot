"""Smoke tests for ``database.merge_players`` (powering /relink_player).

The previous implementation accessed ``row[0]`` on rows returned by
``conn.execute(...).fetchall()``. That works on sqlite3.Row but raises
``KeyError(0)`` on psycopg2's RealDictRow, which surfaced to admins
as the cryptic message ``❌ Не получилось объединить: 0.``

These tests exercise the function end-to-end against a temp sqlite DB
and assert all four counter keys are present and accurate.
"""
from __future__ import annotations

import os
import sys
import tempfile


def _setup() -> "tuple":
    fd, path = tempfile.mkstemp(suffix=".db", prefix="merge_test_")
    os.close(fd)
    os.environ["DB_PATH"] = path
    # Force a re-import of database with the fresh DB_PATH
    for mod in [m for m in list(sys.modules) if m == "database" or m.startswith("database.")]:
        del sys.modules[mod]
    import database as db  # noqa: WPS433  (deliberate late import)
    db.init_db()
    return db, path


def test_merge_basic_disjoint_match():
    """Drop a tombstone with no matches, keep a fresh id_<tid> row."""
    db, path = _setup()
    try:
        old = db.upsert_player("oldhandle")
        db.set_game_nickname(old["id"], "Horizon")
        new = db.upsert_player("id_77777", telegram_id=77777)

        rival = db.upsert_player("rival")
        tid = db.create_tournament("T1", tournament_type="vsa", created_by=old["id"])
        db.add_player_to_tournament(tid, old["id"], "A")
        db.add_player_to_tournament(tid, rival["id"], "A")

        mid = db.create_match(tid, old["id"], rival["id"], stage="group")
        db.update_match(mid, score1=2, score2=1, status="confirmed")

        c = db.merge_players(keep_id=new["id"], drop_id=old["id"])

        # Counters must all be present and numeric.
        assert set(c) == {"matches_moved", "tp_overlap", "elo_overlap", "goals_moved"}
        assert all(isinstance(v, int) for v in c.values())
        assert c["matches_moved"] == 1
        assert c["tp_overlap"] == 0
        assert c["elo_overlap"] == 0

        # Old row gone, new row inherits the nickname.
        assert db.get_player_by_id(old["id"]) is None
        kept = db.get_player_by_id(new["id"])
        assert kept is not None
        assert kept.get("game_nickname") == "Horizon"

        # Match was re-tagged onto kept player (not orphaned).
        matches = db.get_tournament_matches(tid)
        assert len(matches) == 1
        assert matches[0]["player1_id"] == new["id"]
    finally:
        os.unlink(path)


def test_merge_overlap_sums_tp_stats():
    """When both rows are in the same tournament, group counters are summed."""
    db, path = _setup()
    try:
        old = db.upsert_player("twin1")
        new = db.upsert_player("twin2", telegram_id=42)
        tid = db.create_tournament("T2", tournament_type="vsa", created_by=old["id"])
        db.add_player_to_tournament(tid, old["id"], "A")
        db.add_player_to_tournament(tid, new["id"], "A")

        # Give each side some group stats.
        db.update_tournament_player(
            tid, old["id"],
            group_points=4, group_wins=1, group_draws=1,
            group_gf=3, group_ga=1,
        )
        db.update_tournament_player(
            tid, new["id"],
            group_points=3, group_wins=1, group_draws=0,
            group_gf=2, group_ga=2,
        )

        c = db.merge_players(keep_id=new["id"], drop_id=old["id"])
        assert c["tp_overlap"] == 1

        # Kept row should now hold the sums.
        kept_tps = db.get_tournament_players(tid)
        assert len(kept_tps) == 1
        row = kept_tps[0]
        assert row["player_id"] == new["id"]
        assert row["group_points"] == 7
        assert row["group_wins"] == 2
        assert row["group_gf"] == 5
        assert row["group_ga"] == 3
    finally:
        os.unlink(path)


def test_merge_self_match_is_pruned():
    """A pending matchup between the two ids becomes invalid after merging."""
    db, path = _setup()
    try:
        old = db.upsert_player("a")
        new = db.upsert_player("b", telegram_id=99)
        tid = db.create_tournament("T3", tournament_type="vsa", created_by=old["id"])
        db.add_player_to_tournament(tid, old["id"], "A")
        db.add_player_to_tournament(tid, new["id"], "A")

        db.create_match(tid, old["id"], new["id"], stage="group")

        db.merge_players(keep_id=new["id"], drop_id=old["id"])

        # Self-match must be removed (not silently rewritten to a→a).
        for m in db.get_tournament_matches(tid):
            assert m["player1_id"] != m["player2_id"]
    finally:
        os.unlink(path)


def test_merge_rejects_same_id():
    db, path = _setup()
    try:
        p = db.upsert_player("solo")
        try:
            db.merge_players(keep_id=p["id"], drop_id=p["id"])
        except ValueError:
            return
        raise AssertionError("expected ValueError for same id")
    finally:
        os.unlink(path)



def test_merge_survives_schema_drift_in_optional_steps():
    """Reproduce the production failure mode where a column missing
    in ``players`` (e.g. ``elo_vsa``) would crash the whole merge
    with ``current transaction is aborted, commands ignored…``.

    We simulate it by patching ``conn.cursor().execute`` to raise on
    the global-stats UPDATE — the savepoint should isolate the failure
    and let the merge complete with correct counters for everything
    else.
    """
    db, path = _setup()
    try:
        old = db.upsert_player("ghost")
        new = db.upsert_player("phoenix", telegram_id=1)
        rival = db.upsert_player("opp")
        tid = db.create_tournament("Drift", tournament_type="vsa", created_by=old["id"])
        db.add_player_to_tournament(tid, old["id"], "A")
        db.add_player_to_tournament(tid, rival["id"], "A")
        mid = db.create_match(tid, old["id"], rival["id"], stage="group")
        db.update_match(mid, score1=1, score2=0, status="confirmed")

        # Monkey-patch the cursor to fail on the global-stats UPDATE.
        import db_backend as dbb
        orig_execute = dbb._CursorWrapper.execute

        def patched(self, sql, params=None):
            if sql.lstrip().upper().startswith("UPDATE PLAYERS SET\n") or (
                "elo_vsa" in (sql or "") and "UPDATE players" in (sql or "")
            ):
                raise RuntimeError("simulated column drift: elo_vsa missing")
            return orig_execute(self, sql, params)

        dbb._CursorWrapper.execute = patched
        try:
            counters = db.merge_players(keep_id=new["id"], drop_id=old["id"])
        finally:
            dbb._CursorWrapper.execute = orig_execute

        # The simulated drift should NOT have killed the merge.
        assert counters["matches_moved"] == 1
        assert db.get_player_by_id(old["id"]) is None
        assert db.get_player_by_id(new["id"]) is not None
    finally:
        os.unlink(path)




def test_merge_promotes_elo_when_keep_is_zero():
    """Reproduces the prod report: keep row had ELO 0 and empty nick,
    drop had real ELO and a 'Loading...' placeholder nick. With the
    SQL ``MAX(elo, ?)`` form (which is invalid on Postgres), the entire
    promote block silently aborted and neither value was carried over.

    After the fix, max() is computed in Python and each column UPDATE
    sits in its own savepoint, so even if some optional column is
    missing on the production schema, the elo / nick still land on the
    kept row.
    """
    db, path = _setup()
    try:
        old = db.upsert_player("ghost_with_elo")
        # Simulate the dropped player having real stats: ELO 1234, a
        # game_nickname ('Loading...' is what the bot stores while it
        # is fetching the in-game name from the API; if the fetch
        # never finishes we still want the merge to carry whatever is
        # on the row, even a placeholder).
        db.set_player_elo(old["id"], 1234, by_user="test")
        db.set_game_nickname(old["id"], "Loading...")

        new = db.upsert_player("real_account", telegram_id=99001)
        # Keep row left at default (ELO 0, no nick).

        c = db.merge_players(keep_id=new["id"], drop_id=old["id"])
        assert isinstance(c.get("matches_moved"), int)

        kept = db.get_player_by_id(new["id"])
        assert kept is not None, "kept row vanished after merge"
        assert float(kept.get("elo") or 0) == 1234, (
            f"expected ELO 1234 carried from drop, got {kept.get('elo')!r}"
        )
        assert (kept.get("game_nickname") or "") == "Loading...", (
            f"expected nick 'Loading...' carried from drop, "
            f"got {kept.get('game_nickname')!r}"
        )
    finally:
        os.unlink(path)


def test_merge_keeps_higher_elo_when_keep_is_already_strong():
    """If the kept row already has a higher ELO than the drop, we
    must NOT downgrade it.
    """
    db, path = _setup()
    try:
        old = db.upsert_player("weak")
        new = db.upsert_player("strong", telegram_id=99002)
        db.set_player_elo(old["id"], 800, by_user="test")
        db.set_player_elo(new["id"], 1500, by_user="test")

        db.merge_players(keep_id=new["id"], drop_id=old["id"])

        kept = db.get_player_by_id(new["id"])
        assert float(kept.get("elo") or 0) == 1500, (
            f"merge must not downgrade keep's ELO 1500 to drop's 800, "
            f"got {kept.get('elo')!r}"
        )
    finally:
        os.unlink(path)

def main() -> int:
    failures: list[str] = []
    for fn in [
        test_merge_basic_disjoint_match,
        test_merge_overlap_sums_tp_stats,
        test_merge_self_match_is_pruned,
        test_merge_rejects_same_id,
        test_merge_survives_schema_drift_in_optional_steps,
        test_merge_promotes_elo_when_keep_is_zero,
        test_merge_keeps_higher_elo_when_keep_is_already_strong,
    ]:
        try:
            fn()
            print(f"  PASS  {fn.__name__}")
        except AssertionError as e:
            print(f"  FAIL  {fn.__name__}: {e}")
            failures.append(fn.__name__)
        except Exception as e:  # pragma: no cover  (surfaces real bugs)
            print(f"  ERROR  {fn.__name__}: {type(e).__name__}: {e}")
            failures.append(fn.__name__)
    if failures:
        print(f"\n{len(failures)} test(s) failed.")
        return 1
    print("\nAll merge_players tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
