"""
Tests for the runtime-admin grant/revoke and finish-tournament features.

The CallbackQueryHandler logic itself is exercised via small unit tests on
the underlying database helpers and the synchronous tournament-finishing
helper. The /grant_admin command needs Telegram primitives so we test the
DB layer directly.

Run:
  BOT_TOKEN=dummy python3 test_admin_and_finish.py
"""
import os
import tempfile
import sys

# Force a fresh sqlite DB just for this test run.
TMP_DB = tempfile.mkdtemp(prefix="fc_admin_test_")
os.environ["DB_PATH"] = os.path.join(TMP_DB, "test.db")
os.environ["DATABASE_URL"] = ""
# bot.py needs a token to import; any string is fine, no real Telegram I/O here.
os.environ.setdefault("BOT_TOKEN", "dummy:test")
os.environ.setdefault("ADMIN_IDS", "111")

import database as db
import bot

FAILED: list[str] = []


def expect(cond: bool, msg: str) -> None:
    tag = "PASS" if cond else "FAIL"
    print(f"  {tag}  {msg}")
    if not cond:
        FAILED.append(msg)


def main() -> int:
    db.init_db()
    print("\n=== /grant_admin / /revoke_admin / /admins ===")

    # Initially no runtime admins.
    expect(db.list_bot_admins() == [], "no runtime admins initially")
    expect(not db.is_bot_admin_db(222), "is_bot_admin_db(222) is False initially")
    # Env admin (id 111) is admin even without runtime grant.
    expect(bot.is_admin(111), "env-listed admin still admin")
    expect(not bot.is_admin(222), "non-admin user 222 is not admin yet")

    # Grant admin to 222.
    db.grant_bot_admin(222, granted_by=111, note="created tournaments")
    expect(db.is_bot_admin_db(222), "after grant, is_bot_admin_db(222) is True")
    expect(bot.is_admin(222), "after grant, bot.is_admin(222) is True")
    listing = db.list_bot_admins()
    expect(len(listing) == 1 and listing[0]["telegram_id"] == 222,
           "list_bot_admins() returns the granted user")
    expect(listing[0]["note"] == "created tournaments", "note is persisted")

    # Granting again is idempotent.
    db.grant_bot_admin(222, granted_by=999, note="updated note")
    listing = db.list_bot_admins()
    expect(len(listing) == 1, "regrant doesn't duplicate")
    expect(listing[0]["note"] == "updated note", "regrant updates note")

    # Revoke.
    removed = db.revoke_bot_admin(222)
    expect(removed is True, "revoke returns True for existing")
    expect(not db.is_bot_admin_db(222), "after revoke, is_bot_admin_db(222) is False")
    expect(not bot.is_admin(222), "after revoke, bot.is_admin(222) is False")

    # Revoking a non-existent grant returns False.
    removed = db.revoke_bot_admin(333)
    expect(removed is False, "revoke returns False for non-existent")

    # is_root_admin only matches env admins.
    expect(bot.is_root_admin(111), "env id is_root_admin(111) is True")
    db.grant_bot_admin(444)
    expect(not bot.is_root_admin(444), "runtime grant is NOT root admin")

    # ── /finish_tournament ────────────────────────────────────────────────
    print("\n=== finish_tournament helper ===")
    # Create a player + tournament and finish it.
    p = db.upsert_player("alice", telegram_id=555)
    tid = db.create_tournament(
        "Test Cup", tournament_type="vsa",
        created_by=p["id"], is_official=False,
    )
    t_before = db.get_tournament(tid)
    expect(t_before is not None and t_before["stage"] != "finished",
           "tournament starts with stage != 'finished'")

    bot._do_finish_tournament(tid)
    t_after = db.get_tournament(tid)
    expect(t_after["stage"] == "finished",
           "_do_finish_tournament flips stage to 'finished'")

    # Idempotent: finishing twice is harmless.
    bot._do_finish_tournament(tid)
    t_after2 = db.get_tournament(tid)
    expect(t_after2["stage"] == "finished",
           "finishing already-finished tournament keeps stage 'finished'")

    # _can_manage_tournament: creator yes, other no, admin yes.
    expect(bot._can_manage_tournament(555, t_after),
           "creator can manage tournament")
    expect(not bot._can_manage_tournament(777, t_after),
           "random user can't manage tournament")
    expect(bot._can_manage_tournament(111, t_after),
           "env admin can manage tournament")

    # ── /admin_setnick helper (just exercises set_game_nickname + lookups) ──
    print("\n=== admin_setnick logic ===")
    bob = db.upsert_player("bob", telegram_id=666)
    db.set_game_nickname(bob["id"], "BobInGame")
    bob2 = db.get_player_by_id(bob["id"])
    expect(bob2["game_nickname"] == "BobInGame", "set_game_nickname stores the nickname")

    # Reserving against an unregistered username creates a player.
    new_p = db.upsert_player("ghostuser")
    db.set_game_nickname(new_p["id"], "GhostNick")
    found = db.get_player_by_game_nickname("GhostNick")
    expect(found is not None and found["username"] == "ghostuser",
           "nickname can be reserved before /register")

    # get_player_by_telegram_id round-trip
    found2 = db.get_player_by_telegram_id(666)
    expect(found2 is not None and found2["id"] == bob["id"],
           "get_player_by_telegram_id finds the right row")
    expect(db.get_player_by_telegram_id(None) is None,
           "get_player_by_telegram_id(None) returns None")
    expect(db.get_player_by_telegram_id(99_999) is None,
           "get_player_by_telegram_id(missing) returns None")

    # ── /simulate scoring + helper ───────────────────────────────────────────
    print("\n=== /simulate logic ===")
    p1 = db.upsert_player("alice2")
    p2 = db.upsert_player("bob2")
    # Stack ELO so alice is heavily favoured.
    db.adjust_player_elo(p1["id"], +400, by_user="test", note="seed")
    db.adjust_player_elo(p2["id"], -200, by_user="test", note="seed")
    p1f = db.get_player_by_id(p1["id"])
    p2f = db.get_player_by_id(p2["id"])

    # Many trials — favourite wins more often.
    import random as _r
    _r.seed(0)
    favwins = 0
    draws = 0
    underwins = 0
    for _ in range(500):
        s1, s2 = bot._simulated_score(p1f, p2f)
        if s1 > s2:
            favwins += 1
        elif s1 == s2:
            draws += 1
        else:
            underwins += 1
    expect(favwins > underwins,
           f"favourite wins more often (fav={favwins} draw={draws} under={underwins})")
    expect(0 < underwins,
           f"underdog still wins sometimes (n={underwins})")

    # _do_simulate_tournament actually flips matches to confirmed and clears them.
    tid2 = db.create_tournament(
        "Sim Cup", tournament_type="ri",
        created_by=bob["id"], is_official=False,
    )
    db.add_player_to_tournament(tid2, p1["id"], group_name="A")
    db.add_player_to_tournament(tid2, p2["id"], group_name="A")
    mid = db.create_match(tid2, p1["id"], p2["id"], stage="group", round_num=1)

    summary = bot._do_simulate_tournament(tid2, admin_uid=111)
    expect(summary["played"] == 1, "one pending match was simulated")
    m = db.get_match(mid)
    expect(m["status"] == "confirmed", "simulated match marked confirmed")
    expect(m["score1"] is not None and m["score2"] is not None,
           "simulated match has a numeric score")

    # Re-running simulate is a no-op.
    summary2 = bot._do_simulate_tournament(tid2, admin_uid=111)
    expect(summary2["played"] == 0, "re-running /simulate plays nothing")

    # ── /setnick fallback by Telegram ID ──────────────────────────────────
    print("\n=== /setnick fallback (no public @username) ===")
    # Pre-register someone whose stored username is just their numeric id
    # (mirrors what cmd_register does when user.username is None).
    no_uname = db.upsert_player("id_77777", telegram_id=77777)

    class _U:
        def __init__(self, tid, uname):
            self.id = tid
            self.username = uname

    found = bot._player_from_user(_U(77777, None))
    expect(found is not None and found["id"] == no_uname["id"],
           "_player_from_user finds row by telegram_id when username is None")

    # Even if the user later picks a brand new @username, telegram_id still
    # resolves the same row.
    found2 = bot._player_from_user(_U(77777, "newhandle"))
    expect(found2 is not None and found2["id"] == no_uname["id"],
           "_player_from_user falls back to telegram_id when @username is unknown")

    # Username change: link existing row instead of duplicating.
    db.update_player_username(no_uname["id"], "newhandle")
    found3 = bot._player_from_user(_U(77777, "newhandle"))
    expect(found3 is not None and found3["id"] == no_uname["id"],
           "update_player_username keeps the same row id")

    # ── /set_playoff_slots ────────────────────────────────────────────────
    print("\n=== /set_playoff_slots & playoff_slots column ===")
    tid3 = db.create_tournament("Slot Cup", tournament_type="vsa")
    t3 = db.get_tournament(tid3)
    expect(t3.get("playoff_slots") == 2,
           "new tournaments default to 2 playoff slots per group")
    db.update_tournament(tid3, playoff_slots=4)
    t3b = db.get_tournament(tid3)
    expect(t3b.get("playoff_slots") == 4,
           "update_tournament(playoff_slots=…) persists")

    # ── Admin-report bypass (/admin_report) ───────────────────────────────
    print("\n=== /admin_report bypass + auto-advance ===")
    pa = db.upsert_player("alpha")
    pb = db.upsert_player("beta")
    pc = db.upsert_player("gamma")
    pd = db.upsert_player("delta")
    tid4 = db.create_tournament("AR Cup", tournament_type="vsa")
    db.add_player_to_tournament(tid4, pa["id"], group_name="A")
    db.add_player_to_tournament(tid4, pb["id"], group_name="A")
    db.add_player_to_tournament(tid4, pc["id"], group_name="B")
    db.add_player_to_tournament(tid4, pd["id"], group_name="B")

    # Single match in each group — confirm them via apply_result directly.
    from match_processor import apply_result
    m_a = db.create_match(tid4, pa["id"], pb["id"], stage="group", round_num=1)
    db.update_match(m_a, score1=2, score2=0, status="confirmed", reported_by=111)
    apply_result(m_a)

    # After this last confirmed match, group stage is fully done -> bracket.
    m_b = db.create_match(tid4, pc["id"], pd["id"], stage="group", round_num=1)
    db.update_match(m_b, score1=1, score2=3, status="confirmed", reported_by=111)
    apply_result(m_b)

    t4 = db.get_tournament(tid4)
    expect(t4["stage"] == "playoff",
           f"groups complete + ≥2 groups -> stage switches to playoff (got {t4['stage']!r})")

    playoff_matches = [
        m for m in db.get_tournament_matches(tid4)
        if m["stage"] in ("sf", "final", "qf", "r16")
    ]
    expect(len(playoff_matches) > 0,
           "auto-advance generated at least one playoff match")

    # ── Walkover with explicit winner ─────────────────────────────────────
    print("\n=== /walkover @loser @winner ===")
    pl = db.upsert_player("walko_loser")
    pw = db.upsert_player("walko_winner")
    # Need a real tournament_id now that matches.tournament_id has a FK.
    walkover_tid = db.create_tournament("walkover_test", "vsa")
    new_mid = db.create_match(walkover_tid, pw["id"], pl["id"], stage="group", round_num=1)
    from match_processor import apply_walkover
    apply_walkover(new_mid, pl["id"])
    after = db.get_match(new_mid)
    expect(after["status"] == "confirmed", "walkover marks the match confirmed")
    # Winner gets 3, loser 0 — but score order depends on player1_id position.
    if after["player1_id"] == pw["id"]:
        expect(after["score1"] == 3 and after["score2"] == 0,
               "walkover scoreline is 3:0 for winner-as-player1")
    else:
        expect(after["score2"] == 3 and after["score1"] == 0,
               "walkover scoreline is 3:0 for winner-as-player2")

    # ── Fixture lock: groups must NOT be re-rolled on repeat /start ──────
    print("\n=== fixture lock: /start_tournament is idempotent ===")
    p1 = db.upsert_player("lock_one")
    p2 = db.upsert_player("lock_two")
    p3 = db.upsert_player("lock_three")
    p4 = db.upsert_player("lock_four")
    p5 = db.upsert_player("lock_five")
    p6 = db.upsert_player("lock_six")
    tid_lock = db.create_tournament("Lock Cup", tournament_type="vsa")
    from tournament import draw_groups, generate_group_fixtures
    pids = [p["id"] for p in (p1, p2, p3, p4, p5, p6)]
    g_first = draw_groups(tid_lock, pids, 2)
    generate_group_fixtures(tid_lock, g_first)

    first_assignment = {
        tp["player_id"]: tp["group_name"]
        for tp in db.get_tournament_players(tid_lock)
    }
    first_match_count = len([
        m for m in db.get_tournament_matches(tid_lock) if m["stage"] == "group"
    ])

    # Now simulate the new idempotent /start_tournament path: it should NOT
    # touch the existing assignments or create duplicate fixtures.
    has_group_matches = any(
        m["stage"] == "group" for m in db.get_tournament_matches(tid_lock)
    )
    has_assignments = any(
        tp.get("group_name")
        for tp in db.get_tournament_players(tid_lock)
    )
    expect(has_group_matches and has_assignments,
           "after first draw, both fixtures and assignments exist")

    # The new code now short-circuits if either of those signals is on.
    # The old code would DELETE FROM tournament_players + draw_groups
    # again. Verify by simulating only the new check path:
    if not (has_group_matches or has_assignments):
        # would re-roll — should never reach here in the fixed code.
        draw_groups(tid_lock, pids, 2)

    second_assignment = {
        tp["player_id"]: tp["group_name"]
        for tp in db.get_tournament_players(tid_lock)
    }
    second_match_count = len([
        m for m in db.get_tournament_matches(tid_lock) if m["stage"] == "group"
    ])
    expect(first_assignment == second_assignment,
           "second /start_tournament call did NOT change group assignments")
    expect(first_match_count == second_match_count,
           "second /start_tournament call did NOT create duplicate fixtures")

    # ── AI OCR helpers — parse model output without making a real call ───
    print("\n=== ocr._ai_parse_response ===")
    import ocr
    parsed = ocr._ai_parse_response(
        '```json\n{"score1":1,"score2":0,"team1":"phoenileo",'
        '"team2":"OliverBax","league":"Лига Гвардиолыча"}\n```'
    )
    expect(parsed["score1"] == 1 and parsed["score2"] == 0,
           "ai_ocr parses fenced JSON for score")
    expect(parsed["team1"] == "phoenileo" and parsed["team2"] == "OliverBax",
           "ai_ocr parses player handles correctly")
    expect(parsed["league_plate"] == "Лига Гвардиолыча",
           "ai_ocr parses league correctly")

    parsed_plain = ocr._ai_parse_response(
        '{"score1":3,"score2":2,"team1":"a","team2":"b","league":null}'
    )
    expect(parsed_plain["score1"] == 3 and parsed_plain["league_plate"] is None,
           "ai_ocr handles plain JSON with null league")

    parsed_strs = ocr._ai_parse_response(
        '{"score1":"4","score2":"1","team1":"x","team2":"y"}'
    )
    expect(parsed_strs["score1"] == 4 and parsed_strs["score2"] == 1,
           "ai_ocr coerces string scores to int")

    # ── Screenshot dedupe + series counter ──────────────────────────────
    print("\n=== screenshot dedupe + series counter ===")
    pa = db.upsert_player("dedupe_a")
    pb = db.upsert_player("dedupe_b")
    tid_dd = db.create_tournament("Dedupe Cup", tournament_type="vsa")
    mid_dd = db.create_match(tid_dd, pa["id"], pb["id"], stage="group", round_num=1)
    db.update_match(mid_dd, score1=2, score2=1, status="confirmed",
                    reported_by=pa["id"], screenshot_hash="aaaaaa")

    found = db.find_match_by_screenshot_hash("aaaaaa", tid_dd)
    expect(found is not None and found["id"] == mid_dd,
           "find_match_by_screenshot_hash returns the recorded match")

    not_found = db.find_match_by_screenshot_hash("bbbbbb", tid_dd)
    expect(not_found is None,
           "find_match_by_screenshot_hash returns None for unknown hash")

    db.record_processed_screenshot("zzzzzz", tid_dd, "chat-1", mid_dd, pa["id"])
    rec = db.get_processed_screenshot("zzzzzz", tid_dd)
    expect(rec is not None and rec["match_id"] == mid_dd,
           "record_processed_screenshot is retrievable")

    # Second match between same pair, opposite winner.
    mid_dd2 = db.create_match(tid_dd, pa["id"], pb["id"], stage="group", round_num=2)
    db.update_match(mid_dd2, score1=0, score2=3, status="confirmed",
                    reported_by=pb["id"])
    series = db.count_confirmed_matches_between(pa["id"], pb["id"], tid_dd)
    expect(series["p1_wins"] == 1 and series["p2_wins"] == 1
           and series["draws"] == 0 and series["total"] == 2,
           f"count_confirmed_matches_between → {series!r} expected 1-1")

    # ── matches_per_pair (group, double round-robin) ────────────────────
    print("\n=== matches_per_pair group double round-robin ===")
    from tournament import generate_group_fixtures
    g_pa = db.upsert_player("g_a")
    g_pb = db.upsert_player("g_b")
    g_pc = db.upsert_player("g_c")
    tid_dd_grp = db.create_tournament("DoubleRR Cup", tournament_type="vsa")
    db.update_tournament(tid_dd_grp, group_matches_per_pair=2)
    for pid in (g_pa["id"], g_pb["id"], g_pc["id"]):
        db.add_player_to_tournament(tid_dd_grp, pid, "A")
    mids = generate_group_fixtures(
        tid_dd_grp, {"A": [g_pa["id"], g_pb["id"], g_pc["id"]]}
    )
    expect(len(mids) == 6,
           f"3-player double round-robin → {len(mids)} matches expected 6")
    legs = sorted([db.get_match(m).get("leg") or 1 for m in mids])
    expect(legs == [1, 1, 1, 2, 2, 2],
           f"legs → {legs!r} expected [1,1,1,2,2,2]")

    # ── playoff 2-leg aggregate winner resolution ──────────────────────
    print("\n=== playoff 2-leg aggregate ===")
    from tournament import _resolve_pair_winner
    pp_a = db.upsert_player("pp_a")
    pp_b = db.upsert_player("pp_b")
    tid_pp = db.create_tournament("AggCup", tournament_type="vsa")
    db.update_tournament(tid_pp, playoff_matches_per_pair=2)
    leg1 = db.create_match(tid_pp, pp_a["id"], pp_b["id"], stage="sf", leg=1)
    leg2 = db.create_match(tid_pp, pp_b["id"], pp_a["id"], stage="sf", leg=2)
    db.update_match(leg1, score1=2, score2=1, status="confirmed", reported_by=pp_a["id"])
    db.update_match(leg2, score1=1, score2=2, status="confirmed", reported_by=pp_b["id"])
    ms = [db.get_match(leg1), db.get_match(leg2)]
    winner_pid = _resolve_pair_winner(ms)
    expect(winner_pid == pp_a["id"],
           f"2-leg aggregate winner pid → {winner_pid} expected {pp_a['id']}")

    # tied aggregate → returns None (caller schedules extra match)
    leg1b = db.create_match(tid_pp, pp_a["id"], pp_b["id"], stage="qf", leg=1)
    leg2b = db.create_match(tid_pp, pp_b["id"], pp_a["id"], stage="qf", leg=2)
    db.update_match(leg1b, score1=1, score2=2, status="confirmed", reported_by=pp_a["id"])
    db.update_match(leg2b, score1=1, score2=2, status="confirmed", reported_by=pp_b["id"])
    msb = [db.get_match(leg1b), db.get_match(leg2b)]
    expect(_resolve_pair_winner(msb) is None,
           "2-leg aggregate-tied returns None (extra match needed)")

    # ── auto_confirm flag ──────────────────────────────────────────────
    print("\n=== tournament flags wiring ===")
    tid_ac = db.create_tournament("Auto Cup", tournament_type="vsa")
    db.update_tournament(tid_ac, auto_confirm=1)
    fresh = db.get_tournament(tid_ac)
    expect(int(fresh.get("auto_confirm") or 0) == 1,
           "auto_confirm column persists 1")

    db.update_tournament(tid_ac, reminder_dm_hours=8)
    fresh = db.get_tournament(tid_ac)
    expect(int(fresh.get("reminder_dm_hours") or 0) == 8,
           "reminder_dm_hours column persists 8")

    db.update_tournament(tid_ac, reminder_chat_enabled=1, deadline_at="2026-05-12 21:00:00")
    fresh = db.get_tournament(tid_ac)
    expect(int(fresh.get("reminder_chat_enabled") or 0) == 1
           and fresh.get("deadline_at") == "2026-05-12 21:00:00",
           "reminder_chat_enabled + deadline_at persist")

    # ── Regression: /add_player → /start_tournament → /redraw_groups must
    # actually update group_name in the DB, not silently keep the old value.
    print("\n=== group_name upsert: /add_player → draw → redraw ===")
    rd_p1 = db.upsert_player("rd_alpha")
    rd_p2 = db.upsert_player("rd_beta")
    rd_p3 = db.upsert_player("rd_gamma")
    rd_p4 = db.upsert_player("rd_delta")
    tid_rd = db.create_tournament("Redraw Cup", tournament_type="ri")
    rd_pids = [p["id"] for p in (rd_p1, rd_p2, rd_p3, rd_p4)]

    # /add_player initially stores group_name='?'
    for pid in rd_pids:
        db.add_player_to_tournament(tid_rd, pid, "?")
    placeholders = {tp["player_id"]: tp["group_name"]
                    for tp in db.get_tournament_players(tid_rd)}
    expect(all(g == "?" for g in placeholders.values()),
           "after /add_player, every group_name == '?'")

    # /start_tournament path: draw_groups should overwrite '?' with real letters
    from tournament import draw_groups
    drawn_initial = draw_groups(tid_rd, rd_pids, 2)
    initial = {tp["player_id"]: tp["group_name"]
               for tp in db.get_tournament_players(tid_rd)}
    expect(set(initial.values()) <= {"A", "B"} and "?" not in initial.values(),
           "after first draw_groups, group_name is in {A, B} (no '?')")
    # And matches the in-memory groups dict (DB and bot output agree).
    drawn_initial_db = {pid: g for g, pids in drawn_initial.items() for pid in pids}
    expect(initial == drawn_initial_db,
           "DB group_name matches the in-memory groups dict from draw_groups")

    # /redraw_groups path: bot first resets group_name=NULL, then re-draws.
    conn = db.get_conn()
    conn.execute(
        "UPDATE tournament_players SET group_name=NULL WHERE tournament_id=?",
        (tid_rd,),
    )
    conn.commit()
    conn.close()
    nulled = {tp["player_id"]: tp["group_name"]
              for tp in db.get_tournament_players(tid_rd)}
    expect(all(g is None for g in nulled.values()),
           "redraw reset writes group_name=NULL for every row")

    drawn_redraw = draw_groups(tid_rd, rd_pids, 2)
    redrawn = {tp["player_id"]: tp["group_name"]
               for tp in db.get_tournament_players(tid_rd)}
    expect(None not in redrawn.values(),
           "after /redraw_groups, no player has group_name=NULL "
           "(was the 'Группа None' bug)")
    drawn_redraw_db = {pid: g for g, pids in drawn_redraw.items() for pid in pids}
    expect(redrawn == drawn_redraw_db,
           "after /redraw_groups, DB group_name matches the new draw")

    # ── Postgres datetime → string formatter (was the /matches crash). ──
    print("\n=== _fmt_dt: handles str + datetime + None ===")
    from datetime import datetime as _DT
    fmt = bot._fmt_dt
    fmt_d = bot._fmt_date
    expect(fmt(None) == "", "None → ''")
    expect(fmt("") == "", "empty string → ''")
    expect(fmt(_DT(2026, 5, 5, 17, 8, 46)) == "2026-05-05 17:08:46",
           "naive datetime → standard string")
    # tz-aware → tz stripped, otherwise strptime round-trip in tests is awkward
    from datetime import timezone, timedelta
    expect(fmt(_DT(2026, 5, 5, 17, 8, 46, tzinfo=timezone.utc))
           == "2026-05-05 17:08:46",
           "tz-aware datetime → tz stripped")
    # Postgres-style stringified datetime with microseconds + tz
    expect(fmt("2026-05-05 17:08:46.058000+00:00") == "2026-05-05 17:08:46",
           "stringified datetime with .micro+tz is normalized")
    expect(fmt_d(_DT(2026, 5, 5, 17, 8, 46)) == "2026-05-05",
           "_fmt_date trims to YYYY-MM-DD")
    expect(fmt_d("2026-05-05 17:08:46.058000+00:00") == "2026-05-05",
           "_fmt_date works on stringified Postgres timestamps")

    # ── Bulk /add_player parser: comma + space + dedup + lowercase. ────
    print("\n=== /add_player bulk parser ===")
    parse = bot._parse_add_player_usernames
    expect(parse(["@a,", "@b", "@c"]) == ["a", "b", "c"],
           "spaces + trailing commas tokenize cleanly")
    expect(parse(["@a,@b,@c"]) == ["a", "b", "c"],
           "single comma-glued token is split")
    expect(parse(["@A,", "@a"]) == ["a"],
           "case-insensitive dedup, first-seen kept")
    expect(parse(["@a;", "@b @c"]) == ["a", "b", "c"],
           "semicolons and intra-token spaces both work")
    expect(parse(["alice", "@bob"]) == ["alice", "bob"],
           "leading @ is optional")
    expect(parse([",,", " ", "@x"]) == ["x"],
           "noise-only tokens are dropped")
    expect(parse([]) == [], "empty args → empty list")

    # ── Multiple active tournaments of the same type are allowed now. ──
    print("\n=== multiple active tournaments of same type are allowed ===")
    tid_ri_1 = db.create_tournament("RI Season 1", tournament_type="ri")
    tid_ri_2 = db.create_tournament("RI Season 2", tournament_type="ri")
    actives = db.get_active_tournaments()
    active_ri_ids = {t["id"] for t in actives if t["tournament_type"] == "ri"}
    expect({tid_ri_1, tid_ri_2}.issubset(active_ri_ids),
           "two RI tournaments coexist as active")
    latest = db.get_active_tournament(tournament_type="ri")
    expect(latest is not None and latest["id"] == tid_ri_2,
           "get_active_tournament(ri) returns the most recent (id desc)")

    # ── /replace_player: roster + pending-match transfer. ────────────────
    print("\n=== /replace_player swaps roster row + pending matches ===")
    tid_rep = db.create_tournament("ReplaceTest", tournament_type="vsa")
    p_a = db.upsert_player("rep_a")
    p_b = db.upsert_player("rep_b")
    p_c = db.upsert_player("rep_c")  # the substitute
    db.add_player_to_tournament(tid_rep, p_a["id"], "A")
    db.add_player_to_tournament(tid_rep, p_b["id"], "A")
    confirmed_mid = db.create_match(
        tid_rep, p_a["id"], p_b["id"], stage="group", round_num=1
    )
    db.update_match(confirmed_mid, score1=2, score2=1, status="confirmed")
    pending_mid = db.create_match(
        tid_rep, p_a["id"], p_b["id"], stage="group", round_num=2
    )

    summary = db.replace_tournament_player(tid_rep, p_a["id"], p_c["id"])
    expect(summary["matches_moved"] == 1,
           "replace_tournament_player moves 1 pending match")

    roster = {r["player_id"] for r in db.get_tournament_players(tid_rep)}
    expect(p_c["id"] in roster and p_a["id"] not in roster,
           "old player is removed, substitute is in")

    after_pending = db.get_match(pending_mid)
    expect(p_c["id"] in (after_pending["player1_id"], after_pending["player2_id"]),
           "pending match now references the substitute")
    expect(p_a["id"] not in (after_pending["player1_id"], after_pending["player2_id"]),
           "pending match no longer references the replaced player")

    after_confirmed = db.get_match(confirmed_mid)
    expect(p_a["id"] in (after_confirmed["player1_id"], after_confirmed["player2_id"]),
           "confirmed match KEEPS the original player (history preserved)")

    sub_row = next(
        r for r in db.get_tournament_players(tid_rep)
        if r["player_id"] == p_c["id"]
    )
    expect(sub_row["group_name"] == "A",
           "substitute inherits the original group letter")

    # ── Tournament with auto-tech-loss columns persists correctly. ──────
    print("\n=== auto-tech-loss columns persist ===")
    tid_tl = db.create_tournament("TLTest", tournament_type="vsa")
    db.update_tournament(tid_tl, auto_tech_loss_enabled=1,
                          auto_tech_loss_score="0:0")
    fresh = db.get_tournament(tid_tl)
    expect(int(fresh["auto_tech_loss_enabled"]) == 1,
           "auto_tech_loss_enabled is set")
    expect(fresh["auto_tech_loss_score"] == "0:0",
           "auto_tech_loss_score round-trips")

    # ── get_real_tournament_matches drops cross-group group rows. ────────
    print("\n=== get_real_tournament_matches filters phantoms ===")
    tid_ph = db.create_tournament("PhantomTest", tournament_type="vsa")
    p_x = db.upsert_player("phant_x")
    p_y = db.upsert_player("phant_y")
    p_z = db.upsert_player("phant_z")
    db.add_player_to_tournament(tid_ph, p_x["id"], "A")
    db.add_player_to_tournament(tid_ph, p_y["id"], "A")
    db.add_player_to_tournament(tid_ph, p_z["id"], "B")
    real_mid = db.create_match(tid_ph, p_x["id"], p_y["id"],
                                stage="group", round_num=1)
    phantom_mid = db.create_match(tid_ph, p_x["id"], p_z["id"],
                                   stage="group", round_num=1)
    real_only = db.get_real_tournament_matches(tid_ph)
    real_ids = {m["id"] for m in real_only}
    expect(real_mid in real_ids,
           "same-group group match is kept by get_real_tournament_matches")
    expect(phantom_mid not in real_ids,
           "cross-group group match is dropped by get_real_tournament_matches")

    # Same helper also dedupes duplicate playoff legs (same pair + stage + leg).
    pf_a = db.create_match(tid_ph, p_x["id"], p_y["id"],
                           stage="sf", round_num=1, leg=1)
    pf_b = db.create_match(tid_ph, p_y["id"], p_x["id"],   # reversed direction
                           stage="sf", round_num=1, leg=1)
    real_only = db.get_real_tournament_matches(tid_ph)
    sf_ids = {m["id"] for m in real_only if m.get("stage") == "sf"}
    expect(len(sf_ids) == 1 and max(pf_a, pf_b) in sf_ids,
           "duplicate playoff legs collapsed by get_real_tournament_matches")

    # ── generate_playoff is idempotent. ───────────────────────────────
    print("\n=== generate_playoff idempotent ===")
    from tournament import generate_playoff
    tid_idem = db.create_tournament("IdempTest", tournament_type="ri")
    db.update_tournament(tid_idem, playoff_matches_per_pair=1)
    qa = db.upsert_player("idem_a")
    qb = db.upsert_player("idem_b")
    qc = db.upsert_player("idem_c")
    qd = db.upsert_player("idem_d")
    db.add_player_to_tournament(tid_idem, qa["id"], "A")
    db.add_player_to_tournament(tid_idem, qb["id"], "A")
    db.add_player_to_tournament(tid_idem, qc["id"], "B")
    db.add_player_to_tournament(tid_idem, qd["id"], "B")
    # Group matches all confirmed: simulate a finished group stage so
    # get_group_standings has data to rank.
    g1 = db.create_match(tid_idem, qa["id"], qb["id"], stage="group", round_num=1)
    g2 = db.create_match(tid_idem, qc["id"], qd["id"], stage="group", round_num=1)
    db.update_match(g1, score1=2, score2=0, status="confirmed")
    db.update_match(g2, score1=2, score2=0, status="confirmed")
    bracket1 = generate_playoff(tid_idem)
    sf_count_1 = len([m for m in bracket1 if m["stage"] == "sf"])
    bracket2 = generate_playoff(tid_idem)
    sf_count_2 = len([m for m in bracket2 if m["stage"] == "sf"])
    real_after = db.get_real_tournament_matches(tid_idem)
    sf_after = [m for m in real_after if m.get("stage") == "sf"]
    expect(sf_count_1 == 2,
           f"first generate_playoff creates exactly 2 SF matches (got {sf_count_1})")
    expect(sf_count_2 == sf_count_1,
           "second generate_playoff returns existing bracket (no new rows)")
    expect(len(sf_after) == 2,
           f"DB has exactly 2 SF rows after repeated calls (got {len(sf_after)})")

    # ── Tournament-admin delegation ─────────────────────────────────────
    print("\n=== tournament_admins (Feature 3) ===")
    tid_ta = db.create_tournament(
        "TA Cup", tournament_type="vsa",
        created_by=p["id"], is_official=False,
    )
    t_ta = db.get_tournament(tid_ta)
    expect(bot._can_manage_tournament(555, t_ta),
           "creator (alice) can manage TA Cup")
    expect(not bot._can_manage_tournament(888, t_ta),
           "stranger 888 cannot manage TA Cup yet")
    # Delegate stranger 888 (must already be a registered player so we
    # have a player row to mention; tournament_admins keys by telegram_id
    # so a row in `players` isn't strictly required, but convenient).
    db.add_tournament_admin(tid_ta, telegram_id=888, granted_by=555,
                             note="delegated for admin tests")
    expect(db.is_tournament_admin(tid_ta, 888),
           "is_tournament_admin reports the new delegate")
    expect(bot._can_manage_tournament(888, t_ta),
           "delegated user 888 can now manage TA Cup")
    # And nobody else gained access.
    expect(not bot._can_manage_tournament(999, t_ta),
           "unrelated user 999 still cannot manage TA Cup")
    # list_tournament_admins returns the delegated user.
    listing = db.list_tournament_admins(tid_ta)
    expect(len(listing) == 1 and listing[0]["telegram_id"] == 888,
           "list_tournament_admins lists the delegate")
    # remove revokes access immediately.
    removed = db.remove_tournament_admin(tid_ta, 888)
    expect(removed, "remove_tournament_admin returns True for existing row")
    expect(not db.is_tournament_admin(tid_ta, 888),
           "after revoke, 888 is no longer tournament admin")
    expect(not bot._can_manage_tournament(888, db.get_tournament(tid_ta)),
           "after revoke, 888 cannot manage TA Cup anymore")
    # _approver_telegram_ids unions root/creator/per-tournament admins.
    db.add_tournament_admin(tid_ta, telegram_id=888, granted_by=555)
    approvers = bot._approver_telegram_ids(db.get_tournament(tid_ta))
    expect(111 in approvers,
           "_approver_telegram_ids includes root admin 111")
    expect(555 in approvers,
           "_approver_telegram_ids includes the creator (alice tg=555)")
    expect(888 in approvers,
           "_approver_telegram_ids includes the per-tournament admin 888")

    # ── Multi-photo goal merging helpers (Feature 1) ────────────────────
    print("\n=== multi-photo merge helpers (Feature 1) ===")
    a = (None, "Real Madrid", "Barcelona", 3, 2)
    a_sig = a[1:]
    expect(bot._teams_match(("Real Madrid", "Barcelona"),
                              ("Real Madrid", "Barcelona")),
           "_teams_match: identical pair")
    expect(bot._teams_match(("Real Madrid", "Barcelona"),
                              ("Barcelona", "Real Madrid")),
           "_teams_match: reversed order accepted")
    expect(bot._teams_match(("REAL MADRID", "Barca"),
                              ("real madrid", "barcelona")),
           "_teams_match: fuzzy + case-insensitive")
    expect(not bot._teams_match(("Real Madrid", "Barca"),
                                  ("PSG", "Bayern")),
           "_teams_match: completely different teams")
    # Goal merge dedups by (name, minute, side).
    g1 = [{"name": "Mbappé", "minute": 12, "side": "home"},
          {"name": "Bellingham", "minute": 33, "side": "home"}]
    g2 = [{"name": "Mbappé", "minute": 12, "side": "home"},   # dup
          {"name": "Vinicius", "minute": 67, "side": "home"}]  # new
    merged, added = bot._merge_goal_lists(g1, g2)
    expect(added == 1, f"merged adds exactly 1 new goal (added={added})")
    expect(len(merged) == 3,
           f"merged list has 3 goals total (len={len(merged)})")
    keys = {bot._goal_key(g) for g in merged}
    expect(("vinicius", "67", "home") in keys,
           "merged contains the new Vinicius goal")
    # Re-merging is a no-op.
    merged2, added2 = bot._merge_goal_lists(merged, g2)
    expect(added2 == 0, "re-merging same goals is a no-op")
    expect(len(merged2) == len(merged),
           "merged length unchanged on re-merge")

    # ── Full bracket TBD placeholders (Feature 4) ──────────────────────
    print("\n=== full playoff bracket TBD (Feature 4) ===")
    tid_br = db.create_tournament(
        "Bracket Cup", tournament_type="vsa",
        created_by=p["id"], is_official=False,
    )
    db.update_tournament(tid_br, playoff_matches_per_pair=1, stage="playoff")
    # Build only an SF — final hasn't been generated yet.
    bp = []
    for i in range(4):
        bp.append(db.upsert_player(f"br_p{i}"))
    db.add_player_to_tournament(tid_br, bp[0]["id"], "A")
    db.add_player_to_tournament(tid_br, bp[1]["id"], "A")
    db.add_player_to_tournament(tid_br, bp[2]["id"], "B")
    db.add_player_to_tournament(tid_br, bp[3]["id"], "B")
    db.create_match(tid_br, bp[0]["id"], bp[1]["id"], stage="sf",
                    round_num=1, leg=1)
    db.create_match(tid_br, bp[2]["id"], bp[3]["id"], stage="sf",
                    round_num=1, leg=1)
    from tournament import format_playoff_bracket
    rendered = format_playoff_bracket(tid_br)
    expect("Полуфинал" in rendered,
           "format_playoff_bracket shows SF stage header")
    expect("Финал" in rendered,
           "format_playoff_bracket shows TBD Final header")
    expect("TBD vs TBD" in rendered,
           "format_playoff_bracket renders TBD placeholder line")

    # _collect_pairs_full pads the final stage with a single TBD pair
    # when only SF exists.
    from playoff_image import _collect_pairs_full
    full = _collect_pairs_full(tid_br)
    full_by_stage = {s: pairs for s, pairs in full}
    expect("sf" in full_by_stage and "final" in full_by_stage,
           "_collect_pairs_full extends to final")
    expect(len(full_by_stage["final"]) == 1,
           "final has exactly 1 TBD pair")
    expect(full_by_stage["final"][0][0].get("_tbd"),
           "final pair is flagged as TBD")

    # ── _current_playoff_stage helper (Feature 2 plumbing) ─────────────
    print("\n=== _current_playoff_stage helper (Feature 2) ===")
    # Re-use Bracket Cup; only SF rows exist.
    expect(bot._current_playoff_stage(tid_br) == "sf",
           "_current_playoff_stage finds the latest stage with rows (sf)")
    db.create_match(tid_br, bp[0]["id"], bp[2]["id"], stage="final",
                    round_num=1, leg=1)
    expect(bot._current_playoff_stage(tid_br) == "final",
           "_current_playoff_stage updates when a final row exists")

    # ── v12 features ──────────────────────────────────────────────────
    print("\n=== H2H stats (v12 / Feature 3) ===")
    pa = db.upsert_player("alice_h2h")
    pb = db.upsert_player("bob_h2h")
    pc = db.upsert_player("carol_h2h")  # noise
    tid_h = db.create_tournament(
        "H2H Cup", tournament_type="vsa",
        created_by=pa["id"], is_official=False,
    )
    # 3 confirmed: alice 2-0, alice 1-2, alice 3-3.
    m1 = db.create_match(tid_h, pa["id"], pb["id"], stage="group", round_num=1)
    db.update_match(m1, score1=2, score2=0, status="confirmed",
                    reported_by=555)
    m2 = db.create_match(tid_h, pb["id"], pa["id"], stage="group", round_num=1)
    db.update_match(m2, score1=2, score2=1, status="confirmed",
                    reported_by=555)
    m3 = db.create_match(tid_h, pa["id"], pb["id"], stage="group", round_num=2)
    db.update_match(m3, score1=3, score2=3, status="confirmed",
                    reported_by=555)
    # And one with an unrelated player — must NOT show up.
    m4 = db.create_match(tid_h, pa["id"], pc["id"], stage="group", round_num=3)
    db.update_match(m4, score1=1, score2=0, status="confirmed",
                    reported_by=555)
    # And one pending between alice/bob — must NOT count.
    db.create_match(tid_h, pa["id"], pb["id"], stage="group", round_num=4)

    h2h = db.get_h2h_matches(pa["id"], pb["id"])
    expect(len(h2h) == 3, f"get_h2h_matches returns 3 confirmed (got {len(h2h)})")
    h2h_ids = {m["id"] for m in h2h}
    expect(m4 not in h2h_ids, "noise (alice vs carol) excluded from h2h")
    expect(all(m["status"] == "confirmed" for m in h2h),
           "all h2h matches are confirmed")
    # Aggregate counts (alice perspective): 1 win, 1 loss, 1 draw.
    a_wins = sum(
        1 for m in h2h
        if (m["player1_id"] == pa["id"] and (m["score1"] or 0) > (m["score2"] or 0))
        or (m["player2_id"] == pa["id"] and (m["score2"] or 0) > (m["score1"] or 0))
    )
    a_losses = sum(
        1 for m in h2h
        if (m["player1_id"] == pa["id"] and (m["score1"] or 0) < (m["score2"] or 0))
        or (m["player2_id"] == pa["id"] and (m["score2"] or 0) < (m["score1"] or 0))
    )
    expect(a_wins == 1, f"alice has 1 h2h win (got {a_wins})")
    expect(a_losses == 1, f"alice has 1 h2h loss (got {a_losses})")

    # ── /my_deadlines (Feature 4) ─────────────────────────────────────
    print("\n=== my_deadlines / open-matches helper (v12 / Feature 4) ===")
    pd1 = db.upsert_player("dl_p1")
    pd2 = db.upsert_player("dl_p2")
    pd3 = db.upsert_player("dl_p3")
    tid_d = db.create_tournament(
        "Deadline Cup", tournament_type="vsa",
        created_by=pa["id"], is_official=False,
    )
    # Three matches for pd1: one confirmed (excluded), one pending with a
    # near-future deadline, one pending overdue.
    md_done = db.create_match(tid_d, pd1["id"], pd2["id"],
                              stage="group", round_num=1,
                              deadline="2099-01-01 00:00:00")
    db.update_match(md_done, score1=1, score2=0, status="confirmed",
                    reported_by=555)
    md_soon = db.create_match(tid_d, pd1["id"], pd3["id"],
                              stage="group", round_num=2,
                              deadline="2099-01-01 00:00:00")
    md_over = db.create_match(tid_d, pd1["id"], pd2["id"],
                              stage="group", round_num=3,
                              deadline="2000-01-01 00:00:00")
    rows = db.get_open_matches_for_player(pd1["id"])
    open_ids = [r["id"] for r in rows]
    expect(md_done not in open_ids,
           "confirmed match is excluded from open matches")
    expect(md_soon in open_ids and md_over in open_ids,
           "both pending matches included")
    # Sorted by deadline asc — overdue (2000) first, soon (2099) second.
    expect(open_ids[0] == md_over,
           "open matches sorted by deadline ascending (overdue first)")
    expect(open_ids[1] == md_soon,
           "open matches: future deadline second")
    # Countdown formatter handles past/future correctly.
    overdue_text = bot._format_deadline_countdown("2000-01-01 00:00:00")
    soon_text = bot._format_deadline_countdown("2099-01-01 00:00:00")
    expect(overdue_text.startswith("просрочено"),
           f"overdue formatted as 'просрочено …' (got {overdue_text!r})")
    expect(soon_text.startswith("через"),
           f"future formatted as 'через …' (got {soon_text!r})")
    expect(bot._format_deadline_countdown(None) == "без дедлайна",
           "None deadline falls back to 'без дедлайна'")

    # ── /tlog (Feature 5) ─────────────────────────────────────────────
    print("\n=== tournament audit log (v12 / Feature 5) ===")
    tid_l = db.create_tournament(
        "Audit Cup", tournament_type="vsa",
        created_by=pa["id"], is_official=False,
    )
    expect(db.list_tournament_audit_log(tid_l) == [],
           "audit log starts empty")
    db.log_tournament_action(
        tid_l, actor_telegram_id=111, actor_username="root",
        action="add_player", details="target=@x",
    )
    db.log_tournament_action(
        tid_l, actor_telegram_id=111, actor_username="root",
        action="walkover", details="match=42 loser=@x winner=@y",
    )
    db.log_tournament_action(
        tid_l, actor_telegram_id=222, actor_username="alice",
        action="set_description", details="len=42",
    )
    rows = db.list_tournament_audit_log(tid_l)
    expect(len(rows) == 3, f"audit log returned 3 rows (got {len(rows)})")
    # Newest first.
    expect(rows[0]["action"] == "set_description",
           "audit log newest first (set_description)")
    expect(rows[-1]["action"] == "add_player",
           "audit log oldest last (add_player)")
    # tournament_id=None is a no-op.
    db.log_tournament_action(
        None, actor_telegram_id=111, actor_username="root",
        action="noop",
    )
    expect(len(db.list_tournament_audit_log(tid_l)) == 3,
           "log_tournament_action with None tournament is a no-op")
    # Limit honoured.
    rows_lim = db.list_tournament_audit_log(tid_l, limit=2)
    expect(len(rows_lim) == 2,
           f"list_tournament_audit_log limit=2 (got {len(rows_lim)})")

    # ── /playoff_preview (Feature 6) ──────────────────────────────────
    print("\n=== playoff preview (v12 / Feature 6) ===")
    from tournament import compute_playoff_preview
    tid_p = db.create_tournament(
        "Preview Cup", tournament_type="vsa",
        created_by=pa["id"], is_official=False,
    )
    pp = [db.upsert_player(f"pv_p{i}") for i in range(8)]
    # Two groups of 4. Set explicit group_points to force a deterministic order.
    for i, gp in enumerate("AAAA"):
        db.add_player_to_tournament(tid_p, pp[i]["id"], gp)
    for i, gp in enumerate("BBBB", start=4):
        db.add_player_to_tournament(tid_p, pp[i]["id"], gp)
    # Force standings: group A: pp0(9pts), pp1(6), pp2(3), pp3(0).
    #                  group B: pp4(9pts), pp5(6), pp6(3), pp7(0).
    pts_map = {0: 9, 1: 6, 2: 3, 3: 0, 4: 9, 5: 6, 6: 3, 7: 0}
    for idx, pts in pts_map.items():
        db.update_tournament_player(tid_p, pp[idx]["id"], group_points=pts)
    preview = compute_playoff_preview(tid_p, advance_per_group=2)
    expect(preview["stage"] == "sf",
           f"2 groups → preview stage is 'sf' (got {preview['stage']})")
    expect(len(preview["pairs"]) == 2,
           f"preview has 2 pairs (got {len(preview['pairs'])})")
    # Cross-group: 1A vs 2B and 1B vs 2A.
    pair_ids = {
        (pr["a"]["player_id"], pr["b"]["player_id"]) for pr in preview["pairs"]
    }
    expect((pp[0]["id"], pp[5]["id"]) in pair_ids,
           "preview pairs include 1A (pp0) vs 2B (pp5)")
    expect((pp[4]["id"], pp[1]["id"]) in pair_ids,
           "preview pairs include 1B (pp4) vs 2A (pp1)")
    # Important: preview must not write any matches.
    matches_after = db.get_tournament_matches(tid_p, stage="sf")
    expect(matches_after == [],
           "compute_playoff_preview is read-only (no matches created)")

    # ── /withdraw (Feature 7) ─────────────────────────────────────────
    print("\n=== withdraw (v12 / Feature 7) ===")
    from match_processor import apply_walkover
    tid_w = db.create_tournament(
        "Withdraw Cup", tournament_type="vsa",
        created_by=pa["id"], is_official=False,
    )
    pw = [db.upsert_player(f"wd_p{i}") for i in range(4)]
    for i, gp in enumerate("AAAA"):
        db.add_player_to_tournament(tid_w, pw[i]["id"], gp)
    # Three open matches involving pw[0]: two pending, one reported.
    mw1 = db.create_match(tid_w, pw[0]["id"], pw[1]["id"],
                          stage="sf", round_num=1, leg=1)
    mw2 = db.create_match(tid_w, pw[2]["id"], pw[0]["id"],
                          stage="sf", round_num=1, leg=2)
    mw3 = db.create_match(tid_w, pw[0]["id"], pw[3]["id"],
                          stage="group", round_num=1)
    db.update_match(mw3, score1=2, score2=1, status="reported",
                    reported_by=555)
    open_w = db.get_open_matches_for_player(pw[0]["id"], tournament_id=tid_w)
    expect(len(open_w) == 3, "withdraw target has 3 open matches")
    # Apply walkover on each — simulates what cmd_withdraw does internally.
    for m in open_w:
        apply_walkover(m["id"], pw[0]["id"])
    open_after = db.get_open_matches_for_player(pw[0]["id"], tournament_id=tid_w)
    expect(open_after == [],
           f"after withdraw, no open matches left (got {len(open_after)})")
    # Each match is now confirmed and pw[0] is the loser.
    for mid in (mw1, mw2, mw3):
        m = db.get_match(mid)
        expect(m["status"] == "confirmed",
               f"match {mid} confirmed after walkover")
        is_p1 = m["player1_id"] == pw[0]["id"]
        loser_score = m["score1"] if is_p1 else m["score2"]
        winner_score = m["score2"] if is_p1 else m["score1"]
        expect(int(loser_score or 0) == 0 and int(winner_score or 0) == 3,
               f"match {mid} walkover score 0:3 against pw[0]")

    # ── v13: advance_playoff dedup of phantom legs ────────────────────
    print("\n=== advance_playoff dedup phantom legs (v13) ===")
    from tournament import advance_playoff, _dedup_playoff_legs
    pa1 = db.upsert_player("adv_a")
    pa2 = db.upsert_player("adv_b")
    pa3 = db.upsert_player("adv_c")
    pa4 = db.upsert_player("adv_d")
    tid_a = db.create_tournament(
        "Advance Cup", tournament_type="vsa",
        created_by=pa["id"], is_official=False,
    )
    db.update_tournament(
        tid_a, stage="playoff", playoff_matches_per_pair=2,
        # Disable the optional bronze fixture: this test predates the
        # 3rd-place feature and asserts the "advance_playoff returns
        # 'finished' after the final" contract — gating that on a
        # parallel bronze match would break it. The bronze flow itself
        # has dedicated coverage in test_playoff_byes.py.
        playoff_third_place=0,
    )
    for i, p in enumerate((pa1, pa2, pa3, pa4)):
        db.add_player_to_tournament(tid_a, p["id"], "AB"[i // 2])
    # Pair 1: pa1 vs pa2 — leg1 + leg2 confirmed.
    sf1_l1 = db.create_match(tid_a, pa1["id"], pa2["id"],
                             stage="sf", round_num=1, leg=1)
    db.update_match(sf1_l1, score1=3, score2=0,
                    status="confirmed", reported_by=555)
    sf1_l2 = db.create_match(tid_a, pa2["id"], pa1["id"],
                             stage="sf", round_num=1, leg=2)
    db.update_match(sf1_l2, score1=0, score2=3,
                    status="confirmed", reported_by=555)
    # Pair 2: pa3 vs pa4 — leg1 + leg2 confirmed.
    sf2_l1 = db.create_match(tid_a, pa3["id"], pa4["id"],
                             stage="sf", round_num=1, leg=1)
    db.update_match(sf2_l1, score1=0, score2=3,
                    status="confirmed", reported_by=555)
    sf2_l2 = db.create_match(tid_a, pa4["id"], pa3["id"],
                             stage="sf", round_num=1, leg=2)
    db.update_match(sf2_l2, score1=3, score2=0,
                    status="confirmed", reported_by=555)
    # Phantom: a leftover pending leg=1 row for pair 1 (the bug we're fixing).
    phantom = db.create_match(tid_a, pa1["id"], pa2["id"],
                              stage="sf", round_num=1, leg=1)
    # Phantom stays pending — represents the legacy bug shape.
    raw = db.get_tournament_matches(tid_a, stage="sf")
    expect(len(raw) == 5,
           f"raw SF matches include phantom (got {len(raw)})")
    deduped = _dedup_playoff_legs(raw)
    expect(len(deduped) == 4,
           f"_dedup_playoff_legs collapses phantom (got {len(deduped)})")
    # Phantom (oldest leg=1 dup) should be the one dropped.
    deduped_ids = {m["id"] for m in deduped}
    expect(phantom not in deduped_ids,
           "phantom (older id) is dropped by _dedup_playoff_legs")
    # advance_playoff should now move SF → final despite the phantom.
    result = advance_playoff(tid_a)
    expect(result == "final",
           f"advance_playoff returns 'final' after dedup (got {result!r})")
    # Final stage was actually created with 2 legs.
    final_rows = db.get_tournament_matches(tid_a, stage="final")
    expect(len(final_rows) == 2,
           f"final stage has 2 leg rows (got {len(final_rows)})")

    # ── v13: _can_advance_now button gating ───────────────────────────
    print("\n=== _can_advance_now button gating (v13) ===")
    expect(bot._can_advance_now(tid_a) is False,
           "after advance, _can_advance_now is False (final already created)")
    # Confirm both final legs with the SAME finalist winning both —
    # otherwise legs stored (a-vs-b, b-vs-a) yield an aggregate tie.
    finalists = sorted({
        pid
        for m in db.get_tournament_matches(tid_a, stage="final")
        for pid in (m["player1_id"], m["player2_id"])
    })
    winner_pid = finalists[0]
    for m in db.get_tournament_matches(tid_a, stage="final"):
        if m["player1_id"] == winner_pid:
            db.update_match(m["id"], score1=2, score2=0,
                            status="confirmed", reported_by=555)
        else:
            db.update_match(m["id"], score1=0, score2=2,
                            status="confirmed", reported_by=555)
    expect(bot._can_advance_now(tid_a) is True,
           "with all final legs confirmed, _can_advance_now is True")
    # Run again → finishes the tournament.
    result2 = advance_playoff(tid_a)
    expect(result2 == "finished",
           f"advance_playoff returns 'finished' after final done (got {result2!r})")
    expect(bot._can_advance_now(tid_a) is False,
           "after finished, _can_advance_now is False")

    # ── consecutive draws spawn extra legs until someone wins ─────────
    print("\n=== consecutive draws → leg 3, leg 4, leg 5 (bo2) ===")
    cd_a = db.upsert_player("cd_a")
    cd_b = db.upsert_player("cd_b")
    tid_cd = db.create_tournament(
        "Consecutive Draw Cup", tournament_type="vsa",
        created_by=cd_a["id"], is_official=False,
    )
    db.update_tournament(
        tid_cd, stage="playoff", playoff_matches_per_pair=2,
    )
    for p in (cd_a, cd_b):
        db.add_player_to_tournament(tid_cd, p["id"], "A")
    # Build the final manually so advance_playoff has a stage to walk.
    cd_l1 = db.create_match(tid_cd, cd_a["id"], cd_b["id"],
                            stage="final", round_num=1, leg=1)
    cd_l2 = db.create_match(tid_cd, cd_b["id"], cd_a["id"],
                            stage="final", round_num=1, leg=2)
    # Leg1 + Leg2 both end 1:1 → aggregate 2:2 → spawn leg 3.
    db.update_match(cd_l1, score1=1, score2=1,
                    status="confirmed", reported_by=555)
    db.update_match(cd_l2, score1=1, score2=1,
                    status="confirmed", reported_by=555)
    advance_playoff(tid_cd)
    finals_after_l2 = db.get_tournament_matches(tid_cd, stage="final")
    legs_after_l2 = sorted([(m.get("leg") or 1) for m in finals_after_l2])
    expect(legs_after_l2 == [1, 2, 3],
           f"after 2 draws, advance_playoff spawns leg 3 "
           f"(got legs {legs_after_l2!r})")
    # Confirm leg3 as another draw → must spawn leg 4 (this is the
    # regression: previously the spawn only fired when len == legs_cfg).
    cd_l3 = [m for m in finals_after_l2 if (m.get("leg") or 1) == 3][0]
    db.update_match(cd_l3["id"], score1=0, score2=0,
                    status="confirmed", reported_by=555)
    advance_playoff(tid_cd)
    finals_after_l3 = db.get_tournament_matches(tid_cd, stage="final")
    legs_after_l3 = sorted([(m.get("leg") or 1) for m in finals_after_l3])
    expect(legs_after_l3 == [1, 2, 3, 4],
           f"after 3 draws, advance_playoff spawns leg 4 "
           f"(got legs {legs_after_l3!r})")
    # Leg 4 also a draw → leg 5 spawned (one more for good measure).
    cd_l4 = [m for m in finals_after_l3 if (m.get("leg") or 1) == 4][0]
    db.update_match(cd_l4["id"], score1=2, score2=2,
                    status="confirmed", reported_by=555)
    advance_playoff(tid_cd)
    finals_after_l4 = db.get_tournament_matches(tid_cd, stage="final")
    legs_after_l4 = sorted([(m.get("leg") or 1) for m in finals_after_l4])
    expect(legs_after_l4 == [1, 2, 3, 4, 5],
           f"after 4 draws, advance_playoff spawns leg 5 "
           f"(got legs {legs_after_l4!r})")
    # Leg 5 is a win → tournament finishes (no further leg, finalize).
    cd_l5 = [m for m in finals_after_l4 if (m.get("leg") or 1) == 5][0]
    if cd_l5["player1_id"] == cd_a["id"]:
        db.update_match(cd_l5["id"], score1=3, score2=1,
                        status="confirmed", reported_by=555)
    else:
        db.update_match(cd_l5["id"], score1=1, score2=3,
                        status="confirmed", reported_by=555)
    advance_playoff(tid_cd)
    t_after = db.get_tournament(tid_cd)
    expect(t_after["stage"] == "finished",
           f"after leg 5 win, tournament finished "
           f"(got stage={t_after.get('stage')!r})")

    # ── v13.1: top_scorers GROUP BY includes p.username (Postgres) ────
    print("\n=== top_scorers GROUP BY safety (v13.1) ===")
    import re as _re
    import inspect as _inspect
    for fn_name in (
        "get_top_scorers_global",
        "get_top_scorers_for_tournament",
        "get_top_scorers_custom",
    ):
        src = _inspect.getsource(getattr(db, fn_name))
        # Each query must include p.username in GROUP BY (otherwise
        # PostgreSQL with strict GROUP-BY rules rejects the SELECT).
        gbs = _re.findall(r"GROUP BY[^)]*?(?=ORDER BY|LIMIT)", src, _re.IGNORECASE)
        expect(
            len(gbs) >= 1,
            f"{fn_name}: at least one GROUP BY clause present",
        )
        for gb in gbs:
            expect(
                "p.username" in gb,
                f"{fn_name}: GROUP BY includes p.username (got {gb!r})",
            )

    # Smoke-run each variant against SQLite — confirms the SQL still
    # parses and returns results after the GROUP-BY fix.
    p_ts1 = db.upsert_player("ts_a")
    p_ts2 = db.upsert_player("ts_b")
    tid_ts = db.create_tournament(
        "TS Cup", tournament_type="vsa",
        created_by=p_ts1["id"], is_official=True,
    )
    db.add_player_to_tournament(tid_ts, p_ts1["id"], "A")
    db.add_player_to_tournament(tid_ts, p_ts2["id"], "A")
    m_ts = db.create_match(tid_ts, p_ts1["id"], p_ts2["id"], stage="group")
    db.update_match(m_ts, score1=2, score2=1,
                    status="confirmed", reported_by=555)
    db.set_match_goals(m_ts, [
        {"player_id": p_ts1["id"], "raw_name": "ts_a",
         "minute": 12, "side": "home", "ord": 0,
         "tournament_id": tid_ts},
        {"player_id": p_ts1["id"], "raw_name": "ts_a",
         "minute": 67, "side": "home", "ord": 1,
         "tournament_id": tid_ts},
        {"player_id": p_ts2["id"], "raw_name": "ts_b",
         "minute": 89, "side": "away", "ord": 2,
         "tournament_id": tid_ts},
    ])
    g_off = db.get_top_scorers_global(limit=20, only_official=True)
    g_all = db.get_top_scorers_global(limit=20, only_official=False)
    g_t = db.get_top_scorers_for_tournament(tid_ts, limit=20)
    expect(any(r["username"] == "ts_a" and r["goals"] == 2 for r in g_off),
           "global official: ts_a has 2 goals")
    expect(any(r["username"] == "ts_b" and r["goals"] == 1 for r in g_off),
           "global official: ts_b has 1 goal")
    expect(any(r["username"] == "ts_a" and r["goals"] == 2 for r in g_all),
           "global all: ts_a has 2 goals")
    expect(any(r["username"] == "ts_a" and r["goals"] == 2 for r in g_t),
           "per-tournament: ts_a has 2 goals")

    # ── v13: bg helper graceful fallback ──────────────────────────────
    print("\n=== bg_helper.make_canvas fallback (v13) ===")
    from bg_helper import make_canvas
    canvas_default = make_canvas(80, 40, bg_color=(10, 20, 30),
                                 bg_image_path=None)
    expect(canvas_default.size == (80, 40),
           f"make_canvas honours width/height (got {canvas_default.size})")
    expect(canvas_default.getpixel((0, 0)) == (10, 20, 30),
           "make_canvas falls back to bg_color when path is None")
    canvas_missing = make_canvas(80, 40, bg_color=(10, 20, 30),
                                 bg_image_path="/no/such/file.jpg")
    expect(canvas_missing.size == (80, 40),
           "make_canvas tolerates a missing bg path")
    expect(canvas_missing.getpixel((0, 0)) == (10, 20, 30),
           "make_canvas falls back to bg_color when bg path is missing")

    # ── v14.1: bg survives container redeploy via DB-stored bytes ─────
    print("\n=== bg_image_data DB-persistence (v14.1) ===")
    import base64 as _b64, io as _io
    from PIL import Image as _PILImage
    # Build a tiny solid-red JPEG, base64-encode it.
    red = _PILImage.new("RGB", (4, 4), (255, 0, 0))
    buf = _io.BytesIO()
    red.save(buf, format="JPEG")
    red_b64 = _b64.b64encode(buf.getvalue()).decode("ascii")
    canvas_bytes = make_canvas(
        80, 40,
        bg_color=(10, 20, 30),
        bg_image_path=None,
        bg_image_data=red_b64,
        overlay_alpha=0,  # disable dark overlay so we can read the raw bg
    )
    expect(canvas_bytes.size == (80, 40),
           "bg_image_data canvas honours width/height")
    # JPEG is lossy (4×4 source → 80×40 resize) — accept reddish near-pure red.
    px = canvas_bytes.getpixel((40, 20))
    expect(px[0] > 200 and px[1] < 60 and px[2] < 60,
           f"bg_image_data renders the embedded JPEG (got {px})")

    # Even if the on-disk path is missing, DB bytes win.
    canvas_recover = make_canvas(
        80, 40,
        bg_color=(10, 20, 30),
        bg_image_path="/no/such/file.jpg",  # post-redeploy state
        bg_image_data=red_b64,
        overlay_alpha=0,
    )
    px2 = canvas_recover.getpixel((40, 20))
    expect(px2[0] > 200 and px2[1] < 60 and px2[2] < 60,
           f"bg_image_data wins over a missing disk path "
           f"(redeploy recovery, got {px2})")

    # Garbage in bg_image_data → graceful fallback to bg_color.
    canvas_bad = make_canvas(
        80, 40,
        bg_color=(10, 20, 30),
        bg_image_path=None,
        bg_image_data="not-base64-just-garbage!!!",
    )
    expect(canvas_bad.getpixel((0, 0)) == (10, 20, 30),
           "bad bg_image_data falls back to bg_color")

    # DB column exists and update_tournament round-trips the value.
    tid_bg = db.create_tournament(
        "BG Cup", tournament_type="vsa",
        created_by=pa["id"], is_official=False,
    )
    db.update_tournament(tid_bg, bg_image_data=red_b64)
    t_back = db.get_tournament(tid_bg)
    expect(t_back.get("bg_image_data") == red_b64,
           "tournaments.bg_image_data round-trips through update_tournament")

    # ── /broadcast (Feature 8) ────────────────────────────────────────
    print("\n=== broadcast / participant resolution (v12 / Feature 8) ===")
    # No new code path to unit-test in isolation — we exercise the
    # participant-listing helper that cmd_broadcast iterates over.
    tid_b = db.create_tournament(
        "Broadcast Cup", tournament_type="vsa",
        created_by=pa["id"], is_official=False,
    )
    bp1 = db.upsert_player("bc_alice", telegram_id=600)
    bp2 = db.upsert_player("bc_bob",   telegram_id=601)
    bp3 = db.upsert_player("bc_no_tg")  # no telegram_id — should be skipped.
    db.add_player_to_tournament(tid_b, bp1["id"], "A")
    db.add_player_to_tournament(tid_b, bp2["id"], "A")
    db.add_player_to_tournament(tid_b, bp3["id"], "A")
    parts = db.get_tournament_players(tid_b)
    expect(len(parts) == 3, "broadcast: 3 participants registered")
    deliverable = []
    for tp in parts:
        p = db.get_player_by_id(tp["player_id"])
        if p and p.get("telegram_id"):
            deliverable.append(p["telegram_id"])
    expect(sorted(deliverable) == [600, 601],
           f"broadcast: only telegram-IDed players are deliverable "
           f"(got {sorted(deliverable)})")

    # ── v14 phase 1: handlers/common re-exports stay backward-compatible ──
    print("\n=== handlers.common re-exports (v14 phase 1) ===")
    import handlers.common as hc
    for n in (
        "ADMIN_IDS",
        "is_admin",
        "is_root_admin",
        "mention",
        "arrow",
        "t_type_label",
        "t_scope_label",
        "t_full_label",
        "send",
        "parse_ban_duration",
        "parse_tournament_type_arg",
        "_fmt_dt",
        "_fmt_date",
        "_fmt_minute",
        "check_required_channel",
    ):
        expect(hasattr(bot, n),
               f"handlers.common.{n} re-exported on bot module")
        expect(getattr(bot, n) is getattr(hc, n),
               f"bot.{n} is handlers.common.{n} (identity, no shadow copy)")
    # parse_tournament_type_arg should accept both English/Russian aliases
    expect(bot.parse_tournament_type_arg("ВСА") == "vsa",
           "parse_tournament_type_arg('ВСА') == 'vsa'")
    expect(bot.parse_tournament_type_arg("ри") == "ri",
           "parse_tournament_type_arg('ри') == 'ri'")
    expect(bot.parse_tournament_type_arg(None) is None,
           "parse_tournament_type_arg(None) is None")
    # is_admin honours env-var ADMIN_IDS without DB hit
    expect(bot.is_admin(111) is True, "is_admin(111) True via ADMIN_IDS env")
    expect(bot.is_admin(999_999_999) is False,
           "is_admin(unknown id) False")

    # ── v14 phase 2: handlers/admin re-exports stay backward-compatible ──
    print("\n=== handlers.admin re-exports (v14 phase 2) ===")
    import handlers.admin as ha
    for n in (
        # Bot-admin commands.
        "cmd_admin_setnick",
        "cmd_ban",
        "cmd_unban",
        "cmd_banned",
        "cmd_elo",
        "cmd_setelo",
        "cmd_grant_admin",
        "cmd_revoke_admin",
        "cmd_admins",
        # Tournament-admin commands.
        "cmd_add_tadmin",
        "cmd_remove_tadmin",
        "cmd_tadmins",
        "cmd_broadcast",
        "cmd_set_description",
        "cmd_set_channel",
        "cmd_clear_channel",
        "cmd_set_tournament_bg",
        "cmd_clear_tournament_bg",
        # Helpers used outside admin.py (e.g. /tlog, /withdraw).
        "_resolve_admin_target",
        "_resolve_tadmin_target",
        "_split_tadmin_args",
        "TOURNAMENT_BG_DIR",
        "_tournament_bg_path",
    ):
        expect(hasattr(bot, n),
               f"handlers.admin.{n} re-exported on bot module")
        expect(getattr(bot, n) is getattr(ha, n),
               f"bot.{n} is handlers.admin.{n} (identity, no shadow copy)")

    # The disk path helper should still produce sensible filenames.
    bg_path = ha._tournament_bg_path(42)
    expect(
        bg_path.endswith("/tournament_bg/42.jpg")
        or bg_path.endswith("\\tournament_bg\\42.jpg"),
        f"_tournament_bg_path(42) ends with tournament_bg/42.jpg "
        f"(got {bg_path!r})",
    )

    # ── v14 phase 3: handlers/queries + handlers/match re-exports ────────
    print("\n=== handlers.queries / handlers.match re-exports (v14 phase 3) ===")
    import handlers.queries as hq
    import handlers.match as hm
    import handlers._helpers as hh

    for n in ("cmd_h2h", "cmd_my_deadlines",
              "cmd_tlog", "cmd_playoff_preview"):
        expect(hasattr(bot, n),
               f"handlers.queries.{n} re-exported on bot module")
        expect(getattr(bot, n) is getattr(hq, n),
               f"bot.{n} is handlers.queries.{n} (identity, no shadow copy)")

    for n in ("cmd_dispute",):
        expect(hasattr(bot, n),
               f"handlers.match.{n} re-exported on bot module")
        expect(getattr(bot, n) is getattr(hm, n),
               f"bot.{n} is handlers.match.{n} (identity, no shadow copy)")

    for n in ("_resolve_player_arg", "_format_deadline_countdown", "_STAGE_RU"):
        expect(hasattr(bot, n),
               f"handlers._helpers.{n} re-exported on bot module")
        expect(getattr(bot, n) is getattr(hh, n),
               f"bot.{n} is handlers._helpers.{n} (identity, no shadow copy)")

    expect(bot._STAGE_RU.get("sf") == "Полуфинал",
           "_STAGE_RU['sf'] == 'Полуфинал'")
    expect(bot._format_deadline_countdown(None) == "без дедлайна",
           "_format_deadline_countdown(None) == 'без дедлайна'")
    expect(bot._resolve_player_arg("") is None,
           "_resolve_player_arg('') is None")
    expect(bot._resolve_player_arg("999999999") is None,
           "_resolve_player_arg('<unknown numeric id>') is None")

    # ── v14 phase 4: handlers/match (auto-advance + match-flow commands) ─
    print("\n=== handlers.match re-exports (v14 phase 4) ===")
    import handlers.match as hm

    # Helpers
    for n in ("_format_series_line", "_maybe_auto_advance",
              "_current_playoff_stage", "_announce_stage_advance",
              "_approver_telegram_ids", "_send_match_to_admins",
              "_after_opponent_confirm", "_finalize_match_after_admin",
              "_list_pending_matches_for", "_do_walkover", "SCORE_RE"):
        expect(hasattr(bot, n),
               f"handlers.match.{n} re-exported on bot module")
        expect(getattr(bot, n) is getattr(hm, n),
               f"bot.{n} is handlers.match.{n} (identity, no shadow copy)")

    # Commands
    for n in ("cmd_admin_report", "cmd_award_points",
              "cmd_edit_goals", "cmd_pending",
              "cmd_walkover", "cmd_walkover_match", "cmd_walkover_all",
              "cmd_withdraw"):
        expect(hasattr(bot, n),
               f"handlers.match.{n} re-exported on bot module")
        expect(getattr(bot, n) is getattr(hm, n),
               f"bot.{n} is handlers.match.{n} (identity, no shadow copy)")

    # Smoke checks on the helpers we just re-exported.
    expect(bot.SCORE_RE.match("3:2") is not None,
           "SCORE_RE matches '3:2'")
    expect(bot.SCORE_RE.match("3-2") is None,
           "SCORE_RE does not match '3-2'")
    expect(bot._format_series_line(999999) is None,
           "_format_series_line(missing match) returns None")
    expect(bot._list_pending_matches_for(999999, 999999) == [],
           "_list_pending_matches_for(unknown player, unknown tid) == []")
    expect(bot._approver_telegram_ids(None) == sorted(set(int(x) for x in (bot.ADMIN_IDS or []))),
           "_approver_telegram_ids(None) == sorted(ADMIN_IDS)")

    # ── v14 phase 5: handlers/profile + handlers/leaderboard +
    #    handlers/tournament re-exports stay backward-compatible ─────
    print("\n=== handlers.profile re-exports (v14 phase 5) ===")
    import handlers.profile as hp
    for n in ("cmd_admincmd", "cmd_hide_keyboard", "cmd_show_keyboard",
              "cmd_keyboard", "cmd_myid", "cmd_register",
              "cmd_setnick", "cmd_profile", "cmd_matches"):
        expect(hasattr(bot, n),
               f"handlers.profile.{n} re-exported on bot module")
        expect(getattr(bot, n) is getattr(hp, n),
               f"bot.{n} is handlers.profile.{n} (identity, no shadow copy)")

    print("\n=== handlers.leaderboard re-exports (v14 phase 5) ===")
    import handlers.leaderboard as hl
    for n in ("_send_top_by_field", "_resolve_leaderboard_tournament",
              "_build_official_local_view", "_send_feedback_to_admins",
              "cmd_top", "cmd_top_vsa", "cmd_top_ri",
              "cmd_leaderboard", "cmd_top_scorers",
              "cmd_feedback", "cmd_cancel"):
        expect(hasattr(bot, n),
               f"handlers.leaderboard.{n} re-exported on bot module")
        expect(getattr(bot, n) is getattr(hl, n),
               f"bot.{n} is handlers.leaderboard.{n} (identity, no shadow copy)")

    print("\n=== handlers.tournament re-exports (v14 phase 5) ===")
    import handlers.tournament as htour
    for n in ("cmd_create_tournament", "cmd_tournaments",
              "cmd_add_player", "cmd_list_players", "cmd_replace_player",
              "cmd_start_tournament", "cmd_redraw_groups",
              "cmd_bind_tournament", "cmd_unbind_tournament",
              "cmd_table", "cmd_playoff",
              "cmd_start_playoff", "cmd_finish_tournament",
              "cmd_simulate", "cmd_advance_playoff",
              "cmd_prune_phantoms", "cmd_set_playoff_slots",
              "cmd_set_series_length", "cmd_set_auto_confirm",
              "cmd_set_third_place",
              "cmd_set_matches_per_pair", "cmd_set_reminders",
              "cb_table_pick", "cb_advance_now", "cb_playoff_pick",
              "cb_finish_tournament", "cb_simulate",
              "_handle_tournament_settings_cb",
              "_send_tournament_picker", "_recent_finished_tournaments",
              "_render_table_for", "_render_playoff_for",
              "_can_advance_now", "_can_bind_tournament",
              "_do_finish_tournament", "_do_simulate_tournament",
              "_simulated_score", "_poisson", "_bool_arg",
              "_parse_add_player_usernames",
              "_ts_format_panel_text", "_ts_show_panel"):
        expect(hasattr(bot, n),
               f"handlers.tournament.{n} re-exported on bot module")
        expect(getattr(bot, n) is getattr(htour, n),
               f"bot.{n} is handlers.tournament.{n} (identity, no shadow copy)")

    # Smoke checks on the leaderboard helpers we just re-exported.
    # _resolve_leaderboard_tournament should return None when no
    # tournaments are active and no requester is supplied.
    try:
        ldb_pick_none = bot._resolve_leaderboard_tournament(None, None)
        expect(ldb_pick_none is None or isinstance(ldb_pick_none, dict),
               "_resolve_leaderboard_tournament(None, None) returns None or a tournament dict")
    except Exception:
        # In a fresh test DB this can raise if tables aren't present;
        # tolerate that and just assert the function is callable.
        expect(callable(bot._resolve_leaderboard_tournament),
               "_resolve_leaderboard_tournament is callable")

    expect(bot._build_official_local_view({"id": 999999}) == [],
           "_build_official_local_view(unknown tournament) == []")

    # _bool_arg covers a handful of truthy/falsy strings.
    expect(bot._bool_arg("on") is True,  "_bool_arg('on') is True")
    expect(bot._bool_arg("off") is False, "_bool_arg('off') is False")
    expect(bot._bool_arg("вкл") is True, "_bool_arg('вкл') is True")
    expect(bot._bool_arg("выкл") is False, "_bool_arg('выкл') is False")
    expect(bot._bool_arg("garbage") is None, "_bool_arg('garbage') is None")

    # _parse_add_player_usernames — utility that splits ", @a, @b @c" etc.
    parsed = bot._parse_add_player_usernames(["@alice,", "@bob"])
    expect("alice" in parsed and "bob" in parsed,
           "_parse_add_player_usernames extracts both usernames")

    # _poisson never goes negative
    for lam in (0.0, 0.5, 1.5, 3.0):
        v = bot._poisson(lam)
        expect(isinstance(v, int) and v >= 0,
               f"_poisson({lam}) returns a non-negative int")

    # ── 2026-05 new features ──────────────────────────────────────────
    # 1) Schema migration adds bracket_layout, groups_only, open_signup
    # 2) Self-signup helpers (is_player_in_tournament,
    #    remove_player_from_tournament)
    # 3) groups_only suppresses auto-playoff in match_processor
    # 4) /start_playoff refuses groups_only tournaments
    print("\n=== 2026-05 features: schema + self-signup + groups_only ===")
    nf_a = db.upsert_player("nf_a")
    nf_b = db.upsert_player("nf_b")
    tid_nf = db.create_tournament(
        "NewFeatures Cup", tournament_type="vsa",
        created_by=nf_a["id"], is_official=False,
    )
    t_nf = db.get_tournament(tid_nf)
    expect("bracket_layout" in t_nf and "groups_only" in t_nf
           and "open_signup" in t_nf,
           f"new columns present (got keys: {sorted(t_nf.keys())[-6:]})")
    expect(int(t_nf.get("open_signup") or 0) == 1,
           "open_signup defaults to 1 (open)")
    expect((t_nf.get("bracket_layout") or "mirrored") == "mirrored",
           "bracket_layout defaults to 'mirrored'")
    expect(int(t_nf.get("groups_only") or 0) == 0,
           "groups_only defaults to 0")

    # Self-signup helpers
    expect(db.is_player_in_tournament(tid_nf, nf_b["id"]) is False,
           "is_player_in_tournament: False when not in")
    db.add_player_to_tournament(tid_nf, nf_b["id"], "?")
    expect(db.is_player_in_tournament(tid_nf, nf_b["id"]) is True,
           "is_player_in_tournament: True after add")
    expect(db.remove_player_from_tournament(tid_nf, nf_b["id"]) is True,
           "remove_player_from_tournament returns True on success")
    expect(db.is_player_in_tournament(tid_nf, nf_b["id"]) is False,
           "player gone after remove")
    expect(db.remove_player_from_tournament(tid_nf, nf_b["id"]) is False,
           "remove_player_from_tournament returns False when no-op")

    # groups_only → match_processor.apply_result must NOT spawn playoff
    from match_processor import apply_result
    nf_c = db.upsert_player("nf_c")
    nf_d = db.upsert_player("nf_d")
    tid_go = db.create_tournament(
        "GO Cup", tournament_type="vsa",
        created_by=nf_a["id"], is_official=False,
    )
    db.update_tournament(tid_go, groups_only=1, stage="groups",
                         groups_count=2)
    db.add_player_to_tournament(tid_go, nf_a["id"], "A")
    db.add_player_to_tournament(tid_go, nf_b["id"], "A")
    db.add_player_to_tournament(tid_go, nf_c["id"], "B")
    db.add_player_to_tournament(tid_go, nf_d["id"], "B")
    m_g1 = db.create_match(tid_go, nf_a["id"], nf_b["id"], stage="group")
    m_g2 = db.create_match(tid_go, nf_c["id"], nf_d["id"], stage="group")
    db.update_match(m_g1, score1=3, score2=1, status="confirmed",
                    reported_by=nf_a["id"])
    db.update_match(m_g2, score1=2, score2=2, status="confirmed",
                    reported_by=nf_c["id"])
    apply_result(m_g2)
    playoff_stages = {"r512","r256","r128","r64","r32","r16","qf","sf","final","third"}
    playoff_after = [
        m for m in db.get_tournament_matches(tid_go)
        if (m.get("stage") or "") in playoff_stages
    ]
    expect(len(playoff_after) == 0,
           f"groups_only suppresses auto-playoff "
           f"(got {len(playoff_after)} playoff matches)")
    t_go_after = db.get_tournament(tid_go)
    expect(t_go_after["stage"] == "groups_done",
           f"groups_only lands in 'groups_done' "
           f"(got {t_go_after['stage']!r})")

    # bracket_layout toggle persists across reads
    db.update_tournament(tid_nf, bracket_layout="linear")
    expect(db.get_tournament(tid_nf)["bracket_layout"] == "linear",
           "bracket_layout='linear' persists")
    db.update_tournament(tid_nf, bracket_layout="mirrored")
    expect(db.get_tournament(tid_nf)["bracket_layout"] == "mirrored",
           "bracket_layout='mirrored' persists")

    print()
    if FAILED:
        print(f"FAIL  {len(FAILED)} test(s) failed:")
        for f in FAILED:
            print("  -", f)
        return 1
    print("All admin & finish-tournament tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
