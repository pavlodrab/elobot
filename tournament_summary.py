"""
Post-tournament summary: structured stats, plain-text report, optional
AI analysis via OpenRouter, and a Telegra.ph publisher.

Public entry points:

* ``compute_tournament_summary(tid)`` — read-only projection of a finished
  (or in-progress) tournament: podium, per-player elimination stage, group
  standings, full bracket, top scorers, biggest win/loss, ELO leaderboard.
* ``format_tournament_summary_text(summary)`` — multi-line plain-text
  digest, ready to be uploaded as a ``.txt`` Telegram document or
  pasted into Telegra.ph.
* ``format_tournament_summary_telegraph_nodes(summary, ai_text=None)`` —
  Telegra.ph "Node[]" representation of the same digest.
* ``analyze_with_openrouter(summary_text, lang="ru")`` — best-effort AI
  analysis using the free OpenRouter models requested by the operator
  (NVIDIA Nemotron Super, Owl-Alpha, GPT-OSS-120B). Returns the analysis
  string or ``None`` if every model fails.
* ``publish_to_telegraph(title, summary, ai_text=None, author="GovNL bot")``
  — anonymous Telegra.ph publish. Returns ``{"url": "...", "path": "..."}``
  or ``None`` if the API call fails.

The module deliberately has **no telegram dependency** so it can be
unit-tested in isolation. All Telegram I/O lives in the calling
handler (``handlers/tournament.py``).
"""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from datetime import datetime
from typing import Any, Optional

import database as db
from tournament import (
    ALL_PLAYOFF_STAGES,
    PLAYOFF_STAGES,
    THIRD_PLACE_STAGE,
    _dedup_playoff_legs,
    _pair_key,
    _resolve_pair_winner,
    get_group_standings,
    get_stage_config,
    get_tournament_podium,
)

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Stage labels (Russian, mirrored from tournament.py / bot.py)
# ─────────────────────────────────────────────────────────────────────────────

_STAGE_LABELS_RU: dict[str, str] = {
    "group":   "Групповой этап",
    "groups":  "Групповой этап",
    "playoff": "Плей-офф",   # legacy stage code in older DB rows
    "po":      "Плей-офф",
    "r512":    "1/256 финала",
    "r256":    "1/128 финала",
    "r128":    "1/64 финала",
    "r64":     "1/32 финала",
    "r32":     "1/16 финала",
    "r16":     "1/8 финала",
    "qf":      "Четвертьфинал",
    "sf":      "Полуфинал",
    "final":   "Финал",
    "third":   "Матч за 3-е место",
}

# Order from "least progressed" to "most progressed" — used to rank
# eliminations so the champion appears first in the elimination block.
_ELIM_ORDER: list[str] = [
    "group", "r512", "r256", "r128", "r64",
    "r32", "r16", "qf", "sf", "third", "final",
    # "champion" is synthetic — assigned to the tournament winner.
    "champion",
]


def _stage_label(stage: str) -> str:
    return _STAGE_LABELS_RU.get(stage, stage.upper())


# ── Per-render team-tag cache ───────────────────────────────────────────────
# Mirrors the same approach used in ``playoff_image``. Populated by
# ``compute_tournament_summary(tid)`` and consumed by every
# ``_player_label*`` call so the produced ``.txt`` / Telegra.ph blocks
# include team tags ("phoenileo - Германия (@Phoenileo)") wherever a
# player name appears, without us having to thread a tag map through
# every helper.
_TAG_BY_PID: dict[int, str] = {}

# Per-render display-mode cache — populated by ``compute_tournament_summary``
# from ``tournaments.name_display_mode`` (``"full"`` / ``"tag"`` /
# ``"nick"``) and consumed by ``_player_label`` so the produced ``.txt``
# / Telegra.ph blocks honour the same per-tournament setting as the
# rendered standings / bracket / tablebomb images.
_NAME_MODE: str = "full"


def _load_tag_map(tid: int) -> None:
    """Refresh ``_TAG_BY_PID`` with the per-tournament team tags."""
    _TAG_BY_PID.clear()
    try:
        rows = db.get_tournament_players(tid)
    except Exception:
        return
    for r in rows:
        pid = r.get("player_id")
        tag = (r.get("team_tag") or "").strip()
        if isinstance(pid, int) and tag:
            _TAG_BY_PID[pid] = tag


def _load_name_mode(t: dict | None) -> None:
    """Refresh module-level ``_NAME_MODE`` from the tournament row."""
    global _NAME_MODE
    raw = ((t or {}).get("name_display_mode") or "full")
    mode = str(raw).strip().lower()
    if mode not in ("full", "tag", "nick"):
        mode = "full"
    _NAME_MODE = mode


def _player_label(p: dict | None) -> str:
    """Human-readable name. Prefers ``"<nick> - <Team> (@user)"`` when a
    per-tournament team tag is registered for the player (via the
    ``_TAG_BY_PID`` cache populated at the top of
    ``compute_tournament_summary``); falls back through ``@username``,
    nickname and synthetic-id-aware shortenings.

    Honours the per-tournament ``_NAME_MODE`` override (``"full"`` /
    ``"tag"`` / ``"nick"``) so the text summary stays in sync with the
    rendered images when admins flip "🎨 Оформление" → "🪪 Имена".

    Never returns an empty string — always something the user can map
    to a real participant.
    """
    if not p:
        return "—"
    pid = p.get("id") or p.get("player_id")
    tag = ""
    if isinstance(pid, int):
        tag = _TAG_BY_PID.get(pid, "") or ""
    nick = (p.get("game_nickname") or "").strip()
    user = (p.get("username") or "").strip()
    is_synthetic = bool(user) and user.lower().startswith("id_") and user[3:].isdigit()
    pretty_user = "" if is_synthetic else user
    synth_label = (
        user.lower().replace("id_", "id ", 1) if is_synthetic else ""
    )

    mode = _NAME_MODE
    if mode == "tag":
        if pretty_user:
            return f"@{pretty_user}"
        if nick:
            return nick
        if tag:
            return tag
        if synth_label:
            return synth_label
        return f"id{pid}" if pid else "—"
    if mode == "nick":
        if nick and tag:
            return f"{nick} - {tag}"
        if nick:
            return nick
        if tag:
            return tag
        if pretty_user:
            return f"@{pretty_user}"
        if synth_label:
            return synth_label
        return f"id{pid}" if pid else "—"

    # mode == "full" — original behaviour, preserved verbatim.
    # Hide synthetic ``id_<digits>`` placeholder usernames the same way
    # the standings / bracket renderers do.
    if is_synthetic:
        synth = nick or synth_label
        return f"{synth} - {tag}" if tag else synth
    if user and nick:
        # When no tag is set, drop the nickname if it duplicates the
        # username case-insensitively (avoids "phoenileo (@phoenileo)").
        # When a tag IS set, always keep both so the team affiliation
        # has clear context: "phoenileo - Германия (@Phoenileo)".
        if tag:
            return f"{nick} - {tag} (@{user})"
        if nick.lower() == user.lower():
            return f"@{user}"
        return f"{nick} (@{user})"
    if user:
        if tag:
            return f"{tag} (@{user})"
        return f"@{user}"
    if nick:
        return f"{nick} - {tag}" if tag else nick
    if tag:
        return tag
    return f"id{pid}" if pid else "—"


def _player_label_by_id(pid: int | None) -> str:
    if not pid:
        return "—"
    p = db.get_player_by_id(pid)
    return _player_label(p)


# ─────────────────────────────────────────────────────────────────────────────
# Core: compute_tournament_summary
# ─────────────────────────────────────────────────────────────────────────────

def _format_type(t: dict) -> str:
    raw = (t.get("tournament_type") or "").lower()
    return {"vsa": "ВСА", "ri": "РИ"}.get(raw, raw.upper() or "—")


def _format_format(t: dict) -> str:
    if int(t.get("bracket_only") or 0):
        return "Сразу плей-офф (без групп)"
    if int(t.get("groups_only") or 0):
        return "Только группы (без плей-офф)"
    return "Группы + плей-офф"


def _confirmed_matches(tid: int) -> list[dict]:
    return [m for m in db.get_tournament_matches(tid) if m.get("status") == "confirmed"]


def _aggregate_scores(pair_legs: list[dict]) -> tuple[int, int, int, int, list[str]]:
    """Sum the score of all legs from the perspective of ``ms[0]``'s
    player1 / player2. Returns ``(a_id, b_id, a_goals, b_goals, leg_strs)``.
    """
    pair_sorted = sorted(pair_legs, key=lambda x: x.get("leg") or 1)
    a_id = pair_sorted[0]["player1_id"]
    b_id = pair_sorted[0]["player2_id"]
    a_goals = b_goals = 0
    legs: list[str] = []
    for m in pair_sorted:
        s1 = int(m.get("score1") or 0)
        s2 = int(m.get("score2") or 0)
        if m["player1_id"] == a_id:
            a_goals += s1
            b_goals += s2
            legs.append(f"{s1}:{s2}")
        else:
            a_goals += s2
            b_goals += s1
            legs.append(f"{s2}:{s1}")
    return a_id, b_id, a_goals, b_goals, legs


def _compute_eliminations(tid: int, t: dict, players: list[dict]) -> list[dict]:
    """For every tournament participant decide the latest stage they
    survived — i.e. the stage at which they were eliminated (or
    ``"champion"`` for the trophy winner).

    Result: list of ``{player_id, username, label, stage_code}`` sorted
    so the champion is first and group-stage casualties are last.
    """
    # Build {player_id: latest_stage_survived}. Default = "group" for
    # everyone in the roster.
    elim: dict[int, str] = {}
    for p in players:
        pid = p["player_id"]
        elim[pid] = "group"

    # Walk every playoff stage. Players who appeared in stage X but did
    # NOT win their pair were eliminated AT stage X. Pair winners
    # advance and get a later stage assigned in the next iteration.
    podium = get_tournament_podium(tid)

    for s in PLAYOFF_STAGES:
        rows = _dedup_playoff_legs(db.get_tournament_matches(tid, stage=s))
        if not rows:
            continue
        cfg = get_stage_config(t, s)
        # Group legs by pair.
        pairs: dict[tuple[int, int], list[dict]] = {}
        for m in rows:
            pairs.setdefault(_pair_key(m), []).append(m)
        for key, ms in pairs.items():
            a_id, b_id = key
            # Bye row — same player both sides; just mark them as
            # "still alive" at this stage. They'll get eliminated at
            # the NEXT stage if they lose, otherwise they end up the
            # champion.
            if a_id == b_id:
                elim[a_id] = s
                continue
            ms_sorted = sorted(ms, key=lambda x: x.get("leg") or 1)
            all_done = all((m.get("status") or "") == "confirmed" for m in ms_sorted)
            if not all_done:
                # Stage in progress — record both as "made it to stage s"
                # so the report still shows partial progress.
                elim.setdefault(a_id, s)
                elim.setdefault(b_id, s)
                if _ELIM_ORDER.index(s) > _ELIM_ORDER.index(elim[a_id]):
                    elim[a_id] = s
                if _ELIM_ORDER.index(s) > _ELIM_ORDER.index(elim[b_id]):
                    elim[b_id] = s
                continue
            w = _resolve_pair_winner(
                ms_sorted, advance_mode=cfg["mode"], series_len=cfg["len"],
            )
            loser = b_id if w == a_id else a_id
            # Loser eliminated at stage s.
            if (s != elim.get(loser)
                and _ELIM_ORDER.index(s) >= _ELIM_ORDER.index(elim.get(loser, "group"))):
                elim[loser] = s
            # Winner advanced — bump their "latest survived" pointer.
            if w is not None:
                # We mark survival as "the next stage they'll play".
                # When the next stage runs, this gets overwritten.
                elim[w] = s

    # 3rd-place fixture: SF losers played here. Update only the loser's
    # stage to ``"third"`` (the winner is bronze, so they're "more
    # advanced" than the loser).
    third_rows = _dedup_playoff_legs(db.get_tournament_matches(tid, stage=THIRD_PLACE_STAGE))
    if third_rows:
        cfg = get_stage_config(t, THIRD_PLACE_STAGE)
        ms_sorted = sorted(third_rows, key=lambda x: x.get("leg") or 1)
        if all((m.get("status") or "") == "confirmed" for m in ms_sorted):
            a_id = ms_sorted[0]["player1_id"]
            b_id = ms_sorted[0]["player2_id"]
            w = _resolve_pair_winner(
                ms_sorted, advance_mode=cfg["mode"], series_len=cfg["len"],
            )
            if w is not None:
                loser = b_id if w == a_id else a_id
                # Bronze winner stays at "third" as their final stage —
                # they finished 3rd. Loser also finished at "third"
                # (eliminated in the bronze match → 4th place). The
                # text report disambiguates via the podium block.
                elim[w] = "third"
                elim[loser] = "third"

    # Apply the podium overrides (champion / final loser are decided by
    # the playoff resolution helper).
    if podium.get("first"):
        elim[int(podium["first"])] = "champion"
    if podium.get("second"):
        elim[int(podium["second"])] = "final"

    # Build the result list sorted by (stage rank desc, username asc).
    out: list[dict] = []
    for p in players:
        pid = p["player_id"]
        stage_code = elim.get(pid, "group")
        if stage_code == "champion":
            label = "🏆 Чемпион"
        elif stage_code == "final":
            label = "🥈 Финалист"
        elif stage_code == "third":
            # Could be 3rd or 4th; podium clarifies.
            if podium.get("third") == pid:
                label = "🥉 3-е место"
            elif podium.get("fourth") == pid:
                label = "4-е место (бронзовый матч)"
            elif pid in (podium.get("third_tied") or []):
                label = "🥉 1/2 финала (без бронзового матча)"
            else:
                label = "Бронзовый матч"
        elif stage_code == "sf":
            label = "Вылет в полуфинале"
        elif stage_code == "qf":
            label = "Вылет в четвертьфинале"
        elif stage_code in ("r16", "r32", "r64", "r128", "r256", "r512"):
            label = f"Вылет в {_stage_label(stage_code)}"
        else:
            label = "Не вышел из группы"
        out.append({
            "player_id":  pid,
            "username":   p.get("username") or "",
            "label":      label,
            "stage_code": stage_code,
        })

    # Sort: most-advanced first; ties broken by username for stability.
    out.sort(
        key=lambda r: (-_ELIM_ORDER.index(r["stage_code"]), (r["username"] or "").lower())
    )
    return out


def _build_groups_block(tid: int) -> list[dict]:
    """Return ``[{"letter", "standings": [...]}]`` for every group.
    ``standings`` rows are pre-sorted by points / GD / GF.
    """
    standings = get_group_standings(tid)
    out: list[dict] = []
    for letter in sorted(standings.keys()):
        rows = standings[letter]
        # Skip the synthetic "?" lobby pseudo-group when present.
        if letter in ("", "?"):
            continue
        formatted = []
        for pos, p in enumerate(rows, 1):
            played = (
                int(p.get("group_wins") or 0)
                + int(p.get("group_draws") or 0)
                + int(p.get("group_losses") or 0)
            )
            gf = int(p.get("group_gf") or 0)
            ga = int(p.get("group_ga") or 0)
            formatted.append({
                "pos":      pos,
                "username": p.get("username") or "",
                "played":   played,
                "wins":     int(p.get("group_wins") or 0),
                "draws":    int(p.get("group_draws") or 0),
                "losses":   int(p.get("group_losses") or 0),
                "gf":       gf,
                "ga":       ga,
                "gd":       gf - ga,
                "pts":      int(p.get("group_points") or 0),
            })
        out.append({"letter": letter, "standings": formatted})
    return out


def _build_bracket_block(tid: int, t: dict) -> list[dict]:
    """Return one block per playoff stage that has matches, in
    chronological order. Each block: ``{stage, label, matches: [...]}``.
    """
    out: list[dict] = []
    for s in PLAYOFF_STAGES + [THIRD_PLACE_STAGE]:
        rows = _dedup_playoff_legs(db.get_tournament_matches(tid, stage=s))
        if not rows:
            continue
        cfg = get_stage_config(t, s)
        # Group legs by pair.
        pairs: dict[tuple[int, int], list[dict]] = {}
        for m in rows:
            pairs.setdefault(_pair_key(m), []).append(m)

        matches_out: list[dict] = []
        for key, ms in pairs.items():
            ms_sorted = sorted(ms, key=lambda x: x.get("leg") or 1)
            a_id = ms_sorted[0]["player1_id"]
            b_id = ms_sorted[0]["player2_id"]
            pa = db.get_player_by_id(a_id) or {}
            pb = db.get_player_by_id(b_id) or {}
            a_label = _player_label(pa)
            b_label = _player_label(pb)
            if a_id == b_id:
                matches_out.append({
                    "a":         a_label,
                    "b":         "BYE",
                    "score":     "bye",
                    "winner":    a_label,
                    "legs":      ["bye"],
                    "bye":       True,
                    "confirmed": True,
                })
                continue
            all_done = all((m.get("status") or "") == "confirmed" for m in ms_sorted)
            if not all_done:
                matches_out.append({
                    "a":         a_label,
                    "b":         b_label,
                    "score":     "—",
                    "winner":    None,
                    "legs":      [(m.get("status") or "?") for m in ms_sorted],
                    "bye":       False,
                    "confirmed": False,
                })
                continue
            _, _, ag, bg, leg_strs = _aggregate_scores(ms_sorted)
            w = _resolve_pair_winner(
                ms_sorted, advance_mode=cfg["mode"], series_len=cfg["len"],
            )
            winner_label = a_label if w == a_id else b_label
            matches_out.append({
                "a":         a_label,
                "b":         b_label,
                "score":     f"{ag}:{bg}",
                "winner":    winner_label,
                "legs":      leg_strs,
                "bye":       False,
                "confirmed": True,
            })
        out.append({
            "stage":   s,
            "label":   _stage_label(s),
            "matches": matches_out,
        })
    return out


def _build_player_stats(tid: int, players: list[dict]) -> list[dict]:
    """Per-player W/D/L/GF/GA derived directly from confirmed matches.

    More reliable than reading group_* counters because it covers
    playoff matches too, which the group counters never see.
    """
    confirmed = _confirmed_matches(tid)
    stats: dict[int, dict] = {}
    for p in players:
        stats[p["player_id"]] = {
            "player_id": p["player_id"],
            "username":  p.get("username") or "",
            "played":    0,
            "wins":      0,
            "draws":     0,
            "losses":    0,
            "gf":        0,
            "ga":        0,
        }
    for m in confirmed:
        a, b = m["player1_id"], m["player2_id"]
        s1 = int(m.get("score1") or 0)
        s2 = int(m.get("score2") or 0)
        if a == b:
            # Bye row — auto-confirmed; doesn't count for stats.
            continue
        for pid, gf, ga in ((a, s1, s2), (b, s2, s1)):
            row = stats.get(pid)
            if row is None:
                continue
            row["played"] += 1
            row["gf"] += gf
            row["ga"] += ga
            if gf > ga:
                row["wins"] += 1
            elif gf == ga:
                row["draws"] += 1
            else:
                row["losses"] += 1
    return list(stats.values())


def _biggest_match(tid: int) -> dict | None:
    """Return the confirmed match with the biggest goal difference,
    or the highest-scoring one as a tie-break. ``None`` if no matches.
    """
    rows = _confirmed_matches(tid)
    best: tuple | None = None
    best_row: dict | None = None
    for m in rows:
        a = m["player1_id"]
        b = m["player2_id"]
        if a == b:
            continue  # bye
        s1 = int(m.get("score1") or 0)
        s2 = int(m.get("score2") or 0)
        diff = abs(s1 - s2)
        total = s1 + s2
        key = (diff, total)
        if best is None or key > best:
            best = key
            best_row = m
    if not best_row:
        return None
    pa = db.get_player_by_id(best_row["player1_id"]) or {}
    pb = db.get_player_by_id(best_row["player2_id"]) or {}
    return {
        "stage":   _stage_label(best_row.get("stage") or "group"),
        "a":       _player_label(pa),
        "b":       _player_label(pb),
        "score":   f"{best_row.get('score1', 0)}:{best_row.get('score2', 0)}",
        "diff":    abs(int(best_row.get('score1') or 0) - int(best_row.get('score2') or 0)),
    }


def _compute_awards(
    podium: dict, player_stats: list[dict], top_scorers: list[dict],
    biggest: dict | None, total_matches: int, total_goals: int,
    footballer_top: list[dict] | None = None,
) -> dict:
    """Decide one player per award category. The picks back the .txt
    digest **and** the visual hero PNG, so they stay in sync.

    Categories:
      * ``champion`` / ``runner_up`` / ``bronze`` — copy the podium.
      * ``best_attack`` — player with the most goals scored across all
        confirmed matches (group + playoff). Ties break by goal
        difference, then fewest matches played (so a 9-goal sprinter
        outranks an 11-goal grinder when level).
      * ``best_defense`` — fewest goals conceded; **must have played at
        least 2 matches** so a single-game lurker doesn't accidentally
        win it. Ties break by most matches played, then highest GD.
      * ``top_scorer`` — leader from match_goals (per-event scorer
        ranking via OCR). Falls back to the ``best_attack`` pick when
        no goal events were ever recorded for the tournament.
      * ``win_rate`` — best wins-per-played ratio among players with at
        least ⌈avg-played × 0.6⌉ matches. Filters out bye lurkers and
        single-fixture squad members.
      * ``spectacle`` — copy of ``biggest_match`` for the visual card.

    Returns a dict where every value is either ``None`` or a row of
    ``{username, label, value, sub}`` ready for the renderer.
    """
    def _label(u: str) -> str:
        u = (u or "").strip()
        return f"@{u}" if u else "—"

    awards: dict[str, dict | None] = {}

    # Champion / runner-up / bronze straight from the podium.
    for src_key, dst_key in (
        ("first", "champion"), ("second", "runner_up"), ("third", "bronze"),
    ):
        person = podium.get(src_key)
        awards[dst_key] = (
            {"username": person["username"], "label": person["label"]}
            if person else None
        )

    # Filter out byes / 0-played rows once — every award below uses it.
    real_players = [p for p in player_stats if p["played"] > 0]
    awards["best_attack"] = None
    awards["best_defense"] = None
    awards["win_rate"] = None

    if real_players:
        # Best attack — most goals scored.
        atk = sorted(
            real_players,
            key=lambda r: (-r["gf"], -(r["gf"] - r["ga"]), r["played"]),
        )[0]
        awards["best_attack"] = {
            "username": atk["username"],
            "label":    _label(atk["username"]),
            "value":    f"{atk['gf']} голов",
            "sub":      f"в {atk['played']} матчах · РГ {atk['gf'] - atk['ga']:+d}",
        }

        # Best defense — fewest goals against, min 2 matches.
        defenders = [r for r in real_players if r["played"] >= 2]
        if defenders:
            d = sorted(
                defenders,
                key=lambda r: (r["ga"], -r["played"], -(r["gf"] - r["ga"])),
            )[0]
            awards["best_defense"] = {
                "username": d["username"],
                "label":    _label(d["username"]),
                "value":    f"пропущено {d['ga']}",
                "sub":      f"в {d['played']} матчах · РГ {d['gf'] - d['ga']:+d}",
            }

        # Best win rate — needs a meaningful sample so 1-and-done
        # bracket fodder isn't crowned. Use 60% of the average matches
        # played as the floor.
        avg_played = sum(r["played"] for r in real_players) / len(real_players)
        floor = max(2, int(avg_played * 0.6 + 0.5))
        contenders = [r for r in real_players if r["played"] >= floor]
        if contenders:
            wr = sorted(
                contenders,
                key=lambda r: (
                    -(r["wins"] / max(1, r["played"])),
                    -r["wins"],
                    r["played"],
                ),
            )[0]
            pct = round(100 * wr["wins"] / max(1, wr["played"]))
            awards["win_rate"] = {
                "username": wr["username"],
                "label":    _label(wr["username"]),
                "value":    f"{pct}% побед",
                "sub":      f"{wr['wins']}-{wr['draws']}-{wr['losses']} в {wr['played']} матчах",
            }

    # Top scorer (Бомбардир) — by FOOTBALLER (raw_name from
    # match_goals), not by the human player who controlled them. The
    # operator wants the in-game striker's name front-and-centre, with
    # the participant @handle in the sub line so chat still knows
    # who's behind the goals. Falls back to the per-player
    # ranking, then ``best_attack``, when no raw_name was ever
    # captured (older tournaments without OCR events).
    awards["top_scorer"] = None
    if footballer_top:
        fb = footballer_top[0]
        scorer_user = (fb.get("scorer_username") or "").strip()
        sub = f"забил @{scorer_user}" if scorer_user else "по событиям матчей"
        awards["top_scorer"] = {
            "username": scorer_user,
            "label":    fb.get("raw_name") or "—",
            "value":    f"{fb['goals']} {_pluralize_goals(fb['goals'])}",
            "sub":      sub,
        }
    elif top_scorers:
        ts = top_scorers[0]
        awards["top_scorer"] = {
            "username": ts["username"],
            "label":    _label(ts["username"]),
            "value":    f"{ts['goals']} {_pluralize_goals(ts['goals'])}",
            "sub":      "по событиям матчей",
        }
    elif awards["best_attack"]:
        awards["top_scorer"] = {
            **awards["best_attack"],
            "sub": "по разнице мячей (нет данных по событиям)",
        }

    awards["spectacle"] = (
        {
            "label":  f"{biggest['a']} {biggest['score']} {biggest['b']}",
            "value":  biggest["score"],
            "sub":    f"{biggest['stage']} · разница {biggest['diff']}",
        }
        if biggest else None
    )

    awards["totals"] = {
        "matches": int(total_matches or 0),
        "goals":   int(total_goals or 0),
        "avg":     round((total_goals / total_matches), 2) if total_matches else 0.0,
    }
    return awards


def _pluralize_goals(n: int) -> str:
    """Russian-aware pluralisation: 1 гол · 2-4 гола · 5+ голов · 11-14 голов."""
    n10 = abs(int(n)) % 10
    n100 = abs(int(n)) % 100
    if 11 <= n100 <= 14:
        return "голов"
    if n10 == 1:
        return "гол"
    if 2 <= n10 <= 4:
        return "гола"
    return "голов"


def _build_elo_table(
    t: dict, player_stats: list[dict], elo_rows: list[dict],
) -> list[dict]:
    """Build the post-tournament leaderboard.

    For OFFICIAL tournaments ELO is tracked globally in ``players.elo``
    (the per-tournament ``tournament_elo`` table only fills for local
    custom events), so we ignore the JOIN's zero ELO/zero-stats output
    and rebuild the row from authoritative sources:

      * ``elo`` — current global ``players.elo`` from ``get_player_by_id``
        (already reflects this tournament's results because the
        match-confirm pipeline updated it).
      * ``wins`` / ``draws`` / ``losses`` / ``gf`` / ``ga`` — derived
        from confirmed matches in this tournament (already in
        ``player_stats``), so the panel shows what each player
        actually did *in this event* and not their lifetime totals.

    For LOCAL tournaments (``is_official == 0``) the existing
    ``tournament_elo`` row is the authoritative source — keep it as is.
    """
    is_official = bool(t.get("is_official", 1))
    elo_by_pid: dict[int, float] = {}
    if is_official:
        for ps in player_stats:
            pid = int(ps["player_id"])
            try:
                pl = db.get_player_by_id(pid) or {}
                elo_by_pid[pid] = float(pl.get("elo") or 0)
            except Exception:
                elo_by_pid[pid] = 0.0
    else:
        for r in elo_rows:
            pid = r.get("player_id")
            if pid is not None:
                elo_by_pid[int(pid)] = float(r.get("elo") or 0)

    out: list[dict] = []
    for ps in player_stats:
        pid = int(ps["player_id"])
        out.append({
            "username": ps.get("username") or "",
            "elo":      round(elo_by_pid.get(pid, 0.0)),
            "wins":     int(ps.get("wins", 0)),
            "draws":    int(ps.get("draws", 0)),
            "losses":   int(ps.get("losses", 0)),
            "gf":       int(ps.get("gf", 0)),
            "ga":       int(ps.get("ga", 0)),
        })
    out.sort(key=lambda r: (-r["elo"],
                             -(r["wins"] * 3 + r["draws"]),
                             -(r["gf"] - r["ga"]),
                             -r["gf"],
                             r["username"].lower()))
    return out


def compute_tournament_summary(tid: int) -> dict | None:
    """Build a structured projection of tournament ``tid``. ``None`` if
    the tournament does not exist."""
    t = db.get_tournament(tid)
    if not t:
        return None

    # Load per-tournament team tags before any ``_player_label`` call —
    # the cache is consumed implicitly by the label helper.
    _load_tag_map(tid)
    _load_name_mode(t)

    players = db.get_tournament_players(tid)
    confirmed = _confirmed_matches(tid)
    total_goals = sum(
        int(m.get("score1") or 0) + int(m.get("score2") or 0)
        for m in confirmed
        if m["player1_id"] != m["player2_id"]   # exclude byes
    )

    # Podium: build a richly-labelled version with player names.
    podium_raw = get_tournament_podium(tid)
    podium: dict[str, Any] = {}
    for k in ("first", "second", "third", "fourth"):
        pid = podium_raw.get(k)
        if pid:
            p = db.get_player_by_id(pid) or {}
            podium[k] = {
                "player_id": pid,
                "username":  p.get("username") or "",
                "label":     _player_label(p),
            }
    if podium_raw.get("third_tied"):
        tied = []
        for pid in podium_raw["third_tied"]:
            p = db.get_player_by_id(pid) or {}
            tied.append({
                "player_id": pid,
                "username":  p.get("username") or "",
                "label":     _player_label(p),
            })
        podium["third_tied"] = tied

    # Date range.
    played_at = [m.get("played_at") for m in confirmed if m.get("played_at")]
    started = min(played_at) if played_at else (t.get("created_at") or "")
    finished = max(played_at) if played_at else ""

    # Top scorers — uses match_goals, may be empty when OCR wasn't run.
    try:
        scorers = db.get_top_scorers_for_tournament(tid, limit=10)
    except Exception:
        log.exception("get_top_scorers_for_tournament failed for tid=%s", tid)
        scorers = []

    # Footballer-grouped top scorers (by ``match_goals.raw_name``).
    # The "Бомбардир" award shows the in-game striker's name, not the
    # human participant — operators care about which footballer was
    # the goal machine of the tournament. Empty list when OCR didn't
    # capture raw_name on any event (legacy tournaments).
    try:
        fb_rows = db.get_footballer_scorers_for_tournament(tid, limit=200)
    except Exception:
        log.exception(
            "get_footballer_scorers_for_tournament failed for tid=%s", tid
        )
        fb_rows = []
    fb_totals: dict[str, dict] = {}
    for r in fb_rows:
        name = (r.get("raw_name") or "").strip()
        if not name:
            continue
        bucket = fb_totals.setdefault(name, {
            "raw_name":          name,
            "goals":             0,
            "scorer_pid":        None,
            "scorer_username":   "",
            "_top_scorer_goals": 0,
        })
        rg = int(r.get("total_goals") or 0)
        bucket["goals"] += rg
        # Track which participant scored the most with this footballer
        # — surfaces in the sub line as "забил @handle".
        if rg > bucket["_top_scorer_goals"]:
            bucket["_top_scorer_goals"] = rg
            bucket["scorer_pid"] = r.get("player_id")
            bucket["scorer_username"] = (r.get("username") or "").strip()
    footballer_top = sorted(
        fb_totals.values(),
        key=lambda r: (-r["goals"], r["raw_name"].lower()),
    )

    # Final ELO leaderboard.
    try:
        elo_rows = db.get_tournament_leaderboard(tid)
    except Exception:
        log.exception("get_tournament_leaderboard failed for tid=%s", tid)
        elo_rows = []

    biggest = _biggest_match(tid)
    player_stats = sorted(
        _build_player_stats(tid, players),
        key=lambda r: (-r["wins"], -(r["gf"] - r["ga"]), -r["gf"], r["username"].lower()),
    )
    top_scorers_norm = [
        {"username": s.get("username") or "", "goals": int(s.get("goals") or 0)}
        for s in scorers if int(s.get("goals") or 0) > 0
    ]
    awards = _compute_awards(
        podium=podium,
        player_stats=player_stats,
        top_scorers=top_scorers_norm,
        biggest=biggest,
        total_matches=len(confirmed),
        total_goals=total_goals,
        footballer_top=footballer_top,
    )

    summary_dict: dict[str, Any] = {
        "id":             int(t["id"]),
        "name":           t.get("name") or "",
        "type":           t.get("tournament_type") or "",
        "type_label":     _format_type(t),
        "format_label":   _format_format(t),
        "stage":          t.get("stage") or "",
        "is_official":    bool(t.get("is_official", 1)),
        "started_at":     str(started or ""),
        "finished_at":    str(finished or ""),
        "total_players":  len(players),
        "total_matches":  len(confirmed),
        "total_goals":    total_goals,
        "podium":         podium,
        "groups":         _build_groups_block(tid),
        "bracket":        _build_bracket_block(tid, t),
        "eliminations":   _compute_eliminations(tid, t, players),
        "player_stats":   player_stats,
        "biggest_match":  biggest,
        "top_scorers":    top_scorers_norm,
        # Footballer-grouped top scorers (in-game player names from
        # ``match_goals.raw_name``). The summary text/telegraph
        # renderers prefer this list — operators want to see which
        # in-game striker was the goal machine, not which participant
        # racked up the totals (the per-player view is still
        # available as ``top_scorers`` for legacy consumers).
        "top_footballer_scorers": footballer_top,
        "awards":         awards,
        "elo_table":      _build_elo_table(t, player_stats, elo_rows),
    }
    # Compute facts now that the rest of the projection is ready —
    # facts can read groups/bracket from the dict above.
    summary_dict["facts"] = compute_tournament_facts(int(t["id"]), summary_dict)
    return summary_dict


# ─────────────────────────────────────────────────────────────────────────────
# Plain-text formatting (.txt file ready for Telegram document upload)
# ─────────────────────────────────────────────────────────────────────────────

def _fmt_user(u: str) -> str:
    u = (u or "").strip()
    return f"@{u}" if u else "—"


def format_tournament_summary_text(summary: dict, ai_text: str | None = None) -> str:
    """Multi-line plain-text digest. Suitable for ``.txt`` upload or
    paste into Telegra.ph as the article body.

    When ``ai_text`` is provided it becomes the LEAD of the document —
    the AI write-up sits at the top, then the structured stats follow.
    This matches the operator's requested layout: «весь текст на
    анализе ИИ, статистика и прочее ниже».
    """
    if not summary:
        return ""
    lines: list[str] = []
    lines.append(f"🏆 {summary['name']} ({summary['type_label']})")
    lines.append("=" * 60)
    lines.append(f"Формат: {summary['format_label']}")
    lines.append(f"Игроков: {summary['total_players']}")
    lines.append(f"Сыграно матчей: {summary['total_matches']}")
    lines.append(f"Всего голов: {summary['total_goals']}")
    if summary.get("started_at"):
        lines.append(f"Начало: {summary['started_at']}")
    if summary.get("finished_at"):
        lines.append(f"Окончание: {summary['finished_at']}")
    lines.append("")

    # ── AI lead (when available) — full article goes BEFORE tables. ──
    if ai_text:
        lines.append("🤖 АНАЛИЗ ОТ ИИ")
        lines.append("=" * 60)
        lines.append(ai_text.strip())
        lines.append("")
        lines.append("─" * 60)
        lines.append("📊 ЦИФРЫ И ТАБЛИЦЫ")
        lines.append("─" * 60)
        lines.append("")

    # Podium
    podium = summary.get("podium") or {}
    if podium:
        lines.append("🏅 ПОДИУМ")
        lines.append("-" * 60)
        if podium.get("first"):
            lines.append(f"🥇 1-е место: {podium['first']['label']}")
        if podium.get("second"):
            lines.append(f"🥈 2-е место: {podium['second']['label']}")
        if podium.get("third"):
            lines.append(f"🥉 3-е место: {podium['third']['label']}")
        if podium.get("fourth"):
            lines.append(f"   4-е место: {podium['fourth']['label']}")
        if podium.get("third_tied") and "third" not in podium:
            tied = ", ".join(p["label"] for p in podium["third_tied"])
            lines.append(f"🥉 3-е место (поровну, без бронзы): {tied}")
        lines.append("")

    # Awards strip — quick at-a-glance highlights mirroring the PNG.
    awards = summary.get("awards") or {}
    award_lines: list[str] = []
    for emoji, key, label in (
        ("🏆", "champion",     "Чемпион"),
        ("🥈", "runner_up",    "Финалист"),
        ("🥉", "bronze",       "Бронза"),
        ("⚽", "best_attack",  "Лучшая атака"),
        ("🛡️", "best_defense", "Лучшая оборона"),
        ("🥅", "top_scorer",   "Бомбардир"),
        ("🎯", "win_rate",     "Лучший % побед"),
    ):
        a = awards.get(key)
        if not a:
            continue
        line = f"{emoji} {label}: {a.get('label', '—')}"
        v = a.get("value")
        if v:
            line += f" — {v}"
        sub = a.get("sub")
        if sub:
            line += f" ({sub})"
        award_lines.append(line)
    if award_lines:
        lines.append("🏅 НОМИНАЦИИ ТУРНИРА")
        lines.append("-" * 60)
        for line in award_lines:
            lines.append(line)
        lines.append("")

    # Facts strip — top-6 most interesting observations (with diversity).
    facts = summary.get("facts") or []
    if facts:
        champ = ((summary.get("podium") or {}).get("first") or {}).get("label")
        lines.append(render_facts_text(facts, top=6, champion_label=champ))

    # Eliminations — every participant + the stage they bowed out at.
    elim = summary.get("eliminations") or []
    if elim:
        lines.append("🎯 КТО НА КАКОЙ СТАДИИ ВЫЛЕТЕЛ")
        lines.append("-" * 60)
        for row in elim:
            lines.append(f"• {_fmt_user(row['username'])} — {row['label']}")
        lines.append("")

    # Group standings
    groups = summary.get("groups") or []
    if groups:
        lines.append("📊 ГРУППОВОЙ ЭТАП")
        lines.append("-" * 60)
        for g in groups:
            lines.append(f"Группа {g['letter']}")
            lines.append(
                f"  {'#':>2} {'Игрок':<20} {'И':>3} {'В':>3} {'Н':>3} "
                f"{'П':>3} {'ГЗ':>4} {'ГП':>4} {'РГ':>4} {'О':>3}"
            )
            for r in g["standings"]:
                user_disp = _fmt_user(r["username"])[:20]
                lines.append(
                    f"  {r['pos']:>2} {user_disp:<20} {r['played']:>3} "
                    f"{r['wins']:>3} {r['draws']:>3} {r['losses']:>3} "
                    f"{r['gf']:>4} {r['ga']:>4} {r['gd']:>+4} {r['pts']:>3}"
                )
            lines.append("")

    # Bracket
    bracket = summary.get("bracket") or []
    if bracket:
        lines.append("⚔️ ПЛЕЙ-ОФФ")
        lines.append("-" * 60)
        for stage in bracket:
            lines.append(stage["label"])
            for m in stage["matches"]:
                if m.get("bye"):
                    lines.append(f"  {m['a']} → bye")
                    continue
                if not m.get("confirmed"):
                    lines.append(f"  {m['a']} vs {m['b']} — не сыграно")
                    continue
                legs_disp = (
                    f"  ({' · '.join(m['legs'])})" if len(m["legs"]) > 1 else ""
                )
                lines.append(
                    f"  {m['a']} {m['score']} {m['b']}{legs_disp}  →  {m['winner']}"
                )
            lines.append("")

    # Player stats
    pstats = summary.get("player_stats") or []
    if pstats:
        lines.append("📈 СТАТИСТИКА ИГРОКОВ (все матчи)")
        lines.append("-" * 60)
        lines.append(
            f"  {'Игрок':<20} {'И':>3} {'В':>3} {'Н':>3} {'П':>3} "
            f"{'ГЗ':>4} {'ГП':>4} {'РГ':>4}"
        )
        for r in pstats:
            user_disp = _fmt_user(r["username"])[:20]
            lines.append(
                f"  {user_disp:<20} {r['played']:>3} "
                f"{r['wins']:>3} {r['draws']:>3} {r['losses']:>3} "
                f"{r['gf']:>4} {r['ga']:>4} {(r['gf']-r['ga']):>+4}"
            )
        lines.append("")

    # Top scorers — prefer the footballer-grouped list (in-game player
    # names from match_goals.raw_name); fall back to the per-participant
    # ranking when no OCR raw_name was captured for the tournament.
    fb_scorers = summary.get("top_footballer_scorers") or []
    if fb_scorers:
        lines.append("⚽ БОМБАРДИРЫ")
        lines.append("-" * 60)
        for i, s in enumerate(fb_scorers, 1):
            scorer_user = (s.get("scorer_username") or "").strip()
            hint = f"  (@{scorer_user})" if scorer_user else ""
            lines.append(
                f"  {i}. {s['raw_name']} — {s['goals']} гол(а){hint}"
            )
        lines.append("")
    else:
        scorers = summary.get("top_scorers") or []
        if scorers:
            lines.append("⚽ БОМБАРДИРЫ")
            lines.append("-" * 60)
            for i, s in enumerate(scorers, 1):
                lines.append(f"  {i}. {_fmt_user(s['username'])} — {s['goals']} гол(а)")
            lines.append("")

    # Biggest match
    big = summary.get("biggest_match")
    if big:
        lines.append("💥 САМЫЙ ЗАМЕТНЫЙ МАТЧ")
        lines.append("-" * 60)
        lines.append(
            f"  {big['a']} {big['score']} {big['b']}  ({big['stage']}, "
            f"разница {big['diff']})"
        )
        lines.append("")

    # Final ELO standings
    elo = summary.get("elo_table") or []
    if elo:
        lines.append("📉 ИТОГОВЫЙ ЛИДЕРБОРД (ELO)")
        lines.append("-" * 60)
        for i, r in enumerate(elo, 1):
            user_disp = _fmt_user(r["username"])[:20]
            lines.append(
                f"  {i:>2}. {user_disp:<20} ELO {r['elo']:>5}  "
                f"({r['wins']}W {r['draws']}D {r['losses']}L, "
                f"{r['gf']}:{r['ga']})"
            )
        lines.append("")

    if ai_text:
        # Already rendered at the top as the article lead.
        pass

    lines.append("─" * 60)
    lines.append(f"Сгенерировано: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# Telegra.ph node format
# ─────────────────────────────────────────────────────────────────────────────

def _tg_p(text: str) -> dict:
    return {"tag": "p", "children": [text]}


def _tg_h(level: int, text: str) -> dict:
    return {"tag": f"h{level}" if level in (3, 4) else "h3", "children": [text]}


def _tg_pre(text: str) -> dict:
    return {"tag": "pre", "children": [text]}


def _tg_li(text: str) -> dict:
    return {"tag": "li", "children": [text]}


def format_tournament_summary_telegraph_nodes(
    summary: dict, ai_text: str | None = None
) -> list[dict]:
    """Telegra.ph "Node[]" array. Telegra.ph only supports a small set
    of tags — we use h3/h4/p/ul/li/pre/hr — and nothing else (no
    inline styles beyond plain text + <a>, <b>, <i>, <code>).

    When ``ai_text`` is provided the AI analysis becomes the **lead**
    of the article — readers see the human-friendly story first, with
    the structured tables underneath as supporting evidence. When
    ``ai_text`` is missing the article reverts to a stats-only layout.
    """
    if not summary:
        return []
    nodes: list[dict] = []

    # ── Lead: 1-line subtitle + the AI-written article (if any). ──
    nodes.append(_tg_h(4,
        f"Формат: {summary['format_label']} · "
        f"Игроков: {summary['total_players']} · "
        f"Матчей: {summary['total_matches']} · "
        f"Голов: {summary['total_goals']}"
    ))
    if summary.get("started_at") or summary.get("finished_at"):
        nodes.append(_tg_p(
            f"Период: {summary.get('started_at') or '—'} → "
            f"{summary.get('finished_at') or '—'}"
        ))

    if ai_text:
        nodes.append({"tag": "hr"})
        # No "🤖 АНАЛИЗ" heading — the AI prose IS the article. Keeping
        # it heading-less makes the post read like a journalist's
        # column with stats below.
        for chunk in ai_text.strip().split("\n\n"):
            chunk = chunk.strip()
            if chunk:
                nodes.append(_tg_p(chunk))
        nodes.append({"tag": "hr"})
        nodes.append(_tg_h(3, "📊 Цифры и таблицы"))

    podium = summary.get("podium") or {}
    if podium:
        nodes.append(_tg_h(3, "🏅 Подиум"))
        bullets: list[dict] = []
        if podium.get("first"):
            bullets.append(_tg_li(f"🥇 1-е место — {podium['first']['label']}"))
        if podium.get("second"):
            bullets.append(_tg_li(f"🥈 2-е место — {podium['second']['label']}"))
        if podium.get("third"):
            bullets.append(_tg_li(f"🥉 3-е место — {podium['third']['label']}"))
        if podium.get("fourth"):
            bullets.append(_tg_li(f"4-е место — {podium['fourth']['label']}"))
        if podium.get("third_tied") and "third" not in podium:
            tied = ", ".join(p["label"] for p in podium["third_tied"])
            bullets.append(_tg_li(f"🥉 3-е место (поровну): {tied}"))
        if bullets:
            nodes.append({"tag": "ul", "children": bullets})

    elim = summary.get("eliminations") or []
    if elim:
        nodes.append(_tg_h(3, "🎯 Кто на какой стадии вылетел"))
        nodes.append({"tag": "ul", "children": [
            _tg_li(f"{_fmt_user(r['username'])} — {r['label']}")
            for r in elim
        ]})

    groups = summary.get("groups") or []
    if groups:
        nodes.append(_tg_h(3, "📊 Групповой этап"))
        for g in groups:
            nodes.append(_tg_h(4, f"Группа {g['letter']}"))
            text_lines = [
                f"{'#':>2} {'Игрок':<18} {'И':>3} {'В':>3} {'Н':>3} "
                f"{'П':>3} {'ГЗ':>4} {'ГП':>4} {'РГ':>4} {'О':>3}"
            ]
            for r in g["standings"]:
                user_disp = _fmt_user(r["username"])[:18]
                text_lines.append(
                    f"{r['pos']:>2} {user_disp:<18} {r['played']:>3} "
                    f"{r['wins']:>3} {r['draws']:>3} {r['losses']:>3} "
                    f"{r['gf']:>4} {r['ga']:>4} {r['gd']:>+4} {r['pts']:>3}"
                )
            nodes.append(_tg_pre("\n".join(text_lines)))

    bracket = summary.get("bracket") or []
    if bracket:
        nodes.append(_tg_h(3, "⚔️ Плей-офф"))
        for stage in bracket:
            nodes.append(_tg_h(4, stage["label"]))
            items: list[dict] = []
            for m in stage["matches"]:
                if m.get("bye"):
                    items.append(_tg_li(f"{m['a']} → bye"))
                    continue
                if not m.get("confirmed"):
                    items.append(_tg_li(f"{m['a']} vs {m['b']} — не сыграно"))
                    continue
                legs_disp = (
                    f" ({' · '.join(m['legs'])})" if len(m["legs"]) > 1 else ""
                )
                items.append(_tg_li(
                    f"{m['a']} {m['score']} {m['b']}{legs_disp} → {m['winner']}"
                ))
            if items:
                nodes.append({"tag": "ul", "children": items})

    fb_scorers = summary.get("top_footballer_scorers") or []
    if fb_scorers:
        nodes.append(_tg_h(3, "⚽ Бомбардиры"))
        items = []
        for s in fb_scorers:
            scorer_user = (s.get("scorer_username") or "").strip()
            hint = f" (@{scorer_user})" if scorer_user else ""
            items.append(_tg_li(
                f"{s['raw_name']} — {s['goals']} гол(а){hint}"
            ))
        nodes.append({"tag": "ol", "children": items})
    else:
        scorers = summary.get("top_scorers") or []
        if scorers:
            nodes.append(_tg_h(3, "⚽ Бомбардиры"))
            nodes.append({"tag": "ol", "children": [
                _tg_li(f"{_fmt_user(s['username'])} — {s['goals']} гол(а)")
                for s in scorers
            ]})

    big = summary.get("biggest_match")
    if big:
        nodes.append(_tg_h(3, "💥 Самый заметный матч"))
        nodes.append(_tg_p(
            f"{big['a']} {big['score']} {big['b']} ({big['stage']}, "
            f"разница {big['diff']})"
        ))

    elo = summary.get("elo_table") or []
    if elo:
        nodes.append(_tg_h(3, "📉 Итоговый лидерборд (ELO)"))
        text_lines = [
            f"{'#':>2} {'Игрок':<18} {'ELO':>5} {'В':>3} {'Н':>3} "
            f"{'П':>3} {'ГЗ':>4} {'ГП':>4}"
        ]
        for i, r in enumerate(elo, 1):
            user_disp = _fmt_user(r["username"])[:18]
            text_lines.append(
                f"{i:>2} {user_disp:<18} {r['elo']:>5} "
                f"{r['wins']:>3} {r['draws']:>3} {r['losses']:>3} "
                f"{r['gf']:>4} {r['ga']:>4}"
            )
        nodes.append(_tg_pre("\n".join(text_lines)))

    if ai_text:
        # AI text already rendered at the top as the article lead.
        # Just close the article with the generation timestamp.
        pass

    nodes.append({"tag": "hr"})
    nodes.append(_tg_p(
        f"Сгенерировано ботом GovNL · "
        f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}"
    ))
    return nodes


# ─────────────────────────────────────────────────────────────────────────────
# OpenRouter AI analysis
# ─────────────────────────────────────────────────────────────────────────────

# Free OpenRouter models for the tournament summary, in fallback order.
#
# Selection rationale (Russian football-style narrative, must follow
# instructions and not invent stats):
#
#   1. ``qwen/qwen3-next-80b-a3b-instruct:free`` — instruction-tuned,
#      stable answers WITHOUT thinking traces, fast (3B active params).
#   2. ``z-ai/glm-4.5-air:free`` — 107B MoE, capable, multilingual.
#   3. ``meta-llama/llama-3.3-70b-instruct:free`` — most battle-tested.
#   4. ``openai/gpt-oss-120b:free`` — OpenAI open-weights.
#   5. ``nvidia/nemotron-nano-9b-v2:free`` — small but reliable.
#
# Override via env: ``TOURNAMENT_AI_MODELS`` (comma-separated, replaces
# the list) or ``TOURNAMENT_AI_MODEL`` (single override prepended to
# the defaults).
_DEFAULT_AI_MODELS: tuple[str, ...] = (
    "qwen/qwen3-next-80b-a3b-instruct:free",
    "z-ai/glm-4.5-air:free",
    "meta-llama/llama-3.3-70b-instruct:free",
    "openai/gpt-oss-120b:free",
    "nvidia/nemotron-nano-9b-v2:free",
)
_OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

# Google Gemini — generous free tier (1500 req/day), text-only call
# of the same model used by the OCR pipeline.
_GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models"


def _ai_models() -> list[str]:
    env_list = os.getenv("TOURNAMENT_AI_MODELS", "").strip()
    if env_list:
        return [m.strip() for m in env_list.split(",") if m.strip()]
    single = os.getenv("TOURNAMENT_AI_MODEL", "").strip()
    if single:
        return [single, *_DEFAULT_AI_MODELS]
    return list(_DEFAULT_AI_MODELS)


def _openrouter_keys() -> list[str]:
    """Reuse the same key rotation as the OCR module — picks up the
    hardcoded fallback keys so the AI summary works without any extra
    env-var setup."""
    keys: list[str] = []
    primary = os.getenv("OPENROUTER_API_KEY", "").strip()
    if primary:
        keys.append(primary)
    try:
        from ocr import _openrouter_keys as _ocr_keys  # type: ignore
        for k in _ocr_keys():
            if k and k not in keys:
                keys.append(k)
    except Exception:
        pass
    return keys


def _gemini_config() -> tuple[str, str]:
    """Return ``(api_key, model)`` reusing ocr.py's settings."""
    k = os.getenv("GEMINI_API_KEY", "").strip()
    m = os.getenv("GEMINI_MODEL", "gemini-2.5-flash").strip() or "gemini-2.5-flash"
    if k:
        return k, m
    try:
        from ocr import _GEMINI_API_KEY, _GEMINI_MODEL  # type: ignore
        return (_GEMINI_API_KEY or ""), (_GEMINI_MODEL or m)
    except Exception:
        return "", m


# ── Prompts. The "rich" variant asks for a longer, fact-driven write-up
#    that also surfaces interesting numerical observations the operator
#    might miss when reading the raw .txt digest.
_AI_PROMPT_RICH_RU = """Ты футбольный аналитик и комментатор.
На вход — структурированная сводка только что завершившегося турнира:
формат, подиум, кто на какой стадии вылетел, групповой этап, плей-офф,
бомбардиры, общая статистика, лидерборд по ELO, награды.

Напиши развёрнутый обзор на русском языке, 6–10 абзацев. Каждый
абзац — самостоятельная мысль. Структура:

1. Чемпион — кто, как пришёл к титулу: путь по плей-офф, ключевые
   победы, разница голов, очень кратко статистика (W-D-L, GF:GA).
2. Финалист — как добрался до финала, что не получилось.
3. Бронза или 3–4 место (если есть бронзовый матч).
4. Главная сенсация турнира: неожиданно ранний вылет фаворита,
   неожиданно высокий результат тёмной лошадки, или просто странные
   цифры (например, чемпион пропустил больше, чем кто-то из выбывших).
5. Лучший бомбардир и индивидуальные рекорды (макс голов за матч,
   лучшая разница голов, лучший процент побед).
6. Лучшая оборона vs лучшая атака — сравни цифры.
7. Самый яркий матч (по разнице мячей) — как такое получилось.
8. Несколько любопытных фактов из цифр сводки: средние голы за
   матч, общее количество голов, самые «голевые» пары и т.п.
9. Короткий итог — что запомнилось, главный вывод.

Жёсткие правила:
* Опирайся ТОЛЬКО на цифры в сводке. Не выдумывай факты, не давай
  оценок «лучший игрок ХХ года» и т.п.
* Если данных нет (например, нет бронзового матча или нет данных
  по бомбардирам), честно пиши «без данных» и пропускай абзац —
  не выдумывай.
* Никаких markdown-заголовков, никаких эмодзи, никаких списков с
  буллетами. Только связный текст. Это публицистика, не отчёт.
* Никаких служебных меток типа «Абзац 1:» — пиши сразу содержание.
* Если видишь интересное наблюдение, которое явно не вытекает из
  одной строки сводки (например, чемпион ни разу не проиграл), —
  обязательно подсвети его.
"""


def _try_openrouter(summary_text: str, timeout: float, attempts: list[str]) -> Optional[str]:
    """Try every (model × key) combo on OpenRouter. ``attempts`` collects
    a one-line audit trail per attempt for the diagnostic message."""
    keys = _openrouter_keys()
    if not keys:
        attempts.append("openrouter: нет ключей")
        return None

    models = _ai_models()
    body_template = {
        "messages": [
            {"role": "system", "content": _AI_PROMPT_RICH_RU},
            {"role": "user", "content": summary_text},
        ],
        # Russian text is multi-byte in tokenisation — a 7-paragraph
        # narrative comfortably needs 1500-2500 visible tokens, plus
        # we leave headroom for reasoning models that emit hidden
        # chain-of-thought before the answer (gpt-oss, nemotron). 5000
        # output tokens stops the wire-cut we kept seeing on the
        # qwen3-next response.
        "max_tokens": 5000,
        "temperature": 0.7,
        # Hint reasoning models to spend less effort thinking and more
        # tokens on the actual answer. Ignored by non-reasoning models.
        "reasoning": {"effort": "low"},
    }

    for model in models:
        body = {**body_template, "model": model}
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        for key in keys:
            req = urllib.request.Request(
                _OPENROUTER_URL,
                data=payload,
                headers={
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                    "HTTP-Referer": "https://github.com/fc-league-bot",
                    # NB: HTTP headers are latin-1 in stdlib http.client —
                    # keep ASCII-only here, em-dash crashes the request.
                    "X-Title": "FC League Bot - Tournament Summary",
                },
            )
            t0 = time.time()
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    raw = r.read()
            except urllib.error.HTTPError as e:
                err_body = ""
                try:
                    err_body = e.read().decode("utf-8", errors="replace")[:200]
                except Exception:
                    pass
                attempts.append(
                    f"openrouter {model} key…{key[-6:]}: HTTP {e.code} {err_body[:80]}"
                )
                if e.code in (401, 403, 429):
                    continue
                break
            except Exception as e:
                attempts.append(f"openrouter {model} key…{key[-6:]}: {type(e).__name__} {e}")
                continue
            dt = time.time() - t0
            try:
                data = json.loads(raw)
            except Exception:
                attempts.append(f"openrouter {model}: non-JSON {raw[:80]!r}")
                continue
            err = data.get("error") if isinstance(data, dict) else None
            if err:
                attempts.append(f"openrouter {model}: API error {str(err)[:120]}")
                continue
            choices = (data.get("choices") or []) if isinstance(data, dict) else []
            if not choices:
                attempts.append(f"openrouter {model}: empty choices")
                continue
            msg = choices[0].get("message") if isinstance(choices[0], dict) else None
            content = (msg.get("content") if isinstance(msg, dict) else None) or ""
            content = content.strip()
            if not content:
                content = (msg.get("reasoning") if isinstance(msg, dict) else "") or ""
                content = content.strip()
            if content:
                log.info("OpenRouter %s OK in %.1fs (%d chars)", model, dt, len(content))
                attempts.append(f"openrouter {model}: OK ({len(content)} chars, {dt:.1f}s)")
                return content
            attempts.append(f"openrouter {model}: empty content")
    return None


def _try_gemini(summary_text: str, timeout: float, attempts: list[str]) -> Optional[str]:
    """Google Gemini text fallback — 1500 req/day free, very stable."""
    key, model = _gemini_config()
    if not key:
        attempts.append("gemini: нет ключа (GEMINI_API_KEY)")
        return None
    url = f"{_GEMINI_URL}/{model}:generateContent?key={key}"
    body = json.dumps({
        "contents": [{
            "parts": [{
                "text": _AI_PROMPT_RICH_RU + "\n\n--- Сводка турнира ---\n" + summary_text,
            }],
        }],
        "generationConfig": {
            "temperature": 0.7,
            "maxOutputTokens": 5000,
        },
    }, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"},
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        err_body = ""
        try:
            err_body = e.read().decode("utf-8", errors="replace")[:200]
        except Exception:
            pass
        attempts.append(f"gemini {model}: HTTP {e.code} {err_body[:80]}")
        return None
    except Exception as e:
        attempts.append(f"gemini {model}: {type(e).__name__} {e}")
        return None
    dt = time.time() - t0
    try:
        data = json.loads(raw)
    except Exception:
        attempts.append(f"gemini {model}: non-JSON {raw[:80]!r}")
        return None
    if data.get("error"):
        attempts.append(f"gemini {model}: API error {str(data['error'])[:120]}")
        return None
    candidates = data.get("candidates") or []
    if not candidates:
        attempts.append(f"gemini {model}: empty candidates")
        return None
    parts = candidates[0].get("content", {}).get("parts", [])
    text = "".join(p.get("text", "") for p in parts if isinstance(p, dict)).strip()
    if text:
        log.info("Gemini %s OK in %.1fs (%d chars)", model, dt, len(text))
        attempts.append(f"gemini {model}: OK ({len(text)} chars, {dt:.1f}s)")
        return text
    attempts.append(f"gemini {model}: empty text")
    return None


def analyze_with_ai(
    summary_text: str,
    lang: str = "ru",
    timeout: float = 90.0,
) -> tuple[str | None, list[str]]:
    """Best-effort AI analysis with multi-provider fallback.

    Tries OpenRouter (cheapest, multiple free models) → Gemini
    (1500/day, very stable). Groq support was removed 2026-06 after
    their free tier was discontinued. Returns ``(analysis_text,
    attempts_log)`` so the caller can show the user exactly what
    failed when nothing came back.

    Default timeout bumped to 90s so reasoning models that emit a
    chain-of-thought before the actual answer can complete without
    the request being cut off mid-paragraph.
    """
    attempts: list[str] = []
    for fn in (_try_openrouter, _try_gemini):
        try:
            result = fn(summary_text, timeout, attempts)
        except Exception as e:
            log.exception("AI provider %s crashed", fn.__name__)
            attempts.append(f"{fn.__name__}: crash {type(e).__name__}: {e}")
            continue
        if result:
            return result, attempts
    log.warning("analyze_with_ai: all providers failed; attempts=%s", attempts)
    return None, attempts


# Back-compat alias used by older imports.
def analyze_with_openrouter(summary_text: str, lang: str = "ru",
                             timeout: float = 60.0) -> str | None:
    text, _ = analyze_with_ai(summary_text, lang=lang, timeout=timeout)
    return text


# ─────────────────────────────────────────────────────────────────────────────
# Telegra.ph publisher (anonymous)
# ─────────────────────────────────────────────────────────────────────────────

_TELEGRAPH_API = "https://api.telegra.ph"


def _telegraph_call(method: str, params: dict, timeout: float = 30.0) -> dict | None:
    """POST to ``api.telegra.ph/<method>`` with form-encoded params."""
    import urllib.parse
    data = urllib.parse.urlencode(
        {k: (json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict)) else v)
         for k, v in params.items() if v is not None},
    ).encode("utf-8")
    req = urllib.request.Request(
        f"{_TELEGRAPH_API}/{method}",
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
    except Exception as e:
        log.warning("telegra.ph %s failed: %s", method, e)
        return None
    try:
        data = json.loads(raw)
    except Exception:
        log.warning("telegra.ph %s: non-JSON response: %s", method, raw[:200])
        return None
    if not isinstance(data, dict) or not data.get("ok"):
        log.warning("telegra.ph %s: API error: %s", method, data)
        return None
    return data.get("result")


def _telegraph_account_token() -> str | None:
    """Reuse a cached access token via env var, or create a fresh
    anonymous account."""
    cached = os.getenv("TELEGRAPH_TOKEN", "").strip()
    if cached:
        return cached
    res = _telegraph_call("createAccount", {
        "short_name": "GovNL",
        "author_name": "GovNL bot",
    })
    if not res:
        return None
    return res.get("access_token")


def publish_to_telegraph(
    title: str,
    summary: dict,
    ai_text: str | None = None,
    author: str = "GovNL bot",
    author_url: str | None = None,
) -> dict | None:
    """Publish a tournament summary to Telegra.ph anonymously.

    Returns ``{"url": "https://telegra.ph/...", "path": "..."}`` or
    ``None`` on failure. The URL is publicly accessible immediately;
    no editing requires the access token.
    """
    token = _telegraph_account_token()
    if not token:
        return None
    nodes = format_tournament_summary_telegraph_nodes(summary, ai_text=ai_text)
    if not nodes:
        return None
    title = (title or summary.get("name") or "Турнир").strip()[:256]
    res = _telegraph_call("createPage", {
        "access_token": token,
        "title":        title,
        "author_name":  author[:128],
        "author_url":   author_url or "",
        "content":      nodes,
        "return_content": "false",
    })
    if not res:
        return None
    return {"url": res.get("url"), "path": res.get("path")}




# ─────────────────────────────────────────────────────────────────────────────
# Cross-tournament comparison: aggregate stats across every finished
# tournament so the operator can answer "who's the most successful
# participant?" / "which tournament was the goalfest?" at a glance.
# ─────────────────────────────────────────────────────────────────────────────

def compute_all_tournaments_overview(limit: int | None = None) -> dict:
    """Aggregate every finished tournament into one comparison dataset.

    Result shape (every list pre-sorted, ready for direct rendering):

      {
        "tournaments":   [...row per finished tournament, newest first...],
        "total":         int,
        "by_type":       {"vsa": N, "ri": N, "other": N},
        "totals":        {"matches": N, "goals": N, "players": N (unique)},
        "champions":     [{"username", "titles"}, ...],   # most titles
        "appearances":   [{"username", "tournaments"}, ...],
        "scorers":       [{"username", "goals"}, ...],    # combined
        "elo":           [{"username", "elo"}, ...],      # global ELO
        "biggest":       {"a", "b", "score", "diff", "tournament"} | None,
        "highest_avg":   {"name", "avg", "matches", "goals"} | None,
      }

    Use ``limit`` to cap the per-tournament rows in the listing (the
    aggregated leaderboards always cover every finished tournament).
    """
    conn = db.get_conn()
    rows = conn.execute(
        "SELECT * FROM tournaments WHERE stage = 'finished' "
        "ORDER BY id DESC"
    ).fetchall()
    finished = [dict(r) for r in rows]
    conn.close()

    by_type: dict[str, int] = {}
    rows_out: list[dict] = []
    title_counts: dict[int, int] = {}
    appearance_counts: dict[int, int] = {}
    scorer_totals: dict[str, int] = {}
    biggest: dict | None = None
    highest_avg: dict | None = None
    total_matches = 0
    total_goals = 0
    unique_players: set[int] = set()

    for t in finished:
        ttype = (t.get("tournament_type") or "").lower() or "other"
        by_type[ttype] = by_type.get(ttype, 0) + 1
        try:
            podium = get_tournament_podium(int(t["id"])) or {}
        except Exception:
            podium = {}
        first_pid = podium.get("first")
        if first_pid:
            title_counts[int(first_pid)] = title_counts.get(int(first_pid), 0) + 1
        try:
            players = db.get_tournament_players(int(t["id"]))
        except Exception:
            players = []
        for p in players:
            pid = int(p.get("player_id") or 0)
            if pid:
                unique_players.add(pid)
                appearance_counts[pid] = appearance_counts.get(pid, 0) + 1
        try:
            scorers = db.get_top_scorers_for_tournament(int(t["id"]), limit=200)
        except Exception:
            scorers = []
        for s in scorers:
            u = (s.get("username") or "").strip()
            g = int(s.get("goals") or 0)
            if u and g > 0:
                scorer_totals[u] = scorer_totals.get(u, 0) + g
        try:
            confirmed = [m for m in db.get_tournament_matches(int(t["id"]))
                         if m.get("status") == "confirmed"
                         and m.get("player1_id") != m.get("player2_id")]
        except Exception:
            confirmed = []
        t_matches = len(confirmed)
        t_goals = sum(int(m.get("score1") or 0) + int(m.get("score2") or 0)
                      for m in confirmed)
        total_matches += t_matches
        total_goals += t_goals

        # Track biggest match across all tournaments.
        for m in confirmed:
            s1 = int(m.get("score1") or 0)
            s2 = int(m.get("score2") or 0)
            diff = abs(s1 - s2)
            total = s1 + s2
            if biggest is None or (diff, total) > (biggest["diff"], biggest["total"]):
                pa = db.get_player_by_id(m["player1_id"]) or {}
                pb = db.get_player_by_id(m["player2_id"]) or {}
                biggest = {
                    "a":          _player_label(pa),
                    "b":          _player_label(pb),
                    "score":      f"{s1}:{s2}",
                    "diff":       diff,
                    "total":      total,
                    "tournament": t.get("name") or f"#{t['id']}",
                }

        # Track tournament with the highest goals-per-match average.
        if t_matches >= 3:
            avg = t_goals / t_matches
            if highest_avg is None or avg > highest_avg["avg"]:
                highest_avg = {
                    "name":    t.get("name") or f"#{t['id']}",
                    "avg":     avg,
                    "matches": t_matches,
                    "goals":   t_goals,
                }

        # Build the per-tournament row.
        first_label = "—"
        if first_pid:
            first_label = _player_label(db.get_player_by_id(int(first_pid)) or {})
        rows_out.append({
            "id":         int(t["id"]),
            "name":       t.get("name") or "—",
            "type_label": _format_type(t),
            "champion":   first_label,
            "players":    len(players),
            "matches":    t_matches,
            "goals":      t_goals,
            "avg":        round(t_goals / t_matches, 2) if t_matches else 0.0,
        })

    # ── leaderboards ──────────────────────────────────────────────────
    # Most titles.
    champs: list[dict] = []
    for pid, n in sorted(title_counts.items(), key=lambda kv: (-kv[1], kv[0])):
        p = db.get_player_by_id(pid) or {}
        champs.append({
            "player_id": pid,
            "username":  p.get("username") or "",
            "label":     _player_label(p),
            "titles":    n,
        })

    # Most appearances.
    appearances: list[dict] = []
    for pid, n in sorted(
        appearance_counts.items(), key=lambda kv: (-kv[1], kv[0])
    ):
        p = db.get_player_by_id(pid) or {}
        appearances.append({
            "player_id":   pid,
            "username":    p.get("username") or "",
            "label":       _player_label(p),
            "tournaments": n,
        })

    # Combined goal-scorers across all tournaments.
    scorers_combined = sorted(
        ({"username": u, "label": f"@{u}", "goals": g}
         for u, g in scorer_totals.items()),
        key=lambda r: (-r["goals"], r["username"]),
    )

    # Global ELO leaderboard — pulls all players ordered by ELO desc
    # and surfaces the top 20.
    elo_top: list[dict] = []
    try:
        all_players = db.get_all_players()
        for r in all_players[:20]:
            elo_top.append({
                "username": r.get("username") or "",
                "label":    _player_label(r),
                "elo":      round(float(r.get("elo") or 0)),
                "wins":     int(r.get("wins") or 0),
                "draws":    int(r.get("draws") or 0),
                "losses":   int(r.get("losses") or 0),
            })
    except Exception:
        log.exception("get_all_players failed")

    visible_rows = rows_out if not limit else rows_out[:limit]
    return {
        "tournaments":  visible_rows,
        "all_rows":     rows_out,
        "total":        len(finished),
        "by_type":      by_type,
        "totals":       {
            "matches": total_matches,
            "goals":   total_goals,
            "players": len(unique_players),
        },
        "champions":    champs,
        "appearances":  appearances,
        "scorers":      scorers_combined,
        "elo":          elo_top,
        "biggest":      biggest,
        "highest_avg":  highest_avg,
    }


def format_all_tournaments_text(overview: dict) -> str:
    """Plain-text comparison digest, ready for ``.txt`` upload."""
    if not overview or overview.get("total", 0) == 0:
        return "Завершённых турниров пока нет."
    lines: list[str] = []
    lines.append("📊 СРАВНЕНИЕ ВСЕХ ТУРНИРОВ")
    lines.append("=" * 60)
    lines.append(f"Завершённых турниров: {overview['total']}")
    by_type = overview.get("by_type") or {}
    if by_type:
        type_str = " · ".join(
            f"{_format_type({'tournament_type': k})} {v}"
            for k, v in sorted(by_type.items())
        )
        lines.append(f"По типам: {type_str}")
    totals = overview.get("totals") or {}
    lines.append(
        f"Уникальных участников: {totals.get('players', 0)} · "
        f"Всего матчей: {totals.get('matches', 0)} · "
        f"Всего голов: {totals.get('goals', 0)}"
    )
    if totals.get("matches"):
        lines.append(
            f"Среднее голов за матч: "
            f"{totals['goals'] / totals['matches']:.2f}"
        )
    lines.append("")

    # Most titles.
    champs = overview.get("champions") or []
    if champs:
        lines.append("🏆 БОЛЬШЕ ВСЕГО ТИТУЛОВ")
        lines.append("-" * 60)
        for i, c in enumerate(champs[:15], 1):
            lines.append(f"  {i:>2}. {c['label']:<24} — {c['titles']} титул(а)")
        lines.append("")

    # Most appearances.
    apps = overview.get("appearances") or []
    if apps:
        lines.append("🎯 БОЛЬШЕ ВСЕГО УЧАСТИЙ")
        lines.append("-" * 60)
        for i, c in enumerate(apps[:15], 1):
            lines.append(
                f"  {i:>2}. {c['label']:<24} — {c['tournaments']} турнир(а)"
            )
        lines.append("")

    # Combined scorers.
    scorers = overview.get("scorers") or []
    if scorers:
        lines.append("⚽ БОМБАРДИРЫ ВСЕХ ВРЕМЁН")
        lines.append("-" * 60)
        for i, s in enumerate(scorers[:15], 1):
            lines.append(f"  {i:>2}. {s['label']:<24} — {s['goals']} {_pluralize_goals(s['goals'])}")
        lines.append("")

    # Global ELO.
    elo = overview.get("elo") or []
    if elo:
        lines.append("📈 ТОП ELO (официальный пул)")
        lines.append("-" * 60)
        for i, e in enumerate(elo[:15], 1):
            lines.append(
                f"  {i:>2}. {e['label']:<24} ELO {e['elo']:>5}  "
                f"({e['wins']}W {e['draws']}D {e['losses']}L)"
            )
        lines.append("")

    # Per-tournament list.
    rows = overview.get("tournaments") or []
    if rows:
        lines.append("📋 СПИСОК ТУРНИРОВ (новые сверху)")
        lines.append("-" * 60)
        lines.append(
            f"  {'#':>3} {'Название':<24} {'Тип':<5} "
            f"{'Чемпион':<18} {'И':>3} {'М':>3} {'Г':>4} {'Ср':>5}"
        )
        for r in rows:
            name = (r["name"] or "")[:24]
            champ = (r["champion"] or "—")[:18]
            ttype = (r["type_label"] or "")[:5]
            lines.append(
                f"  {r['id']:>3} {name:<24} {ttype:<5} "
                f"{champ:<18} {r['players']:>3} {r['matches']:>3} "
                f"{r['goals']:>4} {r['avg']:>5.2f}"
            )
        lines.append("")

    # Notable single-match record.
    big = overview.get("biggest")
    if big:
        lines.append("💥 КРУПНЕЙШИЙ МАТЧ ЗА ВСЕ ТУРНИРЫ")
        lines.append("-" * 60)
        lines.append(
            f"  {big['a']} {big['score']} {big['b']} ({big['tournament']}, "
            f"разница {big['diff']})"
        )
        lines.append("")

    avg = overview.get("highest_avg")
    if avg:
        lines.append("🔥 САМЫЙ ГОЛЕВОЙ ТУРНИР")
        lines.append("-" * 60)
        lines.append(
            f"  {avg['name']}: {avg['avg']:.2f} гола за матч "
            f"({avg['goals']} голов в {avg['matches']} матчах)"
        )
        lines.append("")

    lines.append("─" * 60)
    lines.append(f"Сгенерировано: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")
    return "\n".join(lines)




# ─────────────────────────────────────────────────────────────────────────────
# Tournament facts: rich, ranked observations like "@alice — 6 матчей подряд
# с забитыми голами" or "@bob отыграл весь турнир без поражений".
# Every fact carries a numeric ``score`` (higher = more interesting) so
# callers can pick top-N for hero cards and reshuffle on the "🎲 Ещё
# факты" callback.
# ─────────────────────────────────────────────────────────────────────────────


def _player_goals_in_match(match_id: int) -> dict[int, int]:
    """Count per-player goals from ``match_goals`` for ``match_id``.
    Returns ``{player_id: goal_count}``. Empty dict when OCR didn't
    register events for the match (most matches in practice)."""
    out: dict[int, int] = {}
    try:
        rows = db.get_match_goals(match_id)
    except Exception:
        return out
    for r in rows:
        pid = r.get("player_id")
        if pid:
            out[int(pid)] = out.get(int(pid), 0) + 1
    return out


def _footballer_goals_in_match(match: dict) -> dict[tuple[str, int | None], int]:
    """Count per-(footballer, scorer-side) goals from ``match_goals`` for
    a match row. Returns ``{(raw_name, scorer_player_id): goals}``.

    The "footballer" is the in-game player name OCR captured in
    ``match_goals.raw_name`` (e.g. ``"Pirlo"`` / ``"Месси"``). The
    scorer-side is resolved via ``match_goals.side`` → the match's
    ``player1_id`` (home) or ``player2_id`` (away), so the same
    footballer used by two different participants stays separated.
    Goals with empty/null ``raw_name`` are skipped — they carry no
    footballer identity to display.
    """
    out: dict[tuple[str, int | None], int] = {}
    try:
        rows = db.get_match_goals(int(match["id"]))
    except Exception:
        return out
    p1 = match.get("player1_id")
    p2 = match.get("player2_id")
    for r in rows:
        raw = (r.get("raw_name") or "").strip()
        if not raw:
            continue
        side = r.get("side")
        if side == "home":
            scorer_pid = p1
        elif side == "away":
            scorer_pid = p2
        else:
            # Fallback for legacy rows without side detection — fall
            # back to the resolved player_id when present.
            scorer_pid = r.get("player_id")
        key = (raw, int(scorer_pid) if scorer_pid is not None else None)
        out[key] = out.get(key, 0) + 1
    return out


def _per_player_match_log(tid: int) -> dict[int, list[dict]]:
    """Return ``{player_id: [match dict, ...]}`` ordered by play time
    (or by match id when ``played_at`` is NULL). Each match dict gets
    ``self_gf``, ``self_ga``, ``opponent_id``, ``stage`` for downstream
    streak / clean-sheet calculations."""
    confirmed = sorted(
        [m for m in db.get_tournament_matches(tid)
         if m.get("status") == "confirmed"
         and m.get("player1_id") != m.get("player2_id")],
        key=lambda m: (m.get("played_at") or "", m.get("id") or 0),
    )
    log_per: dict[int, list[dict]] = {}
    for m in confirmed:
        a, b = int(m["player1_id"]), int(m["player2_id"])
        s1 = int(m.get("score1") or 0)
        s2 = int(m.get("score2") or 0)
        log_per.setdefault(a, []).append({
            "match_id": m["id"], "stage": m.get("stage"),
            "self_gf": s1, "self_ga": s2, "opponent_id": b,
        })
        log_per.setdefault(b, []).append({
            "match_id": m["id"], "stage": m.get("stage"),
            "self_gf": s2, "self_ga": s1, "opponent_id": a,
        })
    return log_per


def _max_streak(log: list[dict], pred) -> int:
    """Longest streak of consecutive matches in ``log`` for which
    ``pred(match)`` is truthy."""
    best = cur = 0
    for m in log:
        if pred(m):
            cur += 1
            best = max(best, cur)
        else:
            cur = 0
    return best


def _label_for_pid(pid: int | None) -> str:
    if not pid:
        return "—"
    p = db.get_player_by_id(int(pid)) or {}
    return _player_label(p)


def compute_tournament_facts(tid: int, summary: dict) -> list[dict]:
    """Build the full pool of facts for tournament ``tid``. Each entry
    is a dict::

        {
          "kind":   str,      # short id (e.g. 'scoring_streak')
          "icon":   str,
          "title":  str,      # card heading
          "label":  str,      # main bold line — usually @username
          "value":  str,      # the headline number, e.g. '6 матчей подряд'
          "sub":    str,      # secondary line, e.g. 'с 17 голами в сумме'
          "score":  float,    # ranking — higher is more interesting
        }

    The list is sorted descending by ``score`` so callers can take
    ``facts[:N]`` for the visible cards.
    """
    if not summary:
        return []
    facts: list[dict] = []
    confirmed = [m for m in db.get_tournament_matches(tid)
                 if m.get("status") == "confirmed"
                 and m.get("player1_id") != m.get("player2_id")]
    if not confirmed:
        return facts
    logs = _per_player_match_log(tid)

    # ── 1. Голевая серия (consecutive matches with at least 1 goal) ──
    best_score_streak = 0
    best_score_pid = None
    best_score_total_goals = 0
    for pid, log in logs.items():
        n = _max_streak(log, lambda m: m["self_gf"] > 0)
        if n >= 2 and n > best_score_streak:
            best_score_streak = n
            best_score_pid = pid
            best_score_total_goals = sum(
                m["self_gf"] for m in log if m["self_gf"] > 0
            )
    if best_score_pid and best_score_streak >= 3:
        facts.append({
            "kind":  "scoring_streak",
            "icon":  "🔥",
            "title": "Голевая серия",
            "label": _label_for_pid(best_score_pid),
            "value": f"{best_score_streak} матчей подряд с голами",
            "sub":   f"всего {best_score_total_goals} забил за турнир",
            "score": 6.0 + best_score_streak,
        })

    # ── 2. Победная серия (consecutive wins) ──
    best_win_streak = 0
    best_win_pid = None
    for pid, log in logs.items():
        n = _max_streak(log, lambda m: m["self_gf"] > m["self_ga"])
        if n >= 2 and n > best_win_streak:
            best_win_streak = n
            best_win_pid = pid
    if best_win_pid and best_win_streak >= 3:
        facts.append({
            "kind":  "win_streak",
            "icon":  "👑",
            "title": "Победная серия",
            "label": _label_for_pid(best_win_pid),
            "value": f"{best_win_streak} побед(ы) подряд",
            "sub":   "ни одного срыва на этом отрезке",
            "score": 5.0 + best_win_streak,
        })

    # ── 3. Без поражений (no losses, ≥3 matches) ──
    unbeaten: list[tuple[int, int, int, int]] = []
    for pid, log in logs.items():
        wins = sum(1 for m in log if m["self_gf"] > m["self_ga"])
        draws = sum(1 for m in log if m["self_gf"] == m["self_ga"])
        losses = sum(1 for m in log if m["self_gf"] < m["self_ga"])
        if losses == 0 and len(log) >= 3:
            unbeaten.append((pid, wins, draws, len(log)))
    unbeaten.sort(key=lambda r: (-r[3], -r[1]))
    for pid, w, d, played in unbeaten[:1]:
        facts.append({
            "kind":  "unbeaten",
            "icon":  "🛡️",
            "title": "Без поражений",
            "label": _label_for_pid(pid),
            "value": f"{played} матчей без поражений",
            "sub":   f"{w} побед, {d} ничьих",
            "score": 8.0 + played * 0.5,
        })

    # ── 4. Без побед (zero wins, ≥3 matches) ──
    winless: list[tuple[int, int, int]] = []
    for pid, log in logs.items():
        wins = sum(1 for m in log if m["self_gf"] > m["self_ga"])
        losses = sum(1 for m in log if m["self_gf"] < m["self_ga"])
        if wins == 0 and len(log) >= 3:
            winless.append((pid, losses, len(log)))
    winless.sort(key=lambda r: (-r[1], -r[2]))
    for pid, losses, played in winless[:1]:
        facts.append({
            "kind":  "winless",
            "icon":  "💀",
            "title": "Без побед",
            "label": _label_for_pid(pid),
            "value": f"{losses} поражений из {played}",
            "sub":   "ни одной победы за турнир",
            "score": 2.0 + played * 0.2,
        })

    # ── 5. Король сухих матчей (clean-sheet rate, ≥3 matches, ≥1 CS) ──
    clean_best: tuple[int, int, int, float] | None = None
    for pid, log in logs.items():
        if len(log) < 3:
            continue
        cs = sum(1 for m in log if m["self_ga"] == 0)
        if cs == 0:
            continue
        rate = cs / len(log)
        if clean_best is None or (cs, rate) > (clean_best[1], clean_best[3]):
            clean_best = (pid, cs, len(log), rate)
    if clean_best:
        pid, cs, played, rate = clean_best
        if rate >= 0.5:
            facts.append({
                "kind":  "clean_sheet_king",
                "icon":  "🧱",
                "title": "Король сухих матчей",
                "label": _label_for_pid(pid),
                "value": f"{cs} из {played} на ноль",
                "sub":   f"{int(round(rate * 100))}% матчей без пропущенных",
                "score": 5.0 + cs * 0.8,
            })

    # ── 6. Группа смерти (group with the tightest standings) ──
    groups = summary.get("groups") or []
    if groups:
        tightest: tuple[str, int, list] | None = None
        for g in groups:
            standings = g.get("standings") or []
            if len(standings) < 3:
                continue
            pts = [r["pts"] for r in standings]
            spread = max(pts) - min(pts)
            if tightest is None or spread < tightest[1]:
                tightest = (g["letter"], spread, standings)
        if tightest:
            letter, spread, st = tightest
            if spread <= 3:
                names = ", ".join(
                    _fmt_user(r["username"]) for r in st[:3]
                )
                facts.append({
                    "kind":  "group_of_death",
                    "icon":  "🎲",
                    "title": "Группа смерти",
                    "label": f"Группа {letter}",
                    "value": f"разрыв всего {spread} оч.",
                    "sub":   f"топ-3: {names}",
                    "score": 4.0 + (4 - spread),
                })

    # ── 7. Самая забивная стадия (stage with the highest avg goals/match) ──
    by_stage: dict[str, list[int]] = {}
    for m in confirmed:
        s = m.get("stage") or "group"
        s_total = int(m.get("score1") or 0) + int(m.get("score2") or 0)
        by_stage.setdefault(s, []).append(s_total)
    if by_stage:
        best_stage = max(
            by_stage.items(),
            key=lambda kv: (sum(kv[1]) / len(kv[1])) if kv[1] else 0,
        )
        avg = sum(best_stage[1]) / max(1, len(best_stage[1]))
        if avg >= 3.5 and len(best_stage[1]) >= 2:
            stage_name = _stage_label(best_stage[0])
            facts.append({
                "kind":  "goal_avalanche",
                "icon":  "⛈",
                "title": "Самая забивная стадия",
                "label": stage_name,
                "value": f"{avg:.2f} гола/матч",
                "sub":   f"{sum(best_stage[1])} голов в "
                         f"{len(best_stage[1])} матчах — рекорд турнира",
                "score": 4.0 + avg,
            })

    # ── 8. Самая упорная пара плей-офф (closest playoff series) ──
    closest_pair: tuple[int, int, str, int, int, list[str]] | None = None
    for stage_block in summary.get("bracket") or []:
        for m in stage_block.get("matches") or []:
            if not m.get("confirmed") or m.get("bye"):
                continue
            score = m.get("score") or ""
            if score in ("—", "bye"):
                continue
            try:
                a_g, b_g = score.split(":")
                a_g, b_g = int(a_g), int(b_g)
            except (ValueError, AttributeError):
                continue
            diff = abs(a_g - b_g)
            total = a_g + b_g
            if total == 0:
                continue
            if (closest_pair is None
                    or (diff, -total) < (closest_pair[3], -closest_pair[4])):
                closest_pair = (
                    0, 0, stage_block.get("label") or "плей-офф",
                    diff, total, m.get("legs") or [],
                )
                # store readable a/b labels into kind dict
                closest_pair_meta = {
                    "a": m.get("a"), "b": m.get("b"), "score": score,
                }
    if closest_pair and closest_pair_meta:
        legs_str = (" · ".join(closest_pair_meta.get("legs") or [])
                    if closest_pair[5] else "")
        sub = (f"в одной игре" if not legs_str
               else f"по матчам: {legs_str}")
        facts.append({
            "kind":  "closest_pair",
            "icon":  "⚔️",
            "title": "Самая упорная пара",
            "label": f"{closest_pair_meta['a']} {closest_pair_meta['score']} "
                     f"{closest_pair_meta['b']}",
            "value": closest_pair[2],
            "sub":   sub if sub else "",
            "score": 3.0 + (5 - closest_pair[3]),
        })

    # ── 9. Хет-трики (3+ goals by one footballer in a single match) ──
    # The "hero" of a hat-trick is the in-game footballer (raw_name
    # from match_goals), not the human player who controlled them.
    # We still show which participant scored with that footballer in
    # the sub line so chat can connect the dots.
    hat_tricks: list[tuple[str, int | None, int, int]] = []
    # ↳ (raw_name, scorer_pid, match_id, goals)
    doubles_by_fb: dict[tuple[str, int | None], int] = {}
    for m in confirmed:
        per_fb = _footballer_goals_in_match(m)
        for (raw_name, scorer_pid), goals in per_fb.items():
            if goals >= 3:
                hat_tricks.append(
                    (raw_name, scorer_pid, int(m["id"]), int(goals))
                )
            elif goals == 2:
                key = (raw_name, scorer_pid)
                doubles_by_fb[key] = doubles_by_fb.get(key, 0) + 1
    if hat_tricks:
        # Pick the biggest hat-trick (most goals); tie → newest match.
        hat_tricks.sort(key=lambda r: (-r[3], -r[2]))
        raw_name, scorer_pid, mid, goals = hat_tricks[0]
        extra = (f" + ещё {len(hat_tricks) - 1} хет-трик(а) в турнире"
                 if len(hat_tricks) > 1 else "")
        scorer_label = _label_for_pid(scorer_pid) if scorer_pid else ""
        sub_bits = [f"матч #{mid}"]
        if scorer_label and scorer_label != "—":
            sub_bits.append(f"забил {scorer_label}")
        sub = " · ".join(sub_bits) + extra
        facts.append({
            "kind":  "hat_trick",
            "icon":  "🎩",
            "title": "Хет-трик",
            "label": raw_name,
            "value": f"{goals} голов в одном матче",
            "sub":   sub,
            "score": 8.0 + goals,
        })

    # ── 10. Дубли — footballers with multiple braces ──
    if doubles_by_fb:
        top_doubles = max(doubles_by_fb.items(), key=lambda kv: kv[1])
        (raw_name, scorer_pid), n = top_doubles
        if n >= 2:
            scorer_label = _label_for_pid(scorer_pid) if scorer_pid else ""
            if scorer_label and scorer_label != "—":
                sub = f"забил {scorer_label} · по 2 гола в матче"
            else:
                sub = "по 2 гола в матче"
            facts.append({
                "kind":  "doubles",
                "icon":  "⚡",
                "title": "Король дублей",
                "label": raw_name,
                "value": f"{n} дубля за турнир",
                "sub":   sub,
                "score": 4.0 + n,
            })

    # ── 11. Голевой матч турнира (highest combined score) ──
    spectacle: tuple[int, int, int, int] | None = None
    for m in confirmed:
        s1 = int(m.get("score1") or 0)
        s2 = int(m.get("score2") or 0)
        total = s1 + s2
        if spectacle is None or total > spectacle[2]:
            spectacle = (int(m["player1_id"]), int(m["player2_id"]), total, int(m["id"]))
    if spectacle and spectacle[2] >= 6:
        a, b, total, mid = spectacle
        # Look up the actual scoreline.
        score_str = "?:?"
        for m in confirmed:
            if int(m["id"]) == mid:
                score_str = f"{m.get('score1')}:{m.get('score2')}"
                break
        facts.append({
            "kind":  "goalfest",
            "icon":  "🎆",
            "title": "Голевой матч",
            "label": f"{_label_for_pid(a)} {score_str} {_label_for_pid(b)}",
            "value": f"{total} голов в одном матче",
            "sub":   "ни в чём себе не отказывали",
            "score": 3.0 + total * 0.4,
        })

    # ── 12. Король конверсии (best GF / (GF + GA), ≥3 matches) ──
    conv_best: tuple[int, float, int, int, int] | None = None
    for pid, log in logs.items():
        if len(log) < 3:
            continue
        gf = sum(m["self_gf"] for m in log)
        ga = sum(m["self_ga"] for m in log)
        if gf + ga < 3:
            continue
        conv = gf / (gf + ga)
        if conv_best is None or (conv, gf) > (conv_best[1], conv_best[2]):
            conv_best = (pid, conv, gf, ga, len(log))
    if conv_best and conv_best[1] >= 0.7:
        pid, conv, gf, ga, played = conv_best
        facts.append({
            "kind":  "conversion_king",
            "icon":  "🎯",
            "title": "Конверсия",
            "label": _label_for_pid(pid),
            "value": f"{int(round(conv * 100))}% забитых",
            "sub":   f"{gf}:{ga} в {played} матчах",
            "score": 2.5 + conv * 5,
        })

    # ── 13. Ничейный король (≥2 draws) ──
    drawmaster: tuple[int, int, int] | None = None
    for pid, log in logs.items():
        draws = sum(1 for m in log if m["self_gf"] == m["self_ga"])
        if draws >= 2 and (drawmaster is None or draws > drawmaster[1]):
            drawmaster = (pid, draws, len(log))
    if drawmaster:
        pid, d, played = drawmaster
        facts.append({
            "kind":  "draw_specialist",
            "icon":  "🤝",
            "title": "Ничейный король",
            "label": _label_for_pid(pid),
            "value": f"{d} ничьих из {played}",
            "sub":   "не отдаёт, но и не дожимает",
            "score": 1.5 + d,
        })

    # ── 14. Голевой вклад (best % of own team's goals from match_goals) ──
    # Hero of the card is the IN-GAME FOOTBALLER (raw_name from
    # match_goals) the participant leaned on for goals — same
    # convention as «Хет-трик»/«Король дублей». The participant
    # @handle moves to the sub line so chat still knows who controlled
    # that footballer. Falls back to @username when no raw_name was
    # ever captured for this player's goals (legacy / no-OCR matches).
    contrib_best: tuple[int, float, int, int, dict[str, int]] | None = None
    # Index confirmed matches by id once so the inner loop can resolve
    # the home/away player ids when looking up footballer goals.
    confirmed_by_id = {
        int(m["id"]): m
        for m in db.get_tournament_matches(tid)
        if m.get("status") == "confirmed"
        and m.get("player1_id") != m.get("player2_id")
    }
    for pid, log in logs.items():
        own = 0
        team = 0
        fb_goals_for_pid: dict[str, int] = {}
        for m in log:
            per_player = _player_goals_in_match(m["match_id"])
            mine = per_player.get(pid, 0)
            own += mine
            team += m["self_gf"]
            # Per-footballer tally for this player's own goals in
            # this match. The log entry is a compact dict (only
            # match_id / self_gf / opponent_id) — we need the full
            # match row to know home/away player ids before
            # ``_footballer_goals_in_match`` can resolve a scorer.
            full_match = confirmed_by_id.get(int(m["match_id"]))
            if full_match is None:
                continue
            for (raw_name, scorer_pid), goals in \
                    _footballer_goals_in_match(full_match).items():
                if scorer_pid == pid and raw_name:
                    fb_goals_for_pid[raw_name] = (
                        fb_goals_for_pid.get(raw_name, 0) + goals
                    )
        if team >= 4 and own >= 3:
            ratio = own / team if team else 0.0
            if contrib_best is None or ratio > contrib_best[1]:
                contrib_best = (pid, ratio, own, team, fb_goals_for_pid)
    if contrib_best and contrib_best[1] >= 0.6:
        pid, ratio, own, team, fb_goals_for_pid = contrib_best
        scorer_label = _label_for_pid(pid)
        # Pick the footballer the player relied on most. Tie-break by
        # alphabetic name so output is stable across reruns.
        if fb_goals_for_pid:
            fb_name, fb_goals = max(
                fb_goals_for_pid.items(),
                key=lambda kv: (kv[1], -ord(kv[0][:1]) if kv[0] else 0),
            )
            label = fb_name
            sub_bits = [f"{own} из {team} голов сам забил"]
            if scorer_label and scorer_label != "—":
                sub_bits.append(f"забил {scorer_label}")
            sub = " · ".join(sub_bits)
        else:
            # No raw_name on file — keep the older behaviour rather
            # than dropping the fact entirely; the percentage is still
            # interesting on its own.
            label = scorer_label
            sub = f"{own} из {team} голов сам забил"
        facts.append({
            "kind":  "contribution_king",
            "icon":  "💪",
            "title": "Голевой вклад",
            "label": label,
            "value": f"{int(round(ratio * 100))}% голов команды",
            "sub":   sub,
            "score": 2.5 + ratio * 4,
        })

    # ── 15. Доминатор группы (best group GD) ──
    if groups:
        dom_best: tuple[str, str, int, int] | None = None  # (group, name, gd, pts)
        for g in groups:
            for r in g.get("standings") or []:
                gd = r["gd"]
                if dom_best is None or (gd, r["pts"]) > (dom_best[2], dom_best[3]):
                    dom_best = (g["letter"], r["username"], gd, r["pts"])
        if dom_best and dom_best[2] >= 5:
            letter, username, gd, pts = dom_best
            facts.append({
                "kind":  "group_dominator",
                "icon":  "🦁",
                "title": "Доминатор группы",
                "label": _fmt_user(username),
                "value": f"РГ {gd:+d} в группе {letter}",
                "sub":   f"{pts} очков на групповом этапе",
                "score": 1.5 + gd * 0.3,
            })

    # ── 16. Низкорезультативный отрезок ("Тише едешь") ──
    quiet_matches = [m for m in confirmed
                     if int(m.get("score1") or 0) + int(m.get("score2") or 0) <= 1]
    if quiet_matches and len(confirmed) >= 4 and len(quiet_matches) / len(confirmed) >= 0.3:
        facts.append({
            "kind":  "low_scoring",
            "icon":  "🐢",
            "title": "Тише едешь",
            "label": f"{len(quiet_matches)} низкорезультативных матча",
            "value": f"≤1 гол на матч",
            "sub":   f"из {len(confirmed)} ({int(100 * len(quiet_matches) / len(confirmed))}%)",
            "score": 1.0 + len(quiet_matches) * 0.3,
        })

    # ── 17. Зрелищный игрок (highest combined goals/match in his games) ──
    spect_best: tuple[int, float, int] | None = None  # pid, avg, played
    for pid, log in logs.items():
        if len(log) < 3:
            continue
        avg_goals = sum(m["self_gf"] + m["self_ga"] for m in log) / len(log)
        if spect_best is None or avg_goals > spect_best[1]:
            spect_best = (pid, avg_goals, len(log))
    if spect_best and spect_best[1] >= 4.0:
        pid, avg, played = spect_best
        facts.append({
            "kind":  "spectator_player",
            "icon":  "🎭",
            "title": "Зрелищный игрок",
            "label": _label_for_pid(pid),
            "value": f"{avg:.1f} гола/матч",
            "sub":   f"никогда не бывает скучно ({played} матчей)",
            "score": 2.0 + avg * 0.5,
        })

    # ── 18. Сухарь (longest no-goal streak) ──
    silent_best: tuple[int, int, int] | None = None  # pid, streak, played
    for pid, log in logs.items():
        if len(log) < 3:
            continue
        n = _max_streak(log, lambda m: m["self_gf"] == 0)
        if n >= 3 and (silent_best is None or n > silent_best[1]):
            silent_best = (pid, n, len(log))
    if silent_best:
        pid, n, played = silent_best
        facts.append({
            "kind":  "no_goal_streak",
            "icon":  "🥶",
            "title": "Заморозка",
            "label": _label_for_pid(pid),
            "value": f"{n} матча без забитых",
            "sub":   "атака отказала",
            "score": 1.5 + n * 0.6,
        })

    # ── 19. Снайпер (highest goals-per-match average, ≥3 matches) ──
    sniper_best: tuple[int, float, int, int] | None = None
    for pid, log in logs.items():
        if len(log) < 3:
            continue
        gf = sum(m["self_gf"] for m in log)
        avg = gf / len(log)
        if avg >= 2.0 and (sniper_best is None or avg > sniper_best[1]):
            sniper_best = (pid, avg, gf, len(log))
    if sniper_best:
        pid, avg, gf, played = sniper_best
        facts.append({
            "kind":  "sniper",
            "icon":  "🎯",
            "title": "Снайпер",
            "label": _label_for_pid(pid),
            "value": f"{avg:.1f} гола/матч",
            "sub":   f"{gf} забил в {played} матчах",
            "score": 2.0 + avg * 0.7,
        })

    # ── 20. Самый невезучий (most losses, played enough) ──
    unlucky_best: tuple[int, int, int] | None = None  # pid, losses, played
    for pid, log in logs.items():
        if len(log) < 3:
            continue
        losses = sum(1 for m in log if m["self_gf"] < m["self_ga"])
        if losses >= 3 and (unlucky_best is None or losses > unlucky_best[1]):
            unlucky_best = (pid, losses, len(log))
    if unlucky_best:
        pid, losses, played = unlucky_best
        facts.append({
            "kind":  "underdog",
            "icon":  "💸",
            "title": "Не его турнир",
            "label": _label_for_pid(pid),
            "value": f"{losses} поражений",
            "sub":   f"в {played} матчах",
            "score": 1.5 + losses * 0.4,
        })

    # ── 21. Тёмная лошадка (low group seed → reached playoff late) ──
    elims = summary.get("eliminations") or []
    groups_data = summary.get("groups") or []
    seed_by_pid: dict[int, tuple[str, int]] = {}
    for g in groups_data:
        for r in g.get("standings") or []:
            for p in summary.get("player_stats") or []:
                if (p.get("username") or "").lower() == (r.get("username") or "").lower():
                    seed_by_pid[p["player_id"]] = (g["letter"], r["pos"])
                    break
    stage_rank = {
        "group": 0, "r256": 1, "r128": 2, "r64": 3, "r32": 4, "r16": 5,
        "qf": 6, "sf": 7, "third": 8, "final": 9, "champion": 10,
    }
    darkhorse_best: tuple[int, int, str, str] | None = None  # pid, climb, seed, stage
    for e in elims:
        pid = e.get("player_id")
        stage = e.get("stage_code") or "group"
        if not pid or pid not in seed_by_pid:
            continue
        group, pos = seed_by_pid[pid]
        # Only count as dark-horse if seeded 3rd or worse in the group
        # but reached at least the QF.
        if pos < 3:
            continue
        if stage_rank.get(stage, 0) < stage_rank["qf"]:
            continue
        climb = stage_rank.get(stage, 0)
        if (darkhorse_best is None
                or (pos, -climb) > (darkhorse_best[1], -stage_rank.get(darkhorse_best[3], 0))):
            darkhorse_best = (pid, pos, group, stage)
    if darkhorse_best:
        pid, pos, group, stage = darkhorse_best
        facts.append({
            "kind":  "darkhorse",
            "icon":  "🐎",
            "title": "Тёмная лошадка",
            "label": _label_for_pid(pid),
            "value": f"{pos}-е в группе {group} → {_stage_label(stage)}",
            "sub":   "никто не ждал такого результата",
            "score": 4.0 + pos + (stage_rank.get(stage, 0) - 5) * 0.5,
        })

    # ── 22. Железный человек (most matches played) ──
    ironman_best: tuple[int, int] | None = None
    avg_played = (sum(len(l) for l in logs.values()) / max(1, len(logs)))
    for pid, log in logs.items():
        if len(log) < 4:
            continue
        if ironman_best is None or len(log) > ironman_best[1]:
            ironman_best = (pid, len(log))
    if ironman_best and ironman_best[1] >= avg_played + 2:
        pid, played = ironman_best
        facts.append({
            "kind":  "ironman",
            "icon":  "💪",
            "title": "Железный человек",
            "label": _label_for_pid(pid),
            "value": f"{played} матчей за турнир",
            "sub":   f"в среднем {avg_played:.1f} на игрока",
            "score": 2.0 + (played - avg_played) * 0.6,
        })

    # ── 23. Молниеносный старт (won first N matches in a row) ──
    fast_best: tuple[int, int] | None = None
    for pid, log in logs.items():
        n = 0
        for m in log:
            if m["self_gf"] > m["self_ga"]:
                n += 1
            else:
                break
        if n >= 3 and (fast_best is None or n > fast_best[1]):
            fast_best = (pid, n)
    if fast_best:
        pid, n = fast_best
        facts.append({
            "kind":  "fast_start",
            "icon":  "🏁",
            "title": "Молниеносный старт",
            "label": _label_for_pid(pid),
            "value": f"{n} побед с первого матча",
            "sub":   "не дал шанса на раскачку",
            "score": 3.0 + n * 0.5,
        })

    # ── 24. Перепад настроений (widest spread of self_gf across matches) ──
    swing_best: tuple[int, int, int] | None = None
    for pid, log in logs.items():
        if len(log) < 4:
            continue
        gfs = [m["self_gf"] for m in log]
        spread = max(gfs) - min(gfs)
        if spread >= 4 and (swing_best is None or spread > swing_best[1]):
            swing_best = (pid, spread, min(gfs))
    if swing_best:
        pid, spread, _ = swing_best
        facts.append({
            "kind":  "mood_swings",
            "icon":  "🌪",
            "title": "Перепад настроений",
            "label": _label_for_pid(pid),
            "value": f"размах {spread} голов за матч",
            "sub":   "то ураган, то штиль",
            "score": 1.5 + spread * 0.4,
        })

    # ── 25. Везунчик (most 1-goal-margin wins) ──
    lucky_best: tuple[int, int, int] | None = None
    for pid, log in logs.items():
        narrow = sum(1 for m in log
                     if m["self_gf"] - m["self_ga"] == 1)
        if narrow >= 2 and (lucky_best is None or narrow > lucky_best[1]):
            lucky_best = (pid, narrow, len(log))
    if lucky_best:
        pid, narrow, played = lucky_best
        facts.append({
            "kind":  "lucky_one",
            "icon":  "🍀",
            "title": "Везунчик",
            "label": _label_for_pid(pid),
            "value": f"{narrow} побед в 1 гол",
            "sub":   "выкручивал концовки",
            "score": 1.8 + narrow * 0.7,
        })

    # ── 26. Открытие турнира (first played match) ──
    sorted_confirmed = sorted(
        confirmed,
        key=lambda m: (m.get("played_at") or "", m.get("id") or 0),
    )
    if sorted_confirmed and len(confirmed) >= 4:
        first_match = sorted_confirmed[0]
        s1 = int(first_match.get("score1") or 0)
        s2 = int(first_match.get("score2") or 0)
        a_lbl = _label_for_pid(first_match["player1_id"])
        b_lbl = _label_for_pid(first_match["player2_id"])
        facts.append({
            "kind":  "opener",
            "icon":  "🎬",
            "title": "Открытие турнира",
            "label": f"{a_lbl} {s1}:{s2} {b_lbl}",
            "value": _stage_label(first_match.get("stage") or "group"),
            "sub":   "с этого начался турнир",
            "score": 1.5,
        })

    # ── 27. Главная вражда (most-played pair) ──
    pair_counts: dict[tuple[int, int], list[dict]] = {}
    for m in confirmed:
        a, b = sorted([int(m["player1_id"]), int(m["player2_id"])])
        pair_counts.setdefault((a, b), []).append(m)
    most_played_pair = None
    if pair_counts:
        most_played_pair = max(pair_counts.items(), key=lambda kv: len(kv[1]))
    if most_played_pair and len(most_played_pair[1]) >= 3:
        (a, b), pair_matches = most_played_pair
        wins_a = sum(
            1 for m in pair_matches
            if (m["player1_id"] == a and int(m.get("score1") or 0) > int(m.get("score2") or 0))
            or (m["player2_id"] == a and int(m.get("score2") or 0) > int(m.get("score1") or 0))
        )
        wins_b = sum(
            1 for m in pair_matches
            if (m["player1_id"] == b and int(m.get("score1") or 0) > int(m.get("score2") or 0))
            or (m["player2_id"] == b and int(m.get("score2") or 0) > int(m.get("score1") or 0))
        )
        draws = len(pair_matches) - wins_a - wins_b
        facts.append({
            "kind":  "rivalry",
            "icon":  "🤼",
            "title": "Главная вражда",
            "label": f"{_label_for_pid(a)} vs {_label_for_pid(b)}",
            "value": f"{len(pair_matches)} матчей",
            "sub":   f"{wins_a}-{draws}-{wins_b}",
            "score": 2.5 + len(pair_matches) * 0.5,
        })

    # ── 28. Ничейная группа (group with most draws %) ──
    if groups_data:
        draw_share_best: tuple[str, int, int] | None = None
        for g in groups_data:
            standings = g.get("standings") or []
            if len(standings) < 3:
                continue
            total_draws = sum(r["draws"] for r in standings)
            total_played = sum(r["played"] for r in standings)
            actual_draws = total_draws // 2
            actual_matches = total_played // 2
            if actual_matches >= 4 and actual_draws >= 2:
                share = actual_draws / actual_matches
                if (draw_share_best is None
                        or share > draw_share_best[1] / max(1, draw_share_best[2])):
                    draw_share_best = (g["letter"], actual_draws, actual_matches)
        if draw_share_best:
            letter, draws, matches = draw_share_best
            pct = int(round(100 * draws / max(1, matches)))
            if pct >= 30:
                facts.append({
                    "kind":  "draw_group",
                    "icon":  "🤝",
                    "title": "Ничейная группа",
                    "label": f"Группа {letter}",
                    "value": f"{draws} ничьих из {matches}",
                    "sub":   f"{pct}% всех матчей",
                    "score": 2.0 + pct * 0.05,
                })

    # ── 29. Реванш (pair that lost in groups but won in playoff) ──
    group_results: dict[tuple[int, int], tuple[int, int, int]] = {}
    for m in confirmed:
        if (m.get("stage") or "") != "group":
            continue
        a, b = int(m["player1_id"]), int(m["player2_id"])
        s1 = int(m.get("score1") or 0)
        s2 = int(m.get("score2") or 0)
        if s1 == s2:
            continue
        winner = a if s1 > s2 else b
        win_score, loss_score = (s1, s2) if s1 > s2 else (s2, s1)
        group_results[tuple(sorted([a, b]))] = (winner, win_score, loss_score)
    revenge_pair = None
    for m in confirmed:
        stage = m.get("stage") or ""
        if not stage or stage == "group":
            continue
        a, b = int(m["player1_id"]), int(m["player2_id"])
        key = tuple(sorted([a, b]))
        if key not in group_results:
            continue
        s1 = int(m.get("score1") or 0)
        s2 = int(m.get("score2") or 0)
        if s1 == s2:
            continue
        po_winner = a if s1 > s2 else b
        group_winner, gw_score, gl_score = group_results[key]
        if po_winner != group_winner:
            revenge_pair = (
                po_winner, group_winner,
                f"{gw_score}:{gl_score}",
                f"{max(s1,s2)}:{min(s1,s2)}",
            )
            break
    if revenge_pair:
        rev_winner, rev_loser, gs, ps = revenge_pair
        facts.append({
            "kind":  "revenge",
            "icon":  "⚔️",
            "title": "Реванш",
            "label": f"{_label_for_pid(rev_winner)} → {_label_for_pid(rev_loser)}",
            "value": f"группы {gs} → плей-офф {ps}",
            "sub":   "вернул должок где это решало",
            "score": 5.5,
        })

    # ── 30. Самый зрелищный игрок группы (best group GD diff vs avg) ──
    if groups_data:
        # Already covered by group_dominator; instead look for the
        # CLOSEST runner-up — player who finished 2nd in his group with
        # the smallest gap to the leader. Highlights tight group races.
        runnerup_best: tuple[str, str, int, int] | None = None
        for g in groups_data:
            st = g.get("standings") or []
            if len(st) < 2:
                continue
            top, second = st[0], st[1]
            gap = top["pts"] - second["pts"]
            if gap > 3:
                continue
            if runnerup_best is None or gap < runnerup_best[2]:
                runnerup_best = (g["letter"], second["username"], gap, second["pts"])
        if runnerup_best:
            letter, username, gap, pts = runnerup_best
            facts.append({
                "kind":  "close_runnerup",
                "icon":  "🥈",
                "title": "Тесная борьба",
                "label": _fmt_user(username),
                "value": f"2-е в {letter}, {gap} очков от 1-го",
                "sub":   f"набрал {pts} оч.",
                "score": 2.5 + (4 - gap),
            })

    # ── 31. Сухарь финалу (final won 1:0 / 2:1 — close & dramatic) ──
    if summary.get("podium", {}).get("first"):
        for stage_block in summary.get("bracket") or []:
            if stage_block.get("stage") != "final":
                continue
            for m in stage_block.get("matches") or []:
                if not m.get("confirmed") or m.get("bye"):
                    continue
                score = m.get("score") or ""
                try:
                    a_g, b_g = score.split(":")
                    a_g, b_g = int(a_g), int(b_g)
                except (ValueError, AttributeError):
                    continue
                diff = abs(a_g - b_g)
                if 0 < diff <= 1 and (a_g + b_g) <= 4:
                    facts.append({
                        "kind":  "tight_final",
                        "icon":  "⚖️",
                        "title": "Тесный финал",
                        "label": f"{m['a']} {score} {m['b']}",
                        "value": f"разница {diff}",
                        "sub":   "решалось до последней секунды",
                        "score": 3.5 + (4 - a_g - b_g) * 0.3,
                    })
                    break

    # ── 32. Чистая победа (most wins on a clean sheet, ≥3) ──
    clean_wins_best: tuple[int, int, int] | None = None
    for pid, log in logs.items():
        cw = sum(1 for m in log if m["self_gf"] > 0 and m["self_ga"] == 0)
        if cw >= 3 and (clean_wins_best is None or cw > clean_wins_best[1]):
            clean_wins_best = (pid, cw, len(log))
    if clean_wins_best:
        pid, cw, played = clean_wins_best
        facts.append({
            "kind":  "clean_win_king",
            "icon":  "🎖️",
            "title": "Чистая победа",
            "label": _label_for_pid(pid),
            "value": f"{cw} побед без пропущенных",
            "sub":   f"в {played} матчах",
            "score": 4.0 + cw * 0.7,
        })

    # ── 33. Непобедимый в группе (no losses in group, ≥3 group matches) ──
    group_unbeaten_best: tuple[int, int, int] | None = None
    for pid, log in logs.items():
        group_log = [m for m in log if (m.get("stage") or "group") == "group"]
        if len(group_log) < 3:
            continue
        wins = sum(1 for m in group_log if m["self_gf"] > m["self_ga"])
        losses = sum(1 for m in group_log if m["self_gf"] < m["self_ga"])
        if (losses == 0 and wins >= 2
                and (group_unbeaten_best is None
                     or len(group_log) > group_unbeaten_best[2])):
            group_unbeaten_best = (pid, wins, len(group_log))
    if group_unbeaten_best:
        pid, wins, played = group_unbeaten_best
        facts.append({
            "kind":  "group_unbeaten",
            "icon":  "🏰",
            "title": "Непобедим в группе",
            "label": _label_for_pid(pid),
            "value": f"{played} матчей без поражений",
            "sub":   f"{wins} побед на групповом этапе",
            "score": 3.5 + played * 0.4,
        })

    # ── 34. Худшая оборона группы (most goals against in group, ≥3 m.) ──
    worst_def_group: tuple[int, int, int] | None = None
    for pid, log in logs.items():
        group_log = [m for m in log if (m.get("stage") or "group") == "group"]
        if len(group_log) < 3:
            continue
        ga = sum(m["self_ga"] for m in group_log)
        if ga >= 8 and (worst_def_group is None or ga > worst_def_group[1]):
            worst_def_group = (pid, ga, len(group_log))
    if worst_def_group:
        pid, ga, played = worst_def_group
        facts.append({
            "kind":  "worst_group_defense",
            "icon":  "🪤",
            "title": "Дырявая оборона",
            "label": _label_for_pid(pid),
            "value": f"пропустил {ga} голов",
            "sub":   f"за {played} матчей в группе",
            "score": 1.8 + ga * 0.15,
        })

    # ── 35. Шторм плей-офф (most goals in playoff matches) ──
    playoff_attack: tuple[int, int, int] | None = None
    for pid, log in logs.items():
        playoff_log = [m for m in log
                       if (m.get("stage") or "group") not in ("group", "")]
        if len(playoff_log) < 2:
            continue
        gf = sum(m["self_gf"] for m in playoff_log)
        if gf >= 4 and (playoff_attack is None or gf > playoff_attack[1]):
            playoff_attack = (pid, gf, len(playoff_log))
    if playoff_attack:
        pid, gf, played = playoff_attack
        facts.append({
            "kind":  "playoff_attacker",
            "icon":  "⚡",
            "title": "Шторм плей-офф",
            "label": _label_for_pid(pid),
            "value": f"{gf} голов в плей-офф",
            "sub":   f"в {played} матчах на вылет",
            "score": 4.5 + gf * 0.3,
        })

    # ── 36. Бастион плей-офф (lowest GA per match in playoff, ≥2 PO matches) ──
    playoff_def: tuple[int, int, int] | None = None
    for pid, log in logs.items():
        playoff_log = [m for m in log
                       if (m.get("stage") or "group") not in ("group", "")]
        if len(playoff_log) < 2:
            continue
        ga = sum(m["self_ga"] for m in playoff_log)
        if (playoff_def is None
                or (ga, -len(playoff_log)) < (playoff_def[1], -playoff_def[2])):
            playoff_def = (pid, ga, len(playoff_log))
    if playoff_def and playoff_def[1] <= playoff_def[2]:  # ≤1 GA per match
        pid, ga, played = playoff_def
        facts.append({
            "kind":  "playoff_defender",
            "icon":  "🛡️",
            "title": "Бастион плей-офф",
            "label": _label_for_pid(pid),
            "value": f"пропустил {ga} в {played} матчах",
            "sub":   "почти не дал шансов соперникам",
            "score": 4.0 + (played - ga) * 0.4,
        })

    # ── 37. Разгром в плей-офф (biggest goal diff in any PO match, ≥3) ──
    biggest_po: tuple[int, int, int, str] | None = None
    for m in confirmed:
        stage = m.get("stage") or "group"
        if stage == "group":
            continue
        s1 = int(m.get("score1") or 0)
        s2 = int(m.get("score2") or 0)
        diff = abs(s1 - s2)
        if diff < 3:
            continue
        if biggest_po is None or (diff, -s1 - s2) > (biggest_po[0], -biggest_po[1]):
            biggest_po = (diff, s1 + s2, int(m["id"]), stage)
    if biggest_po:
        diff, total, mid, stage = biggest_po
        target_match = next((m for m in confirmed if int(m["id"]) == mid), None)
        if target_match:
            a_lbl = _label_for_pid(target_match["player1_id"])
            b_lbl = _label_for_pid(target_match["player2_id"])
            score_str = f"{target_match.get('score1')}:{target_match.get('score2')}"
            facts.append({
                "kind":  "playoff_blowout",
                "icon":  "🔪",
                "title": "Разгром в плей-офф",
                "label": f"{a_lbl} {score_str} {b_lbl}",
                "value": f"разница {diff} в {_stage_label(stage)}",
                "sub":   "никакой интриги",
                "score": 3.0 + diff * 0.5,
            })

    # ── 38. Универсал (scored in ≥3 different stages) ──
    universal: tuple[int, int, list[str]] | None = None
    for pid, log in logs.items():
        stages_scored: set[str] = set()
        for m in log:
            if m["self_gf"] > 0:
                stages_scored.add(m.get("stage") or "group")
        if (len(stages_scored) >= 3
                and (universal is None or len(stages_scored) > universal[1])):
            universal = (pid, len(stages_scored), sorted(stages_scored))
    if universal:
        pid, n, stages_list = universal
        stages_human = " · ".join(_stage_label(s) for s in stages_list)
        facts.append({
            "kind":  "multistage_scorer",
            "icon":  "🌐",
            "title": "Универсал",
            "label": _label_for_pid(pid),
            "value": f"забил на {n} разных стадиях",
            "sub":   stages_human,
            "score": 3.0 + n * 0.6,
        })

    # ── 39. Стабильность (lowest GF variance, ≥4 matches, avg ≥1.0) ──
    stable_best: tuple[int, float, int] | None = None
    for pid, log in logs.items():
        if len(log) < 4:
            continue
        gfs = [m["self_gf"] for m in log]
        avg = sum(gfs) / len(gfs)
        var = sum((g - avg) ** 2 for g in gfs) / len(gfs)
        # Boring zero-everywhere players are technically stable; require
        # a real attacking output (avg ≥1.0) before crowning anyone.
        if avg >= 1.0 and var <= 0.6:
            if stable_best is None or var < stable_best[1]:
                stable_best = (pid, var, len(log))
    if stable_best:
        pid, var, played = stable_best
        facts.append({
            "kind":  "stability_king",
            "icon":  "🎯",
            "title": "Стабильность",
            "label": _label_for_pid(pid),
            "value": f"{played} похожих матчей",
            "sub":   "разброс счёта почти нулевой",
            "score": 2.5 + (1.0 - var) * 2,
        })

    # ── 40. Закрытие турнира (the very last played match) ──
    if sorted_confirmed and len(confirmed) >= 4:
        last_match = sorted_confirmed[-1]
        if last_match.get("id") != sorted_confirmed[0].get("id"):
            s1 = int(last_match.get("score1") or 0)
            s2 = int(last_match.get("score2") or 0)
            a_lbl = _label_for_pid(last_match["player1_id"])
            b_lbl = _label_for_pid(last_match["player2_id"])
            facts.append({
                "kind":  "closer",
                "icon":  "🍿",
                "title": "Закрытие турнира",
                "label": f"{a_lbl} {s1}:{s2} {b_lbl}",
                "value": _stage_label(last_match.get("stage") or "final"),
                "sub":   "последний свисток",
                "score": 1.7,
            })

    # ── 41. Голевой апгрейд / Тише к финалу (group avg vs playoff avg) ──
    group_matches = [m for m in confirmed
                     if (m.get("stage") or "group") == "group"]
    po_matches = [m for m in confirmed
                  if (m.get("stage") or "group") not in ("group", "")]
    if len(group_matches) >= 4 and len(po_matches) >= 3:
        g_avg = sum(int(m.get("score1") or 0) + int(m.get("score2") or 0)
                    for m in group_matches) / len(group_matches)
        p_avg = sum(int(m.get("score1") or 0) + int(m.get("score2") or 0)
                    for m in po_matches) / len(po_matches)
        if p_avg - g_avg >= 1.0:
            facts.append({
                "kind":  "ascending_intensity",
                "icon":  "📈",
                "title": "Голевой апгрейд",
                "label": "плей-офф пошёл результативнее",
                "value": f"группы {g_avg:.1f} → плей-офф {p_avg:.1f} гола",
                "sub":   "к финалу разыгрались",
                "score": 2.5 + (p_avg - g_avg),
            })
        elif g_avg - p_avg >= 1.5:
            facts.append({
                "kind":  "descending_intensity",
                "icon":  "📉",
                "title": "Тише к финалу",
                "label": "плей-офф осторожнее группы",
                "value": f"группы {g_avg:.1f} → плей-офф {p_avg:.1f} гола",
                "sub":   "цена ошибки выросла",
                "score": 2.0 + (g_avg - p_avg) * 0.6,
            })

    # ── 42. Битва нулей (most-played pair with the lowest goals/match) ──
    if pair_counts:
        defensive_pair: tuple[int, int, int, float, int] | None = None
        for (a, b), pms in pair_counts.items():
            if len(pms) < 2:
                continue
            total_goals = sum(int(m.get("score1") or 0) + int(m.get("score2") or 0)
                              for m in pms)
            avg = total_goals / len(pms)
            if avg <= 1.5:
                if (defensive_pair is None
                        or (len(pms), -avg) > (defensive_pair[2], -defensive_pair[3])):
                    defensive_pair = (a, b, len(pms), avg, total_goals)
        if defensive_pair:
            a, b, n, avg, total = defensive_pair
            facts.append({
                "kind":  "defensive_rivalry",
                "icon":  "🧊",
                "title": "Битва нулей",
                "label": f"{_label_for_pid(a)} vs {_label_for_pid(b)}",
                "value": f"{total} голов в {n} матчах",
                "sub":   f"{avg:.1f} гола за матч — оба заперлись",
                "score": 1.8 + n * 0.4,
            })

    # ── 43. Хронометраж турнира (calendar span if played_at populated) ──
    played_dates = [m.get("played_at") for m in confirmed
                    if m.get("played_at")]
    if len(played_dates) >= 4:
        try:
            from datetime import datetime as _dt
            def _parse(d: str) -> _dt:
                if "T" in d:
                    return _dt.fromisoformat(d.replace("Z", "+00:00"))
                return _dt.strptime(d[:19], "%Y-%m-%d %H:%M:%S")
            parsed = sorted(_parse(d) for d in played_dates)
            span = parsed[-1] - parsed[0]
            seconds = span.total_seconds()
            if seconds >= 3600:
                hours = seconds / 3600
                if hours < 48:
                    label_d = f"{int(round(hours))} ч."
                elif hours < 24 * 14:
                    label_d = f"{int(round(hours / 24))} дн."
                else:
                    label_d = f"{int(round(hours / 24 / 7))} нед."
                facts.append({
                    "kind":  "tournament_duration",
                    "icon":  "📅",
                    "title": "Хронометраж",
                    "label": "От старта до финала",
                    "value": label_d,
                    "sub":   f"{len(confirmed)} матчей за это время",
                    "score": 1.4,
                })
        except Exception:
            pass

    facts.sort(key=lambda f: (-f["score"], f["kind"]))
    return facts


# Fact "kinds" whose subject is a single player — used by the diversity
# selector to cap how many cards a single player can occupy. Pair-based
# and tournament-wide facts are intentionally excluded so the floor of
# unique-subject coverage stays achievable on small tournaments.
_PLAYER_FACT_KINDS = frozenset({
    "scoring_streak", "win_streak", "unbeaten", "winless",
    "clean_sheet_king", "conversion_king", "draw_specialist",
    "contribution_king", "group_dominator", "hat_trick", "doubles",
    "spectator_player", "no_goal_streak", "darkhorse", "sniper",
    "fewest_goals", "underdog", "comeback_kid", "ironman",
    "fast_start", "mood_swings", "lucky_one", "close_runnerup",
    "clean_win_king", "group_unbeaten", "worst_group_defense",
    "playoff_attacker", "playoff_defender", "multistage_scorer",
    "stability_king",
})


def _fact_subject(f: dict) -> str:
    """Diversity key for a fact. Player-anchored facts share their
    @label so multiple wins by the same player collapse to one bucket;
    everything else is keyed by ``kind`` so different tournament-wide
    observations stay independent."""
    if f.get("kind") in _PLAYER_FACT_KINDS:
        return (f.get("label") or "").strip().lower() or f.get("kind", "")
    return f.get("kind") or "unknown"


def select_top_facts(
    facts: list[dict],
    n: int = 6,
    *,
    max_per_subject: int = 1,
    champion_label: str | None = None,
    champion_max: int = 1,
    seed: int | None = None,
) -> list[dict]:
    """Pick ``n`` facts maximising subject diversity.

    Rules:
      * Same fact ``kind`` (e.g. two ``scoring_streak`` rows) is never
        chosen twice — only one winner per category.
      * Each subject (single player / single tournament-wide topic)
        gets at most ``max_per_subject`` facts in the result.
      * The tournament champion — already on the hero card as the big
        gold name — is capped separately to ``champion_max`` so the
        secondary panel doesn't just rehash who won everything.
      * If the pool is too thin to fill ``n`` slots while honouring
        the caps, the cap relaxes one step at a time until we either
        fill the panel or run out of facts.
      * ``seed`` injects deterministic randomness into the whole
        ranking: every slot (headline included) rotates on each
        «🎲 Ещё факты» click. The shuffle is weighted by score —
        high-score facts surface more often — but no fact is pinned,
        so consecutive rerolls produce genuinely different sets.
    """
    if not facts:
        return []
    pool = list(facts)
    pool.sort(key=lambda f: -(f.get("score") or 0.0))
    if seed is not None:
        import math
        import random
        rnd = random.Random(seed)
        # Full weighted shuffle — every slot (including the headline)
        # rotates on reroll. The weight is ``sqrt(1 + score)`` so
        # higher-score facts still tend to surface first, but the bias
        # is gentle enough that even low-score facts genuinely make it
        # into the panel sometimes; otherwise the same 6 high-score
        # facts dominate every spin.
        #
        # Efraimidis–Spirakis weighted reservoir keys:
        #   k = rnd.random() ** (1 / w),   w = sqrt(1 + score)
        # which means score=26 → w≈5.2 (key^0.19, biased high),
        # score=4 → w≈2.2 (key^0.45, moderate), score=1 → w≈1.4
        # (key^0.71, almost uniform).
        keyed = [
            (rnd.random() ** (1.0 / max(1.0, math.sqrt(
                1.0 + float(f.get("score") or 0.0)
            ))), f)
            for f in pool
        ]
        keyed.sort(key=lambda kv: -kv[0])
        pool = [f for _, f in keyed]

    champ_norm = (champion_label or "").strip().lower()

    def _try_pick(cap: int, champ_cap: int) -> list[dict]:
        used: dict[str, int] = {}
        kinds: set[str] = set()
        out: list[dict] = []
        for f in pool:
            kind = f.get("kind") or ""
            if kind in kinds:
                continue
            subj = _fact_subject(f)
            ceiling = (champ_cap
                       if subj == champ_norm and champ_norm
                       else cap)
            if used.get(subj, 0) >= ceiling:
                continue
            out.append(f)
            used[subj] = used.get(subj, 0) + 1
            kinds.add(kind)
            if len(out) >= n:
                break
        return out

    # First pass: hard caps. Then relax one step at a time until we
    # either fill the panel or exhaust the pool. The relaxation order
    # is "more facts per regular subject" first, then "more facts for
    # the champion" — diversity is preferred over showcasing the
    # winner over and over.
    for cap, champ_cap in (
        (max_per_subject, champion_max),
        (max_per_subject + 1, champion_max),
        (max_per_subject + 2, champion_max + 1),
        (99, 99),  # last resort — anything goes
    ):
        chosen = _try_pick(cap, champ_cap)
        if len(chosen) >= n or len(chosen) == len(pool):
            return chosen[:n]
    return _try_pick(99, 99)[:n]


def render_facts_text(facts: list[dict], top: int = 6,
                      champion_label: str | None = None) -> str:
    """Plain-text version of the top-N facts. Used by the .txt body.
    Routes through ``select_top_facts`` so the text digest matches the
    diversity rules of the PNG (no fact-spam from a single dominant
    player)."""
    if not facts:
        return ""
    chosen = select_top_facts(
        facts, n=top, max_per_subject=1, champion_label=champion_label,
        champion_max=2,
    )
    if not chosen:
        return ""
    lines = ["🎲 А ВЫ ЗНАЛИ?", "-" * 60]
    for f in chosen:
        line = f"{f['icon']}  {f['title']}: {f['label']}"
        if f.get("value"):
            line += f" — {f['value']}"
        if f.get("sub"):
            line += f"  ({f['sub']})"
        lines.append(line)
    lines.append("")
    return "\n".join(lines)
