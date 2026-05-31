"""
Tests for tournament ↔ chat binding and the photo-filter resolver.

Exercises:
- set_tournament_chat / get_tournament_by_chat round-trip.
- find_tournaments_by_name_substring matches case-insensitively.
- resolve_tournament_for_photo prioritises caption ID > caption name >
  chat binding > None (silent skip).

Standalone — no telegram or external deps. Uses an isolated temp DB.
"""
from __future__ import annotations

import os
import sys
import tempfile

# Force isolated DB BEFORE importing the bot modules.
TMPDIR = tempfile.mkdtemp(prefix="fc_chat_bind_test_")
DB_PATH = os.path.join(TMPDIR, "league.db")
os.environ["DB_PATH"] = DB_PATH
# `bot.py` requires BOT_TOKEN at import-time; provide a dummy.
os.environ.setdefault("BOT_TOKEN", "dummy")

import database as db  # noqa: E402
import bot  # noqa: E402

FAILED: list[str] = []


def expect(cond, msg):
    if cond:
        print(f"  PASS  {msg}")
    else:
        print(f"  FAIL  {msg}")
        FAILED.append(msg)


def main():
    print(f"Using temp DB: {DB_PATH}")
    db.init_db()

    alice = db.upsert_player("alice", telegram_id=1)
    bob = db.upsert_player("bob", telegram_id=2)

    # Two distinct active tournaments.
    t1 = db.create_tournament(
        "Тур Гвардиолыча", tournament_type="vsa",
        created_by=alice["id"], is_official=False,
    )
    t2 = db.create_tournament(
        "Кубок 27371", tournament_type="ri",
        created_by=bob["id"], is_official=False,
    )
    db.add_player_to_tournament(t1, alice["id"], "A")
    db.add_player_to_tournament(t1, bob["id"], "A")
    db.add_player_to_tournament(t2, alice["id"], "A")
    db.add_player_to_tournament(t2, bob["id"], "A")

    # ── 1) Chat binding round-trip ──────────────────────────────────────────
    chat_a = -1001000000001
    chat_b = -1001000000002

    expect(db.get_tournament_by_chat(chat_a) is None,
           "No binding initially → get_tournament_by_chat returns None")

    db.set_tournament_chat(t1, chat_a)
    bound = db.get_tournament_by_chat(chat_a)
    expect(bound is not None and bound["id"] == t1,
           "set_tournament_chat → get_tournament_by_chat finds t1")

    expect(db.get_tournament_by_chat(chat_b) is None,
           "Other chat is still unbound")

    # Bind a different tournament to the same chat — last write wins.
    db.set_tournament_chat(t2, chat_a)
    re_bound = db.get_tournament_by_chat(chat_a)
    expect(
        re_bound is not None and re_bound["id"] == t2,
        "Re-binding same chat to t2 overrides the previous binding",
    )

    db.unset_tournament_chat(t2)
    expect(db.get_tournament_by_chat(chat_a) is None,
           "unset_tournament_chat clears binding")

    # Bind t1 again so resolver has something to find.
    db.set_tournament_chat(t1, chat_a)

    # ── 2) Substring search ─────────────────────────────────────────────────
    found = db.find_tournaments_by_name_substring("гвардиолыча")
    expect(any(t["id"] == t1 for t in found),
           "find_tournaments_by_name_substring case-insensitive match")
    expect(db.find_tournaments_by_name_substring("nothinglikethat") == [],
           "No match → empty list")
    expect(db.find_tournaments_by_name_substring("") == [],
           "Empty query → empty list")

    # ── 3) Photo resolver — caption ID wins ─────────────────────────────────
    r = bot.resolve_tournament_for_photo(chat_a, f"#турнир {t2}")
    expect(r is not None and r["id"] == t2,
           "Caption '#турнир ID' overrides chat binding")

    r = bot.resolve_tournament_for_photo(None, f"#{t2}")
    expect(r is not None and r["id"] == t2, "Bare '#ID' resolves tournament")

    r = bot.resolve_tournament_for_photo(None, f"тур {t1}")
    expect(r is not None and r["id"] == t1, "'тур ID' resolves tournament")

    r = bot.resolve_tournament_for_photo(None, f"tournament {t2}")
    expect(r is not None and r["id"] == t2, "English 'tournament ID' resolves")

    # ── 4) Photo resolver — caption name substring ──────────────────────────
    r = bot.resolve_tournament_for_photo(None, "Тур Гвардиолыча матч 1")
    expect(r is not None and r["id"] == t1,
           "Caption containing tournament name substring resolves to t1")

    r = bot.resolve_tournament_for_photo(None, "что-то про Гвардиолыча")
    expect(r is not None and r["id"] == t1,
           "Caption with name token resolves to t1")

    # ── 5) Photo resolver — chat binding fallback (DM-only) ───────────────
    # By default, allow_chat_binding=True (DM path): bound chat resolves
    # even with empty/generic caption.
    r = bot.resolve_tournament_for_photo(chat_a, "")
    expect(r is not None and r["id"] == t1,
           "DM path: no caption, but chat is bound → falls back to chat binding")

    r = bot.resolve_tournament_for_photo(chat_a, "просто скрин")
    expect(r is not None and r["id"] == t1,
           "DM path: generic caption + bound chat → chat binding")

    # In groups the handler passes ``allow_chat_binding=False`` so the
    # binding fallback is disabled — random screenshots without an
    # explicit tournament reference in the caption must be skipped.
    r = bot.resolve_tournament_for_photo(chat_a, "", allow_chat_binding=False)
    expect(r is None,
           "Group path: no caption + bound chat → silent skip (no auto-resolve)")

    r = bot.resolve_tournament_for_photo(
        chat_a, "просто скрин", allow_chat_binding=False,
    )
    expect(r is None,
           "Group path: irrelevant caption + bound chat → silent skip")

    # Even in group mode an explicit caption ID/name still wins.
    r = bot.resolve_tournament_for_photo(
        chat_a, f"#турнир {t1}", allow_chat_binding=False,
    )
    expect(r is not None and r["id"] == t1,
           "Group path: explicit '#турнир ID' still resolves")

    # ── 6) Photo resolver — silent skip ────────────────────────────────────
    r = bot.resolve_tournament_for_photo(None, "")
    expect(r is None, "No caption + no chat → silent skip (None)")

    r = bot.resolve_tournament_for_photo(chat_b, "")
    expect(r is None, "Unbound chat + no caption → silent skip (None)")

    r = bot.resolve_tournament_for_photo(chat_b, "просто скрин")
    expect(r is None, "Unbound chat + irrelevant caption → silent skip (None)")

    r = bot.resolve_tournament_for_photo(None, "#турнир 99999")
    expect(r is None, "Caption with non-existent ID → silent skip (None)")

    # Auto-bind on /create_tournament: emulated by passing chat_id=
    new_tid = db.create_tournament(
        "Auto-bound", tournament_type="vsa",
        created_by=alice["id"], is_official=False,
        chat_id=chat_b,
    )
    bound2 = db.get_tournament_by_chat(chat_b)
    expect(
        bound2 is not None and bound2["id"] == new_tid,
        "create_tournament(chat_id=...) auto-binds the chat",
    )

    # ── 7) Photo handler — group vs DM gating ──────────────────────────────
    # Strict skip ONLY applies to groups/channels; DMs fall back to OCR.
    # We can't easily simulate the real Telegram chat object here, but we
    # CAN check the helper used by the handler — if the resolver returns
    # None, the handler decides per-chat-type whether to skip. Document
    # the contract here: resolver returns None for unbound chats.
    expect(
        bot.resolve_tournament_for_photo(
            None, "matchresult.png attached"
        ) is None,
        "Generic caption from DM (no chat_id) → resolver returns None "
        "(handler will fall back to OCR-based detection)",
    )

    print()
    if FAILED:
        print(f"FAIL  {len(FAILED)} test(s) failed:")
        for m in FAILED:
            print(f"   - {m}")
        sys.exit(1)
    print("All chat-binding tests passed.")


if __name__ == "__main__":
    main()
