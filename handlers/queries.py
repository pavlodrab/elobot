"""Read-only query commands (Phase 3 of the bot.py split).

Self-contained "report" handlers that only read DB rows and format
output — no state mutation, no auto-advancement, no callback wiring.

Currently houses the v12 convenience commands:

* ``/h2h``              — head-to-head history between two players.
* ``/my_deadlines``     — calling user's open matches sorted by deadline.
* ``/tlog``             — tournament audit-log tail.
* ``/playoff_preview``  — what the bracket *would* look like if started now.

These were previously inline in ``bot.py``; ``bot.py`` re-exports them
unchanged for backward compatibility.
"""

from __future__ import annotations

import html

from telegram import Update
from telegram.ext import ContextTypes

from database import (
    get_h2h_matches,
    get_open_matches_for_player,
    get_player_by_id,
    get_tournament,
    list_tournament_audit_log,
)
from tournament import compute_playoff_preview

from handlers._helpers import (
    _STAGE_RU,
    _can_manage_tournament,
    _format_deadline_countdown,
    _player_from_user,
    _resolve_player_arg,
    _resolve_tournament_from_args,
)
from handlers.common import (
    _fmt_date,
    _fmt_dt,
    mention,
    send,
    t_full_label,
)


# ─────────────────────────────────────────────────────────────────────────────
# /h2h
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_h2h(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """``/h2h @a [@b]`` — head-to-head history between two players.

    With one argument, compares the calling user against ``@a``. Aggregates
    wins/draws/losses + goal totals across all confirmed matches, breaks
    them down by tournament type (ВСА / РИ), and shows the most recent 5
    matches with date and tournament tag.
    """
    args = list(ctx.args or [])
    if not args:
        await send(
            update,
            "Использование: <code>/h2h @user1 @user2</code> "
            "или <code>/h2h @opponent</code> (сравнение с тобой).",
        )
        return
    me = _player_from_user(update.effective_user)
    if len(args) == 1:
        if not me:
            await send(update, "❌ Сначала зарегистрируйся: /register")
            return
        a, b = me, _resolve_player_arg(args[0])
    else:
        a = _resolve_player_arg(args[0])
        b = _resolve_player_arg(args[1])
    if not a or not b:
        await send(update, "❌ Не нашёл одного из игроков. "
                            "Они должны быть зарегистрированы.")
        return
    if a["id"] == b["id"]:
        await send(update, "🙃 Это один и тот же игрок.")
        return

    matches = get_h2h_matches(a["id"], b["id"])
    if not matches:
        await send(
            update,
            f"🤝 {mention(a['username'])} vs {mention(b['username'])}\n"
            f"<i>Между ними нет подтверждённых матчей.</i>",
        )
        return

    a_wins = b_wins = draws = 0
    a_goals = b_goals = 0
    by_type: dict[str, dict] = {}
    for m in matches:
        s1 = int(m.get("score1") or 0)
        s2 = int(m.get("score2") or 0)
        a_is_p1 = m["player1_id"] == a["id"]
        a_score = s1 if a_is_p1 else s2
        b_score = s2 if a_is_p1 else s1
        a_goals += a_score
        b_goals += b_score
        if a_score > b_score:
            a_wins += 1
        elif a_score < b_score:
            b_wins += 1
        else:
            draws += 1
        # Per-type breakdown.
        tt = (m.get("tournament_type") or "").lower() or None
        if not tt and m.get("tournament_id"):
            t = get_tournament(m["tournament_id"])
            if t:
                tt = (t.get("tournament_type") or "").lower() or None
        bucket = by_type.setdefault(
            tt or "other",
            {"a": 0, "b": 0, "d": 0, "ag": 0, "bg": 0},
        )
        bucket["ag"] += a_score
        bucket["bg"] += b_score
        if a_score > b_score:
            bucket["a"] += 1
        elif a_score < b_score:
            bucket["b"] += 1
        else:
            bucket["d"] += 1

    lines = [
        f"🤝 <b>{mention(a['username'])} vs {mention(b['username'])}</b>",
        f"Всего матчей: <b>{len(matches)}</b>",
        f"  Победы {mention(a['username'])}: <b>{a_wins}</b>",
        f"  Победы {mention(b['username'])}: <b>{b_wins}</b>",
        f"  Ничьи: <b>{draws}</b>",
        f"  Голы: <b>{a_goals}:{b_goals}</b>",
    ]
    if len(by_type) > 1 or (by_type and "other" not in by_type):
        lines.append("")
        for tt in ("vsa", "ri", "other"):
            if tt not in by_type:
                continue
            bk = by_type[tt]
            label = (
                "ВСА" if tt == "vsa"
                else "РИ" if tt == "ri"
                else "Другое"
            )
            lines.append(
                f"<b>{label}</b>: "
                f"{bk['a']}-{bk['d']}-{bk['b']} "
                f"(голы {bk['ag']}:{bk['bg']})"
            )

    lines.append("\n<b>Последние матчи</b>")
    for m in matches[:5]:
        s1 = int(m.get("score1") or 0)
        s2 = int(m.get("score2") or 0)
        a_is_p1 = m["player1_id"] == a["id"]
        a_score = s1 if a_is_p1 else s2
        b_score = s2 if a_is_p1 else s1
        date = _fmt_date(m.get("played_at") or m.get("created_at"))
        tag = ""
        if m.get("tournament_id"):
            t = get_tournament(m["tournament_id"])
            if t:
                tag = f" · <i>{html.escape(t.get('name') or '?')}</i>"
        lines.append(
            f"  {date}: <b>{a_score}:{b_score}</b>"
            f" {mention(a['username'])} vs {mention(b['username'])}"
            f"{tag}"
        )
    await send(update, "\n".join(lines))


# ─────────────────────────────────────────────────────────────────────────────
# /my_deadlines
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_my_deadlines(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """``/my_deadlines`` — calling user's open matches, sorted by deadline.

    Each entry shows the opponent, tournament + stage, ETA / overdue
    countdown, status and match ID. Used both as a top-level command and
    via the "📅 Мои матчи / дедлайны" button in /profile.
    """
    me = _player_from_user(update.effective_user)
    if not me:
        await send(update, "❌ Сначала зарегистрируйся: /register")
        return
    rows = get_open_matches_for_player(me["id"])
    if not rows:
        await send(update, "🎉 У тебя нет открытых матчей — всё сыграно!")
        return
    lines = [f"📅 <b>Мои матчи</b> ({mention(me['username'])})"]
    for m in rows:
        opp_id = m["player2_id"] if m["player1_id"] == me["id"] else m["player1_id"]
        opp = get_player_by_id(opp_id)
        opp_label = mention(opp["username"]) if opp else f"id{opp_id}"
        t = get_tournament(m["tournament_id"]) if m.get("tournament_id") else None
        t_label = ""
        if t:
            stage = m.get("stage") or t.get("stage") or ""
            stage_ru = _STAGE_RU.get(stage, "")
            t_label = f" · <i>{html.escape(t['name'])}</i>"
            if stage_ru:
                t_label += f" · {stage_ru}"
        eta = _format_deadline_countdown(m.get("deadline"))
        status_ru = (
            "ожидает соперника" if m.get("status") == "reported"
            else "не сыгран"
        )
        lines.append(
            f"  • vs {opp_label}{t_label}\n"
            f"    {eta} · <code>{status_ru}</code>"
            f" · матч #<code>{m['id']}</code>"
        )
    await send(update, "\n".join(lines))


# ─────────────────────────────────────────────────────────────────────────────
# /tlog  — tournament audit-log tail
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_tlog(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """``/tlog [ID] [N]`` — last N (≤50, default 30) tournament audit entries.

    Available to the tournament creator, root admins and delegated
    tournament admins.
    """
    # Late import to avoid the import cycle (handlers.admin → handlers._helpers
    # → handlers.queries would otherwise loop on a top-level import).
    from handlers.admin import _split_tadmin_args

    args = list(ctx.args or [])
    limit = 30
    # Pull optional numeric limit off the tail.
    if args and args[-1].isdigit() and int(args[-1]) <= 200:
        n = int(args.pop())
        limit = max(1, min(50, n))
    # Reuse the t-admin arg splitter shape (optional leading tournament ID).
    saved_args = ctx.args
    ctx.args = args
    try:
        t, _rest, err = _split_tadmin_args(update, ctx)
    finally:
        ctx.args = saved_args
    if t is None:
        await send(update, err or "❌ Нет активного турнира.")
        return

    user_id = update.effective_user.id
    if not _can_manage_tournament(user_id, t):
        await send(
            update,
            "❌ Журнал турнира доступен только админам этого турнира "
            "(создатель / делегированные / root).",
        )
        return

    rows = list_tournament_audit_log(int(t["id"]), limit=limit)
    header = (
        f"📜 <b>Журнал турнира</b> "
        f"<b>{html.escape(t['name'])}</b> [{t_full_label(t)}]"
    )
    if not rows:
        await send(
            update,
            header + "\n<i>Записей пока нет — журнал начнёт заполняться "
                     "с первого админ-действия после обновления бота.</i>",
        )
        return
    lines = [header, ""]
    for r in rows:
        ts = _fmt_dt(r.get("ts") or "", fmt="%Y-%m-%d %H:%M")
        actor_lbl = (
            f"@{r['actor_username']}" if r.get("actor_username")
            else (f"id{r['actor_telegram_id']}"
                  if r.get("actor_telegram_id") else "?")
        )
        details = r.get("details") or ""
        line = f"  {ts} · {html.escape(actor_lbl)} · <code>{html.escape(r['action'])}</code>"
        if details:
            line += f" — {html.escape(details)}"
        lines.append(line)
    await send(update, "\n".join(lines))


# ─────────────────────────────────────────────────────────────────────────────
# /playoff_preview
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_playoff_preview(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """``/playoff_preview [ID]`` — preview the bracket *before* /start_playoff.

    Re-runs the cross-group seeding algorithm against the current group
    standings, but writes nothing to the DB. Useful for resolving
    tie-breaker questions before the bracket is fixed.
    """
    t, err = _resolve_tournament_from_args(update, ctx)
    if t is None:
        await send(update, err or "❌ Нет активного турнира.")
        return
    is_bracket_only = bool(int(t.get("bracket_only") or 0))
    advance = max(1, int(t.get("playoff_slots") or 2))
    preview = compute_playoff_preview(int(t["id"]), advance_per_group=advance)
    stage = preview.get("stage")
    pairs = preview.get("pairs") or []
    quals = preview.get("qualifiers") or {}
    seeded = preview.get("seeded") or []

    lines = [
        f"🔮 <b>Превью плей-офф</b> "
        f"<b>{html.escape(t['name'])}</b> [{t_full_label(t)}]",
    ]
    if not stage or not pairs:
        if is_bracket_only:
            lines.append(
                "<i>Добавь хотя бы 2 игроков "
                "(<code>/add_player @user1, @user2, ...</code>).</i>"
            )
        else:
            lines.append(
                "<i>Недостаточно данных для жеребьёвки — "
                "сыграй больше групповых матчей.</i>"
            )
    else:
        stage_ru = _STAGE_RU.get(stage, stage.upper())
        lines.append(f"\n<b>Стадия старта:</b> {stage_ru}")
        if is_bracket_only:
            lines.append(
                "<b>Формат:</b> сразу плей-офф (без групп). "
                "Сидование — по глобальному ELO."
            )
            lines.append("\n<b>Сиды:</b>")
            for i, p in enumerate(seeded, start=1):
                lines.append(
                    f"  {i}. {mention(p['username'])} "
                    f"(ELO {int(p.get('_elo') or 0)})"
                )
        else:
            lines.append(f"<b>Из группы выходят:</b> {advance}")
            lines.append("\n<b>Квалифайеры по группам:</b>")
            for g in sorted(quals.keys()):
                qs = quals[g]
                entries = ", ".join(
                    f"{i+1}{g} {mention(p['username'])}"
                    f" ({p.get('group_points', 0)} оч)"
                    for i, p in enumerate(qs)
                )
                lines.append(f"  • <b>{g}:</b> {entries}")
        lines.append("\n<b>Пары:</b>")
        for pr in pairs:
            a, b = pr["a"], pr.get("b")
            if pr.get("bye") or b is None:
                # Top seed with no first-round opponent: bye to the next
                # round (happens when n_qualifiers is not a power of 2).
                if is_bracket_only:
                    lines.append(
                        f"  🎟 {mention(a['username'])}"
                        f" (ELO {int(a.get('_elo') or 0)}) — bye"
                    )
                else:
                    lines.append(
                        f"  🎟 {mention(a['username'])}"
                        f" ({a.get('group_points', 0)} оч) — bye"
                    )
            else:
                if is_bracket_only:
                    lines.append(
                        f"  ⚔️ {mention(a['username'])}"
                        f" (ELO {int(a.get('_elo') or 0)})"
                        f" — {mention(b['username'])}"
                        f" (ELO {int(b.get('_elo') or 0)})"
                    )
                else:
                    lines.append(
                        f"  ⚔️ {mention(a['username'])}"
                        f" ({a.get('group_points', 0)} оч)"
                        f" — {mention(b['username'])}"
                        f" ({b.get('group_points', 0)} оч)"
                    )
        lines.append(
            "\n<i>Это предварительная сетка. Окончательная зафиксируется "
            "по команде /start_tournament (для bracket-only) или "
            "/start_playoff.</i>"
        )
    await send(update, "\n".join(lines))


__all__ = [
    "cmd_h2h",
    "cmd_my_deadlines",
    "cmd_tlog",
    "cmd_playoff_preview",
]
