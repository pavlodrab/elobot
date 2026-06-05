"""
Tournament management: group draws, standings calculation, playoff bracket.
"""
import json
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
    get_player_by_id,
    update_match,
    bulk_set_match_tours,
)

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


def round_robin_schedule(player_ids: list) -> list[list[tuple]]:
    """Standard circle method for round-robin scheduling.

    Returns a list of rounds; each round is a list of (p1_id, p2_id)
    pairs. The result never contains ``None`` entries — BYE slots are
    silently dropped so callers always get real match pairs.

    For N players (N even)  → N-1 rounds of N/2 matches each.
    For N players (N odd)   → N rounds of (N-1)/2 matches each (one BYE
    per round is discarded).
    """
    ids: list = list(player_ids)
    n = len(ids)
    if n < 2:
        return []
    if n % 2 == 1:
        ids.append(None)  # BYE placeholder
        n += 1

    rounds: list[list[tuple]] = []
    for _ in range(n - 1):
        round_pairs: list[tuple] = []
        half = n // 2
        for i in range(half):
            p1 = ids[i]
            p2 = ids[n - 1 - i]
            if p1 is not None and p2 is not None:
                round_pairs.append((p1, p2))
        rounds.append(round_pairs)
        # Rotate: fix ids[0], rotate ids[1:] one step to the right
        # (last element moves to position 1).
        ids = [ids[0]] + [ids[-1]] + ids[1:-1]

    return rounds


def generate_group_fixtures(tid: int, groups: dict[str, list[int]]):
    """
    Create all round-robin matches for each group. Honours the
    tournament's `group_matches_per_pair` setting: 1 = single round-robin,
    2 = double round-robin (each pair plays twice, second leg with
    swapped sides).

    Assigns ``tour_num`` to each match using the Berger circle-method
    schedule so matches in the same gameweek share the same tour number
    across all groups.
    """
    deadline = datetime.utcnow() + timedelta(hours=MATCH_DEADLINE_HOURS)
    t = get_tournament(tid) or {}
    mpp = max(1, int(t.get("group_matches_per_pair") or 1))

    # Build per-group round-robin schedules and merge them by round index.
    # All groups' round-r matches → same tour_num (= r + 1 for first leg,
    # offset by (n-1) rounds for the second leg when mpp=2).
    group_schedules: dict[str, list[list[tuple]]] = {}
    for group, pids in groups.items():
        group_schedules[group] = round_robin_schedule(pids)

    # Maximum rounds in any group (usually equal across all groups)
    max_rounds = max((len(s) for s in group_schedules.values()), default=0)

    # We collect (mid, tour_num) pairs and bulk-update at the end for
    # efficiency — avoids one extra DB round-trip per match.
    tour_assignments: list[tuple[int, int]] = []

    deadline_str = deadline.strftime("%Y-%m-%d %H:%M:%S")
    mids: list[int] = []

    # First leg: tours 1 .. max_rounds
    for round_idx in range(max_rounds):
        tour = round_idx + 1  # 1-based
        for group, schedule in group_schedules.items():
            if round_idx >= len(schedule):
                continue
            for (a, b) in schedule[round_idx]:
                mid = create_match(
                    tid, a, b,
                    stage="group",
                    round_num=1,
                    deadline=deadline_str,
                    leg=1,
                    tour_num=tour,
                )
                mids.append(mid)

    if mpp >= 2:
        # Second leg: tours max_rounds+1 .. 2*max_rounds
        # Reverse the schedule so return fixtures feel natural.
        for round_idx in range(max_rounds):
            tour = max_rounds + round_idx + 1
            for group, schedule in group_schedules.items():
                # Second leg reverses home/away and plays rounds in
                # reverse order (last-round first leg → first-round
                # return leg gives variety).
                rev_idx = max_rounds - 1 - round_idx
                if rev_idx >= len(schedule):
                    continue
                for (a, b) in schedule[rev_idx]:
                    mid = create_match(
                        tid, b, a,  # swapped sides for return leg
                        stage="group",
                        round_num=2,
                        deadline=deadline_str,
                        leg=2,
                        tour_num=tour,
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
    lines = ["📊 <b>Турнирная таблица</b>\n"]
    for g, players in sorted(standings.items()):
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
