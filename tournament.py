"""
Tournament management: group draws, standings calculation, playoff bracket.
"""
import json
import logging
import random
from datetime import datetime, timedelta
from database import (
    get_conn,
    get_tournament,
    get_tournament_players,
    add_player_to_tournament,
    update_tournament,
    update_tournament_player,
    get_tournament_matches,
    create_match,
    create_tournament,
    get_player_by_id,
    update_match,
    create_tournament_tour,
    get_next_tour_number,
    set_current_tour,
    get_tour_matches,
)

log = logging.getLogger(__name__)

GROUP_LETTERS = "ABCDEFGH"
# Playoff round names from largest to smallest. ``r512`` = 1/256 финала
# (512-player bracket), ..., ``r16`` = 1/8. ``advance_playoff`` walks
# this list in order, so newer (larger) stages must come first.
PLAYOFF_STAGES = [
    "r512", "r256", "r128", "r64", "r32", "r16",
    "qf", "sf", "final",
]

# Stage code for the optional "3rd place" match between the two SF
# losers. It runs **in parallel** with the final (does not feed any
# next stage) and is therefore intentionally kept OUT of
# ``PLAYOFF_STAGES`` so the linear advance loop in ``advance_playoff``
# treats it as a sibling rather than the next round after SF.
THIRD_PLACE_STAGE = "third"

# Superset of every stage code the bot may write into ``matches.stage``
# for a playoff fixture. Used by code that needs to enumerate "all
# playoff rows" (deduplication, prune_phantoms, leaderboard helpers).
ALL_PLAYOFF_STAGES = PLAYOFF_STAGES + [THIRD_PLACE_STAGE]

MATCH_DEADLINE_HOURS = 48


def get_stage_config(tournament: dict, stage: str) -> dict:
    """Return ``{"len": int, "mode": str}`` for the given playoff stage.

    Per-stage overrides live in ``tournaments.playoff_stage_config`` as a
    JSON blob keyed by stage code. Stages not present fall back to the
    tournament-wide ``playoff_matches_per_pair`` / ``playoff_advance_mode``.

    ``len``: max number of legs in the series (1, 3, 5, 7, …).
    ``mode``: ``"wins"`` → first to majority (early-stop allowed);
              ``"goals"`` → play all ``len`` legs, aggregate decides.

    The 3rd-place match accepts its own per-stage entry under the
    ``"third"`` key — when missing, it inherits the *final* stage's
    config so the bronze match defaults to the same bo-N / mode as
    the championship.
    """
    raw = (tournament or {}).get("playoff_stage_config") or ""
    cfg: dict = {}
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                cfg = parsed
        except (ValueError, TypeError):
            cfg = {}
    stage_cfg = cfg.get(stage) or {}
    # 3rd-place match falls back to whatever the FINAL is configured
    # for — admins who set the final to bo3 typically want the bronze
    # match to honour the same series length without a second knob.
    if not stage_cfg and stage == THIRD_PLACE_STAGE:
        stage_cfg = cfg.get("final") or {}
    try:
        legs = int(stage_cfg.get("len") or 0)
    except (TypeError, ValueError):
        legs = 0
    if legs <= 0:
        legs = int(tournament.get("playoff_matches_per_pair") or 1)
    mode = (
        stage_cfg.get("mode")
        or tournament.get("playoff_advance_mode")
        or "goals"
    ).lower()
    if mode not in ("wins", "goals"):
        mode = "goals"
    return {"len": max(1, legs), "mode": mode}


def set_stage_config(tid: int, stage: str, legs: int, mode: str) -> dict:
    """Persist per-stage playoff override and return the full updated JSON.

    Passing ``legs<=0`` removes the override for that stage so it falls
    back to the tournament defaults.
    """
    t = get_tournament(tid) or {}
    raw = t.get("playoff_stage_config") or ""
    cfg: dict = {}
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                cfg = parsed
        except (ValueError, TypeError):
            cfg = {}
    if legs <= 0:
        cfg.pop(stage, None)
    else:
        mode = (mode or "goals").lower()
        if mode not in ("wins", "goals"):
            mode = "goals"
        cfg[stage] = {"len": int(legs), "mode": mode}
    new_raw = json.dumps(cfg, ensure_ascii=False, sort_keys=True)
    update_tournament(tid, playoff_stage_config=new_raw)
    return cfg


def draw_groups(tid: int, player_ids: list[int], groups_count: int) -> dict[str, list[int]]:
    """Randomly assign players to groups, return {group_letter: [player_id, ...]}."""
    shuffled = player_ids[:]
    random.shuffle(shuffled)

    groups: dict[str, list[int]] = {GROUP_LETTERS[i]: [] for i in range(groups_count)}
    for idx, pid in enumerate(shuffled):
        g = GROUP_LETTERS[idx % groups_count]
        groups[g].append(pid)
        add_player_to_tournament(tid, pid, g)

    return groups


def generate_group_fixtures(tid: int, groups: dict[str, list[int]]):
    """
    Create all round-robin matches for each group. Honours the
    tournament's `group_matches_per_pair` setting: 1 = single round-robin,
    2 = double round-robin (each pair plays twice, second leg with
    swapped sides).
    """
    deadline = datetime.utcnow() + timedelta(hours=MATCH_DEADLINE_HOURS)
    t = get_tournament(tid) or {}
    mpp = max(1, int(t.get("group_matches_per_pair") or 1))
    mids = []
    for group, pids in groups.items():
        for i in range(len(pids)):
            for j in range(i + 1, len(pids)):
                for leg in range(1, mpp + 1):
                    # Alternate the home/away order so 2nd leg is the
                    # "return" fixture — feels natural in standings.
                    if leg % 2 == 1:
                        a, b = pids[i], pids[j]
                    else:
                        a, b = pids[j], pids[i]
                    mid = create_match(
                        tid, a, b,
                        stage="group",
                        round_num=leg,
                        deadline=deadline.strftime("%Y-%m-%d %H:%M:%S"),
                        leg=leg,
                    )
                    mids.append(mid)
    return mids


def get_group_standings(tid: int) -> dict[str, list[dict]]:
    """Return standings dict keyed by group letter."""
    players = get_tournament_players(tid)
    groups: dict[str, list[dict]] = {}
    for p in players:
        g = p["group_name"]
        if g not in groups:
            groups[g] = []
        groups[g].append(p)

    # Sort each group: pts desc, GD desc, GF desc
    for g in groups:
        groups[g].sort(
            key=lambda x: (x["group_points"], x["group_gf"] - x["group_ga"], x["group_gf"]),
            reverse=True,
        )
    return groups


def format_standings_message(tid: int) -> str:
    # Best-effort load of per-player titles ("🏅 Чемпион №76") so the
    # text-table renders them inline. Fail-soft: if anything goes wrong,
    # we just skip the titles and render the legacy plain-username
    # column.
    titles_by_pid: dict[int, list[str]] = {}
    try:
        from database import list_player_titles  # local: avoid cycle
        # Resolve only for the players that actually appear in
        # standings — saves a query per non-tournament player.
        rows_for_pids = get_group_standings(tid)
        seen_pids: set[int] = set()
        for _, ps in rows_for_pids.items():
            for r in ps:
                pid = r.get("player_id") or r.get("id")
                if isinstance(pid, int):
                    seen_pids.add(pid)
        for pid in seen_pids:
            try:
                titles_by_pid[pid] = [
                    t["title"] for t in list_player_titles(pid)
                ]
            except Exception:
                continue
    except Exception:
        titles_by_pid = {}

    standings = get_group_standings(tid)
    # Custom group name (if set via /set_groupname). Falls back to
    # "Группа A/B/..." when empty.
    t = get_tournament(tid) or {}
    custom_name = (t.get("group_display_name") or "").strip()
    lines = ["📊 <b>Турнирная таблица</b>\n"]
    for g, players in sorted(standings.items()):
        if custom_name and len(standings) == 1:
            lines.append(f"<b>{custom_name}</b>")
        else:
            lines.append(f"<b>Группа {g}</b>")
        lines.append("```")
        lines.append(f"{'#':<3} {'Игрок':<15} {'И':>3} {'В':>3} {'Н':>3} {'П':>3} {'Г':>6} {'О':>4}")
        lines.append("─" * 42)
        for pos, p in enumerate(players, 1):
            played = p["group_wins"] + p["group_draws"] + p["group_losses"]
            gd = f"{p['group_gf']}:{p['group_ga']}"
            lines.append(
                f"{pos:<3} {p['username']:<15} {played:>3} {p['group_wins']:>3} "
                f"{p['group_draws']:>3} {p['group_losses']:>3} {gd:>6} {p['group_points']:>4}"
            )
        lines.append("```")
        # Append a per-group titles block when at least one player has
        # awards. Kept separate from the monospace box above so emoji
        # in titles render correctly without breaking column widths.
        title_lines: list[str] = []
        for p in players:
            pid = p.get("player_id") or p.get("id")
            tts = titles_by_pid.get(int(pid) if isinstance(pid, int) else 0) if pid else []
            if tts:
                # Dedup while keeping order.
                seen_t: set[str] = set()
                uniq_t: list[str] = []
                for t in tts:
                    k = (t or "").strip().lower()
                    if k in seen_t:
                        continue
                    seen_t.add(k)
                    uniq_t.append(t)
                title_lines.append(
                    f"🏅 {p['username']}: {' • '.join(uniq_t)}"
                )
        if title_lines:
            lines.extend(title_lines)
        lines.append("")
    return "\n".join(lines)


def check_groups_complete(tid: int) -> bool:
    """Return True if all group-stage matches are confirmed."""
    matches = get_tournament_matches(tid, stage="group")
    return all(m["status"] == "confirmed" for m in matches) and len(matches) > 0


def _next_pow2(n: int) -> int:
    """Smallest power of two >= n. ``_next_pow2(14) == 16``,
    ``_next_pow2(8) == 8``."""
    if n <= 1:
        return 1
    p = 1
    while p < n:
        p *= 2
    return p


def _bracket_seed_order(size: int) -> list[int]:
    """Standard tournament-bracket seed ordering for a power-of-two field.

    Returns a list of seed numbers (1..size) such that every adjacent
    pair ``(out[2i], out[2i+1])`` is a first-round opponent and the
    pair structure cascades correctly: winners of pairs ``(0,1)`` meet
    in the next round, ``(2,3)`` meet, etc. Top seed always faces the
    weakest seed available, second seed faces the second-weakest, and
    halves are split so the top two seeds can only meet in the final.

    Examples::

        _bracket_seed_order(2)  == [1, 2]
        _bracket_seed_order(4)  == [1, 4, 2, 3]
        _bracket_seed_order(8)  == [1, 8, 4, 5, 2, 7, 3, 6]
        _bracket_seed_order(16) == [1, 16, 8, 9, 4, 13, 5, 12,
                                    2, 15, 7, 10, 3, 14, 6, 11]
    """
    if size <= 1:
        return [1]
    half = _bracket_seed_order(size // 2)
    out: list[int] = []
    for s in half:
        out.append(s)
        out.append(size + 1 - s)
    return out


def _seed_qualifiers(
    standings: dict[str, list[dict]],
    advance_per_group: int,
    pairing: str = "auto",
) -> list[dict]:
    """Flatten group standings into a globally-seeded qualifier list.

    For the common 2-group case we use a **cross-bracket** draw that
    guarantees first-round opponents come from different groups —
    qualifiers are interleaved by group within each position tier
    (A1, B1, A2, B2, …). Combined with the standard bracket-seed order
    ``[1, 8, 4, 5, 2, 7, 3, 6]``, every QF pair is cross-group:
    ``A1×B4``, ``B2×A3``, ``B1×A4``, ``A2×B3``.

    For 2+ groups we use the same interleaved approach: qualifiers are
    ordered by group strength (winner's record), then by position,
    so first-round opponents never come from the same group.

    When ``pairing="pairs"`` (and there are exactly 4 groups with 2
    advancing each), groups are paired as (A,C) and (B,D) to produce
    the specific pairs ``A1×C2``, ``B2×D1``, ``A2×C1``, ``B1×D2``.

    For a single group qualifiers are returned in position order.

    Group ordering: the group whose winner has the strongest record
    (pts → GD → GF → letter) becomes the "primary" group.
    """
    if not standings:
        return []

    groups = sorted(standings.keys())

    # ── Pairs mode for 4 groups (2 advancers each) ─────────────────────
    # Pair groups as (A,C) and (B,D) so QF pairs are:
    #   A1×C2, B2×D1, A2×C1, B1×D2
    if pairing == "pairs" and len(groups) == 4 and advance_per_group == 2:
        pair1 = (groups[0], groups[2])  # e.g. (A, C)
        pair2 = (groups[1], groups[3])  # e.g. (B, D)
        # Block order: pair1[0], pair2[0], pair2[1], pair1[1]
        block_groups = [pair1[0], pair2[0], pair2[1], pair1[1]]
        quals: list[dict] = []
        for g in block_groups:
            for pos, p in enumerate(standings[g][:advance_per_group]):
                quals.append({
                    **p,
                    "_group": g,
                    "_pos": pos,
                    "_pts": int(p.get("group_points") or 0),
                    "_gd": int(p.get("group_gf") or 0)
                            - int(p.get("group_ga") or 0),
                    "_gf": int(p.get("group_gf") or 0),
                })
        return quals

    # ── Cross-bracket interleave for the 2-group case ─────────────────
    # Same-group QF matchups were the seeding bug we hit on
    # 8-qualifier brackets: sorting tier-3 by points (so e.g. B4 with
    # 15 pts beat A4 with 13 pts onto seed 7) put both group-A players
    # into the (1,8) pair and both group-B players into the (2,7) pair.
    if len(groups) == 2:
        def _grp_key(g: str) -> tuple[int, int, int, str]:
            row = standings[g][0] if standings[g] else None
            if row is None:
                # Empty group sorts last (after all populated groups).
                return (10**9, 10**9, 10**9, g)
            return (
                -int(row.get("group_points") or 0),
                -((int(row.get("group_gf") or 0))
                  - (int(row.get("group_ga") or 0))),
                -int(row.get("group_gf") or 0),
                g,
            )

        ordered_groups = sorted(groups, key=_grp_key)
        quals: list[dict] = []
        for pos in range(advance_per_group):
            for g in ordered_groups:
                rows = standings[g]
                if pos < len(rows):
                    p = rows[pos]
                    quals.append({
                        **p,
                        "_group": g,
                        "_pos": pos,
                        "_pts": int(p.get("group_points") or 0),
                        "_gd": int(p.get("group_gf") or 0)
                                - int(p.get("group_ga") or 0),
                        "_gf": int(p.get("group_gf") or 0),
                    })
        return quals

    # ── Single group: just return qualifiers in position order ────────
    if len(groups) == 1:
        quals: list[dict] = []
        for pos, p in enumerate(standings[groups[0]][:advance_per_group]):
            quals.append({
                **p,
                "_group": groups[0],
                "_pos": pos,
                "_pts": int(p.get("group_points") or 0),
                "_gd": int(p.get("group_gf") or 0) - int(p.get("group_ga") or 0),
                "_gf": int(p.get("group_gf") or 0),
            })
        return quals

    # ── 2+ groups: interleave by group to guarantee cross-group QF ────
    def _grp_key(g: str) -> tuple[int, int, int, str]:
        row = standings[g][0] if standings[g] else None
        if row is None:
            return (10**9, 10**9, 10**9, g)
        return (
            -int(row.get("group_points") or 0),
            -((int(row.get("group_gf") or 0))
              - (int(row.get("group_ga") or 0))),
            -int(row.get("group_gf") or 0),
            g,
        )

    ordered_groups = sorted(groups, key=_grp_key)
    quals: list[dict] = []
    for pos in range(advance_per_group):
        for g in ordered_groups:
            rows = standings[g]
            if pos < len(rows):
                p = rows[pos]
                quals.append({
                    **p,
                    "_group": g,
                    "_pos": pos,
                    "_pts": int(p.get("group_points") or 0),
                    "_gd": int(p.get("group_gf") or 0)
                            - int(p.get("group_ga") or 0),
                    "_gf": int(p.get("group_gf") or 0),
                })
    return quals


def _seed_players_by_elo(tid: int, t_type: str | None) -> list[dict]:
    """Bracket-only mode: seed all tournament participants by global ELO.

    No groups exist, so we sort by per-type ELO (``elo_vsa`` or
    ``elo_ri``), falling back to global ``elo`` and finally to the
    username for a fully-deterministic ordering. The returned list
    contains ``{player_id, username, ...}`` dicts in seeded order
    (top ELO = top seed).
    """
    from database import get_player_by_id as _gpbi
    elo_field = {"vsa": "elo_vsa", "ri": "elo_ri"}.get(
        (t_type or "").lower(), "elo"
    )
    rows = get_tournament_players(tid)
    out: list[dict] = []
    for row in rows:
        p = _gpbi(row["player_id"]) or {}
        out.append({
            "player_id": row["player_id"],
            "username":  p.get("username") or row.get("username")
                         or f"id{row['player_id']}",
            "_elo":      float(p.get(elo_field) or p.get("elo") or 0),
            "_uname":    (p.get("username") or "").lower(),
        })
    # Higher ELO first; tie-break by username (alphabetical, deterministic).
    out.sort(key=lambda x: (-x["_elo"], x["_uname"]))
    return out


def _bracket_first_stage(bracket_size: int) -> str:
    """Map bracket size to the stage name of the first round."""
    return {
        2:   "final",
        4:   "sf",
        8:   "qf",
        16:  "r16",
        32:  "r32",
        64:  "r64",
        128: "r128",
        256: "r256",
        512: "r512",
    }.get(bracket_size, "r512")


def _build_bracket_pairs(
    seeded: list[dict], bracket_size: int
) -> list[tuple[dict | None, dict | None]]:
    """Produce ``bracket_size/2`` first-round pairs in bracket order.

    Each element is a ``(player_or_None, player_or_None)`` tuple. A
    ``None`` slot represents a non-existent seed (i.e. seed > len(seeded))
    and turns its bracket pair into a bye for the surviving side.
    """
    seed_to_player: dict[int, dict] = {
        i + 1: q for i, q in enumerate(seeded)
    }
    order = _bracket_seed_order(bracket_size)
    pairs: list[tuple[dict | None, dict | None]] = []
    for i in range(0, bracket_size, 2):
        sa, sb = order[i], order[i + 1]
        pairs.append((seed_to_player.get(sa), seed_to_player.get(sb)))
    return pairs


def compute_playoff_preview(
    tid: int, advance_per_group: int | None = None
) -> dict:
    """Pure (read-only) projection of what ``generate_playoff`` would
    create right now, given the current group standings.

    Returns ``{"stage": <stage>, "pairs": [{"a": p, "b": p, "bye": bool},
    ...], "qualifiers": {grp: [p, ...]}, "seeded": [...]}``. ``a`` /
    ``b`` are tournament-player dicts (``b`` is ``None`` for bye pairs).
    """
    t = get_tournament(tid) or {}

    # Bracket-only mode: no groups, seed by global ELO directly.
    if int(t.get("bracket_only") or 0):
        seeded = _seed_players_by_elo(tid, t.get("tournament_type"))
        qualifiers_by_group: dict[str, list[dict]] = {}
    else:
        standings = get_group_standings(tid)
        groups = sorted(standings.keys())
        if not groups:
            return {"stage": None, "pairs": [], "qualifiers": {}, "seeded": []}

        if advance_per_group is None:
            advance_per_group = max(1, int(t.get("playoff_slots") or 2))

        qualifiers_by_group = {
            g: standings[g][:advance_per_group] for g in groups
        }
        pairing = (t.get("playoff_pairing") or "auto").lower()
        seeded = _seed_qualifiers(standings, advance_per_group, pairing=pairing)
    n = len(seeded)
    if n < 2:
        return {"stage": None, "pairs": [], "qualifiers": qualifiers_by_group,
                "seeded": seeded}

    bracket_size = min(_next_pow2(n), 512)
    if n > 512:
        # >512 quals would need r1024+ stages; pragmatic cap. The lowest
        # seeds drop out instead of inventing new stage names.
        seeded = seeded[:512]
        n = 512
    stage = _bracket_first_stage(bracket_size)
    raw_pairs = _build_bracket_pairs(seeded, bracket_size)

    pairs: list[dict] = []
    for pa, pb in raw_pairs:
        if pa and pb:
            pairs.append({"a": pa, "b": pb, "bye": False})
        elif pa or pb:
            pairs.append({"a": pa or pb, "b": None, "bye": True})
        # both None: bracket position has no players → skip silently
    return {
        "stage": stage,
        "pairs": pairs,
        "qualifiers": qualifiers_by_group,
        "seeded": seeded,
        "bracket_size": bracket_size,
    }


def generate_playoff(
    tid: int,
    advance_per_group: int | None = None,
) -> list[dict]:
    """
    Build playoff bracket from group standings.

    The bracket is **seeded** (best group winner = top seed) and sized
    to the next power of two ≥ qualifiers, up to 64. Top seeds receive
    byes for any extra slots — e.g. with 14 qualifiers the bracket is
    16-wide and the top 2 seeds skip the first round; with 18 the
    bracket is 32-wide, the top 14 seeds skip straight to R16, and
    the bottom 4 fight in 2 R32 matches for the remaining slots.

    Honours ``playoff_matches_per_pair`` (1 = single-leg, 2 = two legs
    by aggregate goals); bye "matches" are always single-leg and
    auto-confirmed so ``advance_playoff`` cascades them naturally.

    The qualifier count per group defaults to ``tournaments.playoff_slots``
    (2 if unset), but callers can override it with ``advance_per_group``.

    **Idempotent**: if any playoff match already exists for this
    tournament (any stage, any status), the function returns the
    existing bracket as-is and does NOT create new rows.

    Returns a list of created match dicts:
    ``{stage, player1, player2, match_id, leg, bye?}``.
    """
    # ── Idempotency guard: never recreate an existing bracket. ─────────
    for s in PLAYOFF_STAGES:
        existing = get_tournament_matches(tid, stage=s)
        if existing:
            # Build the same shape of dict the original code returns so
            # callers can reuse the response uniformly.
            from database import get_player_by_id as _gpbi
            res = []
            for m in existing:
                pa = _gpbi(m["player1_id"]) or {}
                pb = _gpbi(m["player2_id"]) or {}
                is_bye = m["player1_id"] == m["player2_id"]
                res.append({
                    "stage":     m.get("stage"),
                    "player1":   pa.get("username") or f"id{m['player1_id']}",
                    "player2":   "BYE" if is_bye else (pb.get("username") or f"id{m['player2_id']}"),
                    "match_id":  m["id"],
                    "leg":       m.get("leg") or 1,
                    "bye":       is_bye,
                })
            update_tournament(tid, playoff_started=1, stage="playoff")
            return res

    t = get_tournament(tid) or {}
    legs = max(1, int(t.get("playoff_matches_per_pair") or 1))

    # ── Bracket-only tournaments: skip group-standings entirely. ───────
    # All registered players go straight into the bracket, seeded by
    # global ELO of the matching tournament type (default). If
    # draw_mode="random", we shuffle instead of seeding.
    # draw_mode="manual" is handled externally (handlers/templates.py).
    if int(t.get("bracket_only") or 0):
        draw_mode = (t.get("draw_mode") or "auto").lower()
        if draw_mode == "manual":
            # Manual draw is done via the /draw_manual command.
            # If generate_playoff is called anyway, fall back to auto.
            pass
        seeded = _seed_players_by_elo(tid, t.get("tournament_type"))
        if draw_mode == "random":
            random.shuffle(seeded)
    else:
        standings = get_group_standings(tid)
        if not standings:
            return []

        if advance_per_group is None:
            advance_per_group = max(1, int(t.get("playoff_slots") or 2))

        pairing = (t.get("playoff_pairing") or "auto").lower()
        seeded = _seed_qualifiers(standings, advance_per_group, pairing=pairing)

    n = len(seeded)
    if n < 2:
        return []

    bracket_size = min(_next_pow2(n), 512)
    if n > 512:
        # >512 quals would need r1024+ stages; pragmatic cap. The lowest
        # seeds drop out instead of inventing new stage names.
        seeded = seeded[:512]
        n = 512
    stage = _bracket_first_stage(bracket_size)
    raw_pairs = _build_bracket_pairs(seeded, bracket_size)

    created: list[dict] = []
    deadline = datetime.utcnow() + timedelta(hours=MATCH_DEADLINE_HOURS)
    dl_str = deadline.strftime("%Y-%m-%d %H:%M:%S")

    for pa, pb in raw_pairs:
        if pa and pb:
            # Real first-round match (1 or 2 legs).
            for leg in range(1, legs + 1):
                if leg % 2 == 1:
                    a, b = pa, pb
                else:
                    a, b = pb, pa
                mid = create_match(tid, a["player_id"], b["player_id"],
                                   stage=stage, deadline=dl_str, leg=leg)
                created.append({
                    "stage":    stage,
                    "player1":  a["username"],
                    "player2":  b["username"],
                    "match_id": mid,
                    "leg":      leg,
                    "bye":      False,
                })
        elif pa or pb:
            # Bye for the surviving seed: a single auto-confirmed
            # "match" with the byed player on both sides. ``advance_playoff``
            # will treat it as already won and pair the byed player with
            # the winner of the adjacent first-round pair (per bracket
            # ordering). Bye matches are always single-leg, even when
            # ``playoff_matches_per_pair=2``.
            byed = pa or pb
            mid = create_match(tid, byed["player_id"], byed["player_id"],
                               stage=stage, deadline=dl_str, leg=1)
            update_match(mid, score1=1, score2=0, status="confirmed",
                         reported_by=None)
            created.append({
                "stage":    stage,
                "player1":  byed["username"],
                "player2":  "BYE",
                "match_id": mid,
                "leg":      1,
                "bye":      True,
            })
        # else: bracket position has no players — happens only if
        # bracket_size > 2*n (impossible by construction), so skip.

    update_tournament(tid, playoff_started=1, stage="playoff")
    return created


def format_playoff_bracket(tid: int) -> str:
    tournament = get_tournament(tid)
    lines = [f"🏆 <b>Плей-офф: {tournament['name']}</b>\n"]

    def _fmt_stage_cfg(stage: str) -> str:
        cfg = get_stage_config(tournament, stage)
        mode = "по победам" if cfg["mode"] == "wins" else "по голам"
        return f"bo{cfg['len']} · {mode}"

    stage_names = {
        "r512": "1/256 финала",
        "r256": "1/128 финала",
        "r128": "1/64 финала",
        "r64":  "1/32 финала",
        "r32":  "1/16 финала",
        "r16":  "1/8 финала",
        "qf":   "Четвертьфинал",
        "sf":   "Полуфинал",
        "final": "Финал",
        "third": "Матч за 3-е место",
    }

    # Walk forward until the final so the user sees the WHOLE path
    # (TBD placeholders for stages that haven't been generated yet).
    populated_stages = [
        s for s in PLAYOFF_STAGES if get_tournament_matches(tid, stage=s)
    ]
    earliest = (
        PLAYOFF_STAGES.index(populated_stages[0])
        if populated_stages else len(PLAYOFF_STAGES)
    )
    prev_pair_count: int | None = None
    # Ordered list of resolved winners of the previous stage's pairs
    # (None when the pair isn't decided yet). Used to project partial
    # cards into the next TBD stage so e.g. the Final shows
    # ``@phoenileo vs TBD`` as soon as one SF pair closes.
    prev_winners: list[int | None] | None = None

    for s in PLAYOFF_STAGES[earliest:]:
        matches = get_tournament_matches(tid, stage=s)
        if not matches:
            # Future stage — render TBD placeholders so the bracket is
            # visible top-to-bottom. Project partial winners from the
            # previous stage when known.
            n = max(1, (prev_pair_count or 2) // 2)
            lines.append(
                f"<b>{stage_names.get(s, s.upper())}</b> "
                f"<i>({_fmt_stage_cfg(s)})</i>"
            )
            for slot in range(n):
                wa = wb = None
                if prev_winners is not None:
                    if 2 * slot < len(prev_winners):
                        wa = prev_winners[2 * slot]
                    if 2 * slot + 1 < len(prev_winners):
                        wb = prev_winners[2 * slot + 1]
                if wa is None and wb is None:
                    lines.append("  ⏳ TBD vs TBD")
                else:
                    pa = get_player_by_id(wa) if wa is not None else None
                    pb = get_player_by_id(wb) if wb is not None else None
                    na = f"@{pa['username']}" if pa else "TBD"
                    nb = f"@{pb['username']}" if pb else "TBD"
                    # When only one side is known the slot is waiting
                    # for an opponent; when both are known the match
                    # row just hasn't spawned yet.
                    icon = "🕐" if (wa is None or wb is None) else "▶️"
                    lines.append(f"  {icon} {na} vs {nb}")
            lines.append("")
            prev_pair_count = n
            # We can't project further than one stage deep.
            prev_winners = None
            continue
        lines.append(
            f"<b>{stage_names.get(s, s.upper())}</b> "
            f"<i>({_fmt_stage_cfg(s)})</i>"
        )
        stage_cfg = get_stage_config(tournament, s)
        adv_mode = stage_cfg["mode"]
        legs_target = stage_cfg["len"]

        # Group by pair so 2-leg ties show as a single block. Within
        # each (pair, leg) bucket we keep only the highest-id row —
        # that's how we silently swallow duplicate playoff fixtures
        # from old bugs without rewriting the database.
        leg_dedup: dict[tuple, dict] = {}
        for m in matches:
            key = (_pair_key(m), int(m.get("leg") or 1))
            cur = leg_dedup.get(key)
            if cur is None or (m["id"] or 0) > (cur["id"] or 0):
                leg_dedup[key] = m
        pairs: dict[tuple[int, int], list[dict]] = {}
        for m in leg_dedup.values():
            pairs.setdefault(_pair_key(m), []).append(m)
        prev_pair_count = len(pairs)
        # Track winners of this stage's pairs in iteration order so the
        # next-stage TBD block can project them as ``winner vs TBD``.
        stage_winners: list[int | None] = []

        for key, ms in pairs.items():
            ms_sorted = sorted(ms, key=lambda x: x.get("leg") or 1)
            a_id = ms_sorted[0]["player1_id"]
            b_id = ms_sorted[0]["player2_id"]
            pa = get_player_by_id(a_id)
            pb = get_player_by_id(b_id)
            # Bye row (auto-confirmed, same player on both sides) — render
            # as a free pass to the next round instead of "X 1:0 X".
            if a_id == b_id:
                lines.append(
                    f"  🎟  {pa['username']} → bye"
                )
                stage_winners.append(a_id)
                continue
            if any(m["status"] != "confirmed" for m in ms_sorted):
                if len(ms_sorted) == 1:
                    lines.append(
                        f"  ⚔️  {pa['username']} vs {pb['username']}  [{ms_sorted[0]['status']}]"
                    )
                else:
                    legs_str = " · ".join(
                        f"L{m.get('leg') or 1}: "
                        + (f"{m['score1']}:{m['score2']}" if m["status"] == "confirmed" else m["status"])
                        for m in ms_sorted
                    )
                    lines.append(f"  ⚔️  {pa['username']} vs {pb['username']}  ({legs_str})")
                stage_winners.append(None)
                continue

            # All legs confirmed → show aggregate.
            a_goals = b_goals = 0
            leg_strs = []
            for m in ms_sorted:
                if m["player1_id"] == a_id:
                    a_goals += m["score1"]
                    b_goals += m["score2"]
                    leg_str = f"{m['score1']}:{m['score2']}"
                    p1, p2 = m.get("pen1"), m.get("pen2")
                else:
                    a_goals += m["score2"]
                    b_goals += m["score1"]
                    leg_str = f"{m['score2']}:{m['score1']}"
                    # Swap pens to canonical a/b orientation.
                    p1, p2 = m.get("pen2"), m.get("pen1")
                if p1 is not None and p2 is not None:
                    leg_str += f" (пен. {p1}:{p2})"
                leg_strs.append(leg_str)
            winner_id = _resolve_pair_winner(
                ms_sorted, advance_mode=adv_mode, series_len=legs_target,
            )
            stage_winners.append(winner_id)
            winner_name = (pa if winner_id == a_id else pb)["username"]
            if len(ms_sorted) == 1:
                lines.append(
                    f"  ✅ {pa['username']} {leg_strs[0]} {pb['username']}  → 🏅 {winner_name}"
                )
            else:
                agg = f"{a_goals}:{b_goals}"
                legs_disp = " · ".join(f"L{i+1} {s}" for i, s in enumerate(leg_strs))
                lines.append(
                    f"  ✅ {pa['username']} <b>{agg}</b> {pb['username']}  ({legs_disp}) → 🏅 {winner_name}"
                )
        lines.append("")
        prev_winners = stage_winners

    # Optional 3rd-place fixture lives outside ``PLAYOFF_STAGES`` and
    # runs in parallel with the final. Render it as a trailing block so
    # the bronze match shows up below the main bracket.
    third_matches = _dedup_playoff_legs(
        get_tournament_matches(tid, stage=THIRD_PLACE_STAGE)
    )
    if third_matches:
        s = THIRD_PLACE_STAGE
        lines.append(
            f"<b>{stage_names[s]}</b> <i>({_fmt_stage_cfg(s)})</i>"
        )
        stage_cfg = get_stage_config(tournament, s)
        adv_mode = stage_cfg["mode"]
        legs_target = stage_cfg["len"]
        ms_sorted = sorted(third_matches, key=lambda x: x.get("leg") or 1)
        a_id = ms_sorted[0]["player1_id"]
        b_id = ms_sorted[0]["player2_id"]
        pa = get_player_by_id(a_id)
        pb = get_player_by_id(b_id)
        if any(m["status"] != "confirmed" for m in ms_sorted):
            if len(ms_sorted) == 1:
                lines.append(
                    f"  ⚔️  {pa['username']} vs {pb['username']}  "
                    f"[{ms_sorted[0]['status']}]"
                )
            else:
                legs_str = " · ".join(
                    f"L{m.get('leg') or 1}: "
                    + (
                        f"{m['score1']}:{m['score2']}"
                        if m["status"] == "confirmed" else m["status"]
                    )
                    for m in ms_sorted
                )
                lines.append(
                    f"  ⚔️  {pa['username']} vs {pb['username']}  "
                    f"({legs_str})"
                )
        else:
            a_goals = b_goals = 0
            leg_strs = []
            for m in ms_sorted:
                if m["player1_id"] == a_id:
                    a_goals += m["score1"]
                    b_goals += m["score2"]
                    leg_str = f"{m['score1']}:{m['score2']}"
                    p1, p2 = m.get("pen1"), m.get("pen2")
                else:
                    a_goals += m["score2"]
                    b_goals += m["score1"]
                    leg_str = f"{m['score2']}:{m['score1']}"
                    p1, p2 = m.get("pen2"), m.get("pen1")
                if p1 is not None and p2 is not None:
                    leg_str += f" (пен. {p1}:{p2})"
                leg_strs.append(leg_str)
            winner_id = _resolve_pair_winner(
                ms_sorted, advance_mode=adv_mode, series_len=legs_target,
            )
            winner_name = (pa if winner_id == a_id else pb)["username"]
            if len(ms_sorted) == 1:
                lines.append(
                    f"  🥉 {pa['username']} {leg_strs[0]} {pb['username']}  "
                    f"→ 🥉 {winner_name}"
                )
            else:
                agg = f"{a_goals}:{b_goals}"
                legs_disp = " · ".join(
                    f"L{i + 1} {s}" for i, s in enumerate(leg_strs)
                )
                lines.append(
                    f"  🥉 {pa['username']} <b>{agg}</b> {pb['username']}  "
                    f"({legs_disp}) → 🥉 {winner_name}"
                )
        lines.append("")

    return "\n".join(lines)


def _resolve_pair_winner(
    matches: list[dict],
    advance_mode: str = "goals",
    series_len: int | None = None,
) -> int | None:
    """
    Given all matches between the same pair in the same stage (1 or
    more legs, possibly an extra match), return the player_id of the
    winner. Returns None if not enough data yet (some leg is still
    pending, or aggregate is tied with no extra match yet).

    ``advance_mode`` controls the primary comparison:
      * ``"goals"`` (default) — total goals across all legs decide;
        wins count is the tiebreaker.
      * ``"wins"`` — number of match wins decides; total goals is the
        tiebreaker.

    ``series_len`` enables early-stop for best-of-N series in ``"wins"``
    mode. When set, a player who has accumulated ``(series_len+1)//2``
    wins is declared the winner immediately, even if not every leg has
    been played yet. In ``"goals"`` mode the parameter still bounds the
    "wait for more legs" decision: while fewer than ``series_len`` legs
    are confirmed the resolver returns ``None`` (caller will spawn the
    next leg).

    When the tournament has penalties enabled and the pair is still
    deadlocked after the regular comparison (same wins AND same goals),
    the LAST leg's penalty-shootout score (``pen1``/``pen2``) breaks the
    tie. If only one leg in the series has a recorded shootout, that
    one decides; if multiple have shootouts (rare — usually only the
    deciding leg goes to pens) the latest leg by id wins.
    """
    if not matches:
        return None
    # Exclude rejected matches — they don't count as "unfinished legs"
    # and should not block series resolution.
    matches = [m for m in matches if (m.get("status") or "") != "rejected"]
    confirmed = [m for m in matches if m["status"] == "confirmed"]

    # Determine the canonical pair from the first match (any leg works
    # — the pair is the same for the whole series).
    first = matches[0]
    a_id = first["player1_id"]
    b_id = first["player2_id"]

    a_goals = b_goals = 0
    a_wins_count = b_wins_count = 0
    a_pens = b_pens = None
    # Track the "shootout leg" by id so when multiple legs are played
    # to pens the most recent one decides.
    pen_leg_id = -1
    for m in confirmed:
        s1, s2 = m["score1"], m["score2"]
        if s1 is None or s2 is None:
            continue
        # Map this leg's player1/player2 to canonical a/b
        if m["player1_id"] == a_id:
            a_goals += s1
            b_goals += s2
            if s1 > s2:
                a_wins_count += 1
            elif s2 > s1:
                b_wins_count += 1
            p1, p2 = m.get("pen1"), m.get("pen2")
        else:
            a_goals += s2
            b_goals += s1
            if s2 > s1:
                a_wins_count += 1
            elif s1 > s2:
                b_wins_count += 1
            # Swap pens to canonical a/b orientation.
            p1, p2 = m.get("pen2"), m.get("pen1")
        if p1 is not None and p2 is not None and (m.get("id") or 0) > pen_leg_id:
            a_pens, b_pens = int(p1), int(p2)
            pen_leg_id = m.get("id") or 0

    # Early-stop for "wins" mode: first to majority of ``series_len``.
    if advance_mode == "wins" and series_len and series_len > 0:
        threshold = series_len // 2 + 1
        if a_wins_count >= threshold:
            return a_id
        if b_wins_count >= threshold:
            return b_id

    # Without early-stop we need every scheduled leg confirmed before
    # comparing aggregates.
    if len(confirmed) < len(matches):
        return None

    if advance_mode == "wins":
        # Primary: wins count; tiebreaker: total goals
        if a_wins_count > b_wins_count:
            return a_id
        if b_wins_count > a_wins_count:
            return b_id
        if a_goals > b_goals:
            return a_id
        if b_goals > a_goals:
            return b_id
    else:
        # Primary: total goals; tiebreaker: wins count
        if a_goals > b_goals:
            return a_id
        if b_goals > a_goals:
            return b_id
        if a_wins_count > b_wins_count:
            return a_id
        if b_wins_count > a_wins_count:
            return b_id

    # Final tiebreaker: penalty shootout from the latest leg that had one.
    if a_pens is not None and b_pens is not None and a_pens != b_pens:
        return a_id if a_pens > b_pens else b_id
    return None  # still tied → caller schedules an extra match


def _pair_key(m: dict) -> tuple[int, int]:
    """Order-insensitive key for a pair of players."""
    a, b = m["player1_id"], m["player2_id"]
    return (min(a, b), max(a, b))


def _dedup_playoff_legs(matches: list[dict]) -> list[dict]:
    """Drop phantom duplicate playoff rows.

    Older bot versions occasionally inserted a second row for the same
    (pair, stage, leg). Pick the most authoritative one:
      1. prefer ``status='confirmed'`` over anything else;
      2. then prefer ``status='reported'`` over ``'pending'``;
      3. finally tie-break by highest ``id`` (newest insert).
    This is robust both ways: a stale pending row preceding a newer
    confirmed insert AND a stale pending row inserted *after* the
    confirmed one (e.g. by an older buggy code path).
    """
    if not matches:
        return matches

    status_rank = {"confirmed": 3, "reported": 2, "pending": 1}

    def rank(m: dict) -> tuple[int, int]:
        return (status_rank.get(m.get("status") or "", 0), int(m.get("id") or 0))

    best: dict[tuple, dict] = {}
    for m in matches:
        # Skip rejected matches entirely — they should not block
        # series resolution or count as "unfinished legs".
        if (m.get("status") or "") == "rejected":
            continue
        a, b = m["player1_id"], m["player2_id"]
        key = (min(a, b), max(a, b), m.get("stage"), int(m.get("leg") or 1))
        cur = best.get(key)
        if cur is None or rank(m) > rank(cur):
            best[key] = m
    return list(best.values())


def _initial_legs_for_stage(stage_cfg: dict) -> int:
    """How many legs to spawn up front when a playoff stage starts.

    ``"wins"`` mode → minimum-to-decide ``(N+1)//2`` so an early sweep
    closes the series without an unused leg row sitting around.
    ``"goals"`` mode → all ``N`` legs because aggregate needs the full
    set to be definitive.
    """
    n = max(1, int(stage_cfg.get("len") or 1))
    if (stage_cfg.get("mode") or "goals").lower() == "wins":
        return (n + 1) // 2
    return n


def _third_place_complete(tid: int, tournament: dict | None = None) -> bool | None:
    """State of the optional 3rd-place match for ``tid``.

    Returns:
      * ``None``  — there is no 3rd-place fixture for this tournament
        (either it was never spawned or 3rd-place is disabled);
      * ``True``  — every leg of the 3rd-place series is confirmed;
      * ``False`` — at least one leg is still pending/reported.
    """
    rows = _dedup_playoff_legs(
        get_tournament_matches(tid, stage=THIRD_PLACE_STAGE)
    )
    if not rows:
        return None
    tournament = tournament or get_tournament(tid) or {}
    cfg = get_stage_config(tournament, THIRD_PLACE_STAGE)
    if any(m["status"] != "confirmed" for m in rows):
        return False
    w = _resolve_pair_winner(
        sorted(rows, key=lambda x: x.get("leg") or 1),
        advance_mode=cfg["mode"],
        series_len=cfg["len"],
    )
    return w is not None


def _maybe_spawn_third_place_match(
    tid: int, tournament: dict, sf_pairs_decided: list[tuple[int, int]],
) -> None:
    """Spawn the optional 3rd-place fixture between the two SF losers.

    ``sf_pairs_decided`` is a list of ``(winner_id, loser_id)`` for
    every SF pair, in iteration order. A pair where the "loser" was
    the byéd phantom side (player1_id == player2_id) is excluded by
    the caller.

    No-ops when:
      * the tournament has 3rd-place disabled
        (``playoff_third_place == 0``);
      * there aren't exactly two SF pairs with real losers (a bracket
        with a single SF or a SF bye can't produce a bronze match);
      * a 3rd-place row already exists (idempotency).
    """
    if not int(tournament.get("playoff_third_place") or 0):
        return
    if len(sf_pairs_decided) != 2:
        return
    losers = [l for _, l in sf_pairs_decided if l is not None]
    if len(losers) != 2:
        return
    existing = _dedup_playoff_legs(
        get_tournament_matches(tid, stage=THIRD_PLACE_STAGE)
    )
    if existing:
        return
    cfg = get_stage_config(tournament, THIRD_PLACE_STAGE)
    initial_legs = _initial_legs_for_stage(cfg)
    deadline = datetime.utcnow() + timedelta(hours=MATCH_DEADLINE_HOURS)
    dl_str = deadline.strftime("%Y-%m-%d %H:%M:%S")
    for leg in range(1, initial_legs + 1):
        if leg % 2 == 1:
            a, b = losers[0], losers[1]
        else:
            a, b = losers[1], losers[0]
        create_match(
            tid, a, b, stage=THIRD_PLACE_STAGE, deadline=dl_str, leg=leg,
        )


def advance_playoff(tid: int) -> str | None:
    """
    Check if current playoff round is done, generate next round.

    Per-stage config (``tournaments.playoff_stage_config``) controls:
      * ``len``: max number of legs (``bo1``/``bo3``/``bo5``/…);
      * ``mode``: ``"wins"`` for first-to-majority with early stop, or
        ``"goals"`` for aggregate-decides across all legs.
    Stages without a per-stage entry fall back to the tournament-wide
    ``playoff_matches_per_pair`` / ``playoff_advance_mode`` defaults.

    If a series is tied after all configured legs have been played, an
    extra tiebreaker leg is spawned (and another, and another) until
    someone wins — same behaviour as before.

    When ``tournaments.playoff_third_place = 1`` and the semifinals
    produced two non-bye pairs, a 3rd-place fixture is created between
    the SF losers in parallel with the final. The tournament does not
    flip to ``stage='finished'`` until BOTH the final and the bronze
    match are confirmed.

    Returns stage name if advanced, None if not ready / tournament finished.
    """
    tournament = get_tournament(tid)
    if tournament["stage"] != "playoff":
        return None

    # The 3rd-place fixture follows the same "spawn extra legs on tie"
    # rules as any other stage, but it never feeds a next stage. Run
    # that bookkeeping first so a tiebreaker leg is created if needed.
    _process_third_place_extra_legs(tid, tournament)

    for s in PLAYOFF_STAGES:
        matches = _dedup_playoff_legs(get_tournament_matches(tid, stage=s))
        if not matches:
            continue

        stage_cfg = get_stage_config(tournament, s)
        legs_target = stage_cfg["len"]
        adv_mode = stage_cfg["mode"]

        # Group matches by pair.
        pairs: dict[tuple[int, int], list[dict]] = {}
        for m in matches:
            pairs.setdefault(_pair_key(m), []).append(m)

        # First pass: for each pair where every existing leg is
        # confirmed, decide whether to spawn another leg. Spawn when the
        # series isn't decided AND we either haven't reached
        # ``legs_target`` yet, or we have but the aggregate is still
        # tied (tiebreaker extension).
        scheduled_extra = False
        for key, ms in pairs.items():
            ms_sorted = sorted(ms, key=lambda x: x.get("leg") or 1)
            confirmed_ms = [m for m in ms_sorted if m["status"] == "confirmed"]
            if len(confirmed_ms) < len(ms_sorted):
                continue  # this pair still has unfinished legs

            w = _resolve_pair_winner(
                ms_sorted, advance_mode=adv_mode, series_len=legs_target,
            )
            if w is not None:
                # Series decided — early-stop "wins" or full N-leg
                # aggregate. Nothing more to spawn.
                continue

            played = len(ms_sorted)
            a_id = ms_sorted[0]["player1_id"]
            b_id = ms_sorted[0]["player2_id"]
            deadline = datetime.utcnow() + timedelta(hours=MATCH_DEADLINE_HOURS)
            dl_str = deadline.strftime("%Y-%m-%d %H:%M:%S")
            create_match(tid, a_id, b_id, stage=s, deadline=dl_str,
                         leg=played + 1)
            scheduled_extra = True

        if scheduled_extra:
            # Extra legs spawned for a tied series in this stage. Normally
            # we'd stop here — but if a later stage already has matches
            # (partially spawned bracket), keep going to check it.
            idx = PLAYOFF_STAGES.index(s)
            if idx + 1 < len(PLAYOFF_STAGES):
                later_has_matches = any(
                    _dedup_playoff_legs(get_tournament_matches(tid, stage=ls))
                    for ls in PLAYOFF_STAGES[idx + 1:]
                )
                if later_has_matches:
                    continue
            return None

        # Second pass: resolve each pair independently and build a
        # winners list (None for unresolved pairs). This allows
        # incremental creation of next-stage matches as soon as two
        # adjacent winners are both known, without waiting for the
        # entire stage to finish.
        winners: list[int | None] = []
        pair_outcomes: list[tuple[int, int | None] | None] = []
        for key, ms in pairs.items():
            ms_sorted = sorted(ms, key=lambda x: x.get("leg") or 1)
            a_id = ms_sorted[0]["player1_id"]
            b_id = ms_sorted[0]["player2_id"]
            if a_id == b_id:
                # Bye row — winner = byed seed, no real loser.
                winners.append(a_id)
                pair_outcomes.append((a_id, None))
                continue
            w = _resolve_pair_winner(
                ms_sorted, advance_mode=adv_mode, series_len=legs_target,
            )
            if w is None:
                winners.append(None)
                pair_outcomes.append(None)
                continue
            winners.append(w)
            loser = a_id if w == b_id else b_id
            pair_outcomes.append((w, loser))

        all_done = all(w is not None for w in winners)

        # Spawn the 3rd-place fixture once ALL SF pairs are decided.
        if all_done and s == "sf":
            _maybe_spawn_third_place_match(tid, tournament,
                                           [po for po in pair_outcomes if po is not None])

        # Determine next stage
        idx = PLAYOFF_STAGES.index(s)
        if idx + 1 >= len(PLAYOFF_STAGES):
            # Final stage — can only finish when all done.
            if not all_done:
                return None
            # Final is done -> finish tournament, but only when the
            # optional 3rd-place fixture (if any) has also been played
            # out. Otherwise we'd close the bracket while the bronze
            # match is still pending in the bound chat.
            third_state = _third_place_complete(tid, tournament)
            if third_state is False:
                return None
            update_tournament(tid, stage="finished")
            return "finished"

        next_stage = PLAYOFF_STAGES[idx + 1]

        # Fetch existing next-stage matches for dedup.
        next_matches = _dedup_playoff_legs(
            get_tournament_matches(tid, stage=next_stage)
        )
        next_pairs: dict[tuple[int, int], list[dict]] = {}
        for m in next_matches:
            next_pairs.setdefault(_pair_key(m), []).append(m)

        # Create next-stage matches incrementally: for each adjacent
        # winner pair where BOTH are resolved, create the match if it
        # does not already exist.
        next_cfg = get_stage_config(tournament, next_stage)
        initial_legs = _initial_legs_for_stage(next_cfg)
        created_any = False

        deadline = datetime.utcnow() + timedelta(hours=MATCH_DEADLINE_HOURS)
        dl_str = deadline.strftime("%Y-%m-%d %H:%M:%S")
        for i in range(0, len(winners), 2):
            if i + 1 >= len(winners):
                break
            a, b = winners[i], winners[i + 1]
            if a is None or b is None:
                continue
            pair_k = (min(a, b), max(a, b))
            if pair_k in next_pairs:
                continue  # already exists
            for leg in range(1, initial_legs + 1):
                if leg % 2 == 1:
                    create_match(tid, a, b, stage=next_stage,
                                 deadline=dl_str, leg=leg)
                else:
                    create_match(tid, b, a, stage=next_stage,
                                 deadline=dl_str, leg=leg)
            created_any = True

        if created_any:
            return next_stage

        # Nothing was created. If stage is not fully done, check if a
        # later stage already has matches; if so, continue the loop to
        # inspect it, otherwise return None.
        if not all_done:
            if idx + 1 < len(PLAYOFF_STAGES):
                later_has_matches = any(
                    _dedup_playoff_legs(get_tournament_matches(tid, stage=ls))
                    for ls in PLAYOFF_STAGES[idx + 1:]
                )
                if later_has_matches:
                    continue
            return None

        # all_done but nothing new to create means next stage is fully
        # populated already — move on to check the next stage.
        continue

    return None


def _process_third_place_extra_legs(tid: int, tournament: dict) -> None:
    """Mirror the per-pair tiebreaker spawn for the 3rd-place fixture.

    The bronze match is not part of the linear ``PLAYOFF_STAGES`` walk,
    so the main ``advance_playoff`` loop never visits it. We replicate
    just enough of that loop here: if every leg of the 3rd-place series
    is confirmed but the aggregate is still tied and we haven't reached
    ``legs_target`` yet — OR we have but it's still tied — spawn one
    more leg with the deadline reset.
    """
    rows = _dedup_playoff_legs(
        get_tournament_matches(tid, stage=THIRD_PLACE_STAGE)
    )
    if not rows:
        return
    cfg = get_stage_config(tournament, THIRD_PLACE_STAGE)
    ms_sorted = sorted(rows, key=lambda x: x.get("leg") or 1)
    confirmed_ms = [m for m in ms_sorted if m["status"] == "confirmed"]
    if len(confirmed_ms) < len(ms_sorted):
        return  # still has unfinished legs
    w = _resolve_pair_winner(
        ms_sorted, advance_mode=cfg["mode"], series_len=cfg["len"],
    )
    if w is not None:
        return  # series decided
    played = len(ms_sorted)
    a_id = ms_sorted[0]["player1_id"]
    b_id = ms_sorted[0]["player2_id"]
    deadline = datetime.utcnow() + timedelta(hours=MATCH_DEADLINE_HOURS)
    dl_str = deadline.strftime("%Y-%m-%d %H:%M:%S")
    create_match(
        tid, a_id, b_id, stage=THIRD_PLACE_STAGE, deadline=dl_str,
        leg=played + 1,
    )


def get_tournament_podium(tid: int) -> dict:
    """Return the playoff podium for ``tid`` as a dict with optional
    ``first``/``second``/``third``/``fourth`` keys (each a player_id).

    Resolution rules:

    * **1st / 2nd:** taken from the Final pair when every leg of the
      final is confirmed. ``first`` is the series winner;
      ``second`` is the runner-up. If the final is unresolved or has
      no rows, both keys are omitted.
    * **3rd / 4th:** taken from the 3rd-place fixture when present and
      fully confirmed (winner = bronze, loser = 4th). If the bronze
      match is absent (feature disabled or never spawned), the two
      semifinal losers are jointly reported under ``"third_tied"``
      as a list of player_ids — there is no way to break the tie
      without a played bronze match. The ``third_tied`` key is only
      present when ``third`` is absent.
    * Stages still pending → keys omitted. Caller can render whatever
      they have (e.g. just 1st/2nd if the bronze is still being played).

    Used by ``cb_finish_tournament`` and ``_announce_stage_advance`` to
    build the "🏆 турнир завершён" итог-сообщение.
    """
    t = get_tournament(tid)
    if not t:
        return {}

    podium: dict = {}

    # ── Final → 1st / 2nd ────────────────────────────────────────────
    fin_rows = _dedup_playoff_legs(
        get_tournament_matches(tid, stage="final")
    )
    if fin_rows:
        fin_sorted = sorted(fin_rows, key=lambda x: x.get("leg") or 1)
        all_done = all(
            (m.get("status") or "") == "confirmed" for m in fin_sorted
        )
        if all_done:
            cfg = get_stage_config(t, "final")
            w = _resolve_pair_winner(
                fin_sorted, advance_mode=cfg["mode"], series_len=cfg["len"],
            )
            if w is not None:
                a_id = fin_sorted[0]["player1_id"]
                b_id = fin_sorted[0]["player2_id"]
                podium["first"] = w
                podium["second"] = b_id if w == a_id else a_id

    # ── Bronze → 3rd / 4th ───────────────────────────────────────────
    bronze_rows = _dedup_playoff_legs(
        get_tournament_matches(tid, stage=THIRD_PLACE_STAGE)
    )
    if bronze_rows:
        bronze_sorted = sorted(bronze_rows, key=lambda x: x.get("leg") or 1)
        all_done = all(
            (m.get("status") or "") == "confirmed" for m in bronze_sorted
        )
        if all_done:
            cfg = get_stage_config(t, THIRD_PLACE_STAGE)
            w = _resolve_pair_winner(
                bronze_sorted, advance_mode=cfg["mode"], series_len=cfg["len"],
            )
            if w is not None:
                a_id = bronze_sorted[0]["player1_id"]
                b_id = bronze_sorted[0]["player2_id"]
                podium["third"] = w
                podium["fourth"] = b_id if w == a_id else a_id
    else:
        # No bronze rows ever spawned — semifinal losers are tied for
        # 3rd. Compute them so the итог can show "@a / @b — 3-е место".
        sf_rows = _dedup_playoff_legs(
            get_tournament_matches(tid, stage="sf")
        )
        sf_losers: list[int] = []
        pairs_seen: set[tuple[int, int]] = set()
        for m in sorted(sf_rows, key=lambda x: x.get("leg") or 1):
            key = _pair_key(m)
            if key in pairs_seen:
                continue
            pair_ms = [r for r in sf_rows if _pair_key(r) == key]
            pair_sorted = sorted(pair_ms, key=lambda x: x.get("leg") or 1)
            if not all(
                (r.get("status") or "") == "confirmed" for r in pair_sorted
            ):
                continue
            cfg = get_stage_config(t, "sf")
            w = _resolve_pair_winner(
                pair_sorted, advance_mode=cfg["mode"], series_len=cfg["len"],
            )
            if w is None:
                continue
            pairs_seen.add(key)
            a_id, b_id = key
            sf_losers.append(b_id if w == a_id else a_id)
        if sf_losers:
            podium["third_tied"] = sf_losers

    return podium


# ── Tours (rounds) ─────────────────────────────────────────────────────────────


def circle_method_schedule(player_ids: list[int], mpp: int = 1) -> list[list[tuple[int, int, int]]]:
    """
    Generate a round-robin schedule using the circle method.

    Returns a list of tours, where each tour is a list of
    ``(player1_id, player2_id, leg)`` tuples.

    - N even → N-1 tours, each player plays once per tour
    - N odd  → N tours, one player sits out each tour (bye)
    - ``mpp=1`` → single round-robin (each pair once)
    - ``mpp=2`` → double round-robin (second circle with swapped sides)
    """
    ids = list(player_ids)
    n = len(ids)

    # If odd, add a sentinel (None) to make it even
    if n % 2 == 1:
        ids.append(None)
        n += 1

    fixed = ids[0]
    rotating = ids[1:]

    rounds: list[list[tuple[int, int, int]]] = []

    def _one_circle(base_ids: list, leg_num: int = 1) -> list[list[tuple[int, int, int]]]:
        """Single round-robin circle, returns list of tour match lists."""
        m = len(base_ids)
        fixed_p = base_ids[0]
        rot = list(base_ids[1:])
        tours = []
        for _ in range(m - 1):
            tour = []
            # Pair fixed with rot[0], then rot[1] with rot[-1], rot[2] with rot[-2], ...
            for i in range(m // 2):
                a = fixed_p if i == 0 else rot[i - 1]
                b = rot[m - 2 - i] if i > 0 else rot[m - 2]
                if a is not None and b is not None:
                    tour.append((a, b, leg_num))
            tours.append(tour)
            # Rotate: keep fixed, move rot[-1] to rot[1] position
            if len(rot) > 1:
                rot = [rot[-1]] + rot[:-1]
        return tours

    schedule = _one_circle(ids, leg_num=1)

    if mpp >= 2:
        # Second circle: re-init ids (same fixed player) but swap sides
        ids2 = list(player_ids)
        if len(ids2) % 2 == 1:
            ids2.append(None)
        second = _one_circle(ids2, leg_num=2)
        # Swap home/away in second circle
        for tour in second:
            swapped = []
            for a, b, leg in tour:
                swapped.append((b, a, leg))
            tour[:] = swapped
        schedule.extend(second)

    return schedule


def _played_pair_counts(tid: int) -> dict[frozenset, int]:
    """How many times each pair of players has already been scheduled in
    this tournament's group stage (any status). Keyed by ``frozenset({a, b})``.

    This is the "memory" the tour generator needs so it never re-creates a
    pairing that already exists, regardless of how the roster changed
    between tours.
    """
    conn = get_conn()
    rows = conn.execute(
        "SELECT player1_id, player2_id FROM matches "
        "WHERE tournament_id=? AND stage='group'",
        (tid,),
    ).fetchall()
    conn.close()
    counts: dict[frozenset, int] = {}
    for r in rows:
        try:
            a, b = r["player1_id"], r["player2_id"]
        except (KeyError, TypeError, IndexError):
            a, b = r[0], r[1]
        if a is None or b is None:
            continue
        key = frozenset((a, b))
        counts[key] = counts.get(key, 0) + 1
    return counts


def _try_match_pairs(
    pids: list[int],
    pair_counts: dict[frozenset, int],
    cap: int,
) -> list[tuple[int, int]] | None:
    """One matching attempt with each pair limited to ``cap`` total meetings.

    Returns the list of ``(a, b)`` pairs (one per slot, the bye drops out
    if the count was odd), or ``None`` if no perfect matching exists at
    this cap.
    """
    players: list = list(pids)
    if len(players) % 2 == 1:
        players.append(None)

    def allowed(a, b) -> bool:
        if a is None or b is None:
            return True
        return pair_counts.get(frozenset((a, b)), 0) < cap

    def match(remaining: list):
        if not remaining:
            return []
        first = min(
            remaining,
            key=lambda x: sum(1 for y in remaining if y != x and allowed(x, y)),
        )
        rest = [p for p in remaining if p != first]
        cands = [y for y in rest if allowed(first, y)]
        cands.sort(
            key=lambda y: sum(1 for z in rest if z != y and allowed(y, z))
        )
        for partner in cands:
            sub = [p for p in rest if p != partner]
            res = match(sub)
            if res is not None:
                return [(first, partner)] + res
        return None

    pairing = match(players)
    if pairing is None:
        # Random shuffles as a safety net for tightly constrained graphs
        # where the deterministic heuristic can dead-end.
        rng = random.Random(0xC0FFEE)
        for _ in range(8):
            shuffled = list(players)
            rng.shuffle(shuffled)
            pairing = match(shuffled)
            if pairing is not None:
                break
    if pairing is None:
        return None
    return [(a, b) for (a, b) in pairing if a is not None and b is not None]


def _build_repeat_free_tour(
    pids: list[int],
    pair_counts: dict[frozenset, int],
    mpp: int,
) -> tuple[list[tuple[int, int]] | None, int]:
    """Build a single tour, preferring a repeat-free matching but falling
    back to higher caps if no perfect repeat-free matching exists.

    Returns ``(pairs, cap_used)``. ``cap_used == mpp`` means the result is
    fully repeat-free for the configured matches-per-pair. ``cap_used >
    mpp`` indicates we had to relax — some pairs in this tour are already
    at or above their normal quota. ``pairs is None`` means no full pairing
    is possible at any cap (e.g., fewer than two players).
    """
    if len(pids) < 2:
        return None, mpp

    # Try the strict cap first, then relax one step at a time. The relax
    # path is only ever reached when the data has gotten into a state
    # where the strict round-robin can't be completed (orphan match rows,
    # eliminated-player history, leftover duplicates from earlier bugs,
    # etc.). Better to schedule a slightly imperfect tour than to leave
    # the league stuck.
    max_cap = mpp + 8
    cap = mpp
    while cap <= max_cap:
        result = _try_match_pairs(pids, pair_counts, cap)
        if result is not None:
            return result, cap
        cap += 1
    return None, cap


def _regularize_residual(
    pids: list[int],
    pair_counts: dict[frozenset, int],
    target_degree: int,
    mpp: int = 1,
) -> list[tuple[int, int]] | None:
    """Make the residual graph ``target_degree``-regular on ``pids`` by
    picking a few pairs that the remaining schedule will simply not play.

    A round-robin tail of ``T`` tours can be 1-factorized only if every
    vertex has *exactly* ``T`` legal partners left. Tournaments where
    early tours had ghost matches (a real player paired with a guest who
    isn't in the active roster) leave some vertices with one or more
    extra free partners — making the residual graph irregular and
    breaking 1-factorization.

    This helper finds a b-matching on the excess-vertex subgraph using
    backtracking: for each vertex ``v`` with excess ``k``, we add ``k``
    edges from ``v`` to other excess vertices, decrementing their
    excesses too. Net effect: those pairs **never** play in the upcoming
    schedule, but in exchange every other pair fits cleanly with no
    repeats.

    Returns the list of pairs that were marked synthetic, or ``None`` if
    no valid b-matching exists on the excess subgraph (rare — would mean
    high-excess players already met each other in every legal way). The
    caller should fall back to greedy in that case.
    """
    def degree(v: int) -> int:
        return sum(
            1 for q in pids
            if q != v and pair_counts.get(frozenset((v, q)), 0) < mpp
        )

    excess: dict[int, int] = {}
    for p in pids:
        d = degree(p)
        if d > target_degree:
            excess[p] = d - target_degree

    if not excess:
        return []  # already regular

    # Try to find a b-matching on the excess subgraph using backtracking.
    # State: a copy of `excess` we mutate. Pick the player with the
    # highest residual excess, find a partner with excess > 0 and a
    # legal residual edge; recurse.
    chosen: list[tuple[int, int]] = []

    def can_pair(a: int, b: int) -> bool:
        return pair_counts.get(frozenset((a, b)), 0) < mpp

    def solve(state: dict[int, int]) -> bool:
        # Trim zeros
        active = {p: k for p, k in state.items() if k > 0}
        if not active:
            return True
        # Pick the most-constrained player (highest excess; tiebreak by
        # smallest id so the search is deterministic).
        v = max(active, key=lambda x: (active[x], -x))
        # Candidate partners: other players with excess > 0 connected by
        # a legal residual edge. Try the highest-excess partner first.
        partners = sorted(
            (u for u in active if u != v and can_pair(v, u)),
            key=lambda u: (active[u], -u),
            reverse=True,
        )
        for u in partners:
            new_state = dict(state)
            new_state[v] -= 1
            new_state[u] -= 1
            chosen.append((v, u))
            # Tentatively bump pair_counts so subsequent can_pair calls
            # see the synthetic edge.
            key = frozenset((v, u))
            pair_counts[key] = pair_counts.get(key, 0) + 1
            if solve(new_state):
                return True
            # Undo
            chosen.pop()
            pair_counts[key] -= 1
            if pair_counts[key] == 0:
                del pair_counts[key]
        return False

    if not solve(dict(excess)):
        return None
    return list(chosen)


def _solve_full_schedule(
    pids: list[int],
    num_tours: int,
    pair_counts: dict[frozenset, int],
    mpp: int = 1,
    max_seconds: float = 12.0,
) -> list[list[tuple[int, int]]] | None:
    """Find ``num_tours`` perfect matchings on ``pids`` such that no pair
    ever exceeds ``mpp`` meetings across all matchings combined with the
    history in ``pair_counts``.

    Strategy: **iterative matching with random-seed restarts** — no
    cross-tour backtracking.

    For each restart:
      1. Iterate ``tour = 0 .. num_tours-1``.
      2. At each tour, find a perfect matching of the residual via DFS
         with a "most-constrained-first" heuristic and randomized
         tie-breaking on the candidate-partner ordering.
      3. If at some tour no matching exists, abandon the attempt and
         restart from scratch with a fresh seed.
      4. If ``num_tours`` matchings are produced, return success.

    The previous implementation was a cross-tour backtracker. Two
    practical issues with that approach on ~32-vertex residuals:

      • The branching factor (≈ 13 matchings per tour) is small but the
        search depth is the tour count (~23 here), so the worst-case
        tree explodes.
      • The random shuffles inside the per-tour matcher were reseeded
        from a *fixed* constant on every recursion call, so every level
        of the tree explored the same 13 candidate orderings — defeating
        the diversification the shuffles were meant to provide.

    Restart-based randomized search converges in practice for our case:
    after ``_regularize_residual`` makes the residual ``num_tours``-
    regular, the 1-factorization conjecture (Csaba-Kühn-Lo-Osthus-
    Treglown 2014: every k-regular graph on 2n vertices with k ≥ n is
    1-factorizable) guarantees a factorization exists. A randomized
    iterative matcher hits one within a handful of restarts on graphs
    of this size; if it does fail it is the regularization itself that
    needs a wider skip set, not the per-tour matcher.

    Returns the schedule (list of perfect matchings) on success, or
    ``None`` if no factorization was found within ``max_seconds``.
    Caller falls back to greedy + relax in that case.
    """
    import time

    start = time.monotonic()
    real_pids = list(pids)
    has_bye = (len(real_pids) % 2 == 1)
    n_real = len(real_pids)

    def find_one_matching(
        pc: dict, rng: random.Random
    ) -> list[tuple] | None:
        """Single perfect-matching attempt against the current ``pc``.

        Most-constrained-vertex first, randomized tie-break on the
        candidate-partner ordering. Returns the matching as a list of
        ``(a, b)`` pairs (the bye, if present, gets its ``None`` partner
        in the result and is filtered out by the caller).
        """
        # Local closures to avoid recomputing ``frozenset`` allocations
        # on the hot path.
        get = pc.get
        cap = mpp

        def allowed(a, b) -> bool:
            if a is None or b is None:
                return True
            return get(frozenset((a, b)), 0) < cap

        def free_count(x, remaining):
            c = 0
            for y in remaining:
                if y != x and allowed(x, y):
                    c += 1
            return c

        def match(remaining: list):
            if not remaining:
                return []
            # Pick the most-constrained vertex first; ties broken by
            # the rng so different restarts explore different subtrees.
            best = None
            best_count = n_real + 2
            for x in remaining:
                c = free_count(x, remaining)
                if c < best_count or (c == best_count and rng.random() < 0.5):
                    best = x
                    best_count = c
            first = best
            if best_count == 0:
                return None
            rest = [p for p in remaining if p != first]
            cands = [y for y in rest if allowed(first, y)]
            # Sort candidates by their own free-count (most-constrained
            # first), with random tie-break.
            cands.sort(
                key=lambda y: (free_count(y, rest), rng.random())
            )
            for partner in cands:
                sub = [p for p in rest if p != partner]
                res = match(sub)
                if res is not None:
                    return [(first, partner)] + res
            return None

        order = list(real_pids)
        if has_bye:
            order.append(None)
        return match(order)

    attempts = 0
    fail_at_tour: list[int] = []  # debug: which tour killed each attempt
    seed = 0
    while time.monotonic() - start < max_seconds:
        attempts += 1
        rng = random.Random(seed)
        seed += 1
        pc = dict(pair_counts)
        schedule: list[list[tuple[int, int]]] = []
        success = True
        for tour_idx in range(num_tours):
            m = find_one_matching(pc, rng)
            if m is None:
                fail_at_tour.append(tour_idx + 1)
                success = False
                break
            real_pairs = [
                (a, b) for a, b in m if a is not None and b is not None
            ]
            schedule.append(real_pairs)
            for a, b in real_pairs:
                key = frozenset((a, b))
                pc[key] = pc.get(key, 0) + 1
        if success:
            log.info(
                "_solve_full_schedule: factorization found on attempt %s "
                "(elapsed=%.2fs, n=%s, tours=%s)",
                attempts, time.monotonic() - start, n_real, num_tours,
            )
            return schedule

    log.warning(
        "_solve_full_schedule: timed out after %s attempts in %.2fs "
        "(n=%s, tours=%s); fail-at-tour distribution (last 20): %s",
        attempts, time.monotonic() - start, n_real, num_tours,
        fail_at_tour[-20:],
    )
    return None


def generate_next_tour(tid: int) -> list[int]:
    """
    Create matches for the next tour of a league-format tournament.

    The pairing is built so it **never repeats** a fixture that already
    exists in the tournament (see ``_build_repeat_free_tour``). This is
    deliberately not the old purely-positional circle method: that approach
    reshuffled every player's slot whenever the roster changed between tours
    (late joins, drop-outs, eliminations), which caused already-played
    pairings to reappear in later tours.

    - Determines the next tour number
    - Builds a repeat-free pairing for the current, non-eliminated roster
    - Creates match rows with ``tour_number`` set
    - Records the tour in ``tournament_tours``
    - Updates ``current_tour`` on the tournament row

    Returns list of created match ids.
    """
    t = get_tournament(tid)
    if not t:
        return []

    players = get_tournament_players(tid)
    # Deterministic ordering; correctness no longer depends on it because
    # repeats are prevented explicitly, but a stable order keeps the
    # generated schedule tidy.
    pids = sorted(
        [p["player_id"] for p in players if not p.get("eliminated")]
    )
    if len(pids) < 2:
        return []

    mpp = max(1, int(t.get("group_matches_per_pair") or 1))
    total_tours = int(t.get("total_tours") or 0)

    n = len(pids)
    # Theoretical maximum number of tours for a (double) round-robin.
    max_tours = (n - 1 if n % 2 == 0 else n) * mpp

    # Determine next tour number (1-based)
    next_tour = get_next_tour_number(tid)

    # If total_tours is auto (0) or larger than possible, cap at the max.
    if total_tours == 0 or total_tours > max_tours:
        total_tours = max_tours
        # Persist so the UI shows the correct number
        update_tournament(tid, total_tours=total_tours)

    if next_tour > total_tours:
        return []

    # Build a pairing that avoids every fixture already played in this
    # tournament (up to ``mpp`` meetings per pair).
    pair_counts = _played_pair_counts(tid)
    log.info(
        "generate_next_tour tid=%s next_tour=%s n_players=%s "
        "total_tours=%s max_tours=%s pair_count_entries=%s",
        tid, next_tour, n, total_tours, max_tours, len(pair_counts),
    )
    tour_pairs, cap_used = _build_repeat_free_tour(pids, pair_counts, mpp)
    if not tour_pairs:
        # Last-ditch diagnostic: report each player's free-partner count
        # so the logs make it obvious whether the matcher genuinely had
        # no options or hit some other edge case.
        free = {
            p: sum(
                1 for q in pids
                if q != p and pair_counts.get(frozenset((p, q)), 0) < mpp
            )
            for p in pids
        }
        log.warning(
            "generate_next_tour tid=%s: matcher returned no pairing "
            "(mpp=%s, players=%s, min_free=%s, players_with_zero_free=%s)",
            tid, mpp, n, min(free.values()) if free else None,
            sum(1 for v in free.values() if v == 0),
        )
        return []
    if cap_used > mpp:
        log.warning(
            "generate_next_tour tid=%s: had to relax mpp from %s to %s "
            "to find a complete tour — some pairs may repeat",
            tid, mpp, cap_used,
        )

    deadline = (datetime.utcnow() + timedelta(hours=MATCH_DEADLINE_HOURS)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )

    mids = []
    for a, b in tour_pairs:
        # leg = how many times this pair has met so far + 1
        leg = pair_counts.get(frozenset((a, b)), 0) + 1
        # On even legs swap home/away so it reads as the "return" fixture.
        if leg % 2 == 0:
            a, b = b, a
        mid = create_match(
            tid, a, b,
            stage="group",
            round_num=1,
            deadline=deadline,
            leg=leg,
        )
        # Set tour_number after creation
        conn = get_conn()
        conn.execute("UPDATE matches SET tour_number=? WHERE id=?", (next_tour, mid))
        conn.commit()
        conn.close()
        mids.append(mid)

    # Record the tour
    create_tournament_tour(tid, next_tour)
    set_current_tour(tid, next_tour)

    return mids


def regenerate_unplayed_tours(tid: int) -> dict:
    """Wipe every not-yet-played tour and dedupe orphan match rows so
    the league can pick up cleanly.

    Two passes happen here:

    1. **Dedupe.** Old buggy code, late-add-player flows, or partial
       runs of earlier regen attempts can leave multiple rows in
       ``matches`` for the same pair of players (e.g. one ``confirmed``
       and several ``pending``). These duplicates inflate the
       pair-counts the round-robin matcher reads and force it into the
       relax fallback, which then schedules even more duplicates. Per
       pair we keep all confirmed rows + at most one pending row
       (smallest id wins); everything else is deleted.
    2. **Trim tail.** Tours that already have at least one confirmed
       result are kept; matches and tour rows beyond ``last_played``
       are dropped, and ``current_tour`` is reset to ``last_played``.

    The next press of the "Создать матчи следующего тура" button will
    then build a fresh repeat-free tour over a clean schedule.

    Returns a summary dict::

        {
            "kept_through":    <last tour with a confirmed result>,
            "removed_tours":   <int>,
            "removed_matches": <int>,  # from the tail
            "removed_dupes":   <int>,  # extra pending rows for already-scheduled pairs
            "next_tour":       <int>,  # number of the tour the next click will create
        }
    """
    t = get_tournament(tid)
    if not t:
        return {"error": "not_found"}
    if not int(t.get("tours_enabled") or 0):
        return {"error": "tours_disabled"}

    def _scalar(row):
        if row is None:
            return 0
        try:
            return int(row[0])
        except (KeyError, TypeError, IndexError):
            try:
                return int(list(row.values())[0])
            except Exception:
                return 0

    conn = get_conn()
    # Highest tour number that contains at least one confirmed match.
    last_played = _scalar(
        conn.execute(
            "SELECT COALESCE(MAX(tour_number), 0) FROM matches "
            "WHERE tournament_id=? AND status='confirmed'",
            (tid,),
        ).fetchone()
    )

    # ── Pass 1: dedupe ───────────────────────────────────────────────
    # Walk every group-stage match in this tournament, group by the
    # unordered pair of players, and figure out which rows are
    # redundant. Confirmed rows are sacred; pending rows for a pair
    # that already has a confirmed match are deleted; if a pair has
    # only pending rows we keep the one with the smallest id and drop
    # the rest.
    rows = conn.execute(
        "SELECT id, player1_id, player2_id, status "
        "FROM matches WHERE tournament_id=? AND stage='group' "
        "ORDER BY id",
        (tid,),
    ).fetchall()

    by_pair: dict[frozenset, dict] = {}
    for r in rows:
        try:
            mid = r["id"]
            p1 = r["player1_id"]
            p2 = r["player2_id"]
            status = r["status"]
        except (KeyError, TypeError, IndexError):
            mid, p1, p2, status = r[0], r[1], r[2], r[3]
        if p1 is None or p2 is None or p1 == p2:
            continue
        key = frozenset((p1, p2))
        bucket = by_pair.setdefault(key, {"confirmed": [], "pending": []})
        if status == "confirmed":
            bucket["confirmed"].append(mid)
        else:
            bucket["pending"].append(mid)

    dupe_ids: list[int] = []
    for bucket in by_pair.values():
        if bucket["confirmed"]:
            # Confirmed exists — drop every pending row for this pair.
            dupe_ids.extend(bucket["pending"])
        elif len(bucket["pending"]) > 1:
            # Multiple pending — keep first (smallest id), drop rest.
            dupe_ids.extend(bucket["pending"][1:])

    removed_dupes = 0
    if dupe_ids:
        # Delete in chunks so very long IN clauses don't blow up.
        for i in range(0, len(dupe_ids), 500):
            chunk = dupe_ids[i : i + 500]
            placeholders = ",".join(["?"] * len(chunk))
            conn.execute(
                f"DELETE FROM matches WHERE id IN ({placeholders})",
                chunk,
            )
        removed_dupes = len(dupe_ids)
        log.info(
            "regenerate_unplayed_tours tid=%s: removed %s duplicate "
            "match rows across %s pairs",
            tid, removed_dupes, len(by_pair),
        )

    # ── Pass 2: delete EVERY pending match ────────────────────────────
    # The user's preferred policy: keep all confirmed matches as-is,
    # delete every pending row (regardless of tour_number), and let the
    # re-planner fit the unplayed pairs back into the empty slots
    # across all tours. This is more aggressive than the previous
    # "trim past last_played" approach but it's much cleaner: stale
    # pending rows from earlier buggy generators don't survive into
    # the new schedule at all.
    removed_matches = _scalar(
        conn.execute(
            "SELECT COUNT(*) FROM matches "
            "WHERE tournament_id=? AND stage='group' "
            "AND status != 'confirmed'",
            (tid,),
        ).fetchone()
    )
    conn.execute(
        "DELETE FROM matches WHERE tournament_id=? AND stage='group' "
        "AND status != 'confirmed'",
        (tid,),
    )
    # Drop tournament_tours rows that no longer have any matches behind
    # them. Tours with a confirmed match keep their row so existing tour
    # views (incl. /tourstext, /tours) stay intact.
    cur = conn.execute(
        "SELECT tt.tour_number FROM tournament_tours tt "
        "WHERE tt.tournament_id=? AND NOT EXISTS ("
        "  SELECT 1 FROM matches m WHERE m.tournament_id=tt.tournament_id "
        "  AND m.tour_number=tt.tour_number AND m.status='confirmed'"
        ")",
        (tid,),
    ).fetchall()
    empty_tour_nums: list[int] = []
    for r in cur:
        try:
            empty_tour_nums.append(int(r["tour_number"]))
        except (KeyError, TypeError, IndexError):
            empty_tour_nums.append(int(r[0]))
    removed_tours = len(empty_tour_nums)
    if empty_tour_nums:
        for i in range(0, len(empty_tour_nums), 500):
            chunk = empty_tour_nums[i : i + 500]
            placeholders = ",".join(["?"] * len(chunk))
            conn.execute(
                f"DELETE FROM tournament_tours "
                f"WHERE tournament_id=? AND tour_number IN ({placeholders})",
                [tid] + chunk,
            )
    conn.commit()

    # Pass 1.5 (orphan pending) is now subsumed by Pass 2 — every
    # non-confirmed row is gone regardless of tour_number — so we no
    # longer need a separate pass for it. Keep the field in the
    # response for backwards compatibility with the handler.
    removed_orphans = 0

    conn.close()

    # Reset the pointer so the next button press starts at last_played + 1.
    set_current_tour(tid, last_played)

    # ── Pass 3: re-plan the full schedule ────────────────────────────
    # Confirmed matches stay where they are (with their existing
    # tour_number). For each tour 1..total_tours we look at which
    # roster players are already committed by a confirmed match and
    # match the remaining "free" players using only pairs that haven't
    # played yet. The randomized iterative solver inside handles the
    # ghost-residual irregularity by skipping a minimal set of pairs
    # so a complete factorization fits.
    pre_filled_tours = 0
    pre_filled_matches = 0
    relax_used = 0
    skipped_pairs = 0
    t_now = get_tournament(tid)
    if t_now:
        try:
            pre_filled_tours, pre_filled_matches, relax_used, skipped_pairs = (
                _prefill_full_schedule(tid, t_now)
            )
        except Exception:
            log.exception(
                "regenerate_unplayed_tours tid=%s: full-schedule pre-fill "
                "crashed, falling back to remaining-tail pre-fill",
                tid,
            )
            try:
                pre_filled_tours, pre_filled_matches, relax_used, skipped_pairs = (
                    _prefill_remaining_tours(tid, t_now, last_played)
                )
            except Exception:
                log.exception(
                    "regenerate_unplayed_tours tid=%s: tail pre-fill "
                    "also crashed, leaving the schedule empty",
                    tid,
                )

    return {
        "kept_through": last_played,
        "removed_tours": removed_tours,
        "removed_matches": removed_matches,
        "removed_dupes": removed_dupes,
        "removed_orphans": removed_orphans,
        "next_tour": last_played + 1,
        "pre_filled_tours": pre_filled_tours,
        "pre_filled_matches": pre_filled_matches,
        "relax_used": relax_used,
        "skipped_pairs": skipped_pairs,
    }


def _prefill_remaining_tours(
    tid: int, t: dict, last_played: int
) -> tuple[int, int, int, int]:
    """After /regen_tours has cleaned the slate, build every remaining
    tour in one shot using the cross-tour solver.

    Returns ``(tours_created, matches_created, relax_used, skipped_pairs)``:

    - ``relax_used`` is 0 if the schedule is fully repeat-free and 1 if
      the solver had to fall back to the per-tour relax matcher because
      no clean 1-factorization fits the residual graph.
    - ``skipped_pairs`` is the count of pairs the regularization step
      explicitly chose not to schedule so the solver could find a
      perfect repeat-free factorization. Inevitable when early tours
      had ghost matches; those pairs simply will not play in the league.
    """
    players = get_tournament_players(tid)
    pids = sorted(
        [p["player_id"] for p in players if not p.get("eliminated")]
    )
    if len(pids) < 2:
        return 0, 0, 0, 0

    mpp = max(1, int(t.get("group_matches_per_pair") or 1))
    total_tours = int(t.get("total_tours") or 0)
    n = len(pids)
    max_tours = (n - 1 if n % 2 == 0 else n) * mpp
    if total_tours == 0 or total_tours > max_tours:
        total_tours = max_tours
        update_tournament(tid, total_tours=total_tours)

    remaining = total_tours - last_played
    if remaining <= 0:
        return 0, 0, 0, 0

    pair_counts = _played_pair_counts(tid)

    # ── Regularize the residual graph if needed ─────────────────────
    # If early tours had ghost matches, some players have an extra free
    # partner left. The strict 1-factorization solver can't handle that
    # without help — make the residual ``remaining``-regular by marking
    # a few pairs as 'won't play in the upcoming tours'.
    skipped_pairs = _regularize_residual(
        pids, pair_counts, target_degree=remaining, mpp=mpp,
    )
    if skipped_pairs is None:
        log.warning(
            "_prefill_remaining_tours tid=%s: residual graph couldn't be "
            "regularized — will fall back to greedy if solver fails",
            tid,
        )
        skipped_pairs = []
    elif skipped_pairs:
        log.info(
            "_prefill_remaining_tours tid=%s: marked %s pair(s) as "
            "'won't play' to make the residual %s-regular: %s",
            tid, len(skipped_pairs), remaining, skipped_pairs,
        )

    schedule: list[list[tuple[int, int]]] | None = _solve_full_schedule(
        pids, remaining, pair_counts, mpp=mpp, max_seconds=20.0,
    )
    relax_used = 0

    if schedule is None:
        # Solver couldn't produce a fully repeat-free schedule within
        # its time budget. Fall back to greedy + relax per tour so the
        # league still has a complete schedule, even if a few pairs
        # repeat at the very end.
        log.warning(
            "_prefill_remaining_tours tid=%s: global solver gave up, "
            "falling back to greedy+relax for %s tours",
            tid, remaining,
        )
        relax_used = 1
        schedule = []
        running = dict(pair_counts)
        for _ in range(remaining):
            tour_pairs, _cap = _build_repeat_free_tour(pids, running, mpp)
            if not tour_pairs:
                break
            schedule.append(tour_pairs)
            for a, b in tour_pairs:
                key = frozenset((a, b))
                running[key] = running.get(key, 0) + 1

    if not schedule:
        return 0, 0, relax_used, len(skipped_pairs)

    # Persist
    deadline = (
        datetime.utcnow() + timedelta(hours=MATCH_DEADLINE_HOURS)
    ).strftime("%Y-%m-%d %H:%M:%S")

    tours_created = 0
    matches_created = 0
    running = dict(pair_counts)
    for offset, pairs in enumerate(schedule, start=1):
        tour_no = last_played + offset
        for a, b in pairs:
            key = frozenset((a, b))
            leg = running.get(key, 0) + 1
            running[key] = leg
            if leg % 2 == 0:
                a, b = b, a
            mid = create_match(
                tid, a, b,
                stage="group",
                round_num=1,
                deadline=deadline,
                leg=leg,
            )
            conn = get_conn()
            conn.execute(
                "UPDATE matches SET tour_number=? WHERE id=?",
                (tour_no, mid),
            )
            conn.commit()
            conn.close()
            matches_created += 1
        create_tournament_tour(tid, tour_no)
        tours_created += 1
    set_current_tour(tid, last_played + tours_created)
    return tours_created, matches_created, relax_used, len(skipped_pairs)


def _prefill_full_schedule(
    tid: int, t: dict
) -> tuple[int, int, int, int]:
    """Re-plan the full league schedule after /regen_tours wipes every
    pending row.

    Confirmed matches stay where they are — both their pair and their
    ``tour_number``. For each tour ``T`` in ``1..total_tours`` we look at
    which roster players are committed by a confirmed match in ``T`` and
    schedule a matching of the remaining "free" players using only
    pairs that haven't played yet. Each restart of the iterative solver
    runs through every tour in order; if the result is incomplete we try
    a fresh seed.

    Compared to ``_prefill_remaining_tours`` (which only fills tours
    past ``last_played``):

    - Tours 1..last_played that lost pending matches in ``Pass 2`` get
      their open slots refilled, not just trimmed off.
    - Pair-counts come exclusively from confirmed matches, so historic
      ghost pairings still block re-pairing real-vs-ghost but don't
      poison the residual graph with leftover-pending entries.

    Returns ``(tours_created, matches_created, relax_used, skipped_pairs)``,
    same shape as ``_prefill_remaining_tours``. ``relax_used`` reports
    how many tours fell back to the relax matcher. ``skipped_pairs`` is
    the count of pairs that the solver could not place anywhere within
    the time budget — usually those that would only fit in ghost-tight
    tours where the residual is irrecoverably constrained.
    """
    import time

    players = get_tournament_players(tid)
    pids = sorted(
        [p["player_id"] for p in players if not p.get("eliminated")]
    )
    if len(pids) < 2:
        return 0, 0, 0, 0
    pid_set = set(pids)

    mpp = max(1, int(t.get("group_matches_per_pair") or 1))
    total_tours = int(t.get("total_tours") or 0)
    n = len(pids)
    max_tours = (n - 1 if n % 2 == 0 else n) * mpp
    if total_tours == 0 or total_tours > max_tours:
        total_tours = max_tours
        update_tournament(tid, total_tours=total_tours)

    if total_tours <= 0:
        return 0, 0, 0, 0

    # Confirmed-only pair counts (pending was deleted in Pass 2).
    pair_counts_initial = _played_pair_counts(tid)

    # Per-tour committed roster players (from confirmed matches).
    conn = get_conn()
    rows = conn.execute(
        "SELECT tour_number, player1_id, player2_id FROM matches "
        "WHERE tournament_id=? AND stage='group' "
        "AND status='confirmed'",
        (tid,),
    ).fetchall()
    conn.close()
    committed_per_tour: dict[int, set[int]] = {}
    for r in rows:
        try:
            t_num = int(r["tour_number"] or 0)
            p1 = r["player1_id"]
            p2 = r["player2_id"]
        except (KeyError, TypeError, IndexError):
            t_num = int(r[0] or 0)
            p1, p2 = r[1], r[2]
        if t_num <= 0:
            continue
        s = committed_per_tour.setdefault(t_num, set())
        if p1 in pid_set:
            s.add(p1)
        if p2 in pid_set:
            s.add(p2)

    free_per_tour: dict[int, list[int]] = {
        T: sorted(p for p in pids if p not in committed_per_tour.get(T, set()))
        for T in range(1, total_tours + 1)
    }

    # ── How many real opponents does each player still owe? ─────────
    # We need this to detect structural shortages (ghost-affected
    # players have fewer free tours than unmet real opponents) and to
    # log a clear diagnostic if the schedule can't fully close.
    real_played: dict[int, int] = {p: 0 for p in pids}
    for k, v in pair_counts_initial.items():
        try:
            a, b = tuple(k)
        except ValueError:
            continue
        if a in pid_set and b in pid_set:
            real_played[a] = real_played.get(a, 0) + v
            real_played[b] = real_played.get(b, 0) + v
    target_real = (n - 1) * mpp
    free_tours_per_player = {
        p: sum(
            1 for T in range(1, total_tours + 1)
            if p not in committed_per_tour.get(T, set())
        )
        for p in pids
    }
    excess: dict[int, int] = {}
    for p in pids:
        owed = max(0, target_real - real_played.get(p, 0))
        slots = free_tours_per_player[p]
        if owed > slots:
            excess[p] = owed - slots

    if excess:
        log.warning(
            "_prefill_full_schedule tid=%s: %s player(s) structurally "
            "short on tour slots (need > free): %s",
            tid, len(excess), sorted(excess.items()),
        )

    # ── Iterative tour-by-tour matching with random restarts ────────
    best_sched: dict[int, list[tuple[int, int]]] | None = None
    best_count = -1
    best_relax = 0
    start = time.monotonic()
    max_seconds = 25.0
    max_attempts = 64
    attempts = 0
    while attempts < max_attempts and (time.monotonic() - start) < max_seconds:
        attempts += 1
        seed = attempts * 9973
        rng = random.Random(seed)
        pc = dict(pair_counts_initial)
        sched: dict[int, list[tuple[int, int]]] = {}
        relax_count = 0
        # Process tours in a randomized order so different attempts
        # explore different pair → tour assignments.
        tour_order = list(range(1, total_tours + 1))
        rng.shuffle(tour_order)
        for T in tour_order:
            free = list(free_per_tour[T])
            if len(free) < 2:
                continue
            m = _match_subset_with_partial(free, pc, mpp, rng)
            if not m:
                continue
            sched[T] = m
            for a, b in m:
                key = frozenset((a, b))
                pc[key] = pc.get(key, 0) + 1
        # Greedy second pass: try to place any pair that's still unplayed
        # into any tour where both endpoints are still free in `sched`.
        # This catches pairs the random first pass overlooked.
        remaining_pairs = []
        for i in range(len(pids)):
            for j in range(i + 1, len(pids)):
                a, b = pids[i], pids[j]
                if pc.get(frozenset((a, b)), 0) < mpp:
                    remaining_pairs.append((a, b))
        # Track per-tour used players from sched + committed
        used_per_tour: dict[int, set[int]] = {
            T: set(committed_per_tour.get(T, set()))
            for T in range(1, total_tours + 1)
        }
        for T, pairs in sched.items():
            for a, b in pairs:
                used_per_tour[T].add(a)
                used_per_tour[T].add(b)
        rng.shuffle(remaining_pairs)
        for a, b in remaining_pairs:
            for T in range(1, total_tours + 1):
                if a not in used_per_tour[T] and b not in used_per_tour[T]:
                    sched.setdefault(T, []).append((a, b))
                    used_per_tour[T].add(a)
                    used_per_tour[T].add(b)
                    pc[frozenset((a, b))] = pc.get(frozenset((a, b)), 0) + 1
                    break

        count = sum(len(p) for p in sched.values())
        if count > best_count:
            best_count = count
            best_sched = sched
            best_relax = relax_count
            # Early exit if this attempt placed every owed pair.
            owed_total = sum(
                max(0, target_real - real_played.get(p, 0)) for p in pids
            ) // 2
            if count >= owed_total:
                break

    if best_sched is None:
        log.warning(
            "_prefill_full_schedule tid=%s: solver returned no schedule "
            "after %s attempts (elapsed=%.2fs)",
            tid, attempts, time.monotonic() - start,
        )
        return 0, 0, 0, 0

    log.info(
        "_prefill_full_schedule tid=%s: best schedule placed %s pairs "
        "across %s tours after %s attempts (%.2fs)",
        tid, best_count,
        sum(1 for p in best_sched.values() if p),
        attempts, time.monotonic() - start,
    )

    # Persist the new pending matches.
    deadline = (
        datetime.utcnow() + timedelta(hours=MATCH_DEADLINE_HOURS)
    ).strftime("%Y-%m-%d %H:%M:%S")

    tours_with_new = 0
    matches_created = 0
    pair_running = dict(pair_counts_initial)
    for T in sorted(best_sched.keys()):
        pairs = best_sched[T]
        if not pairs:
            continue
        for a, b in pairs:
            key = frozenset((a, b))
            leg = pair_running.get(key, 0) + 1
            pair_running[key] = leg
            if leg % 2 == 0:
                a, b = b, a
            mid = create_match(
                tid, a, b,
                stage="group",
                round_num=1,
                deadline=deadline,
                leg=leg,
            )
            conn = get_conn()
            conn.execute(
                "UPDATE matches SET tour_number=? WHERE id=?",
                (T, mid),
            )
            conn.commit()
            conn.close()
            matches_created += 1
        create_tournament_tour(tid, T)
        tours_with_new += 1

    # Make sure tournament_tours rows exist for every tour that has a
    # confirmed match too — they may have been dropped above if no
    # pending matches were re-scheduled into that tour.
    for T in committed_per_tour:
        create_tournament_tour(tid, T)

    # Compute how many owed pairs the solver couldn't place.
    placed_pairs = best_count
    owed_total = sum(
        max(0, target_real - real_played.get(p, 0)) for p in pids
    ) // 2
    skipped_pairs = max(0, owed_total - placed_pairs)

    return tours_with_new, matches_created, best_relax, skipped_pairs


def _match_subset_with_partial(
    free: list[int],
    pair_counts: dict[frozenset, int],
    mpp: int,
    rng: random.Random,
) -> list[tuple[int, int]]:
    """Find a near-perfect matching of ``free`` using only pairs whose
    pair-count is below ``mpp``.

    Tries the strict matcher first (degree-heuristic DFS with random
    tie-break). If no perfect matching exists, falls back to a greedy
    pass that picks pairs in the order most-constrained-first; the
    leftover players sit out for this tour.

    Returns the list of pairs (possibly empty if ``free`` has < 2
    pairable players).
    """
    if len(free) < 2:
        return []

    pad = list(free)
    if len(pad) % 2 == 1:
        pad = pad + [None]

    get = pair_counts.get

    def allowed(a, b) -> bool:
        if a is None or b is None:
            return True
        return get(frozenset((a, b)), 0) < mpp

    def free_count(x, remaining):
        return sum(1 for y in remaining if y != x and allowed(x, y))

    def perfect(remaining):
        if not remaining:
            return []
        best = None
        best_count = len(pad) + 2
        for x in remaining:
            c = free_count(x, remaining)
            if c < best_count or (c == best_count and rng.random() < 0.5):
                best = x
                best_count = c
        first = best
        if best_count == 0:
            return None
        rest = [p for p in remaining if p != first]
        cands = [y for y in rest if allowed(first, y)]
        cands.sort(key=lambda y: (free_count(y, rest), rng.random()))
        for partner in cands:
            sub = [p for p in rest if p != partner]
            res = perfect(sub)
            if res is not None:
                return [(first, partner)] + res
        return None

    pm = perfect(list(pad))
    if pm is not None:
        return [(a, b) for a, b in pm if a is not None and b is not None]

    # Partial matching fallback — greedy degree-first with random tie-break.
    pool = [p for p in pad if p is not None]
    rng.shuffle(pool)
    pool.sort(
        key=lambda x: (
            sum(1 for y in pool if y != x and allowed(x, y)),
            rng.random(),
        )
    )
    matched: set[int] = set()
    pairs: list[tuple[int, int]] = []
    for a in pool:
        if a in matched:
            continue
        for b in pool:
            if b == a or b in matched:
                continue
            if allowed(a, b):
                matched.add(a)
                matched.add(b)
                pairs.append((a, b))
                break
    return pairs



# ─────────────────────────────────────────────────────────────────────────────
# Champions League (32) follow-up cup spawning
# ─────────────────────────────────────────────────────────────────────────────

def _create_seeded_bracket(
    tid: int,
    seeded: list[dict],
    legs: int,
) -> list[dict]:
    """Insert first-round matches for a bracket-only cup using an
    explicit seeded order.

    Mirrors the bracket-only branch of ``generate_playoff`` but uses
    the caller-supplied seed list instead of sorting by global ELO.
    Used by ``spawn_cl_followup_cups`` so the spawned cups respect
    the league finishing order rather than per-player ELO.

    ``seeded`` items must be dicts with at least ``player_id`` and
    ``username`` (in seed order: index 0 = top seed). Bracket size is
    the next power of two ≥ ``len(seeded)`` (capped at 512); empty
    slots become byes for the top seeds, exactly like ``generate_playoff``.
    """
    n = len(seeded)
    if n < 2:
        return []
    bracket_size = min(_next_pow2(n), 512)
    if n > 512:
        seeded = seeded[:512]
        n = 512
    stage = _bracket_first_stage(bracket_size)
    raw_pairs = _build_bracket_pairs(seeded, bracket_size)

    deadline = datetime.utcnow() + timedelta(hours=MATCH_DEADLINE_HOURS)
    dl_str = deadline.strftime("%Y-%m-%d %H:%M:%S")

    created: list[dict] = []
    for pa, pb in raw_pairs:
        if pa and pb:
            for leg in range(1, max(1, int(legs)) + 1):
                if leg % 2 == 1:
                    a, b = pa, pb
                else:
                    a, b = pb, pa
                mid = create_match(tid, a["player_id"], b["player_id"],
                                   stage=stage, deadline=dl_str, leg=leg)
                created.append({
                    "stage": stage, "player1": a["username"],
                    "player2": b["username"], "match_id": mid,
                    "leg": leg, "bye": False,
                })
        elif pa or pb:
            byed = pa or pb
            mid = create_match(tid, byed["player_id"], byed["player_id"],
                               stage=stage, deadline=dl_str, leg=1)
            update_match(mid, score1=1, score2=0, status="confirmed",
                         reported_by=None)
            created.append({
                "stage": stage, "player1": byed["username"], "player2": "BYE",
                "match_id": mid, "leg": 1, "bye": True,
            })
    update_tournament(tid, playoff_started=1, stage="playoff")
    return created


def spawn_cl_followup_cups(
    league_tid: int,
    *,
    main_size: int = 24,
    consolation_size: int | None = None,
    legs_per_pair: int = 2,
) -> dict:
    """After a Champions-League-style league, spawn the two follow-up cups.

    Reads the final standings of the league (single group, sorted by
    points/GD/GF) and creates two sibling tournaments:

    * **Основной кубок** — places 1..``main_size`` of the league
      (default 24). Bracket-only with ``main_size`` registered, sized
      to the next power of two; the top ``2^k - main_size`` seeds
      receive byes from the first round (e.g. 24 → 32-bracket, top 8
      get byes; first round = ``r32`` where seeds 9-16 face seeds
      17-24). Two-leg ties (aggregate goals), no bronze.

    * **Лига Конфети** — places ``main_size+1``..``main_size+consolation_size``
      of the league. When ``consolation_size`` is ``None`` (the
      default), all remaining players past ``main_size`` are taken,
      so the same call works for 32, 34, 36, … rosters out of the
      box. Bracket size is ``next_pow2`` of that count, byes for the
      top seeds. Two-leg ties, no bronze.

    Both cups are seeded **by league finishing position**, not by
    global ELO, by manually building the first-round matches from
    the league standings. ``draw_mode`` on each spawned cup is set
    to ``"manual"`` so the bracket isn't re-rolled.

    If the resulting consolation roster is < 2 players (e.g. league
    of 25 with main_size=24), the consolation cup is skipped and
    ``consolation_tid`` in the result is ``None``.

    Returns ``{"main_tid": int, "consolation_tid": int | None,
    "main_matches": [...], "consolation_matches": [...]}``.

    Raises ``ValueError`` if the league doesn't have at least
    ``main_size`` players, isn't finished, or already has follow-up
    cups linked to it.
    """
    league = get_tournament(league_tid)
    if not league:
        raise ValueError(f"tournament {league_tid} not found")
    if league.get("groups_only") != 1:
        raise ValueError(
            f"tournament {league_tid} is not a league/groups-only "
            f"format — spawn_cl_followup_cups expects a single-group league"
        )
    # Refuse if cups were already spawned, so the inline button +
    # /cl_spawn_cups can't accidentally double-spawn.
    if league.get("followup_cups_tids"):
        raise ValueError(
            f"follow-up cups already spawned for league {league_tid} "
            f"(see {league.get('followup_cups_tids')})"
        )

    standings = get_group_standings(league_tid)
    if not standings:
        raise ValueError(f"league {league_tid} has no standings yet")
    # Single-group league: take the only group's ordering as-is.
    if len(standings) > 1:
        raise ValueError(
            f"league {league_tid} has {len(standings)} groups — "
            f"spawn_cl_followup_cups expects one"
        )
    league_order = next(iter(standings.values()))
    total = len(league_order)
    if total < int(main_size):
        raise ValueError(
            f"league {league_tid} has only {total} players, need at "
            f"least {main_size} for the main cup"
        )
    # Default consolation = "everybody after main_size", so the same
    # template handles 32 (→ 8-cons), 34 (→ 10-cons), 36 (→ 12-cons) …
    if consolation_size is None:
        cons_n = total - int(main_size)
    else:
        cons_n = int(consolation_size)
    if cons_n < 0:
        cons_n = 0
    if (int(main_size) + cons_n) > total:
        raise ValueError(
            f"main_size ({main_size}) + consolation_size ({cons_n}) "
            f"exceeds league roster ({total})"
        )
    if not check_groups_complete(league_tid):
        raise ValueError(
            f"league {league_tid} hasn't finished — confirm all matches first"
        )

    t_type = league.get("tournament_type") or "vsa"
    base_name = (league.get("name") or f"CL #{league_tid}").strip()
    creator = league.get("created_by")

    def _seed_dict(p: dict) -> dict:
        return {
            "player_id": p["player_id"],
            "username":  p.get("username") or f"id{p['player_id']}",
        }

    # ── Main cup: places 1..main_size (default 24) ────────────────────
    main_seeds = [_seed_dict(p) for p in league_order[:main_size]]
    main_tid = create_tournament(
        f"{base_name} — Кубок",
        tournament_type=t_type,
        created_by=creator,
    )
    update_tournament(
        main_tid,
        bracket_only=1,
        groups_only=0,
        groups_count=0,
        draw_mode="manual",
        playoff_matches_per_pair=int(legs_per_pair),
        playoff_advance_mode="goals",
        playoff_third_place=0,
        open_signup=0,
    )
    for s in main_seeds:
        add_player_to_tournament(main_tid, s["player_id"], "A")
    main_matches = _create_seeded_bracket(main_tid, main_seeds, int(legs_per_pair))

    # ── Consolation cup: places main_size+1..main_size+cons_n.
    #     Skipped if cons_n < 2 (no opponents → no cup).
    cons_tid = None
    cons_matches: list[dict] = []
    if cons_n >= 2:
        cons_seeds = [
            _seed_dict(p)
            for p in league_order[int(main_size):int(main_size) + cons_n]
        ]
        cons_tid = create_tournament(
            f"{base_name} — Лига Конфети",
            tournament_type=t_type,
            created_by=creator,
        )
        update_tournament(
            cons_tid,
            bracket_only=1,
            groups_only=0,
            groups_count=0,
            draw_mode="manual",
            playoff_matches_per_pair=int(legs_per_pair),
            playoff_advance_mode="goals",
            playoff_third_place=0,
            open_signup=0,
        )
        for s in cons_seeds:
            add_player_to_tournament(cons_tid, s["player_id"], "A")
        cons_matches = _create_seeded_bracket(cons_tid, cons_seeds, int(legs_per_pair))

    # Mark the league row so we don't accidentally double-spawn from
    # a stale "Создать кубки" button or a second /cl_spawn_cups call.
    update_tournament(
        league_tid,
        followup_cups_tids=f"{int(main_tid)}:{int(cons_tid) if cons_tid else 0}",
    )

    return {
        "main_tid": main_tid,
        "consolation_tid": cons_tid,
        "main_matches": main_matches,
        "consolation_matches": cons_matches,
    }


def parse_followup_cups_config(raw: str | None) -> dict | None:
    """Decode the JSON-encoded ``followup_cups_config`` column.

    Returns ``None`` if the column is empty/null or doesn't parse.
    Otherwise returns a dict with ``main_size``, ``consolation_size``
    and ``legs_per_pair``. ``consolation_size`` may be ``None`` —
    that's the signal to ``spawn_cl_followup_cups`` to use "all
    remaining players past main_size", which is what the
    ``champions_league_32`` template does so it works for any
    roster size.
    """
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    cs_raw = data.get("consolation_size")
    return {
        "main_size": int(data.get("main_size", 24)),
        "consolation_size": int(cs_raw) if cs_raw is not None else None,
        "legs_per_pair": int(data.get("legs_per_pair", 2)),
    }


def parse_followup_cups_tids(raw: str | None) -> tuple[int, int | None] | None:
    """Decode ``followup_cups_tids`` column (``"<main>:<cons>"``).

    Returns ``None`` if not yet spawned. ``cons`` is ``None`` when
    there was no consolation cup (encoded as ``:0`` in the column).
    """
    if not raw:
        return None
    try:
        main_s, cons_s = str(raw).split(":", 1)
        main_tid = int(main_s)
        cons_tid = int(cons_s)
        return main_tid, (cons_tid if cons_tid > 0 else None)
    except (ValueError, TypeError):
        return None
