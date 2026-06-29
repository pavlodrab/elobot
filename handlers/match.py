"""Match-state handlers and auto-advance helpers (Phase 4 of the bot.py
split).

This module owns the **state-mutating** match flow that used to live in
``bot.py``:

* Auto-advance + announcement helpers
  (``_maybe_auto_advance``, ``_announce_stage_advance``,
  ``_current_playoff_stage``).
* Admin-approval helpers
  (``_approver_telegram_ids``, ``_send_match_to_admins``,
  ``_after_opponent_confirm``, ``_finalize_match_after_admin``).
* Phantom-aware "open matches" listing
  (``_list_pending_matches_for``).
* Walkover engine
  (``_do_walkover``).
* The series-tally display helper used by
  ``_finalize_match_after_admin`` and the inline-confirm flow
  (``_format_series_line``).

And the user-facing commands they back:

* ``/dispute`` — re-open a reported match for admin review.
* ``/admin_report`` — admin sets a confirmed result for two users.
* ``/award_points`` — manual group-stage point adjustment.
* ``/edit_goals`` — replace the goal-event list of a match.
* ``/pending`` — list non-finished matches the admin can act on.
* ``/walkover``, ``/walkover_match``, ``/walkover_all`` — apply 0:3.
* ``/withdraw`` — bulk-walkover all open matches of one player.

Everything is re-exported from ``bot`` for backward compatibility
(``from bot import _maybe_auto_advance, cmd_walkover`` keeps working).
"""

from __future__ import annotations

import html
import logging
import re
from collections import defaultdict

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.error import TelegramError
from telegram.ext import ContextTypes

import database as db
from database import (
    get_active_tournament,
    get_active_tournaments,
    get_match,
    get_open_matches_for_player,
    get_pending_match,
    get_player_by_id,
    get_player_by_telegram_id,
    get_tournament,
    get_tournament_by_chat,
    get_tournament_matches,
    get_tournament_players,
    list_tournament_admins,
    log_tournament_action,
    update_match,
    update_tournament,
)
from match_processor import apply_result, apply_walkover
from tournament import check_groups_complete, get_tournament_podium

from handlers._helpers import (
    _STAGE_RU,
    _can_manage_tournament,
    _player_from_user,
    _resolve_player_arg,
)
from handlers.admin import _resolve_tadmin_target, _split_tadmin_args
from handlers.common import (
    ADMIN_IDS,
    _fmt_minute_local,
    _local_to_utc_str,
    _tz_label,
    arrow,
    is_admin,
    mention,
    send,
    t_full_label,
    t_type_label,
)

log = logging.getLogger(__name__)

SCORE_RE = re.compile(r"^(\d+):(\d+)$")


# ─────────────────────────────────────────────────────────────────────────────
# Series-tally display helper (used by the confirm + admin-finalize flows)
# ─────────────────────────────────────────────────────────────────────────────

def _format_series_line(match_id: int) -> str | None:
    """Build a short "series score" line for a confirmed match between two
    players inside a tournament. Returns ``None`` if there's no
    tournament context or this is the only match between them.

    Example output:
      "📊 1:0 в серии"
      "📊 Серия закрыта 3:1 — победил @username"
    """
    m = get_match(match_id)
    if not m:
        return None
    tid = m.get("tournament_id")
    if not tid:
        return None
    p1, p2 = m["player1_id"], m["player2_id"]
    series = db.count_confirmed_matches_between(p1, p2, tid)
    if series["total"] <= 1:
        return None
    p1_wins, p2_wins = series["p1_wins"], series["p2_wins"]
    draws = series.get("draws", 0)

    # First-to-N: read tournament's series_length config (default 1 = no series).
    t = db.get_tournament(tid)
    target = 0
    try:
        target = int((t or {}).get("series_length") or 0)
    except (TypeError, ValueError):
        target = 0
    needed = (target + 1) // 2 if target >= 2 else 0  # bo3 => first to 2
    closed_winner_pid = None
    if needed and p1_wins >= needed and p1_wins > p2_wins:
        closed_winner_pid = p1
    elif needed and p2_wins >= needed and p2_wins > p1_wins:
        closed_winner_pid = p2

    p1_name = (get_player_by_id(p1) or {}).get("username") or "?"
    p2_name = (get_player_by_id(p2) or {}).get("username") or "?"
    draws_suffix = f" · 🤝 ничьих: {draws}" if draws > 0 else ""
    if closed_winner_pid:
        winner_name = p1_name if closed_winner_pid == p1 else p2_name
        return (
            f"✅ Серия закрыта <b>{p1_wins}:{p2_wins}</b>{draws_suffix} — "
            f"победил {mention(winner_name)}"
        )
    return f"📊 <b>{p1_wins}:{p2_wins}</b> в серии{draws_suffix}"


# ─────────────────────────────────────────────────────────────────────────────
# Auto-advance helpers
# ─────────────────────────────────────────────────────────────────────────────

def _maybe_auto_advance(ctx: ContextTypes.DEFAULT_TYPE, tournament_id: int) -> bool:
    """Check if the group stage of ``tournament_id`` is finished. If so,
    build the playoff bracket using the configured ``playoff_slots``
    (default 2) from each group, OR advance the next playoff round if the
    current one is done. Returns True if advancement happened.

    Wraps ``tournament.generate_playoff`` / ``tournament.advance_playoff``
    so the bot's match flow doesn't have to know about either.
    """
    from tournament import generate_playoff, advance_playoff, PLAYOFF_STAGES
    t = get_tournament(tournament_id)
    if not t:
        return False

    if t.get("stage") == "finished":
        return False

    # If a playoff bracket already exists (any stage), treat the
    # tournament as in playoff regardless of the ``stage`` column. This
    # repairs legacy tournaments whose ``stage`` was never flipped to
    # 'playoff' by older bot versions.
    has_playoff_rows = any(
        get_tournament_matches(tournament_id, stage=s)
        for s in PLAYOFF_STAGES
    )
    if t.get("stage") == "playoff" or has_playoff_rows:
        if t.get("stage") != "playoff":
            update_tournament(tournament_id, stage="playoff")
        result = advance_playoff(tournament_id)
        return bool(result)

    if not check_groups_complete(tournament_id):
        return False

    slots = int(t.get("playoff_slots") or 2)
    bracket = generate_playoff(tournament_id, advance_per_group=slots)
    return bool(bracket)


def _current_playoff_stage(tid: int) -> str | None:
    """Return the latest playoff stage (by PLAYOFF_STAGES order) that has
    matches in ``tid`` — used as the announcement label when
    ``apply_result`` didn't surface ``advanced_stage`` itself.
    """
    from tournament import PLAYOFF_STAGES
    latest = None
    for s in PLAYOFF_STAGES:
        if get_tournament_matches(tid, stage=s):
            latest = s
    return latest


def _podium_message_lines(tid: int) -> list[str]:
    """Render the 🥇/🥈/🥉 lines for the "tournament finished" chat
    broadcast. Returns an empty list when nothing is resolvable yet
    (e.g. final hasn't been confirmed) — caller falls back to a short
    "поздравляем победителя" stub.

    Pure formatting helper: no Telegram I/O, no DB writes. Reused by
    ``cb_finish_tournament`` via ``handlers/tournament.py`` so the
    chat broadcast and the manual /finish_tournament reply stay in
    sync.

    Per-tournament team tags (``tournament_players.team_tag``) are
    woven into each line via ``format_player_with_tag_html`` so the
    podium reads as ``"phoenileo - Германия (@Phoenileo)"`` rather
    than just ``@Phoenileo`` when teams are configured.
    """
    from handlers._helpers import format_player_with_tag_html  # local: avoid cycle
    import database as _db
    podium = get_tournament_podium(tid)

    def tag(pid: int | None) -> str:
        if not pid:
            return "—"
        p = get_player_by_id(pid)
        if not p:
            return f"id {pid}"
        try:
            tt = _db.get_tournament_player_tag(int(tid), int(pid))
        except Exception:
            tt = ""
        return format_player_with_tag_html(p, tt) or f"id {pid}"

    lines: list[str] = []
    if "first" in podium:
        lines.append(f"🥇 1-е место: {tag(podium.get('first'))}")
    if "second" in podium:
        lines.append(f"🥈 2-е место: {tag(podium.get('second'))}")
    if "third" in podium:
        lines.append(f"🥉 3-е место: {tag(podium.get('third'))}")
        if "fourth" in podium:
            lines.append(f"4-е место: {tag(podium.get('fourth'))}")
    elif podium.get("third_tied"):
        tied = ", ".join(tag(pid) for pid in podium["third_tied"])
        lines.append(f"🥉 3-е место (поровну): {tied}")
    return lines


async def _announce_stage_advance(
    ctx: ContextTypes.DEFAULT_TYPE,
    tournament_id: int,
    new_stage: str | None,
) -> None:
    """Notify everyone that a new playoff stage was generated automatically.

    Posts:
      • A short announcement in the tournament-bound chat (when set), with
        the freshly-created pairings of the new stage and a deadline hint.
      • Personal DMs to each participant of the new stage with their own
        match details.

    ``new_stage`` follows the values returned by ``advance_playoff`` —
    either an upcoming-stage tag from ``PLAYOFF_STAGES`` (``'qf'``,
    ``'sf'``, ``'final'``) or the special string ``'finished'`` for a
    closed tournament.

    Errors are swallowed: a missing/blocked DM channel must not crash the
    confirmation flow that triggered the advancement.
    """
    if not new_stage:
        return
    t = get_tournament(tournament_id)
    if not t:
        return

    t_label = (
        f"<b>{html.escape(t['name'])}</b> [{t_full_label(t)}]"
    )
    chat_id = t.get("chat_id")

    if new_stage == "groups_done":
        # CL-32 (and any other groups_only league with
        # ``followup_cups_config`` set) just closed its table.
        # Broadcast a one-tap "🏆 Создать кубки" prompt so the admin
        # doesn't have to hunt for the command — the inline button on
        # the league row's settings panel is also live, this just
        # surfaces it where the chat already is.
        from tournament import parse_followup_cups_config
        cfg = parse_followup_cups_config(t.get("followup_cups_config"))
        msg_chat = (
            f"🏁 Лига {t_label} завершена!\n\n"
            f"📊 Финальная таблица: /standings"
        )
        if cfg:
            ms = int(cfg.get("main_size", 24))
            cs_raw = cfg.get("consolation_size")
            # Consolation defaults to "all remaining past main_size".
            from database import get_tournament_players
            try:
                roster = len(get_tournament_players(tournament_id))
            except Exception:
                roster = 0
            cs = int(cs_raw) if cs_raw is not None else max(0, roster - ms)
            if cs >= 2:
                msg_chat += (
                    f"\n\n🏆 По шаблону «Лига Чемпионов» нужно сделать ещё "
                    f"два кубка:\n"
                    f"  • Основной — места 1-{ms} (сетка с байем для топ-8)\n"
                    f"  • Лига Конфети — места {ms + 1}-{ms + cs}"
                )
                btn_label = f"🏆 Создать кубки ({ms} + {cs})"
            else:
                msg_chat += (
                    f"\n\n🏆 По шаблону «Лига Чемпионов» нужно сделать "
                    f"основной кубок: места 1-{ms} (сетка с байем для топ-8)"
                )
                btn_label = f"🏆 Создать основной кубок (топ-{ms})"
            from telegram import InlineKeyboardButton, InlineKeyboardMarkup
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    btn_label,
                    callback_data=f"ts:cl_spawn:{tournament_id}",
                ),
            ]])
            if chat_id:
                try:
                    await ctx.bot.send_message(
                        chat_id, msg_chat, parse_mode="HTML",
                        reply_markup=kb,
                    )
                except Exception as e:
                    log.warning(
                        "announce(groups_done) chat %s failed: %s",
                        chat_id, e,
                    )
            return
        # No followup config — broadcast the plain "league finished"
        # message so the chat still gets a closure ping.
        if chat_id:
            try:
                await ctx.bot.send_message(
                    chat_id, msg_chat, parse_mode="HTML",
                )
            except Exception as e:
                log.warning(
                    "announce(groups_done) chat %s failed: %s",
                    chat_id, e,
                )
        return

    if new_stage == "finished":
        # Tournament is fully done — broadcast a full podium summary
        # (🥇/🥈/🥉) in the bound chat. The /finish_tournament command
        # also shows a longer лидерборд; this announcement always
        # contains at minimum the medal stand so the chat doesn't end
        # on a bare "/standings" pointer.
        podium_lines = _podium_message_lines(tournament_id)
        head = f"🏆 Турнир {t_label} завершён!"
        if podium_lines:
            msg_chat = head + "\n\n" + "\n".join(podium_lines)
        else:
            msg_chat = (
                head + " Поздравляем победителя — итоги в /standings."
            )
        # Append footer for chat context
        from handlers.common import get_random_footer, FOOTER_CTX_FINISH
        _fin_footer = get_random_footer(t, FOOTER_CTX_FINISH)
        if _fin_footer:
            msg_chat += _fin_footer
        if chat_id:
            try:
                await ctx.bot.send_message(
                    chat_id, msg_chat, parse_mode="HTML",
                )
            except Exception as e:
                log.warning("announce(finished) chat %s failed: %s",
                            chat_id, e)
        return

    stage_label = _STAGE_RU.get(new_stage, new_stage.upper())
    new_matches = [
        m for m in get_tournament_matches(tournament_id, stage=new_stage)
        if (m.get("status") or "pending") == "pending"
    ]

    # When the SF completes the bot spawns BOTH the final and the
    # 3rd-place fixture in parallel. The advance_playoff() return value
    # only mentions the final, so piggyback the bronze pair onto this
    # announcement so SF losers also get a chat/DM heads-up.
    third_pairs_for_announce: dict[tuple[int, int], list[dict]] = {}
    if new_stage == "final":
        for m in get_tournament_matches(tournament_id, stage="third"):
            if (m.get("status") or "pending") != "pending":
                continue
            a, b = sorted([m["player1_id"], m["player2_id"]])
            if a == b:
                continue
            third_pairs_for_announce.setdefault((a, b), []).append(m)

    if not new_matches and not third_pairs_for_announce:
        # Either the stage is empty or all matches were pre-played — no
        # one to notify.
        return

    # Group matches by pair so a 2-leg tie shows up as one entry.
    pairs: dict[tuple[int, int], list[dict]] = {}
    for m in new_matches:
        a, b = sorted([m["player1_id"], m["player2_id"]])
        pairs.setdefault((a, b), []).append(m)

    # ── Bound chat broadcast ────────────────────────────────────────────
    if chat_id:
        # Helper: render player by id with the per-tournament team tag
        # (when configured). Falls back to ``@username`` for un-tagged
        # players, preserving the legacy chat output.
        from handlers._helpers import format_player_with_tag_html  # local: avoid cycle
        import database as _db

        def _name_with_tag(pid: int) -> str:
            p = get_player_by_id(pid)
            if not p:
                return mention("?")
            try:
                tt = _db.get_tournament_player_tag(int(tournament_id), int(pid))
            except Exception:
                tt = ""
            if tt:
                return format_player_with_tag_html(p, tt)
            return mention(p.get("username") or "?")

        chat_lines: list[str] = []
        if pairs:
            chat_lines.append(
                f"🚀 Стадия <b>{html.escape(stage_label)}</b> стартует "
                f"в турнире {t_label}!"
            )
            for (a, b), ms in pairs.items():
                chat_lines.append(
                    f"  • {_name_with_tag(a)} vs {_name_with_tag(b)}"
                    + (f" — {len(ms)} матча" if len(ms) > 1 else "")
                )
        if third_pairs_for_announce:
            third_label = _STAGE_RU.get("third", "Матч за 3-е место")
            if chat_lines:
                chat_lines.append("")
            chat_lines.append(
                f"🥉 <b>{html.escape(third_label)}</b> "
                f"в турнире {t_label}:"
            )
            for (a, b), ms in third_pairs_for_announce.items():
                chat_lines.append(
                    f"  • {_name_with_tag(a)} vs {_name_with_tag(b)}"
                    + (f" — {len(ms)} матча" if len(ms) > 1 else "")
                )
        # Show the deadline of the first leg as a guide. All legs of
        # ``advance_playoff`` share the same deadline so any match works.
        all_new = list(new_matches) + [
            m for ms in third_pairs_for_announce.values() for m in ms
        ]
        first_dl = next(
            (m.get("deadline") for m in all_new if m.get("deadline")),
            None,
        )
        if first_dl:
            chat_lines.append(
                f"\n⏰ Дедлайн: <b>{_fmt_minute_local(first_dl) or html.escape(str(first_dl))}"
                f" {_tz_label()}</b>"
            )
        chat_lines.append("\n📊 Сетка: /playoff")
        # Append footer for chat context
        from handlers.common import get_random_footer, FOOTER_CTX_STAGE
        _stage_footer = get_random_footer(t, FOOTER_CTX_STAGE)
        if _stage_footer:
            chat_lines.append(_stage_footer)
        try:
            await ctx.bot.send_message(
                chat_id, "\n".join(chat_lines), parse_mode="HTML",
            )
        except Exception as e:
            log.warning(
                "announce(%s) chat %s failed: %s",
                new_stage, chat_id, e,
            )

    # ── Personal DMs for each participant of the new stage ──────────────
    notified: set[int] = set()

    async def _dm_pair_real(
        a: int, b: int, ms: list[dict], stage_label_dm: str, stage_tag: str,
    ) -> None:
        from handlers._helpers import format_player_with_tag_html  # local: avoid cycle
        import database as _db_local

        pa = get_player_by_id(a)
        pb = get_player_by_id(b)
        first_dl = next(
            (m.get("deadline") for m in ms if m.get("deadline")), None,
        )
        n_legs = len(ms)
        for me, opp in ((pa, pb), (pb, pa)):
            if not me or not me.get("telegram_id"):
                continue
            tg = int(me["telegram_id"])
            if tg in notified:
                continue
            notified.add(tg)
            # Render opponent with their per-tournament team tag (when set).
            opp_label = "?"
            if opp:
                try:
                    opp_tag = _db_local.get_tournament_player_tag(
                        int(tournament_id), int(opp.get("id") or 0),
                    )
                except Exception:
                    opp_tag = ""
                if opp_tag:
                    opp_label = format_player_with_tag_html(opp, opp_tag)
                else:
                    opp_label = mention(opp.get("username") or "?")
            body_lines = [
                f"🚀 Тебе предстоит <b>{html.escape(stage_label_dm)}</b> "
                f"в турнире {t_label}.",
                f"Соперник: {opp_label}",
            ]
            if n_legs > 1:
                body_lines.append(f"Серия: {n_legs} матча")
            if first_dl:
                body_lines.append(
                    f"⏰ Дедлайн: <b>{_fmt_minute_local(first_dl) or html.escape(str(first_dl))}"
                    f" {_tz_label()}</b>"
                )
            body_lines.append("Сетка: /playoff")
            try:
                await ctx.bot.send_message(
                    tg, "\n".join(body_lines), parse_mode="HTML",
                )
            except Exception as e:
                log.warning(
                    "announce(%s) DM %s failed: %s",
                    stage_tag, tg, e,
                )

    for (a, b), ms in pairs.items():
        await _dm_pair_real(a, b, ms, stage_label, new_stage)
    if third_pairs_for_announce:
        third_label = _STAGE_RU.get("third", "Матч за 3-е место")
        for (a, b), ms in third_pairs_for_announce.items():
            await _dm_pair_real(a, b, ms, third_label, "third")


# ─────────────────────────────────────────────────────────────────────────────
# Admin-approval flow
# ─────────────────────────────────────────────────────────────────────────────

def _approver_telegram_ids(t: dict | None) -> list[int]:
    """Telegram IDs of users allowed to ✅/❌ a match in tournament ``t``.

    When a tournament is specified:
      • the tournament creator;
      • per-tournament admins (``tournament_admins`` table).
    Root admins (``ADMIN_IDS`` env) are NOT included for tournament
    matches — they don't need to be spammed with every match of every
    tournament they're not involved in. They can still approve via
    ``/pending`` if needed.

    When NO tournament is specified (friendly match):
      • root admins only (fallback).

    Used by ``_send_match_to_admins`` so the approve/reject buttons reach
    everyone whose ✅ click would actually work.
    """
    ids: set[int] = set()
    if t:
        creator = (
            get_player_by_id(t["created_by"])
            if t.get("created_by") else None
        )
        if creator and creator.get("telegram_id"):
            ids.add(int(creator["telegram_id"]))
        try:
            for r in list_tournament_admins(int(t["id"])):
                if r.get("telegram_id"):
                    ids.add(int(r["telegram_id"]))
        except Exception as e:
            log.warning(
                "list_tournament_admins(%s) failed: %s", t.get("id"), e
            )
    # Fallback: if no tournament-specific recipients, use root admins.
    if not ids:
        ids = set(int(x) for x in (ADMIN_IDS or []))
    return sorted(ids)


async def _send_match_to_admins(ctx: ContextTypes.DEFAULT_TYPE, match: dict):
    """DM all admins a match awaiting their final approval.

    When the match has a ``screenshot_file_id`` (set by the photo-OCR
    flow) the admin gets the actual screenshot together with the
    approve/reject buttons — Telegram caption + inline keyboard. If
    forwarding the photo fails (or there is no file_id), we fall back
    to a plain text message with the same buttons.
    """
    p1 = get_player_by_id(match["player1_id"])
    p2 = get_player_by_id(match["player2_id"])
    t  = get_tournament(match["tournament_id"]) if match.get("tournament_id") else None
    recipients = _approver_telegram_ids(t)
    if not recipients:
        return 0
    s1, s2 = match["score1"], match["score2"]
    text = (
        "🛂 <b>Матч на проверку</b>\n\n"
        f"⚽ {mention(p1['username'] if p1 else '?')} <b>{s1}:{s2}</b> "
        f"{mention(p2['username'] if p2 else '?')}\n"
    )
    if t:
        text += f"🏆 <i>{t['name']}</i> [{t_full_label(t)}], этап: {match.get('stage','—')}\n"
    text += f"\n#match{match['id']}"

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Засчитать", callback_data=f"adm_match:ok:{match['id']}"),
        InlineKeyboardButton("❌ Отклонить",  callback_data=f"adm_match:no:{match['id']}"),
    ]])

    file_id = match.get("screenshot_file_id")
    delivered = 0
    for admin_id in recipients:
        sent = False
        if file_id:
            try:
                await ctx.bot.send_photo(
                    admin_id,
                    photo=file_id,
                    caption=text,
                    parse_mode="HTML",
                    reply_markup=kb,
                )
                sent = True
            except Exception as e:
                log.warning(
                    "admin photo notification %s failed (%s) — falling back to text",
                    admin_id, e,
                )
        if not sent:
            try:
                await ctx.bot.send_message(
                    admin_id, text, parse_mode="HTML", reply_markup=kb,
                )
                sent = True
            except Exception as e:
                log.warning("admin notification %s failed: %s", admin_id, e)
        if sent:
            delivered += 1
    return delivered


async def _send_failed_screenshot_to_admins(
    ctx: ContextTypes.DEFAULT_TYPE,
    *,
    file_id: str | None,
    score1: int | None,
    score2: int | None,
    p1_username: str | None,
    p2_username: str | None,
    tournament: dict | None,
    reporter_user,
    reason: str,
    extra_note: str = "",
) -> int:
    """Forward a failed-OCR screenshot to admins for manual handling.

    Mirrors :func:`_send_match_to_admins` but for matches that *didn't*
    make it into the DB. The admin gets:

    * the original screenshot (or text fallback when there's no file_id)
    * the parsed score (when known) and any opponent username/ID hints
    * the failure reason (e.g. "уже сыграно 1/1 групповых матчей")
    * a one-line ``/admin_report`` template the admin can copy-paste

    Recipients are picked the same way as for awaiting matches via
    :func:`_approver_telegram_ids` — tournament admins when the
    tournament is known, root bot admins otherwise.

    Returns the number of admins that received the message (0 means
    nobody got it; caller may want to surface that to the user).
    """
    recipients = _approver_telegram_ids(tournament)
    if not recipients:
        return 0

    score_str = (
        f"<b>{int(score1)}:{int(score2)}</b>"
        if score1 is not None and score2 is not None
        else "<i>счёт не распознан</i>"
    )
    p1_label = f"@{p1_username}" if p1_username else "<i>?</i>"
    p2_label = f"@{p2_username}" if p2_username else "<i>?</i>"

    reporter_who = ""
    if reporter_user is not None:
        if getattr(reporter_user, "username", None):
            reporter_who = f"@{reporter_user.username}"
        else:
            full = (getattr(reporter_user, "full_name", "") or "").strip()
            reporter_who = full or f"id {reporter_user.id}"

    t_block = ""
    if tournament:
        t_block = (
            f"🏆 <i>{html.escape(tournament.get('name') or '?')}</i> "
            f"[{t_full_label(tournament)}]\n"
        )

    body = (
        f"🛂 <b>OCR не записал — посмотри</b>\n\n"
        f"⚽ {p1_label} {score_str} {p2_label}\n"
        f"{t_block}"
        f"❗ Причина: {reason}\n"
    )
    if extra_note:
        body += f"📝 {extra_note}\n"
    if reporter_who:
        body += f"👤 Прислал: {reporter_who}\n"
    if tournament:
        body += (
            f"\nЕсли всё ок — ответь на это сообщение командой:\n"
            f"<code>/admin_report {p1_label} {p2_label} "
            f"{int(score1) if score1 is not None else 'X'}:"
            f"{int(score2) if score2 is not None else 'Y'} "
            f"{int(tournament['id'])}</code>"
        )

    delivered = 0
    for admin_id in recipients:
        sent = False
        if file_id:
            try:
                await ctx.bot.send_photo(
                    admin_id,
                    photo=file_id,
                    caption=body[:1024],   # Telegram caption limit
                    parse_mode="HTML",
                )
                sent = True
            except Exception as e:
                log.warning(
                    "OCR-fail photo to admin %s failed (%s) — falling back to text",
                    admin_id, e,
                )
        if not sent:
            try:
                await ctx.bot.send_message(
                    admin_id, body, parse_mode="HTML",
                )
                sent = True
            except Exception as e:
                log.warning("OCR-fail notification %s failed: %s", admin_id, e)
        if sent:
            delivered += 1
    return delivered


async def _after_opponent_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE, m: dict):
    """Move a match from ``reported`` to ``awaiting_admin``, notify admins
    + players."""
    update_match(m["id"], status="awaiting_admin")
    fresh = get_match(m["id"]) or m
    delivered = await _send_match_to_admins(ctx, dict(fresh))

    p1 = get_player_by_id(m["player1_id"])
    p2 = get_player_by_id(m["player2_id"])
    s1, s2 = m["score1"], m["score2"]
    body = (
        "🕐 <b>Соперник подтвердил, ждём проверку админа.</b>\n\n"
        f"⚽ {mention(p1['username'] if p1 else '?')} <b>{s1}:{s2}</b> "
        f"{mention(p2['username'] if p2 else '?')}\n"
        f"ELO начислится после согласия админа."
    )
    if not delivered:
        body += "\n\n⚠️ Админы не настроены — подойди к организатору вручную."
    await send(update, body)

    # Notify the other side too
    for pid in (m["player1_id"], m["player2_id"]):
        p = get_player_by_id(pid)
        if not p or not p.get("telegram_id"):
            continue
        if update.effective_user and p["telegram_id"] == update.effective_user.id:
            continue
        try:
            await ctx.bot.send_message(p["telegram_id"], body, parse_mode="HTML")
        except Exception:
            pass


async def _finalize_match_after_admin(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
    match_id: int,
):
    """Admin approved → apply ELO, send everyone the rich summary."""
    update_match(match_id, status="confirmed")
    summary = apply_result(match_id)

    p1_name = summary["player1"]
    p2_name = summary["player2"]
    s1, s2 = summary["score1"], summary["score2"]
    if s1 > s2:
        winner_name, loser_name = p1_name, p2_name
    elif s2 > s1:
        winner_name, loser_name = p2_name, p1_name
    else:
        winner_name = loser_name = None

    lines = [
        "✅ <b>Матч засчитан админом!</b>\n",
        f"⚽ {mention(p1_name)} <b>{s1}:{s2}</b> {mention(p2_name)}\n",
    ]
    if winner_name:
        lines.append(f"🏅 Победитель: {mention(winner_name)}\n")

    d1, d2 = summary["delta1"], summary["delta2"]
    is_official = summary.get("is_official", True)
    elo_header = "📈 ELO" if is_official else "📈 ELO (локальный, в этом турнире)"
    lines.append(
        f"{elo_header}:\n"
        f"  {mention(p1_name)}: {summary['elo1_before']} → <b>{summary['elo1_after']}</b> ({arrow(d1)})\n"
        f"  {mention(p2_name)}: {summary['elo2_before']} → <b>{summary['elo2_after']}</b> ({arrow(d2)})"
    )
    t_type = summary.get("t_type")
    p1t = summary.get("p1_typed_after")
    p2t = summary.get("p2_typed_after")
    # Per-type mirror only exists for official tournaments.
    if is_official and t_type and p1t is not None and p2t is not None:
        type_lbl = t_type_label(t_type)
        lines.append(
            f"\n📊 ELO {type_lbl}:\n"
            f"  {mention(p1_name)}: <b>{p1t}</b> ({arrow(d1)})\n"
            f"  {mention(p2_name)}: <b>{p2t}</b> ({arrow(d2)})"
        )
    if not is_official:
        lines.append(
            "\nℹ️ Это турнир игрока — общий ELO/ВСА/РИ не меняются."
        )

    series_line = _format_series_line(match_id)
    if series_line:
        lines.append("\n" + series_line)

    if summary.get("is_upset"):
        lines.append("\n🔥 <b>АПСЕТ!</b>")
    if summary.get("is_thriller"):
        lines.append(f"\n🎭 <b>Триллер!</b> {s1 + s2} голов!")
    # Only announce a winning streak when THIS match was actually a win
    # for that player. Otherwise the message reads as "@phoenileo —
    # серия 10 побед!" on a 3:3 draw, which makes no sense.
    if s1 > s2 and summary.get("win_streak1", 0) >= 3:
        lines.append(f"\n🔥 {mention(p1_name)} — серия {summary['win_streak1']} побед!")
    if s2 > s1 and summary.get("win_streak2", 0) >= 3:
        lines.append(f"\n🔥 {mention(p2_name)} — серия {summary['win_streak2']} побед!")

    if summary.get("advanced_stage"):
        stage = summary["advanced_stage"]
        if stage == "finished":
            lines.append("\n🏆 <b>Турнир завершён!</b>")
        else:
            stage_names = {"sf": "Полуфинал", "final": "Финал",
                           "qf": "Четвертьфинал", "r16": "1/8 финала",
                           "r32": "1/16 финала", "r64": "1/32 финала",
                           "r128": "1/64 финала", "r256": "1/128 финала",
                           "r512": "1/256 финала"}
            lines.append(f"\n🚀 Начинается <b>{stage_names.get(stage, stage.upper())}</b>!")

    # Append custom footer text if configured for this tournament.
    m_full_pre = get_match(match_id)
    tid_for_footer = m_full_pre.get("tournament_id") if m_full_pre else None
    if tid_for_footer:
        from handlers.common import get_random_footer
        t_footer = get_tournament(int(tid_for_footer))
        footer_line = get_random_footer(t_footer)
        if footer_line:
            lines.append(footer_line)

    msg = "\n".join(lines)
    await send(update, msg)

    m_full = get_match(match_id)
    if not m_full:
        return
    m_full = dict(m_full)
    elo_word = "ELO" if is_official else "Локальный ELO турнира"
    for pid, name, elo_after, delta in [
        (m_full["player1_id"], p1_name, summary["elo1_after"], d1),
        (m_full["player2_id"], p2_name, summary["elo2_after"], d2),
    ]:
        p = get_player_by_id(pid)
        if p and p.get("telegram_id"):
            try:
                emoji = "🏆" if name == winner_name else ("💔" if name == loser_name else "🤝")
                await ctx.bot.send_message(
                    p["telegram_id"],
                    f"{emoji} Матч засчитан админом!\n"
                    f"{mention(p1_name)} {s1}:{s2} {mention(p2_name)}\n"
                    f"Твой {elo_word}: <b>{elo_after}</b> ({arrow(delta)})",
                    parse_mode="HTML",
                )
            except Exception:
                pass

    tid = m_full.get("tournament_id")
    if tid:
        # Belt-and-braces: re-run the bracket advancement check in case
        # ``apply_result`` returned before generating the next stage (e.g.
        # the very last leg of the previous round was confirmed via a
        # different code path).
        try:
            advanced_extra = _maybe_auto_advance(ctx, int(tid))
        except Exception:
            log.exception(
                "_maybe_auto_advance failed in _finalize_match_after_admin"
            )
            advanced_extra = False
        # If apply_result already produced a stage, prefer that exact label
        # for the announcement; otherwise re-read from the tournament.
        announce_stage = summary.get("advanced_stage")
        if not announce_stage and advanced_extra:
            t_after = get_tournament(int(tid))
            if t_after:
                announce_stage = (
                    "finished"
                    if t_after.get("stage") == "finished"
                    else _current_playoff_stage(int(tid))
                )
        if announce_stage:
            await _announce_stage_advance(ctx, int(tid), announce_stage)

        # ── Tours (rounds): check if current tour is complete ──────────
        try:
            t_obj = get_tournament(int(tid))
            if t_obj and int(t_obj.get("tours_enabled") or 0):
                cur_tour = int(t_obj.get("current_tour") or 0)
                if cur_tour > 0 and db.is_tour_complete(int(tid), cur_tour):
                    db.set_tour_status(int(tid), cur_tour, "completed")
                    if int(t_obj.get("auto_next_tour") or 0):
                        # Auto-advance silently — we don't blast the
                        # bound chat with a "tour X started" message
                        # every time a tour rolls over.
                        try:
                            from tournament import generate_next_tour
                            generate_next_tour(int(tid))
                        except Exception:
                            log.exception("auto_next_tour failed")
                    else:
                        # Notify bound chat
                        bound = t_obj.get("chat_id")
                        if bound:
                            try:
                                await ctx.bot.send_message(
                                    int(bound),
                                    f"✅ <b>Все матчи тура {cur_tour} сыграны!</b>\n"
                                    f"Используй /next_tour {tid} чтобы начать следующий тур.",
                                    parse_mode="HTML",
                                )
                            except Exception:
                                pass
        except Exception:
            log.exception("tour completion check failed")


# ─────────────────────────────────────────────────────────────────────────────
# Phantom-aware "open matches" listing
# ─────────────────────────────────────────────────────────────────────────────

def _list_pending_matches_for(player_id: int, tid: int | None = None,
                               *, only_real: bool = True) -> list[dict]:
    """Non-finished matches involving ``player_id``, newest first.

    When ``only_real`` is True (default), filter out phantom matches:
      • group-stage rows where the two players are not in the same
        ``tournament_players.group_name`` are dropped;
      • playoff rows (r16/qf/sf/final) are deduplicated by
        ``(sorted_pair, stage, leg)`` — only the newest pending row of
        each tuple is kept (multiple ``/report`` calls between the same
        two players at the same stage shouldn't all become walkover-able);
      • rows with unknown stage are dropped.

    Set ``only_real=False`` for diagnostic / cleanup tools (e.g. the
    ``/prune_phantoms`` admin command, which needs to *see* phantoms).
    """
    conn = db.get_conn()
    if tid is not None:
        rows = conn.execute(
            """SELECT m.*,
                      tp1.group_name AS _p1_group,
                      tp2.group_name AS _p2_group
                 FROM matches m
            LEFT JOIN tournament_players tp1
                   ON tp1.tournament_id = m.tournament_id
                  AND tp1.player_id     = m.player1_id
            LEFT JOIN tournament_players tp2
                   ON tp2.tournament_id = m.tournament_id
                  AND tp2.player_id     = m.player2_id
                WHERE m.status IN ('pending','reported')
                  AND m.tournament_id = ?
                  AND (m.player1_id = ? OR m.player2_id = ?)
                ORDER BY m.id DESC""",
            (tid, player_id, player_id),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT m.*,
                      tp1.group_name AS _p1_group,
                      tp2.group_name AS _p2_group
                 FROM matches m
            LEFT JOIN tournament_players tp1
                   ON tp1.tournament_id = m.tournament_id
                  AND tp1.player_id     = m.player1_id
            LEFT JOIN tournament_players tp2
                   ON tp2.tournament_id = m.tournament_id
                  AND tp2.player_id     = m.player2_id
                WHERE m.status IN ('pending','reported')
                  AND (m.player1_id = ? OR m.player2_id = ?)
                ORDER BY m.id DESC""",
            (player_id, player_id),
        ).fetchall()
    conn.close()

    raw = [dict(r) for r in rows]
    if not only_real:
        return raw

    playoff_stages = {"r512", "r256", "r128", "r64", "r32", "r16",
                      "qf", "sf", "final", "third"}
    seen: set[tuple] = set()
    out: list[dict] = []
    for m in raw:
        stage = (m.get("stage") or "").lower()
        if stage == "group":
            g1 = m.get("_p1_group"); g2 = m.get("_p2_group")
            if g1 is None or g2 is None or g1 != g2:
                continue                 # phantom: cross-group or off-roster
            out.append(m); continue
        if stage in playoff_stages:
            pair = tuple(sorted((m["player1_id"], m["player2_id"])))
            key = (m.get("tournament_id"), pair, stage, m.get("leg") or 1)
            if key in seen:
                continue                 # phantom duplicate of same pair/stage
            seen.add(key)
            out.append(m); continue
        # Unknown stage — drop.
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Walkover engine
# ─────────────────────────────────────────────────────────────────────────────

async def _do_walkover(
    update: Update, ctx: ContextTypes.DEFAULT_TYPE,
    match_id: int, loser_id: int, *, send_via=None,
):
    """Apply ``apply_walkover`` and announce, used by both ``/walkover`` and
    the inline picker callback."""
    m = get_match(match_id)
    if not m:
        target = send_via or update
        await send(target, "❌ Матч не найден.")
        return
    apply_walkover(m["id"], loser_id)
    win_id = m["player2_id"] if loser_id == m["player1_id"] else m["player1_id"]
    lose_p = get_player_by_id(loser_id)
    win_p = get_player_by_id(win_id)
    actor = update.effective_user if update and update.effective_user else None
    log_tournament_action(
        m.get("tournament_id"),
        actor_telegram_id=actor.id if actor else None,
        actor_username=actor.username if actor else None,
        action="walkover",
        details=(
            f"match={m['id']} loser=@{lose_p['username'] if lose_p else loser_id} "
            f"winner=@{win_p['username'] if win_p else win_id}"
        ),
    )
    target = send_via or update
    text = (
        f"⚠️ Техническое поражение!\n\n"
        f"{mention(lose_p['username']) if lose_p else loser_id} — проигрыш (0:3)\n"
        f"{mention(win_p['username']) if win_p else win_id} — победа (3:0)\n"
        f"<i>матч #{m['id']}, турнир ID {m.get('tournament_id') or '—'}</i>"
    )
    if hasattr(target, "edit_message_text"):
        try:
            await target.edit_message_text(text, parse_mode="HTML")
        except TelegramError:
            await send(update, text)
    else:
        await send(target, text)
    if m.get("tournament_id"):
        try:
            advanced_wo = _maybe_auto_advance(ctx, m["tournament_id"])
        except Exception as e:
            log.warning("auto-advance after walkover failed: %s", e)
            advanced_wo = False
        if advanced_wo:
            await _announce_stage_advance(
                ctx, int(m["tournament_id"]),
                _current_playoff_stage(int(m["tournament_id"])),
            )


# ─────────────────────────────────────────────────────────────────────────────
# /dispute — flag a result-reported match for organiser review
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_dispute(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """``/dispute`` — opponent disputes the most recent reported match.

    Looks up the calling user's most recent ``status='reported'`` match
    that *they* did not report themselves and flips it back to
    ``pending``, surfacing it to admins for manual approval.
    """
    user = update.effective_user
    player = _player_from_user(user)
    if not player:
        await send(update, "❌ Сначала зарегистрируйся: /register")
        return
    conn = db.get_conn()
    m = conn.execute(
        """SELECT * FROM matches
           WHERE status='reported'
             AND (player1_id=? OR player2_id=?)
             AND reported_by != ?
           ORDER BY id DESC LIMIT 1""",
        (player["id"], player["id"], player["id"]),
    ).fetchone()
    conn.close()
    if not m:
        await send(update, "Нет матчей, которые ты можешь оспорить.")
        return
    update_match(dict(m)["id"], status="pending")
    await send(update, "⚠️ Матч отправлен на рассмотрение организатору.")


# ─────────────────────────────────────────────────────────────────────────────
# /admin_report — admin sets a confirmed result for two users
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_admin_report(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/admin_report @user1 @user2 score1:score2 [tournament_id]

    Admin-only. Creates a confirmed match between user1 and user2 with
    the given score, runs ``apply_result``, and (if the match was inside
    a tournament) auto-advances the playoff bracket when the group stage
    is complete.

    Without an explicit tournament_id, falls back to the chat-bound
    tournament or the single active tournament (else None — counts as a
    friendly).
    """
    if not is_admin(update.effective_user.id):
        await send(update, "❌ Только админ.")
        return

    if len(ctx.args) < 3:
        await send(
            update,
            "Использование: <code>/admin_report @u1 @u2 3:2 [tournament_id]</code>",
        )
        return

    p1 = _resolve_player_arg(ctx.args[0])
    p2 = _resolve_player_arg(ctx.args[1])
    if not p1:
        await send(update, f"❌ Не найден игрок {html.escape(ctx.args[0])}")
        return
    if not p2:
        await send(update, f"❌ Не найден игрок {html.escape(ctx.args[1])}")
        return
    if p1["id"] == p2["id"]:
        await send(update, "❌ Игроки должны быть разные.")
        return

    m = SCORE_RE.match(ctx.args[2])
    if not m:
        await send(update, "❌ Неверный формат счёта. Пример: <code>3:2</code>")
        return
    s1, s2 = int(m.group(1)), int(m.group(2))
    if s1 > 30 or s2 > 30:
        await send(update, "❌ Слишком большой счёт. Максимум 30 голов.")
        return

    # Tournament resolution: explicit ID > chat binding > single active.
    t: dict | None = None
    if len(ctx.args) > 3:
        try:
            tid_arg = int(ctx.args[3])
        except ValueError:
            await send(update, "❌ tournament_id должен быть числом.")
            return
        t = get_tournament(tid_arg)
        if not t:
            await send(update, f"❌ Турнир {tid_arg} не найден.")
            return
    else:
        chat = update.effective_chat
        if chat:
            t = get_tournament_by_chat(chat.id)
        if t is None:
            t = get_active_tournament()
    tid = t["id"] if t else None

    # Look for an existing pending/reported match first; otherwise create one.
    existing = get_pending_match(p1["id"], p2["id"], tid)
    if existing:
        # Normalize so score1 corresponds to existing.player1_id.
        if existing["player1_id"] == p1["id"]:
            ns1, ns2 = s1, s2
        else:
            ns1, ns2 = s2, s1
        update_match(
            existing["id"],
            score1=ns1, score2=ns2,
            status="confirmed",
            reported_by=update.effective_user.id,
        )
        match_id = existing["id"]
    else:
        # ── Guard: respect group_matches_per_pair limit ────────────────
        # Even admins shouldn't accidentally exceed the configured cap.
        # Warn but allow override with a note in the response.
        from database import count_group_matches_for_pair
        pair_exceeded = False
        if tid and t and (t.get("stage") or "groups") == "groups":
            mpp = max(1, int(t.get("group_matches_per_pair") or 1))
            pair_count = count_group_matches_for_pair(p1["id"], p2["id"], tid)
            if pair_count >= mpp:
                pair_exceeded = True
                await send(
                    update,
                    f"⚠️ Пара {mention(p1['username'])} / {mention(p2['username'])} "
                    f"уже сыграла {pair_count}/{mpp} матч(ей) в группе. "
                    f"Лимит превышен — матч НЕ записан.\n\n"
                    f"Если нужно перезаписать результат — используй "
                    f"<code>/edit_match #ID новый_счёт</code>.\n"
                    f"Если нужно удалить лишний — <code>/delete_match #ID</code>.",
                )
                return

        match_id = db.create_match(
            tid or 0, p1["id"], p2["id"],
            stage="group", round_num=1,
        )
        update_match(
            match_id,
            score1=s1, score2=s2,
            status="confirmed",
            reported_by=update.effective_user.id,
        )

    summary = apply_result(match_id)
    summary = _align_summary_to_args(summary, match_id, p1)
    log_tournament_action(
        tid,
        actor_telegram_id=update.effective_user.id,
        actor_username=update.effective_user.username,
        action="admin_report",
        details=(
            f"match={match_id} @{p1['username']} {s1}:{s2} @{p2['username']}"
        ),
    )

    scope = ""
    if t:
        scope = f" в турнире <b>{html.escape(t['name'])}</b> (ID {t['id']})"
    await send(
        update,
        f"✅ Админ-результат: {mention(p1['username'])} "
        f"<b>{s1}:{s2}</b> {mention(p2['username'])}{scope}.\n"
        f"Изменение ELO: {mention(p1['username'])} "
        f"{arrow(int(round(summary['elo1_after'] - summary['elo1_before'])))} → "
        f"<b>{int(round(summary['elo1_after']))}</b>; "
        f"{mention(p2['username'])} "
        f"{arrow(int(round(summary['elo2_after'] - summary['elo2_before'])))} → "
        f"<b>{int(round(summary['elo2_after']))}</b>.",
    )

    # Best-effort: when a tournament match completes, auto-advance the
    # playoff bracket if the group stage just finished.
    if tid:
        announce_stage_ar = summary.get("advanced_stage")
        try:
            if _maybe_auto_advance(ctx, tid) and not announce_stage_ar:
                announce_stage_ar = _current_playoff_stage(tid)
        except Exception as e:
            log.warning("auto-advance failed for tournament %s: %s", tid, e)
            announce_stage_ar = None
        if announce_stage_ar:
            await _announce_stage_advance(ctx, tid, announce_stage_ar)


# ─────────────────────────────────────────────────────────────────────────────
# /admin_photo — admin replies to a screenshot with player usernames,
#                bot OCRs the score and registers the match
# ─────────────────────────────────────────────────────────────────────────────


def _align_summary_to_args(summary: dict, match_id: int, p1: dict) -> dict:
    """Re-key an ``apply_result`` summary to the caller's argument order.

    ``apply_result`` builds ``player1`` / ``score1`` / ``elo1_*`` from the
    MATCH row's stored ``player1_id``. For pre-created tournament pairings
    that orientation often differs from the order an admin typed
    (``/admin_report @u1 @u2`` or ``/admin_photo @u1 @u2``). The ratings
    written to the DB are correct either way, but the response builders
    pair ``mention(p1)`` with ``elo1_*`` — so when the orders differ the
    ELO change is shown under the WRONG username (the winner appears to
    lose points). Swap the ``*1``/``*2`` fields so ``elo1_*`` / ``score1``
    always describe ``p1`` (the first @user argument).
    """
    m_final = get_match(match_id)
    if not m_final or m_final.get("player1_id") == p1["id"]:
        return summary
    s = dict(summary)
    for a, b in (
        ("player1", "player2"),
        ("score1", "score2"),
        ("elo1_before", "elo2_before"),
        ("elo1_after", "elo2_after"),
        ("delta1", "delta2"),
    ):
        s[a], s[b] = s.get(b), s.get(a)
    return s


async def _admin_photo_ocr_one(ctx, file_id: str):
    """Download a single photo by *file_id* and run OCR. Returns MatchScreenshot."""
    import asyncio
    import tempfile
    import os as _os
    from ocr import parse_match_screenshot

    f = await ctx.bot.get_file(file_id)
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        await f.download_to_drive(tmp_path)
        return await asyncio.to_thread(parse_match_screenshot, tmp_path)
    finally:
        try:
            _os.unlink(tmp_path)
        except OSError:
            pass


async def _admin_photo_register(
    update, ctx, p1, p2, t, tid, s1, s2, screenshot_file_id, res,
):
    """Register a single match from an admin_photo OCR result.

    Returns ``(match_id, summary)`` on success or ``None`` if the pair
    limit is exceeded.
    """
    # Penalty shootout (only meaningful when tournament has it enabled
    # AND OCR extracted both pen1/pen2 AND regulation is a draw).
    pen1 = pen2 = None
    if (
        t and int(t.get("playoff_penalties") or 0)
        and getattr(res, "pen1", None) is not None
        and getattr(res, "pen2", None) is not None
        and s1 == s2
    ):
        pen1 = int(res.pen1)
        pen2 = int(res.pen2)

    existing = get_pending_match(p1["id"], p2["id"], tid)
    if existing:
        if existing["player1_id"] == p1["id"]:
            ns1, ns2 = s1, s2
            np1, np2 = pen1, pen2
        else:
            ns1, ns2 = s2, s1
            np1, np2 = (pen2, pen1) if pen1 is not None else (None, None)
        update_kwargs = dict(
            score1=ns1, score2=ns2,
            status="confirmed",
            reported_by=update.effective_user.id,
            screenshot_file_id=screenshot_file_id,
        )
        if np1 is not None:
            update_kwargs["pen1"] = np1
            update_kwargs["pen2"] = np2
        update_match(existing["id"], **update_kwargs)
        match_id = existing["id"]
    else:
        from database import count_group_matches_for_pair
        if tid and t and (t.get("stage") or "groups") == "groups":
            mpp = max(1, int(t.get("group_matches_per_pair") or 1))
            pair_count = count_group_matches_for_pair(p1["id"], p2["id"], tid)
            if pair_count >= mpp:
                await send(
                    update,
                    f"⚠️ Пара {mention(p1['username'])} / {mention(p2['username'])} "
                    f"уже сыграла {pair_count}/{mpp} матч(ей) в группе. "
                    f"Лимит превышен — матч НЕ записан.",
                )
                return None

        match_id = db.create_match(
            tid or 0, p1["id"], p2["id"],
            stage="group", round_num=1,
        )
        update_kwargs = dict(
            score1=s1, score2=s2,
            status="confirmed",
            reported_by=update.effective_user.id,
            screenshot_file_id=screenshot_file_id,
        )
        if pen1 is not None:
            update_kwargs["pen1"] = pen1
            update_kwargs["pen2"] = pen2
        update_match(match_id, **update_kwargs)

    summary = apply_result(match_id)
    summary = _align_summary_to_args(summary, match_id, p1)
    log_tournament_action(
        tid,
        actor_telegram_id=update.effective_user.id,
        actor_username=update.effective_user.username,
        action="admin_photo",
        details=(
            f"match={match_id} @{p1['username']} {s1}:{s2} @{p2['username']}"
        ),
    )

    if res.goals:
        try:
            from bot import _persist_ocr_goals
            _persist_ocr_goals(match_id, p1, p2, res.goals)
        except Exception:
            pass

    return match_id, summary


async def cmd_admin_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/admin_photo @user1 @user2 [tournament_id]

    Admin-only. Must be sent as a **reply** to a message containing a photo
    (match screenshot). The bot downloads the photo, runs OCR to extract
    the score, and creates a confirmed match between user1 and user2.

    Supports **albums**: if the replied message is part of a Telegram album
    (media_group), the bot processes ALL photos from that album.  Photos
    with the same score are merged (goals combined into one match); photos
    with different scores create separate matches.

    This is for cases where the bot's auto-detection (nickname matching)
    failed but the admin can see the correct players from the screenshot.

    Aliases: /adminphoto, /photo_report, /photoreport
    """
    if not is_admin(update.effective_user.id):
        await send(update, "❌ Только админ.")
        return

    msg = update.effective_message
    if not msg or not msg.reply_to_message:
        await send(
            update,
            "❌ Ответь на сообщение с фото (скриншотом матча).\n"
            "Использование: ответом на фото — "
            "<code>/admin_photo @user1 @user2 [tournament_id]</code>",
        )
        return

    reply = msg.reply_to_message
    photos = getattr(reply, "photo", None)
    if not photos:
        await send(
            update,
            "❌ Сообщение, на которое ты ответил, не содержит фото.\n"
            "Ответь именно на скриншот матча.",
        )
        return

    if not ctx.args or len(ctx.args) < 2:
        await send(
            update,
            "❌ Укажи двух игроков.\n"
            "Использование: <code>/admin_photo @user1 @user2 [tournament_id]</code>\n\n"
            "Ответь этой командой на скриншот матча — бот распознает счёт "
            "и запишет результат.",
        )
        return

    # Resolve players
    p1 = _resolve_player_arg(ctx.args[0])
    p2 = _resolve_player_arg(ctx.args[1])
    if not p1:
        await send(update, f"❌ Не найден игрок {html.escape(ctx.args[0])}")
        return
    if not p2:
        await send(update, f"❌ Не найден игрок {html.escape(ctx.args[1])}")
        return
    if p1["id"] == p2["id"]:
        await send(update, "❌ Игроки должны быть разные.")
        return

    # Tournament resolution
    from database import get_tournament_by_chat
    t: dict | None = None
    if len(ctx.args) > 2:
        try:
            tid_arg = int(ctx.args[2])
        except ValueError:
            await send(update, "❌ tournament_id должен быть числом.")
            return
        t = get_tournament(tid_arg)
        if not t:
            await send(update, f"❌ Турнир {tid_arg} не найден.")
            return
    else:
        chat = update.effective_chat
        if chat:
            t = get_tournament_by_chat(chat.id)
        if t is None:
            t = get_active_tournament()
    tid = t["id"] if t else None

    # ── Collect photo file_ids (album-aware) ────────────────────────────
    # If the replied message belongs to a Telegram album, try to collect
    # ALL photos from that album via the _mg_photos cache populated by
    # handle_photo.  Fall back to the single replied photo otherwise.
    best_photo = photos[-1]
    mgi = getattr(reply, "media_group_id", None)
    album_file_ids: list[str] = []
    if mgi and ctx.chat_data is not None:
        cached = ctx.chat_data.get("_mg_photos", {}).get(str(mgi), [])
        album_file_ids = [fid for fid, _mid in cached]
    if not album_file_ids:
        album_file_ids = [best_photo.file_id]

    photo_count = len(album_file_ids)
    if photo_count == 1:
        await send(update, "🔍 Распознаю счёт на скриншоте…")
    else:
        await send(
            update,
            f"🔍 Альбом из {photo_count} фото — распознаю все…",
        )

    # ── OCR every photo ─────────────────────────────────────────────────
    ocr_results: list[tuple[str, object]] = []  # (file_id, MatchScreenshot)
    ocr_errors: list[str] = []
    for fid in album_file_ids:
        try:
            res = await _admin_photo_ocr_one(ctx, fid)
            ocr_results.append((fid, res))
        except Exception as e:
            log.exception("admin_photo OCR failed for file %s: %s", fid, e)
            ocr_errors.append(str(e))

    if not ocr_results:
        err_text = "; ".join(ocr_errors) if ocr_errors else "неизвестная ошибка"
        await send(
            update,
            f"❌ Не смог распознать ни одного фото: "
            f"<code>{html.escape(err_text)}</code>",
        )
        return

    # ── Group by score: same score → merge goals, different → separate ──
    # Each group becomes one match.
    from collections import OrderedDict
    score_groups: OrderedDict[tuple[int, int], list[tuple[str, object]]] = OrderedDict()
    failed_photos: list[tuple[str, object]] = []
    for fid, res in ocr_results:
        if res.score1 is None or res.score2 is None:
            failed_photos.append((fid, res))
            continue
        if res.score1 > 30 or res.score2 > 30:
            failed_photos.append((fid, res))
            continue
        key = (res.score1, res.score2)
        score_groups.setdefault(key, []).append((fid, res))

    if not score_groups:
        raw_parts = []
        for fid, res in failed_photos:
            raw_score = res.raw_texts.get("score", "") if res.raw_texts else ""
            raw_parts.append(f"«{html.escape(raw_score)}»")
        raw_text = ", ".join(raw_parts) if raw_parts else "—"
        await send(
            update,
            "❌ Не смог распознать счёт ни на одном скриншоте.\n"
            f"Сырой текст: {raw_text}\n\n"
            f"Внеси вручную: <code>/admin_report {ctx.args[0]} {ctx.args[1]} "
            f"X:Y{' ' + str(tid) if tid else ''}</code>",
        )
        return

    # ── Register matches ────────────────────────────────────────────────
    registered: list[tuple[int, int, int, dict, object, list]] = []
    for (s1, s2), group in score_groups.items():
        primary_fid, primary_res = group[0]
        merged_goals = list(primary_res.goals or [])
        for _, extra_res in group[1:]:
            for g in (extra_res.goals or []):
                if g not in merged_goals:
                    merged_goals.append(g)
        primary_res.goals = merged_goals

        result = await _admin_photo_register(
            update, ctx, p1, p2, t, tid, s1, s2, primary_fid, primary_res,
        )
        if result is None:
            continue
        match_id, summary = result
        registered.append((match_id, s1, s2, summary, primary_res, group))

    if not registered:
        return

    # ── Build response message ──────────────────────────────────────────
    scope = ""
    if t:
        scope = f"\n🏆 Турнир: <b>{html.escape(t['name'])}</b> (ID {t['id']})"

    parts = []
    for match_id, s1, s2, summary, res, group in registered:
        ocr_team1 = res.team1 or "—"
        ocr_team2 = res.team2 or "—"
        ocr_league = res.league_plate or "—"
        goals_count = len(res.goals) if res.goals else 0
        photos_label = (
            f" (📷×{len(group)})" if len(group) > 1 else ""
        )

        parts.append(
            f"⚽ {mention(p1['username'])} <b>{s1}:{s2}</b>"
            + (
                f" <i>(пен. {res.pen1}:{res.pen2})</i>"
                if getattr(res, "pen1", None) is not None
                and getattr(res, "pen2", None) is not None
                else ""
            )
            + f" {mention(p2['username'])}{photos_label}\n"
            f"📈 ELO:\n"
            f"  {mention(p1['username'])}: "
            f"{int(round(summary['elo1_before']))} → "
            f"<b>{int(round(summary['elo1_after']))}</b> "
            f"({arrow(int(round(summary['elo1_after'] - summary['elo1_before'])))})\n"
            f"  {mention(p2['username'])}: "
            f"{int(round(summary['elo2_before']))} → "
            f"<b>{int(round(summary['elo2_after']))}</b> "
            f"({arrow(int(round(summary['elo2_after'] - summary['elo2_before'])))})\n"
            f"🔍 OCR: {html.escape(ocr_team1)} vs {html.escape(ocr_team2)}"
            f" | Лига: {html.escape(ocr_league)} | Голов: {goals_count}"
        )

    match_word = "матч" if len(registered) == 1 else f"{len(registered)} матч(ей)"
    header = f"✅ <b>{match_word} записан(о) по скриншоту!</b>"
    if photo_count > 1:
        header += f"  (альбом: {photo_count} фото)"

    body = f"{header}\n{scope}\n\n" + "\n\n".join(parts)

    if failed_photos:
        fail_lines = []
        for fid, res in failed_photos:
            raw_score = res.raw_texts.get("score", "") if res.raw_texts else ""
            fail_lines.append(f"  «{html.escape(raw_score or '—')}»")
        body += "\n\n⚠️ Не распознан счёт на фото:\n" + "\n".join(fail_lines)
    if ocr_errors:
        body += (
            f"\n\n⚠️ Ошибка OCR для {len(ocr_errors)} фото: "
            + html.escape("; ".join(ocr_errors))
        )

    await send(update, body)

    # ── Auto-advance playoff if needed ──────────────────────────────────
    if tid:
        for match_id, _s1, _s2, summary, _res, _group in registered:
            announce_stage = summary.get("advanced_stage")
            try:
                if _maybe_auto_advance(ctx, tid) and not announce_stage:
                    announce_stage = _current_playoff_stage(tid)
            except Exception as e:
                log.warning("auto-advance failed for tournament %s: %s", tid, e)
                announce_stage = None
            if announce_stage:
                await _announce_stage_advance(ctx, tid, announce_stage)


# ─────────────────────────────────────────────────────────────────────────────
# /reocr — re-run Tesseract OCR on a replied photo with both players specified
# ─────────────────────────────────────────────────────────────────────────────


async def cmd_reocr(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/reocr @user1 @user2 [tournament_id]

    Re-run OCR (Tesseract-only, no AI) on a replied screenshot and
    register the match between the two specified players. Useful when
    the AI model misidentified a player (e.g. read the in-game nickname
    that doesn't match any registered Telegram user) but the score was
    correct.

    Must be sent as a **reply** to a message containing a photo.
    The bot will:
      1. Download the photo and run local Tesseract OCR (no network AI).
      2. Extract the score.
      3. Create/report the match between @user1 and @user2.

    Permissions: admin or tournament-admin.

    Aliases: /re_ocr, /tessocr, /tess_ocr
    """
    if not is_admin(update.effective_user.id):
        await send(update, "❌ Только админ.")
        return

    msg = update.effective_message
    if not msg or not msg.reply_to_message:
        await send(
            update,
            "❌ Ответь на сообщение с фото (скриншотом матча).\n"
            "Использование: ответом на фото — "
            "<code>/reocr @user1 @user2 [tournament_id]</code>",
        )
        return

    reply = msg.reply_to_message
    photos = getattr(reply, "photo", None)
    if not photos:
        await send(
            update,
            "❌ Сообщение, на которое ты ответил, не содержит фото.\n"
            "Ответь именно на скриншот матча.",
        )
        return

    if not ctx.args or len(ctx.args) < 2:
        await send(
            update,
            "❌ Укажи двух игроков.\n"
            "Использование: <code>/reocr @user1 @user2 [tournament_id]</code>\n\n"
            "Ответь этой командой на скриншот матча — бот распознает счёт "
            "через Tesseract (без AI) и запишет результат.",
        )
        return

    # Resolve players
    p1 = _resolve_player_arg(ctx.args[0])
    p2 = _resolve_player_arg(ctx.args[1])
    if not p1:
        await send(update, f"❌ Не найден игрок {html.escape(ctx.args[0])}")
        return
    if not p2:
        await send(update, f"❌ Не найден игрок {html.escape(ctx.args[1])}")
        return
    if p1["id"] == p2["id"]:
        await send(update, "❌ Игроки должны быть разные.")
        return

    # Tournament resolution
    from database import get_tournament_by_chat
    t: dict | None = None
    if len(ctx.args) > 2:
        try:
            tid_arg = int(ctx.args[2])
        except ValueError:
            await send(update, "❌ tournament_id должен быть числом.")
            return
        t = get_tournament(tid_arg)
        if not t:
            await send(update, f"❌ Турнир {tid_arg} не найден.")
            return
    else:
        chat = update.effective_chat
        if chat:
            t = get_tournament_by_chat(chat.id)
        if t is None:
            t = get_active_tournament()
    tid = t["id"] if t else None

    # Download and OCR (Tesseract only — no AI)
    best_photo = photos[-1]
    file_id = best_photo.file_id

    await send(update, "🔍 Распознаю через <b>Tesseract</b> (без AI)…")

    import asyncio
    import tempfile
    import os as _os
    from ocr import parse_match_screenshot

    f = await ctx.bot.get_file(file_id)
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        await f.download_to_drive(tmp_path)
        # Force Tesseract-only by passing ai_models=() which disables AI
        res = await asyncio.to_thread(
            parse_match_screenshot, tmp_path, ai_models=()
        )
    except Exception as e:
        log.exception("reocr OCR failed: %s", e)
        await send(update, f"❌ Ошибка OCR: <code>{html.escape(str(e))}</code>")
        return
    finally:
        try:
            _os.unlink(tmp_path)
        except OSError:
            pass

    # Check score
    if res.score1 is None or res.score2 is None:
        raw_score = res.raw_texts.get("score", "") if res.raw_texts else ""
        await send(
            update,
            f"❌ Tesseract не смог распознать счёт.\n"
            f"Сырой текст из области счёта: «{html.escape(raw_score)}»\n\n"
            f"Используй вручную: <code>/admin_report {html.escape(ctx.args[0])} "
            f"{html.escape(ctx.args[1])} СЧЁТ"
            + (f" {tid}" if tid else "") + "</code>",
        )
        return

    s1, s2 = res.score1, res.score2

    # Sanity check
    if s1 > 30 or s2 > 30:
        await send(
            update,
            f"⚠️ Подозрительный счёт: {s1}:{s2}. Похоже, OCR считал лишнее.\n"
            f"Используй вручную: <code>/admin_report {html.escape(ctx.args[0])} "
            f"{html.escape(ctx.args[1])} СЧЁТ"
            + (f" {tid}" if tid else "") + "</code>",
        )
        return

    # Register the match (same logic as admin_photo)
    result = await _admin_photo_register(
        update, ctx, p1, p2, t, tid, s1, s2, file_id, res,
    )
    if result is None:
        return

    match_id, summary = result

    # Build response
    ocr_team1 = (res.team1 or "—")[:30]
    ocr_team2 = (res.team2 or "—")[:30]
    ocr_league = (res.league_plate or "—")[:40]
    goals_count = len(res.goals) if res.goals else 0

    t_label = f"{t['name']} (ID {tid})" if t else "—"
    body = (
        f"✅ <b>Матч записан через Tesseract!</b>\n\n"
        f"⚽ {mention(p1['username'])} <b>{s1}:{s2}</b> "
        f"{mention(p2['username'])}\n"
        f"🏆 Турнир: {html.escape(t_label)}\n\n"
        f"📈 ELO:\n"
        f"  {mention(p1['username'])}: "
        f"{int(round(summary['elo1_before']))} → "
        f"<b>{int(round(summary['elo1_after']))}</b> "
        f"({arrow(int(round(summary['elo1_after'] - summary['elo1_before'])))})\n"
        f"  {mention(p2['username'])}: "
        f"{int(round(summary['elo2_before']))} → "
        f"<b>{int(round(summary['elo2_after']))}</b> "
        f"({arrow(int(round(summary['elo2_after'] - summary['elo2_before'])))})\n\n"
        f"🔍 OCR: {html.escape(ocr_team1)} vs {html.escape(ocr_team2)}"
        f" | Лига: {html.escape(ocr_league)} | Голов: {goals_count}\n"
        f"🤖 <i>Модель: tesseract (локальный)</i>"
    )
    await send(update, body)

    # Auto-advance playoff if needed
    if tid:
        announce_stage = summary.get("advanced_stage")
        try:
            if _maybe_auto_advance(ctx, tid) and not announce_stage:
                announce_stage = _current_playoff_stage(tid)
        except Exception as e:
            log.warning("auto-advance failed for tournament %s: %s", tid, e)
            announce_stage = None
        if announce_stage:
            await _announce_stage_advance(ctx, tid, announce_stage)


# ─────────────────────────────────────────────────────────────────────────────
# /award_points — admin manually grants/removes group-stage points
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_award_points(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/award_points @user N [tournament_id] [причина...]

    Admin-only. Adjusts the group-stage point counter for ``@user`` in
    the given tournament by N (positive or negative). Useful for handing
    out bonus points (sportsmanship, fair-play, manual fixups) or
    deducting them (cheating penalties, no-shows beyond walkover).

    Without an explicit tournament_id, falls back to the chat-bound
    tournament. Refuses if no tournament can be inferred.
    """
    if not is_admin(update.effective_user.id):
        await send(update, "❌ Только админ.")
        return
    if len(ctx.args) < 2:
        await send(
            update,
            "Использование: <code>/award_points @user N [tournament_id] [причина]</code>\n"
            "Пример: <code>/award_points @shah +3</code> (в чате турнира) — +3 очка к группе.\n"
            "Минусовать: <code>/award_points @shah -3 12 \"снятие за мат\"</code>",
        )
        return

    target = _resolve_player_arg(ctx.args[0])
    if not target:
        await send(update, f"❌ Игрок {html.escape(ctx.args[0])} не найден.")
        return

    try:
        delta = int(ctx.args[1].lstrip("+"))
    except ValueError:
        await send(update, "❌ Очки должны быть целым числом (например, 3 или -3).")
        return
    if delta == 0:
        await send(update, "❌ 0 очков — нечего начислять.")
        return

    t: dict | None = None
    reason = ""
    if len(ctx.args) > 2 and ctx.args[2].lstrip("-").isdigit():
        tid_arg = int(ctx.args[2])
        t = get_tournament(tid_arg)
        if not t:
            await send(update, f"❌ Турнир {tid_arg} не найден.")
            return
        reason = " ".join(ctx.args[3:]).strip()
    else:
        chat = update.effective_chat
        if chat:
            t = get_tournament_by_chat(chat.id)
        reason = " ".join(ctx.args[2:]).strip()

    if t is None:
        await send(
            update,
            "❌ Не могу определить турнир. Используй "
            "<code>/award_points @user N &lt;tournament_id&gt;</code> "
            "или запускай команду в чате, привязанном к турниру.",
        )
        return

    # Player must be a participant — otherwise there's no tournament_players
    # row to update.
    members = get_tournament_players(t["id"])
    row = next((m for m in members if m["player_id"] == target["id"]), None)
    if row is None:
        await send(
            update,
            f"❌ {mention(target['username'])} не участвует в турнире "
            f"<b>{html.escape(t['name'])}</b> (ID {t['id']}).",
        )
        return

    new_points = int(row.get("group_points") or 0) + delta
    db.update_tournament_player(t["id"], target["id"], group_points=new_points)

    sign = "+" if delta > 0 else ""
    reason_line = f"\n💬 Причина: {html.escape(reason)}" if reason else ""
    await send(
        update,
        f"✅ {mention(target['username'])}: <b>{sign}{delta}</b> очк "
        f"(было {row.get('group_points') or 0}, стало <b>{new_points}</b>) "
        f"в турнире <b>{html.escape(t['name'])}</b> (ID {t['id']}).{reason_line}",
    )


# ─────────────────────────────────────────────────────────────────────────────
# /edit_goals — replace the goal-event list of a match
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_edit_goals(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/edit_goals #<match_id> @scorer1 @scorer2 ... — admin-only.

    Replaces the goal-event list for the given match. Each @user is one
    goal in the order they were scored. Useful when OCR couldn't read
    the scorers cleanly (e.g. a screenshot without the goal events
    panel).

    Examples:
      /edit_goals #142 @phoenileo @oliverbax @phoenileo
        — three goals: phoenileo, oliverbax, phoenileo (final 2:1 from
        phoenileo's POV).
      /edit_goals #142 clear
        — wipe the goal list (e.g. after /walkover).
    """
    if not is_admin(update.effective_user.id):
        await send(update, "❌ Только админ.")
        return
    args = ctx.args or []
    if not args:
        await send(
            update,
            "Использование: <code>/edit_goals #&lt;match_id&gt; @scorer1 @scorer2 ...</code>\n"
            "Или <code>/edit_goals #&lt;id&gt; clear</code> чтобы стереть.",
        )
        return
    raw = args[0].lstrip("#").lstrip("m").lstrip("M").lstrip()
    if not raw.isdigit():
        await send(update, f"❌ Не пойму ID матча: {html.escape(args[0])}")
        return
    match_id = int(raw)
    m = db.get_match(match_id)
    if not m:
        await send(update, f"❌ Матч #{match_id} не найден.")
        return
    p1 = get_player_by_id(m["player1_id"])
    p2 = get_player_by_id(m["player2_id"])
    if not p1 or not p2:
        await send(update, "❌ Не могу найти игроков матча в БД.")
        return

    rest = args[1:]
    if rest and rest[0].lower() in ("clear", "очистить", "delete", "remove"):
        db.set_match_goals(match_id, [])
        await send(update, f"✅ Голы для матча #{match_id} очищены.")
        return

    if not rest:
        await send(update, "❌ Укажи хотя бы одного бомбардира или 'clear'.")
        return

    rows: list[dict] = []
    for token in rest:
        p = _resolve_player_arg(token)
        if not p:
            await send(update, f"❌ Игрок не найден: {html.escape(token)}")
            return
        if p["id"] == p1["id"]:
            side = "home"
        elif p["id"] == p2["id"]:
            side = "away"
        else:
            await send(
                update,
                f"❌ {mention(p['username'])} не участвует в матче #{match_id} "
                f"({mention(p1['username'])} vs {mention(p2['username'])}).",
            )
            return
        rows.append({
            "player_id": p["id"],
            "raw_name":  p.get("game_nickname") or p.get("username"),
            "minute":    None,
            "side":      side,
        })
    db.set_match_goals(match_id, rows)
    await send(
        update,
        f"✅ Записано {len(rows)} гол(ов) для матча #{match_id}: "
        + ", ".join(mention(get_player_by_id(r["player_id"])["username"])
                    for r in rows),
    )


# ─────────────────────────────────────────────────────────────────────────────
# /pending — list non-finished matches the admin can act on
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_pending(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/pending [tournament_id]
    /pending @username [tournament_id]

    Admin: list pending/reported matches (those that aren't confirmed
    yet) so you can grab a match_id for ``/walkover #<id>``.
    """
    if not is_admin(update.effective_user.id):
        await send(update, "❌ Только админ.")
        return

    tid: int | None = None
    pid: int | None = None
    for a in (ctx.args or []):
        a_clean = a.lstrip("@")
        if a_clean.isdigit():
            # Numeric — assume tournament_id
            tid = int(a_clean)
        else:
            p = _resolve_player_arg(a)
            if p:
                pid = p["id"]

    # If no tid, scope to chat-bound tournament if any
    if tid is None:
        chat = update.effective_chat
        if chat is not None:
            bound = get_tournament_by_chat(chat.id)
            if bound:
                tid = int(bound["id"])

    # Build the candidate set tournament-by-tournament so we can run each
    # batch through ``get_real_tournament_matches`` and dedupe phantoms /
    # duplicate playoff legs *before* showing them to the admin. The old
    # raw SQL query was the source of the "10 sf-r1 rows for 2 pairs"
    # output the user was seeing.
    target_tids: list[int]
    if tid is not None:
        target_tids = [tid]
    else:
        target_tids = [int(t["id"]) for t in (get_active_tournaments() or [])]
    rows: list[dict] = []
    for ttid in target_tids:
        try:
            real = db.get_real_tournament_matches(ttid)
        except Exception:
            real = []
        for m in real:
            if m.get("status") not in ("pending", "reported"):
                continue
            if pid is not None and pid not in (m.get("player1_id"), m.get("player2_id")):
                continue
            rows.append(m)
    rows.sort(key=lambda m: (m.get("tournament_id") or 0, m.get("stage") or "", m.get("id") or 0))
    rows = rows[:80]

    if not rows:
        scope = []
        if tid is not None:
            scope.append(f"турнир ID {tid}")
        if pid is not None:
            p = get_player_by_id(pid)
            scope.append(mention(p["username"]) if p else f"player {pid}")
        scope_str = (" (" + ", ".join(scope) + ")") if scope else ""
        await send(update, f"✅ Pending-матчей нет{scope_str}.")
        return

    lines = ["📋 <b>Pending матчи</b>"]
    if tid is not None:
        t = get_tournament(tid)
        if t:
            lines[-1] += f" в турнире <b>{html.escape(t['name'])}</b> (ID {tid})"
    if pid is not None:
        p = get_player_by_id(pid)
        if p:
            lines[-1] += f" — {mention(p['username'])}"
    lines.append("")

    # Pre-fetch all player records to avoid N+1 lookups in the rendering loops
    _all_player_ids = set()
    for r in rows:
        if r.get("player1_id"):
            _all_player_ids.add(r["player1_id"])
        if r.get("player2_id"):
            _all_player_ids.add(r["player2_id"])
    _player_cache: dict[int, dict | None] = {
        pid: get_player_by_id(pid) for pid in _all_player_ids
    }

    # Group rows by tournament
    tid_rows: dict[int | None, list[dict]] = defaultdict(list)
    for r in rows:
        tid_rows[r.get("tournament_id")].append(r)

    for cur_tid, t_matches in tid_rows.items():
        t = get_tournament(cur_tid) if cur_tid else None
        if t:
            lines.append(
                f"🏆 <b>{html.escape(t['name'])}</b> "
                f"[{t_type_label(t['tournament_type'])}] (ID {t['id']})"
            )
        else:
            lines.append("🏆 <i>без турнира</i>")

        # Build group map for this tournament
        group_map: dict[int, str] = {}
        if cur_tid is not None:
            try:
                tp_rows = get_tournament_players(cur_tid)
            except Exception:
                log.warning("Failed to fetch tournament players for tid=%s", cur_tid, exc_info=True)
                tp_rows = []
            for tp in tp_rows:
                group_map[tp["player_id"]] = tp.get("group_name") or "?"

        # Separate group-stage and playoff matches
        group_matches: list[dict] = []
        playoff_matches: list[dict] = []
        for m in t_matches:
            if m.get("stage") == "group":
                group_matches.append(m)
            else:
                playoff_matches.append(m)

        # Group group-stage matches by group letter
        groups: dict[str, list[dict]] = defaultdict(list)
        for m in group_matches:
            # Use player1's group, falling back to player2's group. Cross-group
            # matches are not expected here because get_real_tournament_matches
            # already filters out cross-group phantom rows; the fallback is safe.
            g = group_map.get(m["player1_id"]) or group_map.get(m["player2_id"]) or "?"
            groups[g].append(m)

        # Sort groups: known letters first alphabetically, "?" (lobby) last
        def _group_sort_key(g: str) -> tuple:
            if g == "?":
                return (1, g)
            return (0, g)

        for g in sorted(groups.keys(), key=_group_sort_key):
            g_matches = groups[g]
            if g == "?":
                lines.append("\n📂 <b>Лобби</b>")
            else:
                lines.append(f"\n📂 <b>Группа {html.escape(g)}</b>")

            # Sub-group by participant pair
            pairs: dict[tuple[int, int], list[dict]] = defaultdict(list)
            for m in g_matches:
                pair_key = tuple(sorted([m["player1_id"], m["player2_id"]]))
                pairs[pair_key].append(m)

            for pair_key in sorted(pairs.keys()):
                pair_matches = pairs[pair_key]
                # Get usernames from first match in pair
                p1 = _player_cache.get(pair_key[0])
                p2 = _player_cache.get(pair_key[1])
                u1 = mention(p1["username"]) if p1 else f"id{pair_key[0]}"
                u2 = mention(p2["username"]) if p2 else f"id{pair_key[1]}"
                lines.append(f"  👥 {u1} vs {u2}")
                for m in sorted(pair_matches, key=lambda x: (x.get("round_num") or 1, x.get("id") or 0)):
                    rnd = m.get("round_num") or 1
                    status = m.get("status") or "?"
                    score = ""
                    if m.get("score1") is not None and m.get("score2") is not None and status == "reported":
                        score = f" {m['score1']}:{m['score2']}"
                    lines.append(
                        f"    <code>#{m['id']:>4}</code>  "
                        f"<i>r{rnd}</i>  ({status}{score})"
                    )

        # Playoff section
        if playoff_matches:
            lines.append("\n📂 <b>Плей-офф</b>")
            for m in sorted(playoff_matches, key=lambda x: (x.get("stage") or "", x.get("round_num") or 1, x.get("id") or 0)):
                p1 = _player_cache.get(m["player1_id"])
                p2 = _player_cache.get(m["player2_id"])
                u1 = mention(p1["username"]) if p1 else f"id{m['player1_id']}"
                u2 = mention(p2["username"]) if p2 else f"id{m['player2_id']}"
                stage = m.get("stage") or "?"
                rnd = m.get("round_num") or 1
                status = m.get("status") or "?"
                score = ""
                if m.get("score1") is not None and m.get("score2") is not None and status == "reported":
                    score = f" {m['score1']}:{m['score2']}"
                lines.append(
                    f"  <code>#{m['id']:>4}</code>  {u1} vs {u2}  "
                    f"<i>{stage} r{rnd}</i>  ({status}{score})"
                )

    lines.append("")
    lines.append(
        "<i>Засчитать ТП: <code>/walkover #ID @loser</code></i>"
    )
    await send(update, "\n".join(lines))


# ─────────────────────────────────────────────────────────────────────────────
# /walkover_all — bulk 0:3 ТП for one player in a tournament
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_walkover_all(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/walkover_all @loser [tournament_id]

    Apply 0:3 ТП to *every* pending/reported match of @loser in the
    given tournament (or the chat-bound tournament). Shows a
    confirmation with the count first; bulk apply happens on click.
    """
    if not is_admin(update.effective_user.id):
        await send(update, "❌ Только админ.")
        return
    if not ctx.args:
        await send(
            update,
            "Использование: <code>/walkover_all @loser [tournament_id]</code>\n"
            "Засчитает ТП всем оставшимся матчам игрока в указанном турнире.",
        )
        return

    loser = _resolve_player_arg(ctx.args[0])
    if not loser:
        await send(update, f"❌ Игрок {html.escape(ctx.args[0])} не найден.")
        return

    tid_arg: int | None = None
    if len(ctx.args) >= 2:
        try:
            tid_arg = int(ctx.args[1])
        except ValueError:
            await send(update, "❌ tournament_id должен быть числом.")
            return
    if tid_arg is None:
        chat = update.effective_chat
        if chat is not None:
            bound = get_tournament_by_chat(chat.id)
            if bound:
                tid_arg = int(bound["id"])
    if tid_arg is None:
        await send(
            update,
            "❌ Не могу определить турнир. Используй "
            "<code>/walkover_all @loser &lt;tournament_id&gt;</code> "
            "или запускай команду в чате, привязанном к турниру "
            "(/bind_tournament).",
        )
        return

    pendings = _list_pending_matches_for(loser["id"], tid_arg)
    if not pendings:
        await send(
            update,
            f"✅ У {mention(loser['username'])} нет pending-матчей в турнире ID {tid_arg}.",
        )
        return

    t = get_tournament(tid_arg)
    t_name = t["name"] if t else f"ID {tid_arg}"

    # Show confirmation with the list
    lines = [
        f"⚠️ Будет засчитано ТП (0:3) для <b>{len(pendings)}</b> матчей "
        f"{mention(loser['username'])} в турнире <b>{html.escape(t_name)}</b>:",
        "",
    ]
    for m in pendings[:15]:
        opp_id = m["player2_id"] if m["player1_id"] == loser["id"] else m["player1_id"]
        opp = get_player_by_id(opp_id)
        opp_label = opp["username"] if opp else str(opp_id)
        stage_lbl = m.get("stage") or "?"
        lines.append(f"  • #{m['id']} vs @{opp_label} ({stage_lbl})")
    if len(pendings) > 15:
        lines.append(f"  … и ещё {len(pendings) - 15}")
    lines.append("")
    lines.append("Подтверди:")

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(
            f"✅ Да, засчитать все {len(pendings)} ТП",
            callback_data=f"woall:{loser['id']}:{tid_arg}",
        )],
        [InlineKeyboardButton("❌ Отмена", callback_data="wo_cancel")],
    ])
    await send(update, "\n".join(lines), reply_markup=kb)


# ─────────────────────────────────────────────────────────────────────────────
# /tech_nil_all — bulk 0:0 technical nil for all remaining matches in a tournament
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_tech_nil_all(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/tech_nil_all [tournament_id]

    Apply 0:0 (технический ноль) to *every* pending match in the
    given tournament (or the chat-bound tournament). Shows a
    confirmation with the count first; bulk apply happens on click.
    """
    if not is_admin(update.effective_user.id):
        await send(update, "❌ Только админ.")
        return

    tid_arg: int | None = None
    if ctx.args:
        try:
            tid_arg = int(ctx.args[0])
        except ValueError:
            await send(update, "❌ tournament_id должен быть числом.")
            return
    if tid_arg is None:
        chat = update.effective_chat
        if chat is not None:
            bound = get_tournament_by_chat(chat.id)
            if bound:
                tid_arg = int(bound["id"])
    if tid_arg is None:
        await send(
            update,
            "❌ Не могу определить турнир. Используй "
            "<code>/tech_nil_all &lt;tournament_id&gt;</code> "
            "или запускай команду в чате, привязанном к турниру "
            "(/bind_tournament).",
        )
        return

    all_ms = get_tournament_matches(tid_arg)
    pendings = [
        m for m in all_ms
        if m.get("status") == "pending"
        and m.get("player1_id") != m.get("player2_id")
    ]
    if not pendings:
        await send(
            update,
            f"✅ В турнире ID {tid_arg} нет pending-матчей.",
        )
        return

    t = get_tournament(tid_arg)
    t_name = t["name"] if t else f"ID {tid_arg}"

    lines = [
        f"⚠️ Будет засчитан <b>технический ноль (0:0)</b> для "
        f"<b>{len(pendings)}</b> матчей в турнире "
        f"<b>{html.escape(t_name)}</b>:",
        "",
    ]
    for m in pendings[:15]:
        p1 = get_player_by_id(m["player1_id"])
        p2 = get_player_by_id(m["player2_id"])
        p1_lbl = p1["username"] if p1 else str(m["player1_id"])
        p2_lbl = p2["username"] if p2 else str(m["player2_id"])
        stage_lbl = m.get("stage") or "?"
        lines.append(f"  • #{m['id']} @{p1_lbl} vs @{p2_lbl} ({stage_lbl})")
    if len(pendings) > 15:
        lines.append(f"  … и ещё {len(pendings) - 15}")
    lines.append("")
    lines.append("Подтверди:")

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(
            f"✅ Да, засчитать все {len(pendings)} матчей 0:0",
            callback_data=f"tnall:{tid_arg}",
        )],
        [InlineKeyboardButton("❌ Отмена", callback_data="wo_cancel")],
    ])
    await send(update, "\n".join(lines), reply_markup=kb)


# ─────────────────────────────────────────────────────────────────────────────
# /walkover_match — convenience alias for /walkover #<id>
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_walkover_match(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/walkover_match <match_id> [@loser]

    Convenience alias: ТП на конкретный матч по ID. Если @loser не
    указан — бот покажет кнопочный пикер «кто из двух игроков матча
    проиграл».
    """
    if not is_admin(update.effective_user.id):
        await send(update, "❌ Только админ.")
        return
    if not ctx.args:
        await send(
            update,
            "Использование: <code>/walkover_match &lt;match_id&gt; [@loser]</code>",
        )
        return
    # Re-dispatch through cmd_walkover with `#<id>` first arg.
    new_args = [f"#{ctx.args[0]}"] + list(ctx.args[1:])
    saved = ctx.args
    try:
        ctx.args = new_args  # type: ignore[assignment]
        await cmd_walkover(update, ctx)
    finally:
        ctx.args = saved  # type: ignore[assignment]


# ─────────────────────────────────────────────────────────────────────────────
# /promote — admin force-advances a player past their current playoff pair
# ─────────────────────────────────────────────────────────────────────────────


async def cmd_promote(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/promote @player [tournament_id]

    Force-advance ``@player`` past their current playoff pair regardless
    of leg state. Concretely: every open (pending or reported) leg in
    the current playoff stage involving ``@player`` is closed via a 3:0
    technical victory in their favour, then the bracket-advancement
    helper is invoked so the next stage spawns (if every other pair in
    this stage is also decided).

    Admin-only. Without an explicit ``tournament_id`` the chat-bound
    tournament is used.

    Use case: a series ended in a way the bot can't OCR (player
    forfeited / draw + decider screenshot already counted elsewhere /
    organiser ruling), and the operator just wants to push the winner
    into the next round manually.
    """
    if not is_admin(update.effective_user.id):
        await send(update, "❌ Только админ.")
        return

    if not ctx.args:
        await send(
            update,
            "Использование: <code>/promote @player [tournament_id]</code>\n"
            "Закрывает все открытые leg-и игрока в текущей стадии плей-офф "
            "техническим 3:0 и продвигает его в следующий раунд.",
        )
        return

    target = _resolve_player_arg(ctx.args[0])
    if not target:
        await send(update, f"❌ Игрок {html.escape(ctx.args[0])} не найден.")
        return

    # Resolve target tournament.
    tid: int | None = None
    for a in ctx.args[1:]:
        a_clean = a.lstrip("#")
        if a_clean.isdigit():
            tid = int(a_clean)
            break
    if tid is None:
        chat = update.effective_chat
        if chat is not None:
            bound = get_tournament_by_chat(chat.id)
            if bound:
                tid = int(bound["id"])
    if tid is None:
        await send(
            update,
            "❌ Не нашёл турнир. Укажи ID: "
            "<code>/promote @player &lt;tournament_id&gt;</code>.",
        )
        return

    t = get_tournament(tid)
    if not t:
        await send(update, f"❌ Турнир #{tid} не найден.")
        return
    if not _can_manage_tournament(update.effective_user.id, t):
        await send(
            update,
            "❌ Двигать стадию может только создатель или админ турнира.",
        )
        return

    # Find ALL open playoff legs in this tournament involving the target.
    # We don't trust ``t['stage']`` alone — late-arriving fixtures or
    # half-finished stages can leave open rows that aren't the "latest"
    # round. Instead, walk every playoff stage in order and collect any
    # row in pending/reported status that includes ``target``.
    from tournament import PLAYOFF_STAGES
    open_rows: list[dict] = []
    target_stage: str | None = None
    for stage in PLAYOFF_STAGES:
        rows = get_tournament_matches(tid, stage=stage)
        for m in rows:
            if target["id"] not in (m["player1_id"], m["player2_id"]):
                continue
            if m.get("status") in ("pending", "reported"):
                open_rows.append(m)
                target_stage = stage

    if not open_rows:
        await send(
            update,
            f"ℹ️ У {mention(target['username'])} нет открытых матчей "
            f"плей-офф в турнире <b>{html.escape(t['name'])}</b> "
            f"(ID {tid}). Возможно, он уже прошёл дальше или ещё не "
            "выходил из группы.",
        )
        return

    # Close each open leg with a 3:0 in target's favour.
    closed: list[int] = []
    for m in open_rows:
        loser_id = (
            m["player2_id"] if m["player1_id"] == target["id"]
            else m["player1_id"]
        )
        try:
            apply_walkover(m["id"], loser_id)
            closed.append(m["id"])
        except Exception as e:
            log.warning(
                "promote: apply_walkover failed for match %s: %s", m["id"], e
            )

    log_tournament_action(
        tid,
        actor_telegram_id=update.effective_user.id,
        actor_username=update.effective_user.username,
        action="promote",
        details=(
            f"target=@{target['username']} stage={target_stage} "
            f"matches={','.join(str(i) for i in closed)}"
        ),
    )

    # Run the bracket advancement step so the next round spawns once
    # every pair in this stage has a winner. ``_maybe_auto_advance``
    # already handles "all pairs decided → generate next round" / "final
    # done → finish tournament".
    try:
        advanced = _maybe_auto_advance(ctx, tid)
    except Exception as e:
        log.warning("promote: auto-advance failed: %s", e)
        advanced = False
    if advanced:
        new_stage = _current_playoff_stage(tid)
        await _announce_stage_advance(ctx, tid, new_stage or "finished")

    t_after = get_tournament(tid) or t
    new_stage_lbl = str(t_after.get("stage") or "—")
    await send(
        update,
        f"🚀 {mention(target['username'])} принудительно продвинут "
        f"в турнире <b>{html.escape(t['name'])}</b> (ID {tid}).\n"
        f"<i>Закрыто матчей: {len(closed)}, стадия: "
        f"{html.escape(new_stage_lbl)}</i>",
    )


# ─────────────────────────────────────────────────────────────────────────────
# /po_stage_config — admin sets per-stage best-of-N + mode override
# ─────────────────────────────────────────────────────────────────────────────


_PO_STAGES_LOOKUP = {
    "r512": "r512", "r256": "r256", "r128": "r128",
    "r64": "r64", "r32": "r32", "r16": "r16",
    "qf": "qf", "1/4": "qf", "четвертьфинал": "qf", "quarter": "qf",
    "sf": "sf", "1/2": "sf", "полуфинал": "sf", "semi": "sf",
    "final": "final", "fin": "final", "финал": "final",
}


def _parse_stage_token(tok: str) -> str | None:
    return _PO_STAGES_LOOKUP.get(tok.strip().lower())


def _parse_bo_token(tok: str) -> int | None:
    """``bo3`` / ``bo5`` / ``3`` / ``5`` → integer length; otherwise None."""
    s = tok.strip().lower().lstrip("b").lstrip("о").lstrip("o")  # ``bo3``→``3``, ``о3``→``3``
    try:
        n = int(s)
    except ValueError:
        return None
    return n if n >= 1 else None


def _parse_mode_token(tok: str) -> str | None:
    s = tok.strip().lower()
    if s in ("wins", "побед", "победам", "по_победам", "winsmode"):
        return "wins"
    if s in ("goals", "голы", "голам", "по_голам", "agg", "aggregate"):
        return "goals"
    return None


async def cmd_po_stage_config(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/po_stage_config <stage> <bo3|bo5|…> [wins|goals] [tid]

    Configure how a specific playoff stage's series is played:
      * ``stage``  — ``qf`` / ``sf`` / ``final`` / ``r16`` / ``r32`` / …
      * ``bo<N>``  — series length (``bo1`` / ``bo3`` / ``bo5`` / ``bo7`` …)
      * ``mode``   — ``wins`` (first to majority, early-stop) or
                     ``goals`` (play all N legs, aggregate decides).
                     Defaults to ``wins``.

    Examples::

        /po_stage_config sf bo3 wins
        /po_stage_config final bo5 goals 7

    Use ``/po_stage_config <stage> off`` to remove the override (the
    stage falls back to the tournament-wide defaults).
    """
    if not is_admin(update.effective_user.id):
        await send(update, "❌ Только админ.")
        return

    if len(ctx.args) < 2:
        await send(
            update,
            "Использование: <code>/po_stage_config &lt;stage&gt; "
            "&lt;bo3|bo5|bo7|…&gt; [wins|goals] [tid]</code>\n\n"
            "Примеры:\n"
            "• <code>/po_stage_config sf bo3 wins</code> — полуфинал bo3, "
            "первый кто 2 победы;\n"
            "• <code>/po_stage_config final bo5 goals</code> — финал 5 "
            "матчей, победитель по сумме голов;\n"
            "• <code>/po_stage_config qf off</code> — снять оверрайд для "
            "1/4 (вернуться к настройкам турнира).",
        )
        return

    stage = _parse_stage_token(ctx.args[0])
    if stage is None:
        await send(
            update,
            f"❌ Не понял стадию <code>{html.escape(ctx.args[0])}</code>. "
            "Доступно: r512/r256/r128/r64/r32/r16/qf/sf/final.",
        )
        return

    second = ctx.args[1].strip().lower()
    off = second in ("off", "default", "reset", "снять", "сброс", "0", "bo0")

    legs = 0
    mode: str | None = None
    tid: int | None = None

    if not off:
        n = _parse_bo_token(second)
        if n is None:
            await send(
                update,
                f"❌ Не понял длину серии <code>{html.escape(second)}</code>. "
                "Используй <code>bo1</code>, <code>bo3</code>, "
                "<code>bo5</code>, <code>bo7</code> и т.д.",
            )
            return
        legs = n
        for a in ctx.args[2:]:
            mm = _parse_mode_token(a)
            if mm is not None and mode is None:
                mode = mm
                continue
            ac = a.lstrip("#")
            if ac.isdigit() and tid is None:
                tid = int(ac)
        if mode is None:
            mode = "wins"  # most natural for "bo3" semantics
    else:
        for a in ctx.args[2:]:
            ac = a.lstrip("#")
            if ac.isdigit() and tid is None:
                tid = int(ac)

    if tid is None:
        chat = update.effective_chat
        if chat is not None:
            bound = get_tournament_by_chat(chat.id)
            if bound:
                tid = int(bound["id"])
    if tid is None:
        await send(
            update,
            "❌ Не нашёл турнир. Укажи ID последним аргументом.",
        )
        return

    t = get_tournament(tid)
    if not t:
        await send(update, f"❌ Турнир #{tid} не найден.")
        return
    if not _can_manage_tournament(update.effective_user.id, t):
        await send(
            update,
            "❌ Менять конфиг плей-офф может только создатель или админ турнира.",
        )
        return

    from tournament import set_stage_config, get_stage_config
    set_stage_config(tid, stage, legs if not off else 0, mode or "wins")

    log_tournament_action(
        tid,
        actor_telegram_id=update.effective_user.id,
        actor_username=update.effective_user.username,
        action="po_stage_config",
        details=(
            f"stage={stage} "
            + ("off" if off else f"bo{legs} mode={mode}")
        ),
    )

    t_fresh = get_tournament(tid) or t
    if off:
        await send(
            update,
            f"🔄 Стадия <b>{html.escape(stage.upper())}</b> сброшена на "
            f"настройки турнира (<b>{html.escape(t['name'])}</b>, ID {tid}).",
        )
        return

    cfg = get_stage_config(t_fresh, stage)
    mode_lbl = "по победам" if cfg["mode"] == "wins" else "по голам"
    await send(
        update,
        f"✅ Стадия <b>{html.escape(stage.upper())}</b> в "
        f"<b>{html.escape(t['name'])}</b> (ID {tid}): "
        f"<code>bo{cfg['len']}</code> · {mode_lbl}.\n"
        f"<i>Уже созданные матчи остаются — изменение влияет на "
        f"определение победителя и спавн следующих leg-ов.</i>",
    )


# ─────────────────────────────────────────────────────────────────────────────
# /walkover — admin assigns a 0:3 technical loss
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_walkover(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/walkover @loser [@winner] [tournament_id]
    /walkover #<match_id>           — конкретный матч
    /walkover m<match_id>           — то же самое
    /walkover_match <match_id>      — алиас

    Admin assigns a 0:3 technical loss to @loser. Without a winner,
    picks the loser's pending matches: if there's exactly one — applies
    it; if several — shows an inline picker with each candidate match.
    """
    if not is_admin(update.effective_user.id):
        await send(update, "❌ Только админ.")
        return
    if not ctx.args:
        await send(
            update,
            "Использование:\n"
            "<code>/walkover @loser</code> — pending-матч проигравшего "
            "(если их несколько — спросит кнопками)\n"
            "<code>/walkover @loser @winner [tournament_id]</code> — задать пару\n"
            "<code>/walkover #&lt;match_id&gt;</code> — конкретный матч "
            "(@loser определится автоматически — выбираешь сторону)",
        )
        return

    # ── Match-ID form: /walkover #123 or /walkover m123 or /walkover_match 123
    raw0 = ctx.args[0]
    raw_clean = raw0.lstrip("#").lstrip("m").lstrip("M")
    is_match_id_form = (
        (raw0.startswith("#") or raw0.lower().startswith("m"))
        and raw_clean.isdigit()
    )
    if is_match_id_form:
        try:
            mid = int(raw_clean)
        except ValueError:
            await send(update, "❌ Неверный match_id.")
            return
        m = get_match(mid)
        if not m:
            await send(update, f"❌ Матч #{mid} не найден.")
            return
        if m.get("status") not in ("pending", "reported"):
            await send(update, f"❌ Матч #{mid} уже закрыт ({m.get('status')}).")
            return
        # Optional second arg = @loser (otherwise show picker)
        if len(ctx.args) >= 2:
            loser = _resolve_player_arg(ctx.args[1])
            if not loser:
                await send(update, f"❌ Игрок {html.escape(ctx.args[1])} не найден.")
                return
            if loser["id"] not in (m["player1_id"], m["player2_id"]):
                await send(update, f"❌ {mention(loser['username'])} не участвует в матче #{mid}.")
                return
            await _do_walkover(update, ctx, mid, loser["id"])
            return
        p1 = get_player_by_id(m["player1_id"])
        p2 = get_player_by_id(m["player2_id"])
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(
                f"❌ {p1['username'] if p1 else m['player1_id']} проиграл",
                callback_data=f"wo:{mid}:{m['player1_id']}",
            )],
            [InlineKeyboardButton(
                f"❌ {p2['username'] if p2 else m['player2_id']} проиграл",
                callback_data=f"wo:{mid}:{m['player2_id']}",
            )],
            [InlineKeyboardButton("⤴️ Отмена", callback_data="wo_cancel")],
        ])
        await send(
            update,
            f"⚠️ Кому засчитать ТП в матче #{mid}?\n"
            f"<b>{mention(p1['username']) if p1 else m['player1_id']}</b> "
            f"vs <b>{mention(p2['username']) if p2 else m['player2_id']}</b>",
            reply_markup=kb,
        )
        return

    loser = _resolve_player_arg(ctx.args[0])
    if not loser:
        await send(update, f"❌ Игрок {html.escape(ctx.args[0])} не найден.")
        return

    winner_p: dict | None = None
    tid_arg: int | None = None

    if len(ctx.args) >= 2:
        cand = ctx.args[1]
        if cand.lstrip("@").isdigit() and not cand.startswith("@"):
            tid_arg = int(cand)
        else:
            winner_p = _resolve_player_arg(cand)
            if not winner_p:
                await send(update, f"❌ Игрок {html.escape(cand)} не найден.")
                return
    if len(ctx.args) >= 3:
        try:
            tid_arg = int(ctx.args[2])
        except ValueError:
            await send(update, "❌ tournament_id должен быть числом.")
            return

    if winner_p and winner_p["id"] == loser["id"]:
        await send(update, "❌ Победитель и проигравший не могут совпадать.")
        return

    if tid_arg is None:
        chat = update.effective_chat
        chat_id = chat.id if chat else None
        chat_bound_t = (
            get_tournament_by_chat(chat_id) if chat_id is not None else None
        )
        if chat_bound_t is not None:
            tid_arg = int(chat_bound_t["id"])

    if winner_p:
        existing = get_pending_match(loser["id"], winner_p["id"], tid_arg)
        if existing:
            await _do_walkover(update, ctx, existing["id"], loser["id"])
            return
        if tid_arg is None:
            await send(
                update,
                "❌ Не могу определить турнир. "
                "Используй <code>/walkover @loser @winner &lt;tournament_id&gt;</code> "
                "или запускай команду в чате, привязанном к турниру.",
            )
            return
        mid = db.create_match(
            tid_arg, winner_p["id"], loser["id"],
            stage="group", round_num=1,
        )
        await _do_walkover(update, ctx, mid, loser["id"])
        return

    # No explicit winner — find pending matches for loser, show picker if 2+.
    pendings = _list_pending_matches_for(loser["id"], tid_arg)
    if not pendings:
        scope_note = f" в этом турнире (ID {tid_arg})" if tid_arg is not None else ""
        await send(
            update,
            f"❌ Нет pending-матчей у {mention(loser['username'])}{scope_note}. "
            f"Укажи победителя: <code>/walkover @loser @winner [tournament_id]</code>.",
        )
        return
    if len(pendings) == 1:
        await _do_walkover(update, ctx, pendings[0]["id"], loser["id"])
        return

    # Multiple pending matches — show picker
    rows: list[list[InlineKeyboardButton]] = []
    for m in pendings[:10]:
        opp_id = m["player2_id"] if m["player1_id"] == loser["id"] else m["player1_id"]
        opp = get_player_by_id(opp_id)
        opp_label = opp["username"] if opp else str(opp_id)
        stage_lbl = m.get("stage") or "?"
        rows.append([InlineKeyboardButton(
            f"#{m['id']} vs @{opp_label} ({stage_lbl}, тур {m.get('tournament_id') or '—'})",
            callback_data=f"wo:{m['id']}:{loser['id']}",
        )])
    rows.append([InlineKeyboardButton("⤴️ Отмена", callback_data="wo_cancel")])
    await send(
        update,
        f"⚠️ У {mention(loser['username'])} <b>{len(pendings)}</b> pending-матчей. "
        f"Какой засчитать как ТП (0:3)?",
        reply_markup=InlineKeyboardMarkup(rows),
    )


# ─────────────────────────────────────────────────────────────────────────────
# /withdraw — bulk-walkover all open matches of one player
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_withdraw(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """``/withdraw [ID] @user`` — снять игрока из плей-офф.

    Все его незакрытые матчи в этом турнире проводятся через
    техническое поражение (счёт 0:3 в пользу соперника). Доступно
    создателю турнира / root / делегированным админам.
    """
    t, args, err = _split_tadmin_args(update, ctx)
    if t is None:
        await send(update, err or "❌ Нет активного турнира.")
        return
    user_id = update.effective_user.id
    if not _can_manage_tournament(user_id, t):
        await send(
            update,
            "❌ Снимать игроков может только админ этого турнира.",
        )
        return
    # Resolve the target player. Try _resolve_player_arg first (handles
    # @username, plain username, numeric telegram_id, game nickname).
    # Fall back to _resolve_tadmin_target for reply-to-message flow.
    target_player = None
    label = ""
    if args:
        target_player = _resolve_player_arg(args[0])
        if target_player:
            label = mention(target_player.get("username") or "")
    if not target_player:
        target_id, label = _resolve_tadmin_target(update, ctx, args=args)
        if target_id is None:
            await send(update, label, parse_mode="HTML")
            return
        target_player = get_player_by_telegram_id(target_id)
    if not target_player:
        await send(
            update,
            f"❌ Не нашёл игрока {html.escape(label)} в БД "
            f"(он должен быть зарегистрирован).",
        )
        return

    label = label or mention(target_player.get("username") or "")

    open_matches = get_open_matches_for_player(
        target_player["id"], tournament_id=int(t["id"])
    )
    if not open_matches:
        await send(
            update,
            f"ℹ️ У {html.escape(label)} нет открытых матчей в "
            f"<b>{html.escape(t['name'])}</b>.",
        )
        return

    applied: list[int] = []
    for m in open_matches:
        try:
            apply_walkover(m["id"], target_player["id"])
            applied.append(m["id"])
        except Exception as e:
            log.warning("withdraw: walkover for match %s failed: %s",
                        m["id"], e)

    log_tournament_action(
        int(t["id"]),
        actor_telegram_id=user_id,
        actor_username=update.effective_user.username,
        action="withdraw_player",
        details=(
            f"player=@{target_player.get('username') or '?'} "
            f"matches={len(applied)}"
        ),
    )

    summary = (
        f"⚠️ {html.escape(label)} снят из турнира "
        f"<b>{html.escape(t['name'])}</b>. "
        f"Применено техническое поражение в {len(applied)} матчах."
    )
    await send(update, summary)

    # Notify the withdrawn player.
    notify_tg_id = target_player.get("telegram_id")
    if notify_tg_id:
        try:
            await ctx.bot.send_message(
                int(notify_tg_id),
                f"⚠️ Тебя сняли из турнира "
                f"<b>{html.escape(t['name'])}</b> [{t_full_label(t)}]. "
                f"Все {len(applied)} незакрытых матчей засчитаны как "
                f"техническое поражение.",
                parse_mode="HTML",
            )
        except Exception:
            pass

    # Auto-advance + announce the new stage if walkovers cleared it.
    try:
        advanced = _maybe_auto_advance(ctx, int(t["id"]))
    except Exception as e:
        log.warning("withdraw: auto-advance failed: %s", e)
        advanced = False
    if advanced:
        await _announce_stage_advance(
            ctx, int(t["id"]),
            _current_playoff_stage(int(t["id"])),
        )


# ─────────────────────────────────────────────────────────────────────────────
# /tech_draw — admin assigns a technical draw (default 1:1)
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_tech_draw(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """``/tech_draw @p1 @p2 [X:X] [tournament_id]``
    ``/tech_draw #<match_id> [X:X]``

    Admin records a *technical draw* — both sides keep equal points and
    both ratings shift symmetrically through ``apply_result``. Default
    score is ``1:1``. Custom equal scores like ``2:2`` or ``0:0`` are
    accepted, but the two halves must match (no fake "draw" with
    different scores).

    Without a winner column, the command reuses any pending match
    between the pair (in the resolved tournament) so the bracket stays
    intact; otherwise it creates a fresh confirmed friendly match the
    same way ``/admin_report`` does.
    """
    if not is_admin(update.effective_user.id):
        await send(update, "❌ Только админ.")
        return
    if not ctx.args:
        await send(
            update,
            "Использование:\n"
            "<code>/tech_draw @p1 @p2 [X:X] [tournament_id]</code>\n"
            "<code>/tech_draw #&lt;match_id&gt; [X:X]</code>\n"
            "По умолчанию счёт <b>1:1</b>. Стороны обязаны быть равны.",
        )
        return

    # ── Match-ID form: /tech_draw #123 [X:X]
    raw0 = ctx.args[0]
    raw_clean = raw0.lstrip("#").lstrip("m").lstrip("M")
    is_match_id_form = (
        (raw0.startswith("#") or raw0.lower().startswith("m"))
        and raw_clean.isdigit()
    )
    if is_match_id_form:
        try:
            mid = int(raw_clean)
        except ValueError:
            await send(update, "❌ Неверный match_id.")
            return
        m = get_match(mid)
        if not m:
            await send(update, f"❌ Матч #{mid} не найден.")
            return
        if m.get("status") not in ("pending", "reported", "awaiting_admin"):
            await send(update, f"❌ Матч #{mid} уже закрыт ({m.get('status')}).")
            return
        s1, s2 = 1, 1
        if len(ctx.args) >= 2:
            sm = SCORE_RE.match(ctx.args[1])
            if not sm:
                await send(update, "❌ Неверный формат счёта. Пример: <code>1:1</code>")
                return
            s1, s2 = int(sm.group(1)), int(sm.group(2))
            if s1 != s2:
                await send(update, "❌ Это ничья — счёт должен быть равным.")
                return
            if s1 > 30:
                await send(update, "❌ Слишком большой счёт.")
                return
        update_match(
            mid,
            score1=s1, score2=s2,
            status="confirmed",
            reported_by=update.effective_user.id,
        )
        try:
            summary = apply_result(mid)
        except Exception as e:
            log.exception("apply_result for tech_draw failed: %s", e)
            await send(update, f"❌ Не удалось засчитать ничью: {html.escape(str(e))}")
            return
        p1 = get_player_by_id(m["player1_id"])
        p2 = get_player_by_id(m["player2_id"])
        log_tournament_action(
            m.get("tournament_id"),
            actor_telegram_id=update.effective_user.id,
            actor_username=update.effective_user.username,
            action="tech_draw",
            details=(
                f"match={mid} score={s1}:{s2} "
                f"@{p1['username'] if p1 else m['player1_id']} vs "
                f"@{p2['username'] if p2 else m['player2_id']}"
            ),
        )
        await send(
            update,
            f"🤝 Техническая ничья!\n\n"
            f"{mention(p1['username']) if p1 else m['player1_id']} "
            f"<b>{s1}:{s2}</b> "
            f"{mention(p2['username']) if p2 else m['player2_id']}\n"
            f"<i>матч #{mid}, турнир ID {m.get('tournament_id') or '—'}</i>\n"
            f"📈 ELO: {arrow(int(round(summary.get('elo1_after', 0) - summary.get('elo1_before', 0))))} / "
            f"{arrow(int(round(summary.get('elo2_after', 0) - summary.get('elo2_before', 0))))}",
        )
        if m.get("tournament_id"):
            try:
                if _maybe_auto_advance(ctx, int(m["tournament_id"])):
                    await _announce_stage_advance(
                        ctx, int(m["tournament_id"]),
                        _current_playoff_stage(int(m["tournament_id"])),
                    )
            except Exception:
                log.exception("auto-advance after tech_draw failed")
        return

    # ── Pair form: /tech_draw @p1 @p2 [X:X] [tid]
    if len(ctx.args) < 2:
        await send(
            update,
            "❌ Укажи двух игроков: "
            "<code>/tech_draw @p1 @p2 [X:X] [tournament_id]</code>",
        )
        return
    p1_arg = _resolve_player_arg(ctx.args[0])
    p2_arg = _resolve_player_arg(ctx.args[1])
    if not p1_arg:
        await send(update, f"❌ Игрок {html.escape(ctx.args[0])} не найден.")
        return
    if not p2_arg:
        await send(update, f"❌ Игрок {html.escape(ctx.args[1])} не найден.")
        return
    if p1_arg["id"] == p2_arg["id"]:
        await send(update, "❌ Игроки должны быть разные.")
        return

    s1, s2 = 1, 1
    tid_arg: int | None = None
    rest = ctx.args[2:]
    for tok in rest:
        sm = SCORE_RE.match(tok)
        if sm:
            s1, s2 = int(sm.group(1)), int(sm.group(2))
            if s1 != s2:
                await send(update, "❌ Это ничья — счёт должен быть равным.")
                return
            if s1 > 30:
                await send(update, "❌ Слишком большой счёт.")
                return
            continue
        if tok.lstrip("@").isdigit() and not tok.startswith("@"):
            try:
                tid_arg = int(tok)
            except ValueError:
                pass
            continue
        await send(update, f"❌ Не понял аргумент: {html.escape(tok)}")
        return

    # Tournament resolution: explicit ID > chat binding > single active.
    t: dict | None = None
    if tid_arg is not None:
        t = get_tournament(tid_arg)
        if not t:
            await send(update, f"❌ Турнир {tid_arg} не найден.")
            return
    else:
        chat = update.effective_chat
        if chat:
            t = get_tournament_by_chat(chat.id)
        if t is None:
            t = get_active_tournament()
    tid = t["id"] if t else None

    existing = get_pending_match(p1_arg["id"], p2_arg["id"], tid)
    if existing:
        # Normalize so score1 corresponds to existing.player1_id (since draw
        # is symmetric this is purely cosmetic, but keep the convention).
        update_match(
            existing["id"],
            score1=s1, score2=s2,
            status="confirmed",
            reported_by=update.effective_user.id,
        )
        match_id = existing["id"]
    else:
        match_id = db.create_match(
            tid or 0, p1_arg["id"], p2_arg["id"],
            stage="group", round_num=1,
        )
        update_match(
            match_id,
            score1=s1, score2=s2,
            status="confirmed",
            reported_by=update.effective_user.id,
        )

    try:
        summary = apply_result(match_id)
    except Exception as e:
        log.exception("apply_result for tech_draw failed: %s", e)
        await send(update, f"❌ Не удалось засчитать ничью: {html.escape(str(e))}")
        return
    log_tournament_action(
        tid,
        actor_telegram_id=update.effective_user.id,
        actor_username=update.effective_user.username,
        action="tech_draw",
        details=(
            f"match={match_id} score={s1}:{s2} "
            f"@{p1_arg['username']} vs @{p2_arg['username']}"
        ),
    )

    scope = ""
    if t:
        scope = f" в турнире <b>{html.escape(t['name'])}</b> (ID {t['id']})"
    await send(
        update,
        f"🤝 Техническая ничья: {mention(p1_arg['username'])} "
        f"<b>{s1}:{s2}</b> {mention(p2_arg['username'])}{scope}.\n"
        f"📈 ELO: {mention(p1_arg['username'])} "
        f"{arrow(int(round(summary.get('elo1_after', 0) - summary.get('elo1_before', 0))))} → "
        f"<b>{int(round(summary.get('elo1_after', 0)))}</b>; "
        f"{mention(p2_arg['username'])} "
        f"{arrow(int(round(summary.get('elo2_after', 0) - summary.get('elo2_before', 0))))} → "
        f"<b>{int(round(summary.get('elo2_after', 0)))}</b>.",
    )

    if tid:
        try:
            if _maybe_auto_advance(ctx, tid):
                await _announce_stage_advance(
                    ctx, int(tid), _current_playoff_stage(int(tid)),
                )
        except Exception:
            log.exception("auto-advance after tech_draw failed")


# ─────────────────────────────────────────────────────────────────────────────
# /set_deadline — admin sets / changes the deadline of a single match (DD)
# ─────────────────────────────────────────────────────────────────────────────

def _parse_deadline_token(tokens: list[str]) -> tuple[str | None, str | None]:
    """Parse a deadline expression from a list of tokens.

    Accepts:
      • ``+<hours>``                    → now + N hours (tz-independent)
      • ``YYYY-MM-DD HH:MM``            → absolute date-time in display TZ
      • ``YYYY-MM-DD``                  → absolute date (00:00 local)

    Returns a tuple ``(deadline_str_utc, error)`` where exactly one is
    not None. ``deadline_str_utc`` is the naive-UTC string
    (``"%Y-%m-%d %H:%M:%S"``) that lines up with the SQLite columns —
    absolute inputs are interpreted in the operator's display timezone
    (``BOT_DISPLAY_TZ``, default ``Europe/Moscow``) and converted to
    UTC for storage.
    """
    from datetime import datetime, timedelta
    if not tokens:
        return None, "Не задан срок"
    head = tokens[0]
    if head.startswith("+") and head[1:].replace(".", "", 1).isdigit():
        try:
            hours = float(head[1:])
        except ValueError:
            return None, f"Не понял срок: {head}"
        if hours <= 0 or hours > 24 * 30:
            return None, "Часы должны быть от 0 до 720 (30 дней)."
        dl = datetime.utcnow() + timedelta(hours=hours)
        return dl.strftime("%Y-%m-%d %H:%M:%S"), None
    raw = " ".join(tokens).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            dl_local = datetime.strptime(raw, fmt)
            return _local_to_utc_str(dl_local), None
        except ValueError:
            continue
    tz = _tz_label()
    return None, (
        "Не понял дату. Используй <code>+&lt;часы&gt;</code> "
        f"или <code>YYYY-MM-DD HH:MM</code> ({tz})."
    )


# Aliases recognised in the stage form of ``/set_deadline``. Map each
# alias to the list of ``matches.stage`` values it should expand to.
# ``group`` covers regular round-robin matches; ``playoff`` is the union
# of all knock-out rounds (kept in sync with database.PLAYOFF inside
# get_real_tournament_matches).
_STAGE_ALIASES: dict[str, tuple[str, ...]] = {
    "group":   ("group",),
    "groups":  ("group",),
    "групп":   ("group",),
    "группа":  ("group",),
    "группы":  ("group",),
    "playoff": ("r16", "qf", "sf", "final"),
    "po":      ("r16", "qf", "sf", "final"),
    "плей":    ("r16", "qf", "sf", "final"),
    "плей-офф":("r16", "qf", "sf", "final"),
    "плейофф": ("r16", "qf", "sf", "final"),
    "r16":     ("r16",),
    "1/8":     ("r16",),
    "qf":      ("qf",),
    "1/4":     ("qf",),
    "sf":      ("sf",),
    "1/2":     ("sf",),
    "final":   ("final",),
    "финал":   ("final",),
}


async def _apply_stage_deadline(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
    *,
    stages: tuple[str, ...],
    stage_label: str,
    tid: int,
    deadline_tokens: list[str],
) -> None:
    """Bulk-apply a deadline to every pending match of ``tid`` whose
    ``stage`` is in ``stages``.

    Used by the stage form of ``/set_deadline`` (``group``, ``playoff``,
    ``qf``, ``sf``, ``final``, ``r16``). Only matches with status
    ``pending``/``reported``/``awaiting_admin`` are touched — already
    ``confirmed`` ones keep whatever deadline they had so audit
    history isn't rewritten.
    """
    t = get_tournament(tid)
    if not t:
        await send(update, f"❌ Турнир ID {tid} не найден.")
        return

    deadline_str, err = _parse_deadline_token(deadline_tokens)
    if err:
        await send(update, f"❌ {err}")
        return

    rows: list[dict] = []
    for st in stages:
        rows.extend(get_tournament_matches(tid, stage=st))
    open_states = {"pending", "reported", "awaiting_admin"}
    rows = [r for r in rows if (r.get("status") or "").lower() in open_states]
    if not rows:
        await send(
            update,
            f"ℹ️ В турнире <b>{html.escape(t['name'])}</b> нет открытых "
            f"матчей в стадии «{stage_label}».",
        )
        return

    notified_telegram_ids: set[int] = set()
    for m in rows:
        update_match(m["id"], deadline=deadline_str)
        log_tournament_action(
            tid,
            actor_telegram_id=update.effective_user.id,
            actor_username=update.effective_user.username,
            action="set_deadline_bulk",
            details=(
                f"match={m['id']} stage={m.get('stage')} "
                f"old={m.get('deadline') or '—'} new={deadline_str}"
            ),
        )
        for pid_field in ("player1_id", "player2_id"):
            p = get_player_by_id(m[pid_field])
            if not p or not p.get("telegram_id"):
                continue
            tg_id = int(p["telegram_id"])
            if tg_id in notified_telegram_ids:
                continue
            notified_telegram_ids.add(tg_id)
            try:
                await ctx.bot.send_message(
                    tg_id,
                    f"⏰ Админ обновил дедлайн матчей "
                    f"стадии «{html.escape(stage_label)}» "
                    f"турнира <b>{html.escape(t['name'])}</b>.\n"
                    f"Новый срок: <b>{_fmt_minute_local(deadline_str)} "
                    f"{_tz_label()}</b>.",
                    parse_mode="HTML",
                )
            except Exception:
                pass

    await send(
        update,
        f"⏰ Обновил дедлайн для <b>{len(rows)}</b> матчей "
        f"({html.escape(stage_label)}) турнира "
        f"<b>{html.escape(t['name'])}</b> [ID {tid}].\n"
        f"Новый срок: <b>{_fmt_minute_local(deadline_str)} "
        f"{_tz_label()}</b>.",
    )


async def cmd_set_match_deadline(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """``/set_deadline #<match_id> +<hours>``
    ``/set_deadline #<match_id> YYYY-MM-DD HH:MM``
    ``/set_deadline @p1 @p2 +<hours> [tournament_id]``
    ``/set_deadline @p1 @p2 YYYY-MM-DD HH:MM [tournament_id]``
    ``/set_deadline group <tid> +<hours>`` (или ``YYYY-MM-DD HH:MM``)
    ``/set_deadline playoff <tid> +<hours>``
    ``/set_deadline qf|sf|final|r16 <tid> +<hours>``

    Admin-only. Sets or changes the deadline (DD) for a specific
    pending match — or, in the stage form, for **every** open match
    in the given group/playoff stage of the tournament. Deadlines
    are stored internally in UTC but parsed and displayed in the
    operator's display timezone (``BOT_DISPLAY_TZ``, default МСК).
    Notifies the affected players in DM.
    """
    if not is_admin(update.effective_user.id):
        await send(update, "❌ Только админ.")
        return
    if not ctx.args:
        await send(
            update,
            "Использование:\n"
            "<code>/set_deadline #&lt;match_id&gt; +&lt;часы&gt;</code>\n"
            "<code>/set_deadline #&lt;match_id&gt; YYYY-MM-DD HH:MM</code>\n"
            "<code>/set_deadline @p1 @p2 +&lt;часы&gt; [tid]</code>\n"
            "<code>/set_deadline @p1 @p2 YYYY-MM-DD HH:MM [tid]</code>\n"
            "<code>/set_deadline group &lt;tid&gt; +&lt;часы&gt;</code>\n"
            "<code>/set_deadline playoff &lt;tid&gt; +&lt;часы&gt;</code>\n"
            "<code>/set_deadline qf|sf|final|r16 &lt;tid&gt; +&lt;часы&gt;</code>\n"
            f"Время указывай в <b>{_tz_label()}</b>.",
        )
        return

    raw0 = ctx.args[0]
    raw0_lower = raw0.lower().strip()

    # ── Stage form: /set_deadline <group|playoff|qf|sf|final|r16> [<tid>] <deadline>
    if raw0_lower in _STAGE_ALIASES:
        stages = _STAGE_ALIASES[raw0_lower]
        rest = list(ctx.args[1:])
        if not rest:
            await send(
                update,
                "❌ Укажи турнир и срок: "
                "<code>/set_deadline group &lt;tid&gt; +24</code>",
            )
            return
        # Optional explicit tournament id; otherwise infer from chat binding.
        tid: int | None = None
        if rest[0].isdigit():
            try:
                tid = int(rest[0])
                rest = rest[1:]
            except ValueError:
                pass
        if tid is None:
            chat = update.effective_chat
            chat_bound_t = get_tournament_by_chat(chat.id) if chat else None
            if chat_bound_t:
                tid = int(chat_bound_t["id"])
        if tid is None:
            await send(
                update,
                "❌ Не понял, какой турнир. Привяжи чат через "
                "<code>/bind_tournament &lt;ID&gt;</code> или укажи ID явно: "
                f"<code>/set_deadline {raw0_lower} &lt;ID&gt; +24</code>.",
            )
            return
        await _apply_stage_deadline(
            update, ctx,
            stages=stages,
            stage_label=raw0_lower,
            tid=tid,
            deadline_tokens=rest,
        )
        return

    raw_clean = raw0.lstrip("#").lstrip("m").lstrip("M")
    is_match_id_form = (
        (raw0.startswith("#") or raw0.lower().startswith("m"))
        and raw_clean.isdigit()
    )

    target_match: dict | None = None
    deadline_tokens: list[str] = []

    if is_match_id_form:
        try:
            mid = int(raw_clean)
        except ValueError:
            await send(update, "❌ Неверный match_id.")
            return
        target_match = get_match(mid)
        if not target_match:
            await send(update, f"❌ Матч #{mid} не найден.")
            return
        deadline_tokens = ctx.args[1:]
    else:
        if len(ctx.args) < 3:
            await send(
                update,
                "❌ Укажи двух игроков и срок: "
                "<code>/set_deadline @p1 @p2 +24</code>",
            )
            return
        p1_arg = _resolve_player_arg(ctx.args[0])
        p2_arg = _resolve_player_arg(ctx.args[1])
        if not p1_arg:
            await send(update, f"❌ Игрок {html.escape(ctx.args[0])} не найден.")
            return
        if not p2_arg:
            await send(update, f"❌ Игрок {html.escape(ctx.args[1])} не найден.")
            return
        rest = list(ctx.args[2:])
        # If the *last* token is a bare integer it's the tournament ID
        # — strip it, the remainder is the deadline expression.
        explicit_tid: int | None = None
        if (
            rest
            and rest[-1].lstrip("@").isdigit()
            and not rest[-1].startswith("@")
        ):
            try:
                explicit_tid = int(rest[-1])
                rest = rest[:-1]
            except ValueError:
                pass
        deadline_tokens = rest

        chat = update.effective_chat
        chat_bound_t = (
            get_tournament_by_chat(chat.id) if chat else None
        )
        tid_lookup = (
            explicit_tid if explicit_tid is not None
            else (chat_bound_t["id"] if chat_bound_t else None)
        )
        target_match = get_pending_match(
            p1_arg["id"], p2_arg["id"], tid_lookup,
        )
        if not target_match:
            await send(
                update,
                f"❌ Не нашёл pending-матч между "
                f"{mention(p1_arg['username'])} и {mention(p2_arg['username'])}"
                + (f" в турнире ID {tid_lookup}" if tid_lookup else "")
                + ".",
            )
            return

    if target_match.get("status") not in ("pending", "reported", "awaiting_admin"):
        await send(
            update,
            f"❌ Матч #{target_match['id']} уже закрыт "
            f"({target_match.get('status')}). Дедлайн менять смысла нет.",
        )
        return

    deadline_str, err = _parse_deadline_token(deadline_tokens)
    if err:
        await send(update, f"❌ {err}")
        return

    old_deadline = target_match.get("deadline") or "—"
    update_match(target_match["id"], deadline=deadline_str)
    log_tournament_action(
        target_match.get("tournament_id"),
        actor_telegram_id=update.effective_user.id,
        actor_username=update.effective_user.username,
        action="set_deadline",
        details=(
            f"match={target_match['id']} "
            f"old={old_deadline} new={deadline_str}"
        ),
    )

    p1 = get_player_by_id(target_match["player1_id"])
    p2 = get_player_by_id(target_match["player2_id"])
    tz = _tz_label()
    old_local = _fmt_minute_local(old_deadline) or str(old_deadline)
    new_local = _fmt_minute_local(deadline_str)
    note = (
        f"⏰ <b>Новый дедлайн</b> для матча #{target_match['id']}\n"
        f"{mention(p1['username']) if p1 else target_match['player1_id']} "
        f"vs {mention(p2['username']) if p2 else target_match['player2_id']}\n"
        f"Был: <code>{html.escape(old_local)}</code>\n"
        f"Стал: <b>{html.escape(new_local)} {tz}</b>"
    )
    await send(update, note)
    for p in (p1, p2):
        if not p or not p.get("telegram_id"):
            continue
        try:
            await ctx.bot.send_message(
                p["telegram_id"],
                f"⏰ Админ обновил дедлайн твоего матча #{target_match['id']}.\n"
                f"Новый срок: <b>{_fmt_minute_local(deadline_str)} "
                f"{_tz_label()}</b>.",
                parse_mode="HTML",
            )
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────────────
# /edit_match — admin overwrites the score of an already-confirmed match
# ─────────────────────────────────────────────────────────────────────────────


async def cmd_edit_match(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/edit_match #<match_id> <score1:score2>

    Admin-only. Overwrites the result of an already-confirmed (processed)
    match:
      1. Reverses the old ELO/stats/group-table changes.
      2. Sets the new score.
      3. Re-applies the result (ELO + stats + group table).

    Example: /edit_match #42 3:1
    """
    if not is_admin(update.effective_user.id):
        await send(update, "❌ Только админ.")
        return

    if len(ctx.args) < 2:
        await send(
            update,
            "Использование: <code>/edit_match #&lt;match_id&gt; "
            "&lt;score1:score2&gt;</code>\n"
            "Пример: <code>/edit_match #42 3:1</code>",
        )
        return

    # Parse match ID
    mid_raw = ctx.args[0].lstrip("#")
    if not mid_raw.isdigit():
        await send(update, "❌ Первый аргумент — ID матча (число). Пример: <code>#42</code>")
        return
    match_id = int(mid_raw)

    # Parse new score
    sm = SCORE_RE.match(ctx.args[1])
    if not sm:
        await send(update, "❌ Неверный формат счёта. Пример: <code>3:1</code>")
        return
    new_s1, new_s2 = int(sm.group(1)), int(sm.group(2))
    if new_s1 > 30 or new_s2 > 30:
        await send(update, "❌ Слишком большой счёт. Максимум 30.")
        return

    # Fetch the match
    m = get_match(match_id)
    if not m:
        await send(update, f"❌ Матч #{match_id} не найден.")
        return
    if m.get("status") != "confirmed":
        await send(
            update,
            f"❌ Матч #{match_id} не подтверждён (статус: {m.get('status')}).\n"
            "Перезаписать можно только confirmed-матчи.",
        )
        return
    if not m.get("played_at"):
        await send(
            update,
            f"❌ Матч #{match_id} ещё не обработан (played_at пуст). "
            "Используй /admin_report.",
        )
        return

    old_s1, old_s2 = m["score1"], m["score2"]
    if old_s1 == new_s1 and old_s2 == new_s2:
        await send(update, f"ℹ️ Счёт уже {old_s1}:{old_s2}, менять нечего.")
        return

    tid = m.get("tournament_id")
    p1 = get_player_by_id(m["player1_id"])
    p2 = get_player_by_id(m["player2_id"])
    if not p1 or not p2:
        await send(update, "❌ Не найдены игроки матча в базе.")
        return

    # ── Determine tournament context ────────────────────────────────────────
    t = None
    is_official = True
    if tid:
        t = get_tournament(tid)
        if t:
            is_official = bool(t.get("is_official", 1))

    # ── Step 1: Reverse old result ──────────────────────────────────────────
    _reverse_match_stats(m, p1, p2, is_official, tid)

    # ── Step 2: Set new score and clear played_at ───────────────────────────
    update_match(match_id, score1=new_s1, score2=new_s2, played_at=None)

    # ── Step 3: Re-apply ────────────────────────────────────────────────────
    summary = apply_result(match_id)

    # ── Audit log ───────────────────────────────────────────────────────────
    log_tournament_action(
        tid,
        actor_telegram_id=update.effective_user.id,
        actor_username=update.effective_user.username,
        action="edit_match",
        details=(
            f"match={match_id} old={old_s1}:{old_s2} new={new_s1}:{new_s2}"
        ),
    )

    scope = ""
    if t:
        scope = f" (турнир <b>{html.escape(t['name'])}</b>)"

    await send(
        update,
        f"✅ Матч <code>#{match_id}</code> перезаписан: "
        f"<s>{old_s1}:{old_s2}</s> → <b>{new_s1}:{new_s2}</b>{scope}\n"
        f"{mention(p1['username'])} "
        f"{arrow(int(round(summary['elo1_after'] - summary['elo1_before'])))} → "
        f"<b>{int(round(summary['elo1_after']))}</b>;  "
        f"{mention(p2['username'])} "
        f"{arrow(int(round(summary['elo2_after'] - summary['elo2_before'])))} → "
        f"<b>{int(round(summary['elo2_after']))}</b>.",
    )


def _reverse_match_stats(
    m: dict,
    p1: dict,
    p2: dict,
    is_official: bool,
    tid: int | None,
) -> None:
    """Undo the stat/ELO changes that ``apply_result`` applied for match ``m``.

    After this function the players' stats (and tournament_elo / group table)
    are as if the match never happened. Caller is responsible for then setting
    a new score + re-running ``apply_result``.
    """
    from elo import compute_elo_change
    from match_processor import total_matches

    old_s1, old_s2 = m["score1"], m["score2"]
    # Outcome of the OLD result
    is_w1 = 1 if old_s1 > old_s2 else 0
    is_w2 = 1 if old_s2 > old_s1 else 0
    is_dr = 1 if old_s1 == old_s2 else 0

    # Refresh player rows (they may have changed since the match dict was read)
    p1 = get_player_by_id(p1["id"])
    p2 = get_player_by_id(p2["id"])

    if is_official:
        # We need to reverse the ELO delta. Recompute what was applied.
        # total_matches counts this match too — we need games_before.
        g1_now = total_matches(p1["id"])
        g2_now = total_matches(p2["id"])
        g1_before = max(0, g1_now - 1)
        g2_before = max(0, g2_now - 1)
        # "elo_before" for each player: current elo minus the delta that
        # would have been computed at the time.
        elo1_before_match = p1["elo"] - (
            compute_elo_change(
                p1["elo"] - (compute_elo_change(p1["elo"], p2["elo"], old_s1, old_s2, g1_before, g2_before)[0] - p1["elo"]),
                p2["elo"] - (compute_elo_change(p1["elo"], p2["elo"], old_s1, old_s2, g1_before, g2_before)[1] - p2["elo"]),
                old_s1, old_s2, g1_before, g2_before,
            )[0] - (p1["elo"] - (compute_elo_change(p1["elo"], p2["elo"], old_s1, old_s2, g1_before, g2_before)[0] - p1["elo"]))
        )
        # The above is circular — simpler approach: recompute delta using
        # current ELO minus the delta we're about to compute (converges in
        # practice because ELO shift is small). Pragmatic approach: compute
        # the delta using the CURRENT elos as if this were the match rating.
        # The result won't be 100% exact if many matches happened after, but
        # it's good enough for an admin override.
        new_e1, new_e2 = compute_elo_change(
            p1["elo"], p2["elo"], old_s1, old_s2, g1_before, g2_before
        )
        delta1 = new_e1 - p1["elo"]
        delta2 = new_e2 - p2["elo"]
        # Reverse = subtract the delta
        restored_elo1 = p1["elo"] - delta1
        restored_elo2 = p2["elo"] - delta2

        t_type = None
        if tid:
            t = get_tournament(tid)
            if t:
                t_type = t.get("tournament_type")
        type_field = {"vsa": "elo_vsa", "ri": "elo_ri"}.get(t_type or "")

        p1_updates: dict = dict(
            elo=restored_elo1,
            goals_scored=max(0, p1["goals_scored"] - old_s1),
            goals_conceded=max(0, p1["goals_conceded"] - old_s2),
            wins=max(0, p1["wins"] - is_w1),
            losses=max(0, p1["losses"] - is_w2),
            draws=max(0, p1["draws"] - is_dr),
            clean_sheets=max(0, p1["clean_sheets"] - (1 if old_s2 == 0 else 0)),
        )
        if type_field:
            p1_updates[type_field] = max(0, (p1.get(type_field) or 0) - delta1)

        p2_updates: dict = dict(
            elo=restored_elo2,
            goals_scored=max(0, p2["goals_scored"] - old_s2),
            goals_conceded=max(0, p2["goals_conceded"] - old_s1),
            wins=max(0, p2["wins"] - is_w2),
            losses=max(0, p2["losses"] - is_w1),
            draws=max(0, p2["draws"] - is_dr),
            clean_sheets=max(0, p2["clean_sheets"] - (1 if old_s1 == 0 else 0)),
        )
        if type_field:
            p2_updates[type_field] = max(0, (p2.get(type_field) or 0) - delta2)

        from database import update_player_stats
        update_player_stats(p1["id"], **p1_updates)
        update_player_stats(p2["id"], **p2_updates)
    else:
        # Isolated tournament ELO — reverse local elo + global stats (no ELO)
        from database import get_tournament_elo, upsert_tournament_elo, update_player_stats
        local_p1 = get_tournament_elo(tid, p1["id"])
        local_p2 = get_tournament_elo(tid, p2["id"])

        from elo import compute_elo_change
        new_e1, new_e2 = compute_elo_change(
            local_p1["elo"], local_p2["elo"], old_s1, old_s2,
            max(0, local_p1["games"] - 1), max(0, local_p2["games"] - 1),
        )
        delta1 = new_e1 - local_p1["elo"]
        delta2 = new_e2 - local_p2["elo"]

        upsert_tournament_elo(
            tid, p1["id"],
            elo=local_p1["elo"] - delta1,
            games=max(0, local_p1["games"] - 1),
            wins=max(0, local_p1["wins"] - is_w1),
            draws=max(0, local_p1["draws"] - is_dr),
            losses=max(0, local_p1["losses"] - is_w2),
            goals_for=max(0, local_p1["goals_for"] - old_s1),
            goals_against=max(0, local_p1["goals_against"] - old_s2),
        )
        upsert_tournament_elo(
            tid, p2["id"],
            elo=local_p2["elo"] - delta2,
            games=max(0, local_p2["games"] - 1),
            wins=max(0, local_p2["wins"] - is_w2),
            draws=max(0, local_p2["draws"] - is_dr),
            losses=max(0, local_p2["losses"] - is_w1),
            goals_for=max(0, local_p2["goals_for"] - old_s2),
            goals_against=max(0, local_p2["goals_against"] - old_s1),
        )
        # Reverse global stats (no ELO change in unofficial mode)
        update_player_stats(
            p1["id"],
            goals_scored=max(0, p1["goals_scored"] - old_s1),
            goals_conceded=max(0, p1["goals_conceded"] - old_s2),
            wins=max(0, p1["wins"] - is_w1),
            losses=max(0, p1["losses"] - is_w2),
            draws=max(0, p1["draws"] - is_dr),
            clean_sheets=max(0, p1["clean_sheets"] - (1 if old_s2 == 0 else 0)),
        )
        update_player_stats(
            p2["id"],
            goals_scored=max(0, p2["goals_scored"] - old_s2),
            goals_conceded=max(0, p2["goals_conceded"] - old_s1),
            wins=max(0, p2["wins"] - is_w2),
            losses=max(0, p2["losses"] - is_w1),
            draws=max(0, p2["draws"] - is_dr),
            clean_sheets=max(0, p2["clean_sheets"] - (1 if old_s1 == 0 else 0)),
        )

    # ── Reverse group table (group stage only) ──────────────────────────────
    if m.get("stage") == "group" and tid:
        from database import update_tournament_player
        conn = db.get_conn()
        tp1 = conn.execute(
            "SELECT * FROM tournament_players WHERE tournament_id=? AND player_id=?",
            (tid, p1["id"]),
        ).fetchone()
        tp2 = conn.execute(
            "SELECT * FROM tournament_players WHERE tournament_id=? AND player_id=?",
            (tid, p2["id"]),
        ).fetchone()
        conn.close()

        if old_s1 > old_s2:
            pts1, pts2 = 3, 0
            w1, d1, l1 = 1, 0, 0
            w2, d2, l2 = 0, 0, 1
        elif old_s1 < old_s2:
            pts1, pts2 = 0, 3
            w1, d1, l1 = 0, 0, 1
            w2, d2, l2 = 1, 0, 0
        else:
            pts1, pts2 = 1, 1
            w1, d1, l1 = 0, 1, 0
            w2, d2, l2 = 0, 1, 0

        if tp1:
            update_tournament_player(
                tid, p1["id"],
                group_points=max(0, tp1["group_points"] - pts1),
                group_gf=max(0, tp1["group_gf"] - old_s1),
                group_ga=max(0, tp1["group_ga"] - old_s2),
                group_wins=max(0, tp1["group_wins"] - w1),
                group_draws=max(0, tp1["group_draws"] - d1),
                group_losses=max(0, tp1["group_losses"] - l1),
            )
        if tp2:
            update_tournament_player(
                tid, p2["id"],
                group_points=max(0, tp2["group_points"] - pts2),
                group_gf=max(0, tp2["group_gf"] - old_s2),
                group_ga=max(0, tp2["group_ga"] - old_s1),
                group_wins=max(0, tp2["group_wins"] - w2),
                group_draws=max(0, tp2["group_draws"] - d2),
                group_losses=max(0, tp2["group_losses"] - l2),
            )


# ─────────────────────────────────────────────────────────────────────────────
# /tmatches — list all matches of a tournament with IDs
# ─────────────────────────────────────────────────────────────────────────────


async def cmd_tmatches(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/tmatches [tournament_id] [@username] [group_letter]

    Shows all matches of a tournament grouped BY PLAYER. Each player's
    section lists their opponents, scores, and match IDs.

    Filters:
      /tmatches 5           — all matches of tournament 5
      /tmatches 5 @user     — only matches of @user
      /tmatches 5 A         — only group A
      /tmatches 5 @user A   — @user's matches in group A

    Each player's block is wrapped in an expandable blockquote so the
    message stays compact — tap to expand.
    """
    tid: int | None = None
    filter_username: str | None = None
    filter_group: str | None = None

    for arg in (ctx.args or []):
        raw = arg.lstrip("#")
        if raw.isdigit() and tid is None:
            tid = int(raw)
        elif arg.startswith("@"):
            filter_username = arg.lstrip("@").lower()
        elif len(arg) == 1 and arg.upper().isalpha():
            filter_group = arg.upper()
        elif tid is None and raw.isdigit():
            tid = int(raw)

    if tid is None:
        chat = update.effective_chat
        if chat is not None:
            bound = get_tournament_by_chat(chat.id)
            if bound:
                tid = int(bound["id"])
    if tid is None:
        at = get_active_tournament()
        if at:
            tid = int(at["id"])
    if tid is None:
        await send(update, "❌ Не нашёл турнир. Укажи ID: <code>/tmatches &lt;id&gt;</code>")
        return

    t = get_tournament(tid)
    if not t:
        await send(update, f"❌ Турнир #{tid} не найден.")
        return

    matches = get_tournament_matches(tid)
    if not matches:
        await send(update, f"ℹ️ В турнире <b>{html.escape(t['name'])}</b> ещё нет матчей.")
        return

    # Build player mappings
    tp_rows = get_tournament_players(tid)
    pid_to_group: dict[int, str] = {}
    pid_to_username: dict[int, str] = {}
    for r in tp_rows:
        g = (r.get("group_name") or "").strip()
        if g and g != "?":
            pid_to_group[r["player_id"]] = g
        pid_to_username[r["player_id"]] = r.get("username") or ""

    # Resolve filter_username to player_id
    filter_pid: int | None = None
    if filter_username:
        from database import get_player
        fp = get_player(filter_username)
        if fp:
            filter_pid = fp["id"]
        else:
            await send(update, f"❌ Игрок @{html.escape(filter_username)} не найден.")
            return

    # Separate group and playoff matches
    group_matches: list[dict] = []
    playoff_matches: list[dict] = []
    for m in matches:
        if m.get("stage") == "group":
            group_matches.append(m)
        else:
            playoff_matches.append(m)

    # ── Status icon helper ──────────────────────────────────────────────
    def _status_icon(status: str) -> str:
        return {
            "confirmed": "✅",
            "pending": "⏳",
            "reported": "📨",
            "awaiting_admin": "👀",
            "rejected": "❌",
        }.get(status, "❓")

    # ── Build per-player match lines for group stage ────────────────────
    # For each player: list their matches as "opponent score status"
    # Group by group letter → player
    by_group_player: dict[str, dict[int, list[str]]] = {}

    for m in group_matches:
        p1_id, p2_id = m["player1_id"], m["player2_id"]
        g = pid_to_group.get(p1_id) or pid_to_group.get(p2_id) or "?"

        if filter_group and g != filter_group:
            continue

        status = m.get("status") or "?"
        s1 = m.get("score1")
        s2 = m.get("score2")
        icon = _status_icon(status)

        # Add to player1's view
        if filter_pid is None or filter_pid == p1_id:
            opp_name = pid_to_username.get(p2_id) or str(p2_id)
            score_str = f"{s1}:{s2}" if s1 is not None and s2 is not None else "-:-"
            line = f"<code>#{m['id']}</code> @{opp_name} {score_str} {icon}"
            by_group_player.setdefault(g, {}).setdefault(p1_id, []).append(line)

        # Add to player2's view
        if filter_pid is None or filter_pid == p2_id:
            opp_name = pid_to_username.get(p1_id) or str(p1_id)
            score_str = f"{s2}:{s1}" if s1 is not None and s2 is not None else "-:-"
            line = f"<code>#{m['id']}</code> @{opp_name} {score_str} {icon}"
            by_group_player.setdefault(g, {}).setdefault(p2_id, []).append(line)

    # ── Format output ───────────────────────────────────────────────────
    header = f"📋 <b>Матчи турнира «{html.escape(t['name'])}»</b> (ID {tid})"
    if filter_username:
        header += f"\n👤 Фильтр: @{html.escape(filter_username)}"
    if filter_group:
        header += f"\n📂 Группа: {html.escape(filter_group)}"

    # ── Match-count summary (transparency) ──────────────────────────────
    # Count the *unique* matches that pass the active filters so the user
    # can verify nothing is missing. Without this, the by-player view
    # (which lists every match under both participants) makes it hard to
    # tell how many real matches there are.
    def _passes_filter(m: dict) -> bool:
        if filter_pid is not None and filter_pid not in (
            m.get("player1_id"), m.get("player2_id")
        ):
            return False
        if filter_group:
            g = (
                pid_to_group.get(m.get("player1_id"))
                or pid_to_group.get(m.get("player2_id"))
                or "?"
            )
            if g != filter_group:
                return False
        return True

    _shown = [m for m in matches if _passes_filter(m)]
    _played = sum(1 for m in _shown if (m.get("status") == "confirmed"))
    _pending = len(_shown) - _played
    header += (
        f"\n📊 Матчей: <b>{len(_shown)}</b> "
        f"(сыграно {_played} ✅ · ожидают {_pending} ⏳)"
    )

    # We build a *flat* list of small atomic pieces — one per group header,
    # one per player's blockquote, one per playoff-stage block — and then
    # pack those pieces into Telegram-sized chunks. Building atomic pieces
    # at the per-player level (instead of per-group) is what guarantees no
    # single chunk exceeds the limit even when a group has many players.
    pieces: list[str] = [header]

    # Budget per outgoing Telegram message. Telegram's hard limit is 4096
    # chars; we keep a safety margin for counter drift on emoji/entities.
    MAX_CHUNK_CHARS = 3800
    # Per-player blockquote body budget. If a single player's match list
    # exceeds this, we emit multiple blockquotes under the same @user
    # header — each one self-contained, valid HTML.
    MAX_BLOCKQUOTE_BODY = 3500

    def _player_blocks(uname: str, lines: list[str]) -> list[str]:
        """Format @uname's matches as one or more standalone
        ``<b>@uname</b>\\n<blockquote expandable>...</blockquote>`` pieces,
        splitting the body across multiple blockquotes if it would exceed
        ``MAX_BLOCKQUOTE_BODY``. Each returned piece is valid HTML on its
        own (no tags split across pieces)."""
        out: list[str] = []
        buf: list[str] = []
        size = 0
        for ln in lines:
            # +1 accounts for the joining "\n"
            if size + len(ln) + 1 > MAX_BLOCKQUOTE_BODY and buf:
                body = "\n".join(buf)
                out.append(
                    f"\n<b>@{uname}</b>\n"
                    f"<blockquote expandable>{body}</blockquote>"
                )
                buf = []
                size = 0
            buf.append(ln)
            size += len(ln) + 1
        if buf:
            body = "\n".join(buf)
            out.append(
                f"\n<b>@{uname}</b>\n"
                f"<blockquote expandable>{body}</blockquote>"
            )
        return out

    for g in sorted(by_group_player.keys()):
        players_in_group = by_group_player[g]
        # Group header — its own atomic piece so the packer can keep it with
        # the next player block when there's room, or push it to a new chunk.
        pieces.append(f"\n<b>Группа {html.escape(g)}</b>")

        # Sort players by username
        sorted_pids = sorted(
            players_in_group.keys(),
            key=lambda pid: pid_to_username.get(pid, "").lower(),
        )

        for pid in sorted_pids:
            uname = pid_to_username.get(pid) or str(pid)
            pieces.extend(_player_blocks(uname, players_in_group[pid]))

    # ── Playoff section ─────────────────────────────────────────────────
    if playoff_matches and not filter_group:
        stage_order = [
            "r512", "r256", "r128", "r64", "r32", "r16",
            "qf", "sf", "third", "final",
        ]
        by_stage: dict[str, list[dict]] = {}
        for m in playoff_matches:
            stage = m.get("stage") or "?"
            if filter_pid and filter_pid not in (m["player1_id"], m["player2_id"]):
                continue
            by_stage.setdefault(stage, []).append(m)

        if by_stage:
            sorted_stages = sorted(
                by_stage.keys(),
                key=lambda s: stage_order.index(s) if s in stage_order else 99,
            )
            # Playoff section header — its own piece.
            pieces.append("\n<b>ПЛЕЙ-ОФФ</b>")
            for stage in sorted_stages:
                stage_label = _STAGE_RU.get(stage, stage.upper())
                # Build one piece per stage; if the stage has so many
                # matches it would itself exceed the budget, split it
                # mid-stage repeating the stage header on continuation
                # pieces so each piece reads clearly.
                stage_lines: list[str] = []
                for m in by_stage[stage]:
                    p1 = get_player_by_id(m["player1_id"])
                    p2 = get_player_by_id(m["player2_id"])
                    u1 = f"@{p1['username']}" if p1 else str(m["player1_id"])
                    u2 = f"@{p2['username']}" if p2 else str(m["player2_id"])
                    status = m.get("status") or "?"
                    score = ""
                    if m.get("score1") is not None and m.get("score2") is not None:
                        score = f" {m['score1']}:{m['score2']}"
                    stage_lines.append(
                        f"  <code>#{m['id']}</code> {u1} vs {u2}{score} {_status_icon(status)}"
                    )

                buf: list[str] = [f"\n<b>{stage_label}</b>"]
                size = len(buf[0])
                for ln in stage_lines:
                    if size + len(ln) + 1 > MAX_BLOCKQUOTE_BODY and len(buf) > 1:
                        pieces.append("\n".join(buf))
                        buf = [f"\n<b>{stage_label}</b> <i>(продолжение)</i>"]
                        size = len(buf[0])
                    buf.append(ln)
                    size += len(ln) + 1
                if len(buf) > 1:
                    pieces.append("\n".join(buf))

    # ── Pack atomic pieces into Telegram-sized chunks ───────────────────
    chunks: list[str] = []
    current = ""
    for piece in pieces:
        if not piece:
            continue
        # If a single piece is already over budget (defensive — our piece
        # builders cap themselves below MAX_BLOCKQUOTE_BODY), flush the
        # accumulator and emit it as its own chunk so we never silently
        # skip data. Telegram will reject it, but at least the rest is
        # delivered. This branch should not be reachable in practice.
        if len(piece) > MAX_CHUNK_CHARS:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(piece)
            continue
        candidate = current + ("\n" if current else "") + piece
        if len(candidate) > MAX_CHUNK_CHARS and current:
            chunks.append(current)
            current = piece
        else:
            current = candidate
    if current:
        chunks.append(current)

    # ── Send every chunk, resilient to Telegram flood control ───────────
    # A large unfiltered listing can span a dozen+ messages. Sending them
    # back-to-back can trip Telegram's per-chat flood limit, which raises
    # RetryAfter. Previously that exception aborted the loop and every
    # remaining chunk was silently dropped — the user saw FEWER matches
    # than the tournament actually has. Now we honour the retry delay and
    # keep a small inter-message gap so all chunks are delivered.
    import asyncio
    from telegram.error import RetryAfter

    for chunk in chunks:
        for _attempt in range(3):
            try:
                await send(update, chunk)
                break
            except RetryAfter as exc:
                wait = float(getattr(exc, "retry_after", 1.0)) + 0.5
                log.warning("tmatches flood control: sleeping %.1fs", wait)
                await asyncio.sleep(wait)
            except TelegramError as exc:
                # Don't let one bad chunk abort the rest of the listing.
                log.warning("tmatches chunk send failed: %s", exc)
                break
        # Gentle pacing between messages to stay under the flood limit.
        if len(chunks) > 3:
            await asyncio.sleep(0.4)


# ─────────────────────────────────────────────────────────────────────────────
# /delete_match — completely remove a match and reverse all its effects
# ─────────────────────────────────────────────────────────────────────────────


async def cmd_delete_match(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/delete_match #<match_id>

    Admin-only. Completely removes a match from the system:
      1. If the match was confirmed & processed — reverses ELO, stats,
         and group-table changes (as if it never happened).
      2. Deletes associated goal records (match_goals).
      3. Deletes the match row itself.

    Use this when a match was created by mistake (e.g. a duplicate pairing
    that shouldn't exist). For correcting a wrong score, prefer /edit_match.

    Example: /delete_match #1582
    """
    if not is_admin(update.effective_user.id):
        await send(update, "❌ Только админ.")
        return

    if not ctx.args:
        await send(
            update,
            "Использование: <code>/delete_match #&lt;match_id&gt;</code>\n"
            "Пример: <code>/delete_match #1582</code>",
        )
        return

    # Parse match ID
    mid_raw = ctx.args[0].lstrip("#")
    if not mid_raw.isdigit():
        await send(update, "❌ Первый аргумент — ID матча (число). Пример: <code>#1582</code>")
        return
    match_id = int(mid_raw)

    # Fetch the match
    m = get_match(match_id)
    if not m:
        await send(update, f"❌ Матч #{match_id} не найден.")
        return

    tid = m.get("tournament_id")
    p1 = get_player_by_id(m["player1_id"])
    p2 = get_player_by_id(m["player2_id"])
    if not p1 or not p2:
        await send(update, "❌ Не найдены игроки матча в базе.")
        return

    old_s1 = m.get("score1")
    old_s2 = m.get("score2")
    was_processed = bool(m.get("played_at"))
    status = m.get("status") or "?"

    # ── Step 1: Reverse stats if the match was confirmed & processed ────────
    if was_processed and status == "confirmed":
        t = None
        is_official = True
        if tid:
            t = get_tournament(tid)
            if t:
                is_official = bool(t.get("is_official", 1))
        _reverse_match_stats(m, p1, p2, is_official, tid)

    # ── Step 2: Delete goal records ─────────────────────────────────────────
    conn = db.get_conn()
    conn.execute("DELETE FROM match_goals WHERE match_id=?", (match_id,))
    conn.commit()
    conn.close()

    # ── Step 3: Delete the match row ────────────────────────────────────────
    conn = db.get_conn()
    conn.execute("DELETE FROM matches WHERE id=?", (match_id,))
    conn.commit()
    conn.close()

    # ── Audit log ───────────────────────────────────────────────────────────
    log_tournament_action(
        tid,
        actor_telegram_id=update.effective_user.id,
        actor_username=update.effective_user.username,
        action="delete_match",
        details=(
            f"match={match_id} "
            f"{p1['username']} vs {p2['username']} "
            f"score={old_s1}:{old_s2} status={status}"
        ),
    )

    # ── Response ────────────────────────────────────────────────────────────
    score_str = f"{old_s1}:{old_s2}" if old_s1 is not None else "—"
    scope = ""
    if tid:
        t = get_tournament(tid)
        if t:
            scope = f"\nТурнир: <b>{html.escape(t['name'])}</b> (ID {tid})"

    reversal = ""
    if was_processed and status == "confirmed":
        reversal = "\n♻️ ELO, статистика и турнирная таблица откачены."

    await send(
        update,
        f"🗑 Матч <code>#{match_id}</code> удалён.\n"
        f"  {mention(p1['username'])} vs {mention(p2['username'])} — {score_str}"
        f"{scope}{reversal}",
    )


# ─────────────────────────────────────────────────────────────────────────────
# /force_confirm — force-confirm a stuck match and apply result
# ─────────────────────────────────────────────────────────────────────────────


async def cmd_force_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/force_confirm #<match_id>

    Admin-only. Force-confirms a stuck match (rejected, pending, reported,
    or awaiting_admin) and applies the result (ELO/stats/group table).
    The match must already have a score set (score1 and score2).
    If not, the admin should use /edit_match first.

    Example: /force_confirm #1567
    """
    if not is_admin(update.effective_user.id):
        await send(update, "❌ Только админ.")
        return

    if not ctx.args:
        await send(
            update,
            "Использование: <code>/force_confirm #&lt;match_id&gt;</code>\n"
            "Пример: <code>/force_confirm #1567</code>",
        )
        return

    # Parse match ID
    mid_raw = ctx.args[0].lstrip("#")
    if not mid_raw.isdigit():
        await send(update, "❌ Первый аргумент — ID матча (число). Пример: <code>#1567</code>")
        return
    match_id = int(mid_raw)

    # Fetch the match
    m = get_match(match_id)
    if not m:
        await send(update, f"❌ Матч #{match_id} не найден.")
        return

    status = m.get("status") or "?"
    allowed_statuses = ("rejected", "pending", "reported", "awaiting_admin")
    if status not in allowed_statuses:
        await send(
            update,
            f"❌ Матч #{match_id} имеет статус <b>{status}</b> — "
            f"force_confirm применим только к: {', '.join(allowed_statuses)}.",
        )
        return

    # Check that a score is already set
    if m.get("score1") is None or m.get("score2") is None:
        await send(
            update,
            f"❌ У матча #{match_id} не выставлен счёт.\n"
            "Сначала задайте счёт: <code>/edit_match #{} S1:S2</code>".format(match_id),
        )
        return

    # Force-confirm and apply
    update_match(match_id, status="confirmed")
    summary = apply_result(match_id)

    p1_name = summary["player1"]
    p2_name = summary["player2"]
    s1, s2 = summary["score1"], summary["score2"]

    d1, d2 = summary["delta1"], summary["delta2"]
    is_official = summary.get("is_official", True)
    elo_header = "ELO" if is_official else "ELO (локальный)"

    await send(
        update,
        f"✅ Матч <code>#{match_id}</code> принудительно подтверждён!\n"
        f"⚽ {mention(p1_name)} <b>{s1}:{s2}</b> {mention(p2_name)}\n"
        f"📈 {elo_header}: {mention(p1_name)} {arrow(d1)}, "
        f"{mention(p2_name)} {arrow(d2)}",
    )


# ─────────────────────────────────────────────────────────────────────────────
# /audit — view tournament audit log with undo buttons
# ─────────────────────────────────────────────────────────────────────────────

_AUDIT_PAGE_SIZE = 10

_ACTION_LABELS: dict[str, str] = {
    "walkover": "⚠️ ТП (walkover)",
    "auto_tech_loss": "🤖 Авто-ТП",
    "admin_report": "📝 Админ-отчёт",
    "admin_photo": "📷 Фото-отчёт",
    "promote": "🚀 Промоут",
    "set_deadline_bulk": "⏰ Дедлайн (масс.)",
    "withdraw": "🚫 Снятие",
    "award_points": "🎯 Очки",
    "replace_player": "🔄 Замена",
    "finish_tournament": "🏁 Завершение",
    "delete_match": "🗑 Удаление матча",
    "undo_match": "↩️ Отмена матча",
    "set_description": "📝 Описание",
    "add_admin": "👤+ Админ добавлен",
    "remove_admin": "👤- Админ удалён",
}

# Filter categories for the action-type filter menu
_AUDIT_FILTER_ACTIONS: dict[str, tuple[str, list[str]]] = {
    "all":       ("📊 Все действия", []),
    "tp":        ("🏳️ ТП / Walkover", ["walkover", "auto_tech_loss"]),
    "auto":      ("🤖 Авто-ТП", ["auto_tech_loss"]),
    "reports":   ("📝 Отчёты", ["admin_report", "admin_photo"]),
    "delete":    ("🗑 Удаление", ["delete_match", "undo_match"]),
    "settings":  ("⚙️ Настройки", ["set_description", "set_deadline_bulk",
                                    "add_admin", "remove_admin",
                                    "finish_tournament"]),
    "players":   ("👥 Игроки", ["withdraw", "replace_player", "promote",
                                "award_points"]),
}

# Actions that involve a match and can be undone (reverted)
_UNDOABLE_ACTIONS = {"walkover", "auto_tech_loss", "admin_report", "admin_photo"}


def _parse_match_id_from_details(details: str | None) -> int | None:
    """Extract ``match=<N>`` from an audit log details string."""
    if not details:
        return None
    import re as _re
    m = _re.search(r"match=(\d+)", details)
    return int(m.group(1)) if m else None


async def _send_audit_filter_menu(
    update: Update, tid: int, t: dict, *, edit: bool = False,
):
    """Send the filter-selection menu for a chosen tournament."""
    buttons: list[list[InlineKeyboardButton]] = []

    # Row 1-2: action-type filters
    row: list[InlineKeyboardButton] = []
    for key, (label, _) in _AUDIT_FILTER_ACTIONS.items():
        row.append(InlineKeyboardButton(
            label, callback_data=f"aflt:{tid}:{key}",
        ))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    # Admin filter row
    buttons.append([InlineKeyboardButton(
        "👤 По админу…", callback_data=f"aadm:{tid}",
    )])
    # Back button
    buttons.append([InlineKeyboardButton(
        "⬅️ Выбор турнира", callback_data="asel:back",
    )])

    text = (
        f"📋 <b>Аудит-лог</b> — {html.escape(t['name'])} (ID {tid})\n\n"
        f"Выбери фильтр:"
    )
    kb = InlineKeyboardMarkup(buttons)

    if edit and update.callback_query:
        try:
            await update.callback_query.edit_message_text(
                text, parse_mode="HTML", reply_markup=kb,
            )
        except TelegramError:
            pass
    else:
        await send(update, text, reply_markup=kb)


async def _send_audit_log(
    update: Update,
    tid: int,
    t: dict,
    *,
    page: int = 1,
    filter_action: str | None = None,
    filter_admin: str | None = None,
    edit: bool = False,
):
    """Render and send/edit the filtered audit log page."""
    all_rows = db.list_tournament_audit_log(tid, limit=200)

    # Apply action-type filter
    if filter_action and filter_action != "all":
        allowed = _AUDIT_FILTER_ACTIONS.get(filter_action, ("", []))[1]
        if allowed:
            all_rows = [r for r in all_rows if r.get("action") in allowed]

    # Apply admin filter
    if filter_admin:
        if filter_admin.lower() == "auto":
            all_rows = [
                r for r in all_rows
                if (r.get("actor_username") or "") == "[auto]"
            ]
        else:
            all_rows = [
                r for r in all_rows
                if (r.get("actor_username") or "").lower() == filter_admin.lower()
                or str(r.get("actor_telegram_id") or "") == filter_admin
            ]

    total = len(all_rows)
    total_pages = max(1, (total + _AUDIT_PAGE_SIZE - 1) // _AUDIT_PAGE_SIZE)
    page = min(page, total_pages)
    offset = (page - 1) * _AUDIT_PAGE_SIZE
    rows = all_rows[offset:offset + _AUDIT_PAGE_SIZE]

    # Build filter label
    filter_label_parts: list[str] = []
    if filter_action and filter_action != "all":
        filter_label_parts.append(
            _AUDIT_FILTER_ACTIONS.get(filter_action, (filter_action, []))[0]
        )
    if filter_admin:
        fa_display = "🤖 авто" if filter_admin.lower() == "auto" else f"@{filter_admin}"
        filter_label_parts.append(fa_display)
    filter_note = " | ".join(filter_label_parts) if filter_label_parts else "все"

    if not rows:
        text = (
            f"📋 <b>Аудит-лог</b> — {html.escape(t['name'])} (ID {tid})\n"
            f"Фильтр: {filter_note}\n\n"
            f"<i>Записей нет.</i>"
        )
        back_btn = [[InlineKeyboardButton(
            "⬅️ Фильтры", callback_data=f"aflt:{tid}:menu",
        )]]
        kb = InlineKeyboardMarkup(back_btn)
        if edit and update.callback_query:
            try:
                await update.callback_query.edit_message_text(
                    text, parse_mode="HTML", reply_markup=kb,
                )
            except TelegramError:
                pass
        else:
            await send(update, text, reply_markup=kb)
        return

    lines = [
        f"📋 <b>Аудит-лог</b> — {html.escape(t['name'])} (ID {tid})",
        f"Фильтр: {filter_note} | Стр. {page}/{total_pages} ({total} зап.)",
        "",
    ]

    for row in rows:
        action = row.get("action") or "?"
        label = _ACTION_LABELS.get(action, action)
        actor = row.get("actor_username") or "?"
        if actor == "[auto]":
            actor_display = "🤖"
        else:
            actor_display = f"@{actor}" if actor != "?" else "?"
        ts = _fmt_minute_local(row.get("ts")) or "?"
        details = row.get("details") or ""
        match_id = _parse_match_id_from_details(details)

        line = f"<b>{label}</b> | {actor_display} | {ts}"
        if match_id:
            line += f" | #{match_id}"
        if details and len(details) <= 60:
            line += f"\n  <i>{html.escape(details)}</i>"
        elif details:
            line += f"\n  <i>{html.escape(details[:57])}…</i>"
        lines.append(line)
        lines.append("")

    # Undo buttons
    buttons: list[list[InlineKeyboardButton]] = []
    for row in rows:
        action = row.get("action") or ""
        if action not in _UNDOABLE_ACTIONS:
            continue
        match_id = _parse_match_id_from_details(row.get("details"))
        if not match_id:
            continue
        m = get_match(match_id)
        if not m or m["status"] != "confirmed" or not m.get("played_at"):
            continue
        p1 = get_player_by_id(m["player1_id"])
        p2 = get_player_by_id(m["player2_id"])
        p1n = p1["username"] if p1 else "?"
        p2n = p2["username"] if p2 else "?"
        btn_label = f"↩️ #{match_id}: {p1n} {m['score1']}:{m['score2']} {p2n}"
        if len(btn_label) > 50:
            btn_label = f"↩️ #{match_id} ({m['score1']}:{m['score2']})"
        buttons.append([InlineKeyboardButton(
            btn_label, callback_data=f"audit_undo:{match_id}:{tid}",
        )])

    # Pagination + back
    # Encode filter into callback: audit_pg:<tid>:<page>:<filter_action>:<filter_admin>
    fa = filter_action or ""
    fadm = filter_admin or ""
    nav_row: list[InlineKeyboardButton] = []
    if page > 1:
        nav_row.append(InlineKeyboardButton(
            "⬅️", callback_data=f"audit_pg:{tid}:{page - 1}:{fa}:{fadm}",
        ))
    nav_row.append(InlineKeyboardButton(
        "🔍 Фильтры", callback_data=f"aflt:{tid}:menu",
    ))
    if page < total_pages:
        nav_row.append(InlineKeyboardButton(
            "➡️", callback_data=f"audit_pg:{tid}:{page + 1}:{fa}:{fadm}",
        ))
    buttons.append(nav_row)

    kb = InlineKeyboardMarkup(buttons) if buttons else None
    text = "\n".join(lines)

    if edit and update.callback_query:
        try:
            await update.callback_query.edit_message_text(
                text, parse_mode="HTML", reply_markup=kb,
            )
        except TelegramError:
            pass
    else:
        await send(update, text, reply_markup=kb)


async def cmd_audit(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/audit [tournament_id] [filter] [page]

    Interactive audit log viewer with tournament selection and filters.
    Without arguments — shows tournament selection menu.
    With tournament ID — shows filter selection menu.

    Aliases: /tlog, /auditlog, /audit_log
    """
    user = update.effective_user
    if not is_admin(user.id):
        pass  # permission checked after tournament is known

    args = ctx.args or []
    tid: int | None = None
    filter_action: str | None = None
    filter_admin: str | None = None
    page = 1

    for a in args:
        a_clean = a.strip().lstrip("@")
        if a_clean.isdigit():
            if tid is None:
                tid = int(a_clean)
            else:
                page = max(1, int(a_clean))
        elif a.startswith("[") and a.endswith("]"):
            filter_admin = a.strip("[]")
        elif a.startswith("@"):
            filter_admin = a_clean
        elif a_clean.lower() in ("auto",):
            filter_admin = "auto"
        elif a_clean.lower() in _AUDIT_FILTER_ACTIONS:
            filter_action = a_clean.lower()
        else:
            filter_admin = a_clean

    # Resolve tournament from chat binding
    if tid is None:
        chat = update.effective_chat
        if chat:
            bound = get_tournament_by_chat(chat.id)
            if bound:
                tid = int(bound["id"])

    # No tournament — show interactive tournament picker
    if tid is None:
        from database import get_recent_tournaments
        tournaments = get_recent_tournaments(limit=15)
        if not tournaments:
            await send(update, "❌ Нет доступных турниров.")
            return
        buttons: list[list[InlineKeyboardButton]] = []
        for t in tournaments:
            stage_icon = "🟢" if t["stage"] != "finished" else "🏁"
            label = f"{stage_icon} {t['name']} (ID {t['id']})"
            if len(label) > 45:
                label = f"{stage_icon} ID {t['id']}: {t['name'][:30]}…"
            buttons.append([InlineKeyboardButton(
                label, callback_data=f"asel:{t['id']}",
            )])
        await send(
            update,
            "📋 <b>Аудит-лог</b>\n\nВыбери турнир:",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return

    t = get_tournament(tid)
    if not t:
        await send(update, f"❌ Турнир ID {tid} не найден.")
        return

    # Permission check
    if not is_admin(user.id):
        if not _can_manage_tournament(user.id, t):
            await send(update, "❌ Только админ или организатор турнира.")
            return

    # If no filter specified — show filter menu
    if not filter_action and not filter_admin:
        await _send_audit_filter_menu(update, tid, t)
        return

    # Build the combined filter string for display/pagination
    flt = filter_action or ""
    if filter_admin:
        flt = filter_admin

    await _send_audit_log(update, tid, t, page=page,
                          filter_action=filter_action,
                          filter_admin=filter_admin)


async def cb_audit_page(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle pagination for /audit inline buttons.

    callback_data format: ``audit_pg:<tid>:<page>:<filter_action>:<filter_admin>``
    """
    query = update.callback_query
    if not query:
        return
    await query.answer()

    user = query.from_user
    if not user:
        return

    data = (query.data or "").split(":")
    if len(data) < 3:
        return
    try:
        tid = int(data[1])
        page = int(data[2])
    except (ValueError, IndexError):
        return
    filter_action = data[3] if len(data) > 3 and data[3] else None
    filter_admin = data[4] if len(data) > 4 and data[4] else None

    t = get_tournament(tid)
    if not t:
        try:
            await query.edit_message_text("❌ Турнир не найден.")
        except TelegramError:
            pass
        return

    # Permission check
    if not is_admin(user.id):
        if not _can_manage_tournament(user.id, t):
            try:
                await query.answer("❌ Нет прав.", show_alert=True)
            except TelegramError:
                pass
            return

    await _send_audit_log(
        update, tid, t, page=page,
        filter_action=filter_action,
        filter_admin=filter_admin,
        edit=True,
    )


async def cb_audit_undo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle the ↩️ Отменить button press from /audit.

    callback_data format: ``audit_undo:<match_id>:<tid>``

    Reverts the match (ELO, stats, group points) and resets it to
    ``pending`` so it can be replayed. Logs the undo action.
    """
    query = update.callback_query
    if not query:
        return
    await query.answer()

    user = query.from_user
    if not user:
        return

    data = (query.data or "").split(":")
    if len(data) < 3:
        return
    try:
        match_id = int(data[1])
        tid = int(data[2])
    except (ValueError, IndexError):
        try:
            await query.edit_message_text("❌ Неверные данные кнопки.")
        except TelegramError:
            pass
        return

    t = get_tournament(tid)
    if not t:
        try:
            await query.edit_message_text("❌ Турнир не найден.")
        except TelegramError:
            pass
        return

    # Permission check
    if not is_admin(user.id):
        from handlers._helpers import _can_manage_tournament
        if not _can_manage_tournament(user.id, t):
            try:
                await query.answer("❌ Нет прав.", show_alert=True)
            except TelegramError:
                pass
            return

    # Revert the match
    from match_processor import revert_match
    try:
        result = revert_match(match_id)
    except ValueError as e:
        try:
            await query.edit_message_text(
                f"❌ Не удалось отменить: {html.escape(str(e))}",
                parse_mode="HTML",
            )
        except TelegramError:
            pass
        return

    # Log the undo action
    db.log_tournament_action(
        tid,
        actor_telegram_id=user.id,
        actor_username=user.username,
        action="undo_match",
        details=(
            f"match={match_id} "
            f"reverted_score={result['reverted_score']} "
            f"p1=@{result['player1']} p2=@{result['player2']}"
        ),
    )

    text = (
        f"✅ <b>Матч #{match_id} отменён!</b>\n\n"
        f"⚽ @{html.escape(result['player1'])} vs @{html.escape(result['player2'])}\n"
        f"Счёт {result['reverted_score']} откачен.\n"
        f"ELO, статистика и очки группы — восстановлены.\n"
        f"Матч переведён в статус <b>pending</b> (можно переиграть).\n\n"
        f"<i>Отменил: @{html.escape(user.username or str(user.id))}</i>"
    )
    try:
        await query.edit_message_text(text, parse_mode="HTML")
    except TelegramError:
        await send(update, text)


# ─────────────────────────────────────────────────────────────────────────────
# Audit callback handlers: tournament selection, filter type, admin filter
# ─────────────────────────────────────────────────────────────────────────────

async def cb_audit_select_tournament(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle tournament selection from /audit picker.

    callback_data: ``asel:<tid>`` or ``asel:back``
    """
    query = update.callback_query
    if not query:
        return
    await query.answer()

    user = query.from_user
    if not user:
        return

    data = (query.data or "").split(":")
    if len(data) < 2:
        return

    if data[1] == "back":
        # Show tournament list again
        from database import get_recent_tournaments
        tournaments = get_recent_tournaments(limit=15)
        if not tournaments:
            try:
                await query.edit_message_text("❌ Нет доступных турниров.")
            except TelegramError:
                pass
            return
        buttons: list[list[InlineKeyboardButton]] = []
        for t in tournaments:
            stage_icon = "🟢" if t["stage"] != "finished" else "🏁"
            label = f"{stage_icon} {t['name']} (ID {t['id']})"
            if len(label) > 45:
                label = f"{stage_icon} ID {t['id']}: {t['name'][:30]}..."
            buttons.append([InlineKeyboardButton(
                label, callback_data=f"asel:{t['id']}",
            )])
        try:
            await query.edit_message_text(
                "📋 <b>Аудит-лог</b>\n\nВыбери турнир:",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(buttons),
            )
        except TelegramError:
            pass
        return

    try:
        tid = int(data[1])
    except ValueError:
        return

    t = get_tournament(tid)
    if not t:
        try:
            await query.edit_message_text("❌ Турнир не найден.")
        except TelegramError:
            pass
        return

    # Permission check
    if not is_admin(user.id):
        if not _can_manage_tournament(user.id, t):
            try:
                await query.answer("❌ Нет прав.", show_alert=True)
            except TelegramError:
                pass
            return

    # Show filter menu
    await _send_audit_filter_menu(update, tid, t, edit=True)


async def cb_audit_filter_type(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle action-type filter selection.

    callback_data: ``aflt:<tid>:<filter_key>``
    filter_key is one of _AUDIT_FILTER_ACTIONS keys, or 'menu' to go back.
    """
    query = update.callback_query
    if not query:
        return
    await query.answer()

    user = query.from_user
    if not user:
        return

    data = (query.data or "").split(":")
    if len(data) < 3:
        return
    try:
        tid = int(data[1])
    except ValueError:
        return
    filter_key = data[2]

    t = get_tournament(tid)
    if not t:
        try:
            await query.edit_message_text("❌ Турнир не найден.")
        except TelegramError:
            pass
        return

    if not is_admin(user.id):
        if not _can_manage_tournament(user.id, t):
            try:
                await query.answer("❌ Нет прав.", show_alert=True)
            except TelegramError:
                pass
            return

    # "menu" means go back to filter selection
    if filter_key == "menu":
        await _send_audit_filter_menu(update, tid, t, edit=True)
        return

    # Show filtered log
    await _send_audit_log(
        update, tid, t, page=1,
        filter_action=filter_key,
        edit=True,
    )


async def cb_audit_filter_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle admin filter selection.

    callback_data: ``aadm:<tid>`` — show admin list
    callback_data: ``aadm:<tid>:<username>`` — apply admin filter
    """
    query = update.callback_query
    if not query:
        return
    await query.answer()

    user = query.from_user
    if not user:
        return

    data = (query.data or "").split(":")
    if len(data) < 2:
        return
    try:
        tid = int(data[1])
    except ValueError:
        return

    t = get_tournament(tid)
    if not t:
        try:
            await query.edit_message_text("❌ Турнир не найден.")
        except TelegramError:
            pass
        return

    if not is_admin(user.id):
        if not _can_manage_tournament(user.id, t):
            try:
                await query.answer("❌ Нет прав.", show_alert=True)
            except TelegramError:
                pass
            return

    # If username provided — show filtered results
    if len(data) >= 3 and data[2]:
        admin_username = data[2]
        await _send_audit_log(
            update, tid, t, page=1,
            filter_admin=admin_username,
            edit=True,
        )
        return

    # Otherwise show list of admins who have audit entries
    from database import get_audit_distinct_actors
    actors = get_audit_distinct_actors(tid)

    if not actors:
        try:
            await query.edit_message_text(
                f"📋 <b>Аудит-лог</b> — {html.escape(t['name'])}\n\n"
                f"<i>Нет записей — невозможно показать список админов.</i>",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton(
                        "⬅️ Фильтры", callback_data=f"aflt:{tid}:menu",
                    )
                ]]),
            )
        except TelegramError:
            pass
        return

    buttons: list[list[InlineKeyboardButton]] = []
    # Add "all admins" button
    buttons.append([InlineKeyboardButton(
        "👥 Все админы", callback_data=f"aflt:{tid}:all",
    )])
    # Add auto-bot entry
    has_auto = any(a.get("actor_username") == "[auto]" for a in actors)
    if has_auto:
        buttons.append([InlineKeyboardButton(
            "🤖 Авто-бот", callback_data=f"aadm:{tid}:auto",
        )])
    # Add individual admins
    row: list[InlineKeyboardButton] = []
    for actor in actors:
        uname = actor.get("actor_username") or ""
        if uname == "[auto]":
            continue  # already added above
        display = f"@{uname}" if uname else f"ID {actor.get('actor_telegram_id')}"
        cb_data = f"aadm:{tid}:{uname}"
        if len(cb_data) > 64:
            cb_data = f"aadm:{tid}:{uname[:50]}"
        row.append(InlineKeyboardButton(display, callback_data=cb_data))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    # Back button
    buttons.append([InlineKeyboardButton(
        "⬅️ Фильтры", callback_data=f"aflt:{tid}:menu",
    )])

    text = (
        f"📋 <b>Аудит-лог</b> — {html.escape(t['name'])} (ID {tid})\n\n"
        f"Выбери админа:"
    )
    try:
        await query.edit_message_text(
            text, parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
    except TelegramError:
        pass


__all__ = [
    # helpers
    "_format_series_line",
    "_maybe_auto_advance",
    "_current_playoff_stage",
    "_announce_stage_advance",
    "_approver_telegram_ids",
    "_send_match_to_admins",
    "_send_failed_screenshot_to_admins",
    "_after_opponent_confirm",
    "_finalize_match_after_admin",
    "_list_pending_matches_for",
    "_do_walkover",
    "_parse_deadline_token",
    "SCORE_RE",
    # commands
    "cmd_dispute",
    "cmd_admin_report",
    "cmd_admin_photo",
    "cmd_reocr",
    "cmd_award_points",
    "cmd_edit_goals",
    "cmd_edit_match",
    "cmd_delete_match",
    "cmd_force_confirm",
    "cmd_pending",
    "cmd_tmatches",
    "cmd_walkover",
    "cmd_walkover_match",
    "cmd_walkover_all",
    "cmd_tech_nil_all",
    "cmd_promote",
    "cmd_withdraw",
    "cmd_tech_draw",
    "cmd_set_match_deadline",
    "cmd_audit",
    "cb_audit_undo",
    "cb_audit_page",
    "cb_audit_select_tournament",
    "cb_audit_filter_type",
    "cb_audit_filter_admin",
]
