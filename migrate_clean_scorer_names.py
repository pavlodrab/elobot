#!/usr/bin/env python3
"""One-time migration: clean existing match_goals.raw_name entries.

Fixes two issues in historical data:
1. Removes trailing 'ГОЛ'/'GOAL' suffix left by AI OCR in raw_name.
2. Logs entries that would benefit from deduplication (for manual review).

Usage:
    python migrate_clean_scorer_names.py          # dry-run (preview)
    python migrate_clean_scorer_names.py --apply  # apply changes to DB

Safe to run multiple times — idempotent.
"""
from __future__ import annotations

import re
import sys

# ── Same cleaning logic as bot.py _clean_raw_scorer_name ─────────────────────
_GOL_SUFFIX_RE = re.compile(
    r"\s+(?:ГОЛ|GOAL|Гол|gol|GOL|гол)\.?\s*$",
    re.IGNORECASE,
)
_GOL_GLUED_RE = re.compile(
    r"(?<=[a-zа-яёé])"
    r"(?:ГОЛ|GOAL|Гол|gol|GOL|гол)\.?\s*$",
    re.IGNORECASE,
)


def clean_raw_name(name: str) -> str:
    """Strip trailing ГОЛ/GOAL suffix and trim."""
    if not name:
        return name
    s = name.strip()
    s = _GOL_SUFFIX_RE.sub("", s)
    s = _GOL_GLUED_RE.sub("", s)
    s = s.rstrip(" .\t")
    return s


def main():
    dry_run = "--apply" not in sys.argv

    if dry_run:
        print("=== DRY RUN (pass --apply to commit changes) ===\n")
    else:
        print("=== APPLYING CHANGES ===\n")

    # Import database module (needs to be run from project root)
    sys.path.insert(0, ".")
    import database as db

    conn = db.get_conn()

    # Fetch all match_goals with non-empty raw_name
    rows = conn.execute(
        "SELECT id, raw_name FROM match_goals WHERE raw_name IS NOT NULL AND raw_name != ''"
    ).fetchall()

    updates: list[tuple[str, int]] = []
    for row in rows:
        goal_id = row["id"]
        original = row["raw_name"]
        cleaned = clean_raw_name(original)
        if cleaned != original:
            updates.append((cleaned, goal_id))

    if not updates:
        print("No entries need cleaning. Database is already clean!")
        conn.close()
        return

    print(f"Found {len(updates)} entries to clean:\n")
    for cleaned, goal_id in updates:
        original = next(r["raw_name"] for r in rows if r["id"] == goal_id)
        print(f"  ID {goal_id:5d}: {original!r:30} -> {cleaned!r}")

    if dry_run:
        print(f"\n(dry run — {len(updates)} rows would be updated)")
    else:
        for cleaned, goal_id in updates:
            conn.execute(
                "UPDATE match_goals SET raw_name = ? WHERE id = ?",
                (cleaned, goal_id),
            )
        conn.commit()
        print(f"\n✅ Updated {len(updates)} rows in match_goals.")

    conn.close()


if __name__ == "__main__":
    main()
