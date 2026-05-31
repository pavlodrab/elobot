"""
Unit tests for the db_backend SQL/schema translator.

Run with: python3 test_db_backend.py
"""
import sys

# Force the translator on by simulating Postgres mode.
import db_backend
db_backend.IS_POSTGRES = True

import importlib
importlib.reload(db_backend)  # no-op for the IS_POSTGRES flag, just to be safe.
db_backend.IS_POSTGRES = True


FAILED = []


def expect(cond, msg):
    if cond:
        print(f"  PASS  {msg}")
    else:
        print(f"  FAIL  {msg}")
        FAILED.append(msg)


def main():
    t = db_backend.translate_sql

    expect(
        t("SELECT * FROM x WHERE id=?") == "SELECT * FROM x WHERE id=%s",
        "qmark → %s in WHERE",
    )
    expect(
        t("INSERT INTO x (a,b) VALUES (?,?)") == "INSERT INTO x (a,b) VALUES (%s,%s)",
        "multiple qmarks",
    )
    expect(
        t("SELECT '?' FROM x WHERE id=?") == "SELECT '?' FROM x WHERE id=%s",
        "qmark inside string literal preserved",
    )
    expect(
        t("INSERT OR IGNORE INTO x (a) VALUES (?)").lower()
        == "insert into x (a) values (%s) on conflict do nothing",
        "INSERT OR IGNORE → ON CONFLICT DO NOTHING",
    )
    expect(
        "current_timestamp" in t("SELECT * FROM m WHERE deadline < datetime('now')").lower(),
        "datetime('now') → CURRENT_TIMESTAMP",
    )
    expect(
        "interval '6 hours'" in t(
            "deadline BETWEEN datetime('now') AND datetime('now', '+6 hours')"
        ).lower(),
        "datetime('now','+6 hours') → INTERVAL",
    )

    s = db_backend.translate_schema
    expect(
        "BIGSERIAL PRIMARY KEY" in s(
            "CREATE TABLE t (id INTEGER PRIMARY KEY AUTOINCREMENT, x INTEGER)"
        ),
        "INTEGER PRIMARY KEY AUTOINCREMENT → BIGSERIAL PRIMARY KEY",
    )
    expect(
        "BIGSERIAL PRIMARY KEY" in s(
            "CREATE TABLE p (id INTEGER PRIMARY KEY, name TEXT)"
        ),
        "INTEGER PRIMARY KEY → BIGSERIAL PRIMARY KEY",
    )
    expect(
        " BIGINT" in s("CREATE TABLE m (x INTEGER REFERENCES t(id))"),
        "non-PK INTEGER → BIGINT",
    )
    expect(
        "TIMESTAMP" in s("created_at DATETIME DEFAULT CURRENT_TIMESTAMP"),
        "DATETIME → TIMESTAMP",
    )
    expect(
        "DOUBLE PRECISION" in s("elo REAL DEFAULT 0"),
        "REAL → DOUBLE PRECISION",
    )

    # Idempotence: translating an already-translated query is a no-op
    # (well, not strictly, but the second pass shouldn't break things).
    once = t("SELECT * FROM x WHERE id=?")
    twice = t(once.replace("%s", "?"))
    expect(once == twice, "Translation is idempotent on round-trip")

    # SQLite mode (the no-op path)
    db_backend.IS_POSTGRES = False
    sql = "INSERT OR IGNORE INTO x (a) VALUES (?)"
    expect(db_backend.translate_sql(sql) == sql, "No translation when IS_POSTGRES=False")
    db_backend.IS_POSTGRES = True

    print()
    if FAILED:
        print(f"FAIL  {len(FAILED)} test(s) failed:")
        for m in FAILED:
            print(f"   - {m}")
        sys.exit(1)
    print("All db_backend tests passed.")


if __name__ == "__main__":
    main()
