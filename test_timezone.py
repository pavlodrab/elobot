"""Smoke tests for the display-timezone helpers in ``handlers.common``.

The bot stores all timestamps as naive UTC (``datetime.utcnow()`` →
``"YYYY-MM-DD HH:MM:SS"``) and converts to the operator's display
timezone at the rendering boundary. Default display TZ is
``Europe/Moscow`` (МСК, UTC+3, no DST); override with ``BOT_DISPLAY_TZ``.

Run as a script (no pytest dependency):

    BOT_TOKEN=dummy ADMIN_IDS=111 python test_timezone.py
"""
from __future__ import annotations

import os
import sys
from datetime import datetime

os.environ.setdefault("BOT_TOKEN", "dummy")
os.environ.setdefault("ADMIN_IDS", "111")

# Make sure we test the *default* (Moscow) tz, even if a previous import
# already cached a different value.
os.environ["BOT_DISPLAY_TZ"] = "Europe/Moscow"

# Drop any cached handlers.common so the new BOT_DISPLAY_TZ takes effect.
for mod in [m for m in list(sys.modules) if m.startswith("handlers")]:
    del sys.modules[mod]


def main() -> int:
    from handlers.common import (
        _fmt_dt_local,
        _fmt_minute_local,
        _local_to_utc_str,
        _tz_label,
        _utc_to_local,
    )

    failures: list[str] = []

    def check(label: str, got, want) -> None:
        ok = got == want
        marker = "ok  " if ok else "FAIL"
        print(f"  {marker} | {label} (got {got!r}, want {want!r})")
        if not ok:
            failures.append(label)

    print("=== display timezone helpers ===")
    check("_tz_label() == 'МСК'", _tz_label(), "МСК")
    check(
        "UTC '2026-05-13 18:00:00' → local '2026-05-13 21:00:00'",
        _fmt_dt_local("2026-05-13 18:00:00"),
        "2026-05-13 21:00:00",
    )
    check(
        "UTC '2026-05-13 18:00:00' → minute-local '2026-05-13 21:00'",
        _fmt_minute_local("2026-05-13 18:00:00"),
        "2026-05-13 21:00",
    )
    check(
        "UTC '2026-12-31 23:30:00' → local '2027-01-01 02:30:00' (year roll)",
        _fmt_dt_local("2026-12-31 23:30:00"),
        "2027-01-01 02:30:00",
    )
    check(
        "Local '2026-05-13 21:00' → naive-UTC '2026-05-13 18:00:00'",
        _local_to_utc_str(datetime(2026, 5, 13, 21, 0)),
        "2026-05-13 18:00:00",
    )
    check(
        "Local '2027-01-01 02:30' → naive-UTC '2026-12-31 23:30:00' (year wrap)",
        _local_to_utc_str(datetime(2027, 1, 1, 2, 30)),
        "2026-12-31 23:30:00",
    )
    check(
        "Round-trip UTC → local → UTC preserves the original",
        _local_to_utc_str(_utc_to_local("2026-05-13 18:00:00").replace(tzinfo=None)),
        "2026-05-13 18:00:00",
    )
    check(
        "Empty input → '' (no crash)",
        _fmt_minute_local(""),
        "",
    )
    check(
        "None input → '' (no crash)",
        _fmt_minute_local(None),
        "",
    )

    # Verify the deadline-token parser stores UTC even though the user
    # types local time. Imported lazily to avoid pulling all of handlers
    # if we just want to test the tz core.
    from handlers.match import _parse_deadline_token
    print("\n=== _parse_deadline_token timezone semantics ===")
    out, err = _parse_deadline_token(["2026-05-13", "21:00"])
    check("absolute '2026-05-13 21:00' parsed without error", err, None)
    check(
        "stored as UTC '2026-05-13 18:00:00' (3h shift)",
        out,
        "2026-05-13 18:00:00",
    )
    out, err = _parse_deadline_token(["+24"])
    check("relative '+24' still parses", err, None)
    check("relative result is a 19-char timestamp", len(out or ""), 19)

    if failures:
        print(f"\nFAIL  {len(failures)} test(s) failed:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("\nALL TIMEZONE TESTS PASSED.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
