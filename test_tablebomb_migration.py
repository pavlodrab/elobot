"""
Regression tests for the two ``/tablebomb`` bugs that crashed Postgres
production (see CHANGELOG.md / Phase 3.2 hotfix):

Bug A — UndefinedColumn:
    Older Postgres deploys had a ``match_goals`` table that pre-dated the
    ``side TEXT`` and ``tournament_id INTEGER`` columns. Without an
    ``ALTER TABLE`` migration, ``get_top_scorers_by_side_for_tournament``
    crashed with::
        psycopg2.errors.UndefinedColumn: column mg.side does not exist

    Fix: idempotent ``ALTER TABLE … ADD COLUMN`` migrations in
    ``init_db()``, run **before** the ``CREATE INDEX`` lines so the
    index creation itself can't crash on a fresh migration. Backfill
    ``tournament_id`` from the parent ``matches`` row so historical
    goals still rank in /tablebomb.

Bug B — GroupingError:
    Even after the schema migration, the same query still crashed on
    Postgres because ``GROUP BY player_id`` resolved to the real
    ``mg.player_id`` column instead of the SELECT alias (Postgres is
    strict here; SQLite is loose and accidentally accepted it). The CASE
    expression's ``mg.side`` / ``m.player1_id`` / ``m.player2_id`` then
    fell out of any aggregate / GROUP BY scope, and Postgres raised::
        psycopg2.errors.GroupingError:
            column "mg.side" must appear in the GROUP BY clause …

    Fix: rename the alias to ``scorer_id`` (no collision) and group
    positionally (``GROUP BY 1``), which is portable to both backends.

These tests run on whatever backend the suite was launched with — set
``DATABASE_URL=postgresql://…`` to exercise the Postgres path, or leave
it unset for the SQLite path.
"""
from __future__ import annotations

import os
import sys
import tempfile

# Make sure we run from the bot's source directory so all sibling
# modules (db_backend, database, …) import cleanly when the test is
# launched from anywhere.
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)


def _make_legacy_match_goals(conn) -> int:
    """Build the pre-migration ``match_goals`` schema by hand (no
    ``side``, no ``tournament_id``), seed one confirmed match plus one
    legacy goal that points at it, and return the match id.
    """
    cur = conn.cursor()

    if conn.backend == "postgres":
        # Drop in the right order — match_goals first (FK on matches).
        for stmt in (
            "DROP TABLE IF EXISTS match_goals CASCADE",
            "DROP TABLE IF EXISTS matches CASCADE",
            """CREATE TABLE matches (
                id SERIAL PRIMARY KEY,
                tournament_id BIGINT,
                player1_id BIGINT, player2_id BIGINT,
                score1 BIGINT, score2 BIGINT,
                status TEXT DEFAULT 'pending'
            )""",
            """CREATE TABLE match_goals (
                id SERIAL PRIMARY KEY,
                match_id BIGINT NOT NULL,
                player_id BIGINT,
                raw_name TEXT,
                minute BIGINT,
                ord BIGINT NOT NULL DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )""",
        ):
            cur._raw.execute(stmt)
        cur._raw.execute(
            "INSERT INTO matches (tournament_id, player1_id, player2_id, "
            "score1, score2, status) VALUES "
            "(1, 10, 20, 2, 1, 'confirmed') RETURNING id"
        )
        # ``cur._raw`` is a RealDictCursor on Postgres → dict-like row.
        row = cur._raw.fetchone()
        match_id = row["id"] if hasattr(row, "keys") else row[0]
        cur._raw.execute(
            "INSERT INTO match_goals (match_id, player_id, raw_name, minute, ord) "
            "VALUES (%s, 10, 'oldname', 12, 0)",
            (match_id,),
        )
    else:
        # SQLite path: bypass the wrapper so ``CREATE TABLE matches`` here
        # doesn't trip translate_schema (we want raw SQLite types).
        raw = conn._raw
        raw.execute("DROP TABLE IF EXISTS match_goals")
        raw.execute("DROP TABLE IF EXISTS matches")
        raw.execute(
            """CREATE TABLE matches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tournament_id INTEGER,
                player1_id INTEGER, player2_id INTEGER,
                score1 INTEGER, score2 INTEGER,
                status TEXT DEFAULT 'pending'
            )"""
        )
        raw.execute(
            """CREATE TABLE match_goals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                match_id INTEGER NOT NULL,
                player_id INTEGER,
                raw_name TEXT,
                minute INTEGER,
                ord INTEGER NOT NULL DEFAULT 0,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )"""
        )
        raw.execute(
            "INSERT INTO matches (tournament_id, player1_id, player2_id, "
            "score1, score2, status) VALUES (1, 10, 20, 2, 1, 'confirmed')"
        )
        match_id = raw.execute("SELECT id FROM matches LIMIT 1").fetchone()[0]
        raw.execute(
            "INSERT INTO match_goals (match_id, player_id, raw_name, minute, ord) "
            "VALUES (?, 10, 'oldname', 12, 0)",
            (match_id,),
        )

    conn.commit()
    return match_id


def _list_columns(conn, table: str) -> list[str]:
    if conn.backend == "postgres":
        rows = conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = ? ORDER BY ordinal_position",
            (table,),
        ).fetchall()
        return [r["column_name"] if hasattr(r, "keys") else r[0] for r in rows]
    rows = conn._raw.execute(f"PRAGMA table_info({table})").fetchall()
    return [r[1] for r in rows]


def _force_clean_sqlite_db():
    """For SQLite mode, point DB_PATH at a fresh temp file so we
    don't trample the developer's local league.db."""
    if os.getenv("DATABASE_URL", "").strip():
        return None  # Postgres: nothing to do
    tmpd = tempfile.mkdtemp(prefix="tablebomb_test_")
    db_path = os.path.join(tmpd, "league.db")
    os.environ["DB_PATH"] = db_path
    return tmpd


def main() -> int:
    failed: list[str] = []

    def expect(cond: bool, msg: str) -> None:
        prefix = "  PASS" if cond else "  FAIL"
        print(f"{prefix}  {msg}")
        if not cond:
            failed.append(msg)

    _force_clean_sqlite_db()

    # Reload the modules in this order so the IS_POSTGRES flag is
    # picked up from the (possibly just-set) DATABASE_URL env var.
    import importlib
    import db_backend
    importlib.reload(db_backend)
    import database
    importlib.reload(database)

    backend = "postgres" if database.IS_POSTGRES else "sqlite"
    print(f"\n=== /tablebomb migration regression — backend={backend} ===")

    # Step 1: build the legacy schema and seed it.
    conn = database.get_conn()
    match_id = _make_legacy_match_goals(conn)
    cols_before = _list_columns(conn, "match_goals")
    conn.close()
    expect(
        "side" not in cols_before and "tournament_id" not in cols_before,
        f"legacy match_goals lacks side / tournament_id (got {cols_before})",
    )

    # Step 2: idempotent migration — three passes, no crash.
    for i in range(3):
        try:
            database.init_db()
        except Exception as e:  # pragma: no cover - test failure
            failed.append(f"init_db() pass {i + 1} crashed: {e!r}")
            print(f"  FAIL  init_db() pass {i + 1} crashed: {e!r}")
            return 1
    print("  PASS  init_db() x3 idempotent (no crash)")

    # Step 3: verify the columns are now present.
    conn = database.get_conn()
    cols_after = _list_columns(conn, "match_goals")
    expect("side" in cols_after, f"side column added (cols={cols_after})")
    expect(
        "tournament_id" in cols_after,
        f"tournament_id column added (cols={cols_after})",
    )

    # Step 4: backfill — legacy goals should pick up tournament_id from
    # the parent matches row. ``side`` stays NULL because the legacy
    # row didn't carry it.
    rows = conn.execute(
        "SELECT tournament_id, side FROM match_goals ORDER BY id"
    ).fetchall()
    legacy_tid = rows[0]["tournament_id"] if hasattr(rows[0], "keys") else rows[0][0]
    legacy_side = rows[0]["side"] if hasattr(rows[0], "keys") else rows[0][1]
    expect(legacy_tid == 1, f"legacy goal backfilled tournament_id=1 (got {legacy_tid})")
    expect(legacy_side is None, f"legacy goal side stays NULL (got {legacy_side!r})")
    conn.close()

    # Step 5: insert post-fix goals via the public CRUD that production uses.
    database.set_match_goals(
        match_id,
        [
            {"player_id": 10, "raw_name": "p10", "minute": 5,  "side": "home"},
            {"player_id": 10, "raw_name": "p10", "minute": 30, "side": "home"},
            {"player_id": 20, "raw_name": "p20", "minute": 70, "side": "away"},
        ],
    )

    # Step 6: the previously-crashing query — must run cleanly on both
    # backends now (Bug A: schema migration; Bug B: GROUP BY 1 instead
    # of the ambiguous ``GROUP BY player_id``).
    scorers = database.get_top_scorers_by_side_for_tournament(1, limit=50)
    by_pid = {r["player_id"]: r for r in scorers}
    expect(
        10 in by_pid and by_pid[10]["home_goals"] == 2 and by_pid[10]["away_goals"] == 0,
        f"player 10 credited 2 home goals (got {by_pid.get(10)})",
    )
    expect(
        20 in by_pid and by_pid[20]["away_goals"] == 1 and by_pid[20]["home_goals"] == 0,
        f"player 20 credited 1 away goal (got {by_pid.get(20)})",
    )
    expect(
        all(r["total_goals"] == r["home_goals"] + r["away_goals"] for r in scorers),
        "total_goals == home_goals + away_goals for every row",
    )

    print()
    if failed:
        print(f"FAIL  {len(failed)} assertion(s) failed:")
        for m in failed:
            print(f"   - {m}")
        return 1
    print("All /tablebomb migration regression tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
