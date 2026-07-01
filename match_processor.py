"""
Match result processing: report, confirm, apply ELO + stats updates.

Two ELO modes:
- Official tournament (tournaments.is_official = 1, default for admin-created
  tournaments and the historical default before this migration):
  Match feeds the *global* pool — players.elo and the per-type mirrors
  players.elo_vsa / players.elo_ri.
- Player-created (is_official = 0): match is fully isolated — only the
  per-tournament leaderboard `tournament_elo` is touched. The global ELO and
  the ВСА/РИ pools are untouched.

Global stats (wins/losses/goals/streaks/clean sheets) are still updated in
both modes — only the rating pools differ.
"""
import json
from datetime import datetime

from database import (
    get_conn,
    get_match,
    get_player_by_id,
    get_player,
    update_match,
    update_player_stats,
    update_tournament_player,
    get_tournament_matches,
    get_tournament_elo,
    upsert_tournament_elo,
)
from elo import compute_elo_change
from tournament import advance_playoff, check_groups_complete, generate_playoff


def total_matches(player_id: int) -> int:
    conn = get_conn()
    row = conn.execute(
        """SELECT COUNT(*) AS n FROM matches
           WHERE (player1_id=? OR player2_id=?) AND status='confirmed'""",
        (player_id, player_id),
    ).fetchone()
    conn.close()
    if row is None:
        return 0
    # row supports both ['n'] (RealDictRow / sqlite3.Row by name) styles.
    try:
        return int(row["n"])
    except (KeyError, TypeError):
        return int(list(row)[0]) if not isinstance(row, dict) else int(list(row.values())[0])


def apply_result(match_id: int) -> dict:
    """
    Finalize a confirmed match:
    - Update ELO (global pool OR isolated per-tournament leaderboard,
      depending on tournaments.is_official).
    - Update global player stats (wins/losses/goals/streaks) in both modes.
    - Update group table (if group stage).
    - Check if playoff can be advanced.
    Returns a summary dict consumed by the bot.
    """
    m = get_match(match_id)
    if not m:
        raise ValueError("Match not found")
    if m["status"] != "confirmed":
        raise ValueError("Match not confirmed yet")
    if m.get("played_at"):
        raise ValueError("Match already processed")

    p1 = get_player_by_id(m["player1_id"])
    p2 = get_player_by_id(m["player2_id"])
    s1, s2 = m["score1"], m["score2"]
    tid = m["tournament_id"]

    # Penalty shootout scores (present only for playoff matches in
    # tournaments with playoff_penalties=1 where the regulation ended
    # in a draw). When present, the shootout winner is the match winner.
    pen1 = m.get("pen1")
    pen2 = m.get("pen2")
    has_pen = (
        pen1 is not None and pen2 is not None
        and s1 == s2
        and pen1 != pen2
    )

    # Tournament context
    t = None
    t_type = None
    is_official = True   # No tournament context (e.g. friendly match) → global pool
    if tid:
        from database import get_tournament
        t = get_tournament(tid)
        if t:
            t_type = t.get("tournament_type")  # 'vsa' | 'ri' | None
            # Default to official if column is absent in some legacy row.
            is_official = bool(t.get("is_official", 1))

    # ── Outcome flags & streaks (used in both modes) ─────────────────────────
    # When penalties decided the match, the shootout winner gets the W.
    if has_pen:
        if pen1 > pen2:
            new_ws1 = p1["win_streak"] + 1
            new_ws2 = 0
        else:
            new_ws1 = 0
            new_ws2 = p2["win_streak"] + 1
    elif s1 > s2:
        new_ws1 = p1["win_streak"] + 1
        new_ws2 = 0
    elif s2 > s1:
        new_ws1 = 0
        new_ws2 = p2["win_streak"] + 1
    else:
        new_ws1 = p1["win_streak"]
        new_ws2 = p2["win_streak"]

    # Outcome flags: when penalties decided the match, the shootout
    # winner gets the W (and the loser gets the L) — regulation was a
    # draw, but the match result is NOT a draw.
    if has_pen:
        is_w1 = 1 if pen1 > pen2 else 0
        is_w2 = 1 if pen2 > pen1 else 0
        is_dr = 0
    else:
        is_w1 = 1 if s1 > s2 else 0
        is_w2 = 1 if s2 > s1 else 0
        is_dr = 1 if s1 == s2 else 0

    # Defaults populated by either branch below — used to build the summary.
    elo1_before = p1["elo"]
    elo2_before = p2["elo"]
    elo1_after = p1["elo"]
    elo2_after = p2["elo"]
    delta1 = 0.0
    delta2 = 0.0
    type_field = None
    p1_typed_after = None
    p2_typed_after = None
    elo_scope = "global"   # 'global' | 'local'
    local_after_1 = None
    local_after_2 = None

    if is_official:
        # ── Global pool ELO + per-type mirror ───────────────────────────────
        g1 = total_matches(p1["id"])
        g2 = total_matches(p2["id"])
        # When penalties decided the match, pass the penalty winner as
        # the match winner to the ELO engine (treat it as a 1-0 win so
        # goal_factor doesn't inflate the delta for a shootout).
        if has_pen:
            elo_s1, elo_s2 = (1, 0) if pen1 > pen2 else (0, 1)
        else:
            elo_s1, elo_s2 = s1, s2
        new_elo1, new_elo2 = compute_elo_change(p1["elo"], p2["elo"], elo_s1, elo_s2, g1, g2)
        delta1 = new_elo1 - p1["elo"]
        delta2 = new_elo2 - p2["elo"]
        elo1_after = new_elo1
        elo2_after = new_elo2

        type_field = {"vsa": "elo_vsa", "ri": "elo_ri"}.get(t_type or "")
        if type_field:
            p1_typed_before = p1.get(type_field, 0) or 0
            p2_typed_before = p2.get(type_field, 0) or 0
            p1_typed_after = p1_typed_before + delta1
            p2_typed_after = p2_typed_before + delta2

        p1_updates = dict(
            elo=new_elo1,
            goals_scored=p1["goals_scored"] + s1,
            goals_conceded=p1["goals_conceded"] + s2,
            wins=p1["wins"] + is_w1,
            losses=p1["losses"] + is_w2,
            draws=p1["draws"] + is_dr,
            clean_sheets=p1["clean_sheets"] + (1 if s2 == 0 else 0),
            win_streak=new_ws1,
            best_streak=max(p1["best_streak"], new_ws1),
        )
        if type_field:
            p1_updates[type_field] = p1_typed_after
        update_player_stats(p1["id"], **p1_updates)

        p2_updates = dict(
            elo=new_elo2,
            goals_scored=p2["goals_scored"] + s2,
            goals_conceded=p2["goals_conceded"] + s1,
            wins=p2["wins"] + is_w2,
            losses=p2["losses"] + is_w1,
            draws=p2["draws"] + is_dr,
            clean_sheets=p2["clean_sheets"] + (1 if s1 == 0 else 0),
            win_streak=new_ws2,
            best_streak=max(p2["best_streak"], new_ws2),
        )
        if type_field:
            p2_updates[type_field] = p2_typed_after
        update_player_stats(p2["id"], **p2_updates)
    else:
        # ── Isolated tournament leaderboard ─────────────────────────────────
        # Global ELO / ELO_VSA / ELO_RI are NOT touched. Only `tournament_elo`
        # is updated. K-factor scaling uses per-tournament games count so a
        # player's first game in this tournament gets the new-player K bump.
        elo_scope = "local"
        local_p1 = get_tournament_elo(tid, p1["id"])
        local_p2 = get_tournament_elo(tid, p2["id"])
        if has_pen:
            elo_s1, elo_s2 = (1, 0) if pen1 > pen2 else (0, 1)
        else:
            elo_s1, elo_s2 = s1, s2
        new_e1, new_e2 = compute_elo_change(
            local_p1["elo"], local_p2["elo"], elo_s1, elo_s2,
            local_p1["games"], local_p2["games"],
        )
        delta1 = new_e1 - local_p1["elo"]
        delta2 = new_e2 - local_p2["elo"]
        elo1_before = local_p1["elo"]
        elo2_before = local_p2["elo"]
        elo1_after = new_e1
        elo2_after = new_e2
        local_after_1 = new_e1
        local_after_2 = new_e2

        upsert_tournament_elo(
            tid, p1["id"],
            elo=new_e1,
            games=local_p1["games"] + 1,
            wins=local_p1["wins"] + is_w1,
            draws=local_p1["draws"] + is_dr,
            losses=local_p1["losses"] + is_w2,
            goals_for=local_p1["goals_for"] + s1,
            goals_against=local_p1["goals_against"] + s2,
        )
        upsert_tournament_elo(
            tid, p2["id"],
            elo=new_e2,
            games=local_p2["games"] + 1,
            wins=local_p2["wins"] + is_w2,
            draws=local_p2["draws"] + is_dr,
            losses=local_p2["losses"] + is_w1,
            goals_for=local_p2["goals_for"] + s2,
            goals_against=local_p2["goals_against"] + s1,
        )

        # Update GLOBAL stats only (no ELO!) so the player's global profile
        # still tracks wins/losses/goals/streaks. Per the spec only
        # "общий ELO/ВСА/РИ не задеваются".
        update_player_stats(
            p1["id"],
            goals_scored=p1["goals_scored"] + s1,
            goals_conceded=p1["goals_conceded"] + s2,
            wins=p1["wins"] + is_w1,
            losses=p1["losses"] + is_w2,
            draws=p1["draws"] + is_dr,
            clean_sheets=p1["clean_sheets"] + (1 if s2 == 0 else 0),
            win_streak=new_ws1,
            best_streak=max(p1["best_streak"], new_ws1),
        )
        update_player_stats(
            p2["id"],
            goals_scored=p2["goals_scored"] + s2,
            goals_conceded=p2["goals_conceded"] + s1,
            wins=p2["wins"] + is_w2,
            losses=p2["losses"] + is_w1,
            draws=p2["draws"] + is_dr,
            clean_sheets=p2["clean_sheets"] + (1 if s1 == 0 else 0),
            win_streak=new_ws2,
            best_streak=max(p2["best_streak"], new_ws2),
        )

    # ── Update group table (group stage only — applies to both modes) ───────
    if m["stage"] == "group" and tid:
        if s1 > s2:
            pts1, pts2 = 3, 0
            w1, d1, l1 = 1, 0, 0
            w2, d2, l2 = 0, 0, 1
        elif s1 < s2:
            pts1, pts2 = 0, 3
            w1, d1, l1 = 0, 0, 1
            w2, d2, l2 = 1, 0, 0
        else:
            pts1, pts2 = 1, 1
            w1, d1, l1 = 0, 1, 0
            w2, d2, l2 = 0, 1, 0

        # Fetch current group stats
        conn = get_conn()
        tp1 = conn.execute(
            "SELECT * FROM tournament_players WHERE tournament_id=? AND player_id=?",
            (tid, p1["id"]),
        ).fetchone()
        tp2 = conn.execute(
            "SELECT * FROM tournament_players WHERE tournament_id=? AND player_id=?",
            (tid, p2["id"]),
        ).fetchone()
        conn.close()

        if tp1:
            update_tournament_player(
                tid, p1["id"],
                group_points=tp1["group_points"] + pts1,
                group_gf=tp1["group_gf"] + s1,
                group_ga=tp1["group_ga"] + s2,
                group_wins=tp1["group_wins"] + w1,
                group_draws=tp1["group_draws"] + d1,
                group_losses=tp1["group_losses"] + l1,
            )
        if tp2:
            update_tournament_player(
                tid, p2["id"],
                group_points=tp2["group_points"] + pts2,
                group_gf=tp2["group_gf"] + s2,
                group_ga=tp2["group_ga"] + s1,
                group_wins=tp2["group_wins"] + w2,
                group_draws=tp2["group_draws"] + d2,
                group_losses=tp2["group_losses"] + l2,
            )

    # ── Mark match as processed ───────────────────────────────────────────────
    update_match(match_id, played_at=datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"))

    # ── Check for playoff advancement ─────────────────────────────────────────
    advanced_stage = None
    if tid:
        from database import get_tournament
        t = get_tournament(tid)
        if t and t["stage"] == "playoff":
            advanced_stage = advance_playoff(tid)
        elif t and t["stage"] in ("groups", "groups_done"):
            was_already_done = t["stage"] == "groups_done"
            if check_groups_complete(tid):
                # Groups-only tournaments stop here — no playoff bracket
                # is generated even when there are multiple groups, so
                # the leaderboard of the group stage IS the final table.
                if int(t.get("groups_only") or 0):
                    from database import update_tournament
                    update_tournament(tid, stage="groups_done")
                    # Announce the league finishing exactly once: the
                    # FIRST confirmation that closes the table flips
                    # ``stage`` from ``groups`` to ``groups_done``.
                    # Subsequent confirmations on already-confirmed
                    # rounds (rare, but possible after edits) won't
                    # re-announce because ``was_already_done`` is True.
                    if not was_already_done:
                        advanced_stage = "groups_done"
                else:
                    # Auto-advance: build the playoff bracket immediately using
                    # the configured `playoff_slots` (default 2 per group). Only
                    # build if there isn't already a bracket — otherwise we'd
                    # duplicate matches on repeated calls. Requires at least
                    # 2 groups so a real bracket can form.
                    playoff_existing = [
                        pm for pm in get_tournament_matches(tid)
                        if pm.get("stage") in (
                            "r512", "r256", "r128", "r64", "r32", "r16",
                            "qf", "sf", "final", "third",
                        )
                    ]
                    # Count actual groups present in tournament_players. Building
                    # a bracket only makes sense if there are at least two
                    # distinct groups — otherwise generate_playoff will duplicate
                    # the only pair as a "playoff" round.
                    from database import get_tournament_players
                    tp_groups = {
                        (tp.get("group_name") or "")
                        for tp in get_tournament_players(tid)
                        if tp.get("group_name")
                    }
                    if not playoff_existing and len(tp_groups) >= 2:
                        slots = int(t.get("playoff_slots") or 2)
                        try:
                            bracket = generate_playoff(tid, advance_per_group=slots)
                            if bracket:
                                advanced_stage = bracket[0]["stage"]
                        except Exception:
                            from database import update_tournament
                            update_tournament(tid, stage="groups_done")

    # ── Build achievement flags ───────────────────────────────────────────────
    # "Upset" only makes sense relative to the rating that actually moved.
    if is_official:
        is_upset = p2["elo"] - p1["elo"] > 150 and s1 > s2
    else:
        is_upset = (elo2_before - elo1_before) > 150 and s1 > s2
    is_thriller = s1 + s2 >= 7

    return {
        "player1": p1["username"],
        "player2": p2["username"],
        "score1": s1,
        "score2": s2,
        "elo1_before": round(elo1_before),
        "elo2_before": round(elo2_before),
        "elo1_after": round(elo1_after),
        "elo2_after": round(elo2_after),
        "delta1": round(delta1),
        "delta2": round(delta2),
        "is_upset": is_upset,
        "is_thriller": is_thriller,
        "win_streak1": new_ws1,
        "win_streak2": new_ws2,
        "advanced_stage": advanced_stage,
        "t_type": t_type,
        "is_official": is_official,
        "elo_scope": elo_scope,         # 'global' | 'local'
        "elo_typed_field": type_field,  # only set in global mode
        "p1_typed_after": round(p1_typed_after) if p1_typed_after is not None else None,
        "p2_typed_after": round(p2_typed_after) if p2_typed_after is not None else None,
        "local_elo_after_1": round(local_after_1) if local_after_1 is not None else None,
        "local_elo_after_2": round(local_after_2) if local_after_2 is not None else None,
    }


def revert_match(match_id: int) -> dict:
    """Undo a confirmed+processed match: reverse ELO, stats, group points.

    Returns a summary dict describing what was reverted. The match is
    reset to ``status='pending'``, scores are cleared, ``played_at`` is
    NULLed — so it can be replayed or deleted.

    Raises ValueError if the match doesn't exist or wasn't processed.
    """
    m = get_match(match_id)
    if not m:
        raise ValueError("Match not found")
    if m["status"] != "confirmed" or not m.get("played_at"):
        raise ValueError("Match is not in confirmed+processed state")

    p1 = get_player_by_id(m["player1_id"])
    p2 = get_player_by_id(m["player2_id"])
    if not p1 or not p2:
        raise ValueError("Players not found")

    s1, s2 = m["score1"], m["score2"]
    tid = m["tournament_id"]

    # Tournament context
    t = None
    t_type = None
    is_official = True
    if tid:
        from database import get_tournament
        t = get_tournament(tid)
        if t:
            t_type = t.get("tournament_type")
            is_official = bool(t.get("is_official", 1))

    # ── Outcome flags (same logic as apply_result, but subtracted) ────────────
    is_w1 = 1 if s1 > s2 else 0
    is_w2 = 1 if s2 > s1 else 0
    is_dr = 1 if s1 == s2 else 0

    if is_official:
        # ── Reverse global pool ELO ─────────────────────────────────────────
        # We recompute the delta that was applied and subtract it.
        # Because total_matches now counts THIS match as confirmed, we
        # need (total - 1) to reproduce the K-factor that was used.
        g1 = max(0, total_matches(p1["id"]) - 1)
        g2 = max(0, total_matches(p2["id"]) - 1)
        new_elo1, new_elo2 = compute_elo_change(
            p1["elo"] - (p1["elo"] - p1["elo"]),  # placeholder
            p2["elo"] - (p2["elo"] - p2["elo"]),
            s1, s2, g1, g2,
        )
        # Actually: we need to figure out the elo BEFORE this match.
        # The delta was: new_elo = old_elo + delta.
        # So old_elo = current_elo - delta. But delta depends on old_elo.
        # Best approach: recompute from (current - delta) by solving
        # iteratively. However since we know the formula is deterministic,
        # the simplest correct approach is to recompute the delta from
        # (elo_before_this_match) which we can recover as:
        #   elo_before = current_elo - delta_that_was_applied
        # The delta depends on elo_before, games_before — making this a
        # fixed-point. In practice, the safest approach is:
        #   1. Find what old_elo1, old_elo2 would give compute_elo_change
        #      → current elo. We approximate by inverting the formula.
        #
        # Simpler: just recompute the delta using current state minus
        # the match contribution. Since games count changed by 1,
        # and ELO changed by delta, we reverse engineer:
        #
        # For correctness under all K-factor regimes, we iterate:
        # Guess old_elo = current_elo, compute delta, refine.
        # In practice one step is enough (K is fixed within the bracket).

        # Iterative reversal (2 iterations is enough for convergence)
        est_old1 = p1["elo"]
        est_old2 = p2["elo"]
        for _ in range(3):
            test1, test2 = compute_elo_change(est_old1, est_old2, s1, s2, g1, g2)
            delta1 = test1 - est_old1
            delta2 = test2 - est_old2
            est_old1 = p1["elo"] - delta1
            est_old2 = p2["elo"] - delta2

        reverted_elo1 = est_old1
        reverted_elo2 = est_old2

        type_field = {"vsa": "elo_vsa", "ri": "elo_ri"}.get(t_type or "")

        p1_updates = dict(
            elo=reverted_elo1,
            goals_scored=max(0, p1["goals_scored"] - s1),
            goals_conceded=max(0, p1["goals_conceded"] - s2),
            wins=max(0, p1["wins"] - is_w1),
            losses=max(0, p1["losses"] - is_w2),
            draws=max(0, p1["draws"] - is_dr),
            clean_sheets=max(0, p1["clean_sheets"] - (1 if s2 == 0 else 0)),
        )
        if type_field:
            old_typed = (p1.get(type_field) or 0) - (p1["elo"] - reverted_elo1)
            p1_updates[type_field] = old_typed

        p2_updates = dict(
            elo=reverted_elo2,
            goals_scored=max(0, p2["goals_scored"] - s2),
            goals_conceded=max(0, p2["goals_conceded"] - s1),
            wins=max(0, p2["wins"] - is_w2),
            losses=max(0, p2["losses"] - is_w1),
            draws=max(0, p2["draws"] - is_dr),
            clean_sheets=max(0, p2["clean_sheets"] - (1 if s1 == 0 else 0)),
        )
        if type_field:
            old_typed2 = (p2.get(type_field) or 0) - (p2["elo"] - reverted_elo2)
            p2_updates[type_field] = old_typed2

        update_player_stats(p1["id"], **p1_updates)
        update_player_stats(p2["id"], **p2_updates)
    else:
        # ── Reverse isolated tournament ELO ─────────────────────────────────
        local_p1 = get_tournament_elo(tid, p1["id"])
        local_p2 = get_tournament_elo(tid, p2["id"])

        # Reverse: subtract 1 game, revert wins/draws/losses/goals
        est_old1 = local_p1["elo"]
        est_old2 = local_p2["elo"]
        g1_local = max(0, local_p1["games"] - 1)
        g2_local = max(0, local_p2["games"] - 1)
        for _ in range(3):
            test1, test2 = compute_elo_change(est_old1, est_old2, s1, s2, g1_local, g2_local)
            delta1 = test1 - est_old1
            delta2 = test2 - est_old2
            est_old1 = local_p1["elo"] - delta1
            est_old2 = local_p2["elo"] - delta2

        upsert_tournament_elo(
            tid, p1["id"],
            elo=est_old1,
            games=g1_local,
            wins=max(0, local_p1["wins"] - is_w1),
            draws=max(0, local_p1["draws"] - is_dr),
            losses=max(0, local_p1["losses"] - is_w2),
            goals_for=max(0, local_p1["goals_for"] - s1),
            goals_against=max(0, local_p1["goals_against"] - s2),
        )
        upsert_tournament_elo(
            tid, p2["id"],
            elo=est_old2,
            games=g2_local,
            wins=max(0, local_p2["wins"] - is_w2),
            draws=max(0, local_p2["draws"] - is_dr),
            losses=max(0, local_p2["losses"] - is_w1),
            goals_for=max(0, local_p2["goals_for"] - s2),
            goals_against=max(0, local_p2["goals_against"] - s1),
        )

        # Reverse global stats (no ELO) — same as in apply_result's else branch
        update_player_stats(
            p1["id"],
            goals_scored=max(0, p1["goals_scored"] - s1),
            goals_conceded=max(0, p1["goals_conceded"] - s2),
            wins=max(0, p1["wins"] - is_w1),
            losses=max(0, p1["losses"] - is_w2),
            draws=max(0, p1["draws"] - is_dr),
            clean_sheets=max(0, p1["clean_sheets"] - (1 if s2 == 0 else 0)),
        )
        update_player_stats(
            p2["id"],
            goals_scored=max(0, p2["goals_scored"] - s2),
            goals_conceded=max(0, p2["goals_conceded"] - s1),
            wins=max(0, p2["wins"] - is_w2),
            losses=max(0, p2["losses"] - is_w1),
            draws=max(0, p2["draws"] - is_dr),
            clean_sheets=max(0, p2["clean_sheets"] - (1 if s1 == 0 else 0)),
        )

    # ── Reverse group table (group stage only) ──────────────────────────────
    if m.get("stage") == "group" and tid:
        if s1 > s2:
            pts1, pts2 = 3, 0
            w1, d1, l1 = 1, 0, 0
            w2, d2, l2 = 0, 0, 1
        elif s1 < s2:
            pts1, pts2 = 0, 3
            w1, d1, l1 = 0, 0, 1
            w2, d2, l2 = 1, 0, 0
        else:
            pts1, pts2 = 1, 1
            w1, d1, l1 = 0, 1, 0
            w2, d2, l2 = 0, 1, 0

        conn = get_conn()
        tp1 = conn.execute(
            "SELECT * FROM tournament_players WHERE tournament_id=? AND player_id=?",
            (tid, p1["id"]),
        ).fetchone()
        tp2 = conn.execute(
            "SELECT * FROM tournament_players WHERE tournament_id=? AND player_id=?",
            (tid, p2["id"]),
        ).fetchone()
        conn.close()

        if tp1:
            update_tournament_player(
                tid, p1["id"],
                group_points=max(0, tp1["group_points"] - pts1),
                group_gf=max(0, tp1["group_gf"] - s1),
                group_ga=max(0, tp1["group_ga"] - s2),
                group_wins=max(0, tp1["group_wins"] - w1),
                group_draws=max(0, tp1["group_draws"] - d1),
                group_losses=max(0, tp1["group_losses"] - l1),
            )
        if tp2:
            update_tournament_player(
                tid, p2["id"],
                group_points=max(0, tp2["group_points"] - pts2),
                group_gf=max(0, tp2["group_gf"] - s2),
                group_ga=max(0, tp2["group_ga"] - s1),
                group_wins=max(0, tp2["group_wins"] - w2),
                group_draws=max(0, tp2["group_draws"] - d2),
                group_losses=max(0, tp2["group_losses"] - l2),
            )

    # ── Reset match to pending state ─────────────────────────────────────────
    update_match(
        match_id,
        status="pending",
        score1=None,
        score2=None,
        played_at=None,
        reported_by=None,
    )

    return {
        "match_id": match_id,
        "player1": p1["username"],
        "player2": p2["username"],
        "reverted_score": f"{s1}:{s2}",
        "tournament_id": tid,
        "is_official": is_official,
    }


def apply_walkover(match_id: int, loser_id: int) -> dict:
    """Apply a technical walkover (0:3) for a player who missed the deadline."""
    m = get_match(match_id)
    if not m:
        raise ValueError("Match not found")
    if m.get("played_at"):
        raise ValueError("Match already processed")

    if loser_id == m["player1_id"]:
        update_match(match_id, score1=0, score2=3, status="confirmed")
    else:
        update_match(match_id, score1=3, score2=0, status="confirmed")

    return apply_result(match_id)
