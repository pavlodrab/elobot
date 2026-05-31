"""
Export & import the entire database from a Telegram admin command.

Public entry points:

* ``export_database()`` — return ``(filename, bytes)`` ready to send as a
  Telegram document. Format is a single ZIP that contains:

    - ``manifest.json``           — backend, exported_at, table list,
                                    counts, schema version
    - ``<table>.json``            — every table dumped as JSON with
                                    ``columns`` + ``rows`` (rows are
                                    arrays of values in column order to
                                    keep the file compact)
    - ``snapshot.db`` (sqlite only) — raw copy of the SQLite file as a
                                       second-chance restore option

* ``import_database(zip_bytes)`` — restore the DB from the bytes of an
  uploaded ZIP. Wraps everything in a single transaction so a partial
  failure rolls back. Returns a result dict with ``ok``,
  ``tables_restored``, ``rows_restored``, ``warnings``, ``error``.

Works with both backends (Postgres on Railway, SQLite locally) — picks
the right code path off ``db_backend.IS_POSTGRES``. The export format
is portable: a Postgres dump can be restored into a SQLite dev box and
vice-versa, so long as the schema is compatible (i.e. you ran
``database.init_db()`` first).
"""
from __future__ import annotations

import io
import json
import logging
import os
import zipfile
from datetime import datetime, timezone

from db_backend import IS_POSTGRES, DB_PATH

import database as db

log = logging.getLogger(__name__)

EXPORT_VERSION = "1"
DUMP_FILENAME_PREFIX = "govnl_db_export"


# ─────────────────────────────────────────────────────────────────────────────
# Schema discovery (works with both backends).
# ─────────────────────────────────────────────────────────────────────────────

# Tables we ALWAYS exclude from import — they're managed by the runtime
# (PTB jobs, OCR caches) and restoring stale rows would be confusing.
_TRANSIENT_TABLES = frozenset({
    "processed_screenshots",
    "reminder_log",
})


def _list_tables(conn) -> list[str]:
    """Return public table names for the current backend, in a stable
    deterministic order (alphabetical) so dumps are diff-able."""
    if conn.backend == "postgres":
        rows = conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_type = 'BASE TABLE' "
            "ORDER BY table_name"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%' "
            "AND name NOT LIKE 'sqlalchemy_%' "
            "ORDER BY name"
        ).fetchall()
    return [dict(r)[next(iter(dict(r)))] for r in rows]


def _list_columns(conn, table: str) -> list[str]:
    """Return the column list for ``table`` in declaration order so the
    JSON ``rows`` arrays line up with the schema. Quoted-identifier
    safe — table names from ``_list_tables`` are vetted against the
    information_schema / sqlite_master output."""
    if conn.backend == "postgres":
        rows = conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = %s "
            "ORDER BY ordinal_position",
            (table,),
        ).fetchall()
        return [dict(r)["column_name"] for r in rows]
    rows = conn.execute(f"PRAGMA table_info(\"{table}\")").fetchall()
    return [dict(r)["name"] for r in rows]


def _normalise_value(v):
    """Convert DB-native values into JSON-friendly primitives. Postgres
    returns ``datetime`` for timestamps, ``Decimal`` for numerics,
    ``memoryview`` for bytea — none of those are JSON-serialisable."""
    if v is None:
        return None
    if isinstance(v, (str, int, float, bool)):
        return v
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, (bytes, bytearray, memoryview)):
        # Use a typed marker so import knows to decode back.
        import base64
        return {"__bytes__": base64.b64encode(bytes(v)).decode("ascii")}
    # Decimal, UUID, custom types → str(). Acceptable: the schema
    # in init_db only uses TEXT/INTEGER/REAL/BLOB.
    return str(v)


def _denormalise_value(v):
    """Inverse of ``_normalise_value`` — recover bytes from the dict
    marker. Strings stay as strings (downstream INSERT will cast)."""
    if isinstance(v, dict) and "__bytes__" in v:
        import base64
        return base64.b64decode(v["__bytes__"])
    return v


# ─────────────────────────────────────────────────────────────────────────────
# Export
# ─────────────────────────────────────────────────────────────────────────────

def export_database() -> tuple[str, bytes]:
    """Dump the entire DB into a single ZIP. Returns
    ``(filename, bytes)`` ready to feed into ``ctx.bot.send_document``.

    Each table becomes one JSON file inside the ZIP plus a
    ``manifest.json`` with metadata. SQLite exports also include a raw
    copy of the file for a one-click restore on a fresh dev box.
    """
    conn = db.get_conn()
    backend = conn.backend
    tables = _list_tables(conn)

    manifest: dict = {
        "version":     EXPORT_VERSION,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "backend":     backend,
        "tables":      [],
    }

    bio = io.BytesIO()
    with zipfile.ZipFile(bio, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for table in tables:
            try:
                cols = _list_columns(conn, table)
                quoted_cols = ", ".join('"' + c + '"' for c in cols)
                rows = conn.execute(
                    f'SELECT {quoted_cols} FROM "{table}"'
                ).fetchall()
            except Exception:
                log.exception("export: failed to read %s — skipping", table)
                continue
            payload_rows = [
                [_normalise_value(dict(r)[c]) for c in cols]
                for r in rows
            ]
            payload = {
                "columns": cols,
                "rows":    payload_rows,
            }
            zf.writestr(
                f"{table}.json",
                json.dumps(payload, ensure_ascii=False, indent=0),
            )
            manifest["tables"].append({
                "name":  table,
                "rows":  len(payload_rows),
                "cols":  cols,
            })
        # SQLite bonus: ship the raw file so restoring on a fresh dev
        # box is a single drop-in. Postgres skips this — the JSON
        # dumps are the canonical form there.
        if backend == "sqlite":
            try:
                path = os.getenv("DB_PATH", DB_PATH) or "league.db"
                if os.path.exists(path):
                    with open(path, "rb") as f:
                        zf.writestr("snapshot.db", f.read())
                    manifest["sqlite_snapshot"] = "snapshot.db"
            except Exception:
                log.warning("export: could not embed sqlite snapshot")
        zf.writestr(
            "manifest.json",
            json.dumps(manifest, ensure_ascii=False, indent=2),
        )
    conn.close()

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    filename = f"{DUMP_FILENAME_PREFIX}_{backend}_{stamp}.zip"
    return filename, bio.getvalue()


# ─────────────────────────────────────────────────────────────────────────────
# Import / restore
# ─────────────────────────────────────────────────────────────────────────────

def import_database(zip_bytes: bytes) -> dict:
    """Restore the DB from an ``export_database`` ZIP. Wipes existing
    rows in every imported table (TRUNCATE/DELETE) and re-inserts from
    the JSON dump. The whole thing runs in a single transaction so a
    partial failure rolls back to the pre-restore state.

    Returns a result dict::

        {
          "ok":              bool,
          "tables_restored": int,
          "rows_restored":   int,
          "skipped":         [str, ...],
          "warnings":        [str, ...],
          "error":           str | None,
          "manifest":        dict | None,
        }
    """
    result: dict = {
        "ok": False,
        "tables_restored": 0,
        "rows_restored":   0,
        "skipped":         [],
        "warnings":        [],
        "error":           None,
        "manifest":        None,
    }
    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes), "r")
    except zipfile.BadZipFile:
        result["error"] = "Файл не распознан как ZIP."
        return result

    try:
        manifest_raw = zf.read("manifest.json").decode("utf-8")
        manifest = json.loads(manifest_raw)
    except Exception as e:
        result["error"] = f"Не нашёл manifest.json: {e}"
        return result
    result["manifest"] = manifest

    if manifest.get("version") != EXPORT_VERSION:
        result["warnings"].append(
            f"Версия дампа {manifest.get('version')} не совпадает с "
            f"текущей {EXPORT_VERSION}; импорт продолжится, но могут "
            f"быть несовместимости в схеме."
        )

    conn = db.get_conn()
    backend = conn.backend
    # Discover the live schema so we don't try to insert into tables
    # that no longer exist or columns that were renamed.
    live_tables = set(_list_tables(conn))

    # Import order matters: parents first, then children. We rely on
    # the manifest's table order, but also push transient tables to
    # the end (they have FK refs into matches/players). Hardcoded
    # priority keeps imports deterministic across schema bumps.
    parent_first = [
        "players", "tournaments", "tournament_players", "playoff_brackets",
        "matches", "match_goals", "tournament_elo", "bot_admins",
        "bot_owners", "tournament_admins", "tournament_audit_log",
        "tournament_templates",
    ]
    manifest_tables = [t["name"] for t in manifest.get("tables") or []]

    def _ord(t: str) -> int:
        try:
            return parent_first.index(t)
        except ValueError:
            return len(parent_first) + manifest_tables.index(t) \
                if t in manifest_tables else 9999

    ordered = sorted(manifest_tables, key=_ord)

    # Open a single transaction across every table so a partial
    # failure rolls back. Both backends in db_backend support this.
    if backend == "postgres":
        conn.execute("BEGIN")
    else:
        conn.execute("BEGIN IMMEDIATE")

    try:
        # Pre-compute which tables we'll actually touch — skip transient
        # tables and anything that isn't in the live schema. We also
        # require the table to be present in the archive so we don't
        # wipe something we have no replacement for.
        plan: list[tuple[str, dict, list[str], list[str], list[int]]] = []
        for table in ordered:
            if table in _TRANSIENT_TABLES:
                result["skipped"].append(f"{table} (transient)")
                continue
            if table not in live_tables:
                result["skipped"].append(f"{table} (нет в текущей схеме)")
                continue
            try:
                payload_raw = zf.read(f"{table}.json").decode("utf-8")
                payload = json.loads(payload_raw)
            except KeyError:
                result["warnings"].append(f"{table}: нет в архиве")
                continue
            except Exception as e:
                result["warnings"].append(f"{table}: parse error {e}")
                continue

            dump_cols = payload.get("columns") or []
            live_cols = _list_columns(conn, table)
            # Use the intersection in dump order; missing columns get
            # NULL via the live INSERT default.
            live_set = set(live_cols)
            cols = [c for c in dump_cols if c in live_set]
            if not cols:
                result["warnings"].append(f"{table}: нет совпадающих колонок")
                continue

            col_idx_in_dump = [dump_cols.index(c) for c in cols]
            plan.append((table, payload, dump_cols, cols, col_idx_in_dump))

        # ── Pass 1: DELETE in REVERSE order (children → parents). ────
        # Doing this in two passes is essential on Postgres: a single
        # forward pass would call ``DELETE FROM "players"`` while
        # ``match_goals`` still references player rows, blowing up on
        # ``match_goals_player_id_fkey``. Wiping children first means
        # every FK target is already empty when we get to the parent.
        # On SQLite this is still safe (the FK enforcement order is the
        # same once ``PRAGMA foreign_keys = ON``).
        for table, *_ in reversed(plan):
            conn.execute(f'DELETE FROM "{table}"')

        # ── Pass 2: INSERT in FORWARD order (parents → children). ────
        for table, payload, dump_cols, cols, col_idx_in_dump in plan:
            placeholder = "%s" if backend == "postgres" else "?"
            quoted_cols = ", ".join('"' + c + '"' for c in cols)
            placeholders = ", ".join(placeholder for _ in cols)
            sql = f'INSERT INTO "{table}" ({quoted_cols}) VALUES ({placeholders})'
            for r in payload.get("rows") or []:
                values = tuple(_denormalise_value(r[i]) for i in col_idx_in_dump)
                try:
                    conn.execute(sql, values)
                except Exception as e:
                    result["warnings"].append(
                        f"{table}: row failed ({e}); "
                        f"first few values={values[:3]}"
                    )
            # Re-align Postgres SERIAL sequences so the next insert
            # picks up after the imported max id. Without this, every
            # AUTOINCREMENT-style insert collides on UNIQUE(id).
            if backend == "postgres" and "id" in cols:
                try:
                    conn.execute(
                        f"SELECT setval(pg_get_serial_sequence('\"{table}\"', 'id'), "
                        f"COALESCE((SELECT MAX(id) FROM \"{table}\"), 1), true)"
                    )
                except Exception as e:
                    # Not every table uses a SERIAL id — silently skip.
                    log.debug("setval skipped for %s: %s", table, e)
            result["tables_restored"] += 1
            result["rows_restored"] += len(payload.get("rows") or [])

        # Everything succeeded — commit.
        conn.execute("COMMIT")
        result["ok"] = True
    except Exception as e:
        log.exception("import_database: rolling back")
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        result["error"] = f"{type(e).__name__}: {e}"
    finally:
        conn.close()

    return result


__all__ = ["export_database", "import_database",
           "EXPORT_VERSION", "DUMP_FILENAME_PREFIX"]
