"""
Tiny database backend abstraction.

Goal: keep the existing raw-SQL style of `database.py` working transparently
on both SQLite (local dev / single-file) and Postgres (Railway deploy).

How to use:
    from db_backend import connect, IS_POSTGRES, column_exists

    conn = connect()
    row = conn.execute("SELECT * FROM players WHERE id=?", (pid,)).fetchone()
    new_id = conn.insert_returning_id(
        "INSERT INTO players (username) VALUES (?)", ("alice",)
    )
    conn.commit()
    conn.close()

Backend selection:
- If `DATABASE_URL` env var is set (Postgres URL) → Postgres via psycopg2.
  Railway sets `DATABASE_URL` automatically when you add the Postgres plugin.
- Otherwise → SQLite at `DB_PATH` (default `./league.db`).

The wrapper handles:
- `?`  placeholders → `%s` for Postgres (string-literal aware)
- `INSERT OR IGNORE` → `INSERT … ON CONFLICT DO NOTHING`
- `datetime('now')` and `datetime('now', '+N hours')` → CURRENT_TIMESTAMP / INTERVAL
- Schema DDL: `INTEGER PRIMARY KEY [AUTOINCREMENT]` → `BIGSERIAL PRIMARY KEY`,
  `INTEGER` → `BIGINT`, `REAL` → `DOUBLE PRECISION`, `DATETIME` → `TIMESTAMP`
- `cursor.lastrowid` works on both backends via `insert_returning_id()`
"""
from __future__ import annotations

import os
import re
import sqlite3
from typing import Any, Iterable, Optional, Sequence


# Read DATABASE_URL exactly as Railway / Heroku / docker-compose set it.
# Any non-empty value flips the bot into Postgres mode — we don't enforce
# the URL prefix because some hosts hand out variants like ``postgres://``,
# ``postgresql+psycopg2://``, or templated URLs with the scheme injected later.
DATABASE_URL: str = os.getenv("DATABASE_URL", "").strip()
IS_POSTGRES: bool = bool(DATABASE_URL)
DB_PATH: str = os.getenv("DB_PATH", "league.db")


# ── psycopg2 import is lazy so the SQLite path doesn't require it ────────────
_psycopg2 = None
_RealDictCursor = None
if IS_POSTGRES:  # pragma: no cover - depends on env
    import psycopg2 as _psycopg2  # type: ignore
    from psycopg2.extras import RealDictCursor as _RealDictCursor  # type: ignore


# ── SQL translation ──────────────────────────────────────────────────────────

def _qmark_to_pct(sql: str) -> str:
    """
    Replace ``?`` placeholders with ``%s`` while leaving any ``?`` characters
    that appear inside SQL string literals alone.

    Note: we deliberately do NOT escape literal ``%`` characters to ``%%``.
    All SQL in this codebase uses ``?`` placeholders and never embeds raw
    ``%`` chars (LIKE patterns are passed as parameters). If you add SQL
    with a literal ``%`` someday, escape it yourself in the source string.
    """
    out: list[str] = []
    i = 0
    in_quote = False
    quote_ch: Optional[str] = None
    while i < len(sql):
        ch = sql[i]
        if in_quote:
            out.append(ch)
            if ch == quote_ch:
                # Doubled quote inside literal ('' or "") — stay in quote
                if i + 1 < len(sql) and sql[i + 1] == quote_ch:
                    i += 1
                    out.append(sql[i])
                else:
                    in_quote = False
                    quote_ch = None
        elif ch in "'\"":
            in_quote = True
            quote_ch = ch
            out.append(ch)
        elif ch == "?":
            out.append("%s")
        else:
            out.append(ch)
        i += 1
    return "".join(out)


_RE_DDL = re.compile(r"^\s*(CREATE\s+TABLE|ALTER\s+TABLE)\b", re.IGNORECASE)


def _looks_like_ddl(sql: str) -> bool:
    """Whether `sql` is a CREATE TABLE / ALTER TABLE statement we should
    pass through `translate_schema()` as well."""
    return bool(_RE_DDL.match(sql or ""))


_RE_INSERT_OR_IGNORE = re.compile(r"\bINSERT\s+OR\s+IGNORE\s+INTO\b", re.IGNORECASE)
_RE_DATETIME_NOW = re.compile(r"datetime\(\s*'now'\s*\)", re.IGNORECASE)
_RE_DATETIME_OFFSET = re.compile(
    r"datetime\(\s*'now'\s*,\s*'\+(\d+)\s+(hours|minutes|days)'\s*\)",
    re.IGNORECASE,
)


def translate_sql(sql: str) -> str:
    """Translate SQLite-flavored SQL to Postgres flavor (no-op on SQLite)."""
    if not IS_POSTGRES:
        return sql

    has_or_ignore = bool(_RE_INSERT_OR_IGNORE.search(sql))
    if has_or_ignore:
        sql = _RE_INSERT_OR_IGNORE.sub("INSERT INTO", sql)

    sql = _RE_DATETIME_NOW.sub("CURRENT_TIMESTAMP", sql)
    sql = _RE_DATETIME_OFFSET.sub(
        lambda m: f"(CURRENT_TIMESTAMP + INTERVAL '{m.group(1)} {m.group(2)}')",
        sql,
    )

    sql = _qmark_to_pct(sql)

    if has_or_ignore and "on conflict" not in sql.lower():
        sql = sql.rstrip(" \n\t;") + " ON CONFLICT DO NOTHING"
    return sql


def translate_schema(ddl: str) -> str:
    """Translate CREATE TABLE / ALTER TABLE DDL between SQLite and Postgres."""
    if not IS_POSTGRES:
        return ddl
    # Order matters: handle PRIMARY KEY forms first so we don't mangle "INTEGER".
    ddl = re.sub(
        r"\bINTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT\b",
        "__PK_AUTO__", ddl, flags=re.IGNORECASE,
    )
    ddl = re.sub(
        r"\bINTEGER\s+PRIMARY\s+KEY\b",
        "__PK_AUTO__", ddl, flags=re.IGNORECASE,
    )
    ddl = re.sub(r"\bINTEGER\b", "BIGINT", ddl, flags=re.IGNORECASE)
    ddl = re.sub(r"\bDATETIME\b", "TIMESTAMP", ddl, flags=re.IGNORECASE)
    ddl = re.sub(r"\bREAL\b", "DOUBLE PRECISION", ddl, flags=re.IGNORECASE)
    ddl = ddl.replace("__PK_AUTO__", "BIGSERIAL PRIMARY KEY")
    # Postgres datetime() functions used in DEFAULT clauses.
    ddl = _RE_DATETIME_NOW.sub("CURRENT_TIMESTAMP", ddl)
    return ddl


# ── Cursor / Connection wrappers ─────────────────────────────────────────────


class _CursorWrapper:
    """sqlite3 / psycopg2 cursor adapter with a tiny common surface."""

    def __init__(self, raw, backend: str):
        self._raw = raw
        self._backend = backend
        self.lastrowid: Optional[int] = None

    def execute(self, sql: str, params: Optional[Sequence[Any]] = None) -> "_CursorWrapper":
        sql_t = translate_sql(sql)
        if _looks_like_ddl(sql_t):
            sql_t = translate_schema(sql_t)
        if params is None or params == ():
            self._raw.execute(sql_t)
        else:
            self._raw.execute(sql_t, tuple(params))
        if self._backend == "sqlite":
            self.lastrowid = self._raw.lastrowid
        return self

    def fetchone(self):
        return self._raw.fetchone()

    def fetchall(self):
        return self._raw.fetchall()

    @property
    def rowcount(self) -> int:
        return getattr(self._raw, "rowcount", -1) or 0

    def close(self):
        try:
            self._raw.close()
        except Exception:
            pass


class Conn:
    """DB-agnostic connection wrapper. Mimics sqlite3.Connection's surface."""

    def __init__(self, raw, backend: str):
        self._raw = raw
        self.backend = backend

    # ── Cursor / execute ────────────────────────────────────────────────────
    def cursor(self) -> _CursorWrapper:
        if self.backend == "postgres":
            raw_cur = self._raw.cursor(cursor_factory=_RealDictCursor)
        else:
            raw_cur = self._raw.cursor()
        return _CursorWrapper(raw_cur, self.backend)

    def execute(self, sql: str, params: Optional[Sequence[Any]] = None) -> _CursorWrapper:
        cur = self.cursor()
        cur.execute(sql, params)
        return cur

    def executescript(self, ddl: str) -> None:
        """
        Execute multi-statement DDL. The schema string is translated to the
        target backend dialect (BIGSERIAL etc. for Postgres).
        """
        ddl_t = translate_schema(ddl)
        if self.backend == "sqlite":
            self._raw.executescript(ddl_t)
        else:
            cur = self._raw.cursor()
            try:
                cur.execute(ddl_t)
            finally:
                cur.close()

    def commit(self) -> None:
        self._raw.commit()

    def rollback(self) -> None:  # pragma: no cover
        self._raw.rollback()

    def close(self) -> None:
        try:
            self._raw.close()
        except Exception:
            pass

    # ── INSERT … RETURNING id helper ────────────────────────────────────────
    def insert_returning_id(
        self,
        sql: str,
        params: Optional[Sequence[Any]] = None,
    ) -> Optional[int]:
        """
        Run an INSERT and return the new row's `id`.

        On Postgres we append `RETURNING id` (if absent) and fetch the value.
        On SQLite we use `cursor.lastrowid`.
        """
        if self.backend == "postgres":
            if "returning" not in sql.lower():
                sql = sql.rstrip(" \n\t;") + " RETURNING id"
            cur = self.cursor()
            cur.execute(sql, params)
            row = cur.fetchone()
            if row is None:
                return None
            try:
                return int(row["id"])
            except (KeyError, TypeError):
                # Fallback: first value
                vals = list(row.values()) if hasattr(row, "values") else list(row)
                return int(vals[0]) if vals else None
        else:
            cur = self.cursor()
            cur.execute(sql, params)
            return cur.lastrowid


# ── Public connect() ─────────────────────────────────────────────────────────

def connect() -> Conn:
    """Open a new connection to the configured database.

    Re-reads ``DB_PATH`` / ``DATABASE_URL`` from the environment on every
    call so tests can swap the path at runtime via ``os.environ[...] = ...``.
    """
    if IS_POSTGRES:
        url = os.getenv("DATABASE_URL", DATABASE_URL).strip()
        # Some hosts (Railway, Heroku) hand out 'postgres://' which psycopg2
        # accepts but SQLAlchemy rejects. Normalize for clarity.
        if url.startswith("postgres://"):
            url = "postgresql://" + url[len("postgres://"):]
        raw = _psycopg2.connect(url)
        return Conn(raw, "postgres")
    db_path = os.getenv("DB_PATH", DB_PATH)
    raw = sqlite3.connect(db_path)
    raw.row_factory = sqlite3.Row
    raw.execute("PRAGMA foreign_keys = ON")
    return Conn(raw, "sqlite")


# ── Helpers used by migrations ───────────────────────────────────────────────

def column_exists(conn: Conn, table: str, column: str) -> bool:
    """Return True if `column` exists on `table` in this backend's catalog."""
    if conn.backend == "postgres":
        row = conn.execute(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = ? AND column_name = ? LIMIT 1",
            (table, column),
        ).fetchone()
        return bool(row)
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    # sqlite3.Row supports positional indexing; column 1 is `name`.
    return any(r[1] == column for r in rows)


def table_exists(conn: Conn, table: str) -> bool:
    if conn.backend == "postgres":
        row = conn.execute(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_name = ? LIMIT 1",
            (table,),
        ).fetchone()
        return bool(row)
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return bool(row)
